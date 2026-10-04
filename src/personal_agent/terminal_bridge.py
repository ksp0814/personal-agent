"""Long-lived JSON-lines process that adapts TerminalSession to Tauri."""

from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Optional

from .terminal import TerminalSession


def encode_command(command: str, **payload: Any) -> str:
    return json.dumps({"command": command, **payload}, ensure_ascii=False)


def run(workspace: Path) -> None:
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    session: Optional[TerminalSession] = None
    output_lock = threading.Lock()
    stop_reader = threading.Event()
    session_seen_alive = False

    def emit(event: str, **payload: Any) -> None:
        with output_lock:
            # The bridge output may be attached to a cp949 Windows console.
            # Keep the machine protocol ASCII-safe even when terminal data is Korean.
            print(json.dumps({"event": event, **payload}, ensure_ascii=True), flush=True)

    def read_output() -> None:
        nonlocal session_seen_alive
        while not stop_reader.is_set():
            current = session
            if current is not None:
                try:
                    output = current.read()
                    if output:
                        emit("output", data=output)
                    if current.alive():
                        session_seen_alive = True
                    elif session_seen_alive:
                        emit("exit")
                        return
                except Exception as exc:  # pragma: no cover - process-specific failure
                    emit("error", message=str(exc))
                    return
            time.sleep(0.02)

    reader = threading.Thread(target=read_output, name="terminal-output", daemon=True)
    reader.start()
    try:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                request = json.loads(line)
                if not isinstance(request, dict):
                    emit("error", message="터미널 명령은 JSON 객체여야 합니다.")
                    continue
                command = request.get("command")
                if command == "start":
                    if session is not None:
                        session.stop()
                    session = TerminalSession(workspace)
                    session_seen_alive = False
                    session.start()
                    emit("ready")
                elif command == "write":
                    if session is None:
                        raise RuntimeError("터미널 세션이 시작되지 않았습니다.")
                    session.write(str(request.get("text", "")))
                elif command == "resize":
                    if session is not None:
                        session.resize(int(request.get("columns", 140)), int(request.get("rows", 45)))
                elif command == "stop":
                    if session is not None:
                        session.stop()
                        session = None
                    emit("stopped")
                    return
                else:
                    emit("error", message=f"지원하지 않는 터미널 명령입니다: {command}")
            except (json.JSONDecodeError, TypeError, ValueError, OSError, RuntimeError) as exc:
                emit("error", message=str(exc))
    finally:
        stop_reader.set()
        if session is not None:
            session.stop()
        reader.join(timeout=1)


def main() -> None:
    workspace = Path(sys.argv[1]).expanduser() if len(sys.argv) > 1 else Path.cwd()
    run(workspace)


if __name__ == "__main__":
    main()
