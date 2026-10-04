import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from personal_agent.agent import Agent, OfflineProvider
from personal_agent.memory import Memory
from personal_agent.policy import Policy
from personal_agent.tools import WorkspaceTools


def make_agent(tmp_path: Path, approved: bool = True) -> Agent:
    return Agent(WorkspaceTools(tmp_path), Memory(tmp_path / ".agent" / "memory.sqlite3"), Policy(lambda _: approved), OfflineProvider())


class AgentTests(unittest.TestCase):
    def test_memory_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = make_agent(Path(directory))
            self.assertEqual(agent.handle("/remember 선호하는 언어는 Python"), "기억했습니다.")
            self.assertIn("선호하는 언어는 Python", agent.handle("/memory"))


    def test_write_requires_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            agent = make_agent(root, approved=False)
            self.assertIn("승인되지 않아", agent.handle("/write note.txt hello"))
            self.assertFalse((root / "note.txt").exists())


    def test_workspace_path_is_contained(self):
        with tempfile.TemporaryDirectory() as directory:
            result = make_agent(Path(directory)).handle("/read ../secret.txt")
            self.assertIn("workspace 밖", result)

    def test_interaction_history_and_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = make_agent(Path(directory))
            agent.handle("간단한 요청")
            self.assertIn("간단한 요청", agent.handle("/history"))
            self.assertIn("간단한 요청", agent.handle("/resume"))

    def test_test_command_uses_the_standard_library_test_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            agent = make_agent(Path(directory))
            with patch.object(agent.tools, "run_validation", return_value="unittest result") as run_validation:
                self.assertEqual(agent.handle("/test"), "unittest result")
            run_validation.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
