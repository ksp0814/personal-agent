import os
import subprocess
import sys
from pathlib import Path


IGNORED_DIRECTORIES = {
    ".git",
    ".agent",
    ".venv",
    ".venv-py314",
    ".venv-py313",
    ".idea",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "target",
    "dist",
}
MAX_FILE_READ_BYTES = 2_000_000
MAX_DIFF_BYTES = 4_000_000


class WorkspaceTools:
    def __init__(self, root: Path):
        self.root = root.resolve()

    def resolve(self, relative: str) -> Path:
        path = (self.root / relative).resolve()
        if path != self.root and self.root not in path.parents:
            raise ValueError("workspace 밖의 경로는 사용할 수 없습니다")
        return path

    def list_files(self, limit: int = 100) -> list[str]:
        files = []
        pending = [self.root]
        while pending and len(files) < limit:
            path = pending.pop()
            if path != self.root and path.name in IGNORED_DIRECTORIES:
                continue
            try:
                if path.is_dir() and not path.is_symlink():
                    with os.scandir(path) as entries:
                        pending.extend(sorted((Path(entry.path) for entry in entries), reverse=True))
                elif path.is_file():
                    files.append(str(path.relative_to(self.root)))
            except OSError:
                continue
        return files

    def scan_tree(self, file_limit: int = 100_000, directory_limit: int = 10_000) -> tuple[list[str], list[str]]:
        """Scan files and directories in one traversal for the desktop tree."""
        files, directories = [], []
        for current, dir_names, file_names in os.walk(self.root):
            dir_names[:] = sorted(name for name in dir_names if name not in IGNORED_DIRECTORIES)
            relative_dir = Path(current).relative_to(self.root)
            if relative_dir != Path(".") and len(directories) < directory_limit:
                directories.append(str(relative_dir))
            for name in sorted(file_names, key=str.casefold):
                if len(files) >= file_limit:
                    break
                files.append(str(relative_dir / name))
            if len(files) >= file_limit and len(directories) >= directory_limit:
                break
        return files[:file_limit], directories[:directory_limit]

    def scan_tree_snapshot(
        self, file_limit: int = 100_000, directory_limit: int = 10_000
    ) -> tuple[list[str], list[str], dict[str, tuple[int, int]]]:
        """Scan tree and collect stat data in the worker that owns the filesystem walk."""
        files, directories, metadata = [], [], {}
        for current, dir_names, file_names in os.walk(self.root):
            dir_names[:] = sorted(name for name in dir_names if name not in IGNORED_DIRECTORIES)
            relative_dir = Path(current).relative_to(self.root)
            if relative_dir != Path(".") and len(directories) < directory_limit:
                directories.append(str(relative_dir))
            for name in sorted(file_names, key=str.casefold):
                if len(files) >= file_limit:
                    break
                relative = str(relative_dir / name)
                files.append(relative)
                try:
                    stat = (Path(current) / name).stat()
                except OSError:
                    continue
                metadata[relative] = (stat.st_mtime_ns, stat.st_size)
            if len(files) >= file_limit and len(directories) >= directory_limit:
                break
        return files[:file_limit], directories[:directory_limit], metadata

    def read_file(self, relative: str) -> str:
        path = self.resolve(relative)
        if not path.is_file():
            raise FileNotFoundError(relative)
        if path.stat().st_size > MAX_FILE_READ_BYTES:
            raise ValueError(f"파일이 너무 큽니다. {MAX_FILE_READ_BYTES // 1_000_000}MB 이하 파일만 열 수 있습니다.")
        return path.read_text(encoding="utf-8")

    def write_file(self, relative: str, content: str) -> None:
        path = self.resolve(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def git_status(self) -> str:
        return self._run_readonly(["git", "status", "--short"], "Git 상태를 확인하지 못했습니다") or "변경 사항이 없습니다."

    def git_diff(self) -> str:
        return self._run_readonly(["git", "diff", "--"], "Git diff를 확인하지 못했습니다") or "현재 diff가 없습니다."

    def git_snapshot(self) -> dict:
        """Return read-only branch and porcelain status information."""
        try:
            status_result = subprocess.run(
                ["git", "status", "--porcelain=v1", "-b", "--untracked-files=all"],
                cwd=self.root,
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=30,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"available": False, "message": str(exc), "branch": "", "entries": {}}
        if status_result.returncode != 0:
            detail = status_result.stderr.strip()
            return {"available": False, "message": detail or "Git 저장소가 아닙니다.", "branch": "", "entries": {}}
        entries = {}
        lines = status_result.stdout.splitlines()
        branch = "(detached HEAD)"
        if lines and lines[0].startswith("## "):
            branch = lines[0][3:].split("...", 1)[0] or branch
            lines = lines[1:]
        for line in lines:
            if len(line) < 4:
                continue
            code = line[:2]
            relative = line[3:]
            if " -> " in relative:
                relative = relative.rsplit(" -> ", 1)[-1]
            if (self.root / relative).is_dir():
                continue
            entries[relative] = code
        return {"available": True, "message": "", "branch": branch, "entries": entries}

    def git_worktree_create(self, target: Path, branch: str, base: str = "HEAD") -> Path:
        """Create an isolated Git worktree beside the current workspace."""
        target = Path(target).expanduser().resolve()
        branch = branch.strip()
        if not branch or branch.startswith("-"):
            raise ValueError("브랜치 이름이 올바르지 않습니다.")
        if target == self.root or target.exists():
            raise ValueError("worktree 대상 폴더가 이미 존재합니다.")
        if not target.parent.exists():
            raise ValueError("worktree 상위 폴더가 존재하지 않습니다.")
        result = subprocess.run(
            ["git", "worktree", "add", "-b", branch, str(target), base],
            cwd=self.root,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=120,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            raise RuntimeError(detail or "Git worktree를 생성하지 못했습니다.")
        return target

    def git_worktree_create_many(self, targets: list[Path], branches: list[str], base: str = "HEAD") -> list[Path]:
        if len(targets) != len(branches) or not targets:
            raise ValueError("worktree 대상과 브랜치 수가 일치해야 합니다.")
        return [self.git_worktree_create(target, branch, base) for target, branch in zip(targets, branches)]

    def git_diff_file(self, relative: str) -> str:
        """Return tracked, staged, or untracked diff for one contained file."""
        path = self.resolve(relative)
        outputs = []
        for command in (
            ["git", "diff", "--no-ext-diff", "--", relative],
            ["git", "diff", "--cached", "--no-ext-diff", "--", relative],
        ):
            try:
                result = subprocess.run(command, cwd=self.root, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30)
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"Git Diff를 확인하지 못했습니다: {exc}"
            if result.returncode == 0 and result.stdout:
                outputs.append(result.stdout)
        if outputs:
            return _limit_output("\n".join(outputs).rstrip(), MAX_DIFF_BYTES)
        if path.is_file() and not (self.root / ".git").exists():
            return "Git 저장소가 아닙니다."
        if path.is_file():
            try:
                result = subprocess.run(
                    ["git", "diff", "--no-index", "--no-ext-diff", "--", os.devnull, relative],
                    cwd=self.root,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    capture_output=True,
                    timeout=30,
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                return f"Git Diff를 확인하지 못했습니다: {exc}"
            if result.stdout:
                return _limit_output(result.stdout.rstrip(), MAX_DIFF_BYTES)
        return "Git 기준 변경 내용이 없습니다."

    def run_validation(self) -> str:
        """Run project tests without accepting arbitrary user-supplied commands."""
        commands = [[sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]]
        results = []
        for command in commands:
            try:
                result = subprocess.run(command, cwd=self.root, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=120)
            except (OSError, subprocess.TimeoutExpired) as exc:
                results.append(f"검증 실패: {exc}")
                continue
            output = (result.stdout + result.stderr).strip()
            results.append(f"$ {' '.join(command)}\n{output or '출력이 없습니다.'}\n종료 코드: {result.returncode}")
        return "\n\n".join(results)

    def _run_readonly(self, command: list[str], error: str) -> str:
        try:
            result = subprocess.run(command, cwd=self.root, text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=30)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{error}: {exc}"
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()
            return f"{error}: {detail or f'종료 코드: {result.returncode}'}"
        return result.stdout.strip()


def _limit_output(value: str, limit: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= limit:
        return value
    truncated = encoded[:limit].decode("utf-8", errors="ignore")
    return f"{truncated}\n\n[성능 보호를 위해 출력이 {limit // 1_000_000}MB에서 잘렸습니다.]"
