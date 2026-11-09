# TokenAlarm

[English](README.en.md) · 한국어

> Claude 사용량을 아끼다가 리셋 때 버리지 마세요.
> TokenAlarm은 **얼마나 쓰고 있는지·언제 바닥나는지 알려주고**, 주간 리셋이 다가오는데 많이 남아 있으면
> **미리 정해 둔 작업을 자동으로 돌려 목표 %까지만 쓰고 멈춥니다.** (Windows 우선, Claude Pro/Max/Ultra)

```
리셋 3일 전 18% ─▶ "10분 후 자동 실행" 알림 ─▶ 작업 큐 실행 ─▶ 30% 근처에서 정지 ─▶ 결과 알림
                     (tokenalarm pause 로 취소)       (내가 Claude Code를 다시 쓰면 즉시 중단)
```

Claude Code 상태줄 예시: `5h 42% (2h13m) │ 주 18% (3d4h) → 예상 47% │ ⚡대기: 30%까지`

---

## 빠른 시작 (Windows)

1. [Claude Code](https://code.claude.com/docs/en/setup)를 설치하고, `claude`를 한 번 실행해 **구독 계정으로 로그인**합니다.
2. PowerShell에 아래 한 줄을 붙여 넣습니다. Python이나 관리자 권한은 필요 없습니다.
   ```powershell
   irm https://raw.githubusercontent.com/JMH-Chem/ai-token-alarm-and-auto-start/main/install.ps1 | iex
   ```
3. 설치가 끝나면 설정 마법사가 열립니다. 질문에 답하면 다음까지 끝납니다.
   - 규칙 선택
   - 알림 테스트
   - 첫 작업 추가
   - Claude Code 연동
   - 로그온 시 자동 시작

**업데이트**는 같은 한 줄을 다시 실행하면 됩니다.

**삭제**는 아래 한 줄을 실행합니다. 데이터 폴더는 남습니다.
```powershell
$env:TOKENALARM_UNINSTALL=1; irm https://raw.githubusercontent.com/JMH-Chem/ai-token-alarm-and-auto-start/main/install.ps1 | iex
```

macOS/Linux에서는 [uv](https://docs.astral.sh/uv/)를 설치한 뒤 아래 두 명령을 실행합니다. 트레이와 토스트 대신 시스템 알림과 systemd를 씁니다.
```bash
uv tool install "tokenalarm @ https://github.com/JMH-Chem/ai-token-alarm-and-auto-start/archive/refs/heads/main.zip"
```
```bash
tokenalarm setup
```

## 자주 쓰는 명령

| 명령 | 하는 일 |
|---|---|
| `tokenalarm status` | 현재 5시간·주간 사용률, 하루 소모량, 리셋 때 예상 %, 자동 실행 판단 |
| `tokenalarm add research "이번 주 AI 소식 보고서"` | 작업 추가 (`study`, `repo <경로>`도 가능) |
| `tokenalarm tasks` | 작업 목록 (`remove`, `enable`, `disable <id>`) |
| `tokenalarm pause --for 6h` / `resume` | 자동 실행 잠시 멈춤 / 재개 |
| `tokenalarm doctor` | 문제 진단과 해결 방법 |
| `tokenalarm open` | 데이터 폴더 열기 (`config`, `tasks`, `logs`) |

그 밖의 명령:
- `forecast`, `history`: 상세 예측과 실행 기록
- `run --until 40`: 지금 바로 40%까지 실행
- `autostart [--tray|--off]`: 로그온 시 자동 시작 켜기·끄기
- `clean`: 끝난 worktree와 오래된 로그 정리
- `stop`: 실행 중인 데몬 종료
- `uninstall [--all]`: 자동 시작과 Claude 연동 제거
- `notify-test`: 알림 테스트

## 동작 방식

**사용량 측정** (공식 경로 우선)
- **상태줄**: Claude Code 상태줄의 `rate_limits`를 받습니다. 무료이고, Claude Code를 쓰는 동안 자동으로 들어옵니다.
- **`claude -p /usage`**: 무료이고 모델을 호출하지 않습니다.
- **실행 중인 작업**: 자동 실행 중인 작업이 실시간 사용률(`rate_limit_event`)을 알려줍니다.
- **Haiku 1단어 요청**: 자동 실행 직전에만 씁니다. 비용은 극소량입니다.
- **비공식 사용량 엔드포인트**: 기본 꺼짐입니다.

**예측**
- 이번 주 평균 소모 속도로 리셋 때 예상 사용률과 낭비 예상을 계산합니다.
- 지금 속도로 한도에 닿는 시각도 계산합니다. 자동 실행이 쓴 양은 제외합니다.

**규칙** (프리셋 `standard`)
- 리셋 3일 전 20% 미만이면 30%까지
- 2일 전 40% 미만이면 50%까지
- 1일 전 60% 미만이면 70%까지
- 12시간 전 80% 미만이면 90%까지
- 그 밖의 프리셋은 `conservative`(보수적), `aggressive`(적극적)입니다. `weekly_fill_rules`로 직접 정할 수도 있습니다.

**실행과 정지**
- 시작 조건: 최근 30분간 Claude Code를 쓰지 않았고, 키보드·마우스가 15분간 쉬었을 때만 시작합니다.
- 작업은 작은 예산 단위(청크)로 실행하고, 청크마다 실제 사용률을 다시 확인합니다.
- 목표에서 2% 모자란 지점에서 멈춥니다. 다음 기회에는 같은 세션으로 이어서 진행합니다.

## 작업 종류

| kind | 어디서 | 기본 허용 도구 |
|---|---|---|
| `research` | 결과 폴더 (`outputs\<id>\<시각>`) | 읽기·쓰기·웹 검색 |
| `study` | 결과 폴더 | 읽기·쓰기 |
| `repo` | 내 저장소의 **별도 git worktree + 새 브랜치** (`tokenalarm/<id>/<시각>`) | 읽기·편집·`git add/commit` (push 금지) |

**작성 요령**
- 프롬프트에 **"단계마다 저장/커밋"**을 적어 두세요. 중간에 멈춰도 결과가 남습니다.
- 프롬프트가 길면 `@파일.md`로 지정합니다.
- 테스트 명령 같은 추가 권한은 `--allow "Bash(npm test:*)"`로 줍니다.
- **결과 확인**: `tokenalarm history`로 브랜치와 결과 폴더를 확인합니다. 코드 작업은 직접 검토한 뒤 merge하세요.

## 설정

설정 파일은 `tokenalarm open config`로 엽니다. 저장하면 실행 중인 데몬이 자동으로 다시 읽습니다. 전체 옵션은 데이터 폴더의 `config.example.yaml`에 있습니다.

| 키 | 기본값 | 설명 |
|---|---|---|
| `language` | auto | `ko` / `en` |
| `preset` | standard | `conservative` / `aggressive` |
| `guards.quiet_hours` | 없음 | 예: `"09:00-18:00"`에는 자동 실행을 시작하지 않음 |
| `guards.weekly_hard_cap` | 95 | 자동 실행이 넘기지 않는 주간 % |
| `guards.five_hour_ceiling` | 85 | 5시간 창이 이 이상이면 5시간 리셋까지 대기 |
| `guards.pre_run_warning` | 10m | 사전 알림 후 대기 시간 |
| `runner.default_model` | sonnet | Opus/Fable은 훨씬 빨리 소모됩니다 |
| `notify.ntfy.topic` / `notify.webhooks` | — | 휴대폰 푸시 / Slack·Discord |

## 안전과 개인정보

- **실행 방식**: 자동 실행은 **공식 `claude` CLI**(headless 모드)로만 합니다. 로그인 토큰을 다른 곳에서 재사용하지 않습니다.
- **API 과금 차단**: 자동 실행 환경에서 `ANTHROPIC_API_KEY`와 `ANTHROPIC_AUTH_TOKEN`을 제거합니다. 그래도 구독이 아닌 인증이 감지되면 즉시 멈춥니다.
- **저장하는 데이터**: 모두 `%LOCALAPPDATA%\tokenalarm`에 있습니다.
  - 사용량 기록
  - 설정
  - `runs\`: 자동 실행 **대화 전문**
  - `worktrees\`
  - 외부로 보내는 것은 **내가 설정한 알림뿐**입니다.
- **ntfy 주의**: ntfy.sh는 공개 서버라 토픽 이름을 아는 사람은 알림을 볼 수 있습니다. 마법사가 추측하기 어려운 이름을 만들어 줍니다.
- **사용 범위**: 플랜 한도는 "일반적인 개인 사용"을 전제로 합니다. TokenAlarm은 상시 소진이 아니라 **리셋 전에 남는 몫을 내가 정한 작업에 쓰는** 도구입니다. 그래서 목표 %·하드캡·사용자 복귀 시 중단으로 사용량을 제한합니다.

## 문제 해결 <a id="troubleshooting"></a>

**가장 먼저 할 일**
```powershell
tokenalarm doctor
```
각 항목의 상태와 해결 명령을 보여줍니다.

**상태줄이 안 보일 때**
- Claude Code를 재시작하세요. 사용량은 첫 응답 이후에 표시됩니다.
- 그래도 안 되면 아래 명령으로 다시 등록하세요.
  ```powershell
  tokenalarm setup --repair
  ```

**알림이 안 보일 때**
- Windows 설정 > 시스템 > 알림에서 알림 허용과 방해 금지 모드를 확인하세요.
- `config.yaml`에 `notify.desktop_app_id: powershell`을 넣어 보세요.

**자동 실행이 안 될 때**: 아래 명령으로 보류 사유를 확인하세요.
```powershell
tokenalarm status
```
보류 사유의 예:
- 사용 중
- 금지 시간대
- 일시정지
- 작업 없음

**버그 리포트**: `tokenalarm doctor --json` 결과를 [이슈](https://github.com/JMH-Chem/ai-token-alarm-and-auto-start/issues)에 붙여 주세요. 민감한 정보는 자동으로 가려집니다.

## 개발

```bash
python -m pip install -e ".[dev,tray]"
```
```bash
python -m pytest
```

- **가짜 CLI**: 테스트는 `tests/fake_claude.py`로 `claude` CLI를 흉내 냅니다. stream-json 입출력과 interrupt를 처리하고, 사용률 보고를 반올림하고 지연시킵니다.
- **CI**: Windows와 Ubuntu에서 실행합니다. Windows PowerShell 5.1에서 설치 → 설정 → 훅(bash/cmd/PowerShell) → 트레이 → 업데이트 → 삭제까지 실제로 돌립니다.
- **관련 문서**
  - 설계 배경과 기존 도구 조사: [docs/research.md](docs/research.md)
  - 실측 메모: [docs/spike-notes.md](docs/spike-notes.md)
  - 릴리스 절차: [RELEASING.md](RELEASING.md)

## 라이선스

**미정** — 라이선스가 정해지기 전까지는 저작자의 허락 없이 재사용할 수 없습니다.
