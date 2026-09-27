# 서버 프록시 다운로드 운영 반영 — 2026-09-27

운영 웹: https://replay-live-poc.vercel.app

PR #23 → develop, PR #24 → main 순서로 사용자 승인 후 머지했다. 운영 소스는 main `7c8e6734883c2d03f7b53f97df0824851e1c3106`이며, API·웹·워커의 버전은 `rpl-7c8e6734883c2d03`이다. 웹/API는 Vercel, 영상 저장소는 Cloudflare R2를 사용한다.

## 반영과 데이터 보존

- 사용자 승인 경로 `/private/tmp/replay-production-before-011-20260927.dump`에 운영 PostgreSQL 전체 백업을 저장했다. 권한 0600, 49,999 bytes, SHA-256 `02ca30c03f9dbf8755a86506d5ef36234c9ed3742004aaa12b372fc7b7879cc4`이다. PostgreSQL 18.6 `pg_dump`의 일관된 읽기 전용 스냅샷을 사용했고 `pg_restore --list`가 성공했다. 별도 DB에 실제 복원하는 시험은 이번 운영 반영에서 수행하지 않았다.
- 011 마이그레이션으로 공유 잔여량·알림 테이블 두 개를 추가했다. 스키마와 마이그레이션 체크섬이 일치하고 기존 23개 테이블의 데이터 행 수가 보존됨을 확인했다. 마이그레이션 이력은 10개에서 11개로 증가했다.
- DB 접속정보는 승인된 Vercel 항목 하나만 메모리로 읽었다. 접속정보·세션 원문·Google 인증정보는 보고서나 Git에 저장하지 않았다.
- API `dpl_7Y5k3UjDy55oyko9zr3hVKqgu4mt`, 웹 `dpl_9gUrLoN7kkuy2zpZ2PHJiMLyW3Yf`를 정상 promote했다. 운영 `/api/live`와 웹 모두 HTTP 200 및 새 버전/정적 파일을 확인했다.
- 워커 snapshot: `snap_k7S0ux2EWnlKKFssK4c6KHv5aWqm`. Node, yt-dlp EJS, FFmpeg 및 합성 영상 검증을 거친 스냅샷이다.

## 실제 운영 headless E2E

사용자가 승인한 기존 Google 회원의 최대 10분짜리 Replay 앱 세션을 사용했다. 세션 원문은 메모리와 자식 프로세스 stdin에서만 전달했으며, DB에는 기존 인증 방식대로 해시만 저장했다. Google OAuth 로그인 화면·credential 교환 자체를 다시 통과한 시험은 아니다.

검증 영상: https://youtu.be/GcOe4ILS6Ow

| 확인 항목 | 실제 결과 |
| --- | --- |
| 세션 복원 → 스튜디오 연결됨 | 3,167ms |
| 저장소 health 응답을 기다리지 않고 연결 표시 | 통과 — health 요청만 의도적으로 지연한 후 해제 |
| 화면에서 영상 링크 입력 → 서버 가져오기 | HTTP 202, 1건 |
| 가져오기 → 보관함 사용 가능 | 28,245ms |
| 준비된 영상 | 760,024 bytes, 17.024초 |
| 실제 미리보기 저장소 | `replay-live-storage.guswhd1085.workers.dev` (R2) |
| headless Chrome 재생 | 끝까지 재생, 360×640, 509프레임 디코딩, 미디어 오류 없음 |
| 도우미 호출 / 브라우저 파일 업로드 | 각각 0건 |
| JavaScript 오류 | 0건 |
| 새로고침 → 스튜디오 연결됨 | 792ms |
| 임시 세션 폐기 | 세션 family revoke 및 해당 세션 retired, 이후 `/api/me` HTTP 401 |
| 다운로드 워커 | 첫 시도 completed, error code 없음, 종료 처리 `cleaned=true`, Sandbox API의 실제 상태 `stopped` |

워커 최초 기동 중 `/api/health`는 한 번 503을 반환했다. 스튜디오 연결과 영상 가져오기는 정상 진행됐다. 위 시간은 단일 운영 시험의 측정값으로, 모든 영상/네트워크의 속도를 보장하는 값은 아니다.

## 잔여량 감시

운영 dispatcher가 DataImpulse의 실제 공유 잔여량 **5,357,396,365 bytes**를 기록했다. 이후 별도 관찰에서 `observed_at` 갱신도 확인했다. 잔여량이 임계치보다 충분하므로 low/critical 플래그는 false, 알림 outbox는 0건이다.

잔여 1GB / 250MB 알림, 임대 기반 중복 방지와 충전 시 재무장, 구매 링크는 구현·자동 테스트를 완료했다. Telegram 봇의 실제 연결 확인 메시지는 앞선 검증에서 전송 성공했다. 운영 잔여량을 임의로 낮춰 허위 부족 알림을 보내지는 않았다. 관련 구현·사전 검증은 [서버 프록시 가져오기](server-proxy-import.md), [용량 감시 증거](server-proxy-quota-evidence.json)를 참고한다.

## 복구 기준

이전 웹: `dpl_3m8Gpw98cJo7oQhTKjRDSLp8xfPJ`, 이전 API: `dpl_3juqsP6ZrwxBT14QTchMS9aD2TUU`, 이전 워커 snapshot: `snap_53BTQmejbGjk0PEN8mwYXEjesoMV`.

롤백 시 웹·API·워커 버전을 함께 맞춰야 한다. 이번 011은 추가 테이블만 만들므로 기존 데이터나 테이블을 삭제해 되돌릴 필요가 없다. 이 문서는 복구 기준을 기록한 것이며 실제 롤백을 실행하지 않았다.
