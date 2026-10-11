# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.
"""Public feedback from ColdSnap's alternative controller execution pipeline."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sparkrun.core.operation import operation_scope
from sparkrun.plugins.coldsnap.service import ColdSnapService
from test_coldsnap_timing import _request, _receipt, _timing_events


@pytest.mark.parametrize("returncode", [0, 1])
def test_controller_spans_are_structured_and_terminal_outcome_is_verified(returncode, caplog):
    caplog.set_level(25)
    events = []
    request = _request()
    sctx = SimpleNamespace(timing=None, events=events.append, operation=None)

    def run_command(arguments, **_kwargs):
        receipt = _receipt(request)
        if returncode:
            receipt["state"] = "failed"
            receipt["timing"]["spans"][0]["status"] = "error"
        Path(arguments[arguments.index("--receipt-json") + 1]).write_text(json.dumps(receipt))
        Path(arguments[arguments.index("--timing-events") + 1]).write_text("".join(json.dumps(v) + "\n" for v in _timing_events(receipt)))
        return SimpleNamespace(returncode=returncode, stdout="", stderr="failure" if returncode else "")

    with operation_scope(sctx):
        service = ColdSnapService("coldsnap", run_command=run_command)
        if returncode:
            with pytest.raises(RuntimeError, match="failed with exit code"):
                service._invoke(request, prepare_only=False, capture_output=True, sctx=sctx)
        else:
            service._invoke(request, prepare_only=False, capture_output=True, sctx=sctx)
    assert all(event["type"] == "feedback" and event["source"] == "coldsnap" for event in events)
    assert all(event["operation_id"] == sctx.operation.id for event in events)
    root = next(event for event in events if event["stage"] == "controller.restore")
    child = next(event for event in events if event["stage"] == "runtime.restore")
    assert child["host"] == "node-a" and child["parent_id"] == root["activity_id"]
    assert child["metrics"]["duration_seconds"] == 10
    assert child["metrics"]["clock_source"] == "runtime-unit"
    assert events[-1]["status"] == ("failed" if returncode else "succeeded")
    assert events[-1]["activity_id"] == events[0]["activity_id"]
    assert len(caplog.records) == len([v for v in events if v["level"] in {"progress", "error"}])


def test_invalid_receipt_does_not_report_controller_success():
    events = []
    request = _request()
    sctx = SimpleNamespace(timing=None, events=events.append)

    def run_command(arguments, **kwargs):
        Path(arguments[arguments.index("--receipt-json") + 1]).write_text('{"invalid": true}')
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    with pytest.raises(RuntimeError):
        ColdSnapService("coldsnap", run_command=run_command)._invoke(request, prepare_only=False, capture_output=True, sctx=sctx)
    assert events[-1]["status"] == "failed"
    assert not any(event.get("status") == "succeeded" for event in events)


def test_custom_pipeline_can_subscribe_without_core_api_scope():
    from sparkrun.plugins.coldsnap.feedback import progress
    import logging

    events = []
    sctx = SimpleNamespace(events=events.append)
    progress(logging.getLogger("coldsnap"), "weights.native_check", "Checking native payloads", sctx=sctx)
    assert events[0]["stage"] == "weights.native_check"
    assert events[0]["operation_id"] == sctx.operation.id


def test_older_host_keeps_text_feedback(monkeypatch, caplog):
    import logging
    import sparkrun.plugins
    from sparkrun.plugins.coldsnap.feedback import progress

    monkeypatch.delattr(sparkrun.plugins, "get_feedback")
    caplog.set_level(25)
    progress(logging.getLogger("coldsnap"), "prepare", "Preparing old host")
    assert "Preparing old host" in caplog.text
