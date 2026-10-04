from dataclasses import dataclass
import json
import shutil
import subprocess
from typing import Optional, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .memory import Memory
from .policy import Policy
from .tools import WorkspaceTools


class ModelProvider(Protocol):
    def respond(self, prompt: str, context: list[str]) -> str: ...


class OfflineProvider:
    def respond(self, prompt: str, context: list[str]) -> str:
        return "오프라인 모드입니다. /help에서 사용할 수 있는 명령을 확인하세요."

    def describe(self) -> str:
        return "현재 오프라인 모드입니다."


class CodexCliProvider:
    """Use the user's existing Codex CLI login instead of an API key."""

    def __init__(self, workspace, model: Optional[str] = None, reasoning_effort: Optional[str] = None, executable: str = "codex", timeout: int = 120):
        self.workspace = workspace
        self.model = model
        self.reasoning_effort = reasoning_effort
        self.executable = executable
        self.timeout = timeout

    def respond(self, prompt: str, context: list[str]) -> str:
        memory = "\n".join(f"- {item}" for item in context) or "(없음)"
        full_prompt = (
            "당신은 개인비서 겸 코딩 에이전트입니다. 답변은 한국어로 간결하고 실행 가능하게 작성하세요.\n"
            f"사용자 장기 기억:\n{memory}\n\n"
            f"사용자 요청:\n{prompt}"
        )
        command = [
            self.executable,
            "exec",
            "--ephemeral",
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--cd",
            str(self.workspace),
        ]
        if self.model:
            command.extend(["--model", self.model])
        if self.reasoning_effort:
            command.extend(["--config", f'model_reasoning_effort="{self.reasoning_effort}"'])
        command.append(full_prompt)
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=self.timeout,
                check=False,
            )
        except FileNotFoundError as exc:
            raise RuntimeError("Codex CLI를 찾을 수 없습니다. codex 설치 후 다시 실행하세요.") from exc
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Codex CLI 요청 시간이 초과되었습니다.") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[-500:]
            raise RuntimeError(f"Codex CLI 오류({result.returncode}): {detail}")
        response = result.stdout.strip()
        if not response:
            raise RuntimeError("Codex CLI 응답이 비어 있습니다.")
        return response

    def describe(self) -> str:
        return f"현재 모델: {self.model or 'Codex 기본 모델'}\nReasoning effort: {self.reasoning_effort or '기본값'}"

    @staticmethod
    def available(executable: str = "codex") -> bool:
        return shutil.which(executable) is not None


class OpenAIProvider:
    """Minimal Responses API client with no third-party dependency."""

    def __init__(self, api_key: str, model: str, base_url: str = "https://api.openai.com/v1", timeout: int = 60):
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def respond(self, prompt: str, context: list[str]) -> str:
        memory = "\n".join(f"- {item}" for item in context) or "(없음)"
        payload = {
            "model": self.model,
            "store": False,
            "input": [
                {"role": "developer", "content": "당신은 개인비서 겸 코딩 에이전트입니다. 답변은 한국어로 간결하고 실행 가능하게 작성하세요."},
                {"role": "developer", "content": f"사용자 장기 기억:\n{memory}"},
                {"role": "user", "content": prompt},
            ],
        }
        request = Request(
            f"{self.base_url}/responses",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout) as response:
                body = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(f"OpenAI API 오류({exc.code}): {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"OpenAI API 네트워크 오류: {exc.reason}") from exc
        except TimeoutError as exc:
            raise RuntimeError("OpenAI API 요청 시간이 초과되었습니다.") from exc
        return self._extract_text(body)

    def describe(self) -> str:
        return f"현재 모델: {self.model}"

    @staticmethod
    def _extract_text(body: dict) -> str:
        if isinstance(body.get("output_text"), str):
            return body["output_text"]
        chunks = []
        for item in body.get("output", []):
            for content in item.get("content", []):
                if content.get("type") == "output_text" and isinstance(content.get("text"), str):
                    chunks.append(content["text"])
        if chunks:
            return "\n".join(chunks)
        raise RuntimeError("OpenAI API 응답에서 텍스트를 찾지 못했습니다.")


@dataclass
class Agent:
    tools: WorkspaceTools
    memory: Memory
    policy: Policy
    model: ModelProvider

    def handle(self, request: str) -> str:
        request = request.strip()
        if not request:
            return "요청을 입력해 주세요."
        if request == "/help":
            return "명령: /ls, /read <파일>, /write <파일> <내용>, /test, /check, /git, /diff, /history, /resume, /remember <내용>, /memory, /quit"
        if request == "/ls":
            files = self.tools.list_files()
            return "\n".join(files) if files else "작업 공간에 파일이 없습니다."
        if request.startswith("/read "):
            try:
                return self.tools.read_file(request[6:].strip())
            except (ValueError, FileNotFoundError, UnicodeDecodeError) as exc:
                return f"파일을 읽지 못했습니다: {exc}"
        if request.startswith("/remember "):
            content = request[10:].strip()
            if not content:
                return "기억할 내용을 입력해 주세요."
            self.memory.remember(content)
            return "기억했습니다."
        if request == "/memory":
            memories = self.memory.recent()
            return "\n".join(f"- {item}" for item in memories) if memories else "저장된 기억이 없습니다."
        if request == "/test":
            return self.tools.run_validation()
        if request == "/check":
            response = self.tools.run_validation()
            self.memory.record_interaction(request, response)
            return response
        if request == "/git":
            return self.tools.git_status()
        if request == "/diff":
            return self.tools.git_diff()
        if request == "/history":
            interactions = self.memory.recent_interactions()
            if not interactions:
                return "저장된 작업 기록이 없습니다."
            return "\n\n".join(f"요청: {old_request}\n응답: {response}" for old_request, response in interactions)
        if request == "/resume":
            interactions = self.memory.recent_interactions(5)
            if not interactions:
                return "재개할 작업 기록이 없습니다."
            return "최근 작업:\n" + "\n".join(f"- {item[0]}" for item in interactions)
        if any(term in request.casefold() for term in ("무슨 모델", "어떤 모델", "현재 모델", "reasoning effort")):
            describe = getattr(self.model, "describe", None)
            if describe:
                return describe()
        if request.startswith("/write "):
            parts = request[7:].split(" ", 1)
            if len(parts) != 2 or not parts[0] or not parts[1]:
                return "사용법: /write <파일> <내용>"
            relative, content = parts
            try:
                self.tools.resolve(relative)
            except ValueError as exc:
                return f"파일을 쓸 수 없습니다: {exc}"
            if not self.policy.authorize(f"파일 수정: {relative}"):
                return "승인되지 않아 파일을 수정하지 않았습니다."
            self.tools.write_file(relative, content)
            return f"{relative} 파일을 수정했습니다."
        try:
            context = self.memory.recent()
            context.extend(f"이전 요청: {old_request}\n이전 응답: {old_response}" for old_request, old_response in self.memory.recent_interactions(5))
            response = self.model.respond(request, context)
            self.memory.record_interaction(request, response)
            return response
        except RuntimeError as exc:
            return f"모델 호출에 실패했습니다: {exc}"
