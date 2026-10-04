"""Small JSON-lines bridge for the React/Tauri desktop client."""

from __future__ import annotations

import json
import argparse
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Iterable, Optional

from .model_catalog import consume_codex_reset_credit, fetch_codex_rate_limits
from .terminal import build_terminal_environment
from .tools import WorkspaceTools


class ApprovalStore:
    """Persist small text baselines and move new files to a recovery area."""

    max_file_size = 1_000_000
    max_total_content_size = 16_000_000
    format_name = "personal-agent-approval-v1"

    def __init__(self, workspace: Path, recovery_root: Optional[Path] = None):
        self.workspace = workspace.resolve()
        self.tools = WorkspaceTools(self.workspace)
        self.path = self.workspace / ".agent" / "react-baseline.json"
        self.recovery_root = recovery_root or (Path.home() / ".personal-agent" / "rollback-trash")
        self.baseline_files: Optional[set[str]] = None
        self.baseline_complete = False
        self.records = self._load()

    def _load(self) -> dict[str, dict[str, object]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict):
                return {}
            if payload.get("_format") == self.format_name:
                raw_records = payload.get("records")
                raw_files = payload.get("baseline_files")
                if isinstance(raw_files, list) and all(isinstance(path, str) for path in raw_files):
                    self.baseline_files = set(raw_files)
                    self.baseline_complete = payload.get("baseline_complete") is True
                records = raw_records if isinstance(raw_records, dict) else {}
                return self._normalize_records(records)
            # Read the pre-versioned format and migrate it on the next save.
            return self._normalize_records(payload)
        except (OSError, ValueError, TypeError):
            return {}

    def _normalize_records(self, records: dict[object, object]) -> dict[str, dict[str, object]]:
        normalized = {}
        for path, record in records.items():
            if not isinstance(path, str) or not isinstance(record, dict):
                continue
            try:
                normalized[self._normalize_relative(path)] = record
            except ValueError:
                continue
        return normalized

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        payload = {
            "_format": self.format_name,
            "baseline_files": sorted(self.baseline_files) if self.baseline_files is not None else None,
            "baseline_complete": self.baseline_complete,
            "records": self.records,
        }
        temporary.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def _normalize_relative(self, relative: str) -> str:
        if not isinstance(relative, str) or Path(relative).is_absolute():
            raise ValueError("작업 공간 기준 상대 경로가 필요합니다.")
        normalized = Path(os.path.normpath(relative))
        if not normalized.parts or normalized.parts[0] == "..":
            raise ValueError("workspace 밖의 경로는 사용할 수 없습니다")
        key = normalized.as_posix()
        self.tools.resolve(key)
        return key

    def _record_current(
        self,
        relative: str,
        *,
        max_content_size: Optional[int] = None,
        strict_size: bool = False,
    ) -> dict[str, object]:
        path = self.tools.resolve(relative)
        if not path.exists():
            return {"exists": False, "content": None}
        if not path.is_file():
            raise ValueError("파일만 승인 기준선으로 저장할 수 있습니다.")
        size = path.stat().st_size
        if size > self.max_file_size:
            return {"exists": True, "content": None, "unsupported": True}
        if max_content_size is not None and size > max_content_size:
            if strict_size:
                raise ValueError("복구 기준선 저장 공간 한도를 초과했습니다.")
            return {"exists": True, "content": None, "unsupported": "workspace baseline size limit"}
        try:
            return {"exists": True, "content": path.read_text(encoding="utf-8")}
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"텍스트 기준선을 저장하지 못했습니다: {exc}") from exc

    def _stored_content_size(self, excluding: Optional[str] = None) -> int:
        return sum(
            len(str(record["content"]).encode("utf-8"))
            for path, record in self.records.items()
            if path != excluding and isinstance(record.get("content"), str)
        )

    def _limit_record(
        self,
        relative: str,
        record: dict[str, object],
        *,
        stored_content_size: Optional[int] = None,
        strict: bool = False,
    ) -> dict[str, object]:
        content = record.get("content")
        if not isinstance(content, str):
            return record
        if stored_content_size is None:
            stored_content_size = self._stored_content_size(excluding=relative)
        if stored_content_size + len(content.encode("utf-8")) <= self.max_total_content_size:
            return record
        if strict:
            raise ValueError("복구 기준선 저장 공간 한도를 초과했습니다.")
        return {"exists": record.get("exists", True), "content": None, "unsupported": "workspace baseline size limit"}

    def capture(self, relative: str) -> None:
        relative = self._normalize_relative(relative)
        if relative not in self.records:
            stored_size = self._stored_content_size()
            remaining = max(0, self.max_total_content_size - stored_size)
            record = self._record_current(relative, max_content_size=remaining)
            self.records[relative] = self._limit_record(relative, record, stored_content_size=stored_size)
            self._save()

    def capture_many(self, relatives: Iterable[str], *, snapshot_complete: bool = True) -> None:
        normalized = set()
        invalid_paths = False
        for relative in relatives:
            try:
                normalized.add(self._normalize_relative(relative))
            except (OSError, ValueError):
                invalid_paths = True
        normalized = sorted(normalized)
        changed = False
        if self.baseline_files is None:
            self.baseline_files = set(normalized)
            self.baseline_complete = snapshot_complete and not invalid_paths
            changed = True
        stored_size = self._stored_content_size()
        for relative in normalized:
            # Files created after the initial snapshot have an absent baseline.
            if self.baseline_files is not None and relative not in self.baseline_files:
                continue
            if relative in self.records:
                continue
            try:
                remaining = max(0, self.max_total_content_size - stored_size)
                record = self._record_current(relative, max_content_size=remaining)
            except (OSError, ValueError):
                record = {"exists": True, "content": None, "unsupported": "baseline could not be captured"}
            record = self._limit_record(relative, record, stored_content_size=stored_size)
            self.records[relative] = record
            content = record.get("content")
            if isinstance(content, str):
                stored_size += len(content.encode("utf-8"))
            changed = True
        if changed:
            self._save()

    def approve(self, relative: str) -> None:
        relative = self._normalize_relative(relative)
        stored_size = self._stored_content_size(excluding=relative)
        remaining = max(0, self.max_total_content_size - stored_size)
        record = self._record_current(relative, max_content_size=remaining, strict_size=True)
        self.records[relative] = self._limit_record(relative, record, stored_content_size=stored_size, strict=True)
        self._save()

    def rollback(self, relative: str) -> tuple[str, Optional[str]]:
        relative = self._normalize_relative(relative)
        record = self.records.get(relative)
        if record is None and self.baseline_files is not None and self.baseline_complete and relative not in self.baseline_files:
            record = {"exists": False, "content": None}
        if record is None:
            raise ValueError("이 파일의 복구 기준선이 없습니다.")
        target = self.tools.resolve(relative)
        if record.get("unsupported"):
            raise ValueError("파일의 복구 기준선을 저장하지 못해 자동 되돌리기를 지원하지 않습니다.")
        if record.get("exists"):
            recovery_location = None
            if target.is_file():
                recovery = self._recovery_path(target)
                recovery.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(target, recovery)
                recovery_location = str(recovery)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(record.get("content", "")), encoding="utf-8")
            return str(target), recovery_location
        if not target.exists():
            return str(target), None
        if not target.is_file():
            raise ValueError("새 파일만 복구 보관함으로 이동할 수 있습니다.")
        recovery = self._recovery_path(target)
        recovery.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(target), str(recovery))
        return str(recovery), str(recovery)

    def _recovery_path(self, target: Path) -> Path:
        relative = target.relative_to(self.workspace)
        return self.recovery_root / self.workspace.name / str(time.time_ns()) / relative


