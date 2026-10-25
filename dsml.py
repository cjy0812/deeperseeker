"""The DeepSeek tool-call dialect: one parser for text that should have been JSON.

DeepSeek writes tool calls as markup in the prose stream instead of emitting
structured ``tool_calls``. The model is also inconsistent about that markup --
DSML-marked or bare, full-width ``｜｜`` or ASCII ``||``, ``invoke`` or
``tool_calls`` or ``tool_use``, ``string="true"`` typed parameters, closers used
as openers -- so recognition is a pile of tolerant regexes rather than a grammar.

Two consumers, one dialect:

* ``parse_tools`` handles a complete message (non-streaming path, and the
  end-of-turn flush).
* ``StreamToolParser`` is the incremental state machine used while the upstream
  SSE stream is still arriving.

Both share the truncation rule, which is the important part: a missing outer
wrapper closer is NORMAL model behaviour and not evidence of a cut. Only a
last ``<parameter>`` left open, or a JSON payload whose braces were cut, means
the cut landed inside the arguments -- and then the visible args are a prefix
of the real ones, so the call must be dropped rather than forwarded (the client
would report "required parameter missing").

This module deliberately imports nothing from ``functions``: it is pure
string/regex work plus one ContextVar, so it can be read and tested alone.
"""

import contextlib
import contextvars
import json
import random
import re
import string


def normalize_tool_call(tool_data_or_name, args_if_name=None):
    if isinstance(tool_data_or_name, str):
        name = tool_data_or_name
        args = args_if_name if args_if_name is not None else {}
    elif isinstance(tool_data_or_name, dict):
        tool_data = tool_data_or_name
        if "function" in tool_data and isinstance(tool_data["function"], dict):
            fn = tool_data["function"]
            name = fn.get("name") or tool_data.get("name")
            args = (
                fn.get("arguments")
                or fn.get("parameters")
                or fn.get("input")
                or fn.get("args")
                or fn.get("params")
                or {}
            )
        else:
            name = (
                tool_data.get("name")
                or tool_data.get("tool")
                or tool_data.get("tool_name")
                or tool_data.get("function")
                or tool_data.get("action")
            )
            args = (
                tool_data.get("arguments")
                or tool_data.get("parameters")
                or tool_data.get("input")
                or tool_data.get("args")
                or tool_data.get("params")
                or tool_data.get("tool_input")
                or tool_data.get("action_input")
                or {}
            )
    else:
        return None

    if not name or not isinstance(name, str):
        return None
    if isinstance(args, (dict, list)):
        args_str = json.dumps(args)
    elif isinstance(args, str):
        args_str = args
        try:
            json.loads(args_str)
        except Exception:
            # [FIX 4] An arguments string that is not valid JSON is a truncated
            # or corrupted call. Wrapping it in json.dumps() produced a tool_use
            # whose .input was a string instead of an object, which the client
            # then failed to satisfy ("required parameter missing"). Drop the
            # call instead of forwarding garbage.
            return None
    else:
        args_str = json.dumps({})
    call_id = "call_" + "".join(
        random.choices(string.ascii_letters + string.digits, k=8)
    )
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name.strip(),
            "arguments": args_str,
        },
    }


def clean_json_str(s):
    s = s.strip()
    if s.startswith("```json"):
        s = s[7:]
    elif s.startswith("```"):
        s = s[3:]
    if s.endswith("```"):
        s = s[:-3]
    return s.strip()


def _code_fence_spans(text):
    """Return the (start, end) spans of markdown fenced code blocks.

    Tool-call markup inside a fence is documentation/example text, not an
    actual tool call, so matches falling inside these spans are ignored.
    Unclosed fences extend to end-of-text.
    """
    return [
        (m.start(), m.end()) for m in re.finditer(r"```.*?(?:```|$)", text, re.DOTALL)
    ]


_TOOL_OPEN_FAMILY_RE = re.compile(
    r"<[\uFF5C\|]{0,2}(?:DSML[\uFF5C\|]{0,2})?\s*(?:tool_calls?|calls|function_calls?|invoke|function_call|tool_use)\b[^>]*>",
    re.IGNORECASE,
)


