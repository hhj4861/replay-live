# Replay — Codex 프로젝트 지침

## GitOps 작업 인계 — 사용자 지시 2026-09-29

담당 세션: `Replay`. 저장소: `hhj4861/replay-live`.
작업 시작·재개·문맥 압축 후 이 지침과 `docs/branch-workflow.md`를 읽는다.

- 목표: `deploy/replay`에 승인된 PR을 머지하면 Vercel 웹 `replay-live-poc`와 API `replay-live-api`가 자동 배포되도록 GitOps를 완성한다.
- 기존 통합 순서 `feat/* → develop → main` 뒤에 `main → deploy/replay` 승격을 추가한다. 기존 규칙을 임의로 생략하지 않는다.
- 웹 소스는 `web/`, API는 저장소 루트다. 실제 Vercel 설정/배포 이력은 재조회하고 두 프로젝트 및 worker snapshot의 호환되는 revision을 함께 관리한다.
- 공용 구성 PR https://github.com/hhj4861/commerce-automation-kit/pull/60 은 main에 머지됐다. Replay 배포 구현/운영 활성화가 끝났다는 뜻은 아니다.
- 공용 가이드: https://github.com/hhj4861/commerce-automation-kit/blob/main/docs/deployment/platform-gitops.md
- 담당별 요청: https://github.com/hhj4861/commerce-automation-kit/blob/main/docs/deployment/codex-gitops-handoff.md
- 브랜치 생성/PR 필수 보호, CI 배포 자격, 실제 deployment workflow, 중복 native Git 배포 전환, 첫 운영 배포까지 완료한다. 최초 branch 생성은 자동 배포하지 않는다.
- 배포 비밀은 Cloudflare 중앙 비밀관리에서 repo ID/소유자/ref/workflow별 OIDC 신뢰로 필요한 키만 받는다. 다른 프로젝트 키를 무조건 공유하거나 GitHub에 장기 비밀을 중복 저장하지 않는다. 공용 broker 정책 변경은 소유자와 조정한다.
- 기존 DB 마이그레이션·queue/worker snapshot·방송 중 작업의 drain/복구 절차를 확인한다. 기존 GCP 진단 중단 지시는 유지하며 이 요청을 GCP 이전으로 해석하지 않는다.
- 완료 증거: 배포 workflow URL, commit SHA, 웹/API 운영 URL, 실제 로그인/핵심 API 흐름, 실패/복구 확인. 검증 CI 통과만으로 운영 배포 완료라 하지 않는다.

## 작업 경계와 수신 보고

구현은 별도 worktree, 문서·단순 작업은 현재 checkout에서 처리한다. 변경 전 소유 경로 등록 후 본인 변경만 검증·커밋·현재 upstream push한다.
다른 세션의 수정/staged/미완료 파일을 포함하지 않는다. force push·훅 우회 금지. 각 PR 실제 머지는 사용자 명시 승인 후 수행한다.
이 기록은 새 PR 머지 승인이나 운영 배포 완료가 아니다. 이 지침을 실제 읽은 세션은 수신 사실과 후속 작업을 사용자에게 알린다.
파일 저장·push를 이미 열린 세션의 즉시 수신/실행으로 보고하지 않는다.
