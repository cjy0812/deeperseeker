"""Regenerate locales/en.json and locales/zh.json from one table.

Both dictionaries are written from THIS file so they cannot drift apart by
hand-editing: the keys live here once, and the parity test
(tests/test_webui_i18n.py::test_both_dictionaries_have_identical_key_sets) is
the backstop for anyone who edits a JSON directly.

Run from anywhere:

    python i18n/gen_locales.py

A maintainer tool, not an import-time dependency — nothing in the app imports
it, so a syntax error here cannot break a page render.
"""

import json
import os

# (key, english, chinese)
#
# Values may contain HTML (dashboard.step1/step2/step5/step6) — those are
# rendered with |safe in the template because they come from this file, never
# from user input. Do not add |safe anywhere a request-derived value flows.
STRINGS = [
    ("lang.switch_to", "中文", "English"),
    ("lang.current", "Language", "语言"),

    ("nav.dashboard", "Dashboard", "控制台"),
    ("nav.audit", "Audit", "审计"),
    ("nav.logout", "Logout", "退出登录"),

    ("login.title", "Login", "登录"),
    ("login.heading", "Login", "登录"),
    ("login.username", "Username", "用户名"),
    ("login.password", "Password", "密码"),
    ("login.submit", "Login", "登录"),
    ("login.failed", "Invalid username or password.", "用户名或密码错误。"),
    ("login.too_many_attempts", "Too many attempts. Try again later.", "尝试次数过多，请稍后再试。"),

    ("dashboard.title", "Dashboard", "控制台"),
    ("dashboard.heading", "Dashboard", "控制台"),
    ("dashboard.add_token", "Add Auth Token", "添加授权 Token"),
    ("dashboard.steps", "Steps:", "步骤："),
    ("dashboard.step1", "Open an <strong>incognito/private</strong> window", "打开一个<strong>无痕 / 隐私</strong>窗口"),
    ("dashboard.step2", "Go to <a href='https://chat.deepseek.com' target='_blank' rel='noopener'>chat.deepseek.com</a> and log in", "访问 <a href='https://chat.deepseek.com' target='_blank' rel='noopener'>chat.deepseek.com</a> 并登录"),
    ("dashboard.step3", "Open DevTools (F12) → Console → paste:", "打开开发者工具（F12）→ Console → 粘贴："),
    ("dashboard.step4", "Copy the token and paste it below", "复制 token 并粘贴到下方"),
    ("dashboard.step5", "<strong>Remove any surrounding quotes</strong> — paste only the raw token string", "<strong>去掉首尾的引号</strong> —— 只粘贴原始的 token 字符串"),
    ("dashboard.step6", "<strong>Close the incognito window</strong> to preserve the login", "<strong>关闭无痕窗口</strong>，以保住这次登录"),
    ("dashboard.token_warning", "If you log out of DeepSeek in the browser, this token stops working.", "如果在浏览器里退出 DeepSeek 登录，该 token 会立即失效。"),
    ("dashboard.alias_placeholder", "Alias (optional, e.g. personal, work)", "别名（可选，例如 personal、work）"),
    ("dashboard.token_placeholder", "Paste auth token here (no quotes)", "在此粘贴 auth token（不要带引号）"),
    ("dashboard.add_button", "Add Token", "添加 Token"),
    ("dashboard.tokens", "Tokens ({n})", "Token（{n}）"),
    ("dashboard.no_tokens", "No tokens yet.", "还没有任何 token。"),
    ("dashboard.th_id", "ID", "ID"),
    ("dashboard.th_alias", "Alias", "别名"),
    ("dashboard.th_token", "Token", "Token"),
    ("dashboard.th_status", "Status", "状态"),
    ("dashboard.th_in_flight", "In-flight", "进行中"),
    ("dashboard.th_last_used", "Last used", "最近使用"),
    ("dashboard.th_cooldown", "Cooldown", "冷却剩余"),
    ("dashboard.th_actions", "Actions", "操作"),
    ("dashboard.delete", "Delete", "删除"),
    ("dashboard.delete_confirm", "Delete this token?", "确认删除这个 token？"),
    ("dashboard.restore", "Restore", "恢复"),
    ("dashboard.api", "API", "API"),
    ("dashboard.api_openai", "OpenAI-compatible base URL:", "OpenAI 兼容地址："),
    ("dashboard.api_anthropic", "Anthropic-compatible base URL:", "Anthropic 兼容地址："),
    ("dashboard.api_model", "Model:", "模型："),

    # Token lifecycle states. The stored value stays the raw English enum
    # (webui._STATUS_KEYS maps it to these keys); only the visible text is
    # translated. QUARANTINED has no entry on purpose -- it is echoed verbatim.
    ("dashboard.status_active", "Active", "进行中"),
    ("dashboard.status_rate_limited", "Rate limited", "限流中"),

    ("dashboard.usage", "Usage", "用量"),
    ("dashboard.usage_day", "Day", "日期"),
    ("dashboard.usage_key", "Key", "密钥"),
    ("dashboard.usage_surfaces", "Surfaces", "入口"),
    ("dashboard.usage_requests", "Requests", "请求数"),
    ("dashboard.usage_input", "Input tokens", "输入 token"),
    ("dashboard.usage_output", "Output tokens", "输出 token"),
    ("dashboard.usage_cost", "Cost", "费用"),
    ("dashboard.usage_note", "Last 30 days. Key aliases are masked; cost follows the configured pricing source.", "最近 30 天。密钥别名已脱敏；费用按当前配置的价格来源计算。"),
    ("dashboard.usage_empty", "No usage recorded in the last 30 days.", "最近 30 天没有用量记录。"),

    ("audit.title", "Audit", "审计"),
    ("audit.heading", "Audit", "审计"),
    ("audit.window", "Window", "时间窗口"),
    ("audit.window_1h", "Last hour", "最近 1 小时"),
    ("audit.window_24h", "Last 24 hours", "最近 24 小时"),
    ("audit.window_7d", "Last 7 days", "最近 7 天"),
    ("audit.refresh", "Refresh", "刷新"),
    ("audit.auto_refresh", "Auto-refresh", "自动刷新"),
    ("audit.loading", "Loading…", "加载中…"),
    ("audit.unavailable", "Audit store unavailable.", "审计存储不可用。"),
    ("audit.disabled", "Audit logging is disabled; counters below will stay empty.", "审计记录当前已关闭，下方计数会保持为空。"),

    ("audit.metric_requests", "Requests", "请求数"),
    ("audit.metric_error_rate", "Error rate", "错误率"),
    ("audit.metric_p50", "p50 latency", "p50 延迟"),
    ("audit.metric_p95", "p95 latency", "p95 延迟"),
    ("audit.metric_p99", "p99 latency", "p99 延迟"),
    ("audit.metric_tokens_limited", "Tokens rate-limited", "被限流的 Token 数"),

    ("audit.perf_heading", "Performance", "性能"),
    ("audit.perf_errors", "Rate-limit reasons", "限流原因分布"),
    ("audit.perf_recoveries", "Recovery counters", "恢复计数"),
    ("audit.perf_rollovers", "Context rollovers", "上下文滚动"),
    ("audit.perf_empty", "No requests in this window.", "该窗口内没有请求。"),

    ("audit.counter_rate_limit", "Rate limited", "限流"),
    ("audit.counter_rate_recover", "Auto-recovered", "自动恢复"),
    ("audit.counter_failover", "Failover", "故障转移"),
    ("audit.counter_retry", "Retry", "重试"),
    ("audit.counter_rollover", "Rollover", "上下文滚动"),

    ("audit.rl_heading", "Token Rate Limits", "Token 限流审计"),
    ("audit.rl_empty", "No token was rate-limited in this window.", "该窗口内没有被限流的 token。"),
    ("audit.rl_th_token", "Token ID", "Token ID"),
    ("audit.rl_th_count", "Limits", "限流次数"),
    ("audit.rl_th_http429", "HTTP 429", "HTTP 429"),
    ("audit.rl_th_sse", "SSE limit", "SSE 限流"),
    ("audit.rl_th_other", "Other 4xx", "其他 4xx"),
    ("audit.rl_th_recovered", "Recovered", "已恢复"),
    ("audit.rl_th_last", "Last limited", "最近一次"),
    ("audit.rl_th_reason", "Last reason", "最近原因"),

    ("audit.reason_http_429", "Upstream HTTP 429", "上游 HTTP 429"),
    ("audit.reason_sse_rate_limit", "Rate limit inside SSE stream", "SSE 流内限流"),
    ("audit.reason_http_other", "Other upstream 4xx", "其他上游 4xx"),
    ("audit.reason_unknown", "Unknown", "未知"),

    ("common.none", "—", "—"),
]

EN = {k: en for k, en, _zh in STRINGS}
ZH = {k: zh for k, _en, zh in STRINGS}

assert set(EN) == set(ZH)


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    for name, data in (("en.json", EN), ("zh.json", ZH)):
        path = os.path.join(here, "locales", name)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        print(path, len(data), "keys")


if __name__ == "__main__":
    main()