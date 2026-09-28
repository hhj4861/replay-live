# 자동 송출 — 로컬 구현

이 변경은 기능 브랜치의 로컬 구현이다. 운영 환경변수, DB, 예약 실행기와 배포는 변경하지 않는다. `REPLAY_AUTOMATIONS_ENABLED`의 기본값은 꺼짐이며, 기존 운영 DB에 새 테이블이 없어도 비활성 API 조회는 동작한다.

## 사용자 흐름

1. 기존 방송 채널에서 플랫폼 연결 정보를 한 번 저장한다.
2. **자동 송출 → 일정 만들기**에서 보관함 영상 또는 내 YouTube 최신 영상을 선택한다.
3. 요일·시간·시간대와 저장한 플랫폼을 선택하고 **자동 송출 시작**을 누른다.

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

독립 실행기는 루프백 API만 허용하며 15초마다 `/internal/automations/tick`을 호출한다. `--once`는 단일 실행용이다. API만 켜고 tick/worker를 실행하지 않으면 일정은 처리되지 않는다. 운영 예약 트리거는 이 작업에서 연결하지 않았다.

## 내 YouTube 채널 연결 설정

Google Cloud 프로젝트에서 YouTube Data API v3를 활성화하고 웹 애플리케이션 OAuth 클라이언트를 준비한다. API 프로세스에 `REPLAY_YOUTUBE_CLIENT_ID`, `REPLAY_YOUTUBE_CLIENT_SECRET`을 비밀 환경으로 주입한다. 로컬 redirect URI는 다음과 정확히 일치해야 한다.

```text
http://127.0.0.1:18130/api/youtube-channel/callback
```

요청 범위는 `https://www.googleapis.com/auth/youtube.readonly` 하나다. 테스트 중에는 동의 화면의 테스트 사용자에 테스트 계정을 등록한다. 공개 서비스에 필요한 OAuth 검증/공개 전환은 별도 운영 작업이다. 비밀 값은 소스, 문서, 브라우저 저장소에 넣지 않는다. OAuth callback의 query에는 일회용 코드가 있으므로 리버스 프록시/접근 로그에 query를 기록하지 않는다.

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
- 플랫폼별 라이브 공개/시작 설정까지 자동으로 만들지는 않는다. 기존 RTMP 송출과 같은 조건이며 전송 완료는 공개 방송/다시보기 저장 보장을 뜻하지 않는다.

## 검증

```sh
python -m pytest tests/test_automations.py tests/test_youtube_channel.py -q
# 위의 --automations 로컬 미리보기 실행 후:
REPLAY_PLAYWRIGHT_MODULE=/path/to/playwright \
REPLAY_CHROME_PATH=/path/to/chrome \
node scripts/automation-browser-smoke.mjs
```

Python 테스트는 실제 로컬 파일 업로드/FFmpeg 검사와 DB 예약·작업 등록을 연결한다. Google 응답과 다운로드 완료/외부 방송 완료는 통제된 테스트 대역이며 실제 Google 동의나 공개 송출 성공의 증거가 아니다. Headless 검증은 실제 브라우저·API·DB로 일정 생성, 새로고침, 일시중지/재개/삭제, 모바일 넘침을 검사하며 외부 요청을 차단한다.
