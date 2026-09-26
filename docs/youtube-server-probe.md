# YouTube 자체 서버 다운로드 검증 — 2026-09-26

## 판정

**Replay의 클라우드 서버에서 직접 다운로드하는 후보를 실제 실행했지만,
지정 영상의 다운로드는 아직 실패한다. 운영 기능 구현·반영 완료가 아니다.**

- 대상: `https://www.youtube.com/watch?v=GcOe4ILS6Ow` (사용자가 지정한 영상).
- 실행 위치: Replay Vercel 프로젝트의 임시 Sandbox, 기존 Linux 워커 스냅숏 기반.
- 로컬 PC 다운로드, 로컬 도우미, Apify API, 프록시, 사용자 YouTube 쿠키는 사용하지 않았다.
- Google 로그인, 운영 DB·R2·웹/API 설정은 변경하지 않았다.
- 생성에 성공한 임시 서버 2개는 모두 `stop()` 성공을 확인했다. 공개 포트와 영구 스냅숏을 생성하지 않았다.

## 실제 비교 결과

| 구성 | 결과 | 소요 시간 |
| --- | --- | --- |
| 기존 Replay 다운로드 코드 | 봇 확인 요구 | 1.296초 |
| 표준 yt-dlp + Node JavaScript/EJS 구성 | 봇 확인 요구 | 1.186초 |
| mweb + bgutil PO Token 공급 플러그인, 자동 토큰 요청 | 봇 확인 요구 | 2.180초 |
| mweb + 플레이어 요청 전에도 토큰을 요청하도록 `fetch_pot=always` | 토큰 반환 후에도 봇 확인 요구 | 6.536초 |

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

제한은 VM 12분, 개별 검증 240초, 공개 공인 IPv4 목적지, 단일 영상 ID,
길이 120초, 파일 50MiB, 작업 임시 디스크 합계 150MiB이다. 비공개·라이브 영상은 거절한다.
이 도구는 관리자가 지정한 공개 영상의 진단용이며, 사용자 입력을 받는 공개 API가 아니다.
본인 채널 소유 확인, 사용자별 큐·과금 제한, R2 보관함 연결은 이번 검증 범위에 포함하지 않았다.

## 검증 및 다음 판단

오프라인 검사 4개가 통과했다: 영상 ID 제한, 길이·공개 범위 제한, 로그 비밀정보 배제와
시간 초과 오탐 회귀, 합성 영상의 불완전 다운로드 거절·H.264/AAC 변환·전체 디코딩.
합성 영상 검사는 실제 YouTube 다운로드 성공의 대체 증거가 아니다.

현재 후보를 운영에 연결하지 않는다. 다음 유효한 비교는 같은 다운로드 구성을 유지하면서
관리되는 별도 네트워크 경로에서 실행하는 것이다. 새 프록시 계약·유료 인프라 변경은
이번에 하지 않았으며, 서비스·비용 범위가 정해진 뒤 별도로 검토한다. IP만 바꾸면 반드시
성공한다는 보장은 없다.

공식 자료:
- https://github.com/yt-dlp/yt-dlp/wiki/PO-Token-Guide
- https://github.com/yt-dlp/yt-dlp/wiki/EJS
- https://github.com/Brainicism/bgutil-ytdlp-pot-provider
