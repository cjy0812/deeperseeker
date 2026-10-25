"""Request/response shaping: the pure translation layer.

Everything here is a function of its arguments — no database, no token pool, no
network. Moved out of app.py so the wire formats (OpenAI chat.completion,
OpenAI responses, Anthropic message) can be read in one place instead of being
interleaved with the routing and the session machinery.

Nothing in this module imports app.py or functions.py, by construction: the
only module-level names used are json/time/uuid, the two FastAPI types, and
SINGLE_MODEL below.
"""

import json
import time
import uuid

from fastapi import HTTPException, Request

def format_anthropic_response(result, model):
    choice = result["choices"][0]
    msg = choice["message"]
    ant_content = []

    if msg.get("reasoning_content"):
        ant_content.append({"type": "thinking", "thinking": msg["reasoning_content"]})

    if msg.get("content"):
        ant_content.append({"type": "text", "text": msg["content"]})

    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            args = tc["function"]["arguments"]
            tool_input = json.loads(args) if isinstance(args, str) else args
            ant_content.append(
                {
                    "type": "tool_use",
                    "id": tc["id"],
                    "name": tc["function"]["name"],
                    "input": tool_input,
                }
            )
    usage = result.get("usage", {})
    msg_id = result["id"]
    if not msg_id.startswith("msg_"):
        msg_id = f"msg_{msg_id.replace('chatcmpl-', '')}"
    # [FIX 7c] Propagate the truncation signal produced by format_response().
    # finish_reason "length" means the upstream cut the reply mid-tool-call;
    # reporting "tool_use"/"end_turn" here made the non-streaming Anthropic
    # route look like a clean finish, so the client ran a half-delivered turn.
    finish_reason = choice.get("finish_reason")
    if finish_reason == "length":
        stop_reason = "max_tokens"
    elif msg.get("tool_calls"):
        stop_reason = "tool_use"
    else:
        stop_reason = "end_turn"
    return {
        "id": msg_id,
        "type": "message",
        "role": "assistant",
        "content": ant_content,
        "model": model,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _normalize_responses_tools(tools):
    if not isinstance(tools, list):
        return []
    out = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if t.get("type") == "function":
            fn = t.get("function") if isinstance(t.get("function"), dict) else t
            strict_val = fn.get("strict", t.get("strict"))
            out.append(
                {
                    "type": "function",
                    "name": fn.get("name", ""),
                    "description": fn.get("description"),
                    "parameters": fn.get(
                        "parameters",
                        fn.get("input_schema", {"type": "object", "properties": {}}),
                    ),
                    "strict": strict_val if isinstance(strict_val, bool) else True,
                }
            )
        else:
            out.append(t)
    return out


def _responses_usage(in_tokens, out_tokens):
    return {
        "input_tokens": in_tokens,
        "input_tokens_details": {
            "cached_tokens": 0,
            "cache_write_tokens": 0,
        },
        "output_tokens": out_tokens,
        "output_tokens_details": {
            "reasoning_tokens": 0,
        },
        "total_tokens": in_tokens + out_tokens,
    }


def _responses_effort(opts):
    reasoning_cfg = opts.get("reasoning")
    effort = reasoning_cfg.get("effort") if isinstance(reasoning_cfg, dict) else None
    if effort is None and isinstance(reasoning_cfg, str):
        effort = reasoning_cfg
    if effort is None:
        for key in ("reasoning_effort", "effort"):
            candidate = opts.get(key)
            if isinstance(candidate, str) and candidate.strip():
                effort = candidate
                break
    if isinstance(effort, str) and effort.strip().lower() in (
        "minimal",
        "low",
        "medium",
        "high",
    ):
        return effort.strip().lower()
    return None


def _build_response_object(
    resp_id,
    created_at,
    model,
    output,
    status="completed",
    usage=None,
    completed_at=None,
    error=None,
    opts=None,
):
    opts = opts if isinstance(opts, dict) else {}
    reasoning_cfg = opts.get("reasoning")
    effort = _responses_effort(opts)
    if isinstance(reasoning_cfg, dict):
        reasoning_obj = {
            "effort": effort,
            "summary": reasoning_cfg.get("summary"),
        }
    else:
        reasoning_obj = {
            "effort": effort,
            "summary": None,
        }
    text_cfg = opts.get("text")
    if not isinstance(text_cfg, dict) or not isinstance(text_cfg.get("format"), dict):
        text_cfg = {"format": {"type": "text"}}
    metadata = opts.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    parallel_tc = opts.get("parallel_tool_calls")
    if not isinstance(parallel_tc, bool):
        parallel_tc = True
    store_val = opts.get("store")
    if not isinstance(store_val, bool):
        store_val = True
    temp_val = opts.get("temperature")
    temp_val = float(temp_val) if isinstance(temp_val, (int, float)) else 1.0
    top_p_val = opts.get("top_p")
    top_p_val = float(top_p_val) if isinstance(top_p_val, (int, float)) else 1.0
    top_logprobs = opts.get("top_logprobs")
    top_logprobs = top_logprobs if isinstance(top_logprobs, int) else 0
    truncation = opts.get("truncation")
    if truncation not in ("auto", "disabled"):
        truncation = "disabled"
    background = opts.get("background")
    if not isinstance(background, bool):
        background = False
    return {
        "id": resp_id,
        "object": "response",
        "created_at": created_at,
        "status": status,
        "completed_at": completed_at,
        "background": background,
        "error": error,
        "incomplete_details": None,
        "instructions": opts.get("instructions"),
        "max_output_tokens": opts.get("max_output_tokens"),
        "max_tool_calls": opts.get("max_tool_calls"),
        "model": model,
        "output": output,
        "parallel_tool_calls": parallel_tc,
        "previous_response_id": opts.get("previous_response_id"),
        "reasoning": reasoning_obj,
        "service_tier": opts.get("service_tier") or "default",
        "store": store_val,
        "temperature": temp_val,
        "text": text_cfg,
        "tool_choice": (
            opts.get("tool_choice") if opts.get("tool_choice") is not None else "auto"
        ),
        "tools": _normalize_responses_tools(opts.get("tools")),
        "top_logprobs": top_logprobs,
        "top_p": top_p_val,
        "truncation": truncation,
        "usage": usage,
        "user": opts.get("user"),
        "metadata": metadata,
    }


def format_responses_response(result, model=None, opts=None):
    choice = result["choices"][0]
    msg = choice["message"]
    output = []

    if msg.get("reasoning_content"):
        output.append(
            {
                "id": f"rs_{uuid.uuid4().hex}",
                "type": "reasoning",
                "status": "completed",
                "summary": [
                    {
                        "type": "summary_text",
                        "text": msg["reasoning_content"],
                    }
                ],
            }
        )

    tool_calls = msg.get("tool_calls") or []
    content_text = msg.get("content")
    if content_text is not None or not tool_calls:
        output.append(
            {
                "id": f"msg_{uuid.uuid4().hex}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": content_text or "",
                        "annotations": [],
                        "logprobs": [],
                    }
                ],
            }
        )

    for tc in tool_calls:
        fn = tc.get("function", {})
        raw_args = fn.get("arguments", "{}")
        args_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
        output.append(
            {
                "id": f"fc_{uuid.uuid4().hex}",
                "type": "function_call",
                "status": "completed",
                "call_id": tc.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "name": fn.get("name", ""),
                "arguments": args_str,
            }
        )

    raw_usage = result.get("usage", {})
    in_tokens = raw_usage.get("prompt_tokens", raw_usage.get("input_tokens", 0))
    out_tokens = raw_usage.get("completion_tokens", raw_usage.get("output_tokens", 0))
    preset_id = opts.get("_resp_id") if isinstance(opts, dict) else None
    raw_id = str(result.get("id", ""))
    if preset_id:
        resp_id = preset_id
    elif raw_id.startswith("resp_"):
        resp_id = raw_id
    elif raw_id.startswith("chatcmpl-"):
        resp_id = f"resp_{raw_id[len('chatcmpl-') :].replace('-', '')}"
    else:
        resp_id = f"resp_{uuid.uuid4().hex}"
    created_at = int(result.get("created", result.get("created_at", time.time())))
    completed_at = int(time.time())
    model_name = model or result.get("model", SINGLE_MODEL)
    return _build_response_object(
        resp_id,
        created_at,
        model_name,
        output,
        status="completed",
        usage=_responses_usage(in_tokens, out_tokens),
        completed_at=completed_at,
        error=None,
        opts=opts,
    )


