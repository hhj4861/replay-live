# 도우미 0.2.0 개발 릴리스 — 2026-09-26

공개 위치: https://github.com/hhj4861/replay-live/releases/tag/helper-v0.2.0

서명·공증 없는 개발용 prerelease다. OS가 실행을 제한할 수 있으며 보안 설정 해제를 안내하지 않는다. 웹 동의는 파일 다운로드를 시작하며, 파일 열기와 네이티브 설치·실행 승인은 사용자가 수행한다. 일반 사용자용 서명 릴리스 및 새 사용자 계정의 OS 설치 시험은 완료하지 않았다.

## 빌드와 공개 파일

- 소스: 운영 main `cbfafa1edbca48886daad7d78a325f7d39289ef0`.
- [Actions 실행 36205898354](https://github.com/hhj4861/replay-live/actions/runs/36205898354): macOS Apple Silicon, macOS Intel, Windows x64 빌드 및 각 frozen executable self-test 성공.
- 이 Mac에서 Apple Silicon 패키지의 미디어 decode·로컬 pairing self-test도 종료 코드 0. 실제 앱 설치·시작 프로그램 등록은 수행하지 않았다.
- 원본 CI ZIP 바깥 루트에 THIRD_PARTY_NOTICES.txt와 GPLv3 사본을 추가했다. 앱 바이너리는 변경하지 않았다. 최종 ZIP·플랫폼 JSON·SHA-256 및 FFmpeg 소스/안내 ZIP을 함께 공개했다.
- FFmpeg 소스와 외부 라이브러리 소스 목록을 제공한다. 전체 외부 의존성의 독립적인 라이선스 감사를 완료했다는 의미는 아니다.
- 세 플랫폼 ZIP 모두 인증 없이 HTTP 200, 크기·SHA-256 일치 확인.

| 플랫폼 | 바이트 | 최종 ZIP SHA-256 |
| --- | ---: | --- |
| macOS Apple Silicon | 46,526,615 | `bbad30d80978b21ded9205c61c21ce8b3bc92bfe697d3575cdc26dba558e9d37` |
| macOS Intel | 47,440,998 | `316f80384a39fe40fcb9f04a6ccf71c699dd41e55303eddf174c7f8b4b0f060e` |
| Windows x64 | 110,149,749 | `98a55d46f4aea5e1601e78fb3caf8adef10acf77137adf07c8a396baac1b1af1` |

## E2E 검증 범위

모든 브라우저 시험은 별도 프로필의 headless Chrome 153.0.8010.54에서 수행했다. 사용자 Chrome에는 연결하지 않았다.

1. 합성 15초 MP4: 현재 링크 버튼에서 실제 로컬 도우미 자동 pairing → 임시 API 작업 생성 → 도우미 직접 업로드 → 실제 validator → 브라우저 재생 성공.
2. 사용자 제공 YouTube `https://youtu.be/GcOe4ILS6Ow?si=MwRd2upBrYaxK7lN`: 실제 Mac 다운로드 1,194,527 bytes, 17.024초, H264/AAC → 같은 직접 업로드·검증·재생 성공. 원본 SHA-256 `91c2b53da80f29fdec07d45858716d1a26122d1431ae92178cdcc6fbff64bba3`.
3. 두 경로 모두 브라우저 파일 전송 0, 클라우드 원본 다운로드 요청 0, JS 오류 0. 로그인은 합성 OIDC, API·DB·저장소는 격리 테스트 환경이다. 운영 Google 인증·R2 업로드 또는 패키지 앱의 운영 설치 E2E로 보고하지 않는다.
4. 실제 운영 웹 정적 자산과 합성 API/도우미 응답으로 로그인 후 전체 화면, 링크 전용 팝업, 도움말, 취소·늦은 연결·재시도·단일 작업 재개 및 모바일 배치를 확인했다.
5. 현재 브랜치의 `scripts/helper-onboarding-headless.mjs`: 세 플랫폼 선택, 개발용 경고, 동의 전 다운로드 없음, fixture 다운로드, 설치 후 연결 시뮬레이션, 미지원·미공개 상태, 취소·재로그인·새로고침 시 중복 가져오기 방지 통과. OS 설치는 시뮬레이션이다.
6. 별도 headless 브라우저의 실제 팝업에서 공개 manifest를 사용해 동의 버튼 → 실제 GitHub Apple Silicon ZIP 다운로드 → 46,526,615 bytes 및 위 SHA-256 일치 확인. PC 정보와 도우미 미실행 상태만 fixture이며 다운로드 파일은 실제 공개 파일이다. 내려받은 실행 파일을 자동 설치하지 않았다.

웹 테스트 211개, TypeScript·lint·상용 빌드 통과 후 최종 테스트 변경의 관련 10개 테스트도 통과했다. 번들 크기 500 kB 초과 경고는 남아 있다.

로컬 실행 증적: `/private/tmp/replay-helper-release-e2e.json`, `/private/tmp/replay-helper-youtube-e2e.json`. 임시 파일은 영구 보관을 보장하지 않는다.

## 반영 단계

설치 파일은 공개했다. 웹 다운로드 manifest와 개발용 안내는 이 변경의 PR 승인·머지 및 웹 배포가 필요하다. 공개 파일 존재와 운영 웹 반영을 구분한다. 현재 패키지는 기존 운영 Vercel API를 사용하며 Cloudflare 전체 이관을 완료한 것으로 표시하지 않는다.
