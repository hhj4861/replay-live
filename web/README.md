# Replay Live 관리 화면

React/Vinext + shadcn UI로 구성한 로컬 방송 관리 화면입니다.

실행과 검증은 상위 [README](../README.md)를 참고하세요. `../scripts/dev.sh`가 API와 UI를 함께 실행합니다. `/api`는 개발 서버에서 `127.0.0.1:8080`으로 프록시됩니다. UI만 독립 배포하면 로컬 송출 엔진에 연결되지 않습니다.

- `npm run dev`: 관리 화면 개발 서버
- `npm run build`: UI 빌드
- `npm exec tsc -- --noEmit`: 타입 검사
- `npm run lint`: 직접 작성한 소스 검사
- `npm run lint:all`: 스캐폴드 원본 컴포넌트까지 검사 (원본 규칙 불일치 존재)
