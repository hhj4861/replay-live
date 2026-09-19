# PC 데몬 영상 가져오기 운영 배포 — 2026-09-19

## 현재 상태

사용자가 PR #1 (`feat → develop`)과 PR #2 (`develop → main`) 머지, 이후 운영 배포·실제 브라우저 E2E 진행을 승인했다.
배포 기준은 `main` 커밋 `5918f477dddf08982758c433fe048b5036eb531e`다.
**새 API·웹 빌드는 READY지만 정식 운영 도메인 전환·DB 마이그레이션·운영 E2E는 미완료다.**

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

## 운영 주소 상태

- <https://replay-live-poc.vercel.app>: 기존 `dpl_AVE2xC6awHwiLySr1G1tZVEhjCzz` 유지.
- <https://replay-live-api.vercel.app>: 기존 `dpl_5zappXvaU1tsLqpBHgRgviswYhmx` 유지, `/api/live` 200.
- CLI가 프로젝트 보조 alias (`*-dean-10.vercel.app`)를 새 배포에 연결했지만 위 정식 운영 alias는 REST로 기존 배포 유지를 확인했다.

## 2026-09-19 추가 확인과 남은 순서

- 사용자는 DB 연결정보의 메모리 전용 사용과 개인 Google 계정의 운영 E2E 로그인을 승인했다.
- Vercel의 Production `DATABASE_URL` 한 항목만 메모리로 읽었다. 연결정보를 파일·로그·Git에 저장하지 않았다.
- 운영 PostgreSQL 18.6에서 읽기 전용으로 확인한 적용 버전은 001~008이며 모든 체크섬이 이번 릴리스와 일치했다. 대기 중인 009는 감사 테이블, 010은 PC 가져오기 테이블을 추가한다. 기존 데이터·관리자 권한 변경은 없다.
- 기존 Mac 백업 도구는 PostgreSQL 17.5였다. 공식 Homebrew PostgreSQL 18.6 패키지를 SHA-256 검증 후 `/private/tmp/replay-pg18-client`에만 풀어 실행을 확인했다. 시스템 설치는 변경하지 않았다.
- 실제 Chrome에서 사용자가 지정한 개인 Google 계정 로그인이 성공했고 운영 스튜디오 화면을 확인했다. 이 화면은 아직 이전 배포다.
- 승인된 main 소스의 PC 데몬을 `127.0.0.1:17833`에서 실행했다. 새 웹 연결 및 실제 영상 가져오기는 아직 실행하지 않았다.

### 현재 차단점

자동 승인 검토가 운영 DB 전체의 로컬 백업 저장을 거절했다. 메모리 전용 연결정보 사용·백업 목적은 승인됐지만, 사용자·세션 등 데이터의 구체적인 로컬 저장 범위·위치 승인이 필요하다는 이유다.
사용자에게 `/private/tmp/replay-production-before-010-20260919.dump`에 소유자 전용 0600 권한으로 전체 백업을 저장하는 승인을 요청했다. 현재 백업 파일은 생성하지 않았고 DB를 변경하지 않았다.

### 승인 후 실행 순서

1. 일관된 읽기 전용 스냅샷으로 백업하고 아카이브·체크섬을 검증한다.
2. 승인된 main의 009·010 마이그레이션을 트랜잭션으로 적용하고 전체 스키마·체크섬을 검증한다.
3. API → 웹 순서로 정식 운영 도메인을 전환하고 공개 상태·인증 경계를 확인한다.
4. 현재 로그인된 운영 Chrome에서 PC 데몬을 연결하고 제공된 YouTube 링크의 로컬 다운로드 → 직접 업로드 → 클라우드 검증 → 미리보기 재생을 확인한다.

운영 배포 전환과 운영 영상 E2E는 미완료다.
