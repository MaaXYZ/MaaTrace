#!/usr/bin/env python3
"""Black-box plugin tests. No Python packages beyond the standard library.

Each scenario runs in a separate process because plugin configuration and shutdown
are process scoped. The real SDK case loads the plugin through MaaGlobalLoadPlugin
and executes a pipeline; synthetic cases exercise the exported C ABI directly.
"""
from __future__ import annotations

import argparse
import ctypes as C
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib

SUCCESS = 3000
VOID = C.c_void_p
I64 = C.c_int64
BOOL = C.c_uint8
CALLBACK = C.CFUNCTYPE(None, VOID, C.c_char_p, C.c_char_p, VOID)
CUSTOM_ACTION = C.CFUNCTYPE(BOOL, VOID, I64, C.c_char_p, C.c_char_p,
                            C.c_char_p, I64, VOID, VOID)


def bind(lib, name, result, *args):
    fn = getattr(lib, name)
    fn.restype = result
    fn.argtypes = list(args)
    return fn


class Libraries:
    def __init__(self, plugin: Path, framework: Path):
        # Keep these directory handles alive for the duration of the process.
        self.dll_dirs = []
        if os.name == "nt":
            for folder in {plugin.parent, framework.parent}:
                self.dll_dirs.append(os.add_dll_directory(str(folder)))
        self.fw = C.CDLL(str(framework))
        bind(self.fw, "MaaGlobalSetOption", BOOL, C.c_int32, VOID, C.c_uint64)
        off = C.c_int32(0)
        self.fw.MaaGlobalSetOption(4, C.byref(off), C.sizeof(off))
        # MaaFramework keeps its logger open until process teardown. Windows
        # cannot remove that file while cleaning the per-scenario temp directory.
        log_root = Path(os.environ.get("MAA_TRACE_TEST_LOG_ROOT", tempfile.gettempdir()))
        log_path = os.fsencode(log_root / "maatrace-sdk-logs" / str(os.getpid()))
        self.fw.MaaGlobalSetOption(1, C.c_char_p(log_path), len(log_path))
        self.plugin = C.CDLL(str(plugin))
        bind(self.plugin, "GetApiVersion", C.c_uint32)
        bind(self.plugin, "GetPluginVersion", C.c_char_p)
        for name in ("OnTaskerEvent", "OnResourceEvent", "OnControllerEvent"):
            bind(self.plugin, name, None, VOID, C.c_char_p, C.c_char_p, VOID)
        bind(self.plugin, "MaaTraceFlush", C.c_int)
        bind(self.plugin, "MaaTraceShutdown", C.c_int)
        bind(self.plugin, "MaaTraceGetStatus", C.c_char_p)
        bind(self.fw, "MaaGlobalLoadPlugin", BOOL, C.c_char_p)
        bind(self.fw, "MaaTaskerCreate", VOID)
        bind(self.fw, "MaaTaskerDestroy", None, VOID)
        bind(self.fw, "MaaTaskerBindResource", BOOL, VOID, VOID)
        bind(self.fw, "MaaTaskerBindController", BOOL, VOID, VOID)
        bind(self.fw, "MaaTaskerInited", BOOL, VOID)
        bind(self.fw, "MaaTaskerPostTask", I64, VOID, C.c_char_p, C.c_char_p)
        bind(self.fw, "MaaTaskerWait", C.c_int32, VOID, I64)
        bind(self.fw, "MaaTaskerPostStop", I64, VOID)
        bind(self.fw, "MaaTaskerAddContextSink", I64, VOID, CALLBACK, VOID)
        bind(self.fw, "MaaTaskerRemoveContextSink", None, VOID, I64)
        bind(self.fw, "MaaResourceCreate", VOID)
        bind(self.fw, "MaaResourceDestroy", None, VOID)
        bind(self.fw, "MaaResourceSetOption", BOOL, VOID, C.c_int32, VOID, C.c_uint64)
        bind(self.fw, "MaaResourcePostBundle", I64, VOID, C.c_char_p)
        bind(self.fw, "MaaResourceWait", C.c_int32, VOID, I64)
        bind(self.fw, "MaaResourceRegisterCustomAction", BOOL, VOID, C.c_char_p, CUSTOM_ACTION, VOID)
        bind(self.fw, "MaaContextRunTask", I64, VOID, C.c_char_p, C.c_char_p)

    def emit(self, category, owner, message, details):
        payload = details if isinstance(details, bytes) else json.dumps(details, ensure_ascii=False).encode()
        getattr(self.plugin, "On" + category + "Event")(owner, message.encode(), payload, None)

    def status(self):
        return json.loads(self.plugin.MaaTraceGetStatus().decode())


