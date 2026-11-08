# TokenAlarm

Claude 구독(Pro/Max/Ultra) 사용량을 기록해 **언제 얼마나 쓰는지 알려주고**, 주간 리셋이 다가오는데 사용량이 많이 남아 있으면
**미리 정해 둔 작업을 자동으로 실행해서 목표 %까지만 쓰고 멈추는** Windows용 백그라운드 도구입니다.

```
리셋 3일 전 18% (20% 미만) ──▶ "10분 후 자동 실행" 알림 ──▶ 작업 큐 실행 ──▶ 28~30% 도달 시 정지 ──▶ 결과 알림
                                     (tokenalarm pause 로 취소)        (Claude Code를 다시 쓰면 즉시 중단)
```

- **사용량 측정·예측**:
  - 현재 5시간/주간 사용률
  - 하루 평균 소모량
  - 리셋 때 예상 사용률과 예상 낭비량
  - 한도 도달 예상 시각
- **알림**: Windows 데스크톱, ntfy(휴대폰 푸시), Slack/Discord 웹훅
  - "토큰이 남을 것 같아요"
  - 5시간 창 80% 경고
  - 매일 요약
  - 자동 실행 시작/종료 보고
- **리셋 전 자동 실행 (사다리 규칙)**: 3일 전·2일 전·1일 전·12시간 전마다 기준 미만이면 목표 %까지 채웁니다.
- **목표 %에서 멈추기**: 작업을 작은 예산 단위(청크)로 나눠 실행하고, 청크마다 실제 사용률을 다시 확인합니다.
- **안전장치**:
  - 코드 작업은 별도 git worktree와 새 브랜치에서 하고, push는 금지합니다.
  - API 키 과금은 차단합니다.
  - 주간 하드캡과 5시간 상한을 지킵니다.
  - 사용자가 돌아오면 즉시 중단합니다.

기존 도구 조사 결과는 [docs/research.md](docs/research.md)에 있습니다. 요약하면, 모니터링 도구는 많지만 "리셋 전 목표 %까지
자동 소진"을 해 주는 공개 도구는 없었습니다.

---

## 1. 동작 방식

### 사용량을 읽는 곳 (공식 경로 우선)
| 순서 | 소스 | 비용 | 언제 |
|---|---|---|---|
| 1 | Claude Code **상태줄** JSON의 `rate_limits` | 무료 | Claude Code를 쓰는 동안 자동 수집 |
| 2 | `claude -p /usage` | 무료 (모델 호출 없음) | 상태줄 데이터가 오래됐을 때 |
| 3 | 실행 중인 작업의 `rate_limit_event` | 작업 자체 | 자동 실행 중 실시간 |
| 4 | Haiku 1단어 ping | 극소량 (약 $0.0003 상당) | **자동 실행 직전에만** (5시간 창을 시작시키므로) |
| (opt-in) | 비공식 `api/oauth/usage` | 무료 | 기본 꺼짐 (`usage.oauth_endpoint: true`) |

### 사다리 규칙
```yaml
weekly_fill_rules:
  - { before_reset: 72h, if_below: 20, fill_to: 30 }   # 3일 전 20% 미만 → 30%까지
  - { before_reset: 48h, if_below: 40, fill_to: 50 }   # 2일 전 40% 미만 → 50%까지
  - { before_reset: 24h, if_below: 60, fill_to: 70 }   # 1일 전
  - { before_reset: 12h, if_below: 80, fill_to: 90 }   # 12시간 전
```
- **적용 시점**: 리셋까지 남은 시간이 `before_reset` 이하가 되면 그 규칙이 적용됩니다. 여러 개가 해당하면 가장 늦은
  체크포인트(가장 작은 값)가 우선입니다.
- **목표 유지**: 한 번 정해진 목표는 그 주 안에서 유지됩니다. 18%에서 30% 목표가 잡히면, 20%를 넘은 뒤에도 30%까지
  계속 채웁니다.
- **시작 조건**: 아래가 모두 충족돼야 실행을 시작합니다.
  - 대화형 Claude Code를 최근 30분간 쓰지 않음
  - (Windows) 키보드·마우스 입력이 15분간 없음
  - 일시정지 아님
  - 자동 실행 금지 시간대 아님

### 목표 %에서 멈추는 방법
1. 작업 1개를 여러 **청크**로 나눠 실행합니다. 청크 1개는 `claude -p` 프로세스 1회이고, `--max-turns`와
   `--max-budget-usd`가 걸립니다.
2. 청크 예산은 남은 격차에 맞춰 자동으로 정합니다: `(목표 - 현재) ÷ (1달러당 %) × 0.5`.
   - "1달러당 %"는 실행할 때마다 자동으로 보정합니다.
3. 실행 중에는 매 응답의 실제 사용률을 보고, `목표 - stop_margin`에 닿으면 interrupt 신호로 부드럽게 멈춥니다.
   - 응답이 없으면 프로세스 트리를 강제 종료합니다.
4. 멈춘 작업은 `partial` 상태로 남습니다. 다음 기회에 **같은 세션을 이어서(`--resume`)** 실행합니다.

