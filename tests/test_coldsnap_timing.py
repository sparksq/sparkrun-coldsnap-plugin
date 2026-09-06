# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

import hashlib
import json
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from sparkrun.core.timing import Timeline
from sparkrun.plugins.coldsnap.service import ColdSnapService
from sparkrun.plugins.coldsnap.timing import (
    OperationTimingEventStream,
    OperationTimingProgress,
    follow_operation_timing_events,
    import_operation_timing,
    read_operation_receipt,
)
from sparkrun.utils.cli_formatters import format_launch_timings


@pytest.mark.parametrize("operation", ["capture", "restore", "materialize"])
def test_cli_operation_timing_finishes_once(operation, monkeypatch, capsys):
    from sparkrun.plugins.coldsnap.cli import _begin_operation_timing

    calls = []
    timeline = SimpleNamespace(begin=lambda *args, **kwargs: "span", end=lambda *args, **kwargs: calls.append((args, kwargs)))
    sctx = SimpleNamespace(timing=timeline)
    monkeypatch.setattr("sparkrun.plugins.coldsnap.cli._format_timing_table", lambda _: "timing table")
    finish = _begin_operation_timing(sctx, operation, dry_run=False, show_timings=True)
    finish("error")
    finish()
    assert calls == [(("span",), {"status": "error"})]
    assert capsys.readouterr().out.count("timing table") == 1


def test_cli_dry_run_does_not_create_a_timeline(capsys):
    from sparkrun.plugins.coldsnap.cli import _begin_operation_timing

    sctx = SimpleNamespace()
    _begin_operation_timing(sctx, "capture", dry_run=True, show_timings=True)()
    assert not hasattr(sctx, "timing")
    assert capsys.readouterr().out == ""


def _request():
    return {
        "format": 4,
        "kind": "coldsnap-operation-request",
        "id": "operation-one",
        "operation": "restore",
        "snapshot_driver": {"id": "n610"},
        "launch": {
            "engine": "vllm",
            "units": [{"id": "unit-0", "host": "node-a"}],
            "execution": {"workers": [{"id": "worker-0", "unit": "unit-0"}]},
        },
    }


def _receipt(request=None):
    request = request or _request()
    return {
        "format": 2,
        "kind": "coldsnap-operation-receipt",
        "operation_id": request["id"],
        "operation": request["operation"],
        "state": "succeeded",
        "request_sha256": "sha256:" + "a" * 64,
        "engine": request["launch"]["engine"],
        "snapshot_driver": request["snapshot_driver"]["id"],
        "duration_seconds": 12.0,
        "timing": {
            "format": 1,
            "clocks": [
                {"id": "controller", "source": "controller", "origin_unix_ns": 1_800_000_000_000_000_000},
                {
                    "id": "unit-unit-0",
                    "source": "runtime-unit",
                    "origin_unix_ns": 1_800_000_001_000_000_000,
                    "unit": "unit-0",
                },
            ],
            "spans": [
                {
                    "id": "controller-1",
                    "name": "controller.restore",
                    "clock": "controller",
                    "start_offset_seconds": 0.0,
                    "duration_seconds": 12.0,
                    "status": "ok",
                },
                {
                    "id": "runtime-unit-0",
                    "parent": "controller-1",
                    "name": "runtime.restore",
                    "clock": "unit-unit-0",
                    "start_offset_seconds": 0.0,
                    "duration_seconds": 10.0,
                    "status": "ok",
                    "attributes": {"unit": "unit-0"},
                },
            ],
        },
    }


