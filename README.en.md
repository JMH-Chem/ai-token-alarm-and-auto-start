# TokenAlarm

English · [한국어](https://github.com/JMH-Chem/ai-token-alarm-and-auto-start/blob/main/README.md)

> Stop leaving your Claude quota on the table.
> TokenAlarm **tells you how fast you are using your Claude plan and when it will run out**. When the weekly
> reset is near and lots of quota is left, it **runs tasks you queued in advance and stops at a target %**.
> It runs on Windows first and works with Claude Pro, Max and Ultra.

```
3 days before reset at 18% ─▶ "autorun in 10 min" notice ─▶ run task queue ─▶ stop near 30% ─▶ report
                               (cancel: tokenalarm pause)     (stops at once if you start using Claude Code)
```

Claude Code status line: `5h 42% (2h13m) │ wk 18% (3d4h) → proj. 47% │ ⚡pending: to 30%`

---

## Quick start (Windows)

1. Install [Claude Code](https://code.claude.com/docs/en/setup), run `claude` once and **log in with your subscription**.
2. Paste this into PowerShell. You don't need Python or admin rights.
   ```powershell
   irm https://raw.githubusercontent.com/JMH-Chem/ai-token-alarm-and-auto-start/main/install.ps1 | iex
   ```
3. The setup wizard walks you through:
   - choosing rules
   - testing notifications
   - adding a first task
   - connecting Claude Code
   - starting at logon

**Update:** run the same line again.

**Uninstall** (your data folder is kept):
```powershell
$env:TOKENALARM_UNINSTALL=1; irm https://raw.githubusercontent.com/JMH-Chem/ai-token-alarm-and-auto-start/main/install.ps1 | iex
```

**macOS/Linux:** install [uv](https://docs.astral.sh/uv/), then run these two commands. You get system notifications and systemd instead of the tray and Windows toasts.
```bash
uv tool install "tokenalarm @ https://github.com/JMH-Chem/ai-token-alarm-and-auto-start/archive/refs/heads/main.zip"
```
```bash
tokenalarm setup
```

## Everyday commands

| Command | What it does |
|---|---|
| `tokenalarm status` | 5-hour and weekly usage, daily burn, projected % at reset, autorun decision |
| `tokenalarm add research "Weekly AI news report"` | Add a task (also `study`, or `repo <path>`) |
| `tokenalarm tasks` | List tasks (`remove`, `enable`, `disable <id>`) |
| `tokenalarm pause --for 6h` / `resume` | Pause or resume autorun |
| `tokenalarm doctor` | Diagnose problems and show how to fix them |
| `tokenalarm open` | Open the data folder (also `config`, `tasks`, `logs`) |

Other commands:
- `forecast`, `history`: detailed forecast and run history
- `run --until 40`: spend up to 40% right now
- `autostart [--tray|--off]`: start at logon on or off
- `clean`: remove finished worktrees and old logs
- `stop`: stop the running daemon
- `uninstall [--all]`: remove autostart and the Claude Code integration
- `notify-test`: send a test notification

## How it works

**Measuring usage** (official sources first)
- **Status line:** `rate_limits` from the Claude Code status line. Free, and collected automatically while you use Claude Code.
- **`claude -p /usage`:** free, makes no model call.
- **Running tasks:** a running autorun reports live utilization (`rate_limit_event`).
- **One-word Haiku request:** used only right before an autorun, costs next to nothing.
- **Unofficial usage endpoint:** off by default.

**Forecast**
- This week's average pace gives the projected % at reset and the expected unused amount.
- It also shows when you would hit the limit at the current pace. Autorun usage is excluded.

**Rules** (`standard` preset)
- 3 days before reset, below 20% → fill to 30%
- 2 days before, below 40% → fill to 50%
- 1 day before, below 60% → fill to 70%
- 12 hours before, below 80% → fill to 90%
- Other presets are `conservative` and `aggressive`, or define your own with `weekly_fill_rules`.

**Running and stopping**
- It starts only after 30 minutes without Claude Code use and 15 minutes without keyboard or mouse input.
- Work runs in small budgeted chunks, and real usage is re-checked after each one.
- It stops 2% below the target. Next time it resumes the same session.

## Task kinds

| kind | Where it runs | Default tools |
|---|---|---|
| `research` | an output folder (`outputs\<id>\<time>`) | read, write, web search |
| `study` | an output folder | read, write |
| `repo` | a **separate git worktree and new branch** of your repo (`tokenalarm/<id>/<time>`) | read, edit, `git add/commit` (never push) |

**Tips**
- Tell the task to **save or commit after each step**, so nothing is lost if it is stopped.
- For long prompts, use `@file.md`.
- Grant extra tools with `--allow "Bash(npm test:*)"`.
- **Review results:** `tokenalarm history` shows branches and output folders. Review code changes yourself before merging.

## Settings

Open the settings with `tokenalarm open config`. The running daemon reloads them when you save. All options are listed in `config.example.yaml` in the data folder.

| Key | Default | Meaning |
|---|---|---|
| `language` | auto | `ko` / `en` |
| `preset` | standard | `conservative` / `aggressive` |
| `guards.quiet_hours` | none | e.g. `"09:00-18:00"`: never start an autorun in that window |
| `guards.weekly_hard_cap` | 95 | autorun never pushes weekly usage past this % |
| `guards.five_hour_ceiling` | 85 | wait for the 5-hour reset above this % |
| `guards.pre_run_warning` | 10m | delay between the notice and the start |
| `runner.default_model` | sonnet | Opus/Fable burn quota much faster |
| `notify.ntfy.topic` / `notify.webhooks` | — | phone push / Slack or Discord |

## Safety and privacy

- **How tasks run:** only through the **official `claude` CLI** (headless mode). Login tokens are never reused anywhere else.
- **No API billing:** `ANTHROPIC_API_KEY` and `ANTHROPIC_AUTH_TOKEN` are removed for autoruns. If non-subscription auth is still detected, the run stops at once.
- **Stored data:** everything stays in `%LOCALAPPDATA%\tokenalarm`.
  - usage history
  - settings
  - `runs\`: **full autorun transcripts**
  - `worktrees\`
  - Nothing leaves your PC except the notifications you configure.
- **ntfy:** ntfy.sh is a public server, so anyone who knows the topic can read its messages. The wizard generates a hard-to-guess name.
- **Fair use:** plan limits assume ordinary individual use. TokenAlarm is not a 24/7 quota burner. It spends **what would otherwise expire on tasks you chose**, bounded by targets, a hard cap and stopping when you come back.

## Troubleshooting <a id="troubleshooting"></a>

**Start here**
```powershell
tokenalarm doctor
```
It checks each part and shows the command that fixes it.

**No status line**
- Restart Claude Code. Usage appears after the first reply.
- If it still doesn't show, register it again:
  ```powershell
  tokenalarm setup --repair
  ```

**No toasts**
- Check Windows Settings > System > Notifications, including Do not disturb.
- Try `notify.desktop_app_id: powershell` in `config.yaml`.

**Autorun does not start:** check the reason with
```powershell
tokenalarm status
```
Typical reasons:
- you are working
- quiet hours
- paused
- no tasks

**Bug reports:** paste the output of `tokenalarm doctor --json` into an [issue](https://github.com/JMH-Chem/ai-token-alarm-and-auto-start/issues). Secrets are masked automatically.

## Development

```bash
python -m pip install -e ".[dev,tray]"
```
```bash
python -m pytest
```

- **Fake CLI:** tests use `tests/fake_claude.py`, a fake `claude` that speaks the stream-json protocol, handles interrupts, and reports utilization rounded and late.
- **CI:** runs on Windows and Ubuntu. On real Windows PowerShell 5.1 it goes through install → setup → hooks under bash, cmd and PowerShell → tray → upgrade → uninstall.
- **Docs**
  - Background and survey of existing tools (Korean): [docs/research.md](docs/research.md)
  - Measurement notes: [docs/spike-notes.md](docs/spike-notes.md)
  - Release process: [RELEASING.md](RELEASING.md)

## License

**Not chosen yet.** Until a license is added, the code may not be reused without the author's permission.