def read_events(folder):
    files = list(folder.glob("session-*/events.jsonl"))
    assert len(files) == 1, f"expected one session, found {files}"
    content = files[0].read_bytes()
    assert content.endswith(b"\n"), "flush must leave a complete final JSONL line"
    records = [json.loads(line) for line in content.splitlines()]
    assert records, "recording is empty"
    seqs = [e["seq"] for e in records]
    assert all(isinstance(s, int) for s in seqs)
    assert seqs == sorted(set(seqs)), "event order is not strictly monotonic"
    for event in records:
        assert event["schema_version"] == "1.0"
        assert isinstance(event["session_id"], str)
        assert isinstance(event["timestamp"], str)
        assert isinstance(event["elapsed_us"], int)
        assert event["elapsed_us"] >= 0
        assert isinstance(event["type"], str)
        assert isinstance(event["data"], dict)
        assert event.get("task_id") is None or isinstance(event["task_id"], str)
    manifests = list(folder.glob("session-*/manifest.json"))
    assert len(manifests) == 1
    json.loads(manifests[0].read_text(encoding="utf-8"))
    return records


def native(event):
    return event.get("data", {}).get("native_details", {})


def message(event):
    return event.get("data", {}).get("native_message", "")


def basic(libs, output):
    p, fw = libs.plugin, libs.fw
    assert p.GetApiVersion() == 1
    assert p.GetPluginVersion()
    try:
        getattr(p, "OnContextEvent")
    except AttributeError:
        pass
    else:
        raise AssertionError("OnContextEvent must stay hidden to avoid the old loader's callback overwrite")

    owners = [fw.MaaTaskerCreate(), fw.MaaTaskerCreate()]
    assert all(owners)
    ids = [18446744073709551615, "task-with-string-id"]
    try:
        for owner, task in zip(owners, ids):
            libs.emit("Tasker", owner, "Tasker.Task.Starting", {"task_id": task, "entry": "synthetic"})
        # An unknown future framework event must survive schema normalization.
        for owner, task in zip(owners, ids):
            libs.emit("Tasker", owner, "Tasker.Future.Event", {
                "task_id": task, "unicode": "弹窗处理", "future": {"values": [1, True, None]},
            })
        libs.emit("Controller", 0x10, "Controller.Future.Event", b"{broken json")

        def producer(index):
            for counter in range(80):
                libs.emit("Controller", 0x20 + index, "Controller.Test.Event", {
                    "producer": index, "counter": counter,
                })

        workers = [threading.Thread(target=producer, args=(i,)) for i in range(4)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()
        for owner, task in zip(owners, ids):
            libs.emit("Tasker", owner, "Tasker.Task.Succeeded", {"task_id": task, "entry": "synthetic"})
        # A later task on the same Tasker must get a distinct run, even if its ID repeats.
        libs.emit("Tasker", owners[0], "Tasker.Task.Starting", {"task_id": ids[0], "entry": "again"})
        libs.emit("Tasker", owners[0], "Tasker.Task.Succeeded", {"task_id": ids[0], "entry": "again"})
        assert p.MaaTraceFlush() == 1
        events = read_events(output)
        starts = [e for e in events if e["type"] == "task.started"]
        assert len(starts) == 3
        assert len({e["run_id"] for e in starts}) == 3
        assert starts[0]["task_id"] == str(ids[0]), "64-bit task ID lost precision"
        future = [e for e in events if message(e) == "Tasker.Future.Event"]
        assert len(future) == 2
        assert native(future[0])["future"]["values"] == [1, True, None]
        assert native(future[0])["unicode"] == "弹窗处理"
        assert native(future[0])["task_id"] == ids[0]
        assert any(e["type"] == "trace.invalid_event" for e in events)
        emitted = [e for e in events if message(e) == "Controller.Test.Event"]
        assert len(emitted) == 320
        for i in range(4):
            assert [native(e)["counter"] for e in emitted if native(e)["producer"] == i] == list(range(80))
        assert libs.status()["dropped"] == 0
        assert libs.status()["recording_errors"] == 0
        assert p.MaaTraceShutdown() == 1
        assert p.MaaTraceShutdown() == 1, "shutdown must be idempotent"
        before = len(read_events(output))
        libs.emit("Controller", 0x10, "Controller.AfterShutdown", {})
        assert len(read_events(output)) == before, "shutdown must prevent new events"
    finally:
        for owner in owners:
            fw.MaaTaskerDestroy(owner)


def write_png(path):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress((b"\0" + b"\xff\xff\xff" * 2) * 2)) + chunk(b"IEND", b""))


