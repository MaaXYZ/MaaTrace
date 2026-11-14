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

    def test_single_file_alias_normalizes_source_and_root_together(self):
        # A parent-directory alias reproduces the same mixed canonical/source
        # path bug as Windows short (8.3) paths, without requiring an NTFS volume.
        alias = self.root / "session-alias"
        try:
            alias.symlink_to(self.session, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("directory alias creation unavailable on this host")
        selected = alias / "events.jsonl"
        report = self.call("validate", selected)
        self.assertTrue(report["valid"])
        self.assertEqual(report["event_count"], 2)
        output = self.root / "aliased-file.zip"
        self.call("export", selected, "-o", output)
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(archive.read("events.jsonl"), (self.session / "events.jsonl").read_bytes())

        # Canonicalizing parents must not bypass the ban on selecting a linked
        # event file itself.
        linked_event = self.root / "linked-events.jsonl"
        linked_event.symlink_to(self.session / "events.jsonl")
        self.call("validate", linked_event, expected=2, json_output=False)

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

    def test_native_hierarchy_transitions_and_nested_result(self):
        root = "run-1:task:1"
        custom = "run-1:action:10"
        nested = "run-1:task:2"
        child = "run-1:action:20"
        events = []

        def operation(kind, span, task, name, parent=None, *, outcome=None, reason=None):
            native = {"task_id": int(task), "entry" if kind.startswith(("task.", "subtask.")) else "name": name}
            if reason:
                native["reason"] = reason
            value = event(len(events) + 1, kind, span=span, task=task, data={
                "native_details": native, "flow": {"parent_source": "native_event"} if parent else {},
            })
            value["source"] = "context" if kind.startswith("subtask.") or task == "2" else "tasker"
            value["parent_span_id"] = parent
            if outcome:
                value["data"]["outcome"] = outcome
            events.append(value)

        operation("task.started", root, "1", "Start")
        operation("action.started", custom, "1", "Custom", root)
        operation("subtask.started", nested, "2", "Nested", custom)
        operation("action.started", child, "2", "Child", nested)
        operation("action.finished", child, "2", "Child", nested, outcome="succeeded")
        operation("subtask.finished", nested, "2", "Nested", custom, outcome="cancelled", reason="cancelled")
        operation("action.finished", custom, "1", "Custom", root, outcome="succeeded")
        for reason, source, target in (("next", "A", "B"), ("on_error", "B", "Recovery"),
                                       ("jump_back", "Recovery", "A")):
            events.append(event(len(events) + 1, "node.transition", data={"flow": {"transition": {
                "from_span_id": "run-1:node:30", "to_span_id": "run-1:node:31",
                "from_node_name": source, "to_node_name": target,
                "reason": reason, "provenance": "observed", "evidence_seqs": [len(events) + 1],
            }}}))
        operation("task.finished", root, "1", "Start", outcome="succeeded", reason="completed")
        self.write(events)
        summary = self.call("inspect")
        run = summary["runs"][0]
        self.assertEqual((run["entry"], run["task_id"], run["outcome"], run["reason"]),
                         ("Start", "1", "succeeded", "completed"))
        self.assertEqual([item["node_name"] for item in run["route"]], ["Custom", "Child"])
        tree = {item["span_id"]: item for item in run["hierarchy"]}
        self.assertEqual(tree[nested]["kind"], "subtask")
        self.assertEqual(tree[nested]["parent_span_id"], custom)
        self.assertEqual(tree[nested]["parent_source"], "native_event")
        self.assertEqual((tree[nested]["outcome"], tree[nested]["reason"]), ("cancelled", "cancelled"))
        self.assertEqual(tree[child]["parent_span_id"], nested)
        self.assertEqual([item["reason"] for item in run["transitions"]], ["next", "on_error", "jump_back"])
        self.assertEqual(summary["termination_reasons"], {"cancelled": 1, "completed": 1})
        self.assertEqual(summary["unclosed_spans"], 0)
        text = self.call("inspect", json_output=False).stdout
        self.assertIn("subtask Nested [cancelled; reason=cancelled", text)
        self.assertIn("Transition: B → Recovery [on_error]", text)
        self.assertIn("Transition: Recovery → A [jump_back; resumes successor list]", text)

    def test_resource_diagnostics_without_task_are_summarized(self):
        load = event(1, "resource.finished", task=None, run=None, data={
            "native_message": "Resource.Loading.Failed", "native_status": "Failed", "outcome": "failed",
            "native_ids": {"res_id": "9007199254740993"},
            "native_details": {"res_id": 9007199254740993, "path": "resources/bundle", "type": "Bundle",
                               "status": 4000, "errors": [{"code": "node_reference_not_found", "file": "pipeline/main.json",
                                 "node": "A", "pointer": "/A/next", "field": "next", "target": "Missing"}]},
        })
        load["source"] = "resource"
        self.write([load])
        summary = self.call("inspect")
        self.assertEqual(summary["runs"], [])
        self.assertEqual((summary["resource_load_total"], summary["resource_failed_total"], summary["resource_error_total"]), (1, 1, 1))
        diagnostic = summary["resource_loads"][0]
        self.assertEqual(diagnostic["res_id"], "9007199254740993")
        self.assertEqual(diagnostic["errors"][0]["pointer"], "/A/next")
        self.assertTrue(summary["validation"]["valid"])
        text = self.call("inspect", json_output=False).stdout
        self.assertIn("Resource loads: 1; failed: 1; diagnostics: 1", text)
        self.assertIn("node_reference_not_found: file=pipeline/main.json; node=A; pointer=/A/next", text)

    def test_resource_diagnostic_samples_are_bounded_and_skip_malformed_values(self):
        events = []
        for seq in range(1, 206):
            value = event(seq, "resource.finished", task=None, run=None, data={
                "native_message": "Resource.Loading.Failed", "native_status": "Failed", "outcome": "failed",
                "native_details": {"path": "x" * 1000, "type": ["malformed"], "errors": [None, "bad", *[
                    {"code": "invalid_node", "file": "f" * 1000, "pointer": {"bad": "type"}} for _ in range(25)]]},
            })
            value["source"] = "resource"
            events.append(value)
        self.write(events)
        summary = self.call("inspect")
        self.assertEqual(summary["resource_load_total"], 205)
        self.assertEqual(summary["resource_error_total"], 205 * 25)
        self.assertEqual(len(summary["resource_loads"]), 200)
        load = summary["resource_loads"][0]
        self.assertEqual(len(load["errors"]), 20)
        self.assertEqual(load["errors_total"], 25)
        self.assertEqual(len(load["path"]), 257)
        self.assertEqual(load["type"], None)
        self.assertNotIn("pointer", load["errors"][0])
        self.call("inspect", json_output=False)

    def test_hierarchy_text_handles_cycles_and_unknown_parents(self):
        events = []
        for span, parent in (("a", "b"), ("b", "a"), ("c", "missing")):
            for phase in ("started", "finished"):
                value = event(len(events) + 1, "action." + phase, span=span,
                              data={"native_details": {"name": span}, "flow": {"parent_source": "native_event"}})
                value["parent_span_id"] = parent
                events.append(value)
        self.write(events)
        text = self.call("inspect", json_output=False).stdout
        self.assertIn("action a [unknown", text)
        self.assertIn("action b [unknown", text)
        self.assertIn("parent=missing (outside displayed tree)", text)

    def test_hierarchy_uses_the_executed_pipeline_node_name(self):
        start = event(1, "node.started", span="run-1:node:10", data={"native_details": {"name": "Source"}})
        finish = event(2, "node.finished", span=start["span_id"], data={
            "native_details": {"name": "Source", "node_details": {"name": "Selected"}}, "outcome": "succeeded",
        })
        self.write([start, finish])
        operation = self.call("inspect")["runs"][0]["hierarchy"][0]
        self.assertEqual(operation["name"], "Selected")

    def test_helper_nodes_keep_native_parent_and_display_known_caller_without_task_start(self):
        start, finish = task_events()
        caller = "run-1:action:10"
        events = [start]
        for kind, span, name, parent, task_parent in (
            ("action", caller, "Custom", start["span_id"], None),
            ("recognition_node", "run-1:node:20", "ChildRecognition", "run-1:task:77", caller),
            ("action_node", "run-1:node:30", "ChildAction", "run-1:task:88", caller),
        ):
            for phase in ("started", "finished"):
                value = event(len(events) + 1, kind + "." + phase, span=span, data={
                    "native_details": {"name": name},
                    "flow": {"parent_source": "native_event", "task_parent_span_id": task_parent},
                })
                value["parent_span_id"] = parent
                events.append(value)
        finish.update(seq=len(events) + 1, elapsed_us=(len(events) + 1) * 100)
        events.append(finish)
        self.write(events)
        run = self.call("inspect")["runs"][0]
        self.assertEqual(run["hierarchy_start_total"], 4)
        self.assertEqual(len(run["hierarchy"]), 4)
        helper = next(item for item in run["hierarchy"] if item["kind"] == "recognition_node")
        self.assertEqual(helper["parent_span_id"], "run-1:task:77")
        self.assertEqual(helper["task_parent_span_id"], caller)
        self.assertNotIn("run-1:task:77", {item["span_id"] for item in run["hierarchy"]})
        text = self.call("inspect", json_output=False).stdout
        recognition = next(line for line in text.splitlines() if "recognition_node ChildRecognition" in line)
        self.assertTrue(recognition.startswith("        recognition_node"), recognition)
        self.assertIn("via unobserved task run-1:task:77", recognition)
        self.assertIn("via unobserved task run-1:task:88", text)

    def test_hierarchy_samples_are_bounded_without_losing_route_totals(self):
        start, finish = task_events()
        events = [start]
        for index in range(205):
            for phase in ("started", "finished"):
                value = event(len(events) + 1, "action." + phase, span=f"run-1:action:{index}",
                              data={"native_details": {"name": f"Node{index}"}})
                value["parent_span_id"] = start["span_id"]
                events.append(value)
        finish.update(seq=len(events) + 1, elapsed_us=(len(events) + 1) * 100)
        events.append(finish)
        self.write(events)
        run = self.call("inspect")["runs"][0]
        self.assertEqual(len(run["hierarchy"]), 200)
        self.assertEqual(run["hierarchy_start_total"], 206)
        self.assertEqual((len(run["route"]), run["route_total"]), (200, 205))
        self.assertIn("... 6 more operation starts", self.call("inspect", json_output=False).stdout)

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
