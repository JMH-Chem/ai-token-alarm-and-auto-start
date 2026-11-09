"""Configuration (config.yaml) with safe defaults.

Durations accept numbers (seconds) or strings like ``"90s"``, ``"45m"``, ``"72h"``, ``"3d"``, ``"1d12h"``.
"""
from __future__ import annotations

import difflib
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
        from .i18n import L

        d = format_duration(self.before_reset)
        return L(f"리셋 {d} 전 <{self.if_below:g}% → {self.fill_to:g}%",
                 f"{d} before reset & <{self.if_below:g}% → {self.fill_to:g}%")


H = 3600
PRESETS: dict[str, list[FillRule]] = {
    # 3 days / 2 days / 1 day / 12 hours before the weekly reset
    "standard": [FillRule(72 * H, 20, 30), FillRule(48 * H, 40, 50), FillRule(24 * H, 60, 70),
                 FillRule(12 * H, 80, 90)],
    # fill a little, late
    "conservative": [FillRule(48 * H, 30, 40), FillRule(24 * H, 50, 60), FillRule(12 * H, 70, 80)],
    # use almost everything before the reset
    "aggressive": [FillRule(72 * H, 30, 45), FillRule(48 * H, 50, 65), FillRule(24 * H, 70, 85),
                   FillRule(12 * H, 85, 93)],
}
DEFAULT_RULES = PRESETS["standard"]
CONFIG_VERSION = 2


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
        "Continue the previous task from where you stopped. Keep committing/saving after each step, and when "
        "everything is done, summarize what you did (in the same language as the original task)."
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
    desktop_app_id: str = "auto"  # auto | tokenalarm | powershell  (Windows toast sender identity)
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
    config_version: int = CONFIG_VERSION
    language: str = "auto"  # auto | ko | en
    preset: str = "standard"
    weekly_fill_rules: list[FillRule] = field(default_factory=lambda: list(DEFAULT_RULES))
    guards: Guards = field(default_factory=Guards)
    usage: UsageConfig = field(default_factory=UsageConfig)
    runner: RunnerConfig = field(default_factory=RunnerConfig)
    notify: NotifyConfig = field(default_factory=NotifyConfig)
    daemon: DaemonConfig = field(default_factory=DaemonConfig)
    tasks_file: str | None = None
    source_path: Path | None = None


_GUARD_DURATIONS = {"session_heartbeat_timeout", "pre_run_warning"}


class ConfigError(ValueError):
    pass


_SECTION = {"Guards": "guards", "UsageConfig": "usage", "RunnerConfig": "runner", "NotifyConfig": "notify",
            "NtfyConfig": "notify.ntfy", "DaemonConfig": "daemon"}
TOP_KEYS = {"config_version", "language", "preset", "weekly_fill_rules", "guards", "usage", "runner", "notify",
            "daemon", "tasks_file"}


def _unknown(section: str, key: str, known: list[str]) -> ConfigError:
    from .i18n import L

    close = difflib.get_close_matches(key, known, n=1)
    hint = L(f" — 혹시 '{close[0]}'?", f" — did you mean '{close[0]}'?") if close else ""
    where = f"{section}.{key}" if section else key
    return ConfigError(L(f"알 수 없는 설정 키: {where}{hint}", f"unknown config key: {where}{hint}"))


def _apply(obj: Any, data: dict[str, Any], durations: set[str] = frozenset()) -> None:
    if not isinstance(data, dict):
        raise ConfigError(f"{_SECTION.get(type(obj).__name__, type(obj).__name__)}: expected a mapping")
    for key, value in (data or {}).items():
        if not hasattr(obj, key) or key.startswith("_"):
            raise _unknown(_SECTION.get(type(obj).__name__, ""), key, list(vars(obj).keys()))
        if key in durations and value is not None:
            value = parse_duration(value)
        setattr(obj, key, value)


