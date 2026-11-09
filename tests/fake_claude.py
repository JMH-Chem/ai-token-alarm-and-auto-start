"""A stand-in for the `claude` CLI that speaks the stream-json protocol used by tokenalarm.

Behaviour is controlled with environment variables (see defaults below). It simulates real-world
measurement problems: utilization is reported rounded to whole percent and lagging N turns behind.
"""
import json
import os
import queue
import sys
import threading
import time
import uuid

E = os.environ
USAGE_FILE = E.get("FAKE_USAGE_FILE")
STATE_DIR = E.get("FAKE_STATE_DIR", ".")
PCT_PER_TURN = float(E.get("FAKE_PCT_PER_TURN", "1.0"))
COST_PER_TURN = float(E.get("FAKE_COST_PER_TURN", "0.10"))
TURN_SECONDS = float(E.get("FAKE_TURN_SECONDS", "0.02"))
TASK_TURNS = int(E.get("FAKE_TASK_TURNS", "100000"))
LAG_TURNS = int(E.get("FAKE_REPORT_LAG_TURNS", "1"))
EVERY_TURN = E.get("FAKE_EVENT_EVERY_TURN", "1") == "1"
KEY_SOURCE = E.get("FAKE_API_KEY_SOURCE", "none")
IGNORE_INTERRUPT = E.get("FAKE_IGNORE_INTERRUPT") == "1"


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default


def load_usage():
    for _ in range(50):  # Windows: the file may be briefly locked by a reader / os.replace
        try:
            with open(USAGE_FILE) as f:
                return json.load(f)
        except (OSError, ValueError):
            time.sleep(0.02)
    raise RuntimeError("cannot read usage file")


def save_usage(u):
    tmp = USAGE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(u, f)
    for _ in range(50):
        try:
            os.replace(tmp, USAGE_FILE)
            return
        except PermissionError:
            time.sleep(0.02)
    raise RuntimeError("cannot write usage file")


def rate_event(history):
    u = load_usage()
    idx = max(0, len(history) - 1 - LAG_TURNS)
    reported = history[idx] if history else u["seven_day"]
    windows = {"seven_day": {"utilization": round(reported) / 100.0, "resetsAt": u["resets_at"], "status": "allowed"}}
    if "five_hour" in u:
        windows["five_hour"] = {"utilization": round(u["five_hour"]) / 100.0, "resetsAt": u["five_hour_resets_at"],
                                "status": "allowed"}
    out({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "unifiedWindows": windows}})


def main():
    with open(os.path.join(STATE_DIR, "invocations.jsonl"), "a") as f:
        f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd(), "autorun": E.get("TOKENALARM_AUTORUN"),
                            "api_key": E.get("ANTHROPIC_API_KEY")}) + "\n")
    if "/usage" in sys.argv:
        out({"type": "system", "subtype": "init", "apiKeySource": KEY_SOURCE, "session_id": str(uuid.uuid4())})
        text = "Total cost: $0.0000"
        if E.get("FAKE_USAGE_TEXT") == "1" and USAGE_FILE:
            text += f"\nCurrent week (all models)\n {load_usage()['seven_day']:.0f}% used"
        out({"type": "result", "subtype": "success", "num_turns": 0, "total_cost_usd": 0, "result": text})
        return 0

    session = arg("--resume") or str(uuid.uuid4())
    max_turns = int(arg("--max-turns", "1000"))
    budget = float(arg("--max-budget-usd", "1000"))
    progress_file = os.path.join(STATE_DIR, f"progress-{session}.json")
    done_turns = json.load(open(progress_file))["turns"] if os.path.exists(progress_file) else 0

    inbox = queue.Queue()

    def reader():
        for line in sys.stdin:
            line = line.strip()
            if line:
                inbox.put(json.loads(line))
        inbox.put(None)

    threading.Thread(target=reader, daemon=True).start()
    out({"type": "system", "subtype": "init", "apiKeySource": KEY_SOURCE, "session_id": session,
         "model": arg("--model")})

    msg = inbox.get()
    if msg is None:
        return 0
    turns = 0
    cost = 0.0
    history = [load_usage()["seven_day"]]
    interrupted = False
    subtype = "success"
    while True:
        # check for interrupt
        try:
            m = inbox.get_nowait()
            if m is None:
                pass
            elif m.get("type") == "control_request" and m["request"]["subtype"] == "interrupt":
                out({"type": "control_response", "response": {"subtype": "success", "request_id": m["request_id"]}})
                if not IGNORE_INTERRUPT:
                    interrupted = True
        except queue.Empty:
            pass
        if interrupted:
            out({"type": "user", "message": {"role": "user", "content": [{"type": "text",
                                                                       "text": "[Request interrupted by user]"}]}})
            subtype = "error_during_execution"
            break
        if done_turns + turns >= TASK_TURNS:
            subtype = "success"
            break
        if turns >= max_turns:
            subtype = "error_max_turns"
            break
        if cost >= budget:
            subtype = "error_max_budget_usd"
            break
        time.sleep(TURN_SECONDS)
        u = load_usage()
        u["seven_day"] += PCT_PER_TURN
        if "five_hour" in u:
            u["five_hour"] += PCT_PER_TURN * 3
        save_usage(u)
        history.append(u["seven_day"])
        turns += 1
        cost += COST_PER_TURN
        out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text",
                                                                              "text": f"turn {turns}"}]}})
        if EVERY_TURN or turns == 1:
            rate_event(history)
    with open(progress_file, "w") as f:
        json.dump({"turns": done_turns + turns}, f)
    out({"type": "result", "subtype": subtype, "is_error": subtype != "success", "num_turns": turns,
         "total_cost_usd": round(cost, 4) if not interrupted else 0, "session_id": session,
         "result": "작업 요약: 테스트 완료" if subtype == "success" else None})
    # wait for stdin to close like the real CLI
    while True:
        m = inbox.get()
        if m is None:
            break
    return 1 if interrupted else 0


if __name__ == "__main__":
    sys.exit(main())