def is_thinking_enabled(body, request=None):
    effort = body.get("effort")
    if effort is not None:
        e_str = str(effort).strip().lower()
        if e_str in [
            "medium",
            "high",
            "max",
            "ultra",
            "extreme",
            "enabled",
            "adaptive",
            "on",
        ]:
            return True
        if e_str in ["low", "minimal", "none", "off", "disable", "disabled", "false"]:
            return False

    out_cfg = body.get("output_config")
    if isinstance(out_cfg, dict):
        out_effort = out_cfg.get("effort") or out_cfg.get("reasoning_effort")
        if out_effort is not None:
            e_str = str(out_effort).strip().lower()
            if e_str in [
                "medium",
                "high",
                "max",
                "ultra",
                "extreme",
                "enabled",
                "adaptive",
                "on",
            ]:
                return True
            if e_str in [
                "low",
                "minimal",
                "none",
                "off",
                "disable",
                "disabled",
                "false",
            ]:
                return False

    thinking_val = body.get("thinking")
    if isinstance(thinking_val, dict):
        t_type = str(thinking_val.get("type", "")).strip().lower()
        if t_type in ["enabled", "adaptive", "true"]:
            return True
        if t_type == "disabled":
            return False
        budget = thinking_val.get("budget_tokens", 0)
        if isinstance(budget, (int, float)) and budget > 0:
            return True
        t_effort = (
            thinking_val.get("effort")
            or thinking_val.get("reasoning_effort")
            or thinking_val.get("level")
        )
        if t_effort is not None:
            e_str = str(t_effort).strip().lower()
            if e_str in [
                "medium",
                "high",
                "max",
                "ultra",
                "extreme",
                "enabled",
                "adaptive",
                "on",
            ]:
                return True
            if e_str in [
                "low",
                "minimal",
                "none",
                "off",
                "disable",
                "disabled",
                "false",
            ]:
                return False
    elif isinstance(thinking_val, str):
        t_str = thinking_val.strip().lower()
        if t_str in [
            "medium",
            "high",
            "max",
            "ultra",
            "extreme",
            "true",
            "enabled",
            "adaptive",
            "on",
        ]:
            return True
        if t_str in ["low", "minimal", "none", "off", "disable", "disabled", "false"]:
            return False
    elif isinstance(thinking_val, bool):
        return thinking_val

    reasoning_val = body.get("reasoning")
    if isinstance(reasoning_val, dict):
        r_effort = (
            reasoning_val.get("effort")
            or reasoning_val.get("reasoning_effort")
            or reasoning_val.get("level")
        )
        if r_effort is not None:
            e_str = str(r_effort).strip().lower()
            if e_str in [
                "medium",
                "high",
                "max",
                "ultra",
                "extreme",
                "xhigh",
                "enabled",
                "adaptive",
                "on",
            ]:
                return True
            if e_str in [
                "low",
                "minimal",
                "none",
                "off",
                "disable",
                "disabled",
                "false",
            ]:
                return False

    reasoning_effort = body.get("reasoning_effort")
    if reasoning_effort is not None:
        effort_str = str(reasoning_effort).strip().lower()
        if effort_str in ["medium", "high", "max", "ultra", "extreme", "xhigh"]:
            return True
        if effort_str in ["low", "minimal", "none", "off", "disable", "disabled"]:
            return False

    if request:
        req_effort = (
            request.headers.get("anthropic-thinking")
            or request.headers.get("x-anthropic-thinking")
            or request.headers.get("effort")
            or request.headers.get("x-effort")
        )
        if req_effort:
            e_str = str(req_effort).strip().lower()
            if e_str in [
                "medium",
                "high",
                "max",
                "ultra",
                "extreme",
                "enabled",
                "adaptive",
                "on",
            ]:
                return True
    return False


