#!/usr/bin/env python3
"""Validate trace output from the unmodified upstream PipelineTesting suite.

This is deliberately stricter than the interactive inspector: the completed CI
process must leave nonempty, newline-complete JSONL and useful event coverage.
Expected framework failures and interrupted node spans are not recorder errors.
"""
from __future__ import annotations

import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
import re
import sys
from typing import Any


_REQUIRED_TYPES = (
    "task.started", "task.finished", "node.started", "node.finished",
    "recognition.started", "recognition.finished", "action.started", "action.finished",
    "decision.started", "decision.finished",
)
_FATAL_WARNING_CODES = {
    "incomplete_tail", "missing_newline", "source_changed", "tracking_limit",
    "recording_gap", "recording_truncated", "invalid_native_event",
}
_MAX_FINDINGS = 200


def _inspector():
    path = Path(__file__).resolve().parents[1] / "maatrace.py"
    spec = importlib.util.spec_from_file_location("maatrace_ci_inspector", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load inspector: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _object(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _identifier(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


class PipelineValidation:
    def __init__(self):
        self.inspector = _inspector()
        self.report: dict[str, Any] = {
            "valid": True, "sessions": [], "session_count": 0, "event_count": 0,
            "event_types": {}, "run_count": 0, "tasks_started": 0, "tasks_finished": 0,
            "native_failed_events": 0, "unclosed_spans": 0,
            "config_reference_count": 0, "config_file_count": 0, "artifact_count": 0,
            "error_count": 0, "warning_count": 0, "errors": [], "warnings": [],
        }
        self.types: Counter[str] = Counter()
        self.config_files: set[tuple[str, str]] = set()
        self.artifact_files: set[tuple[str, str]] = set()

    def finding(self, severity: str, code: str, message: str, session: str | None = None,
                line: int | None = None) -> None:
        self.report[severity + "_count"] += 1
        if severity == "error":
            self.report["valid"] = False
        findings = self.report["errors" if severity == "error" else "warnings"]
        if len(findings) < _MAX_FINDINGS:
            findings.append({"code": code, "message": message, "session": session, "line": line})

    def session(self, path: Path) -> None:
        name = path.name
        try:
            analysis = self.inspector.analyze(path)
        except (OSError, ValueError, self.inspector.TraceError) as exc:
            self.finding("error", "unreadable_session", str(exc), name)
            return
        summary = analysis.report
        self.report["session_count"] += 1
        self.report["event_count"] += summary["event_count"]
        self.report["unclosed_spans"] += summary["unclosed_spans"]
        if len(self.report["sessions"]) < _MAX_FINDINGS:
            self.report["sessions"].append({
                "directory": name, "session_id": summary["session_id"],
                "event_count": summary["event_count"], "manifest_state": _object(analysis.manifest).get("state"),
                "unclosed_spans": summary["unclosed_spans"],
            })
        for severity, collection in (("error", "errors"), ("warning", "warnings")):
            for finding in summary[collection]:
                level = "error" if finding["code"] in _FATAL_WARNING_CODES else severity
                self.finding(level, finding["code"], finding["message"], name, finding.get("line"))
        if summary["error_count"] > len(summary["errors"]):
            self.finding("error", "additional_validation_errors", "inspector error details exceeded its bounded report", name)
            self.report["error_count"] += summary["error_count"] - len(summary["errors"]) - 1
        if summary["warning_count"] > len(summary["warnings"]):
            # The total number remains visible even when samples are bounded.
            self.report["warning_count"] += summary["warning_count"] - len(summary["warnings"])
        manifest = analysis.manifest
        if not isinstance(manifest, dict):
            self.finding("error", "missing_manifest", "recorder did not produce a readable manifest.json", name)
        else:
            if manifest.get("state") in ("error", "truncated"):
                self.finding("error", "recorder_state", f"manifest state is {manifest['state']}", name)
            for counter in ("recording_errors", "dropped"):
                if counter in manifest and (not _integer(manifest[counter]) or manifest[counter] != 0):
                    self.finding("error", "recorder_counter", f"manifest {counter} must be zero: {manifest[counter]!r}", name)
        if not summary["event_count"]:
            self.finding("error", "empty_session", "recorder produced no complete events", name)

        starts: dict[str, tuple[str, str | None]] = {}
        terminals: set[str] = set()
        seen_runs: set[str] = set()
        for line, raw, terminated, oversized in self.inspector._records(analysis.events):
            if not terminated:
                self.finding("error", "unfinished_jsonl", "completed test left an unterminated JSONL record", name, line)
            if oversized:
                continue  # Already reported by the shared validator.
            try:
                event = self.inspector._load_json(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError):
                continue
            if not isinstance(event, dict):
                continue
            kind, source = event.get("type"), event.get("source")
            if not isinstance(kind, str):
                continue
            if kind in self.types or len(self.types) < 1000:
                self.types[kind] += 1
            data = _object(event.get("data"))
            extra = _object(data.get("enrichment"))
            native = _object(data.get("native_details"))
            run, task = event.get("run_id"), event.get("task_id")
            if data.get("native_status") == "Failed":
                self.report["native_failed_events"] += 1
            if kind in ("trace.gap", "trace.truncated", "trace.invalid_event"):
                self.finding("error", "recorder_diagnostic", f"unexpected CI recording diagnostic: {kind}", name, line)
            if source in ("tasker", "context"):
                if not _identifier(run) or not _identifier(task):
                    self.finding("error", "missing_task_identity", "tasker/context event needs nonempty run_id and task_id", name, line)
                native_task = native.get("task_id")
                if native_task is not None and str(native_task) != task:
                    self.finding("error", "task_identity_mismatch", "task_id disagrees with native task_id", name, line)
            if _identifier(run):
                seen_runs.add(run)
            if kind == "task.started":
                self.report["tasks_started"] += 1
                capture = _object(extra.get("context_capture"))
                if capture.get("method") != "plugin_callback" and capture.get("attached") is not True:
                    self.finding("error", "context_sink_missing", "task has neither native plugin callbacks nor an attached legacy Context sink", name, line)
                if _identifier(run) and _identifier(task):
                    if run in starts:
                        self.finding("error", "duplicate_run_start", f"run started more than once: {run}", name, line)
                    starts[run] = (task, event.get("span_id"))
            elif kind == "task.finished":
                self.report["tasks_finished"] += 1
                if not _identifier(run) or run not in starts:
                    self.finding("error", "orphan_task_finish", "task terminal has no matching run start", name, line)
                elif starts[run] != (task, event.get("span_id")):
                    self.finding("error", "task_terminal_mismatch", "task terminal identity differs from its start", name, line)
                elif run in terminals:
                    self.finding("error", "duplicate_task_finish", f"run ended more than once: {run}", name, line)
                if _identifier(run):
                    terminals.add(run)
            elif source == "context" and _identifier(run):
                if run not in starts or run in terminals:
                    self.finding("error", "context_outside_run", "Context event is outside an observed top-level task lifecycle", name, line)
            for key in ("config", "artifacts"):
                references = data.get(key, [] if key == "artifacts" else {})
                references = references if isinstance(references, list) else [references]
                for reference in references:
                    if not isinstance(reference, dict):
                        continue
                    if reference.get("reason") == "write_failed":
                        self.finding("error", "evidence_write_failed", f"{key} could not be persisted", name, line)
                    if reference.get("availability") != "available":
                        continue
                    relative = reference.get("path")
                    digest, size = reference.get("sha256"), reference.get("size")
                    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", digest) or not _integer(size):
                        self.finding("error", "missing_evidence_integrity", f"available {key} needs size and SHA256", name, line)
                    if isinstance(relative, str):
                        collection = self.config_files if key == "config" else self.artifact_files
                        collection.add((name, relative))
                        if key == "config":
                            self.report["config_reference_count"] += 1
        for run in starts.keys() - terminals:
            self.finding("error", "unfinished_task", f"completed PipelineTesting left top-level task open: {run}", name)
        self.report["run_count"] += len(seen_runs)

    def run(self, directory: Path, upstream_metadata: Path | None = None) -> dict[str, Any]:
        if not directory.is_dir():
            self.finding("error", "missing_trace_directory", f"trace directory not found: {directory}")
            sessions = []
        elif (directory / "events.jsonl").is_file():
            sessions = [directory]
        else:
            sessions = sorted(p for p in directory.glob("session-*") if p.is_dir())
        if not sessions:
            self.finding("error", "no_sessions", "PipelineTesting generated no MaaTrace sessions")
        for session in sessions:
            self.session(session)
        for kind in _REQUIRED_TYPES:
            if not self.types[kind]:
                self.finding("error", "missing_event_coverage", f"no {kind} events captured")
        if not self.config_files:
            self.finding("error", "missing_config_snapshots", "no available effective configuration snapshot was captured")
        self.report["event_types"] = dict(sorted(self.types.items()))
        self.report["config_file_count"] = len(self.config_files)
        self.report["artifact_count"] = len(self.artifact_files)
        if upstream_metadata is not None:
            try:
                metadata = self.inspector._load_json(upstream_metadata.read_bytes())
                if not isinstance(metadata, dict):
                    raise ValueError("upstream metadata must be an object")
                self.report["upstream"] = self.inspector._compact(metadata)
            except (OSError, ValueError, RecursionError) as exc:
                self.finding("error", "invalid_upstream_metadata", str(exc))
        return self.report


def validate(trace_dir: Path, upstream_metadata: Path | None = None) -> dict[str, Any]:
    return PipelineValidation().run(trace_dir, upstream_metadata)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--upstream-metadata", type=Path)
    args = parser.parse_args(argv)
    try:
        report = validate(args.trace_dir, args.upstream_metadata)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Pipeline trace validation failed: {exc}", file=sys.stderr)
        return 1
    print(f"{'PASS' if report['valid'] else 'FAIL'}: {report['session_count']} sessions, "
          f"{report['event_count']} events, {report['tasks_started']} tasks, "
          f"{report['config_file_count']} configuration snapshots; "
          f"{report['error_count']} errors, {report['warning_count']} diagnostic warnings")
    for item in report["errors"][:20]:
        print(f"  ERROR [{item['code']}] {item['message']}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
