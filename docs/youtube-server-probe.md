# YouTube 자체 서버 다운로드 검증 — 2026-09-26

## 판정

**주거용 프록시를 접속 경로로 사용한 Replay 클라우드 서버 직접 다운로드에 성공했다.
지정 영상 17.001초 전체를 H.264/AAC MP4로 변환하고 끝까지 디코딩했다.
다른 임시 서버의 두 번째 실행에서도 다운로드와 headless 브라우저 전체 재생에 성공했다.
운영 웹사이트의 링크 가져오기에는 아직 연결하지 않았다.**

- 대상: `https://www.youtube.com/watch?v=GcOe4ILS6Ow` (사용자가 지정한 영상).
- 실행 위치: Replay Vercel 프로젝트의 임시 Sandbox, 기존 Linux 워커 스냅숏 기반.
- 다운로드·변환 코드는 Replay Vercel Sandbox에서 실행했다. DataImpulse는 네트워크 경로만 제공했다.
- 로컬 PC 다운로드, 로컬 도우미, Apify API, 사용자 YouTube 쿠키는 사용하지 않았다.
- Google 로그인, 운영 DB·R2·웹/API 설정은 변경하지 않았다.
- 초기 다운로드 검증 서버 2개와 후속 검증 서버 3개, 플러그인 import 진단 서버 1개,
  성공한 프록시 검증 서버 2개는 모두 `stop()` 성공을 확인했다. 공개 포트와 영구 스냅숏을 생성하지 않았다.

## 주거용 프록시 성공 결과

[실제 실행 보고서](youtube-server-probe-proxy-2026-09-27.json), 실행 시각은 보고서의 UTC 값을 기준으로 한다.

- 서버: `turquoise-inadequate-hippopotamus-C1oRCo`, 미국 동부 `iad1`, 종료 확인.
- 표준 yt-dlp `2026.8.19` + Node/EJS, 같은 세션 식별자로 접속 경로 유지.
- YouTube 플레이어 `OK`, PO Token 공급 호출 0회.
- 다운로드·변환·전체 디코딩: **8.824초**.
- 원본: AV1/Opus, 360×640, 17.001초.
- 결과: **955,763바이트**, H.264/AAC, 360×640, 17.001초.
- 프로그램에서 계측한 CONNECT/양방향 터널 합계: **557,446바이트**.
- DataImpulse 대시보드 첫 실행 누계: **544.3KB**, 3건, 추가 트래픽 0.
  대시보드 단위 표기를 그대로 기록했으며 `Total Money: $0`은 표시 반올림이므로 무료 실행으로 해석하지 않는다.

[두 번째 실행 및 실제 브라우저 재생 보고서](youtube-server-probe-proxy-playback-2026-09-27.json):

- 서버: `amber-smart-turkey-1VEM2y`, `iad1`, 종료 확인.
- 다운로드·변환·전체 디코딩·실시간 headless 재생: **27.664초**.
- Chromium의 `ended=true`, 재생 위치와 전체 길이 모두 **17.001초**, 디코딩 **509프레임**, 오류 없음.
- 출력 크기와 SHA-256이 첫 실행과 동일하다.
- 두 번째 프록시 계측량 **558,912바이트**, 두 번 합계 **1,116,358바이트**.
- 두 번째 실행 후 DataImpulse 대시보드 누계 **1.06MB**, **6건**, 추가 트래픽 **0**.
  승인 상한 250MB 이내이며 추가 구매는 하지 않았다.
- Playwright 브라우저 설치는 프록시를 거치지 않았다. 영상은 두 번 모두 클라우드에서만 받았다.

이 결과는 같은 지정 영상에 대해 주거용 접속 경로에서 자체 서버 다운로드가 가능함을
입증한다. 여러 영상·국가·동시 사용자에 대한 성공률, 장기 안정성 또는 Apify 내부 구현은
입증하지 않는다. 기존 데이터센터 경로 비교와 서버·시각도 달라졌으므로 IP 단독 원인이라는
통제 실험 결론은 내리지 않는다.

## 실제 비교 결과

| 구성 | 결과 | 소요 시간 |
| --- | --- | --- |
| 기존 Replay 다운로드 코드 | 봇 확인 요구 | 1.296초 |
| 표준 yt-dlp + Node JavaScript/EJS 구성 | 봇 확인 요구 | 1.186초 |
| mweb + bgutil PO Token 공급 플러그인, 자동 토큰 요청 | 봇 확인 요구 | 2.180초 |
| mweb + 플레이어 요청 전에도 토큰을 요청하도록 `fetch_pot=always` | 토큰 반환 후에도 봇 확인 요구 | 6.536초 |
| 클라우드 headless Chromium의 새 익명 세션 + yt-dlp | 브라우저·추출기 모두 로그인/봇 확인 요구, 재생 0초 | 11.788초 |
| curl_cffi Chrome impersonation | 봇 확인 요구 | 1.076초 |
| 서울(icn1) 새 Linux 이미지 + 표준 yt-dlp | 봇 확인 요구 | 1.538초 |

