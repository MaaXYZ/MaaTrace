#!/usr/bin/env python3
"""Exercise optional runtime observation against a real recent MaaFramework SDK.

The released minimum SDK remains supported; it lacks these exports and skips this
suite. Upstream SDK builds exercise native relations, snapshots and plugin hooks.
"""
from __future__ import annotations

import argparse
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from test_plugin import BOOL, CUSTOM_ACTION, I64, SUCCESS, VOID, Libraries, bind, message, native, read_events, write_png
from test_evidence import cached_image, setup_api


def recent_sdk(fw):
    return all(hasattr(fw, name) for name in (
        "MaaGlobalFlushPlugins", "MaaGlobalShutdownPlugins", "MaaTaskerGetRecognitionImageStatus"))


def option(fw, key, value):
    assert fw.MaaGlobalSetOption(key, C.byref(value), C.sizeof(value)), key


def load_resource(fw, root, pipeline):
    bundle = root / "resource"
    (bundle / "pipeline").mkdir(parents=True)
    (bundle / "pipeline" / "test.json").write_text(json.dumps(pipeline), encoding="utf-8")
    resource = fw.MaaResourceCreate()
    assert resource
    try:
        cpu = C.c_int32(1)
        assert fw.MaaResourceSetOption(resource, 2, C.byref(cpu), C.sizeof(cpu))
        operation = fw.MaaResourcePostBundle(resource, os.fsencode(bundle))
        assert operation and fw.MaaResourceWait(resource, operation) == SUCCESS
        return resource
    except BaseException:
        fw.MaaResourceDestroy(resource)
        raise


def close_framework(libs):
    flush = bind(libs.fw, "MaaGlobalFlushPlugins", BOOL)
    shutdown = bind(libs.fw, "MaaGlobalShutdownPlugins", BOOL)
    assert flush()
    assert shutdown()
    assert shutdown(), "framework plugin shutdown must be idempotent"
    assert libs.status()["state"] == "stopped", "framework hook did not shut down MaaTrace"