def load_config(path: Path | None) -> Config:
    cfg = Config()
    if path is None or not Path(path).exists():
        return cfg
    try:
        with open(path, encoding="utf-8-sig") as f:
            raw = yaml.safe_load(f) or {}
    except yaml.YAMLError as e:
        raise ConfigError(f"{path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    for key in raw:
        if key not in TOP_KEYS:
            raise _unknown("", key, sorted(TOP_KEYS))
    cfg.source_path = Path(path)
    cfg.config_version = int(raw.get("config_version") or 1)
    cfg.language = str(raw.get("language") or "auto")
    cfg.preset = str(raw.get("preset") or "standard")
    if cfg.preset not in PRESETS:
        raise ConfigError(f"preset must be one of {', '.join(PRESETS)}")
    cfg.weekly_fill_rules = list(PRESETS[cfg.preset])
    if raw.get("weekly_fill_rules"):
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
            raise ConfigError(f"rule percentages must be 0-100: {r}")
        if r.fill_to < r.if_below:
            raise ConfigError(f"fill_to must be >= if_below: {r}")
        if r.before_reset <= 0:
            raise ConfigError(f"before_reset must be positive: {r}")
    if cfg.usage.rate_limit_scale not in ("auto", "fraction", "percent"):
        raise ConfigError("usage.rate_limit_scale must be auto, fraction or percent")
    if cfg.language not in ("auto", "ko", "en"):
        raise ConfigError("language must be auto, ko or en")
    if cfg.notify.desktop_app_id not in ("auto", "tokenalarm", "powershell"):
        raise ConfigError("notify.desktop_app_id must be auto, tokenalarm or powershell")


def _yaml_scalar(v: Any) -> str:
    import json

    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)  # JSON strings are valid YAML and always quoted
    return str(v)


def minimal_config_text(language: str, preset: str, desktop: bool = True, ntfy_topic: str | None = None,
                        webhooks: list[dict] | None = None, quiet_hours: str | None = None) -> str:
    """The short config.yaml written by ``tokenalarm setup`` (comments in the chosen language)."""
    ko = language == "ko"
    lines = [
        "# TokenAlarm — " + ("설정 파일. 바꾸면 데몬이 자동으로 다시 읽습니다." if ko else
                             "settings. The daemon reloads this file automatically when it changes."),
        "# " + ("전체 옵션: config.example.yaml (tokenalarm open data)" if ko else
                "All options: config.example.yaml (tokenalarm open data)"),
        f"config_version: {CONFIG_VERSION}",
        f"language: {language}            # auto | ko | en",
        f"preset: {preset}          # standard | conservative | aggressive",
        "# " + ("standard = 리셋 3일 전 <20%→30%, 2일 전 <40%→50%, 1일 전 <60%→70%, 12시간 전 <80%→90%" if ko else
                "standard = 3d before reset <20%→30%, 2d <40%→50%, 1d <60%→70%, 12h <80%→90%"),
        "# " + ("직접 정하려면 weekly_fill_rules 를 추가하세요 (config.example.yaml 참고)" if ko else
                "For custom rules add weekly_fill_rules (see config.example.yaml)"),
        "guards:",
        f"  quiet_hours: {_yaml_scalar(quiet_hours)}     # " + ("예: \"09:00-18:00\" 에는 자동 실행 시작 안 함" if ko
                                                       else 'e.g. "09:00-18:00" → never start autorun then'),
        "notify:",
        f"  desktop: {_yaml_scalar(desktop)}",
        "  ntfy:",
        f"    topic: {_yaml_scalar(ntfy_topic)}" + ("   # https://ntfy.sh/<topic> 을 휴대폰 ntfy 앱에서 구독" if ko else
                                              "   # subscribe to https://ntfy.sh/<topic> in the ntfy phone app"),
    ]
    if webhooks:
        lines.append("  webhooks:")
        for h in webhooks:
            lines.append(f"    - {{type: {h['type']}, url: {_yaml_scalar(h['url'])}}}")
    else:
        lines.append("  webhooks: []      # - {type: slack|discord, url: https://...}")
    return "\n".join(lines) + "\n"