def _ok(result: object) -> dict[str, object]:
    return {"ok": True, "result": result}


def _error(message: str) -> dict[str, object]:
    return {"ok": False, "error": message}


def handle_request(request: object, workspace: Path) -> dict[str, object]:
    """Handle one read-oriented request without leaving the workspace root."""
    if not isinstance(request, dict):
        return _error("JSON 요청은 객체 형식이어야 합니다.")
    method = request.get("method")
    tools = WorkspaceTools(workspace)
    if method == "ping":
        return _ok({"status": "ready", "protocol": "jsonl-v1"})
    if method == "usage_snapshot":
        try:
            return _ok(fetch_codex_rate_limits(environment=build_terminal_environment(workspace)))
        except (OSError, RuntimeError, ValueError) as exc:
            return _error(f"사용량을 불러오지 못했습니다: {exc}")
    if method == "usage_reset":
        credit_id = request.get("creditId", "")
        idempotency_key = request.get("idempotencyKey", "")
        if not isinstance(credit_id, str) or not isinstance(idempotency_key, str):
            return _error("초기화권 식별자가 올바르지 않습니다.")
        try:
            outcome = consume_codex_reset_credit(
                credit_id,
                idempotency_key=idempotency_key or None,
                environment=build_terminal_environment(workspace),
            )
            return _ok({"outcome": outcome})
        except (OSError, RuntimeError, ValueError) as exc:
            return _error(f"사용량을 초기화하지 못했습니다: {exc}")
    if method == "workspace_snapshot":
        files, directories = tools.scan_tree()
        approvals = ApprovalStore(workspace)
        snapshot_complete = len(files) < 100_000
        rollback_warning = None if snapshot_complete else "일부 파일의 복구 기준선을 확인할 수 없습니다."
        try:
            approvals.capture_many(files, snapshot_complete=snapshot_complete)
            if not approvals.baseline_complete:
                rollback_warning = "일부 파일의 복구 기준선을 확인할 수 없습니다."
        except OSError as exc:
            rollback_warning = f"복구 기준선을 저장하지 못했습니다: {exc}"
        return _ok({"files": files, "directories": directories, "git": tools.git_snapshot(), "rollbackWarning": rollback_warning})
    if method == "read_file":
        relative = request.get("path")
        if not isinstance(relative, str):
            return _error("path가 필요합니다.")
        try:
            return _ok({"path": relative, "content": tools.read_file(relative)})
        except (OSError, ValueError) as exc:
            return _error(str(exc))
    if method == "git_diff_file":
        relative = request.get("path")
        if not isinstance(relative, str):
            return _error("path가 필요합니다.")
        try:
            return _ok({"path": relative, "content": tools.git_diff_file(relative)})
        except (OSError, ValueError) as exc:
            return _error(str(exc))
    if method in {"approve_file", "rollback_file"}:
        relative = request.get("path")
        if not isinstance(relative, str):
            return _error("path가 필요합니다.")
        try:
            store = ApprovalStore(workspace)
            if method == "approve_file":
                store.approve(relative)
                return _ok({"path": relative, "action": "approved"})
            location, recovery_location = store.rollback(relative)
            return _ok({
                "path": relative,
                "action": "rolled_back",
                "location": location,
                "recoveryLocation": recovery_location,
            })
        except (OSError, ValueError) as exc:
            return _error(str(exc))
    return _error(f"지원하지 않는 bridge 메서드입니다: {method}")