_PARAM_OPEN_RE = re.compile(
    r"<[\uFF5C\|]{0,2}(?:DSML[\uFF5C\|]{0,2})?\s*(?:parameter|param|invoke)"
    r"\s+name=[\x27\x22][^\x27\x22]+[\x27\x22][^>]*>",
    re.IGNORECASE,
)
_PARAM_CLOSE_RE = re.compile(
    r"</[\uFF5C\|]{0,2}(?:DSML[\uFF5C\|]{0,2})?\s*(?:parameter|param|invoke)>",
    re.IGNORECASE,
)


def _payload_is_complete(body):
    if not body.strip():
        return False
    b = body.find("{")
    if b != -1 and not _PARAM_OPEN_RE.search(body[:b]):
        try:
            json.JSONDecoder().raw_decode(body[b:])
            return True
        except ValueError:
            return False
    o = list(_PARAM_OPEN_RE.finditer(body))
    if not o:
        return False
    return _PARAM_CLOSE_RE.search(body, o[-1].end()) is not None


def tool_block_is_truncated(text):
    """True when the LAST tool-call block in *text* never got a closer.

    A truncated block means the upstream stream was cut mid-call, so the
    parameters visible here may be a prefix of the real ones. Callers use
    this to drop the incomplete tail instead of emitting a tool_use with
    missing arguments ("required parameter missing" client-side).
    """
    if not text:
        return False
    fence_spans = _code_fence_spans(text)
    opens = [
        m
        for m in _TOOL_OPEN_FAMILY_RE.finditer(text)
        if not any(a <= m.start() < b for a, b in fence_spans)
    ]
    if not opens:
        return False
    if _TOOL_END_TAG_RE.search(text, opens[-1].end()) is not None:
        return False
    return not _payload_is_complete(text[opens[-1].end() :])


# Openers may come as "</｜｜DSML｜｜ parameter name=...>" (the model mixes up
# open/close), as 'parameter="x"' without name, or without "parameter name=" at all
# ('<｜｜DSML｜｜command" string=...>', '<｜｜DSML｜｜ old_string="true">') ("<｜｜DSML｜｜command" string=...>),
# and a value ends at the proper closer, the next parameter, or ANY DSML marker
# (bare "</｜｜DSML｜｜>", truncated "</｜｜DSML｜｜ ", "</｜｜DSML｜｜ calls>",
# "<"-less "｜｜DSML｜｜parameter>"): markers never belong in a value.
_DSML_PARAM_RE = re.compile(
    r"(?:</?[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:parameter|tool_call|param|invoke)(?:\s+name\s*=|\s*=)\s*[\x27\x22]([^\x27\x22]+)[\x27\x22]"
    r"|</?[｜\|]{1,2}DSML[｜\|]{1,2}\s*(?!(?:parameter|invoke|function_calls?|tool_calls?|calls)\b)([A-Za-z_][\w\-]*)(?=[\x27\x22]|\s*=\s*[\x27\x22](?:true|false)[\x27\x22]))([^>]*)>"
    r"(.*?)(?:</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:parameter|tool_call|param|invoke)>|(?=</?[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:parameter|tool_call|param|invoke)\s+name=)|(?=(?:</?)?[｜\|]{1,2}DSML|</?[｜\|]{0,2}DSML[｜\|])|$)",
    re.DOTALL | re.IGNORECASE,
)


def _dsml_params(body):
    args = {}
    for pm in _DSML_PARAM_RE.finditer(body):
        p_name = (pm.group(1) or pm.group(2)).strip()
        p_val = pm.group(4).strip()
        p_val = re.sub(
            r"</?(?:tool_calls?|invoke|function_call|parameter|param)\b[^>]*>",
            "",
            p_val,
            flags=re.IGNORECASE,
        ).strip()
        # string="true" marks a literal string; don't turn "42"/"true" into JSON.
        # ('<｜｜DSML｜｜old_string="true">' puts the type right after the name.)
        if re.search(r"(?:string\s*=|^\s*=)\s*[\x27\x22]true", pm.group(3), re.IGNORECASE):
            args[p_name] = p_val
            continue
        try:
            args[p_name] = json.loads(p_val)
        except Exception:
            args[p_name] = p_val
    return args


# The request's tool list, set by handle_chat, so parse_tools can recover a call
# whose tool name the model dropped (a function_calls block without an invoke).
request_tools = contextvars.ContextVar("request_tools", default=None)