def integration(libs, output, plugin_path):
    fw, p = libs.fw, libs.plugin
    assert fw.MaaGlobalLoadPlugin(os.fsencode(plugin_path)), "SDK rejected plugin ABI"
    root = output.parent / "resource"
    (root / "pipeline").mkdir(parents=True)
    direct = {"recognition": "DirectHit", "action": "DoNothing", "pre_delay": 0, "post_delay": 0}
    pipeline = {
        "A": dict(direct, next=["Miss", "B"]),
        "Miss": dict(direct, inverse=True),
        "B": direct,
        "Nested": dict(direct, action={"type": "Custom", "param": {"custom_action": "nested"}}),
        "Child": direct,
        "Slow": dict(direct, pre_delay=5000),
        "Broken": dict(direct, action={"type": "Custom", "param": {"custom_action": "missing"}}),
    }
    (root / "pipeline" / "test.json").write_text(json.dumps(pipeline), encoding="utf-8")
    resource = fw.MaaResourceCreate()
    tasker = fw.MaaTaskerCreate()
    controller = None
    assert resource and tasker
    callback_errors = []
    nested_ids = []

    @CUSTOM_ACTION
    def nested(ctx, _task, _node, _custom, _param, _reco, _box, _arg):
        try:
            nested_ids.append(fw.MaaContextRunTask(ctx, b"Child", b"{}"))
            return bool(nested_ids[-1])
        except Exception as exc:
            callback_errors.append(str(exc))
            return False

    started = threading.Event()
    observed_nodes = []

    @CALLBACK
    def observe(_ctx, msg, details, _arg):
        try:
            data = json.loads(details)
            if msg.startswith(b"Node.PipelineNode."):
                observed_nodes.append((msg.decode(), str(data.get("task_id"))))
            if msg == b"Node.PipelineNode.Starting" and data.get("name") == "Slow":
                started.set()
        except Exception as exc:
            callback_errors.append(str(exc))

    try:
        cpu = C.c_int32(1)
        assert fw.MaaResourceSetOption(resource, 2, C.byref(cpu), C.sizeof(cpu))
        resource_id = fw.MaaResourcePostBundle(resource, os.fsencode(root))
        assert resource_id and fw.MaaResourceWait(resource, resource_id) == SUCCESS
        assert fw.MaaTaskerBindResource(tasker, resource)
        if not fw.MaaTaskerInited(tasker):
            # Older SDKs require a connected controller even for DirectHit.
            images = output.parent / "images"
            images.mkdir()
            write_png(images / "screen.png")
            bind(fw, "MaaDbgControllerCreate", VOID, C.c_char_p)
            bind(fw, "MaaControllerPostConnection", I64, VOID)
            bind(fw, "MaaControllerWait", C.c_int32, VOID, I64)
            controller = fw.MaaDbgControllerCreate(os.fsencode(images))
            assert controller
            assert fw.MaaControllerWait(controller, fw.MaaControllerPostConnection(controller)) == SUCCESS
            assert fw.MaaTaskerBindController(tasker, controller)
        assert fw.MaaTaskerInited(tasker)
        assert fw.MaaResourceRegisterCustomAction(resource, b"nested", nested, None)
        top_ids = []
        for entry in (b"A", b"A", b"Nested"):
            task_id = fw.MaaTaskerPostTask(tasker, entry, b"{}")
            assert task_id
            assert fw.MaaTaskerWait(tasker, task_id) == SUCCESS, entry
            top_ids.append(str(task_id))
        failed_id = fw.MaaTaskerPostTask(tasker, b"Broken", b"{}")
        assert fw.MaaTaskerWait(tasker, failed_id) == 4000
        observer = fw.MaaTaskerAddContextSink(tasker, observe, None)
        slow_id = fw.MaaTaskerPostTask(tasker, b"Slow", b"{}")
        assert started.wait(5), "slow node did not start"
        stop_id = fw.MaaTaskerPostStop(tasker)
        fw.MaaTaskerWait(tasker, slow_id)
        fw.MaaTaskerWait(tasker, stop_id)
        fw.MaaTaskerRemoveContextSink(tasker, observer)
        assert not callback_errors, callback_errors
        assert len(nested_ids) == 1 and nested_ids[0]
        assert p.MaaTraceFlush() == 1
        events = read_events(output)
        starts = [e for e in events if e["type"] == "task.started"]
        ordinary_starts = [e for e in starts if native(e).get("entry") != "MaaTaskerPostStop"]
        assert len(ordinary_starts) == 5, f"unexpected top-level starts: {ordinary_starts}"
        assert len({e["run_id"] for e in starts}) == len(starts)
        for task_id in top_ids[:2]:
            task_events = [e for e in events if e.get("task_id") == task_id]
            assert any(e["type"] == "node.started" for e in task_events), "context callback subscription is missing"
            recos = [e for e in task_events if e["type"] == "recognition.finished"]
            assert [native(e)["name"] for e in recos] == ["A", "Miss", "B"], "missing or duplicate recognition events"
            assert [message(e).split(".")[-1] for e in recos] == ["Succeeded", "Failed", "Succeeded"]
            assert any(e["type"] == "action.finished" for e in task_events)
        nested_events = [e for e in events if e.get("task_id") == str(nested_ids[0])]
        assert nested_events, "nested task events missing"
        outer_run = next(e["run_id"] for e in starts if e["task_id"] == top_ids[2])
        assert all(e["run_id"] == outer_run for e in nested_events), "nested task must stay in the outer run"
        slow_events = [e for e in events if e.get("task_id") == str(slow_id)]
        assert any(e["type"] == "node.started" for e in slow_events)
        # Framework versions differ on cancellation terminals. Preserve exactly
        # what this SDK emitted, without synthesizing success/failure/cancellation.
        expected = [msg for msg, task in observed_nodes if task == str(slow_id)]
        actual = [message(e) for e in slow_events if e["type"] in ("node.started", "node.finished")]
        assert actual == expected, "recorder changed native cancellation events"
        assert any(message(e) == "Tasker.Task.Failed" and e.get("task_id") == str(failed_id) for e in events)
        assert libs.status()["dropped"] == 0
        assert p.MaaTraceShutdown() == 1
    finally:
        fw.MaaTaskerDestroy(tasker)
        fw.MaaResourceDestroy(resource)
        if controller:
            bind(fw, "MaaControllerDestroy", None, VOID)(controller)


