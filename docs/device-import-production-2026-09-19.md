# PC 데몬 영상 가져오기 운영 배포 — 2026-09-19

## 현재 상태

사용자가 PR #1 (`feat → develop`)과 PR #2 (`develop → main`) 머지, 이후 운영 배포·실제 브라우저 E2E 진행을 승인했다.
최초 배포 기준은 `5918f47`이며, 현재 운영 기준은 사용자 승인으로 PR #4를 머지한 `main` 커밋 `6ba5e32d89376948065ce28dd71b4f6d2a2c692a`다.
**2026-09-20 오류 표시 수정의 main 머지·운영 배포·실제 Chrome 화면 확인을 완료했다. 영상 가져오기 전체 E2E는 저장소 업로드 준비 단계의 502로 미완료다.**

## 오류 표시 운영 반영 — 2026-09-20 13:31~13:37 KST

- 사용자 승인과 PR #4의 두 CI 통과를 확인한 뒤 `develop → main` 머지를 수행했다. main의 전체 파일 트리는 검증한 작업 커밋 `8d4481e`와 동일하다.
- main만 Git archive로 묶어 `/private/tmp/replay-production-6ba5e32-8lx141oc`에서 API와 웹을 배포했다. 실제 환경변수 파일을 내보내거나 변경하지 않았으며 추가 DB 마이그레이션은 없다.
- API 배포 `dpl_H4cJMZhMWPKHX1HCDxvVFyiLUtP8`, 웹 배포 `dpl_BRsZXC8i1ULWFzErga1Y4kg9tNNG`가 READY다. API → 웹 순서로 promote한 뒤 REST로 두 정식 alias의 배포 ID 일치를 확인했다.
- <https://replay-live-poc.vercel.app> 루트 200과 새 `index-BZ4L57Al.js`·`index-B8Gkyp4Z.css` 참조를 확인했다. API `/api/live` 200, 비인증 `/api/health` 401이다.
- 이 Mac에서 이전 시험 도우미가 종료된 것을 확인하고 main 버전 도우미를 `127.0.0.1:17833`에 실행했다. 연결 코드는 다시 발급되며 보고서에는 저장하지 않는다.
- 실제 Chrome 운영 탭에서 새로고침, 도우미 연결, 제공된 YouTube 링크 입력 및 가져오기를 실행했다. 가져오기 버튼 아래에 **“보관함 업로드에 실패했습니다.”**, 원인 안내, **“현재 링크로 다시 가져오기”**, **“MP4 파일로 업로드”** 버튼이 표시됐고 오류 카드로 초점이 이동했다. 스크린샷과 접근성 트리로 확인했다.
- 04:36:53~59 UTC 요청 로그: 작업 생성 201 → PC 작업 조회 200 → 업로드 준비 400, 이에 대응하는 웹 `/api/blob-control` 502 → 실패 보고 200 → 작업 정리 204. 새 배포에서도 실패가 재현되어 영상 가져오기 성공으로 처리하지 않는다.
- 저장소 제어 함수는 상세 예외를 공통 `STORAGE_UNAVAILABLE` 502로 처리하며 세부 원인을 기록하지 않는다. 현재 로그로는 서명 발급 실패의 하위 원인을 확정할 수 없다. 후속 조치는 토큰·서명 URL·원본 응답을 제외한 단계별 오류 진단을 추가해 원인을 확인하고 실제 링크 가져오기를 다시 검증하는 것이다.

## 최신 재검증 — 2026-09-20

- Chrome 조작 충돌 후 사용자가 다른 조작을 중단하여 검증을 재개했다. 운영 화면에서 PC 도우미를 연결하고 제공된 YouTube 링크의 가져오기를 실행했다.
- 작업 생성 201, 도우미 작업 조회 200 이후 업로드 준비 API가 400을 반환했다. 같은 시각 웹 저장소 제어 요청은 502였다. 실패 보고 200과 작업 정리 204를 확인했다.
- 같은 다운로드 경로로 PC에서 원본 1,194,527바이트를 내려받는 데 성공했다. 실패를 원본 플랫폼 봇 차단으로 단정하지 않는다. 저장소 제어 502의 원인은 아직 확정하지 않았다.
- 업로드 오류 코드를 일반 다운로드 오류로 바꾸던 도우미 예외 처리와 오류 표시 UI를 수정했다. 사용자 승인 후 [PR #3](https://github.com/hhj4861/replay-live/pull/3)을 `develop`에 머지했다(`ee815c2`, 2026-09-20). 머지된 전체 파일 트리는 검증한 `8d4481e`와 동일하다.
- [PR #4](https://github.com/hhj4861/replay-live/pull/4)는 두 CI 통과와 사용자 승인 후 `main`에 머지했다. 현재 운영 반영 결과는 위 절과 같다.
- 실제 Chrome 로컬 합성 실패 화면에서 오류 카드, 초점 이동, 재시도 및 MP4 전환을 확인했다. Python 36개·클라이언트 191개 테스트, 타입 검사·lint·상용 빌드가 통과했다. 운영 영상 E2E 성공으로 계산하지 않는다.
- 저장소 업로드 준비 502 해소 및 보관함 준비 완료 확인이 남아 있다.

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
