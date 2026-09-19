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

## 승인 대기와 남은 순서

1. 자동 승인 검토가 Production 환경변수 전체의 로컬 파일 복사를 비밀정보 저장 위험으로 거절했다. DB 연결정보만 메모리에서 읽고 파일·로그·Git에 남기지 않는 좁은 범위를 사용자에게 요청했다. 답변 전에는 DB 정보 조회·백업·마이그레이션을 진행하지 않는다.
2. 실제 Chrome 창에서 운영 사이트와 Google 계정 선택 화면까지 열었다. 자동 승인 검토가 계정 임의 선택을 거절하여 사용자에게 사용할 계정 지정을 요청했다. 로그인·사용자 영상 작업은 아직 실행하지 않았다.
3. 승인 후 DB의 현재 migration/checksum을 확인하고 필요한 스키마를 적용·검증한다. 기존 데이터·관리자 권한은 임의 변경하지 않는다.
4. API → 웹 순서로 정식 운영 도메인을 전환하고 공개 상태·인증 경계·오류 로그를 확인한다.
5. 사용자 PC에서 새 데몬을 실행·연결한 뒤 제공된 YouTube 링크를 운영 브라우저에서 가져온다. 로컬 다운로드 → 직접 업로드 → 클라우드 검증 → 미리보기 재생을 확인한다.

이 기록은 배포 준비 증거이며 운영 반영 또는 운영 E2E 성공 보고가 아니다.