def overflow(libs, output):
    # Guaranteed overload independent of scheduler speed: this one event exceeds
    # the configured queue byte budget. Following small events must still work.
    libs.emit("Controller", 0x10, "Controller.Oversized.Event", {"payload": "x" * 262144})
    for index in range(200):
        libs.emit("Controller", 0x10, "Controller.Small.Event", {"index": index})
    assert libs.plugin.MaaTraceFlush() == 1
    events = read_events(output)
    assert libs.status()["dropped"] >= 1, libs.status()
    assert any(e["type"] == "trace.gap" for e in events), "dropped data must leave an explicit gap"
    assert not any(message(e) == "Controller.Oversized.Event" for e in events)
    assert libs.plugin.MaaTraceShutdown() == 1


def file_limit(libs, output):
    for index in range(30):
        libs.emit("Controller", 0x10, "Controller.Small.Event", {"index": index})
    assert libs.plugin.MaaTraceFlush() == 0, "truncated trace cannot be reported complete"
    events = read_events(output)
    assert sum(e["type"] == "trace.truncated" for e in events) == 1
    assert libs.status()["truncated"] is True
    assert libs.status()["dropped"] > 0
    assert libs.plugin.MaaTraceShutdown() == 0


def io_failure(libs, output):
    libs.emit("Controller", 0x10, "Controller.Test.Event", {"value": 1})
    assert libs.plugin.MaaTraceFlush() == 0, "flush falsely reported persistence success"
    status = libs.status()
    assert status["recording_errors"] >= 1, status
    libs.emit("Controller", 0x10, "Controller.AfterFailure.Event", {})
    libs.plugin.MaaTraceShutdown()