def _tool_schemas():
    schemas = {}
    for t in request_tools.get() or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name")
        params = fn.get("parameters") or fn.get("input_schema") or {}
        if name and isinstance(params, dict):
            schemas[name] = params
    return schemas


def _infer_tool_name(args, schemas):
    """The one tool whose schema accepts exactly these argument names."""
    # First require every required field; if the model also dropped one of those,
    # still name the one tool that accepts these arguments, so the client reports
    # the missing field instead of an unknown tool.
    for need_required in (True, False):
        fits = []
        for name, params in schemas.items():
            props = set(params.get("properties") or {})
            required = set(params.get("required") or [])
            if set(args) <= props and (not need_required or required <= set(args)):
                fits.append((len(props - set(args)), name))
        fits.sort()
        if fits and (len(fits) == 1 or fits[0][0] < fits[1][0]):
            return fits[0][1]
    return None


def _prune_args(args, params):
    """Drop argument keys *params* never declares; return (args, pruned_count).

    A value that embeds a nested parameter block ends at the inner opener, so
    the rest of the real value is re-read as sibling keys ("command" and
    "description" turning up on a Write). Those are pure residue: the client
    rejects them as "unexpected parameter". Keys are only pruned when the
    schema enumerates properties and does not allow extras, so open schemas
    keep accepting whatever the model sent.
    """
    props = params.get("properties") or {}
    if not props or params.get("additionalProperties") is True:
        return args, 0
    kept = {k: v for k, v in args.items() if k in props}
    return kept, len(args) - len(kept)


def _required_emptied(args, params):
    """True when a required field is missing or empty after pruning residue."""
    for field in params.get("required") or []:
        value = args.get(field)
        if value is None or (isinstance(value, str) and not value.strip()):
            return True
    return False


def _repair_tool_names(tools):
    schemas = _tool_schemas()
    if not schemas:
        return tools
    kept = []
    for t in tools:
        fn = t["function"]
        try:
            args = json.loads(fn["arguments"])
        except Exception:
            args = None
        if isinstance(args, dict):
            if fn["name"] not in schemas:
                fn["name"] = _infer_tool_name(args, schemas) or fn["name"]
            params = schemas.get(fn["name"])
            if isinstance(params, dict):
                pruned, count = _prune_args(args, params)
                if count:
                    # The pruned keys prove the value was cut at an inner
                    # marker. If that left a required field blank, the call
                    # carries a prefix of the real arguments and cannot be
                    # satisfied -- same rule as a stream cut mid-call.
                    if _required_emptied(pruned, params):
                        continue
                    fn["arguments"] = json.dumps(pruned)
        # A name that still is not in the request's tool list names nothing the
        # client has ("No such tool available: function_calls"); dropping the
        # call beats forwarding one that cannot succeed. Empty schemas return
        # above, so the schema-less CLI and test paths are unchanged.
        if fn["name"] in schemas:
            kept.append(t)
    return kept