---

## 2. 설치 (Windows)

준비물:
- Python 3.10 이상
- Git
- [Claude Code](https://code.claude.com/docs) CLI: `claude`를 한 번 실행해 **구독 계정으로 로그인**해 두세요.

```powershell
git clone https://github.com/jmh-chem/ai-token-alarm-and-auto-start.git
cd ai-token-alarm-and-auto-start
py -m pip install -e ".[windows]"
```

```powershell
tokenalarm init            # %LOCALAPPDATA%\tokenalarm 에 config.yaml, tasks.yaml, prompts\ 생성
```

1. `%LOCALAPPDATA%\tokenalarm\config.yaml`과 `tasks.yaml`을 편집합니다(아래 3, 4장 참고).
2. Claude Code에 상태줄과 활동 감지 훅을 등록합니다. 기존 `settings.json`은 백업되고, 기존 훅은 유지됩니다.
   ```powershell
   tokenalarm setup-claude
   ```
3. 알림이 오는지 확인합니다.
   ```powershell
   tokenalarm notify-test
   ```
4. 현재 사용량과 판단을 확인합니다. `--refresh`는 필요하면 Haiku ping 1회를 씁니다.
   ```powershell
   tokenalarm status --refresh
   ```
5. 짧은 작업 하나로 "목표 %에서 멈춤"을 먼저 수동 테스트합니다.
   ```powershell
   tokenalarm run --until 25 --task weekly-ai-news --max-minutes 10
   ```
6. 로그온할 때 데몬이 자동으로 시작되게 등록합니다(작업 스케줄러).
   ```powershell
   tokenalarm install
   ```

> **주의**
> - **PC가 켜져 있고 로그인돼 있어야** 동작합니다. 절전 중에는 실행되지 않으니, 체크포인트 시간대에 PC를 켜 두거나 절전
>   설정을 조정하세요.
> - 작업 스케줄러 항목은 "사용자가 로그온한 경우에만 실행"으로 등록됩니다. 키보드·마우스 유휴 감지에 필요합니다.

---

## 3. 설정 (`config.yaml`)

전체 옵션은 [config.example.yaml](src/tokenalarm/examples/config.example.yaml)에 주석으로 설명돼 있습니다. 자주 바꾸는 것:

| 키 | 기본값 | 설명 |
|---|---|---|
| `weekly_fill_rules` | 위 사다리 | 체크포인트, 기준 %, 목표 % |
| `guards.weekly_hard_cap` | 95 | 자동 실행이 절대 넘기지 않는 주간 % |
| `guards.five_hour_ceiling` | 85 | 5시간 창이 이 이상이면 5시간 리셋까지 대기 |
| `guards.require_idle_minutes` | 30 | 대화형 Claude Code 미사용 시간 |
| `guards.quiet_hours` | null | 예: `"09:00-18:00"`에는 자동 실행 시작 안 함 |
| `guards.pre_run_warning` | 10m | 사전 알림 후 대기 시간 (그 사이 `tokenalarm pause`로 취소) |
| `runner.default_model` | sonnet | Opus/Fable은 훨씬 빨리 소모됩니다 |
| `notify.ntfy.topic` | null | 휴대폰 [ntfy](https://ntfy.sh) 앱에서 같은 토픽 구독 |
| `notify.webhooks` | [] | `{type: slack\|discord, url: ...}` |

---

## 4. 작업 큐 (`tasks.yaml`)

```yaml
tasks:
  - id: myrepo-tests
    kind: repo                 # git worktree + 브랜치 tokenalarm/myrepo-tests/<시각>, push 금지
    repo: C:/dev/myrepo
    repeat: weekly             # once | daily | weekly | always
    priority: 10               # 높을수록 먼저
    prompt_file: prompts/repo_tests.md
    allowed_tools: [Read, Edit, Write, Glob, Grep, "Bash(git add:*)", "Bash(git commit:*)", "Bash(pytest:*)"]

  - id: weekly-ai-news
    kind: research             # <output_dir>/<id>/<시각>/ 에 보고서 저장 (웹 검색 허용)
    repeat: weekly
    prompt_file: prompts/research_weekly.md
    output_dir: ~/TokenAlarm/research

  - id: study-notes
    kind: study                # 학습 노트·문제 생성
    prompt: "열역학 2장 핵심 정리와 연습문제 10개를 notes/ 에 만들어줘"
```

**작성 요령**
- **"단계마다 커밋/저장"을 지시하세요.** 목표 %에서 멈춰도 결과가 남고, 다음에 이어서 진행됩니다.
- **권한은 `allowed_tools` 허용 목록으로 제한합니다.** 기본 권한 모드는 `acceptEdits`이고, 지정하지 않으면 kind별로
  안전한 기본 도구만 씁니다.
- **추가로 막히는 명령**: `git push`, `git remote`, `rm -rf`, `git reset --hard`는 항상 차단 목록에 들어갑니다. 다만 차단
  목록은 보조 수단이고, 실제 안전은 허용 목록과 worktree 격리에서 나옵니다.
- **결과 확인**: `tokenalarm history`로 브랜치와 결과 폴더를 봅니다. 코드 작업은 `git log tokenalarm/<id>/...`로 검토한 뒤
  직접 merge하세요.

---

## 5. 명령어

| 명령 | 설명 |
|---|---|
| `tokenalarm status [--refresh] [--json]` | 현재 사용량, 예측, 자동 실행 판단, 활동 상태 |
| `tokenalarm forecast` | 위 내용 + 최근 14일 일별 사용량 + 최근 실행 |
| `tokenalarm history [-n 20]` | 자동 실행 기록 (Δ%, 비용, 브랜치/결과 위치) |
| `tokenalarm run --until 30 [--task ID] [--dry-run] [--max-minutes N]` | 지금 바로 목표 %까지 실행 |
| `tokenalarm daemon [--once] [--dry-run]` | 백그라운드 루프 (`--dry-run`은 판단만 출력) |
| `tokenalarm tasks [validate]` | 작업 큐 확인 |
| `tokenalarm pause [--for 6h]` / `resume` | 자동 실행 일시정지/재개 (`PAUSE` 파일로도 가능) |
| `tokenalarm probe [--ping] [--oauth]` | 사용량 소스 진단 |
| `tokenalarm setup-claude [--remove] [--force-statusline]` | Claude Code 상태줄/훅 등록·제거 |
| `tokenalarm install` / `uninstall` | 로그온 시 자동 시작 등록·해제 |
| `tokenalarm notify-test` | 알림 테스트 |

**상태줄 예시**: `5h 42% (2h13m) │ 주 18% (3d4h) → 예상 47% │ ⚡대기: 30%까지`

---

## 6. 안전·정책 메모

- **실행 방식**: 자동 실행은 **공식 `claude` CLI**(headless 모드)만 사용합니다. 토큰을 다른 프로그램에서 재사용하지 않습니다.
- **API 과금 차단**: 실행할 때 `ANTHROPIC_API_KEY`와 `ANTHROPIC_AUTH_TOKEN`을 환경에서 제거합니다. 그래도 Claude가
  구독이 아닌 인증(`apiKeySource`)으로 시작하면 즉시 중단합니다.
- **비공식 엔드포인트**: `api/oauth/usage`는 기본 꺼짐입니다.
- **사용 범위**: 플랜 한도는 일반적인 개인 사용을 전제로 합니다. TokenAlarm은 상시 소진이 아니라 **리셋 전 남는 몫을 내가 정한
  작업에 쓰는 것**을 목표로 하고, 목표 %·하드캡·사용자 복귀 시 중단으로 사용량을 제한합니다.
- **데이터 위치**: 모든 데이터는 `%LOCALAPPDATA%\tokenalarm`에 있습니다(`TOKENALARM_HOME`으로 변경 가능).
  - `tokenalarm.db`: 사용량 이력
  - `runs\`: 실행 로그
  - `worktrees\`: 코드 작업
  - `logs\`: 데몬 로그

## 7. 문제 해결

- **상태줄이 안 보임**: Claude Code를 재시작하세요. 상태줄은 첫 응답 이후에 사용량을 표시합니다.
  - 훅과 상태줄은 `"C:/.../python.exe" -m tokenalarm ...` 형식으로 등록됩니다. 셸 설정에 따라 실행되지 않으면
    `settings.json`의 명령을 직접 고치세요.
- **"사용량 데이터 없음"**: `tokenalarm probe --ping`으로 어떤 소스가 동작하는지 확인하세요.
- **데몬이 아무것도 안 함**: 아래 두 명령으로 보류 사유를 확인하세요.
  ```powershell
  tokenalarm daemon --dry-run
  ```
  ```powershell
  tokenalarm status
  ```
  로그는 `%LOCALAPPDATA%\tokenalarm\logs\tokenalarm.log`에 있습니다.
- **인증**: `apiKeySource`가 `none`이 아닌 다른 값인데 구독 인증이 맞다면 `runner.allowed_api_key_sources`에 그 값을
  추가하세요.

## 8. 개발

```bash
python -m pip install -e ".[dev]"
```

```bash
python -m pytest
```

- **가짜 CLI**: 테스트는 `tests/fake_claude.py`로 `claude` CLI를 흉내 냅니다. stream-json 입출력과 interrupt 제어 요청을
  처리하고, 사용률 보고를 반올림하고 지연시킵니다.
- **검증 범위**: 청크 실행, 중단·이어하기, API 키 차단, worktree, 목표 % 정지 정확도, 데몬 상태 전이, settings.json 병합을
  검증합니다.
- **CI**: Windows와 Ubuntu에서 실행합니다.
- **실측 메모**: [docs/spike-notes.md](docs/spike-notes.md)

### v2 아이디어
- 요일·시간대별 사용 패턴 학습 예측
- JSONL 토큰 집계(현재는 [ccusage](https://github.com/ryoppippi/ccusage) 병행 권장)
- 작업별 예상 소모량 학습
- Claude Code 플러그인(`/tokenalarm:status`)
- 체크포인트에 PC 깨우기
