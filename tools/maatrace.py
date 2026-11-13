#!/usr/bin/env python3
"""Inspect, validate, and export MaaTrace sessions without third-party packages.

Exit status: 0 = success (warnings may describe an incomplete recording),
1 = invalid recording, 2 = command/I/O/unsafe export error. Export preserves even
invalid recordings for diagnosis; unsafe paths and links are always refused.
"""
from __future__ import annotations

import argparse
from collections import Counter, OrderedDict
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import stat
import sys
import tempfile
from typing import Any, Iterator
import zipfile

MAX_LINE_BYTES = 32 * 1024 * 1024
MAX_FINDINGS = 1000
MAX_TRACKED_SPANS = 100000
MAX_RUNS = 1000
MAX_SAMPLES = 200
CHUNK_SIZE = 1024 * 1024


class TraceError(Exception):
    """An I/O or unsafe-path error suitable for showing without a traceback."""


def _object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON member: {key}")
        result[key] = value
    return result


def _load_json(value: str | bytes) -> Any:
    def invalid_constant(text: str) -> None:
        raise ValueError(f"non-JSON numeric constant: {text}")
    return json.loads(value, object_pairs_hook=_object_pairs, parse_constant=invalid_constant)


def _integer(value: Any, minimum: int = 0) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_path(root: Path, relative: Any) -> Path:
    """Accept portable relative regular-file references, never links or traversal."""
    if not isinstance(relative, str) or not relative or "\\" in relative or "\0" in relative:
        raise TraceError(f"unsafe relative path: {relative!r}")
    posix = PurePosixPath(relative)
    if (posix.is_absolute() or PureWindowsPath(relative).drive or ":" in relative
            or ".." in posix.parts or relative != posix.as_posix() or relative == "."):
        raise TraceError(f"unsafe relative path: {relative!r}")
    path = root
    for part in posix.parts:
        path = path / part
        if path.is_symlink():
            raise TraceError(f"symbolic link is not allowed: {relative}")
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError as exc:
        raise TraceError(f"path escapes the session: {relative}") from exc
    return path


def _locate(value: Path) -> tuple[Path, Path]:
    path = value.expanduser().absolute()
    if path.is_symlink():
        raise TraceError("select the real session path, not a symbolic link")
    if path.is_dir():
        root = path.resolve()
        events = _safe_path(root, "events.jsonl")
    elif path.is_file():
        # Resolve the file and its parent together. On Windows, resolving only
        # the parent expands an 8.3 alias (RUNNER~1) while leaving the event path
        # under the alias, so later relative_to(root) fails. The unresolved final
        # path was checked for symlinks above before any canonicalization.
        events = path.resolve()
        root = events.parent
    else:
        raise TraceError(f"session or event file not found: {value}")
    if not events.is_file():
        raise TraceError(f"event file not found: {events}")
    return root, events


def _records(path: Path) -> Iterator[tuple[int, bytes, bool, bool]]:
    """Yield bounded records: line, bytes, newline present, line too large."""
    with path.open("rb") as source:
        number = 0
        remaining = os.fstat(source.fileno()).st_size
        while remaining:
            raw = source.readline(min(MAX_LINE_BYTES + 1, remaining))
            if not raw:
                return
            remaining -= len(raw)
            number += 1
            oversized = len(raw) > MAX_LINE_BYTES
            if oversized and not raw.endswith(b"\n"):
                rest = b""
                while remaining:
                    rest = source.readline(min(CHUNK_SIZE, remaining))
                    remaining -= len(rest)
                    if not rest or rest.endswith(b"\n"):
                        break
                terminated = bool(rest and rest.endswith(b"\n"))
            else:
                terminated = raw.endswith(b"\n")
            yield number, raw, terminated, oversized


def _digest(path: Path) -> tuple[int, str]:
    count, digest = 0, hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(CHUNK_SIZE):
            count += len(chunk)
            digest.update(chunk)
    return count, digest.hexdigest()


