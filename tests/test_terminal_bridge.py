import json
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from personal_agent.terminal_bridge import encode_command, run


class TerminalBridgeTests(unittest.TestCase):
    def test_encode_command_keeps_workspace_and_payload(self):
        encoded = encode_command("write", text="echo hello\n")
        self.assertEqual(json.loads(encoded), {"command": "write", "text": "echo hello\n"})

    def test_resize_command_uses_integer_dimensions(self):
        encoded = encode_command("resize", columns=140, rows=45)
        self.assertEqual(json.loads(encoded), {"command": "resize", "columns": 140, "rows": 45})

    def test_non_object_input_returns_an_error_without_crashing(self):
        output = StringIO()
        with patch("personal_agent.terminal_bridge.sys.stdin", StringIO("[]\n")), patch(
            "personal_agent.terminal_bridge.sys.stdout", output
        ), patch("personal_agent.terminal_bridge.TerminalSession") as terminal_session:
            run(Path("."))

        event = json.loads(output.getvalue().strip())
        self.assertEqual(event["event"], "error")
        terminal_session.assert_not_called()


if __name__ == "__main__":
    unittest.main()
