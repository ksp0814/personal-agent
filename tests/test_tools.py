import tempfile
import unittest
from pathlib import Path
import subprocess

from personal_agent.tools import MAX_FILE_READ_BYTES, WorkspaceTools


class WorkspaceToolsTests(unittest.TestCase):
    def test_git_status_reports_clean_non_git_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            result = WorkspaceTools(Path(directory)).git_status()
            self.assertIn("Git 상태를 확인하지 못했습니다", result)

    def test_validation_uses_unittest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tests").mkdir()
            result = WorkspaceTools(root).run_validation()
            self.assertIn("unittest discover", result)

    def test_list_files_preserves_order_and_limit_while_skipping_ignored_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".venv").mkdir()
            (root / "a-folder" / "nested").mkdir(parents=True)
            (root / "a-folder" / "node_modules").mkdir()
            for relative in (
                "readme.md",
                "a-folder/child.txt",
                "a-folder/nested/deep.txt",
                "z.txt",
                ".venv/site.py",
                "a-folder/node_modules/package.js",
            ):
                (root / relative).write_text(relative, encoding="utf-8")

            expected = [
                str(Path("a-folder") / "child.txt"),
                str(Path("a-folder") / "nested" / "deep.txt"),
                "readme.md",
                "z.txt",
            ]
            tools = WorkspaceTools(root)

            self.assertEqual(tools.list_files(), expected)
            self.assertEqual(tools.list_files(limit=2), expected[:2])
            self.assertEqual(tools.list_files(limit=0), [])

    def test_scan_tree_ignores_virtual_environments_and_caches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "src").mkdir()
            (root / ".venv" / "Lib").mkdir(parents=True)
            (root / ".venv-py314").mkdir()
            (root / "__pycache__").mkdir()
            (root / "src" / "main.py").write_text("pass\n", encoding="utf-8")
            (root / ".venv" / "Lib" / "site.py").write_text("ignored\n", encoding="utf-8")

            files, directories = WorkspaceTools(root).scan_tree()

            self.assertEqual(files, [str(Path("src") / "main.py")])
            self.assertEqual(directories, [str(Path("src"))])

    def test_scan_tree_snapshot_returns_file_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            note = root / "note.txt"
            note.write_text("안녕하세요\n", encoding="utf-8")

            files, directories, metadata = WorkspaceTools(root).scan_tree_snapshot()

            self.assertEqual(files, ["note.txt"])
            self.assertEqual(directories, [])
            self.assertEqual(metadata["note.txt"], (note.stat().st_mtime_ns, note.stat().st_size))

    def test_read_file_rejects_oversized_files_before_loading_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "large.txt").write_bytes(b"x" * (MAX_FILE_READ_BYTES + 1))

            with self.assertRaisesRegex(ValueError, "너무 큽니다"):
                WorkspaceTools(root).read_file("large.txt")

    def test_git_worktree_create(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            (root / "README.md").write_text("main\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "초기 커밋"], cwd=root, check=True)

            target = root.parent / "sample-worktree"
            created = WorkspaceTools(root).git_worktree_create(target, "feature/sample")

            self.assertEqual(created, target.resolve())
            self.assertEqual((target / "README.md").read_text(encoding="utf-8"), "main\n")
            self.assertTrue((target / ".git").exists())
            subprocess.run(["git", "worktree", "remove", "--force", str(target)], cwd=root, check=True)

    def test_git_worktree_create_many(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Test User"], cwd=root, check=True)
            (root / "README.md").write_text("main\n", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "초기 커밋"], cwd=root, check=True)

            targets = [root.parent / "sample-one", root.parent / "sample-two"]
            created = WorkspaceTools(root).git_worktree_create_many(targets, ["feature/one", "feature/two"])

            self.assertEqual(created, [target.resolve() for target in targets])
            self.assertTrue(all((target / "README.md").exists() for target in targets))
            for target in targets:
                subprocess.run(["git", "worktree", "remove", "--force", str(target)], cwd=root, check=True)
    def test_git_snapshot_and_untracked_diff(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            note = root / "note.txt"
            note.write_text("안녕하세요\n", encoding="utf-8")
            tools = WorkspaceTools(root)

            snapshot = tools.git_snapshot()

            self.assertTrue(snapshot["available"])
            self.assertEqual(snapshot["entries"]["note.txt"], "??")
            self.assertIn("note.txt", tools.git_diff_file("note.txt"))
            self.assertIn("안녕하세요", tools.git_diff_file("note.txt"))

    def test_git_snapshot_lists_untracked_files_not_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            (root / "new-folder").mkdir()
            (root / "new-folder" / "note.txt").write_text("hello\n", encoding="utf-8")

            snapshot = WorkspaceTools(root).git_snapshot()

            self.assertNotIn("new-folder", snapshot["entries"])
            self.assertEqual(snapshot["entries"]["new-folder/note.txt"], "??")


if __name__ == "__main__":
    unittest.main()
