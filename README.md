# Replay Live POC

완성된 녹화 MP4를 예약 시간에 실시간 속도로 재생하여 YouTube Live에 송출하는 독립 프로젝트입니다. 기존 SaaS에는 `/api/media`와 `/api/broadcasts`를 연결하는 방식으로 통합할 수 있습니다.

## 구현 범위

- MP4 업로드·FFprobe 검사·브라우저 소스 미리보기
- 로컬 테스트 송출 / YouTube RTMPS 송출
- 즉시 실행·예약 실행·단일 송출 대기열
- 진행률, 수동 중지, 예약 취소, 방송별 이력과 이벤트
- FFmpeg 비정상 종료 시 최대 2회 재시도(총 3회, 5초/10초 대기)
- SQLite 영속화, 스트림 키 Fernet 암호화, 응답·애플리케이션 로그의 키 비노출
- 서버 재시작 시 예약 보존, 중단 작업은 실패로 정리
- 로컬 파일 송출과 실제 RTMP loopback 수신 테스트

대화의 최종 기능 논의인 **완성 녹화본 → YouTube 재송출**을 기준으로 합니다. 진행 중인 라이브의 30분 지연 DVR, 다중 플랫폼, OAuth, 반복·플레이리스트, 기존 SaaS 인증, AWS 배포는 이번 POC 범위에 포함하지 않았습니다.

## 실행

필요 환경: macOS 또는 Linux, Python 3.11 이상, Node 22.13 이상(권장 24), FFmpeg/ffprobe와 libx264/AAC 인코더.

```bash
cd /Users/admin/workSpace/replay-live-poc
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
cd web
npm ci
cd ..
.venv/bin/python scripts/sample.py
./scripts/dev.sh
```

- 관리 화면: http://127.0.0.1:3000
- API 문서: http://127.0.0.1:8080/docs
- 종료: 실행한 터미널에서 Ctrl+C
- 스크립트는 이 Mac의 Homebrew Node 24가 있으면 이를 사용합니다. 다른 환경은 PATH의 Node를 사용합니다.
- 서버는 `--workers 1`로만 실행합니다. 동일 데이터 폴더의 다중 인스턴스는 파일 잠금으로 차단합니다.
- 기본 저장 위치는 `data/`. `REPLAY_DATA=/절대/경로`로 변경할 수 있습니다.
- 예약 시간은 UI에서 브라우저 현지 시간으로 입력하고 API에는 시간대가 포함된 ISO 8601로 전달합니다.

## 바로 테스트하기

1. `영상 업로드`에서 `data/sample.mp4`를 선택합니다. 저작권 없는 FFmpeg 테스트 패턴과 440Hz 신호로 만든 15초 영상입니다.
2. 방송 이름을 입력하고 송출 대상 `로컬 테스트`를 선택합니다.
3. `테스트 송출 시작`을 누르면 실제 FFmpeg가 실행됩니다.
4. 완료 이력의 `결과`에서 FLV 파일을 내려받습니다. VLC 또는 FFprobe로 확인할 수 있습니다.
5. 예약 시간을 몇 분 뒤로 설정하여 예약 실행을 확인하거나, 송출 중 `중지`를 누릅니다.

현재 작업 PC에는 검증용 영상과 완료 이력을 넣어두었습니다. 예약은 서버가 실행 중이고 PC가 잠들지 않은 상태에서 동작합니다. 종료 중 놓친 예약은 다음 시작 시 대기열 순서대로 실행됩니다.

## YouTube 연결

1. YouTube Studio에서 라이브 스트리밍을 활성화하고 스트림을 준비합니다.
2. 제목·공개 범위·자동 시작/자동 종료는 Studio에서 설정합니다. 이 POC의 방송 이름은 내부 관리용입니다.
3. 관리 화면에서 `YouTube Live`를 선택하고 본인의 스트림 키를 입력합니다.
4. 송출 확인 체크 후 시작 또는 예약합니다. 키를 채팅이나 Git에 넣을 필요가 없습니다.
5. Studio에서 입력 상태와 실제 공개 여부를 확인합니다. UI의 `송출 중`은 FFmpeg의 데이터 전송 진행을 뜻하며 시청자에게 공개되었다는 의미가 아닙니다.

RTMPS 대상은 YouTube 공식 ingest 호스트로 고정했습니다. 사용자가 임의 URL을 넣는 기능은 없습니다. TLS 인증서 검증을 활성화했습니다. 실제 계정·스트림 키를 사용한 YouTube 송출 검증은 **수행하지 않았습니다**.

## 구조와 API

```text
React / Vinext 관리 화면 (3000)
        │ /api 프록시
FastAPI (8080) ─── SQLite (방송, 영상, 이벤트)
        │               + 암호화 스트림 키
단일 스케줄러 → FFmpeg → 로컬 FLV / YouTube RTMPS
```

| 경로 | 용도 |
|---|---|
| `GET /api/health` | FFmpeg 설치 상태 |
| `GET /api/media` | 영상 목록 |
| `POST /api/media` | multipart `file` 업로드 |
| `GET /api/media/{id}/preview` | MP4 미리보기, Range 지원 |
| `GET /api/broadcasts` | 최근 방송 100개 |
| `POST /api/broadcasts` | 방송 생성 |
| `GET /api/broadcasts/{id}` | 상태와 진행 시간 |
| `POST /api/broadcasts/{id}/stop` | 예약 취소 / 송출 중지 |
| `GET /api/broadcasts/{id}/events` | 최근 이벤트 100개 |
| `GET /api/broadcasts/{id}/output` | 완료된 로컬 송출 FLV |