def disabled(libs, output):
    libs.emit("Controller", 0x10, "Controller.Disabled.Event", {"value": 1})
    assert libs.plugin.MaaTraceFlush() == 1
    assert not output.exists(), "disabled recorder created output files"
    assert libs.status()["accepted"] == 0
    assert libs.status()["recording_errors"] == 0
    assert libs.plugin.MaaTraceShutdown() == 1


def run_case(args):
    with tempfile.TemporaryDirectory(prefix="maatrace-test-") as tmp:
        output = Path(tmp) / ("运行记录" if args.case == "unicode_path" else "traces")
        os.environ["MAA_TRACE_ENABLED"] = "0" if args.case == "disabled" else "1"
        if args.case in ("io_failure", "global_framework"):
            output.write_text("not a directory", encoding="utf-8")
        os.environ["MAA_TRACE_DIR"] = str(output)
        if args.case == "overflow":
            os.environ["MAA_TRACE_QUEUE_CAPACITY"] = "4"
            os.environ["MAA_TRACE_QUEUE_BYTES"] = "16384"
        else:
            os.environ["MAA_TRACE_QUEUE_CAPACITY"] = "8192"
            os.environ["MAA_TRACE_QUEUE_BYTES"] = "16777216"
        os.environ["MAA_TRACE_MAX_FILE_BYTES"] = "2048" if args.case == "file_limit" else "134217728"
        if args.case == "invalid_config":
            os.environ["MAA_TRACE_QUEUE_CAPACITY"] = "invalid"
        global_framework = None
        if args.case == "global_framework":
            # Mirrors a native host whose MaaFramework dependencies occupy the
            # global ELF symbol namespace. Plugin exceptions must remain isolated.
            global_framework = C.CDLL(str(args.framework), mode=C.RTLD_GLOBAL)
        libs = Libraries(args.plugin, args.framework)
        if args.case in ("global_framework", "invalid_config"):
            io_failure(libs, output)
        elif args.case == "unicode_path":
            basic(libs, output)
        elif args.case == "integration":
            integration(libs, output, args.plugin)
        else:
            globals()[args.case](libs, output)
    print(f"PASS {args.case}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--framework", type=Path, required=True)
    parser.add_argument("--case", choices=("basic", "integration", "overflow", "file_limit", "io_failure", "automatic_exit", "unicode_path", "invalid_config", "global_framework", "disabled"))
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    args.plugin = args.plugin.resolve()
    args.framework = args.framework.resolve()
    if args.case == "automatic_exit":
        os.environ["MAA_TRACE_DIR"] = str(args.output)
        libs = Libraries(args.plugin, args.framework)
        for index in range(200):
            libs.emit("Controller", 0x10, "Controller.Exit.Event", {"index": index})
        # Deliberately no Flush/Shutdown: POSIX normal process exit must drain.
        return
    if args.case:
        run_case(args)
        return
    cases = ["basic", "integration", "overflow", "file_limit", "io_failure", "unicode_path", "invalid_config", "disabled"]
    if os.name != "nt":
        cases.append("global_framework")
    for case in cases:
        result = subprocess.run([sys.executable, __file__, "--plugin", str(args.plugin),
                                 "--framework", str(args.framework), "--case", case],
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            raise SystemExit(f"FAIL {case} (exit {result.returncode})")
        print(f"PASS {case}")
    if os.name != "nt":
        with tempfile.TemporaryDirectory(prefix="maatrace-exit-test-") as tmp:
            output = Path(tmp) / "traces"
            result = subprocess.run([sys.executable, __file__, "--plugin", str(args.plugin),
                                     "--framework", str(args.framework), "--case", "automatic_exit",
                                     "--output", str(output)], capture_output=True, text=True, timeout=20)
            assert result.returncode == 0, result.stderr
            events = read_events(output)
            assert len(events) == 200, "normal process exit lost queued events"
            manifest = json.loads(next(output.glob("session-*/manifest.json")).read_text(encoding="utf-8"))
            assert manifest["state"] == "closed", manifest
        print("PASS automatic_exit")


if __name__ == "__main__":
    main()