첫 세 구성은 동일한 임시 서버에서 순서대로 실행했다. 마지막 구성은 추가 관측을
넣은 두 번째 임시 서버에서 한 번 실행했다. 따라서 마지막 구성까지 같은 IP에서
통제한 비교라고 주장하지 않는다. 추출기 버전은 모두 `2026.8.19`이다.
bgutil은 `2.0.0`, 서버 소스 커밋은 `37169ee2656e08c5c2e5dc9df4c598c0cb4c88a8`이다.

마지막 실행에서는 로그 문구 대신 실제 메서드 호출과 반환을 계측했다.

- 토큰 공급 메서드 호출: 2회, 비어 있지 않은 토큰 반환: 1회.
- YouTube 플레이어 응답: `LOGIN_REQUIRED`.
- 최종 추출기 오류: `BOT_CHECK_REQUIRED`.
- JavaScript challenge 처리 요청: 0회.
- 다운로드 진행 콜백에서 관찰한 미디어 바이트: 0.
- 원본 MP4 생성·변환·보관함 저장: 미도달.

이는 **토큰 공급 기능을 추가하는 것만으로 이 실행 환경의 거절이 해결되지 않았다**는
근거다. 반환된 토큰이 YouTube에서 유효하다고 인정됐다는 증거는 아니다.
IP·세션·클라이언트·토큰 특성 중 어떤 요인이 결정적인지는 아직 분리하지 못했다.
이전에 Apify에서 같은 영상 다운로드가 성공한 사실만으로 Apify 내부 구현이나
프록시 사용 여부를 추론해 확정하지 않는다.

## 증거와 한계

- [첫 시도](youtube-server-probe-2026-09-26.json): 서버 제어 인증 거절로 VM 생성 전에 중단.
  기존 Vercel CLI의 `whoami`가 정상 인증 갱신을 수행한 뒤 스냅숏 조회가 복구됐다.
- [동일 서버의 세 구성 비교](youtube-server-probe-2026-09-26-run2.json).
- [실제 토큰 반환·플레이어 응답 계측](youtube-server-probe-2026-09-26-run3.json).
- [클라우드 브라우저·impersonation 비교](youtube-server-probe-browser-2026-09-26.json).
- [서울 새 VM 비교](youtube-server-probe-seoul-2026-09-26.json). 이미지와 리전이 함께 달라졌으므로 IP 하나만의 통제 실험은 아니다.
- [WPC 플러그인 import 검사](youtube-server-probe-wpc-2026-09-26.json).

브라우저 비교 파일의 WPC 항목은 공급자가 로드되지 않아 토큰 요청이 0회였다.
이는 WPC를 사용한 정상적인 비교 결과가 아니다. 명시적 import 검사를 추가한 다음
실행은 `PROVIDER_IMPORT_FAILED`로 중단됐다. 별도 진단 VM
`coffee-irrelevant-scorpion-J67f8m`에서 설치된 nodriver의 `cdp/network.py`를
읽을 때 UTF-8이 아닌 바이트에 대한 SyntaxError가 발생함을 확인했다.
패키지 자체 결함인지 배포·설치 과정의 변형인지는 분리하지 못했다.
이 진단에서는 YouTube에 요청하지 않았고 VM 종료를 확인했다.

첫 비교의 `ejs_observed`는 로그 키워드 관찰이며 실제 solver 실행의 증거가 아니다.
또한 `messages`의 `TIMEOUT`은 설정 로그의 `socket_timeout`을 넓게 매칭한
진단 코드 결함이다. 시간 초과가 발생했다는 판정에 사용하지 않는다.
최종 검증 코드에서는 이 오탐을 수정하고 회귀 검사를 추가했다.
최종 실행의 `events.js_requests=0`이 실제 challenge 호출 여부에 대한 근거다.
초기 비교의 프로세스 종료 코드 0은 검증 절차 종료만 의미했다. 최종 실행기는
클라우드 다운로드 실패 시 2, 제어·설치·정리 실패 시 1을 반환한다.

## 실행 도구

`scripts/youtube-server-probe.mjs`가 비밀정보가 없는 워커 스냅숏으로 VM을 만든다.
Vercel CLI 자격증명은 제어 프로세스에서만 읽고 VM에 전달하지 않는다.
Python 검증기는 클라우드 안에서 실행되며 성공 시 MP4 변환과 전체 디코딩도 확인한다.
영상·토큰·서명된 미디어 URL을 로컬 증거 파일로 가져오지 않는다.

```sh
node scripts/youtube-server-probe.mjs --run \
  --video-id GcOe4ILS6Ow \
  --snapshot snap_YOUR_VERIFIED_SECRET_FREE_WORKER \
  --output /path/to/new-report.json
```

