#!/usr/bin/env python3
"""CI acceptance checks for traces emitted by upstream PipelineTesting."""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


TOOL = Path(__file__).resolve().parents[1] / "tools" / "ci" / "validate_pipeline_trace.py"
SPEC = importlib.util.spec_from_file_location("pipeline_trace_validator", TOOL)
VALIDATOR = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(VALIDATOR)


def event(seq, kind, span=None, *, source="context", data=None, task="1", run="run-1"):
    payload = {
        "native_message": "Native.Test.Notification", "native_details": {"task_id": 1},
        "native_ids": {"task_id": "1"}, "enrichment": {},
    }
    if data:
        payload.update(data)
    return {
        "schema_version": "1.0", "session_id": "session-test", "seq": seq,
        "timestamp": "2026-11-13T12:00:00.000Z", "elapsed_us": seq * 100,
        "type": kind, "source": source, "run_id": run, "task_id": task,
        "span_id": span, "parent_span_id": None, "data": payload,
    }


class PipelineTraceTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pipeline-trace-validator-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.trace = self.root / "traces"
        self.session = self.trace / "session-test"
        self.session.mkdir(parents=True)
        config = b'{"recognition":"DirectHit","action":"DoNothing"}\n'
        (self.session / "configs").mkdir()
        (self.session / "configs" / "node.json").write_bytes(config)
        self.config = {"availability": "available", "path": "configs/node.json",
                       "size": len(config), "sha256": hashlib.sha256(config).hexdigest()}
        self.events = [
            event(1, "task.started", "task-1", source="tasker", data={
                "enrichment": {"context_capture": {"attached": True, "method": "explicit_tasker_sink"}}}),
            event(2, "node.started", "node-1", data={"config": self.config}),
            event(3, "decision.started", "decision-1"),
            event(4, "recognition.started", "reco-1"),
            event(5, "recognition.finished", "reco-1", data={"native_status": "Succeeded", "outcome": "matched"}),
            event(6, "decision.finished", "decision-1"),
            event(7, "action.started", "action-1"),
            event(8, "action.finished", "action-1", data={"native_status": "Failed", "outcome": "failed"}),
            event(9, "node.finished", "node-1", data={"native_status": "Failed", "outcome": "failed"}),
            event(10, "node.started", "interrupted-node"),
            event(11, "task.finished", "task-1", source="tasker", data={"native_status": "Failed", "outcome": "failed"}),
        ]
        self.manifest = {"schema_version": "1.0", "session_id": "session-test", "events": "events.jsonl",
                         "state": "closed", "recording_complete": False, "recording_errors": 0, "dropped": 0}
        self.write()

    def write(self, events=None):
        selected = self.events if events is None else events
        (self.session / "events.jsonl").write_text(
            "".join(json.dumps(e) + "\n" for e in selected), encoding="utf-8")
        (self.session / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")

    def report(self):
        return VALIDATOR.validate(self.trace)

    def assert_error(self, code):
        report = self.report()
        self.assertFalse(report["valid"], report)
        self.assertIn(code, {e["code"] for e in report["errors"]}, report)

    def test_expected_native_failures_and_interrupted_nodes_are_accepted(self):
        report = self.report()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["tasks_started"], 1)
        self.assertEqual(report["native_failed_events"], 3)
        self.assertEqual(report["unclosed_spans"], 1)
        self.assertEqual(report["config_file_count"], 1)
        self.assertGreater(report["warning_count"], 0)

    def test_tasker_object_lifecycle_outside_runs_is_accepted(self):
        def lifecycle(message):
            return event(1, "framework.event", source="tasker", task=None, run=None, data={
                "native_message": message, "native_details": {}, "native_ids": {},
            })

        self.events = [lifecycle("Tasker.Created"), *self.events, lifecycle("Tasker.Destroying")]
        for seq, item in enumerate(self.events, 1):
            item["seq"], item["elapsed_us"] = seq, seq * 100
        self.write()
        report = self.report()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["tasks_started"], 1)
        self.assertEqual(report["tasks_finished"], 1)
        self.assertEqual(report["run_count"], 1)
        self.assertEqual(report["event_types"]["framework.event"], 2)

    def test_lifecycle_exemption_does_not_hide_missing_execution_identity(self):
        cases = (
            ("tasker", "task.started", "Tasker.Task.Starting"),
            ("tasker", "task.finished", "Tasker.Task.Failed"),
            ("tasker", "framework.event", "Tasker.FutureOperation.Created"),
            ("tasker", "framework.event", "Tasker.Created.Extra"),
            ("tasker", "framework.event", ["Tasker.Created"]),
            ("tasker", "task.started", "Tasker.Created"),
            ("context", "framework.event", "Tasker.Created"),
            ("context", "framework.event", "Node.PipelineNode.Cancelled"),
        )
        for source, kind, message in cases:
            with self.subTest(source=source, kind=kind, message=message):
                extra = event(12, kind, source=source, task=None, run=None, data={
                    "native_message": message, "native_details": {}, "native_ids": {},
                })
                self.write([*self.events, extra])
                self.assert_error("missing_task_identity")

    def test_lifecycle_exemption_preserves_native_identity_consistency_check(self):
        extra = event(12, "framework.event", source="tasker", task=None, run=None, data={
            "native_message": "Tasker.Destroying",
        })
        self.write([*self.events, extra])
        self.assert_error("task_identity_mismatch")

    def test_unknown_cancelled_node_event_in_a_run_is_preserved(self):
        # Current recorders preserve new native terminal phases as framework.event.
        cancelled = event(11, "framework.event", "interrupted-node", data={
            "native_message": "Node.PipelineNode.Cancelled",
            "native_details": {"task_id": 1, "node_id": 2, "reason": "cancelled"},
        })
        self.events.insert(-1, cancelled)
        self.events[-1]["seq"], self.events[-1]["elapsed_us"] = 12, 1200
        self.write()
        report = self.report()
        self.assertTrue(report["valid"], report)
        self.assertEqual(report["event_types"]["framework.event"], 1)
        self.assertEqual(report["unclosed_spans"], 1)

    def test_windows_recording_manifest_without_shutdown_is_accepted(self):
        self.manifest = {"schema_version": "1.0", "session_id": "session-test", "events": "events.jsonl", "state": "recording"}
        self.write()
        report = self.report()
        self.assertTrue(report["valid"], report)
        self.assertIn("session_not_closed", {e["code"] for e in report["warnings"]})

    def test_empty_root_and_missing_output_are_rejected(self):
        for path in (self.root / "missing", self.root / "empty"):
            with self.subTest(path=path):
                if path.name == "empty": path.mkdir()
                report = VALIDATOR.validate(path)
                self.assertFalse(report["valid"])
                self.assertIn("no_sessions", {e["code"] for e in report["errors"]})

    def test_empty_session_is_rejected(self):
        self.write([])
        self.assert_error("empty_session")

    def test_resource_only_does_not_count_as_pipeline_capture(self):
        self.write([event(1, "resource.started", source="resource", task=None, run=None)])
        self.assert_error("missing_event_coverage")

    def test_context_attachment_is_required_for_every_task(self):
        for attachment in ({"attached": False}, {}):
            with self.subTest(attachment=attachment):
                self.events[0]["data"]["enrichment"] = {"context_capture": attachment}
                self.write()
                self.assert_error("context_sink_missing")

    def test_recording_gap_is_rejected_even_when_well_formed(self):
        self.events.append(event(12, "trace.gap", source="recorder", task=None, run=None,
                                 data={"reason": "queue_limit", "first_seq": 12, "last_seq": 13, "count": 2}))
        self.write()
        self.assert_error("recording_gap")

    def test_recording_truncation_is_rejected(self):
        self.events.append(event(12, "trace.truncated", source="recorder", task=None, run=None,
                                 data={"reason": "file_size_limit", "max_file_bytes": 1024}))
        self.write()
        self.assert_error("recording_truncated")

    def test_manifest_reports_recorder_errors_or_dropped_records(self):
        for field in ("recording_errors", "dropped"):
            with self.subTest(field=field):
                self.manifest[field] = 1
                self.write()
                self.assert_error("recorder_counter")
                self.manifest[field] = 0

    def test_corrupt_and_missing_config_sidecars_are_rejected(self):
        target = self.session / "configs" / "node.json"
        target.write_bytes(b'corrupted')
        self.assert_error("artifact_hash_mismatch")
        target.unlink()
        self.assert_error("missing_artifact")

    def test_config_integrity_metadata_is_required(self):
        self.events[1]["data"]["config"].pop("sha256")
        self.write()
        self.assert_error("missing_evidence_integrity")

    def test_missing_manifest_is_rejected(self):
        (self.session / "manifest.json").unlink()
        self.assert_error("missing_manifest")

    def test_partial_tail_and_missing_newline_are_rejected(self):
        path = self.session / "events.jsonl"
        with path.open("ab") as output: output.write(b'{"partial":')
        self.assert_error("unfinished_jsonl")
        self.write()
        path.write_bytes(path.read_bytes().rstrip(b"\n"))
        self.assert_error("unfinished_jsonl")

    def test_top_level_task_must_be_closed_even_when_nodes_are_not(self):
        self.write(self.events[:-1])
        self.assert_error("unfinished_task")

    def test_missing_context_identity_is_rejected(self):
        self.events[3]["run_id"] = None
        self.write()
        self.assert_error("missing_task_identity")

    def test_task_terminal_identity_must_match(self):
        self.events[-1]["task_id"] = "different-task"
        self.write()
        self.assert_error("task_terminal_mismatch")

    def test_nested_native_task_ids_remain_in_outer_run(self):
        for e in self.events[2:9]:
            e["task_id"] = "child-task"
            e["data"]["native_details"]["task_id"] = "child-task"
        self.write()
        report = self.report()
        self.assertTrue(report["valid"], report)

    def test_cli_emits_machine_readable_summary_and_provenance(self):
        metadata = self.root / "upstream.json"
        metadata.write_text(json.dumps({"repository": "MaaXYZ/MaaFramework", "run_id": 123, "head_sha": "abc"}), encoding="utf-8")
        output = self.root / "result" / "report.json"
        result = subprocess.run([sys.executable, str(TOOL), "--trace-dir", str(self.trace), "--output", str(output),
                                 "--upstream-metadata", str(metadata)], capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        report = json.loads(output.read_text())
        self.assertTrue(report["valid"])
        self.assertEqual(report["upstream"]["run_id"], 123)
        self.assertIn("PASS:", result.stdout)


if __name__ == "__main__":
    unittest.main()
