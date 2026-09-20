# Cloudflare 전체 이관 준비 — 2026-09-20

운영 이관은 아직 실행하지 않았다. 기존 운영 서비스의 Blob 502도 아직 해소되지 않았다.
이 브랜치는 Pages, R2, Python API 및 FFmpeg 실행을 Cloudflare로 이전하는 코드다.
사용자는 Vercel API·영상 처리까지 제거하는 범위를 지정했다. Containers 유료 활성화와 PR 머지는 미승인이다.

## 확인한 502 원인

- 운영 Blob `replay-live-media`: `billingState=suspended`, `usageQuotaExceeded=true`, `status=limits-exceeded-suspended`.
- 실제 SDK HEAD는 성공했으나 `issueSignedToken`에서 `BlobStoreSuspendedError`를 재현했다.
- 저장소 객체는 상태 조회 시 1개, 28바이트였다. 단순 파일 용량과 호출량 한도는 다르다. 정확히 어느 사용량 지표를 초과했는지는 확인하지 못했다.
- 기존 화면은 3초마다 `/health`를 호출하고 매번 저장소를 조회했다. 이것이 한도 초과의 유일한 원인이라고 단정하지 않는다.
- 관련 Blob 설정 두 값만 메모리에서 진단에 사용했다. 비밀 값·발급 URL은 기록하지 않았다.

## 대상 구성

| 기능 | Cloudflare 대상 |
| --- | --- |
| 웹 | Pages 정적 상용 빌드, `web/vite.pages.config.ts` |
| API | Worker가 단일 FastAPI Container로 전달 |
| 작업 예약·재시도 | Durable Object alarm과 기존 PostgreSQL lease |
| 영상 검증·송출 | 작업 lease마다 별도 FFmpeg Container |
| 영상 | 비공개 R2 바인딩, 단일 객체·크기·체크섬에 한정된 15분 이내 전송 권한 |
| 링크 다운로드 | 각 사용자 PC 도우미를 유지 |
| 사용자·세션·예약 DB | 기존 Neon PostgreSQL 유지; Vercel 컴퓨트와 독립적으로 연결 |

예정 URL은 `https://replay-live.pages.dev` 및 `https://replay-live-api.guswhd1085.workers.dev`다.
아직 해당 Replay Live 리소스를 생성하거나 배포하지 않았으므로 운영 링크로 안내하지 않는다.

R2 쓰기는 Worker에서 스트리밍하며 R2가 SHA-256을 검사한다. 동시 재전송도 `If-None-Match`에 해당하는 원자적 조건으로 덮어쓰기를 거절한다.
R2 키·스토어 자격증명은 브라우저·PC·FFmpeg Container에 전달하지 않는다. FFmpeg에는 해당 작업의 lease와 만료 URL만 전달한다.
객체 endpoint는 공개 R2 URL이 아니라 서명된 Worker URL이며 GET 구간 재생을 지원한다.

Cloudflare 요청 본문 한도를 고려해 결과 파일 상한은 **128 MiB에서 64 MiB로** 바뀐다. 입력 50 MiB·120초 제한은 유지된다.
숨겨진 웹 탭의 주기 조회는 멈추고, R2 정상 상태 조회는 API 인스턴스 내 60초 동안 공유한다.
`web/api/*` 및 기존 Vercel 프로파일은 복구·회귀 검증용으로 남지만 Pages/Cloudflare 배포에는 포함하지 않는다.

## 비용·권한 및 실행 전 조건

- [Cloudflare Containers 요금](https://developers.cloudflare.com/containers/platform/pricing/): Workers Paid 월 $5 기본료와 포함량 초과 과금. 전체 비용이 월 $5로 제한되는 것은 아니다.
- 계정 `guswhd1085@gmail.com`은 기존 R2가 활성화되어 있다. 다른 프로젝트의 `shopshorts-media`는 사용하거나 변경하지 않는다.
- 기존 Wrangler 로그인에는 `containers:write`/`cloudchamber:write`가 없어 이관용 권한 승인이 필요하다.
- 이 Mac에는 Docker 실행 환경이 없다. Worker 번들 dry-run은 성공했고 이미지 빌드·RTMPS TLS 확인은 별도 CI에서 수행한다.
- Google OAuth 웹 클라이언트의 승인 출처에 Pages 운영 URL을 추가해야 한다.
- 운영 DB 연결, callback key, AES key를 그대로 옮겨야 기존 로그인·암호화 연결정보를 보존한다. `API_CONFIG`는 Cloudflare secret으로만 주입한다. 실제 값을 예시 파일에 쓰거나 로그에 출력하지 않는다.
- `REPLAY_OBJECT_KEY`는 기존 control/callback 키와 다른 새 32자 이상의 Cloudflare secret이다.

## 검증

- 실제 로컬 workerd/R2: 업로드·공급자 SHA-256 검증·덮어쓰기 거절·전체/구간 재생·ETag·만료/변조/다른 객체 권한·교차 출처 거절.
- Python: 기존 도우미→API→검증과 새로운 Cloudflare 작업 endpoint의 실제 FFmpeg 검증. 설정·권한·60초 상태 캐시 검사.
- 기존 웹 클라이언트 198개 테스트, lint, TypeScript, Pages 빌드.
- Pages 산출물에 Vercel 운영 URL 없음.
- Cloudflare 실제 배포, 운영 Google 로그인, 사용자 YouTube 링크의 운영 업로드·ready 전환, 실제 RTMP 송출은 아직 미검증이다.

## 승인 이후 전환 순서

1. 이 PR을 승인 후 `develop`에 머지한다. 별도 `develop → main` PR도 해당 PR 승인을 받은 뒤 머지한다.
2. 유료 플랜/권한을 확인하고 전용 비공개 R2 버킷을 만든다. 공개 `r2.dev`와 사용자 지정 공개 도메인은 활성화하지 않는다.
3. 확정 main 커밋으로 Container 이미지와 Worker를 배포한다. `REPLAY_VERSION`을 커밋으로 지정하고 제한된 API 설정을 secret으로 주입한다.
4. 기존 Vercel dispatcher를 중지하고 남은 lease가 종료된 것을 확인한다. DB는 삭제·초기화하지 않는다. Cloudflare dispatcher만 동일 DB에서 작업하도록 전환한다.
5. Google 승인 출처·Pages 공개 빌드 설정·PC 도우미의 고정 API와 허용 출처를 갱신한다.
6. 실제 Chrome에서 로그인→코드 자동 적재→PC 연결→사용자 링크 다운로드→R2 업로드→검증 완료→재생을 확인한다. 파일 송출 및 예약·취소도 확인한다.
7. 위 운영 검증 후 기존 Vercel 배포/cron/queue와 Blob 의존성을 정리한다. Neon DB 및 연결된 데이터는 보존한다. 원래 Vercel Blob 자체가 정지되어 있으므로 구 배포로 되돌리는 것만으로 업로드 장애가 복구되지는 않는다.

공식 구현 근거: [Pages](https://developers.cloudflare.com/pages/), [R2 checksum/조건부 쓰기](https://developers.cloudflare.com/r2/api/workers/workers-api-reference/), [Containers](https://developers.cloudflare.com/containers/reference/container-class/).
