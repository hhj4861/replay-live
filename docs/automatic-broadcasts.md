# 자동 송출

이 변경은 기능 브랜치의 로컬 구현이다. 운영 환경변수, DB, 예약 실행기와 배포는 변경하지 않는다. `REPLAY_AUTOMATIONS_ENABLED`의 기본값은 꺼짐이며, 기존 운영 DB에 새 테이블이 없어도 비활성 API 조회는 동작한다.

## 사용자 흐름

1. 내 계정 또는 기존 방송 채널에서 플랫폼 연결 정보를 한 번 저장한다.
2. **상단 자동 송출 메뉴 → 일정 만들기**에서 보관함 영상 또는 내 YouTube 최신 영상을 선택한다.
3. 요일·시간·시간대와 저장한 플랫폼을 선택하고 **자동 송출 시작**을 누른다.

자동 송출은 상단 방송 이력·내 계정과 같은 단계의 전용 화면이다. `#automations` 주소로 바로 열 수 있고 새로고침해도 해당 화면을 유지한다. 수동 방송 입력 폼과 하단 송출 버튼은 이 화면에서 표시하지 않는다.

보관함 영상은 지정한 요일마다 반복한다. YouTube는 예약 시점에 연결한 본인 채널의 최근 업로드 50개 중 공개 녹화 영상을 확인하고 가장 최근 공개 영상을 가져온다. 첫 회차는 현재 최신 영상을 사용한다. 새 영상이 없으면 **새 영상 없음**으로 기록하고 가져오기·송출을 생략한다. 비공개 영상, 예정/진행 중 생방송, 다른 채널의 영상은 제외한다. 영상 제목은 보관함 이름과 방송 제목에 사용한다.

채널을 직접 입력받지 않고 Google이 소유권을 확인한 `channels.list(mine=true)` 결과를 사용한다. Google 앱 로그인과 채널 읽기 권한 동의는 별개다. 영상 파일은 기존 서버 다운로드 경로를 사용하며 로컬 도우미를 요구하지 않는다. Data API 자체는 영상 파일 다운로드 API가 아니다.

## 로컬 실행

Node 24, 설치된 Python 의존성, FFmpeg 및 `web/node_modules`가 필요하다.

```sh
python scripts/commercial-preview.py --automations --api-port 18130 --web-port 13130
```

웹: `http://127.0.0.1:13130/`. 개발용 로그인을 사용하고 샘플 영상이 제공된다. 자동 송출이 켜진 로컬 화면에서 플랫폼 연결 정보는 API의 계정별 암호화 저장소에 저장한다. 기존 브라우저 전용 연결 정보는 자동 이전하지 않으므로 한 번 다시 저장해야 한다. 브라우저를 닫아도 로컬 API·실행기 프로세스가 켜져 있으면 처리한다. 이 미리보기는 임시 DB를 사용하므로 새로고침/재로그인에는 일정이 유지되지만 미리보기 프로세스 종료 시 자료가 정리된다. **실제 외부 플랫폼 송출은 차단**되며 예약이 도래하면 그 결과가 실패로 표시된다.

영구 로컬 DB를 사용하는 경우 기존 상용 로컬 API/worker 설정에 다음을 추가한다.

```sh
# 개발 DB에만 적용. 운영 연결정보를 가져오지 않는다.
python scripts/commercial-migrate.py --development
# API 프로세스 환경: REPLAY_AUTOMATIONS_ENABLED=1
# 별도 터미널: REPLAY_LOCAL_API_URL과 REPLAY_CONTROL_TOKEN을 환경으로 전달
python scripts/automation-tick.py
```

독립 실행기는 루프백 API만 허용하며 15초마다 `/internal/automations/tick`을 호출한다. `--once`는 단일 실행용이다. API만 켜고 tick/worker를 실행하지 않으면 일정은 처리되지 않는다. 운영 큐 연결은 구현됐으며 실제 활성화 절차는 `production-deployment.md`를 따른다.

## 내 YouTube 채널 연결 설정

Google Cloud 프로젝트에서 YouTube Data API v3를 활성화하고 웹 애플리케이션 OAuth 클라이언트를 준비한다. API 프로세스에 `REPLAY_YOUTUBE_CLIENT_ID`, `REPLAY_YOUTUBE_CLIENT_SECRET`을 비밀 환경으로 주입한다. 로컬 redirect URI는 다음과 정확히 일치해야 한다.

```text
http://127.0.0.1:18130/api/youtube-channel/callback
```

최신 영상 조회는 `https://www.googleapis.com/auth/youtube.readonly`를 요청한다. YouTube 자동 방송은 별도로 `https://www.googleapis.com/auth/youtube.force-ssl`에 동의해야 한다. 기존 읽기 전용 연결은 화면의 **YouTube 방송 권한 연결**로 다시 연결한다. 테스트 중에는 동의 화면의 테스트 사용자에 테스트 계정을 등록한다. 공개 서비스에 필요한 OAuth 검증/공개 전환은 별도 운영 작업이다. 비밀 값은 소스, 문서, 브라우저 저장소에 넣지 않는다. OAuth callback의 query에는 일회용 코드가 있으므로 리버스 프록시/접근 로그에 query를 기록하지 않는다.

