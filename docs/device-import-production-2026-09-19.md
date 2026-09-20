# PC 데몬 영상 가져오기 운영 배포 — 2026-09-19

## 현재 상태

사용자가 PR #1 (`feat → develop`)과 PR #2 (`develop → main`) 머지, 이후 운영 배포·실제 브라우저 E2E 진행을 승인했다.
배포 기준은 `main` 커밋 `5918f477dddf08982758c433fe048b5036eb531e`다.
**2026-09-20 운영 DB 백업·마이그레이션과 API·웹의 정식 운영 도메인 전환을 완료했다. 실제 Chrome E2E는 로그인·PC 연결·작업 요청까지 성공했지만 저장소 업로드 준비 단계의 502로 미완료다.**

## 최신 재검증 — 2026-09-20

- Chrome 조작 충돌 후 사용자가 다른 조작을 중단하여 검증을 재개했다. 운영 화면에서 PC 도우미를 연결하고 제공된 YouTube 링크의 가져오기를 실행했다.
- 작업 생성 201, 도우미 작업 조회 200 이후 업로드 준비 API가 400을 반환했다. 같은 시각 웹 저장소 제어 요청은 502였다. 실패 보고 200과 작업 정리 204를 확인했다.
- 같은 다운로드 경로로 PC에서 원본 1,194,527바이트를 내려받는 데 성공했다. 실패를 원본 플랫폼 봇 차단으로 단정하지 않는다. 저장소 제어 502의 원인은 아직 확정하지 않았다.
- 업로드 오류 코드를 일반 다운로드 오류로 바꾸던 도우미 예외 처리와 오류 표시 UI를 수정했다. 사용자 승인 후 [PR #3](https://github.com/hhj4861/replay-live/pull/3)을 `develop`에 머지했다(`ee815c2`, 2026-09-20). 머지된 전체 파일 트리는 검증한 `8d4481e`와 동일하다.
- [PR #4](https://github.com/hhj4861/replay-live/pull/4)를 `develop → main`으로 생성했다. PR별 승인 규칙에 따라 main 머지 승인을 기다리며, main·운영에는 아직 반영하지 않았다. PR #3의 두 CI는 통과했고 PR #4의 CI는 새로 실행된다.
- 실제 Chrome 로컬 합성 실패 화면에서 오류 카드, 초점 이동, 재시도 및 MP4 전환을 확인했다. Python 36개·클라이언트 191개 테스트, 타입 검사·lint·상용 빌드가 통과했다. 운영 영상 E2E 성공으로 계산하지 않는다.
- 다음 단계는 PR #4 승인·CI 통과 후 main 반영과 운영 재검증이다. 저장소 업로드 준비 502 해소 및 보관함 준비 완료 확인이 남아 있다.

## 준비와 확인

- Git archive로 승인된 커밋만 격리 패키징했다. 기존 checkout의 미커밋 파일은 포함하지 않았다.
- 저장소의 `deploy/commercial/api.vercel.json`, `web.free.vercel.json`을 각 배포 manifest로 사용했다.
- Vercel 업로드 목록을 원본 커밋 내용과 바이트 단위로 대조했다. API 62개·웹 125개 파일 모두 일치하며 비밀 파일·로컬 데이터가 없다.
- `deploy --prod --skip-domain`으로 기존 Production 설정을 클라우드 빌드에 사용했다. Production 비밀 환경변수를 로컬 파일로 내려받지 않았다.
- API: `dpl_D6H9LDn4zijdQ4uuA2ckoeSJFsrM`, READY, <https://replay-live-4f5y7qqsi-dean-10.vercel.app>.
- 웹: `dpl_8v2eNJ1FsbWLxBb41AvoQ2kUpRzE`, READY, <https://replay-live-1byc4hdye-dean-10.vercel.app>.
- 웹 빌드·TypeScript 함수 빌드가 성공했다. 인증된 Vercel CLI 요청으로 새 HTML 및 `index-C2g7-lqX.js`, `index-BidwBSqh.css` 참조를 확인했다.
- API 고유 배포 URL의 런타임 요청은 기존 정확한 Host 허용 정책에 따라 `Invalid host header`를 반환한다. 정책을 완화하지 않았으며 운영 API 성공으로 계산하지 않는다.
- 기존 운영 워커 스냅숏의 일곱 파일 SHA-256이 이번 소스와 모두 동일하다. 스냅숏 재생성·환경 값 변경은 하지 않았다.

## 운영 반영 — 2026-09-20

- 사용자 승인 경로 `/private/tmp/replay-production-before-010-20260919.dump`에 PostgreSQL 18.6 `pg_dump`로 21개 테이블을 일관된 읽기 전용 스냅샷에서 백업했다. 권한 0600, 크기 46,153 bytes이며 `pg_restore --list`가 성공했다. 실제 별도 DB 복원 시험은 수행하지 않았다.
- 백업 SHA-256: `c7da481fdc99256f85f44fed90e1d6217dc526cb8830778d2ec5060e9955b885`. 접속정보는 파일·로그·Git에 저장하지 않았다. 건수·체크섬 manifest도 0600 권한으로 백업 옆에 보관한다.
- 승인된 main의 009·010을 마이그레이션 도구의 트랜잭션으로 적용했다. 전체 스키마·migration 체크섬 검증이 통과했다. 기존 테이블 건수는 migration 이력 8→10을 제외하고 모두 동일했고 신규 테이블 두 개는 비어 있었다.
- API → 웹 순서로 Vercel `promote`를 실행했다. 이후 REST로 아래 정식 alias의 배포 ID 일치를 검증했다.
- <https://replay-live-api.vercel.app>: `dpl_D6H9LDn4zijdQ4uuA2ckoeSJFsrM`.
- <https://replay-live-poc.vercel.app>: `dpl_8v2eNJ1FsbWLxBb41AvoQ2kUpRzE`.
- API `/api/live` 200, 무인증 `/api/health` GET 및 `/api/device-imports` POST는 401이다. 웹 루트는 200이며 새 `index-C2g7-lqX.js` 파일 참조를 확인했다.
- 전환 직후 API `/api/health`에서 503 두 건이 기록됐다. 이후 같은 경로의 200 응답이 여러 차례 관찰되어 정상 복구를 확인했다. 제한된 로그에 응답 본문이 없어 초기 503의 상세 원인은 확정하지 않았다. 같은 조회 기간 웹 5xx 기록은 없었다.
- 기존 워커 내용이 동일하므로 `REPLAY_VERSION`은 `rpl-20260911-68fc62096484a16c`를 유지한다. 버전 문자열 대신 위 배포 ID와 웹 파일로 이번 반영을 확인했다.
- 복구 참고용 이전 배포: API `dpl_5zappXvaU1tsLqpBHgRgviswYhmx`, 웹 `dpl_AVE2xC6awHwiLySr1G1tZVEhjCzz`. 현재 두 alias는 새 배포에 연결돼 있다.

## 2026-09-19 추가 확인과 남은 순서

- 사용자는 DB 연결정보의 메모리 전용 사용과 개인 Google 계정의 운영 E2E 로그인을 승인했다.
- Vercel의 Production `DATABASE_URL` 한 항목만 메모리로 읽었다. 연결정보를 파일·로그·Git에 저장하지 않았다.
- 운영 PostgreSQL 18.6에서 읽기 전용으로 확인한 적용 버전은 001~008이며 모든 체크섬이 이번 릴리스와 일치했다. 대기 중인 009는 감사 테이블, 010은 PC 가져오기 테이블을 추가한다. 기존 데이터·관리자 권한 변경은 없다.
- 기존 Mac 백업 도구는 PostgreSQL 17.5였다. 공식 Homebrew PostgreSQL 18.6 패키지를 SHA-256 검증 후 `/private/tmp/replay-pg18-client`에만 풀어 실행을 확인했다. 시스템 설치는 변경하지 않았다.
- 실제 Chrome에서 사용자가 지정한 개인 Google 계정 로그인이 성공했고 운영 스튜디오 화면을 확인했다. 이 화면은 아직 이전 배포다.
- 승인된 main 소스의 PC 데몬을 `127.0.0.1:17833`에서 실행했다. 새 웹 연결 및 실제 영상 가져오기는 아직 실행하지 않았다.

### 승인 해결과 운영 E2E 잔여

DB 연결정보의 메모리 전용 사용, 개인 Google 계정 로그인, 지정 경로의 운영 전체 백업은 모두 사용자 승인을 받았다. 백업 저장 범위 때문에 발생했던 자동 승인 검토 차단은 해결됐다.

2026-09-20 운영 전환 후 실제 Chrome 창을 열었으나 다른 작업에 의해 활성 탭·창이 반복 변경되어 CUA가 조작을 중단했다. stale 탭 선택을 재시도하지 않고 새 창으로 분리했지만 동일한 화면 점유 충돌이 발생했다. 사용자에게 약 3분 동안 다른 Chrome 조작을 중단할 수 있는지 요청했다.

남은 작업은 새 운영 웹에서 PC 데몬 연결 → 제공된 YouTube 링크 로컬 다운로드 → 직접 업로드 → 클라우드 검증 → 미리보기 재생이다. 이전 로컬 E2E와 9월 19일 운영 로그인 성공을 이번 운영 영상 E2E 성공으로 계산하지 않는다.
