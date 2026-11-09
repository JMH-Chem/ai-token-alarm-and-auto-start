# Changelog

## [0.2.0] - 2026-11-09

### Added
- One-line Windows installer (`install.ps1`): installs uv + a private Python 3.12, installs TokenAlarm,
  starts the setup wizard. Re-running it updates; `TOKENALARM_UNINSTALL=1` removes.
- `tokenalarm setup` wizard (language, rule preset, notifications with test, first task, Claude Code
  integration, start at logon) — `--yes` for non-interactive use, `--repair` after upgrades.
- Rule presets: `standard`, `conservative`, `aggressive`; short generated `config.yaml`.
- Task commands without YAML editing: `add`, `remove`, `enable`, `disable`, `tasks`.
- `doctor` (with `--json`, secrets redacted), `stop`, `open`, `clean`, `uninstall [--all]`, `autostart`.
- Korean and English output (auto-detected from the Windows UI language, or `language:` in config).
- Optional tray icon (`tokenalarm[tray]`): weekly % in a coloured disc, pause/resume, open folders.

### Changed
- Claude Code hooks are registered in exec form (`command` + `args`) so they work with Git Bash,
  PowerShell, spaces and non-ASCII user names; the status line is a single shell-safe executable.
- Logon task runs the windowless `tokenalarmw` entry point.
- Desktop notifications use PowerShell toasts with a registered `TokenAlarm` sender; the
  `win11toast` dependency was removed.
- Config changes are picked up by the running daemon automatically.

### Fixed
- The daemon now notices "user is back" during a long autorun (hook spool is ingested while filling).
- Korean text in Windows toasts (UTF-8 input to PowerShell).
- Child `claude` processes are tied to a Job Object and die with the daemon.

## [0.1.0] - 2026-11-08

- First version: usage collection (status line, `/usage`, `rate_limit_event`), linear forecast,
  fill-before-reset ladder, chunked headless runs with interrupt, safety guards, notifications
  (desktop / ntfy / Slack / Discord), Task Scheduler autostart.
