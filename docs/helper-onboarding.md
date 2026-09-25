# 사용자 PC 도우미 설치·자동 연결

로그인하면 기존 영상 선택·플랫폼·송출 설정·방송 이력을 바로 표시한다. 도우미 패널로 화면을 가리지 않으며 파일 업로드·보관함·방송 시작에는 도우미 연결이 필요하지 않다. 로그인·새로고침만으로 loopback 요청, PC 정보 조회 또는 설치 파일 다운로드를 수행하지 않는다.

사용자가 유효한 HTTPS 링크로 **영상 가져오기**를 누르면 실행 중인 도우미를 팝업에서 자동 확인한다. 코드나 PC 종류를 직접 입력하는 필드는 없다. 살아 있는 도우미의 코드를 loopback에서 읽어 자동 pair하며, 연결이 성공하면 팝업을 닫고 처음 요청한 링크를 한 번만 이어서 가져온다. 설치 대기 중에는 클라우드 작업을 생성하지 않는다. 취소/Escape·MP4 파일 업로드 선택·로그아웃·계정 변경·권한 회수는 대기 중인 요청을 폐기한다. 늦게 도착한 pair 응답은 로컬 인증 권한을 회수하고 영상을 가져오지 않는다.

인증에 쓰이지 않는 탭 구분자만 sessionStorage에 보관해 다음 링크 요청 시 이전 로컬 세션을 대체한다. 코드·로컬 토큰·cloud bearer를 디스크에 저장하지 않으며 cloud bearer는 도우미에 전달하지 않는다. 로컬 세션은 웹 세션 identity에 묶인다.

도우미 실행은 `replay-live-helper://start`로만 가능하다. URL에 서버 주소, 파일 경로, 토큰 또는 명령을 넣을 수 없다. 최초 설치/자동 시작은 각각 네이티브 확인 창에서 동의해야 한다. macOS 사용자 LaunchAgent와 Windows HKCU Run을 사용하며 시스템 데몬/관리자 권한은 사용하지 않는다. 사용자가 OS에서 시작 권한을 끈 경우 이를 되돌리지 않는다.

## 링크 다운로드 시 설치 안내

팝업에서만 OS와 CPU architecture/bitness를 로컬에서 확인하며 서버에 전송·저장하지 않는다. Client Hints가 Mac arm64/x64 또는 Windows x64를 명확히 알려 주면 해당 설치 파일을 자동 선택한다. Intel로 표시되는 Mac User-Agent로 CPU를 추측하지 않는다. 정보가 없으면 수동 PC 선택을 요구하지 않고 Chrome/Edge 재시도 또는 MP4 업로드를 안내한다. 모바일/Linux/Windows ARM·32비트에는 잘못된 설치 파일을 제공하지 않는다.

**동의하고 설치 파일 다운로드**를 눌렀을 때만 다운로드를 요청한다. 브라우저 다운로드 완료나 설치 성공으로 표시하지 않는다. 내려받은 파일 열기와 OS 설치·실행 승인은 사용자가 해야 한다. 설치나 앱 실행 후 팝업이 5분 동안 연결을 재확인하고, 웹 복귀 시에도 확인한다. 시간 초과 시 다시 연결 버튼으로 재개할 수 있다. 자동 연결 후 원래 링크를 이어서 가져오므로 다시 제출할 필요가 없다. 설치 파일 미공개·연결 거부·자동 감지 불가 상태는 팝업 안에 표시하고 MP4 업로드로 전환할 수 있다.

현재는 사용자 요청에 따라 서명 없는 개발 패키지만 준비한다. manifest는 null이며 자동 다운로드를 실제 운영에서 활성화하지 않는다. GitHub Actions의 만료되는 인증 필요 artifact 링크를 일반 사용자 자동 설치 주소로 사용하지 않는다.