def assert_closed(libs, output):
    assert libs.status()["accepted"] == libs.status()["written"]
    assert libs.status()["dropped"] == libs.status()["recording_errors"] == 0
    session = next(output.glob("session-*"))
    manifest = json.loads((session / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "closed", manifest
    assert manifest["recording_complete"], manifest
    return session


def flow_case(libs, root, output, plugin_path):
    fw = libs.fw
    option(fw, 13, BOOL(True))  # CaptureConfig
    assert fw.MaaGlobalLoadPlugin(os.fsencode(plugin_path))
    direct = {"recognition": "DirectHit", "action": "DoNothing", "pre_delay": 0, "post_delay": 0}
    pipeline = {
        "Outer": dict(direct, action={"type": "Custom", "param": {"custom_action": "nested"}}),
        "Child": direct,
        "Broken": dict(direct, action={"type": "Custom", "param": {"custom_action": "fail"}}, on_error=["Recovery"]),
        "Recovery": direct,
        "BeforeJump": dict(direct, next=[{"name": "Handler", "jump_back": True}, "AfterJump"]),
        "Handler": dict(direct, max_hit=1),
        "AfterJump": direct,
    }
    resource = load_resource(fw, root, pipeline)
    tasker = fw.MaaTaskerCreate()
    assert tasker
    errors, children = [], []
    override = bind(fw, "MaaContextOverridePipeline", BOOL, VOID, C.c_char_p)

    @CUSTOM_ACTION
    def nested(ctx, _task, _node, _custom, _param, _reco, _box, _arg):
        try:
            # Mutate the live Context after this action's snapshot was created.
            # Both event references must still describe the operation's old data.
            assert override(ctx, b'{"Outer":{"post_delay":17}}')
            child = fw.MaaContextRunTask(ctx, b"Child", b"{}")
            children.append(str(child))
            return bool(child)
        except BaseException as exc:
            errors.append(repr(exc))
            return False

    @CUSTOM_ACTION
    def fail(*_args):
        return False

    try:
        assert fw.MaaTaskerBindResource(tasker, resource)
        assert fw.MaaResourceRegisterCustomAction(resource, b"nested", nested, None)
        assert fw.MaaResourceRegisterCustomAction(resource, b"fail", fail, None)
        for entry in (b"Outer", b"Broken", b"BeforeJump"):
            operation = fw.MaaTaskerPostTask(tasker, entry, b"{}")
            assert operation and fw.MaaTaskerWait(tasker, operation) == SUCCESS, entry
        assert not errors, errors
        assert len(children) == 1
        assert bind(fw, "MaaGlobalFlushPlugins", BOOL)()
        events = read_events(output)
        subtask = next(event for event in events if event["type"] == "subtask.started")
        child_end = next(event for event in events if event["type"] == "subtask.finished")
        outer = next(event for event in events if event["type"] == "action.started" and native(event).get("name") == "Outer")
        assert subtask["task_id"] == child_end["task_id"] == children[0]
        assert subtask["span_id"] == child_end["span_id"]
        assert subtask["parent_span_id"] == child_end["parent_span_id"] == outer["span_id"]
        assert subtask["run_id"] == outer["run_id"]
        assert subtask["data"]["flow"]["parent_source"] == "native_event"
        child_node = next(event for event in events if event["type"] == "node.started" and event["task_id"] == children[0])
        assert child_node["parent_span_id"] == subtask["span_id"]

        decisions = [event for event in events if event["type"] == "decision.finished"]
        assert decisions
        for event in decisions:
            data, detail = event["data"], native(event)
            flow = data["flow"]
            assert event["span_id"] == f'{event["run_id"]}:decision:{detail["list_id"]}'
            assert flow["round"] == detail["round"]
            assert flow["selected_candidate_index"] == detail["selected_index"]
            assert flow["selected_recognition_span_id"] == f'{event["run_id"]}:recognition:{detail["reco_id"]}'
            assert flow["selection_source"] == "native_event"

        transitions = [event for event in events if event["type"] == "node.transition"]
        assert {native(event)["reason"] for event in transitions} >= {"entry", "next", "on_error", "jump_back"}
        for event in transitions:
            flow = event["data"]["flow"]["transition"]
            assert flow["provenance"] == "observed"
            assert flow["reason"] == native(event)["reason"]
            assert flow["to_span_id"] == f'{event["run_id"]}:node:{native(event)["to_node_id"]}'
        resumed = next(event for event in transitions if native(event)["reason"] == "jump_back")
        assert native(resumed)["to"] == "BeforeJump"
        handler_actions = [event for event in events if event["type"] == "action.started" and native(event).get("name") == "Handler"]
        assert len(handler_actions) == 1, "resuming a predecessor does not repeat the handler action"

        snapshots = [event for event in events if event["type"] in ("recognition.started", "recognition.finished", "action.started", "action.finished")]
        assert snapshots
        by_span = {}
        session = next(output.glob("session-*"))
        for event in snapshots:
            reference = event["data"]["config"]
            assert reference["availability"] == "available"
            payload = (session / reference["path"]).read_bytes()
            assert hashlib.sha256(payload).hexdigest() == reference["sha256"]
            assert len(payload) == reference["size"]
            assert json.loads(payload) == native(event)["pipeline"]
            by_span.setdefault(event["span_id"], []).append(reference["sha256"])
        assert all(len(values) == 2 and values[0] == values[1] for values in by_span.values())
        outer_end = next(event for event in snapshots if event["span_id"] == outer["span_id"] and event["type"] == "action.finished")
        assert native(outer_end)["pipeline"]["post_delay"] == 0, "live override replaced the operation's actual snapshot"
        assert not bind(fw, "MaaGlobalShutdownPlugins", BOOL)(), "shutdown must refuse live objects"
        assert libs.status()["state"] == "recording"
    finally:
        fw.MaaTaskerDestroy(tasker)
        fw.MaaResourceDestroy(resource)
        close_framework(libs)
    assert_closed(libs, output)
    events = read_events(output)
    assert any(message(event) == "Tasker.Destroying" for event in events)
    assert any(message(event) == "Resource.Destroying" for event in events)


def image_case(libs, root, output, plugin_path, case):
    fw = libs.fw
    setup_api(fw)
    option(fw, 6, BOOL(False))  # DebugMode
    option(fw, 10, BOOL(case != "not_captured"))  # RecoImageRaw
    option(fw, 11, BOOL(False))  # RecoImageDraws, including Debug builds
    if case == "budget_exceeded":
        option(fw, 12, C.c_uint64(1))
    assert fw.MaaGlobalLoadPlugin(os.fsencode(plugin_path))
    resource = load_resource(fw, root, {"Seed": {"recognition": "DirectHit"}})
    tasker = fw.MaaTaskerCreate()
    image = fw.MaaImageBufferCreate()
    assert tasker and image
    try:
        assert fw.MaaTaskerBindResource(tasker, resource)
        image_path = root / "input.png"
        write_png(image_path)
        encoded = image_path.read_bytes()
        memory = C.create_string_buffer(encoded)
        assert fw.MaaImageBufferSetEncoded(image, memory, len(encoded))
        operation = fw.MaaTaskerPostRecognition(tasker, b"DirectHit", b"{}", image)
        assert operation and fw.MaaTaskerWait(tasker, operation) == SUCCESS
        assert bind(fw, "MaaGlobalFlushPlugins", BOOL)()
        events = read_events(output)
        event = next(event for event in events if event["type"] == "recognition.finished")
        status = event["data"]["enrichment"]["image"]
        expected = "available" if case == "raw" else case
        assert native(event)["reco_details"]["image_status"] == expected
        assert status["native_image_status"] == expected, status
        if case == "raw":
            assert status["availability"] == "captured", status
            artifact, = event["data"]["artifacts"]
            session = next(output.glob("session-*"))
            payload = (session / artifact["path"]).read_bytes()
            assert payload == cached_image(fw, tasker, native(event)["reco_id"])
            assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]
        else:
            assert status["reason"] == expected, status
            assert status["current_image_status"] == expected, status
            assert not event["data"].get("artifacts")
    finally:
        fw.MaaImageBufferDestroy(image)
        fw.MaaTaskerDestroy(tasker)
        fw.MaaResourceDestroy(resource)
        close_framework(libs)
    assert_closed(libs, output)


def execute(args):
    with tempfile.TemporaryDirectory(prefix="maatrace-native-") as directory:
        root = Path(directory)
        output = root / "traces"
        os.environ.update(MAA_TRACE_DIR=str(output), MAA_TRACE_ENABLED="1", MAA_TRACE_IMAGES="all",
                          MAA_TRACE_QUEUE_CAPACITY="8192", MAA_TRACE_QUEUE_BYTES="16777216",
                          MAA_TRACE_MAX_FILE_BYTES="134217728", MAA_TRACE_MAX_CONFIG_BYTES="16777216",
                          MAA_TRACE_MAX_IMAGE_BYTES="8388608", MAA_TRACE_MAX_ARTIFACT_BYTES="268435456")
        libs = Libraries(args.plugin, args.framework)
        if not recent_sdk(libs.fw):
            if args.require_native:
                raise SystemExit("SDK does not provide the required native runtime observation APIs")
            print("SKIP native runtime observation: SDK does not provide the optional APIs")
            return
        try:
            if args.case == "flow":
                flow_case(libs, root, output, args.plugin)
            else:
                image_case(libs, root, output, args.plugin, args.case)
        finally:
            libs.plugin.MaaTraceShutdown()
    print("PASS native runtime", args.case)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--framework", type=Path, required=True)
    parser.add_argument("--case", choices=("flow", "raw", "not_captured", "budget_exceeded"))
    parser.add_argument("--require-native", action="store_true", help="fail instead of skipping on an older SDK")
    args = parser.parse_args()
    args.plugin, args.framework = args.plugin.resolve(), args.framework.resolve()
    if args.case:
        execute(args)
        return
    for case in ("flow", "raw", "not_captured", "budget_exceeded"):
        command = [sys.executable, __file__, "--plugin", str(args.plugin),
                   "--framework", str(args.framework), "--case", case]
        if args.require_native:
            command.append("--require-native")
        result = subprocess.run(command,
                                capture_output=True, text=True, timeout=90)
        if result.returncode:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            raise SystemExit(f"FAIL native runtime {case}: {result.returncode}")
        print(result.stdout.strip())


if __name__ == "__main__":
    main()