변경 요청에는 `X-Replay-Client: 1` 헤더가 필요합니다. API는 외부 CORS를 허용하지 않습니다.

```json
{
  "media_id": "업로드 응답의 id",
  "title": "신제품 다시보기",
  "target": "local",
  "scheduled_at": "2026-09-10T20:00:00+09:00"
}
```

`scheduled_at` 생략 시 즉시 대기열에 등록합니다. `target: "youtube"`이면 `stream_key`를 함께 전달합니다. 키는 조회 API에서 반환하지 않습니다. 상태는 `scheduled → starting → streaming → completed`이며 중지·재시도·실패 분기가 있습니다.

## 검증 명령

```bash
.venv/bin/python -m pytest -q
.venv/bin/python scripts/smoke.py       # dev.sh 실행 필요, 새 테스트 이력 생성
.venv/bin/python scripts/rtmp_smoke.py  # 로컬 RTMP 수신기를 생성하여 실제 전송 검증
cd web
npm run build
npm exec tsc -- --noEmit
npm run lint
```

`docs/smoke-result.json`과 `docs/rtmp-result.json`에 실제 통합 검증 결과를 기록했습니다. HTTP 검증은 관리 화면 프록시 → API → DB → FFmpeg → 결과 다운로드 경로입니다. 브라우저 클릭 기반 E2E와 시각 검사는 수행하지 않았습니다. WebMCP가 지원되는 브라우저에는 읽기 전용 상태 조회 도구를 등록하며, 이번 환경에서는 WebMCP 호출을 검증하지 않았습니다.

`npm run lint`는 직접 작성한 app/lib/config를 검사합니다. `lint:all`은 생성된 미사용 shadcn 컴포넌트의 접근성/타입 규칙 불일치도 보고하므로 현재 실패합니다. 해당 원본 컴포넌트는 수정하지 않았습니다. 업로드 미리보기의 자막 자동 생성은 이번 POC에서 제공하지 않습니다.

## POC의 한계와 운영 전 확장점

- 500MB, 1초~4시간, H.264(yuv420p)+AAC, 가로 영상 최대 1920×1080/60fps로 입력을 제한합니다. 송출은 30fps, H.264 6Mbps/AAC 128kbps, 2초 키프레임으로 다시 인코딩합니다. 실제 운영에서는 해상도별 비트레이트 프리셋을 적용하세요.
- 재시도는 마지막 FFmpeg 진행 위치부터 시작합니다. 네트워크 수신 확인 기반이 아니므로 경계에 반복·누락이 생길 수 있습니다. 로컬 재시도는 시도별 FLV이며 결과 다운로드는 마지막 시도 파일입니다.
- 인증 없는 단일 사용자 로컬 서버입니다. 외부에 공개하려면 SaaS 인증/테넌트 분리/업로드 제한/요청 제한이 필요합니다.
- 키 암호화 키는 `data/secret.key`에 0600 권한으로 저장합니다. DB와 키 파일을 모두 가진 사용자는 복호화할 수 있습니다. 운영은 KMS/Secrets Manager로 분리하세요.
- **FFmpeg 출력 URL의 스트림 키는 같은 OS 사용자의 프로세스 인자 조회에서 보일 수 있습니다.** 앱 응답·이벤트·stderr에는 남기지 않지만 프로세스 인자 은닉까지 구현한 것은 아닙니다. 운영에서는 격리된 worker와 프로세스 접근 제한이 필요합니다.
- 정상 종료는 worker를 중지하지만 강제 SIGKILL/시스템 장애에서는 FFmpeg가 고아 프로세스로 남을 수 있습니다. 재시작 전 이전 송출 프로세스를 확인해야 합니다. 운영에서는 컨테이너 수명주기·작업 lease·고아 worker 회수를 구현하세요.
- 파일·이력 자동 정리는 없으므로 테스트 결과가 디스크에 쌓입니다. 사용자 영상과 키 파일은 `.gitignore`로 제외했습니다.
- Sites 스캐폴드의 UI를 사용했지만 FFmpeg와 Python 프로세스는 Sites의 Cloudflare Workers 런타임에서 실행할 수 없습니다. 따라서 전체 POC를 로컬 실행 형태로 제공합니다. AWS 단계에서는 S3 + PostgreSQL + SQS/ECS worker로 교체하고 UI와 API를 동일 인증 경계 뒤에 배치해야 합니다. 현재 빌드의 `/api` 프록시는 개발 서버 전용이므로 UI만 별도 배포하면 송출 API에 연결되지 않습니다.

## 참고

- [요구사항 원문 대화](https://chatgpt.com/c/6a9fddd7-fb58-83ee-a410-3708d9ecf720)
- [FFmpeg 실행 옵션](https://ffmpeg.org/ffmpeg.html)
- [FFmpeg RTMP/RTMPS 프로토콜](https://ffmpeg.org/ffmpeg-protocols.html)
- [YouTube 인코더 설정](https://support.google.com/youtube/answer/2853702?hl=en)
