#!/usr/bin/env python3
"""Exercise evidence collection against the real MaaFramework C ABI.

The recorder must preserve the recognition's cached input, not recapture a frame.
No third-party Python packages are required.
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

from test_plugin import Libraries, bind, VOID, I64, BOOL, SUCCESS, read_events, write_png


def setup_api(fw):
    bind(fw, "MaaTaskerPostRecognition", I64, VOID, C.c_char_p, C.c_char_p, VOID)
    bind(fw, "MaaTaskerGetRecognitionDetail", BOOL, VOID, I64, VOID, VOID,
         C.POINTER(BOOL), VOID, VOID, VOID, VOID)
    bind(fw, "MaaImageBufferCreate", VOID)
    bind(fw, "MaaImageBufferDestroy", None, VOID)
    bind(fw, "MaaImageBufferSetEncoded", BOOL, VOID, VOID, C.c_uint64)
    bind(fw, "MaaImageBufferGetEncoded", VOID, VOID)
    bind(fw, "MaaImageBufferGetEncodedSize", C.c_uint64, VOID)
    bind(fw, "MaaStringBufferCreate", VOID)
    bind(fw, "MaaStringBufferDestroy", None, VOID)


def cached_image(fw, tasker, reco_id):
    strings = [fw.MaaStringBufferCreate() for _ in range(3)]
    raw = fw.MaaImageBufferCreate()
    hit = BOOL()
    box = (C.c_int32 * 4)()
    try:
        assert fw.MaaTaskerGetRecognitionDetail(tasker, int(reco_id), strings[0], strings[1],
                                               C.byref(hit), box, strings[2], raw, None)
        size = fw.MaaImageBufferGetEncodedSize(raw)
        data = fw.MaaImageBufferGetEncoded(raw)
        return C.string_at(data, size) if size else b""
    finally:
        fw.MaaImageBufferDestroy(raw)
        for handle in strings:
            fw.MaaStringBufferDestroy(handle)


def execute_case(args):
    with tempfile.TemporaryDirectory(prefix="maatrace-evidence-") as directory:
        root = Path(directory)
        output = root / "traces"
        os.environ["MAA_TRACE_DIR"] = str(output)
        os.environ["MAA_TRACE_ENABLED"] = "1"
        os.environ["MAA_TRACE_IMAGES"] = {"none": "none", "failed": "failed"}.get(args.case, "all")
        os.environ["MAA_TRACE_MAX_IMAGE_BYTES"] = "1" if args.case == "image_limit" else "8388608"
        os.environ["MAA_TRACE_MAX_ARTIFACT_BYTES"] = "1" if args.case == "artifact_limit" else "268435456"
        os.environ["MAA_TRACE_QUEUE_BYTES"] = "16777216"
        libs = Libraries(args.plugin, args.framework)
        fw, plugin = libs.fw, libs.plugin
        setup_api(fw)
        debug = BOOL(args.case != "no_debug")
        assert fw.MaaGlobalSetOption(6, C.byref(debug), C.sizeof(debug))
        assert fw.MaaGlobalLoadPlugin(os.fsencode(args.plugin))
        bundle = root / "resource"
        (bundle / "pipeline").mkdir(parents=True)
        (bundle / "pipeline" / "test.json").write_text('{"Seed":{"recognition":"DirectHit"}}', encoding="utf-8")
        image_path = root / "input.png"
        write_png(image_path)
        image_bytes = image_path.read_bytes()
        buffer = C.create_string_buffer(image_bytes)
        image = fw.MaaImageBufferCreate()
        resource = fw.MaaResourceCreate()
        tasker = fw.MaaTaskerCreate()
        assert image and resource and tasker
        try:
            cpu = C.c_int32(1)
            assert fw.MaaResourceSetOption(resource, 2, C.byref(cpu), C.sizeof(cpu))
            res_id = fw.MaaResourcePostBundle(resource, os.fsencode(bundle))
            assert fw.MaaResourceWait(resource, res_id) == SUCCESS
            assert fw.MaaTaskerBindResource(tasker, resource)
            assert fw.MaaImageBufferSetEncoded(image, buffer, len(image_bytes))
            # The same fixed white image has one hit and one genuine miss.
            for algorithm, parameters in ((b"DirectHit", {}), (b"ColorMatch", {
                    "lower": [0, 0, 0], "upper": [0, 0, 0], "count": 1})):
                task_id = fw.MaaTaskerPostRecognition(tasker, algorithm, json.dumps(parameters).encode(), image)
                assert task_id
                assert fw.MaaTaskerWait(tasker, task_id) == SUCCESS
            assert plugin.MaaTraceFlush() == 1
            events = read_events(output)
            recos = [event for event in events if event["type"] == "recognition.finished"]
            assert len(recos) == 2, recos
            assert [e["data"]["native_status"] for e in recos] == ["Succeeded", "Failed"]
            statuses = [e["data"]["enrichment"]["image"] for e in recos]
            artifacts = [a for event in recos for a in event["data"].get("artifacts", [])]
            available = [a for a in artifacts if a["availability"] == "available"]
            if args.case == "none":
                assert all(s["availability"] == "disabled" for s in statuses)
                assert not artifacts
            elif args.case == "no_debug":
                assert all(s["reason"] in ("debug_required_or_not_retained", "not_captured") for s in statuses)
                assert not artifacts
            elif args.case == "image_limit":
                assert all(s["reason"] == "image_size_limit" for s in statuses)
                assert not artifacts
            elif args.case == "artifact_limit":
                assert all(s["availability"] == "captured" for s in statuses)
                assert len(artifacts) == 2
                assert all(a["availability"] == "omitted" and a["reason"] == "artifact_budget" for a in artifacts)
            else:
                assert len(available) == (1 if args.case == "failed" else 2), artifacts
                if args.case == "failed":
                    assert statuses[0]["reason"] == "policy_failed_only"
                    assert statuses[1]["availability"] == "captured"
                session = next(output.glob("session-*"))
                for event in recos:
                    for artifact in event["data"].get("artifacts", []):
                        if artifact["availability"] != "available":
                            continue
                        reference = Path(artifact["path"])
                        assert not reference.is_absolute() and ".." not in reference.parts
                        payload = (session / reference).read_bytes()
                        assert payload.startswith(b"\x89PNG\r\n\x1a\n")
                        assert len(payload) == artifact["size"]
                        assert hashlib.sha256(payload).hexdigest() == artifact["sha256"]
                        native_id = event["data"]["native_ids"]["reco_id"]
                        assert payload == cached_image(fw, tasker, native_id), "image was not the actual cached recognition input"
                        assert artifact["metadata"]["width"] == artifact["metadata"]["height"] == 2
                        assert artifact["metadata"]["coordinate_space"] == "recognition_image"
            assert plugin.MaaTraceShutdown() == 1
            assert libs.status()["recording_errors"] == 0
        finally:
            # Shutdown remains safe on assertion failures before Python's temp
            # directory disappears and before framework resources are destroyed.
            plugin.MaaTraceShutdown()
            fw.MaaTaskerDestroy(tasker)
            fw.MaaResourceDestroy(resource)
            fw.MaaImageBufferDestroy(image)
    print("PASS evidence", args.case)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin", type=Path, required=True)
    parser.add_argument("--framework", type=Path, required=True)
    parser.add_argument("--case", choices=("none", "no_debug", "all", "failed", "image_limit", "artifact_limit"))
    args = parser.parse_args()
    args.plugin, args.framework = args.plugin.resolve(), args.framework.resolve()
    if args.case:
        execute_case(args)
        return
    for case in ("none", "no_debug", "all", "failed", "image_limit", "artifact_limit"):
        result = subprocess.run([sys.executable, __file__, "--plugin", str(args.plugin),
                                 "--framework", str(args.framework), "--case", case],
                                capture_output=True, text=True, timeout=45)
        if result.returncode:
            print(result.stdout)
            print(result.stderr, file=sys.stderr)
            raise SystemExit(f"FAIL evidence {case}: {result.returncode}")
        print("PASS evidence", case)


if __name__ == "__main__":
    main()
