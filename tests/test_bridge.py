import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from personal_agent.bridge import ApprovalStore, handle_request, process_request


class BridgeTests(unittest.TestCase):
    def test_ping_returns_protocol_status(self):
        with tempfile.TemporaryDirectory() as directory:
            result = handle_request({"method": "ping"}, Path(directory))
        self.assertEqual(result, {"ok": True, "result": {"status": "ready", "protocol": "jsonl-v1"}})

    def test_snapshot_stays_inside_workspace(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "README.md").write_text("hello", encoding="utf-8")
            result = handle_request({"method": "workspace_snapshot"}, root)
        self.assertEqual(result["ok"], True)
        self.assertIn("README.md", result["result"]["files"])

    def test_read_file_rejects_path_escape(self):
        with tempfile.TemporaryDirectory() as directory:
            result = handle_request({"method": "read_file", "path": "../secret.txt"}, Path(directory))
        self.assertFalse(result["ok"])
        self.assertIn("workspace", result["error"])

    def test_stream_protocol_returns_one_json_object_per_request(self):
        from personal_agent.bridge import process_lines

        with tempfile.TemporaryDirectory() as directory:
            output = process_lines([json.dumps({"method": "ping"})], Path(directory))
        self.assertEqual(json.loads(output[0])["result"]["protocol"], "jsonl-v1")

    def test_single_request_serializes_bridge_response(self):
        with tempfile.TemporaryDirectory() as directory:
            output = process_request(json.dumps({"method": "ping"}), Path(directory))
        self.assertEqual(json.loads(output)["result"]["status"], "ready")

    def test_valid_json_non_objects_return_protocol_errors(self):
        with tempfile.TemporaryDirectory() as directory:
            output = process_request("[]", Path(directory))

        result = json.loads(output)
        self.assertFalse(result["ok"])
        self.assertIn("객체 형식", result["error"])

    def test_stream_mode_answers_each_request(self):
        output = StringIO()
        with tempfile.TemporaryDirectory() as directory, patch("personal_agent.bridge.sys.argv", ["bridge", directory]), patch(
            "personal_agent.bridge.sys.stdin", StringIO('{"method":"ping"}\n{"method":"ping"}\n')
        ), patch("personal_agent.bridge.sys.stdout", output):
            from personal_agent.bridge import main

            main()

        responses = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(responses), 2)
        self.assertTrue(all(response["result"]["status"] == "ready" for response in responses))

    def test_usage_reset_forwards_idempotency_key(self):
        with tempfile.TemporaryDirectory() as directory, patch(
            "personal_agent.bridge.consume_codex_reset_credit", return_value="ok"
        ) as consume:
            result = handle_request(
                {"method": "usage_reset", "creditId": "credit-1", "idempotencyKey": "request-1"},
                Path(directory),
            )

        self.assertEqual(result, {"ok": True, "result": {"outcome": "ok"}})
        consume.assert_called_once()
        self.assertEqual(consume.call_args.args, ("credit-1",))
        self.assertEqual(consume.call_args.kwargs["idempotency_key"], "request-1")
        self.assertIn("environment", consume.call_args.kwargs)

    def test_approval_store_restores_captured_content(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("before", encoding="utf-8")
            recovery_root = root / "trash"
            store = ApprovalStore(root, recovery_root=recovery_root)
            store.capture("note.txt")
            target.write_text("after", encoding="utf-8")
            _, recovered_location = store.rollback("note.txt")
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertIsNotNone(recovered_location)
            recovered = list(recovery_root.rglob("note.txt"))
            self.assertEqual(len(recovered), 1)
            self.assertEqual(recovered[0].read_text(encoding="utf-8"), "after")

    def test_rollback_request_returns_recovery_location(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("before", encoding="utf-8")
            recovery_root = root / "trash"
            store_type = ApprovalStore

            with patch(
                "personal_agent.bridge.ApprovalStore",
                side_effect=lambda workspace: store_type(workspace, recovery_root=recovery_root),
            ):
                handle_request({"method": "workspace_snapshot"}, root)
                target.write_text("after", encoding="utf-8")
                result = handle_request({"method": "rollback_file", "path": "note.txt"}, root)

            recovery = Path(result["result"]["recoveryLocation"])
            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertEqual(recovery.read_text(encoding="utf-8"), "after")

    def test_snapshot_captures_more_than_one_hundred_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index in range(101):
                (root / f"file-{index:03}.txt").write_text(f"before-{index}", encoding="utf-8")

            result = handle_request({"method": "workspace_snapshot"}, root)
            self.assertTrue(result["ok"])
            target = root / "file-100.txt"
            target.write_text("after", encoding="utf-8")
            store = ApprovalStore(root, recovery_root=root / "trash")

            store.rollback("file-100.txt")

            self.assertEqual(target.read_text(encoding="utf-8"), "before-100")

    def test_truncated_snapshot_warns_that_rollback_coverage_is_partial(self):
        files = ["file.txt"] * 100_000
        with tempfile.TemporaryDirectory() as directory, patch(
            "personal_agent.bridge.WorkspaceTools.scan_tree", return_value=(files, [])
        ), patch("personal_agent.bridge.ApprovalStore.capture_many") as capture_many:
            result = handle_request({"method": "workspace_snapshot"}, Path(directory))

        self.assertIn("일부 파일", result["result"]["rollbackWarning"])
        self.assertFalse(capture_many.call_args.kwargs["snapshot_complete"])

    def test_snapshot_skips_symlinks_that_escape_the_workspace(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            external_file = Path(outside) / "secret.txt"
            external_file.write_text("outside", encoding="utf-8")
            link = root / "external.txt"
            try:
                link.symlink_to(external_file)
            except OSError as exc:
                self.skipTest(f"symlink creation is unavailable: {exc}")

            result = handle_request({"method": "workspace_snapshot"}, root)

            self.assertTrue(result["ok"])
            self.assertIn("일부 파일", result["result"]["rollbackWarning"])
            store = ApprovalStore(root)
            self.assertNotIn("external.txt", store.baseline_files)
            self.assertEqual(external_file.read_text(encoding="utf-8"), "outside")

    def test_new_files_after_snapshot_move_to_recovery_on_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            recovery_root = root / "trash"
            (root / "existing.txt").write_text("initial", encoding="utf-8")
            store = ApprovalStore(root, recovery_root=recovery_root)
            store.capture_many(["existing.txt"])
            new_file = root / "new.txt"
            new_file.write_text("new content", encoding="utf-8")

            # A later refresh must not turn the newly-created file into baseline content.
            store.capture_many(["existing.txt", "new.txt"])
            reopened = ApprovalStore(root, recovery_root=recovery_root)
            location = Path(reopened.rollback("new.txt")[0])

            self.assertFalse(new_file.exists())
            self.assertEqual(location.read_text(encoding="utf-8"), "new content")

    def test_legacy_baseline_remains_readable(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "note.txt"
            target.write_text("after", encoding="utf-8")
            baseline = root / ".agent" / "react-baseline.json"
            baseline.parent.mkdir(parents=True)
            baseline.write_text(json.dumps({"note.txt": {"exists": True, "content": "before"}}), encoding="utf-8")
            store = ApprovalStore(root, recovery_root=root / "trash")

            store.rollback("note.txt")

            self.assertEqual(target.read_text(encoding="utf-8"), "before")
            self.assertEqual(len(list((root / "trash").rglob("note.txt"))), 1)

    def test_approval_store_batches_baseline_persistence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "one.txt").write_text("one", encoding="utf-8")
            (root / "two.txt").write_text("two", encoding="utf-8")
            store = ApprovalStore(root, recovery_root=root / "trash")

            with patch.object(store, "_save", wraps=store._save) as save:
                store.capture_many(["one.txt", "two.txt"])

            self.assertEqual(save.call_count, 1)
            self.assertEqual(set(store.records), {"one.txt", "two.txt"})

    def test_approval_store_moves_new_file_to_recovery_trash(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = ApprovalStore(root, recovery_root=root / "trash")
            store.capture("new.txt")
            (root / "new.txt").write_text("new", encoding="utf-8")
            result = store.rollback("new.txt")[0]
            self.assertFalse((root / "new.txt").exists())
            self.assertTrue(Path(result).exists())


if __name__ == "__main__":
    unittest.main()