def _timing_events(receipt):
    base = {
        "format": 1,
        "kind": "coldsnap-timing-event",
        "operation_id": receipt["operation_id"],
        "request_sha256": receipt["request_sha256"],
    }
    timing_payload = json.dumps(
        receipt["timing"],
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    timing_sha = "sha256:" + hashlib.sha256(timing_payload).hexdigest()
    root = receipt["timing"]["spans"][0]
    child = receipt["timing"]["spans"][1]
    start = {key: value for key, value in root.items() if key not in {"duration_seconds", "status"}}
    return [
        {**base, "sequence": 1, "event": "stream_start"},
        {**base, "sequence": 2, "event": "clock", "clock": receipt["timing"]["clocks"][0]},
        {**base, "sequence": 3, "event": "clock", "clock": receipt["timing"]["clocks"][1]},
        {**base, "sequence": 4, "event": "span_start", "span": start},
        {**base, "sequence": 5, "event": "span_complete", "span": child},
        {**base, "sequence": 6, "event": "span_complete", "span": root},
        {
            **base,
            "sequence": 7,
            "event": "stream_complete",
            "state": receipt["state"],
            "timing_sha256": timing_sha,
        },
    ]


def test_timing_event_stream_validates_incrementally_and_binds_final_receipt():
    request = _request()
    receipt = _receipt(request)
    seen = []
    stream = OperationTimingEventStream(request, on_event=seen.append)
    for event in _timing_events(receipt):
        stream.accept(json.dumps(event, separators=(",", ":")))
    stream.finish(receipt)
    assert stream.completed is True
    assert len(seen) == 7


def test_timing_event_stream_rejects_sequence_gaps():
    request = _request()
    events = _timing_events(_receipt(request))
    events[1]["sequence"] = 3
    stream = OperationTimingEventStream(request)
    stream.accept(json.dumps(events[0]))
    with pytest.raises(RuntimeError, match="ordering"):
        stream.accept(json.dumps(events[1]))


def test_timing_event_stream_allows_bounded_error_detail_on_completion():
    request = _request()
    events = _timing_events(_receipt(request))
    events[5]["span"] = {
        **events[5]["span"],
        "status": "error",
        "attributes": {"error": "adapter failed"},
    }
    events[6]["state"] = "failed"
    stream = OperationTimingEventStream(request)
    for event in events:
        stream.accept(json.dumps(event))
    assert stream.completed is True


def test_timing_event_file_is_followed_before_the_operation_completes(tmp_path):
    request = _request()
    receipt = _receipt(request)
    events = _timing_events(receipt)
    path = tmp_path / "timing.ndjson"
    stop = threading.Event()
    seen = []
    streams = []

    thread = threading.Thread(
        target=lambda: streams.append(follow_operation_timing_events(path, request, stop, on_event=seen.append)),
        daemon=True,
    )
    thread.start()
    with path.open("w", encoding="utf-8") as output:
        for event in events[:4]:
            output.write(json.dumps(event) + "\n")
        output.flush()
        deadline = time.monotonic() + 1.0
        while len(seen) < 4 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(seen) == 4
        assert stop.is_set() is False
        for event in events[4:]:
            output.write(json.dumps(event) + "\n")
        output.flush()
    stop.set()
    thread.join(timeout=1.0)
    assert thread.is_alive() is False
    streams[0].finish(receipt)


def test_timing_progress_describes_parallel_capsule_pulls_by_node():
    request = _request()
    request["launch"]["units"].append({"id": "unit-1", "host": "node-b"})
    progress = OperationTimingProgress(request)
    base = {
        "event": "span_start",
        "span": {
            "clock": "controller",
            "start_offset_seconds": 0.0,
            "attributes": {},
        },
    }
    aggregate = {
        **base,
        "span": {**base["span"], "id": "span-1", "name": "capsules.prepare"},
    }
    assert progress.accept(aggregate) == ("ColdSnap: preparing capsule images on node-a, node-b", True)
    for index, host in enumerate(("node-a", "node-b")):
        event = {
            **base,
            "span": {
                **base["span"],
                "id": f"span-{index + 2}",
                "parent": "span-1",
                "name": "capsule.pull",
                "attributes": {"unit": f"unit-{index}", "host": host},
            },
        }
        assert progress.accept(event) == (f"ColdSnap: pulling capsule for unit-{index} on {host}", True)

    assert progress.heartbeat_label("ColdSnap [n580]: restore prepare-only") == (
        "ColdSnap [n580]: restore prepare-only — pulling capsules on node-a, node-b"
    )


@pytest.mark.parametrize(
    "mode,materialize,phrase",
    [
        ("native", "required", "configuring native-weight caching for restore"),
        ("recovery", "off", "configuring native-weight caching for restore"),
        ("recovery", "async", "configuring native-weight caching for restore"),
        ("auto", None, "configuring native-weight caching for restore"),
        ("recovery", "required", "configuring required native-weight generation"),
    ],
)
def test_native_cache_progress_does_not_claim_a_dedicated_materialize(mode, materialize, phrase):
    request = _request()
    request["policy"] = {"weights": {"mode": mode, "native": {"materialize": materialize}}}
    progress = OperationTimingProgress(request)
    span = {"id": "prepare", "name": "materialization.prepare", "clock": "controller", "attributes": {}}
    assert progress.accept({"event": "span_start", "span": span}) == ("ColdSnap: " + phrase + " on node-a", True)
    assert progress.heartbeat_label("restore") == "restore — " + phrase + " on node-a"
    assert progress.accept({"event": "span_complete", "span": {**span, "duration_seconds": 2.5, "status": "ok"}}) == (
        "ColdSnap: " + phrase + " on node-a finished (2.5s)",
        True,
    )


def test_receipt_timing_imports_as_nested_foreign_clock_spans(tmp_path):
    request = _request()
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(_receipt(request)), encoding="utf-8")
    receipt = read_operation_receipt(path, request, 0)
    timeline = Timeline(wall_origin=1_800_000_000.0)
    parent = timeline.begin("coldsnap.controller")
    assert import_operation_timing(timeline, receipt, parent) == 2
    timeline.end(parent)

    exported = timeline.export()
    spans = {span["name"]: span for span in exported["spans"]}
    assert spans["coldsnap.controller.restore"]["parent"] == parent
    assert spans["coldsnap.runtime.restore"]["parent"] == spans["coldsnap.controller.restore"]["id"]
    assert spans["coldsnap.runtime.restore"]["clock"] == "coldsnap:runtime-unit:unit-0"
    rendered = format_launch_timings(exported)
    assert "coldsnap.runtime.restore" in rendered
    assert "[coldsnap:runtime-unit:unit-0]" in rendered