브라우저 참고: [Client Hints](https://developer.chrome.com/docs/privacy-security/user-agent-client-hints), [download 속성의 한계](https://developer.mozilla.org/en-US/docs/Web/API/HTMLAnchorElement/download).

## 화면 검증: headless 기본

사용자 지시에 따라 화면 검증은 `scripts/helper-onboarding-headless.mjs`로 headless 브라우저에서 수행한다. 사용 중인 Chrome에 연결하거나 프로필을 재사용하지 않는다. 테스트 전용 임의 loopback 포트와 임시 프로필을 사용하며 성공/실패 후 브라우저·테스트 서버를 종료한다.

```sh
# web/node_modules에 Playwright가 있으면 모듈 경로 지정 생략 가능
REPLAY_PLAYWRIGHT_MODULE=/path/to/playwright \
REPLAY_CHROME_EXECUTABLE=/path/to/chrome \
node scripts/helper-onboarding-headless.mjs
```

브라우저 실행 경로를 생략하면 Playwright가 설치한 Chromium을 사용한다. PC 정보와 loopback 도우미 응답, 릴리스 파일은 합성 fixture이며 외부 API 요청은 차단한다. Mac arm64/x64·Windows x64 자동 선택, CPU 미확인 시 다운로드 차단과 수동 입력 없음, 동의 전 다운로드 없음, 동의 후 정확한 파일 다운로드, Escape/취소와 포커스 복귀, 공개 파일 없음/모바일 차단을 검증한다. 실제 CommercialHome에서 도우미 없이 전체 화면·보관함·파일 업로드·송출 준비, 링크 요청 시 팝업, 연결 후 단일 작업 재개, 취소 후 늦은 pair 회수, 새로고침·세션 만료 후 작업 재실행 방지를 검증한다. 내려받는 fixture에는 실행 파일이 없으며 실제 OS 설치 검증으로 보고하지 않는다.

## 패키지 검증과 릴리스

`python -m pip install -r desktop/requirements-build.txt`, 빌드 호스트에 FFmpeg/ffprobe를 설치한 다음 `python desktop/build.py --version 0.2.0`으로 생성한다. Python·yt-dlp·FFmpeg·ffprobe와 네이티브 라이브러리를 포함한다. 다른 OS용 교차 빌드는 하지 않으며 GitHub Actions가 macOS arm64/x64 및 Windows x64에서 각각 빌드·검증한 ZIP과 체크섬을 artifact로 보관한다. CI가 패키지를 공개하거나 실제 PC에 설치하지 않는다.

현재 운영 API는 Vercel이므로 기본 패키지도 이 API를 사용한다. Cloudflare 전체 이관 후 `--cloudflare`로 패키지를 다시 빌드한다. 웹에서 보내는 임의 주소로 도우미의 API 목적지를 변경하지 않는다.

일반 사용자 배포 전 해야 할 일:

1. macOS Developer ID 서명/공증과 Windows 서명, 포함 FFmpeg 배포본의 라이선스·소스 제공을 확인한다. 개발 패키지로 Gatekeeper/SmartScreen 우회를 안내하지 않는다.
2. 검증된 ZIP을 `helper-v0.2.0` GitHub Release에 게시한다. macOS/Windows 새 사용자 계정에서 설치 승인·거부, OS 재로그인, 프로토콜 실행, 제거를 검증한다.
3. `web/public/helper-release.json`을 `{ "version": "0.2.0", "downloads": [{ "label": "macOS Apple Silicon", "url": "...", "sha256": "..." }] }`로 채운다. 각 플랫폼 빌드 JSON의 값을 사용하고 실제 공개 다운로드·체크섬을 확인한다. macOS Intel/Windows x64도 추가한다.
4. 해당 PR 승인 머지 후 웹을 배포한다. 설치 파일이 공개되기 전에는 null manifest를 유지한다. 웹은 아직 준비 중임을 표시하며 존재하지 않는 설치 파일 링크를 제공하지 않는다.

현재 개발용 도우미가 17833 포트를 사용 중이면 먼저 그 도우미를 종료해야 한다. 설치기는 자신의 로컬 관리 키로 확인할 수 없는 프로세스를 종료하지 않는다. 관리 키는 사용자 전용 설정 폴더에만 저장되며 클라우드 키가 아니고 웹에 반환하지 않는다. 자동 시작 해제/제거에는 확인 창이 필요하다. 업로드 중 제거 시 작업 취소와 임시 파일 정리를 기다린다.

이번 변경은 `server/media_runtime.py`를 포함하므로 운영 웹/API 반영 시 기존 릴리스 절차대로 FFmpeg worker snapshot과 REPLAY_VERSION을 함께 갱신해야 한다. 도우미만 업데이트하는 경우 클라우드 snapshot 변경은 필요하지 않다.