# DeepSeek now serves a single model (v4.1flash) as the website default.
# Every request — whatever model name the client sends, including legacy
# aliases (instant, expert, vision, anthropic/claude-*) — is served by it.
SINGLE_MODEL = "v4.1flash"


def resolve_model(model_raw):
    return SINGLE_MODEL


async def _json_body(request: Request):
    """Parse the body as JSON; empty/malformed payloads are a client error.

    Stage 1 minor list: the completion endpoints used to call request.json()
    directly, so an empty body or invalid JSON surfaced as a 500 — a client
    mistake reported as a server fault (and paged as one)."""
    try:
        raw = await request.body()
    except Exception:
        raise HTTPException(
            status_code=400, detail="Unable to read request body"
        ) from None
    if not raw or not raw.strip():
        raise HTTPException(
            status_code=400, detail="Empty request body; expected a JSON object"
        )
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(
            status_code=400, detail="Invalid JSON in request body"
        ) from None
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=400, detail="Request body must be a JSON object"
        )
    return body


def _responses_input_to_messages(inputs):
    if isinstance(inputs, (str, dict)):
        inputs = [inputs]
    messages = []
    call_names = {}
    for item in inputs:
        if isinstance(item, str):
            messages.append({"role": "user", "content": item})
            continue
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        if item_type == "reasoning":
            continue
        if item_type == "function_call":
            raw_args = item.get("arguments", "{}")
            args_str = raw_args if isinstance(raw_args, str) else json.dumps(raw_args)
            call_id = (
                item.get("call_id")
                or item.get("id")
                or ("call_" + uuid.uuid4().hex[:8])
            )
            fn_name = item.get("name", "")
            if call_id and fn_name:
                call_names[call_id] = fn_name
            tc = {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": fn_name,
                    "arguments": args_str,
                },
            }
            if messages and messages[-1].get("role") == "assistant":
                messages[-1].setdefault("tool_calls", []).append(tc)
            else:
                messages.append(
                    {"role": "assistant", "content": None, "tool_calls": [tc]}
                )
            continue
        if item_type == "function_call_output":
            out_val = item.get("output", "")
            if isinstance(out_val, list):
                out_val = " ".join(
                    p.get("text", "")
                    for p in out_val
                    if isinstance(p, dict)
                    and p.get("type") in ("input_text", "output_text", "text")
                )
            elif not isinstance(out_val, str):
                out_val = json.dumps(out_val)
            call_id = item.get("call_id") or item.get("id") or ""
            tool_msg = {
                "role": "tool",
                "tool_call_id": call_id,
                "content": out_val,
            }
            t_name = item.get("name") or call_names.get(call_id)
            if t_name:
                tool_msg["name"] = t_name
            messages.append(tool_msg)
            continue

        role = item.get("role", "user")
        if role == "developer":
            role = "system"
        content = item.get("content", [])
        msg_content = []
        if isinstance(content, str):
            msg_content = content
        else:
            for c in content:
                if isinstance(c, str):
                    msg_content.append({"type": "text", "text": c})
                elif not isinstance(c, dict):
                    continue
                elif c.get("type") in ("input_text", "output_text"):
                    msg_content.append({"type": "text", "text": c.get("text")})
                elif c.get("type") == "refusal":
                    msg_content.append({"type": "text", "text": c.get("refusal", "")})
                elif c.get("type") == "input_file":
                    if c.get("file_data"):
                        msg_content.append(
                            {
                                "type": "file",
                                "file_data": c.get("file_data"),
                                "filename": c.get("filename"),
                            }
                        )
                    else:
                        msg_content.append(
                            {"type": "file", "file_id": c.get("file_id")}
                        )
                elif c.get("type") == "input_image":
                    url = c.get("image_url")
                    if isinstance(url, dict):
                        url = url.get("url")
                    if url:
                        msg_content.append(
                            {"type": "image_url", "image_url": {"url": url}}
                        )
                    elif c.get("file_id"):
                        msg_content.append(
                            {"type": "file", "file_id": c.get("file_id")}
                        )
                    else:
                        msg_content.append(c)
                else:
                    msg_content.append(c)
        if (
            role == "assistant"
            and isinstance(msg_content, list)
            and all(
                isinstance(p, dict) and p.get("type") == "text" for p in msg_content
            )
        ):
            msg_content = "\n".join(p.get("text", "") for p in msg_content)
        if role == "assistant" and messages and messages[-1].get("role") == "assistant":
            prev_c = messages[-1].get("content")
            if not prev_c:
                messages[-1]["content"] = msg_content
            elif isinstance(prev_c, str) and isinstance(msg_content, str):
                messages[-1]["content"] = (
                    f"{prev_c}\n{msg_content}" if msg_content else prev_c
                )
            elif isinstance(prev_c, list) and isinstance(msg_content, list):
                prev_c.extend(msg_content)
            continue
        messages.append({"role": role, "content": msg_content})
    return messages


