# Spike notes — Claude Code 2.1.319 headless behaviour (2026-11-08)

Verified in a cloud container (`apiKeySource: "none"`, host-managed provider).
Re-check on your own Windows PC with `tokenalarm probe --ping` (and `tokenalarm -v probe` for debug logs).

## 1. `claude -p "/usage" --output-format stream-json --verbose --no-session-persistence`

- Runs locally: `num_turns: 0`, `total_cost_usd: 0`, `duration_api_ms: 0` → **no model call, no quota used**.
- Emits `system/init` (with `apiKeySource`, `session_id`), one synthetic `assistant` message
  (`local_command_run: {"command": "usage"}`) and a `result` with `local_command: "usage"`.
- In this environment the text only contained session cost lines (no plan bars, no `usage_report`).
  On a claude.ai subscription login the text / `usage_report` may contain plan rows — the parser
  handles `usage_report.rate_limits.limits[]`, `rate_limit_event`, and "NN% used" text lines,
  and simply returns nothing if none are present.

## 2. Model calls emit `rate_limit_event` with real plan utilization

```json
{"type": "rate_limit_event",
 "rate_limit_info": {"status": "allowed", "resetsAt": 1794171600, "rateLimitType": "seven_day",
   "overageStatus": "rejected", "isUsingOverage": false,
   "unifiedWindows": {"seven_day": {"utilization": 0.27, "resetsAt": 1794171600, "status": "allowed"}}}}
```

- `unifiedWindows.<window>.utilization` is a **fraction (0–1)** (0.27 = 27 %), `resetsAt` is epoch seconds.
  (Scale is configurable: `usage.rate_limit_scale: auto|fraction|percent`.)
- Only windows that apply to the account are present (this account had only `seven_day`).
- Arrives right after the first assistant message of a request → a running task reports live usage.
- A one-word Haiku "ping" cost `$0.0003` (client-side estimate) — usable as a near-free probe,
  but it *starts* a 5-hour window, so tokenalarm only pings when it is about to fill anyway.

## 3. `--input-format stream-json` + interrupt

- Prompt sent on stdin as `{"type":"user","message":{"role":"user","content":"..."}}` works.
- Interrupt: write `{"type":"control_request","request_id":"<uuid>","request":{"subtype":"interrupt"}}`
  → `control_response {subtype: success}`, then a `user` message `[Request interrupted by user]`
  and `result {subtype: "error_during_execution", is_error: true}`.
- Closing stdin afterwards ends the process (exit code 1 after interrupt, 0 after normal finish).
- `--tools ""` disables all tools (used for the ping probe).

## 4. Other fields used

- `result`: `subtype` (`success`, `error_max_turns`, `error_max_budget_usd`, `error_during_execution`),
  `num_turns`, `total_cost_usd`, `session_id`, `result` (final text).
- `system/init.apiKeySource`: `"none"` for subscription/OAuth logins. Anything else
  (e.g. `ANTHROPIC_API_KEY`) means API billing → tokenalarm refuses to auto-run.
