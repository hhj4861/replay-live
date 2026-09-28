# main 운영 자동 배포

2026-09-28 사용자 지시: develop까지 로컬에서 확인하고, develop → main PR을 머지하면 Replay 운영 사이트에 자동 배포한다. 각 PR 머지는 별도 명시적 승인을 받는다.

## 실행 순서

1. feat/* → develop에서 로컬 확인·CI 검증. 이 단계는 배포하지 않는다.
2. 승인된 develop → main PR을 머지한다.
3. Deploy Replay production이 같은 커밋의 Commercial·Cloudflare 검증 워크플로를 재사용한다. 둘 다 성공해야 배포한다.
4. 정확한 Python/FFmpeg 버전의 워커 스냅샷을 생성하고 미디어 smoke test를 수행한다.
5. API와 웹을 --prod --skip-domain으로 준비한다. 두 빌드가 READY이고 main 최신 커밋과 현재 운영 배포가 그대로일 때 API → 웹 순서로 전환한다.
6. API /api/live의 버전, 웹 /_replay-release.json의 커밋·스냅샷, 웹 HTML 응답을 확인한다. 실패하면 이전 API·웹 배포로 rollback하고 실제 alias가 복구됐는지 확인한다.

운영 URL: https://replay-live-poc.vercel.app

API: https://replay-live-api.vercel.app

## 최초 설정

저장소 Actions secret VERCEL_TOKEN에 dean-10 팀의 배포·Sandbox 접근 가능한 토큰을 등록한다. 토큰 값은 Git·로그·채팅에 기록하지 않는다. 만료 전 갱신은 같은 secret을 교체한다. production environment에 별도의 승인 규칙이 있으면 그 규칙은 유지되므로 배포가 대기할 수 있다.

프로젝트 ID와 팀 ID는 배포 스크립트에 현재 서비스의 공개 식별자로 고정했다. DB·Google 로그인·프록시·R2 인증값은 기존 Vercel 프로젝트 설정을 원격 빌드에서 사용한다. vercel env pull을 실행하거나 운영 환경변수를 파일로 내보내지 않는다. CLI 60.1.3은 VERCEL_TOKEN 환경변수를 사용하므로 명령 인수에 토큰을 넣지 않는다.

워커 FFmpeg는 7:8.0.1-3ubuntu2에 고정되어 있다. 빌드 이미지 저장소에서 이 버전이 사라지면 운영 전환 전에 실패한다. 검증한 새 버전으로 PR에서 변경한다.

## 배포 범위와 복구

- 자동 배포 범위는 현재 운영의 Vercel 웹·Python API·Sandbox 워커다. 기존 Cloudflare R2 저장소는 그대로 사용한다. Cloudflare 인프라 변경과 운영 DB 마이그레이션은 이 워크플로가 실행하지 않으며 해당 릴리스 전에 별도로 완료해야 한다.
- 소스는 Git에 등록된 서버·웹 파일만 격리된 임시 디렉터리에 복사한다. 로컬 환경변수 파일과 캐시를 배포하지 않는다. 프로젝트에 저장된 비밀값을 사용하기 위해 원격 빌드하며, 비밀을 내려받는 prebuilt 빌드는 사용하지 않는다.
- 워크플로 전체를 replay-production 그룹으로 직렬화하고 실행 중인 배포를 새 push가 취소하지 않는다. 새 커밋 때문에 뒤처진 실행은 전환 직전에 중단한다.
- 두 프로젝트의 전환은 원자적이지 않으므로 API와 웹 사이에 짧은 버전 불일치 구간이 생길 수 있다. 이미 실행 중인 영상 처리 작업은 자동으로 중지하지 않는다.
- 기존 보관함 데이터·DB 스키마를 rollback하지 않는다. 자동 smoke는 공개 응답과 릴리스 정합성까지 검사하며 Google OAuth·실제 YouTube 다운로드 성공을 뜻하지 않는다.
- replay-production-<commit>-<attempt> artifact에 이전/후보 배포 ID, 워커 manifest, 전환 시도 및 복구 결과를 30일 보관한다. 비밀값·DB 백업은 포함하지 않는다.
- rolled_back도 배포 실패로 종료한다. manual_recovery_required면 artifact의 이전 배포 ID를 확인해 vercel rollback <deployment-id> --yes를 각 해당 프로젝트에서 실행하고 alias를 확인한다. 다른 운영 변경이 감지되면 덮어쓰지 않는다.
- 수동 강제 취소·runner 소실·전체 job 시간 초과 때는 Python 복구가 실행되지 않을 수 있다. GitHub artifact 업로드도 실패했다면 Vercel deployment history에서 이전 두 프로젝트를 확인한다.
- 같은 main 커밋을 다시 배포하려면 Actions의 Run workflow에서 main을 선택한다. develop이나 기능 브랜치 실행은 배포하지 않는다.

## 검증 근거

tests/test_production_deploy.py는 후보 빌드 실패 시 무변경, 최신 main 제한, 전환 응답 유실, smoke 실패, 복구 실패, 동시 변경 보존, 비밀 파일 제외를 검사한다. 클라우드 호출을 대체하는 로컬 테스트이며 실제 운영 전환 성공과 구분한다. 첫 실제 자동 배포는 이 워크플로의 승인된 main 머지 후 확인해야 한다.

참고: [GitHub 재사용 워크플로](https://docs.github.com/en/actions/concepts/workflows-and-actions/reusing-workflows), [Vercel 배포 준비](https://vercel.com/docs/cli/deploy), [Vercel promote](https://vercel.com/docs/cli/promote), [Vercel rollback](https://vercel.com/docs/cli/rollback).