def test_receipt_rejects_pre_timing_format(tmp_path):
    request = _request()
    receipt = _receipt(request)
    receipt["format"] = 1
    receipt.pop("timing")
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(RuntimeError, match="format is unsupported"):
        read_operation_receipt(path, request, 0)


def test_timing_table_labels_non_additive_cumulative_service_counters():
    rendered = format_launch_timings(
        {
            "duration_s": 4.0,
            "spans": [
                {
                    "id": 1,
                    "name": "coldsnap.runtime.hydration.model_payload.io_service_total",
                    "parent": None,
                    "t_start": 0.0,
                    "duration_s": 3.7,
                    "status": "ok",
                    "clock": "coldsnap:runtime-worker:worker-0",
                    "attrs": {
                        "composition": "non_additive",
                        "timing_semantics": "cumulative_service_time",
                    },
                }
            ],
        },
        title="ColdSnap timings",
    )
    assert "3.7s" in rendered
    assert "[cumulative service time; non-additive]" in rendered


def test_receipt_timing_rejects_cycles(tmp_path):
    request = _request()
    receipt = _receipt(request)
    receipt["timing"]["spans"][0]["parent"] = "runtime-unit-0"
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(RuntimeError, match="cycle"):
        read_operation_receipt(path, request, 0)


def test_service_requests_receipt_and_imports_controller_timing(tmp_path):
    request = _request()
    timeline = Timeline(wall_origin=1_800_000_000.0)
    sctx = SimpleNamespace(timing=timeline)

    def run_command(arguments, **_kwargs):
        receipt_path = Path(arguments[arguments.index("--receipt-json") + 1])
        receipt = _receipt(request)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        event_path = Path(arguments[arguments.index("--timing-events") + 1])
        event_path.write_text("".join(json.dumps(event) + "\n" for event in _timing_events(receipt)), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = ColdSnapService("coldsnap", run_command=run_command)._invoke(
        request,
        prepare_only=False,
        capture_output=True,
        sctx=sctx,
        cluster=None,
    )
    assert result.returncode == 0
    spans = {span["name"]: span for span in timeline.export()["spans"]}
    assert "coldsnap.controller" in spans
    assert spans["coldsnap.controller.restore"]["parent"] == spans["coldsnap.controller"]["id"]


def test_service_streams_progress_events_without_timing_table():
    request = _request()
    sctx = SimpleNamespace(timing=None)
    observed = []

    def run_command(arguments, **_kwargs):
        observed.extend(arguments)
        receipt_path = Path(arguments[arguments.index("--receipt-json") + 1])
        receipt = _receipt(request)
        receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
        event_path = Path(arguments[arguments.index("--timing-events") + 1])
        event_path.write_text("".join(json.dumps(event) + "\n" for event in _timing_events(receipt)), encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    result = ColdSnapService("coldsnap", run_command=run_command)._invoke(
        request,
        prepare_only=False,
        capture_output=True,
        sctx=sctx,
        cluster=None,
    )

    assert result.returncode == 0
    assert "--timing-events" in observed
