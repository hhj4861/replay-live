# 사용자 PC 도우미 설치·자동 연결

웹 로그인 후 운영자 권한이 확인되면 보관함/링크 탭과 관계없이 도우미를 확인한다. 살아 있는 도우미의 일회 실행 코드를 loopback에서 읽고 자동 pair한다. 인증에 쓰이지 않는 탭 구분자만 sessionStorage에 보관해 새로고침 때 이전 로컬 세션을 대체한다. 코드·로컬 토큰·cloud bearer를 디스크에 저장하지 않으며 cloud bearer는 도우미에 전달하지 않는다. 로컬 세션은 웹 세션 identity에 묶이고 로그아웃·계정 전환·중단된 pair 응답은 폐기/회수한다. 사용자가 연결 해제를 누르면 같은 화면에서는 focus 이벤트로 다시 pair하지 않는다.

도우미 실행은 `replay-live-helper://start`로만 가능하다. URL에 서버 주소, 파일 경로, 토큰 또는 명령을 넣을 수 없다. 최초 설치/자동 시작은 각각 네이티브 확인 창에서 동의해야 한다. macOS 사용자 LaunchAgent와 Windows HKCU Run을 사용하며 시스템 데몬/관리자 권한은 사용하지 않는다. 사용자가 OS에서 시작 권한을 끈 경우 이를 되돌리지 않는다.

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
