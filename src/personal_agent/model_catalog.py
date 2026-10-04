import json
import platform
import shutil
import subprocess
from typing import Any, Dict, Optional
from uuid import uuid4

CODEX_RPC_TIMEOUT_SECONDS = 30


def _call_codex(
    method: str,
    params: Dict[str, Any],
    executable: str = "codex",
    environment: Optional[Dict[str, str]] = None,
    timeout: int = CODEX_RPC_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    resolved = shutil.which(executable) or executable
    command = [resolved, "app-server", "--listen", "stdio://"]
    if platform.system() == "Windows" and resolved.lower().endswith((".cmd", ".bat")):
        # npm installs Codex as a CMD shim; CreateProcess cannot launch it
        # directly when shell=False.
        command = ["cmd.exe", "/d", "/c", resolved, "app-server", "--listen", "stdio://"]
    process = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=environment,
    )
    requests = [
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "personal-agent", "title": "Personal Agent", "version": "0.1.0"}, "capabilities": {}}},
        {"method": "initialized", "params": {}},
        {"id": 2, "method": method, "params": params},
    ]
    try:
        stdout, stderr = process.communicate(
            input="\n".join(json.dumps(request) for request in requests) + "\n",
            timeout=timeout,
        )
        for line in stdout.splitlines():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(message, dict):
                continue
            if message.get("id") != 2:
                continue
            if "error" in message:
                raise RuntimeError(f"Codex 응답 오류: {message['error']}")
            result = message.get("result")
            if not isinstance(result, dict):
                raise RuntimeError("Codex 응답 형식이 올바르지 않습니다.")
            return result
        raise RuntimeError(stderr.strip() or "Codex 사용량 응답이 없습니다.")
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            "Codex 사용량 요청 시간이 초과되었습니다. 초기화 요청은 결과를 확인한 뒤 같은 요청으로 다시 시도하세요."
        ) from exc
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()


def fetch_codex_rate_limits(executable: str = "codex", environment: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Read current Codex account rate-limit windows."""
    return _call_codex("account/rateLimits/read", {}, executable, environment)


def consume_codex_reset_credit(
    credit_id: str = "",
    executable: str = "codex",
    environment: Optional[Dict[str, str]] = None,
    idempotency_key: Optional[str] = None,
) -> str:
    """Consume one earned Codex rate-limit reset credit."""
    params = {"idempotencyKey": idempotency_key or str(uuid4())}
    if credit_id:
        params["creditId"] = credit_id
    result = _call_codex("account/rateLimitResetCredit/consume", params, executable, environment)
    outcome = result.get("outcome")
    if not isinstance(outcome, str) or not outcome:
        raise RuntimeError("Codex가 초기화 요청 결과를 반환하지 않았습니다.")
    return outcome
