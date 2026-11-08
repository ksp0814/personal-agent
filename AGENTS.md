# Personal Agent 프로젝트 규칙

## 프로젝트 개요

- Python 기반 개인비서·코딩 에이전트이며, 데스크톱 UI는 React + TypeScript + Tauri를 사용합니다.
- Python은 파일·Git·사용량·터미널을 제공하는 로컬 JSON-line 브리지입니다.
- 중앙 터미널은 xterm.js를 Tauri에 임베드하고, PowerShell을 Windows ConPTY에 연결합니다.
- 사용자는 API 키보다 Codex CLI 로그인 세션을 우선 사용합니다.

## 실행 환경

```bash
source .venv/bin/activate
python -m pip install -e ".[terminal]"
cd frontend
npm install
npm run tauri dev
```

일반 CLI 실행:

```bash
agent
```

Codex 로그인 상태 확인:

```bash
codex login status
```

## 검증

변경 후 최소한 다음 검증을 실행합니다.

```bash
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m unittest discover -s tests -q
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -c "import personal_agent.bridge, personal_agent.terminal_bridge"
git diff --check
```

UI를 변경하면 데스크톱 앱을 직접 실행해 핵심 흐름을 확인합니다.

- 작업 공간 추가·전환
- 작업 공간별 Codex 터미널 분리
- 한글 입력
- 파일 검색·폴더 확장·파일 미리보기
- 사용량 조회·초기화권 확인

## 코드 규칙

- Python 브리지와 React·TypeScript 코드의 기존 스타일을 유지합니다.
- 기능 변경은 가장 가까운 테스트를 함께 수정하거나 추가합니다.
- 사용하지 않는 import·worker·UI 위젯·호환 코드는 제거합니다.
- 파일 시스템 탐색은 작업 공간 루트 밖으로 나가지 않도록 `WorkspaceTools.resolve()`를 사용합니다.
- 사용자 파일을 임의로 삭제하거나 덮어쓰지 않습니다.
- 비밀정보, API 키, 로그인 토큰을 코드·로그·커밋에 넣지 않습니다.
- 모델명이나 사용량을 실제 확인하지 못한 경우 추정해서 표시하지 않습니다.

## 터미널 구조

- `src/personal_agent/terminal.py`: PTY/ConPTY 프로세스와 입출력
- `frontend/src/App.tsx`: 데스크톱 UI와 작업 공간·파일·세션 상태 관리
- `frontend/src/TerminalPane.tsx`: xterm.js 터미널 렌더링과 입력·출력 연결
- `frontend/src-tauri/src/lib.rs`: Tauri 명령 경계와 프로세스 수명 관리
- `src/personal_agent/bridge.py`: 파일·Git·승인 JSON-line 브리지
- `src/personal_agent/terminal_bridge.py`: 터미널 프로세스 JSON-line 브리지
- 작업 공간을 전환해도 기존 터미널 세션을 종료하지 않습니다.
- Codex CLI의 ANSI·IME·커서 처리를 직접 재구현하지 말고 xterm.js에 위임합니다.
- 터미널 세션 종료 시 프로세스와 PTY 핸들을 반드시 정리합니다.

## Git 규칙

- 기본 작업 브랜치는 `dev`입니다.
- 사용자가 요청하지 않으면 commit, push, merge, rebase, PR 생성을 하지 않습니다.
- 커밋 타입 접두사를 사용합니다: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`.
- 커밋 메시지는 반드시 한글로 작성합니다.
- 예시:

```text
feat: 작업 공간별 터미널 세션 추가
fix: 한글 입력 지연 문제 수정
refactor: 사용하지 않는 모델 선택 코드 제거
```

- 커밋 전 테스트와 `git diff --check`를 실행합니다.
- push 전 `git status`와 `git diff --stat`를 확인합니다.
- 강제 push, `git reset --hard`, 대량 삭제는 명시적 승인 없이 실행하지 않습니다.

## 릴리즈 규칙

- `dev`에 푸시하면 `.github/workflows/dev-release.yml`이 최신 커밋에 다음 `vMAJOR.MINOR.PATCH` 태그를 생성합니다. 이미 태그가 붙은 커밋은 중복 생성하지 않습니다.
- `.github/workflows/release.yml`은 버전 태그에서 Python·프론트엔드 테스트와 Windows 빌드를 실행합니다. 서명된 설치 파일과 업데이트 파일을 확인한 뒤 릴리즈를 즉시 공개합니다.
- 공개된 최신 릴리즈는 설치된 앱의 업데이트 확인 대상입니다. 사용자가 앱에서 업데이트를 선택하면 설치합니다.
- 자동 태그 생성에는 저장소 쓰기 권한이 있는 `RELEASE_TOKEN` 비밀정보가 필요합니다. 값은 코드·로그·문서에 기록하지 않습니다.

## 문서

- 실행 방법과 사용자-visible 기능이 바뀌면 `README.md`도 함께 갱신합니다.
- 프로젝트 전용 규칙은 이 파일에 유지하고, 전역 공통 규칙은 전역 AGENTS.md에 둡니다.