def process_lines(lines: Iterable[str], workspace: Path) -> list[str]:
    output = []
    for line in lines:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            response = handle_request(request, workspace)
        except (json.JSONDecodeError, TypeError) as exc:
            response = _error(f"잘못된 JSON 요청입니다: {exc}")
        output.append(json.dumps(response, ensure_ascii=True))
    return output


def process_request(line: str, workspace: Path) -> str:
    """Process one request for a desktop command invocation."""
    try:
        response = handle_request(json.loads(line), workspace)
    except (json.JSONDecodeError, TypeError) as exc:
        response = _error(f"잘못된 JSON 요청입니다: {exc}")
    return json.dumps(response, ensure_ascii=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal Agent JSON-line bridge")
    parser.add_argument("workspace", nargs="?", default=str(Path.cwd()))
    parser.add_argument("--request", help="process one JSON request and exit")
    args = parser.parse_args()
    workspace = Path(args.workspace).expanduser()
    if args.request is not None:
        print(process_request(args.request, workspace), flush=True)
        return
    # Keep bridge process alive and answer each request immediately. Tauri
    # reuses this process for file, Git, and usage requests.
    for line in sys.stdin:
        for response in process_lines([line], workspace):
            print(response, flush=True)


if __name__ == "__main__":
    main()
