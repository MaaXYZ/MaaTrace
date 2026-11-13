#!/usr/bin/env python3
"""Black-box tests for the standalone MaaTrace inspection/export CLI.

Only Python's standard library is required. The fixtures model complete runs,
interrupted runs, missing event intervals, and evidence references rather than
copying the tool's parser implementation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import zipfile


TOOL = Path(__file__).resolve().parents[1] / "tools" / "maatrace.py"


def event(seq, kind="framework.event", *, span=None, task="1", run="run-1", data=None):
    native = {"task_id": int(task)} if task else {}
    details = {
        "native_message": "Future.Component.Notification",
        "native_details": native,
        "native_ids": {"task_id": task} if task else {},
        "enrichment": {},
    }
    if data:
        details.update(data)
    return {
        "schema_version": "1.0", "session_id": "session-test", "run_id": run,
        "seq": seq, "timestamp": "2026-11-13T10:00:00.000Z",
        "elapsed_us": seq * 100, "type": kind, "source": "tasker",
        "task_id": task, "span_id": span, "parent_span_id": None, "data": details,
    }


def task_events():
    start = event(1, "task.started", span="run-1:task:1", data={
        "native_message": "Tasker.Task.Starting",
        "native_details": {"task_id": 1, "entry": "Start"},
    })
    finish = event(2, "task.finished", span="run-1:task:1", data={
        "native_message": "Tasker.Task.Succeeded", "native_status": "Succeeded",
        "outcome": "succeeded",
    })
    return [start, finish]


def encoded(events):
    return b"".join((json.dumps(e, ensure_ascii=False) + "\n").encode("utf-8") for e in events)


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="maatrace-tools-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.session = self.root / "session"
        self.session.mkdir()
        self.manifest = {
            "schema_version": "1.0", "session_id": "session-test", "events": "events.jsonl",
            "state": "closed", "recording_complete": True, "open_runs": 0,
        }
        self.write(task_events())

    def write(self, events=None, *, raw=None, manifest=None):
        (self.session / "events.jsonl").write_bytes(raw if raw is not None else encoded(events))
        if manifest:
            self.manifest.update(manifest)
        (self.session / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def call(self, action, path=None, *args, expected=0, json_output=True):
        cmd = [sys.executable, str(TOOL), action, str(path or self.session), *map(str, args)]
        if json_output:
            cmd.append("--json")
        result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=20)
        self.assertEqual(result.returncode, expected, f"{cmd}\nstdout: {result.stdout}\nstderr: {result.stderr}")
        return json.loads(result.stdout) if json_output and result.stdout.strip() else result

    @staticmethod
    def codes(report, key):
        return {entry["code"] for entry in report[key]}

    def test_complete_run_and_single_file_input(self):
        report = self.call("validate")
        self.assertTrue(report["valid"])
        self.assertTrue(report["complete"])
        self.assertEqual(report["event_count"], 2)
        self.assertEqual(report["errors"], [])
        report2 = self.call("validate", self.session / "events.jsonl")
        self.assertEqual(report2["event_count"], 2)
        summary = self.call("inspect")
        self.assertEqual(summary["event_count"], 2)
        self.assertEqual(len(summary["runs"]), 1)
        self.assertEqual(summary["runs"][0]["run_id"], "run-1")
        self.assertIn("Start", json.dumps(summary))

    def test_utf8_output_under_ascii_environment(self):
        start, finish = task_events()
        start["data"]["native_details"]["entry"] = "领取奖励"
        self.write([start, finish])
        environment = dict(os.environ, PYTHONIOENCODING="ascii")
        result = subprocess.run(
            [sys.executable, str(TOOL), "inspect", str(self.session), "--json"],
            capture_output=True, text=True, encoding="utf-8", env=environment, timeout=20,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        summary = json.loads(result.stdout)
        self.assertEqual(summary["runs"][0]["entry"], "领取奖励")

    def test_unknown_event_and_fields_are_accepted(self):
        future = event(3, "my_script.future")
        future["future_envelope"] = {"precise_id": "18446744073709551615"}
        future["data"]["future_payload"] = {"items": [True, None, "未知"]}
        self.write(task_events() + [future])
        report = self.call("validate")
        self.assertTrue(report["valid"])
        self.assertEqual(report["event_count"], 3)
        output = self.root / "future.zip"
        self.call("export", None, "-o", output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("events.jsonl"), (self.session / "events.jsonl").read_bytes())

    def test_bool_is_not_an_integer_sequence(self):
        invalid = event(1)
        invalid["seq"] = True
        self.write([invalid])
        report = self.call("validate", expected=1)
        self.assertFalse(report["valid"])
        self.assertTrue(report["errors"])

    def test_unterminated_partial_tail_is_recoverable(self):
        raw = encoded(task_events()) + b'{"schema_version":"1.0","seq":3'
        self.write(raw=raw, manifest={"state": "recording", "recording_complete": False})
        report = self.call("validate")
        self.assertTrue(report["valid"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["event_count"], 2)
        self.assertIn("incomplete_tail", self.codes(report, "warnings"))
        output = self.root / "interrupted.zip"
        self.call("export", None, "-o", output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("events.jsonl"), raw)

    def test_bad_middle_line_is_an_error(self):
        start, finish = task_events()
        finish["seq"] = 3
        raw = encoded([start]) + b"this is not JSON\n" + encoded([finish])
        self.write(raw=raw)
        report = self.call("validate", expected=1)
        self.assertFalse(report["valid"])
        self.assertIn("malformed_json", self.codes(report, "errors"))
        issue = next(e for e in report["errors"] if e["code"] == "malformed_json")
        self.assertEqual(issue["line"], 2)

    def test_valid_final_record_without_newline_is_read(self):
        self.write(raw=encoded(task_events()).rstrip(b"\n"))
        report = self.call("validate")
        self.assertTrue(report["valid"])
        self.assertEqual(report["event_count"], 2)

    def test_explicit_gap_explains_missing_sequences(self):
        gap = event(2, "trace.gap", task=None, run=None)
        gap["source"] = "recorder"
        gap["data"] = {"reason": "queue_limit", "first_seq": 2, "last_seq": 4, "count": 3}
        self.write([event(1), gap, event(5)], manifest={"recording_complete": False})
        report = self.call("validate")
        self.assertTrue(report["valid"])
        self.assertFalse(report["complete"])
        self.assertEqual(report["event_count"], 3)
        self.assertNotIn("unexplained_sequence_gap", self.codes(report, "errors"))

    def test_known_diagnostic_events_require_their_payload(self):
        cases = (
            ("trace.gap", {"first_seq": 1, "last_seq": 1, "count": 1}),
            ("trace.truncated", {"max_file_bytes": 4096}),
            ("trace.invalid_event", {}),
        )
        for kind, payload in cases:
            with self.subTest(kind=kind):
                invalid = event(1, kind, task=None, run=None)
                invalid["source"] = "recorder"
                invalid["data"] = payload
                self.write([invalid])
                report = self.call("validate", expected=1)
                self.assertFalse(report["valid"])

    def test_gap_count_must_match_declared_range(self):
        gap = event(1, "trace.gap", task=None, run=None)
        gap["source"] = "recorder"
        gap["data"] = {"reason": "queue_limit", "first_seq": 1, "last_seq": 5, "count": 2}
        self.write([gap, event(6)])
        report = self.call("validate", expected=1)
        self.assertFalse(report["valid"])

    def test_unexplained_sequence_gap_is_reported(self):
        self.write([event(1), event(3)])
        report = self.call("validate", expected=1)
        self.assertIn("unexplained_sequence_gap", self.codes(report, "errors"))

    def test_duplicate_terminal_is_an_error(self):
        start, finish = task_events()
        duplicate = json.loads(json.dumps(finish))
        duplicate["seq"] = 3
        self.write([start, finish, duplicate])
        report = self.call("validate", expected=1)
        self.assertIn("duplicate_terminal", self.codes(report, "errors"))

    def test_unclosed_operation_is_unknown_not_failed(self):
        self.write([task_events()[0]], manifest={"state": "recording", "recording_complete": False, "open_runs": 1})
        report = self.call("validate")
        self.assertTrue(report["valid"])
        self.assertFalse(report["complete"])
        self.assertIn("unclosed_span", self.codes(report, "warnings"))
        summary = self.call("inspect")
        self.assertGreaterEqual(summary["unclosed_spans"], 1)
        self.assertNotEqual(summary["runs"][0].get("outcome"), "failed")

    def test_inspect_includes_nested_task_execution(self):
        start, finish = task_events()
        child_start = event(2, "action.started", span="run-1:action:7", task="2", data={
            "native_message": "Node.Action.Starting", "native_details": {"task_id": 2, "name": "Child", "node_id": 8, "action_id": 7},
            "native_ids": {"task_id": "2", "node_id": "8", "action_id": "7"},
        })
        child_start["source"] = "context"
        child_start["parent_span_id"] = start["span_id"]
        child_finish = json.loads(json.dumps(child_start))
        child_finish.update(seq=3, elapsed_us=300, type="action.finished")
        child_finish["data"].update(native_message="Node.Action.Succeeded", native_status="Succeeded", outcome="succeeded")
        finish["seq"] = 4
        finish["elapsed_us"] = 400
        self.write([start, child_start, child_finish, finish])
        summary = self.call("inspect")
        self.assertEqual(summary["event_count"], 4)
        self.assertEqual(len(summary["runs"]), 1)
        self.assertIn("Child", json.dumps(summary))
        self.assertEqual(summary["unclosed_spans"], 0)

    def add_evidence(self):
        image = b"\x89PNG\r\n\x1a\nexample-evidence"
        config = b'{"name":"ClosePopup","recognition":"DirectHit"}\n'
        (self.session / "artifacts").mkdir()
        (self.session / "configs").mkdir()
        (self.session / "artifacts/frame.png").write_bytes(image)
        (self.session / "configs/node.json").write_bytes(config)
        e = event(1)
        e["data"]["artifacts"] = [{"id": "frame-1", "availability": "available", "path": "artifacts/frame.png",
                                  "mime_type": "image/png", "size": len(image), "sha256": hashlib.sha256(image).hexdigest()}]
        e["data"]["config"] = {"availability": "available", "path": "configs/node.json", "size": len(config),
                               "sha256": hashlib.sha256(config).hexdigest()}
        self.write([e])
        return image, config

    def test_evidence_and_export_checksums(self):
        self.add_evidence()
        report = self.call("validate")
        self.assertTrue(report["valid"])
        output = self.root / "export.zip"
        self.call("export", None, "-o", output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {"manifest.json", "events.jsonl", "artifacts/frame.png", "configs/node.json", "export.json"})
            inventory = json.loads(archive.read("export.json"))
            expected_files = {"manifest.json", "events.jsonl", "artifacts/frame.png", "configs/node.json"}
            self.assertEqual({e["path"] for e in inventory["files"]}, expected_files)
            for entry in inventory["files"]:
                actual = archive.read(entry["path"])
                self.assertEqual(entry["size"], len(actual))
                self.assertEqual(entry["sha256"], hashlib.sha256(actual).hexdigest())
            self.assertTrue(inventory["validation"]["valid"])

    def test_modified_evidence_fails_validation(self):
        self.add_evidence()
        (self.session / "artifacts/frame.png").write_bytes(b"different bytes")
        report = self.call("validate", expected=1)
        self.assertFalse(report["valid"])
        self.assertIn("artifacts/frame.png", json.dumps(report["errors"]))

    def test_missing_evidence_fails_validation(self):
        self.add_evidence()
        (self.session / "configs/node.json").unlink()
        report = self.call("validate", expected=1)
        self.assertFalse(report["valid"])
        self.assertIn("configs/node.json", json.dumps(report["errors"]))

    def test_omitted_evidence_is_explicit_and_not_a_missing_file(self):
        e = event(1)
        e["data"]["artifacts"] = [{"id": "frame-1", "availability": "omitted", "reason": "debug_required_or_not_retained"}]
        self.write([e])
        report = self.call("validate")
        self.assertTrue(report["valid"])

    def test_single_file_export_excludes_unrelated_siblings(self):
        self.add_evidence()
        (self.session / "manifest.json").unlink()
        (self.session / "unrelated.txt").write_text("unrelated user data", encoding="utf-8")
        output = self.root / "single-file.zip"
        self.call("export", self.session / "events.jsonl", "-o", output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(set(archive.namelist()), {
                "events.jsonl", "artifacts/frame.png", "configs/node.json", "export.json",
            })

    def test_corrupt_manifest_can_be_exported_for_diagnosis(self):
        raw = b'{"schema_version":"1.0",broken'
        (self.session / "manifest.json").write_bytes(raw)
        output = self.root / "broken-manifest.zip"
        result = self.call("export", None, "-o", output)
        self.assertFalse(result["validation"]["valid"])
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("manifest.json"), raw)

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_manifest_fifo_never_blocks_validation_or_export(self):
        (self.session / "manifest.json").unlink()
        os.mkfifo(self.session / "manifest.json")
        report = self.call("validate", expected=1)
        self.assertFalse(report["valid"])
        output = self.root / "manifest-pipe.zip"
        self.call("export", None, "-o", output, expected=2, json_output=False)
        self.assertFalse(output.exists())

    def test_export_rejects_traversing_artifact_reference(self):
        outside = self.root / "secret.txt"
        outside.write_text("must never enter export", encoding="utf-8")
        e = event(1)
        e["data"]["artifacts"] = [{"availability": "available", "path": "../secret.txt"}]
        self.write([e])
        output = self.root / "unsafe.zip"
        self.call("export", None, "-o", output, expected=2, json_output=False)
        self.assertFalse(output.exists())

    def test_export_rejects_symlink_even_if_not_referenced(self):
        outside = self.root / "secret.txt"
        outside.write_text("must never enter export", encoding="utf-8")
        try:
            (self.session / "link.txt").symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("symlink creation unavailable on this host")
        output = self.root / "unsafe-link.zip"
        self.call("export", None, "-o", output, expected=2, json_output=False)
        self.assertFalse(output.exists())

    @unittest.skipUnless(hasattr(os, "mkfifo"), "requires POSIX FIFO support")
    def test_export_rejects_special_files_without_blocking(self):
        os.mkfifo(self.session / "pipe")
        output = self.root / "unsafe-pipe.zip"
        self.call("export", None, "-o", output, expected=2, json_output=False)
        self.assertFalse(output.exists())

    def test_export_output_cannot_be_inside_source(self):
        output = self.session / "nested" / "export.zip"
        self.call("export", None, "-o", output, expected=2, json_output=False)
        self.assertFalse(output.exists())

    def test_export_does_not_overwrite_existing_destination(self):
        output = self.root / "existing.zip"
        output.write_bytes(b"keep-existing-file")
        self.call("export", None, "-o", output, expected=2, json_output=False)
        self.assertEqual(output.read_bytes(), b"keep-existing-file")


if __name__ == "__main__":
    unittest.main()
