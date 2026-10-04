import subprocess
import unittest
from unittest.mock import Mock, patch

from personal_agent.model_catalog import _call_codex, consume_codex_reset_credit


class CodexRpcTests(unittest.TestCase):
    def test_call_codex_returns_matching_json_rpc_result(self):
        process = Mock()
        process.communicate.return_value = ('{"id":1,"result":{}}\n{"id":2,"result":{"outcome":"ok"}}\n', "")
        process.poll.return_value = 0

        with patch("personal_agent.model_catalog.subprocess.Popen", return_value=process):
            result = _call_codex("account/test", {})

        self.assertEqual(result, {"outcome": "ok"})
        self.assertIn('"method": "account/test"', process.communicate.call_args.kwargs["input"])

    def test_call_codex_kills_a_server_that_times_out(self):
        process = Mock()
        process.communicate.side_effect = [subprocess.TimeoutExpired("codex", 1), ("", "")]
        process.poll.side_effect = [None, -9]

        with patch("personal_agent.model_catalog.subprocess.Popen", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "시간이 초과"):
                _call_codex("account/test", {}, timeout=1)

        process.kill.assert_called_once_with()
        self.assertEqual(process.communicate.call_count, 2)

    def test_reset_credit_reuses_the_supplied_idempotency_key(self):
        with patch("personal_agent.model_catalog._call_codex", return_value={"outcome": "ok"}) as call:
            outcome = consume_codex_reset_credit("credit-1", idempotency_key="request-1")

        self.assertEqual(outcome, "ok")
        self.assertEqual(call.call_args.args[2], "codex")
        self.assertEqual(call.call_args.args[3], None)
        self.assertEqual(call.call_args.args[1]["idempotencyKey"], "request-1")


if __name__ == "__main__":
    unittest.main()
