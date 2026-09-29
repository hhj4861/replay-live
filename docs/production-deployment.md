# Replay 운영 자동 배포

2026-09-29 GitOps 지침: `feat/* → develop → main → deploy/replay` 순서로 통합한다. develop/main에서는 배포하지 않고, 보호된 `deploy/replay` PR 머지만 운영 배포한다. 각 PR 머지는 별도 명시적 승인을 받는다. 최초 브랜치 생성 이벤트는 배포하지 않는다.

## 실행 순서

1. feat/* → develop에서 로컬 확인·CI 검증. 이 단계는 배포하지 않는다.
2. 승인된 develop → main PR을 머지한 다음 main → deploy/replay PR을 별도로 승인·머지한다.
3. Deploy Replay production이 같은 커밋의 Commercial·Cloudflare 검증 워크플로를 재사용한다. 둘 다 성공해야 배포한다.
4. 정확한 Python/FFmpeg 버전의 워커 스냅샷을 생성하고 미디어 smoke test를 수행한다.
5. API와 웹을 --prod --skip-domain으로 준비한다. 두 빌드가 READY이고 deploy/replay 최신 커밋과 현재 운영 배포가 그대로일 때 API → 웹 순서로 전환한다.
6. API /api/live의 버전, 웹 /_replay-release.json의 커밋·스냅샷, 웹 HTML 응답을 확인한다. 실패하면 이전 API·웹 배포로 rollback하고 실제 alias가 복구됐는지 확인한다.

운영 URL: https://replay-live-poc.vercel.app

API: https://replay-live-api.vercel.app

## 최초 설정

배포 자격은 `scripts/deploy-credentials.mjs`가 GitHub OIDC를 Cloudflare 중앙 브로커에 교환하여 메모리에서만 사용한다. 기존 GitHub `VERCEL_TOKEN`을 읽거나 새 장기 비밀을 복사하지 않는다. 중앙 브로커의 **Replay 전용 정책·키 등록은 아직 미완료**이며 미등록 시 배포를 차단한다.

중앙 브로커 담당자가 적용할 계약:

- endpoint: `https://cak-credential-broker.guswhd1085.workers.dev/github/secrets`, audience `cak-cloudflare-secrets`
- repository `hhj4861/replay-live`, repository ID `1365111099`, owner ID `71001056`
- ref `refs/heads/deploy/replay`, workflow `hhj4861/replay-live/.github/workflows/deploy-production.yml@refs/heads/deploy/replay`
- GitHub hosted runner, push/workflow_dispatch만 허용. 환경은 `production`이므로 실제 GitHub subject 설정을 확인하여 정확히 제한한다. 포크/PR/다른 workflow/ref는 거부한다.
- 응답 키는 Replay 배포 권한의 `REPLAY_DEPLOY_VERCEL_TOKEN` 한 개만 허용한다. 다른 프로젝트의 자격을 반환하지 않는다.
- `deploy/replay`에 PR 필수·강제 push 금지·필수 CI 보호를 적용한다. 실행기는 보호되지 않은 ref와 뒤처진 SHA를 거부한다.
- 최초 브랜치는 검증된 기존 main을 기준으로 준비하고 자동 배포하지 않는다. 이후 승인된 운영 승격 PR로 시작한다.

2026-09-29 Vercel 조회에서 두 프로젝트 모두 Git 연결이 없음을 확인했다. 첫 전환 직전에 재확인하며 기존 native Git 배포가 생겼으면 중복 실행을 먼저 해결한다.

프로젝트 ID와 팀 ID는 배포 스크립트에 현재 서비스의 공개 식별자로 고정했다. DB·Google 로그인·프록시·R2 인증값은 기존 Vercel 프로젝트 설정을 원격 빌드에서 사용한다. vercel env pull을 실행하거나 운영 환경변수를 파일로 내보내지 않는다. 브로커 실행기는 자식 프로세스의 VERCEL_TOKEN 환경변수로만 전달하므로 명령 인수에 토큰을 넣지 않는다.

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
- 같은 배포 커밋을 다시 배포하려면 Actions의 Run workflow에서 deploy/replay를 선택한다. main/develop/기능 브랜치 실행은 배포하지 않는다.

## 검증 근거

tests/test_production_deploy.py는 후보 빌드 실패 시 무변경, 최신 보호된 deploy/replay 제한, 전환 응답 유실, smoke 실패, 복구 실패, 동시 변경 보존, 비밀 파일 제외를 검사한다. 클라우드 호출을 대체하는 로컬 테스트이며 실제 운영 전환 성공과 구분한다. 첫 실제 자동 배포는 이 워크플로의 승인된 deploy/replay 머지 후 확인해야 한다.

참고: [GitHub 재사용 워크플로](https://docs.github.com/en/actions/concepts/workflows-and-actions/reusing-workflows), [Vercel 배포 준비](https://vercel.com/docs/cli/deploy), [Vercel promote](https://vercel.com/docs/cli/promote), [Vercel rollback](https://vercel.com/docs/cli/rollback).


## 자동 송출 활성화 순서

1. 운영 DB 연결정보만 메모리로 읽어 비공개 백업을 확보하고, 활성 방송/작업이 없는지 확인한다. `012_automations.sql`, `013_youtube_live.sql`의 추가 스키마를 적용하고 `commercial-verify.py --database-check`로 확인한다. 기존 테이블·사용자 영상은 삭제하지 않는다.
2. API 프로젝트에 웹 OAuth `REPLAY_YOUTUBE_CLIENT_ID`, `REPLAY_YOUTUBE_CLIENT_SECRET`과 `REPLAY_AUTOMATIONS_ENABLED=1`을 설정한다. callback은 `https://replay-live-api.vercel.app/api/youtube-channel/callback`이다. 로컬 Desktop OAuth와 로컬 사용자 토큰을 운영에 복사하지 않는다.
3. 승인된 승격 배포 후 `/api/live`의 버전·`automations_enabled=true`, 웹 릴리스 SHA, 실제 로그인→자동 송출 화면→YouTube 동의를 확인한다. 운영 사용자별 채널 동의·플랫폼 저장은 새로 필요하다.
4. 큐는 `/internal/automations/tick`→기존 dispatcher→`/internal/next-wakeup` 순서로 진행한다. 생성/재개는 즉시 깨우고, DB의 다음 예약 또는 활성 회차 lease 시각으로 지연 메시지를 보낸다. 30초 단위 큐 반올림과 콜드 스타트 지연이 있을 수 있으며 초 단위 정시성을 보장하지 않는다. 일일 wake cron은 복구용이다.
5. 실제 운영 검증 전에는 사용자 테스트 일정을 생성하지 않는다. 배포만으로 로컬 테스트 일정·스트림 키·Google 토큰은 이전되지 않는다. 실패 시 웹/API를 기존 버전으로 복구하고 추가 스키마와 기존 데이터를 유지한다.

이 문서는 배포 준비 상태다. OAuth, 중앙 브로커 정책, 운영 백업/마이그레이션, PR 승격과 첫 배포는 각각 실제 완료 증거로 확인해야 한다.
