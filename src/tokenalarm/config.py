"""Configuration (config.yaml) with safe defaults.

Durations accept numbers (seconds) or strings like ``"90s"``, ``"45m"``, ``"72h"``, ``"3d"``, ``"1d12h"``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_DUR_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([smhdw])", re.I)
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_duration(value: Any) -> float:
    """Return seconds for ``value`` (number = seconds, or e.g. "72h", "1d12h", "45m")."""
    if value is None:
        raise ValueError("duration is empty")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower()
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return float(text)
    pos = 0
    total = 0.0
    for m in _DUR_RE.finditer(text):
        if text[pos:m.start()].strip():
            raise ValueError(f"invalid duration: {value!r}")
        total += float(m.group(1)) * _UNIT[m.group(2)]
        pos = m.end()
    if pos == 0 or text[pos:].strip():
        raise ValueError(f"invalid duration: {value!r}")
    return total


def format_duration(seconds: float | None) -> str:
    """Compact human duration: 3d4h, 2h13m, 45m, 30s."""
    if seconds is None:
        return "?"
    s = int(max(0, seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return f"{d}d{h}h" if h else f"{d}d"
    if h:
        return f"{h}h{m:02d}m" if m else f"{h}h"
    if m:
        return f"{m}m"
    return f"{s}s"


@dataclass
class FillRule:
    before_reset: float  # seconds before the weekly reset at which the rule starts applying
    if_below: float  # percent
    fill_to: float  # percent

    def label(self) -> str:
        return f"리셋 {format_duration(self.before_reset)} 전 <{self.if_below:g}% → {self.fill_to:g}%"


DEFAULT_RULES = [
    FillRule(72 * 3600, 20, 30),
    FillRule(48 * 3600, 40, 50),
    FillRule(24 * 3600, 60, 70),
    FillRule(12 * 3600, 80, 90),
]


@dataclass
class Guards:
    weekly_hard_cap: float = 95.0
    five_hour_ceiling: float = 85.0
    stop_margin: float = 2.0
    require_idle_minutes: float = 30.0  # no interactive Claude Code activity for this long
    require_input_idle_minutes: float = 15.0  # Windows keyboard/mouse idle (0 = ignore)
    stop_on_input: bool = False  # stop autorun as soon as keyboard/mouse is used
    session_heartbeat_timeout: float = 45 * 60  # seconds; an in-flight prompt older than this is ignored
    quiet_hours: str | None = None  # e.g. "09:00-18:00" → never start autorun in this local window
    pre_run_warning: float = 10 * 60  # seconds between "about to autorun" notice and start


@dataclass
class UsageConfig:
    statusline_fresh_minutes: float = 10.0
    idle_poll_minutes: float = 60.0
    near_checkpoint_poll_minutes: float = 10.0
    probe_usage_command: bool = True  # `claude -p /usage` (free, no model call)
    allow_ping_probe: bool = True  # tiny Haiku request, only right before/while filling
    ping_model: str = "haiku"
    oauth_endpoint: bool = False  # undocumented api/oauth/usage — opt-in only
    oauth_min_interval_minutes: float = 10.0
    rate_limit_scale: str = "fraction"  # fraction | percent | auto  (rate_limit_event utilization)


@dataclass
class RunnerConfig:
    claude_cmd: list[str] | None = None  # None → auto-detect `claude`
    default_model: str = "sonnet"
    default_permission_mode: str = "acceptEdits"
    chunk_turns: int = 15
    chunk_budget_usd_default: float = 0.5  # used before pct_per_usd is calibrated
    chunk_budget_usd_min: float = 0.2
    chunk_budget_usd_max: float = 5.0
    chunk_budget_fraction: float = 0.5  # fraction of the remaining gap one chunk may use
    chunk_timeout_minutes: float = 45.0
    max_chunks_per_task: int = 12
    kill_grace_seconds: float = 20.0
    allowed_api_key_sources: list[str] = field(default_factory=lambda: ["none"])
    continue_prompt: str = (
        "이전 작업을 이어서 계속 진행하세요. 단계마다 커밋/저장하고, 모두 끝났으면 무엇을 했는지 요약하세요."
    )
    append_system_prompt: str = (
        "You are running unattended as a scheduled background job (tokenalarm). The user is away. "
        "Work in small, safe steps and commit or save after each meaningful step so progress is never lost "
        "if you are stopped. Never push, publish, delete data outside the working directory, or ask questions; "
        "if something is ambiguous, choose the conservative option and note it in your final summary."
    )


@dataclass
class NtfyConfig:
    url: str = "https://ntfy.sh"
    topic: str | None = None
    token: str | None = None


@dataclass
class WebhookConfig:
    url: str
    type: str = "slack"  # slack | discord | generic


@dataclass
class NotifyConfig:
    desktop: bool = True
    ntfy: NtfyConfig = field(default_factory=NtfyConfig)
    webhooks: list[WebhookConfig] = field(default_factory=list)
    daily_summary_hour: int | None = 21  # local hour, None = off
    five_hour_warn_pct: float = 80.0
    waste_warn_before: float = 72 * 3600  # seconds: warn when this close to reset and projected waste is large
    waste_warn_pct: float = 30.0


@dataclass
class DaemonConfig:
    tick_seconds: float = 60.0


@dataclass
class Config:
    weekly_fill_rules: list[FillRule] = field(default_factory=lambda: list(DEFAULT_RULES))
    guards: Guards = field(default_factory=Guards)
    usage: UsageConfig = field(default_factory=UsageConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    tasks_file: str | None = None
    source_path: Path | None = None


_GUARD_DURATIONS = {"session_heartbeat_timeout", "pre_run_warning"}


def _apply(obj: Any, data: dict[str, Any], durations: set[str] = frozenset()) -> None:
    for key, value in (data or {}).items():
        if not hasattr(obj, key):
            raise ValueError(f"unknown config key: {type(obj).__name__}.{key}")
        if key in durations and value is not None:
            value = parse_duration(value)
        setattr(obj, key, value)


def load_config(path: Path | None) -> Config:
    cfg = Config()
    if path is None or not Path(path).exists():
        return cfg
    with open(path, encoding="utf-8-sig") as f:
        raw = yaml.safe_load(f) or {}
    cfg.source_path = Path(path)
    if "weekly_fill_rules" in raw:
        rules = []
        for item in raw.get("weekly_fill_rules") or []:
            rules.append(
                FillRule(
                    before_reset=parse_duration(item["before_reset"]),
                    if_below=float(item["if_below"]),
                    fill_to=float(item["fill_to"]),
                )
            )
        cfg.weekly_fill_rules = rules
    _apply(cfg.guards, raw.get("guards") or {}, _GUARD_DURATIONS)
    _apply(cfg.usage, raw.get("usage") or {})
    _apply(cfg.runner, raw.get("runner") or {})
    if isinstance(cfg.runner.claude_cmd, str):
        cfg.runner.claude_cmd = [cfg.runner.claude_cmd]
    notify = dict(raw.get("notify") or {})
    ntfy = notify.pop("ntfy", None)
    hooks = notify.pop("webhooks", None)
    _apply(cfg.notify, notify, {"waste_warn_before"})
    if ntfy:
        _apply(cfg.notify.ntfy, ntfy)
    if hooks:
        cfg.notify.webhooks = [WebhookConfig(**h) for h in hooks if h and h.get("url")]
    _apply(cfg.daemon, raw.get("daemon") or {})
    cfg.tasks_file = raw.get("tasks_file")
    validate_config(cfg)
    return cfg


def validate_config(cfg: Config) -> None:
    for r in cfg.weekly_fill_rules:
        if not (0 <= r.if_below <= 100 and 0 < r.fill_to <= 100):
            raise ValueError(f"rule percentages must be 0-100: {r}")
        if r.fill_to < r.if_below:
            raise ValueError(f"fill_to must be >= if_below: {r}")
        if r.before_reset <= 0:
            raise ValueError(f"before_reset must be positive: {r}")
    if cfg.usage.rate_limit_scale not in ("auto", "fraction", "percent"):
        raise ValueError("usage.rate_limit_scale must be auto, fraction or percent")
