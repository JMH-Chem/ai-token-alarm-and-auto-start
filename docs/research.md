# 기존 도구 조사 (2026-11)

질문: "토큰 사용량을 측정해 예상 소모량·소모 시간을 알려주고, 리셋 전 남는 사용량을 지정 작업으로 자동 소진하며, 목표 %까지만
쓰도록 조절하는" 프로그램·스킬·플러그인이 이미 있는가?

**결론: 모니터링·예측 도구는 많지만, "리셋 N시간 전에 X% 미만이면 Y%까지 큐 작업을 실행하고 멈춘다"를 구현한 공개 도구는
찾지 못했다.** 그래서 TokenAlarm을 만들었고, 아래 도구들의 아이디어를 참고했다.

## 1. 사용량 모니터링·예측

| 도구 | 플랫폼 | 데이터 소스 | 예측/알림 | 자동 실행 |
|---|---|---|---|---|
| [ccusage](https://github.com/ryoppippi/ccusage) | 전체 (npx) | 로컬 JSONL 로그 → 토큰·비용 추정 | 5시간 블록 소모율·예상치 | 없음 |
| [Claude-Code-Usage-Monitor](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor) (`claude-monitor`) | 전체 (Python) | JSONL + statusline `rate_limits` + (실험) OAuth | 소모율, ETA, P90 한도, 종료코드 10/11 | 없음 |
| [CodexBar](https://github.com/steipete/CodexBar) | macOS/Linux | OAuth usage → 쿠키 → `/usage` | 번다운, 페이스, 알림 | 없음 |
| [Claude Usage Tracker](https://github.com/hamed-elfayome/Claude-Usage-Tracker) | macOS | claude.ai 세션 / OAuth | 6단계 페이스, 75/90/95% 알림 | 리셋 시 새 창 시작 ping만 |
| [ccstatusline](https://github.com/sirmalloc/ccstatusline) | 전체 | statusline `rate_limits` (+OAuth) | 블록 예측, 리셋 타이머 | 없음 |
| [usage-monitor-for-claude](https://github.com/jens-duttke/usage-monitor-for-claude) | **Windows**/Linux 트레이 | OAuth usage | 예측 차트, 임계값 알림 | 임계값·리셋 시 셸 명령 실행 (목표 % 로직 없음) |
| [claude-code-usage-quota-mod](https://github.com/anantraghunath/claude-code-usage-quota-mod) | Claude Code 플러그인 | OAuth usage | "리셋 때 약 87% 예상" 페이스 | 자동 `/compact`만 |

## 2. 자동 실행·스케줄러

| 도구 | 트리거 | 사용량 인식 | 한계 |
|---|---|---|---|
| [claude-overnight](https://github.com/rohanprichard/claude-overnight) | 야간 시간대 타이머 | O (5h 20%↑ 시작 안 함, 60%↑ 중단, 주간 80%↑ 건너뜀) | 리셋 기준 아님, 목표 %까지 채우지 않음 |
| [vasiliyk/claude-queue](https://github.com/vasiliyk/claude-queue) | 수동 워커 | O (95%에서 일시정지) | 리셋 전 채우기 없음 |
| [ClaudeMaxxing](https://claudemaxxing.pages.dev/) | 리셋 전 작업 창 | O (최소 여유 %) | 소스 비공개 |
| [claude-code-autopause](https://github.com/mofchris/claude-code-autopause) | statusline 임계값 → PreToolUse 훅 차단 | O | 채우기 없음 |
| [claude-auto-resume](https://github.com/terryso/claude-auto-resume), claude-auto-retry, claude-code-queue | 한도 메시지 감지 | X | 한도 해제 후 "계속"만 |
| 공식: [클라우드 예약 작업](https://code.claude.com/docs/en/routines), [데스크톱 예약 작업](https://code.claude.com/docs/en/desktop-scheduled-tasks), `/loop`, `/goal`, `autoContinueAtUsageLimit` | 스케줄/조건 | X | 사용량을 보고 판단하지 않음 |

## 3. 사용량을 읽는 방법 (공식 우선)

1. **statusline stdin JSON** (공식, v2.1.80+): `rate_limits.five_hour/seven_day.{used_percentage, resets_at}`.
   대화형 세션이 켜져 있을 때만 갱신.
2. **`claude -p /usage`** (공식): 모델 호출 없음. 구독 로그인에서는 플랜 사용량(`usage_report` 또는 텍스트)이 포함될 수 있음.
3. **stream-json `rate_limit_event`** (실측): 모든 모델 응답 뒤에 `unifiedWindows.<창>.utilization`(0~1)과 `resetsAt`이
   옴 → 실행 중인 작업이 실시간 사용률을 알려줌. 자세한 내용은 [spike-notes.md](spike-notes.md).
4. **`api.anthropic.com/api/oauth/usage`** (비공식): 많은 도구가 쓰지만 문서화되지 않았고 레이트리밋이 있으며, 약관상
   Claude.ai 자격증명을 다루는 도구는 회색지대. TokenAlarm에서는 기본 꺼짐(opt-in).

## 4. 플랜·정책 메모

- **창 구성**: Pro/Max는 5시간 창 + 주간 한도가 있다. Ultra(구 Max 20x)는 인증된 계정에 5시간 창이 없을 수 있다.
  Max/Ultra에는 모델별(Fable) 주간 한도가 따로 있다.
- **리셋 시각**: 주간 한도는 계정별 고정 시각에 리셋된다. claude.ai, Claude Code, 데스크톱이 같은 한도를 공유한다.
- **자동 실행 방식**: 공식 CLI의 headless 모드(`claude -p`)를 쓰는 것은 문서화된 기능이다. 반면 구독 OAuth 토큰을 제3자
  도구에서 재사용하는 것은 금지·제한 대상이다. TokenAlarm은 공식 CLI만 실행한다.
- **정상 사용 범위**: 플랜 한도는 "일반적인 개인 사용"을 가정한다. 그래서 상시 소진이 아니라 리셋 전 남는 몫을
  사용자가 정한 작업에 쓰는 범위로 제한했다(목표 %, 하드캡, 사용자 복귀 시 중단).
