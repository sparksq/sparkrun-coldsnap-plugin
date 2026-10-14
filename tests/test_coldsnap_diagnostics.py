# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Snapshot ColdSnap evidence before receipt-directory teardown."""

from contextlib import contextmanager, nullcontext
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import pytest
from scitrera_app_framework import Variables

from sparkrun import api
from sparkrun.core.config import SparkrunConfig
from sparkrun.core.context import SparkrunContext
from sparkrun.core.plugin_identity import PluginIdentity, plugin_registration
from sparkrun.plugins.coldsnap import diagnostics
from sparkrun.plugins.coldsnap import service as service_module
from test_coldsnap_timing import _request, _receipt, _timing_events


@pytest.fixture
def diagnostic_context(tmp_path, monkeypatch):
    monkeypatch.setattr("sparkrun.core.plugin_identity._IDENTITIES", {})
    monkeypatch.setattr("sparkrun.diagnostics.plugin._PLUGINS", {})
    monkeypatch.setattr(diagnostics, "_factory", None)
    with plugin_registration(PluginIdentity("coldsnap", "sparkrun.plugins.coldsnap", ("external", "coldsnap", None))):
        diagnostics.register()
    monkeypatch.setattr("sparkrun.diagnostics.session.DiagnosticsCapture.collect_config", lambda self: None)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("{}\n")
    return SparkrunContext(variables=Variables(), config=SparkrunConfig(config_path))


@pytest.mark.parametrize("returncode", [0, 1])
def test_controller_snapshot_precedes_receipt_cleanup(diagnostic_context, tmp_path, monkeypatch, returncode):
    path = tmp_path / "controller.ndjson"
    request = _request()
    roots = []

    @contextmanager
    def temporary(*args, **kwargs):
        with TemporaryDirectory(*args, **kwargs) as root:
            roots.append(Path(root))
            try:
                yield root
            finally:
                assert '"section": "controller_result"' in path.read_text()
                assert (Path(root) / "operation.json").exists()

    monkeypatch.setattr(service_module, "TemporaryDirectory", temporary)

    def command(arguments, **kwargs):
        receipt = _receipt(request)
        if returncode:
            receipt["state"] = "failed"
            receipt["timing"]["spans"][0]["status"] = "error"
        Path(arguments[arguments.index("--receipt-json") + 1]).write_text(json.dumps(receipt))
        Path(arguments[arguments.index("--timing-events") + 1]).write_text("".join(json.dumps(e) + "\n" for e in _timing_events(receipt)))
        return SimpleNamespace(returncode=returncode, stderr="Bearer controller-secret", stdout="")

    with pytest.raises(RuntimeError, match="failed with exit code") if returncode else nullcontext():
        with api.capture_diagnostics(path, sctx=diagnostic_context):
            service_module.ColdSnapService("coldsnap", run_command=command)._invoke(
                request,
                prepare_only=False,
                capture_output=True,
                sctx=diagnostic_context,
            )
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    entry = next(row for row in rows if row.get("section") == "controller_result")
    assert entry["plugin"] == "coldsnap" and entry["data"]["return_code"] == returncode
    assert "controller-secret" not in path.read_text()
    assert rows[-1]["operation_success"] is (returncode == 0)
    assert all(not root.exists() for root in roots)


@pytest.mark.parametrize("driver", ["n610", "n580"])
def test_artifact_compatibility_decision_is_captured(diagnostic_context, tmp_path, monkeypatch, driver):
    artifact_path = tmp_path / "artifact.json"
    artifact_path.write_text(json.dumps({"kind": "coldsnap-snapshot-artifact", "state": "committed", "snapshot_driver": {"id": driver}}))
    monkeypatch.setattr(service_module.ColdSnapService, "_restore_artifact_path", lambda *args, **kwargs: artifact_path)
    monkeypatch.setattr(
        service_module, "build_request", lambda *args, **kwargs: {"artifact": str(artifact_path), "launch": {"engine": "vllm"}}
    )
    monkeypatch.setattr(service_module, "resolve_request_weight_mode", lambda request: "auto")
    context = SimpleNamespace(sctx=diagnostic_context, plan=SimpleNamespace(workload_id="workload"), options=SimpleNamespace(dry_run=False))
    path = tmp_path / "artifact.ndjson"
    with pytest.raises(RuntimeError, match="snapshot driver") if driver == "n580" else nullcontext():
        with api.capture_diagnostics(path, sctx=diagnostic_context):
            service_module.ColdSnapService().describe_restore(context)
    row = next(json.loads(line) for line in path.read_text().splitlines() if '"section": "artifact_selection"' in line)
    assert row["data"]["compatible"] is (driver == "n610")
    assert row["workload_id"] == "workload"


def test_older_host_has_no_diagnostic_dependency(diagnostic_context, monkeypatch):
    import sparkrun.plugins

    monkeypatch.delattr(sparkrun.plugins, "DIAGNOSTICS_API_VERSION")
    diagnostics.register()
    assert diagnostics.bind(sctx=diagnostic_context) is None