작업 worktree와 로그인된 제어 checkout이 다르면 `REPLAY_PROBE_CONTROL_ROOT`로
기존 checkout을 지정한다. 해당 checkout의 Sandbox SDK와 `.vercel/project.json`을 사용한다.
`--pot-always-only`는 마지막 토큰 구성을 단독 실행한다. 증거 파일은 기존 파일을 덮어쓰지 않는다.
`--browser-candidates`는 새 클라우드 브라우저와 impersonation, WPC를 순차 검사한다.
`--wpc-only`는 WPC import 성공을 선행 조건으로 검사한다.
`--standard-only --fresh --region icn1`은 서울 새 VM에서 표준 엔진을 실행한다.

### 승인된 유료 프록시 비교

사용자가 DataImpulse 개인 계정 가입, 5GB/$5 한 번 구매, 자동 충전 끄기,
테스트 사용량 최대 250MB를 승인했다. 가입·사용자 결제 후 지급된 5GB를 화면에서 확인했다.
사용자가 자동 충전을 선택하지 않았다고 명시적으로 확인했다. 반복 결제 설정은 변경하지 않았다.
공급자 사용량 제한 입력란은 `0.2`를 `2`로 바꾸어 250MB 이하 제한을 설정하지 못했다.
해당 설정은 저장하지 않았으며, 코드의 100MB 상한과 실행 사이 실제 사용량 대조를 사용했다.

`--proxy-only`는 `REPLAY_PROBE_PROXY_URL`을 제어 프로세스 환경으로 받고
일회성 VM 명령 환경에만 전달한다. `http://<user>:<password>@gw.dataimpulse.com:823`
형식만 허용하며 실제 비밀값을 보고서·소스·파일·명령행 인자로 저장하지 않는다.
YouTube 통신의 TLS는 영상 플랫폼까지 유지된다.
일회성 루프백 비밀번호 입력 폼에서 제어 프로세스 메모리로 값을 전달하고 입력 서버와
탭을 닫았다. 폼은 임의 명령 실행을 지원하지 않으며 승인된 고정 테스트만 실행했다.
`--verify-playback`은 클라우드에 Playwright/Chromium을 설치하여 다운로드한 MP4의
실제 `ended`, 재생 시간, 디코딩 프레임 수를 확인한다. 로컬 브라우저에서 영상을 받지 않는다.

`probe_proxy_budget.py`는 VM 내부 루프백 CONNECT 릴레이로 양방향 터널 바이트와
CONNECT 헤더를 합산한다. 실행당 100,000,000바이트에서 연결을 차단하고,
동시 연결은 4개, 목적지는 YouTube/Googlevideo/Ytimg의 443 포트로 제한한다.
계측값에는 TCP/IP 오버헤드와 공급자 별도 집계 차이가 포함되지 않는다.
따라서 첫 실행 후 공급자 대시보드 사용량을 확인해야 하며,
미확인 상태로 재시도하거나 250MB 잔여 승인량보다 큰 실행을 시작하지 않는다.
파일 50MiB 제한만으로 네트워크 사용량이 제한된다고 간주하지 않는다.

제한은 VM 12분, 개별 검증 240초, 공개 공인 IPv4 목적지, 단일 영상 ID,
길이 120초, 파일 50MiB, 작업 임시 디스크 합계 150MiB이다. 비공개·라이브 영상은 거절한다.
이 도구는 관리자가 지정한 공개 영상의 진단용이며, 사용자 입력을 받는 공개 API가 아니다.
본인 채널 소유 확인, 사용자별 큐·과금 제한, R2 보관함 연결은 이번 검증 범위에 포함하지 않았다.

## 검증 및 다음 판단

오프라인 검사 7개가 통과했다. 영상 ID 제한, 길이·공개 범위 제한, 로그 비밀정보 배제와
시간 초과 오탐 회귀, 합성 영상의 불완전 다운로드 거절·H.264/AAC 변환·전체 디코딩,
프록시 설정 제한, CONNECT 목적지 제한, 루프백 양방향 전송량 계측·한도 차단·종료를 다룬다.
합성 영상 검사는 실제 YouTube 다운로드 성공의 대체 증거가 아니다.

서버 다운로드 가능성은 확인했다. 운영 연결은 별도 단계이며 본인 채널 소유 확인,
기존 서버 다운로드 경로의 네트워크 안전장치 유지, 작업 큐와 R2 보관함 연결,
사용자별 비용·동시 실행 제한 및 오류 안내가 필요하다. 이번 $5 테스트 승인을
지속적인 운영 과금이나 자동 충전 승인으로 취급하지 않는다.

공식 자료:
- https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
- https://github.com/yt-dlp/yt-dlp/wiki/EJS
- https://github.com/Brainicism/bgutil-ytdlp-pot-provider