def convert_anthropic_messages(messages):
    """Translate Anthropic message dicts into OpenAI-style dicts.

    tool_use blocks become assistant tool_calls and tool_result blocks become
    role="tool" messages so that build_prompt()/extract_tool_results() see real
    tool results and the signature cache can match across turns.
    """
    openai_msgs = []
    for m in messages:
        content = m.get("content", "")
        tool_calls = []
        tool_results = []
        if isinstance(content, list):
            parts = []
            image_parts = []
            for c in content:
                if not isinstance(c, dict):
                    continue
                if c.get("type") == "text":
                    parts.append(c.get("text", ""))
                elif c.get("type") == "image":
                    image_parts.append(c)
                elif c.get("type") == "tool_use":
                    tool_calls.append(
                        {
                            "id": c.get("id") or ("call_" + uuid.uuid4().hex[:8]),
                            "type": "function",
                            "function": {
                                "name": c.get("name", ""),
                                "arguments": json.dumps(c.get("input", {})),
                            },
                        }
                    )
                elif c.get("type") == "tool_result":
                    res_content = c.get("content", "")
                    if isinstance(res_content, list):
                        for item in res_content:
                            if isinstance(item, dict) and item.get("type") == "image":
                                image_parts.append(item)
                        res_content = " ".join(
                            item.get("text", "")
                            for item in res_content
                            if isinstance(item, dict) and item.get("type") == "text"
                        )
                    elif not isinstance(res_content, str):
                        res_content = str(res_content)
                    tool_results.append(
                        {
                            "tool_call_id": c.get("tool_use_id", ""),
                            "content": res_content,
                        }
                    )
            if image_parts:
                content = [
                    {"type": "text", "text": s} for s in parts if s
                ] + image_parts
            else:
                content = "\n".join(p for p in parts if p)
        if m.get("role") == "system":
            if content:
                openai_msgs.append({"role": "system", "content": content})
            continue
        if m.get("role") == "assistant":
            if isinstance(content, str) and (
                not content.strip() or content.strip() == "(no content)"
            ):
                content = None
            msg = {"role": "assistant", "content": content}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            if msg["content"] is None and not tool_calls:
                continue
            openai_msgs.append(msg)
            continue
        for tr in tool_results:
            openai_msgs.append(
                {
                    "role": "tool",
                    "tool_call_id": tr["tool_call_id"],
                    "content": tr["content"],
                }
            )
        has_content = (
            bool(content) if not isinstance(content, list) else len(content) > 0
        )
        if has_content or not tool_results:
            openai_msgs.append({"role": m.get("role", "user"), "content": content})
    return openai_msgs