def parse_tools(text):
    tools = []
    clean_text = text
    fence_spans = _code_fence_spans(text)
    truncated = tool_block_is_truncated(text)

    def fenced(pos):
        return any(s <= pos < e for s, e in fence_spans)

    param_names = {
        "command",
        "description",
        "file_path",
        "content",
        "path",
        "prompt",
        "query",
        "subject",
        "old_string",
        "new_string",
        "url",
        "input",
    }
    # Call starts: invoke openers (also "</…invoke name=…>", the model mixes up
    # open/close), DSML function_calls blocks with no invoke at all, and a bare
    # "<｜｜DSML｜｜invoke>" without a name; their tool name is inferred from the
    # parameters.
    starts = [
        (m, m.group(1).strip())
        for m in re.finditer(
            r"</?[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|invoke|function_calls?)(?:\s+(?:name|tool)\s*=|\s*=)\s*[\x27\x22]([^\x27\x22]+)[\x27\x22][^>]*>",
            text,
            re.IGNORECASE,
        )
    ] + [
        (m, None)
        for m in re.finditer(
            r"<[｜\|]{0,2}DSML[｜\|]{0,2}\s*(?:function_calls?|tool_calls?|calls|invoke)\s*>",
            text,
            re.IGNORECASE,
        )
    ]
    real_tool_matches = sorted(
        (st for st in starts if not fenced(st[0].start())), key=lambda st: st[0].start()
    )

    if real_tool_matches:
        schemas = _tool_schemas()
        for i, (tm, candidate_name) in enumerate(real_tool_matches):
            start_idx = tm.end()
            # 'invoke name="i" string="true">…': the model fused the invoke with its
            # first parameter; read it as a parameter and infer the tool name.
            if (
                candidate_name
                and "string=" in tm.group(0)
                and schemas
                and candidate_name not in schemas
            ):
                start_idx = tm.start()
                candidate_name = None
            end_idx = (
                real_tool_matches[i + 1][0].start()
                if i + 1 < len(real_tool_matches)
                else len(text)
            )
            body = text[start_idx:end_idx]
            args = {}
            args = _dsml_params(body)
            tag_param_matches = re.finditer(
                r"<([A-Za-z0-9_\-]+)>(.*?)(?:</\1>|$)",
                body,
                flags=re.DOTALL | re.IGNORECASE,
            )
            for pm in tag_param_matches:
                t_name = pm.group(1).strip().lower()
                if t_name in param_names:
                    t_val = pm.group(2).strip()
                    t_val = re.sub(
                        r"</?(?:tool_calls?|invoke|function_call|parameter|param)\b[^>]*>",
                        "",
                        t_val,
                        flags=re.IGNORECASE,
                    ).strip()
                    try:
                        args[t_name] = json.loads(t_val)
                    except Exception:
                        args[t_name] = t_val
            # [FIX 2] The trailing block of a cut-off stream is a partial
            # call: its visible args are a prefix, not the real ones. Drop
            # it instead of emitting a tool_use the client cannot satisfy.
            # A wrapper with no name of its own falls back to "function_calls".
            if (candidate_name or args) and not (
                truncated and i == len(real_tool_matches) - 1
            ):
                norm = normalize_tool_call(candidate_name or "function_calls", args)
                if norm:
                    tools.append(norm)

    if tools:
        clean_text = re.sub(
            r"<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls)[^>]*>.*?(?:</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls)>|$)",
            "",
            clean_text,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()
        clean_text = re.sub(
            r"<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:invoke|function_call)[^>]*>.*?(?:</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:invoke|function_call)>|$)",
            "",
            clean_text,
            flags=re.DOTALL | re.IGNORECASE,
        ).strip()

    if not tools and not truncated and "DSML" in text:
        dsml_block_pattern = re.compile(
            r"<[｜\|]{2}DSML[｜\|]{2}([A-Za-z0-9_]+)>(.*?)(?:</[｜\|]{2}DSML[｜\|]{2}\1>|$)",
            re.DOTALL | re.IGNORECASE,
        )
        param_pattern_b = re.compile(
            r"<[｜\|]{2}DSML[｜\|]{2}B([A-Za-z0-9_]+)[^>]*>(.*?)(?:</[｜\|]{2}DSML[｜\|]{2}B.*?>|$)",
            re.DOTALL | re.IGNORECASE,
        )
        for m in dsml_block_pattern.finditer(text):
            if fenced(m.start()):
                continue
            tool_name = m.group(1).strip()
            body = m.group(2)
            args = {}
            for pm in param_pattern_b.finditer(body):
                p_name = pm.group(1).lower().strip()
                p_val = pm.group(2).strip()
                try:
                    args[p_name] = json.loads(p_val)
                except Exception:
                    args[p_name] = p_val
            norm = normalize_tool_call(tool_name, args)
            if norm:
                tools.append(norm)
        if not tools:
            tool_match = re.search(
                r"[｜\|]{2}DSML[｜\|]{2}(Bash|Read|Write|Edit|Agent|TaskList|TaskCreate|WebSearch|[A-Za-z0-9_]+)",
                text,
                re.IGNORECASE,
            )
            if tool_match and not fenced(tool_match.start()):
                candidate = tool_match.group(1).strip()
                tool_name = (
                    "Bash"
                    if candidate.lower().startswith("b")
                    and candidate.lower() not in ["bdescription", "bparam"]
                    else candidate
                )
                args = {}
                cmd_match = re.search(
                    r"[｜\|]{2}B[\x22\x27]?command[\x22\x27]?[^>]*>(.*?)(?:</[｜\|]{2}B|$)",
                    text,
                    re.DOTALL | re.IGNORECASE,
                )
                desc_match = re.search(
                    r"[｜\|]{2}B[\x22\x27]?description[\x22\x27]?[^>]*>(.*?)(?:</[｜\|]{2}B|$)",
                    text,
                    re.DOTALL | re.IGNORECASE,
                )
                if cmd_match:
                    clean_cmd = re.sub(
                        r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", cmd_match.group(1)
                    ).strip("\x22\x27() ")
                    args["command"] = clean_cmd
                if desc_match:
                    clean_desc = re.sub(
                        r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", desc_match.group(1)
                    ).strip("\x22\x27() ")
                    args["description"] = clean_desc
                norm = normalize_tool_call(tool_name, args)
                if norm:
                    tools.append(norm)
        if tools:
            clean_text = re.sub(
                r"<[｜\|]{2}DSML[｜\|]{2}[^>]*>.*?(?:</[｜\|]{2}DSML[｜\|]{2}[^>]*>|$)",
                "",
                clean_text,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()
            clean_text = re.sub(
                r"</?[｜\|]{2}DSML[｜\|]{2}[^>]*>", "", clean_text, flags=re.IGNORECASE
            ).strip()

    if not tools and not truncated:
        # Some upstreams emit <tool_use><name>X</name><arguments>{...}</arguments>
        # </tool_use> instead of <invoke>/<parameter>. Without this branch the
        # call is handed to the client as plain text and never executes.
        tu = re.compile(
            r"<tool_use>\s*<name>([^<]+)</name>\s*<arguments>(.*?)</arguments>\s*</tool_use>",
            re.DOTALL | re.IGNORECASE,
        )
        for m in tu.finditer(text):
            if fenced(m.start()):
                continue
            raw = m.group(2).strip()
            try:
                args = json.loads(raw)
            except Exception:
                args = None
            norm = normalize_tool_call(m.group(1).strip(), args) if args is not None else None
            if norm:
                tools.append(norm)
        if tools:
            clean_text = tu.sub("", clean_text).strip()

    if not tools and not truncated:
        fn_call_pattern = re.compile(
            r"<function_call>\s*<name>([^<]+)</name>\s*<arguments>(.*?)</arguments>\s*</function_call>",
            re.DOTALL | re.IGNORECASE,
        )
        for m in fn_call_pattern.finditer(text):
            if fenced(m.start()):
                continue
            name = m.group(1).strip()
            args_raw = m.group(2).strip()
            try:
                args = json.loads(args_raw)
            except Exception:
                args = args_raw
            norm = normalize_tool_call(name, args)
            if norm:
                tools.append(norm)
        if tools:
            clean_text = re.sub(
                r"<function_call>.*?</function_call>",
                "",
                clean_text,
                flags=re.DOTALL | re.IGNORECASE,
            ).strip()

    if not tools and not truncated:
        tag_regex = re.compile(
            r"<(?:tool_call|function_call)(?:\s+(?:name|tool|function)=[\x27\x22]([^\x27\x22]+)[\x27\x22])?\s*>",
            re.IGNORECASE,
        )
        decoder = json.JSONDecoder()
        matches = [m for m in tag_regex.finditer(text) if not fenced(m.start())]
        if matches:
            for m in matches:
                tag_name = m.group(1)
                after_tag = text[m.end() :]
                brace_pos = after_tag.find("{")
                if brace_pos != -1:
                    json_substr = after_tag[brace_pos:]
                    data = None
                    with contextlib.suppress(Exception):
                        data, _ = decoder.raw_decode(json_substr)
                    if not data:
                        # [FIX 3] raw_decode() rejected this payload, which means
                        # it is truncated or corrupted. Padding it with right
                        # braces used to turn a half-written call into a
                        # syntactically valid one with missing keys, surfacing
                        # client-side as "required parameter missing". Drop it.
                        data = None
                    if isinstance(data, dict):
                        if tag_name:
                            name = tag_name
                            if "arguments" in data and isinstance(
                                data["arguments"], dict
                            ):
                                args = data["arguments"]
                            elif "parameters" in data and isinstance(
                                data["parameters"], dict
                            ):
                                args = data["parameters"]
                            elif "input" in data and isinstance(data["input"], dict):
                                args = data["input"]
                            else:
                                args = {
                                    k: v
                                    for k, v in data.items()
                                    if k not in ["name", "tool", "function"]
                                }
                        else:
                            name = (
                                data.get("name")
                                or data.get("tool")
                                or data.get("tool_name")
                                or data.get("function")
                                or data.get("action")
                            )
                            args = (
                                data.get("arguments")
                                or data.get("parameters")
                                or data.get("input")
                                or data.get("args")
                                or data.get("params")
                                or data.get("tool_input")
                                or data.get("action_input")
                            )
                            if args is None:
                                args = {}
                        if name:
                            norm = normalize_tool_call(name, args)
                            if norm:
                                tools.append(norm)
            clean_text = re.sub(
                r"<(?:tool_call|function_call)[^>]*>.*?(?:</(?:tool_call|function_call)>|$)",
                "",
                text,
                flags=re.DOTALL,
            ).strip()

    if not tools and not truncated:
        codeblock_pattern = r"```(?:tool_call|function_call)\s*(.*?)\s*```"
        cb_matches = list(re.finditer(codeblock_pattern, clean_text, flags=re.DOTALL))
        for m in cb_matches:
            cleaned = clean_json_str(m.group(1))
            try:
                data = json.loads(cleaned)
                if isinstance(data, dict):
                    name = (
                        data.get("name")
                        or data.get("tool")
                        or data.get("function")
                        or data.get("action")
                    )
                    args = (
                        data.get("arguments")
                        or data.get("parameters")
                        or data.get("input")
                        or data.get("args")
                        or {}
                    )
                    norm = normalize_tool_call(name, args)
                    if norm:
                        tools.append(norm)
            except Exception:
                pass
        if tools:
            clean_text = re.sub(
                codeblock_pattern, "", clean_text, flags=re.DOTALL
            ).strip()

    if not tools and not truncated:
        json_pattern = r"```json\s*(\{.*?\})\s*```"
        json_matches = list(re.finditer(json_pattern, clean_text, flags=re.DOTALL))
        for m in json_matches:
            cleaned = clean_json_str(m.group(1))
            try:
                data = json.loads(cleaned)
                if isinstance(data, dict) and (
                    "name" in data or "tool" in data or "function" in data
                ):
                    name = (
                        data.get("name")
                        or data.get("tool")
                        or data.get("function")
                        or data.get("action")
                    )
                    args = (
                        data.get("arguments")
                        or data.get("parameters")
                        or data.get("input")
                        or data.get("args")
                        or {}
                    )
                    norm = normalize_tool_call(name, args)
                    if norm:
                        tools.append(norm)
            except Exception:
                pass
        if tools:
            clean_text = re.sub(json_pattern, "", clean_text, flags=re.DOTALL).strip()
    # Keep companion text: the tool-call blocks themselves were already removed
    # from clean_text above; only leftover bare tags are stripped here.
    clean_text = _strip_wrapper_tags(clean_text).strip()
    tools = _repair_tool_names(tools)
    return tools, clean_text


# Family-wide closer pattern for the tool-call wrapper family, including
# the |/｜-decorated variants that parse_tools accepts. Used by
# StreamToolParser so a mismatched closer closes the open block instead of
# hanging until flush(). [FIX 3]
_TOOL_END_TAG_RE = re.compile(
    r"</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls|invoke|function_calls?|tool_use)\s*>",
    re.IGNORECASE,
)

# [FIX 4] Spaced-DSML dialect: deepseek-harness emits decorated tags with a
# space between the ｜｜DSML｜｜ marker and the tag name, e.g.
# "<｜｜DSML｜｜ invoke name=...>". Entry detection cannot rely on plain
# substring start tags; this regex accepts bars, the DSML marker and the
# space in any combination.
_STREAM_ENTRY_RE = re.compile(
    r"<[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(tool_calls?|calls|function_calls?|invoke|tool_use)\b[^>]*>",
    re.IGNORECASE,
)

# Orphan closers trailing a block already closed by the per-tag or family
# fallback (e.g. "</｜｜DSML｜｜ calls>" after the inner invoke was flushed).
_ORPHAN_CLOSER_RE = re.compile(
    r"</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls|function_calls?|invoke)\s*[｜\|]{0,2}>",
    re.IGNORECASE,
)

# [FIX 5] Bare DSML marker: a "<｜｜DSML｜｜>" / "</｜｜DSML｜｜>" whose tag-name
# slot is empty (nothing between the DSML marker and ">"). Every other cleanup
# regex in this module requires an actual tag name (tool_calls/calls/invoke/
# function_call/parameter), so these bare markers used to slip through and get
# streamed to the client as literal text. Note the tag-name slot is required to
# be EMPTY: "<｜｜DSML｜｜ invoke ...>" does not match and is left for the entry
# regex.
# The ">" is optional: the model also leaks truncated markers like
# "<｜｜DSML｜｜ </｜｜DSML｜｜ " as prose.
_BARE_DSML_MARKER_RE = re.compile(
    r"</?[｜\|]{0,2}DSML[｜\|]{0,2}+\s*+(?![A-Za-z_])[>＞]?",
    re.IGNORECASE,
)


# Full wrapper-tag family (open or close, with an optional tag name). Used on
# the non-streaming path and on flush() to strip wrapper noise; unlike
# _ORPHAN_CLOSER_RE it also removes stray OPEN tags.
_WRAPPER_TAG_RE = re.compile(
    r"</?[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*(?:tool_calls?|calls|function_calls?|invoke|parameter)[^>]*>",
    re.IGNORECASE,
)


def _strip_stray_markers(text: str) -> str:
    """Prose path: drop bare DSML markers + orphan closers only.

    Open tags are left untouched here so literal prose mentioning e.g.
    "" is not silently deleted mid-stream.
    """
    if not text:
        return text
    text = _BARE_DSML_MARKER_RE.sub("", text)
    return _ORPHAN_CLOSER_RE.sub("", text)


def _strip_wrapper_tags(text: str) -> str:
    """Wrapper path: drop the whole tag family (open or close) + bare markers."""
    if not text:
        return text
    text = _BARE_DSML_MARKER_RE.sub("", text)
    return _WRAPPER_TAG_RE.sub("", text)


def _append_clean_text(results, text: str) -> None:
    """Append cleaned prose, skipping empty deltas after marker stripping."""
    cleaned = _strip_stray_markers(text)
    if cleaned:
        results.append({"text": cleaned})


# Tag names _STREAM_ENTRY_RE can open on.
_STREAM_ENTRY_TAGS = (
    "tool_calls",
    "tool_call",
    "function_calls",
    "function_call",
    "invoke",
    "calls",
)


def _skip_bars(text, pos):
    for _ in range(2):
        if pos < len(text) and text[pos] in "|｜":
            pos += 1
    return pos


def _is_plausible_stream_entry_prefix(segment: str) -> bool:
    if not segment.startswith("<") or ">" in segment:
        return False
    body = segment[1:]
    pos = _skip_bars(body, 0)
    length = len(body)
    if pos < length and body[pos] in "Dd":
        matched = 0
        while matched < 4 and pos < length and body[pos].upper() == "DSML"[matched]:
            pos += 1
            matched += 1
        if matched < 4:
            return pos == length
        pos = _skip_bars(body, pos)
    while pos < length and body[pos].isspace():
        pos += 1
    if pos == length:
        return True
    remainder = body[pos:]
    remainder_lower = remainder.lower()
    for tag in _STREAM_ENTRY_TAGS:
        if tag.startswith(remainder_lower):
            return True
        if remainder_lower.startswith(tag):
            suffix = remainder[len(tag) :]
            if not suffix or not (suffix[0].isalnum() or suffix[0] == "_"):
                return True
    return False


def _is_plausible_stream_closer_prefix(segment: str) -> bool:
    return segment.startswith("</") and _is_plausible_stream_entry_prefix(
        "<" + segment[2:]
    )


class StreamToolParser:
    def __init__(self):
        self.buffer = ""
        self.in_tool = False
        self.has_tool = False
        self.json_done = False
        self._end_re = None
        # [FIX 5] Set by flush() when the stream ended inside a tool block
        # that never received its closer. app.py turns this into a truncated
        # stop_reason instead of pretending the turn ended normally.
        self.truncated = False

    def feed(self, chunk):
        self.buffer += chunk
        results = []
        while True:
            if self.in_tool:
                # [FIX 3] Family-wide closer fallback: accept ANY wrapper closer
                # the tool-call family can emit (including |/｜-decorated
                # variants parse_tools tolerates) instead of hanging an open
                # block until flush() when the model closes with a wrong tag.
                # [FIX 4] Prefer the closer for the tag that OPENED the block
                # so nested wrappers (<｜｜DSML｜｜ calls> wrapping invokes)
                # close as one unit; fall back to the family-wide closer for
                # mismatched or decorated closers [FIX 3].
                end_match = self._end_re.search(self.buffer) if self._end_re else None
                if end_match is None:
                    end_match = _TOOL_END_TAG_RE.search(self.buffer)
                if end_match:
                    if not self.json_done:
                        tool_xml = self.buffer[: end_match.end()]
                        parsed, _ = parse_tools(tool_xml)
                        for item in parsed:
                            results.append({"tool": item})
                    self.buffer = self.buffer[end_match.end() :]
                    self.in_tool = False
                    self.json_done = False
                    self._end_re = None
                    continue
                brace_idx = self.buffer.find("{")
                if brace_idx != -1 and not self.json_done:
                    decoder = json.JSONDecoder()
                    try:
                        data, consumed = decoder.raw_decode(self.buffer[brace_idx:])
                        norm = normalize_tool_call(data)
                        if norm:
                            results.append({"tool": norm})
                            self.json_done = True
                            self.buffer = self.buffer[brace_idx + consumed :]
                            continue
                    except Exception:
                        pass
                break
            else:
                # [FIX 4] Regex entry detection replaces plain substring finds
                # so decorated/spaced DSML openers enter tool mode.
                m = _STREAM_ENTRY_RE.search(self.buffer)
                if m:
                    start = m.start()
                    if start > 0:
                        _append_clean_text(results, self.buffer[:start])
                    self.buffer = self.buffer[start:]
                    tag_name = m.group(1).lower()
                    self._end_re = re.compile(
                        r"</[｜\|]{0,2}(?:DSML[｜\|]{0,2})?\s*"
                        + re.escape(tag_name)
                        + r"[｜\|]{0,2}\s*>",
                        re.IGNORECASE,
                    )
                    self.in_tool = True
                    self.has_tool = True
                    continue
                # [FIX 2] Hold an unclosed '<' tail only while it remains a
                # plausible partial match of _STREAM_ENTRY_RE or of a wrapper
                # closer ("</..."), so a split closer is never dumped as prose.
                last_lt = self.buffer.rfind("<")
                tail = self.buffer[last_lt:] if last_lt != -1 else ""
                hold = ">" not in tail and (
                    _is_plausible_stream_entry_prefix(tail)
                    or _is_plausible_stream_closer_prefix(tail)
                )
                if last_lt != -1 and hold:
                    if last_lt > 0:
                        _append_clean_text(results, self.buffer[:last_lt])
                    self.buffer = tail
                    break
                if self.buffer:
                    _append_clean_text(results, self.buffer)
                self.buffer = ""
                break
        return results

    def flush(self):
        out = []
        if self.buffer and not self.in_tool:
            _append_clean_text(out, self.buffer)
        elif self.in_tool:
            # [FIX 5] No closer arrived for the open block -> the upstream
            # stream was cut. Previously this path still ran parse_tools()
            # over the partial payload and emitted whatever it could parse,
            # so the client received a tool_use with a prefix of the real
            # arguments ("required parameter missing"). Drop it outright and
            # record the truncation for the caller.
            has_closer = bool(
                (self._end_re and self._end_re.search(self.buffer))
                or _TOOL_END_TAG_RE.search(self.buffer)
            )
            _gt = self.buffer.find(">")
            _body = self.buffer[_gt + 1 :] if _gt != -1 else ""
            # A missing outer closer is normal; only an incomplete payload
            # (open parameter / brace-truncated JSON) means the cut landed
            # inside the arguments and the visible args are just a prefix.
            if not has_closer and not _payload_is_complete(_body):
                self.truncated = True
                stripped = _strip_wrapper_tags(self.buffer).strip()
                if not self.json_done and stripped:
                    # Nothing parsed at all and no JSON was consumed: this is
                    # prose that merely looked like an opener. Keep it rather
                    # than silently swallowing the user-visible text.
                    out.append({"text": stripped})
                self.buffer = ""
                self.in_tool = False
                self.json_done = False
                self._end_re = None
                return out
            parsed, _ = parse_tools(self.buffer)
            if parsed:
                for item in parsed:
                    out.append({"tool": item})
        self.buffer = ""
        self.in_tool = False
        self.json_done = False
        self._end_re = None
        return out