def _fingerprint(path: Path) -> tuple[int, int, int, int]:
    current = path.stat()
    return current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns


def _references(data: dict[str, Any]) -> Iterator[dict[str, Any]]:
    # Deliberately do not treat arbitrary native config fields named 'path' as
    # attachments. They can refer to device paths, not files in the session.
    for container in (data, _dict(data.get("enrichment"))):
        artifacts = container.get("artifacts", [])
        if isinstance(artifacts, list):
            for reference in artifacts:
                if isinstance(reference, dict):
                    yield reference
        config = container.get("config")
        if isinstance(config, dict):
            yield config


def _compact(value: Any, depth: int = 4) -> Any:
    if isinstance(value, str):
        return value if len(value) <= 256 else value[:256] + "…"
    if isinstance(value, (dict, list)) and depth == 0:
        return "…"
    if isinstance(value, dict):
        return {key: _compact(item, depth - 1) for key, item in list(value.items())[:20]}
    if isinstance(value, list):
        return [_compact(item, depth - 1) for item in value[:20]]
    return value


class Analysis:
    def __init__(self, path: Path):
        self.root, self.events = _locate(path)
        self.report: dict[str, Any] = {
            "valid": True, "complete": True, "event_count": 0,
            "error_count": 0, "warning_count": 0, "errors": [], "warnings": [],
            "session_id": None, "unclosed_spans": 0,
        }
        self.manifest: dict[str, Any] | None = None
        self.types: Counter[str] = Counter()
        self.outcomes: Counter[str] = Counter()
        self.runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.active: dict[str, dict[str, Any]] = {}
        self.finished: OrderedDict[str, None] = OrderedDict()
        self.lost_open_spans = 0
        self.artifacts: dict[str, tuple[int, str]] = {}
        self.gaps: list[dict[str, Any]] = []
        self.gap_count = 0
        self.expected_seq = 1
        self.last_seq = 0
        self.was_truncated = False
        self._tracking_warning = False
        self.unsafe_export = False
        self.read_fingerprints = {self.events.relative_to(self.root).as_posix(): _fingerprint(self.events)}

    def finding(self, severity: str, code: str, message: str, line: int | None = None,
                incomplete: bool = True) -> None:
        self.report[severity + "_count"] += 1
        collection = self.report["errors" if severity == "error" else "warnings"]
        if len(collection) < MAX_FINDINGS:
            collection.append({"code": code, "line": line, "message": message})
        if severity == "error":
            self.report["valid"] = False
        if incomplete or severity == "error":
            self.report["complete"] = False

    def read_manifest(self) -> None:
        path = self.root / "manifest.json"
        if not path.exists() and not path.is_symlink():
            return
        try:
            path = _safe_path(self.root, "manifest.json")
            if not path.is_file():
                raise TraceError("manifest must be a regular file")
            if path.stat().st_size > MAX_LINE_BYTES:
                raise ValueError("manifest is too large")
            self.read_fingerprints["manifest.json"] = _fingerprint(path)
            manifest = _load_json(path.read_bytes())
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be an object")
            self.manifest = manifest
            if isinstance(manifest.get("session_id"), str):
                self.report["session_id"] = manifest["session_id"]
            if "events" in manifest:
                _safe_path(self.root, manifest["events"])
            if manifest.get("state") not in (None, "closed"):
                self.finding("warning", "session_not_closed", f"manifest state: {manifest['state']}")
            if manifest.get("recording_complete") is False:
                self.finding("warning", "recording_incomplete", "manifest reports an incomplete recording")
        except (OSError, ValueError, TraceError, RecursionError) as exc:
            if isinstance(exc, TraceError):
                self.unsafe_export = True
            self.finding("error", "invalid_manifest", str(exc))

    def schema(self, event: Any, line: int) -> bool:
        if not isinstance(event, dict):
            self.finding("error", "invalid_event", "event must be an object", line)
            return False
        required = ("schema_version", "session_id", "run_id", "seq", "timestamp", "elapsed_us",
                    "type", "source", "task_id", "span_id", "parent_span_id", "data")
        missing = [key for key in required if key not in event]
        if missing:
            self.finding("error", "missing_fields", "missing: " + ", ".join(missing), line)
            return False
        good = True
        for key in ("schema_version", "session_id", "type", "source"):
            if not isinstance(event[key], str) or not event[key]:
                self.finding("error", "invalid_field", f"{key} must be a nonempty string", line)
                good = False
        version = event["schema_version"]
        if isinstance(version, str) and not re.fullmatch(r"1\.\d+", version):
            self.finding("error", "unsupported_schema_version", f"unsupported schema: {version}", line)
            good = False
        for key in ("run_id", "task_id", "span_id", "parent_span_id"):
            if event[key] is not None and (not isinstance(event[key], str) or not event[key]):
                self.finding("error", "invalid_field", f"{key} must be null or a nonempty string", line)
                good = False
        for key, minimum in (("seq", 1), ("elapsed_us", 0)):
            if not _integer(event[key], minimum):
                self.finding("error", "invalid_field", f"{key} must be an integer >= {minimum}", line)
                good = False
        try:
            stamp = event["timestamp"]
            if not isinstance(stamp, str) or not re.fullmatch(
                    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", stamp):
                raise ValueError()
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError()
        except (ValueError, TypeError):
            self.finding("error", "invalid_field", "timestamp must be an RFC 3339 timestamp with timezone", line)
            good = False
        if not isinstance(event["data"], dict):
            self.finding("error", "invalid_field", "data must be an object", line)
            return False
        data = event["data"]
        if event["source"] != "recorder" and event["type"] != "trace.invalid_event":
            for key, kind in (("native_message", str), ("native_details", dict), ("native_ids", dict), ("enrichment", dict)):
                if not isinstance(data.get(key), kind):
                    self.finding("error", "invalid_field", f"data.{key} must be {kind.__name__}", line)
                    good = False
            if isinstance(data.get("native_ids"), dict) and any(not isinstance(v, str) for v in data["native_ids"].values()):
                self.finding("error", "invalid_field", "data.native_ids values must be strings", line)
                good = False
        expected_reasons = {"trace.gap": "queue_limit", "trace.truncated": "file_size_limit",
                            "trace.invalid_event": "details_must_be_json_object"}
        if event["type"] in expected_reasons and data.get("reason") != expected_reasons[event["type"]]:
            self.finding("error", "invalid_diagnostic", f"unexpected or missing reason for {event['type']}", line)
            good = False
        if event["type"] == "trace.invalid_event":
            for key, kind in (("native_message", str), ("raw_details", str), ("enrichment", dict)):
                if not isinstance(data.get(key), kind):
                    self.finding("error", "invalid_diagnostic", f"data.{key} must be {kind.__name__}", line)
                    good = False
        if "artifacts" in data and (not isinstance(data["artifacts"], list)
                                    or any(not isinstance(item, dict) for item in data["artifacts"])):
            self.finding("error", "invalid_field", "data.artifacts must be an array of objects", line)
            good = False
        if "config" in data and not isinstance(data["config"], dict):
            self.finding("error", "invalid_field", "data.config must be an object", line)
            good = False
        return good

    def sequence(self, event: dict[str, Any], line: int) -> None:
        seq = event["seq"]
        if seq < self.expected_seq:
            self.finding("error", "non_monotonic_sequence", f"seq {seq} precedes expected {self.expected_seq}", line)
        elif seq > self.expected_seq:
            self.finding("error", "unexplained_sequence_gap", f"missing seq {self.expected_seq}..{seq - 1}", line)
        self.expected_seq = max(self.expected_seq, seq + 1)
        self.last_seq = max(self.last_seq, seq)
        data = event["data"]
        if event["type"] == "trace.gap":
            first, last, count = (data.get(key) for key in ("first_seq", "last_seq", "count"))
            if not (all(_integer(v, 1) for v in (first, last, count)) and first == seq
                    and last >= first and count == last - first + 1):
                self.finding("error", "invalid_gap", "gap must cover seq..last_seq with the exact count", line)
            else:
                self.expected_seq = max(self.expected_seq, last + 1)
                self.last_seq = max(self.last_seq, last)
                self.gap_count += 1
                if len(self.gaps) < MAX_SAMPLES:
                    self.gaps.append({"first_seq": first, "last_seq": last, "count": count})
                self.finding("warning", "recording_gap", f"recorder dropped {count} events ({first}..{last})", line)
                self.lost_open_spans += len(self.active)
                self.active.clear()
                self.finished.clear()
        if self.was_truncated:
            self.finding("error", "event_after_truncation", "events follow the terminal truncation marker", line)
        if event["type"] == "trace.truncated":
            self.was_truncated = True
            if not _integer(data.get("max_file_bytes"), 1):
                self.finding("error", "invalid_truncation", "max_file_bytes must be positive", line)
            self.finding("warning", "recording_truncated", "recording stopped at its file size limit", line)

    def lifecycle(self, event: dict[str, Any], line: int) -> None:
        span, kind = event["span_id"], event["type"]
        if not span:
            return
        if event["parent_span_id"] == span:
            self.finding("error", "self_parent", "span cannot be its own parent", line)
        if kind.endswith(".started"):
            if span in self.active or span in self.finished:
                self.finding("error", "duplicate_start", f"span started more than once: {span}", line)
            elif len(self.active) < MAX_TRACKED_SPANS:
                self.active[span] = {"span_id": span, "type": kind, "seq": event["seq"], "run_id": event["run_id"]}
            else:
                self.tracking_limit(line)
        elif kind.endswith(".finished"):
            if span in self.finished:
                self.finding("error", "duplicate_terminal", f"span ended more than once: {span}", line)
            elif span not in self.active:
                self.finding("warning", "orphan_terminal", f"no observed start for span: {span}", line)
            self.active.pop(span, None)
            self.finished[span] = None
            if len(self.finished) > MAX_TRACKED_SPANS:
                self.finished.popitem(last=False)
                self.tracking_limit(line)

    def tracking_limit(self, line: int) -> None:
        if not self._tracking_warning:
            self._tracking_warning = True
            self.finding("warning", "tracking_limit", "span/run history exceeded the bounded analysis budget", line)

    def check_attachments(self, event: dict[str, Any], line: int) -> None:
        for reference in _references(event["data"]):
            if reference.get("availability") in ("omitted", "unavailable"):
                continue
            if "path" not in reference:
                self.finding("error", "invalid_artifact", "available artifact/config is missing path", line)
                continue
            relative = reference["path"]
            try:
                path = _safe_path(self.root, relative)
            except TraceError as exc:
                self.unsafe_export = True
                self.finding("error", "unsafe_artifact", str(exc), line)
                continue
            try:
                if not path.is_file():
                    raise TraceError(f"missing artifact/config: {relative}")
                if relative not in self.artifacts:
                    self.read_fingerprints[relative] = _fingerprint(path)
                    self.artifacts[relative] = _digest(path)
                size, digest = self.artifacts[relative]
                expected_size, expected_hash = reference.get("size"), reference.get("sha256")
                if expected_size is not None and (not _integer(expected_size) or expected_size != size):
                    self.finding("error", "artifact_size_mismatch", f"artifact size mismatch: {relative}", line)
                if expected_hash is not None and (not isinstance(expected_hash, str) or expected_hash.lower() != digest):
                    self.finding("error", "artifact_hash_mismatch", f"artifact SHA256 mismatch: {relative}", line)
            except (TraceError, OSError) as exc:
                self.finding("error", "missing_artifact", str(exc), line)

    def summarize(self, event: dict[str, Any], line: int) -> None:
        kind = event["type"]
        self.types[kind if kind in self.types or len(self.types) < 1000 else "<other>"] += 1
        data, run_id = event["data"], event["run_id"]
        outcome = data.get("outcome")
        if isinstance(outcome, str):
            self.outcomes[outcome if outcome in self.outcomes or len(self.outcomes) < 1000 else "<other>"] += 1
        if run_id is None:
            return
        if run_id not in self.runs:
            if len(self.runs) >= MAX_RUNS:
                self.tracking_limit(line)
                return
            self.runs[run_id] = {"run_id": run_id, "entry": None, "task_id": None, "outcome": "unknown",
                                 "events": 0, "route": [], "route_total": 0, "decisions": [], "decision_total": 0,
                                 "transitions": [], "transition_total": 0}
        run = self.runs[run_id]
        run["events"] += 1
        native, flow = _dict(data.get("native_details")), _dict(data.get("flow"))
        if kind == "task.started":
            run["entry"], run["task_id"] = native.get("entry"), event["task_id"]
        elif kind == "task.finished":
            run["outcome"] = outcome if isinstance(outcome, str) else "unknown"
        if kind == "action.started":
            run["route_total"] += 1
            if len(run["route"]) < MAX_SAMPLES:
                run["route"].append({"seq": event["seq"], "node_name": native.get("name"),
                                     "task_id": event["task_id"], "span_id": event["span_id"],
                                     "source": "action.started"})
        if kind == "decision.finished":
            run["decision_total"] += 1
            if len(run["decisions"]) < MAX_SAMPLES:
                run["decisions"].append({"seq": event["seq"], "span_id": event["span_id"],
                                        "outcome": outcome, "source_node": native.get("name"),
                                        "candidates": _compact(flow.get("candidates", native.get("list"))),
                                        "flow": _compact(flow)})
        if isinstance(flow.get("transition"), dict):
            run["transition_total"] += 1
            if len(run["transitions"]) < MAX_SAMPLES:
                run["transitions"].append(_compact(flow["transition"]))

    def run(self) -> dict[str, Any]:
        self.read_manifest()
        for line, raw, terminated, oversized in _records(self.events):
            if oversized:
                self.finding("error", "record_too_large", f"record exceeds {MAX_LINE_BYTES} bytes", line)
                continue
            try:
                event = _load_json(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError, RecursionError) as exc:
                if not terminated:
                    self.finding("warning", "incomplete_tail", "unparseable unterminated final record was left unread", line)
                else:
                    self.finding("error", "malformed_json", f"invalid JSONL record: {exc}", line)
                continue
            if not terminated:
                self.finding("warning", "missing_newline", "final JSON record has no newline terminator", line, incomplete=False)
            self.report["event_count"] += 1
            if not self.schema(event, line):
                continue
            session = self.report["session_id"]
            if session is None:
                self.report["session_id"] = event["session_id"]
            elif session != event["session_id"]:
                self.finding("error", "session_mismatch", "event belongs to a different session", line)
            self.sequence(event, line)
            self.lifecycle(event, line)
            self.check_attachments(event, line)
            self.summarize(event, line)
            if event["type"] == "trace.invalid_event":
                self.finding("warning", "invalid_native_event", "recorder preserved an invalid native event", line)
        for span in self.active.values():
            self.finding("warning", "unclosed_span", f"span has no observed terminal: {span['span_id']}")
        self.report["unclosed_spans"] = len(self.active) + self.lost_open_spans
        if self.lost_open_spans:
            self.finding("warning", "spans_before_gap", f"{self.lost_open_spans} open spans lost reliable association at a gap")
        if self.report["event_count"] == 0:
            self.finding("warning", "empty_recording", "no complete events were read")
        for relative, before in self.read_fingerprints.items():
            try:
                changed = _fingerprint(_safe_path(self.root, relative)) != before
            except (OSError, TraceError):
                changed = True
            if changed:
                self.finding("warning", "source_changed", f"source changed while being read: {relative}")
        return self.report

    def summary(self) -> dict[str, Any]:
        return {"session_id": self.report["session_id"], "event_count": self.report["event_count"],
                "event_types": dict(self.types), "outcomes": dict(self.outcomes), "runs": list(self.runs.values()),
                "gap_count": self.gap_count, "gaps": self.gaps,
                "unclosed_spans": self.report["unclosed_spans"],
                "unclosed_span_samples": list(self.active.values())[:MAX_SAMPLES],
                "artifact_count": len(self.artifacts), "validation": self.report}


def analyze(path: Path) -> Analysis:
    analysis = Analysis(path)
    analysis.run()
    return analysis


def _session_files(root: Path) -> list[tuple[str, Path]]:
    files: list[tuple[str, Path]] = []
    def inaccessible(error: OSError) -> None:
        raise TraceError(f"cannot enumerate session files: {error}")
    for directory, dirs, names in os.walk(root, followlinks=False, onerror=inaccessible):
        for name in sorted(dirs + names):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            safe = _safe_path(root, relative)
            mode = safe.lstat().st_mode
            if stat.S_ISREG(mode):
                files.append((relative, safe))
            elif not stat.S_ISDIR(mode):
                raise TraceError(f"session contains a non-regular file: {relative}")
    return sorted(files)


def export_session(path: Path, destination: Path, overwrite: bool = False) -> dict[str, Any]:
    analysis = analyze(path)
    root = analysis.root
    if analysis.unsafe_export:
        raise TraceError("unsafe export: session contains an unsafe artifact/config/manifest reference")
    destination = destination.expanduser().absolute()
    if destination.is_symlink():
        raise TraceError("export destination must not be a symbolic link")
    try:
        destination.resolve().relative_to(root)
        raise TraceError("export destination must be outside the session directory")
    except ValueError:
        pass
    if destination.exists() and not overwrite:
        raise TraceError(f"destination exists; use --overwrite to replace it: {destination}")
    def selected_files() -> list[tuple[str, Path]]:
        if path.expanduser().is_dir():
            return _session_files(root)
        # Selecting a single JSONL file does not authorize collecting unrelated
        # siblings (for example other files on a user's Desktop).
        names = {analysis.events.relative_to(root).as_posix(), *analysis.artifacts}
        if (root / "manifest.json").exists():
            names.add("manifest.json")
        return [(name, _safe_path(root, name)) for name in sorted(names)]
    files = selected_files()
    if any(relative == "export.json" for relative, _ in files):
        raise TraceError("session already contains export.json; choose the original session directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".maatrace-", suffix=".zip.tmp", dir=destination.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        metadata = {"format_version": "1", "tool": "MaaTrace CLI", "session_id": analysis.report["session_id"],
                    "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "validation": analysis.report, "files": []}
        snapshots: dict[str, tuple[int, int, int, int]] = {}
        with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
            for relative, file in files:
                file = _safe_path(root, relative)
                before = file.stat()
                snapshots[relative] = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                observed = analysis.read_fingerprints.get(relative)
                if observed is not None and observed != snapshots[relative]:
                    raise TraceError(f"source changed after validation; stop recording and retry: {relative}")
                digest, count = hashlib.sha256(), 0
                with file.open("rb") as source, archive.open(relative, "w", force_zip64=True) as target:
                    while chunk := source.read(CHUNK_SIZE):
                        digest.update(chunk)
                        count += len(chunk)
                        target.write(chunk)
                after = file.stat()
                if ((before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns) or count != before.st_size):
                    raise TraceError(f"session changed during export; stop recording and retry: {relative}")
                metadata["files"].append({"path": relative, "size": count, "sha256": digest.hexdigest()})
            archive.writestr("export.json", json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8") + b"\n")
        # Detect updates to an earlier file while later attachments were copied.
        # A recording should be stopped/flushed before creating a stable export.
        final_files = selected_files()
        if [name for name, _ in final_files] != [name for name, _ in files]:
            raise TraceError("session file list changed during export; stop recording and retry")
        for relative, file in final_files:
            current = file.stat()
            if snapshots[relative] != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns):
                raise TraceError(f"session changed during export; stop recording and retry: {relative}")
        if overwrite:
            os.replace(temp, destination)
        else:
            # Atomic creation without replacing a destination that appeared while
            # the archive was being assembled. Temp and destination share a dir.
            os.link(temp, destination)
            temp.unlink()
        return {"archive": str(destination), "files": metadata["files"], "validation": analysis.report}
    finally:
        temp.unlink(missing_ok=True)


def _print_validation(report: dict[str, Any]) -> None:
    label = "VALID" if report["valid"] else "INVALID"
    complete = "no known omissions" if report["complete"] else "incomplete or uncertain"
    print(f"{label}: {report['event_count']} events; {complete}")
    for severity in ("errors", "warnings"):
        for item in report[severity][:20]:
            line = f" line {item['line']}" if item["line"] is not None else ""
            print(f"  {severity[:-1].upper()}{line} [{item['code']}] {item['message']}")
        count = report[severity[:-1] + "_count"]
        if count > 20:
            print(f"  ... {count - 20} more {severity}; use --json for details (up to {MAX_FINDINGS})")


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"Session: {summary['session_id'] or 'unknown'}")
    print(f"Events: {summary['event_count']}; runs: {len(summary['runs'])}; artifacts: {summary['artifact_count']}")
    for run in summary["runs"]:
        print(f"\n{run['run_id']}: {run['entry'] or '(entry not observed)'} [{run['outcome']}]")
        route = " → ".join(str(entry["node_name"] or "?") for entry in run["route"])
        if route:
            print("  Observed action route: " + route)
            if run["route_total"] > len(run["route"]):
                print(f"  ... {run['route_total'] - len(run['route'])} more action entries")
        print(f"  Decisions: {run['decision_total']}; observed/inferred transitions: {run['transition_total']}")
        for decision in run["decisions"][:10]:
            flow = _dict(decision["flow"])
            selected = flow.get("selected_candidate_index")
            print(f"    seq {decision['seq']}: {decision['source_node'] or '?'} → candidate {selected if selected is not None else '?'} [{decision['outcome'] or 'unknown'}]")
    print()
    _print_validation(summary["validation"])


def main(argv: list[str] | None = None) -> int:
    # CLI output is UTF-8 on every platform, including redirected Windows stdout.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("validate", "inspect", "export"):
        command = commands.add_parser(name, help={"validate": "check event structure, sequence and evidence",
                                                  "inspect": "summarize tasks, action route and decisions",
                                                  "export": "archive an entire session with SHA256 checksums"}[name])
        command.add_argument("path", type=Path, help="session directory or events.jsonl")
        command.add_argument("--json", action="store_true", help="write a machine-readable JSON result")
        if name == "export":
            command.add_argument("-o", "--output", type=Path, required=True, help="ZIP file outside the session")
            command.add_argument("--overwrite", action="store_true", help="replace an existing output archive")
    args = parser.parse_args(argv)
    try:
        if args.command == "export":
            result = export_session(args.path, args.output, args.overwrite)
            if args.json:
                print(json.dumps(result, ensure_ascii=False, indent=2))
            else:
                print(f"Exported {len(result['files'])} files to {result['archive']}")
                _print_validation(result["validation"])
            return 0
        analysis = analyze(args.path)
        result = analysis.report if args.command == "validate" else analysis.summary()
        if args.json:
            print(json.dumps(result, ensure_ascii=False, indent=2))
        elif args.command == "validate":
            _print_validation(result)
        else:
            _print_summary(result)
        return 0 if analysis.report["valid"] else 1
    except BrokenPipeError:
        return 0
    except (OSError, TraceError, ValueError, RecursionError) as exc:
        print(f"maatrace: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