일회용 state(10분)와 PKCE로 요청 계정에 연결하며 refresh token은 기존 키 제공자로 소유자 문맥을 포함하여 암호화한다. 연결 해제는 저장된 토큰과 미완료 연결 요청을 삭제한다. Google 계정 자체에서 앱 권한을 철회하는 작업은 별개다. 권한이 철회/만료되면 회차에 재연결 오류가 표시된다.

참조: [Google 공식 업로드 목록 조회](https://developers.google.com/youtube/v3/docs/playlistItems/list), [OAuth 서버 흐름](https://developers.google.com/identity/protocols/oauth2/web-server).

## 실행 규칙과 복구

- 일정은 tenant와 로그인 사용자에 종속된다. 같은 tenant의 다른 사용자도 조회·수정할 수 없다. 실행 직전 비활성 계정 여부를 다시 확인한다.
- UTC 실행 시각을 저장하고 IANA 시간대로 다음 회차를 계산한다. 일광절약 전환으로 없는 시각은 건너뛰고 중복 시각은 첫 번째만 실행한다.
- 계정당 최대 20개 일정. 기존 보관함 용량·작업 수·동시 송출 제한을 적용한다. 저장된 플랫폼 키는 매 실행 시 조회하므로 키 변경이 반영된다.
- 하나의 일정은 한 회차만 진행한다. 실행 중 놓친 회차를 몰아서 송출하지 않고 완료 후 다음 미래 시각으로 이동한다. API 중단 동안 밀린 일정은 복구 후 한 회차만 실행한다.
- DB lease와 회차별 idempotency key로 여러 tick 호출/프로세스 재시작 시 가져오기와 방송 배치를 중복 생성하지 않는다. 최신 영상은 방송 배치가 등록되면 소비한 것으로 기록한다. 일부 플랫폼 실패 때 같은 영상 전체를 다시 송출하지 않으며 필요한 재송출은 수동 방송으로 처리한다.
- 가져오기 실패 시 방송을 만들지 않는다. 실패 회차는 결과를 남기고 다음 예약에서 다시 확인한다. 보관함 영상 삭제/기간 만료, 플랫폼 연결 삭제, 용량 부족도 오류로 남긴다.
- **일시중지**는 이후 회차를 중지한다. 이미 진행 중인 회차는 끝까지 관찰하며, 중단은 기존 방송 이력에서 처리한다. 진행 중인 일정은 삭제할 수 없다.
- 새 영상은 보관함에 쌓이며 계정 보관함 한도와 기존 보존 정책이 유지된다. 자동으로 이전 사용자 영상을 삭제하지 않는다.
- YouTube는 로그인한 채널에서 저장된 스트림 키를 찾고 회차별 방송을 생성·바인딩한다. 기본 일부 공개이며 공개 범위와 아동용 여부는 일정 생성 시 선택한다. 자동 시작/중지와 상태 확인을 사용하며 YouTube의 실제 시작 및 종료 확인 전에는 완료 처리하지 않는다. 방송 시청 링크는 회차 이력에 표시한다. 스트림 키만으로는 예약할 수 없다.
- YouTube 방송 생성 응답이 불확실하면 중복 생성을 하지 않고 일정을 멈춘다. Studio에서 해당 회차를 확인한 후 다시 예약한다. 권한/채널 불일치, 시작·종료 확인 시간 초과 시 오류를 남기고 일정을 멈춘다. 다른 플랫폼은 기존 RTMP 전송 완료 조건을 사용한다.
- 추가 테이블은 `migrations/013_youtube_live.sql`에 있다. 로컬 migrate가 생성하며 운영에는 적용하지 않았다.

## 검증

```sh
python -m pytest tests/test_automations.py tests/test_youtube_channel.py tests/test_youtube_live.py -q
# 위의 --automations 로컬 미리보기 실행 후:
REPLAY_PLAYWRIGHT_MODULE=/path/to/playwright \
REPLAY_CHROME_PATH=/path/to/chrome \
node scripts/automation-browser-smoke.mjs
```

Python 테스트는 실제 로컬 파일 업로드/FFmpeg 검사와 DB 예약·작업 등록을 연결한다. Google 응답과 다운로드 완료/외부 방송 완료는 통제된 테스트 대역이며 실제 Google 동의나 공개 송출 성공의 증거가 아니다. Headless 검증은 실제 브라우저·API·DB로 일정 생성, 새로고침, 일시중지/재개/삭제, 모바일 넘침을 검사하며 외부 요청을 차단한다. 예약 시각까지 최대 3분을 기다려 실제 tick → 작업 큐 1건 생성 → 로컬 외부 송출 차단 → 실패 결과 표시도 확인한다. 실제 플랫폼 전송 성공을 검사하는 테스트는 아니다.

### 실제 YouTube 검증

격리 VM API `18092`의 Google OAuth callback은 `http://127.0.0.1:18092/api/youtube-channel/callback`이다. 해당 URI를 웹 OAuth 클라이언트에 등록하고 런타임에 클라이언트 ID/시크릿을 안전하게 주입한다. 웹 `http://127.0.0.1:13102/#automations`에서 저장된 스트림 키와 같은 채널로 방송 관리 권한을 연결한다.

`automation-youtube-live-smoke.mjs`는 명시적 `REPLAY_ALLOW_YOUTUBE_LIVE=1`과 승인된 `REPLAY_TEST_STREAM_KEY_FILE`을 요구한다. 새 일부 공개 방송의 시청 URL을 자동으로 가져와 headless 플레이어의 움직이는 영상, 플랫폼 시작·종료, 송신 완료를 함께 검증한다. 권한 연결이 없으면 외부 방송 생성 전에 실패한다. 종료 시 테스트 일정을 멈추고 이전 스트림 연결을 복구한다. Google 응답을 대역으로 검증한 단위 테스트는 실제 방송 성공을 뜻하지 않는다.

로컬 OAuth 입력은 `python3 scripts/platform-youtube-oauth-setup.py`로 연다. 준비된 전용 Lima VM에서만 동작하며 활성 작업/일정이 있으면 런타임 교체를 거부한다. 일회성 화면은 30분 뒤 닫히고 OAuth 시크릿은 stdin으로 전달되어 VM 프로세스 환경에만 남는다. 서버 재시작 시 재입력이 필요하다. 직접 만든 공개 자동 방송의 아카이브는 최신 원본 영상 후보에서 제외한다.

이번 Mac의 로컬 검증은 사용자가 제공한 기존 Desktop OAuth 클라이언트의 loopback redirect를 사용한다. 운영 웹에는 별도의 Web OAuth 클라이언트와 승인된 HTTPS callback 설정이 필요하다. 로컬 동의 완료를 운영 OAuth 설정 완료로 취급하지 않는다.

### 2026-09-29 실제 플랫폼 확인

- 첫 연결 채널은 YouTube API가 `liveStreamingNotEnabled`를 반환해 방송 생성 전에 중단했다. 이 오류를 일반 권한 오류와 구분하고 Studio 활성화 안내를 표시한다.
- 사용자가 라이브 가능한 계정으로 다시 연결한 후 스트림 키 소유 채널 일치를 확인했다. 예약 회차가 YouTube 방송을 생성·바인딩했고 실제 시작 및 종료(`complete`, `live_confirmed=true`)와 FFmpeg 59.977초 완료를 확인했다.
- 방송 결과: https://www.youtube.com/watch?v=AhBKbIVv-58 . 별도 headless 시청에서 녹화본의 프레임 34→654, 재생 시간 1.15→21.64초 증가를 확인했다.
- 첫 플레이어 검증은 `video.play()`의 무기한 대기, 다음 검증은 방송 전 열린 페이지의 `LIVE_STREAM_OFFLINE` 잔류를 확인했다. 검증기는 재생 대기를 제한하고 플랫폼 시작 확인 후 시청 페이지를 열며 필요하면 새로고침한다. 녹화본 재생 증거와 실시간 재생 증거는 구분한다.
- 운영 배포는 하지 않았다. 당시 전역 DBQ Git 훅으로 커밋이 차단됐으나, 설치된 0.2.0-dev.6의 회사 저장소 한정 정책을 확인한 뒤 정상 훅으로 커밋·작업 브랜치 push를 완료했다. 훅을 우회하거나 이 작업에서 변경하지 않았다.

최종 재검증은 2026-09-29 11:27 KST에 `sender_and_playback_verified`로 통과했다. https://www.youtube.com/watch?v=ei19Qyy3cMs 에서 `is_live=true`와 플랫폼 `live` 상태 중 프레임 106→1220, 재생 시간 7.26→44.44초를 확인했다. 이후 플랫폼 `complete`, 송신 59.977초 완료를 확인하고 테스트 일정을 일시중지하고 기존 연결을 복구했다. 로컬 증적은 `/private/tmp/replay-auto-live-final.json`이며 운영 배포 증거가 아니다.

### 요일 제외 및 운영 큐 검증

2026-09-29 화요일 12:01:50 KST에 화요일을 제외하고 12:02 일정을 headless UI로 저장했다. 당일 12:02:25까지 실행/작업 0건, 다음 실행은 9월 30일 수요일 12:02임을 확인했다. 확인 후 테스트 일정을 일시중지했다. 증적 `/private/tmp/replay-weekday-exclusion.json`.

운영 큐는 현재 API와 버전이 일치할 때만 자동 송출을 진행한다. 기능 비활성 시 스키마 접근을 생략하며, 일정 생성/재개에서 큐를 깨우고 다음 예약·진행 중 회차를 DB에서 복원한다. 일시중지해도 이미 시작한 방송의 종료 확인은 이어간다. 큐 지연·중복 수신·외부 호출 실패는 기존 DB lease와 재시도로 처리한다. 운영 설정 및 배포 완료를 의미하지 않는다.
