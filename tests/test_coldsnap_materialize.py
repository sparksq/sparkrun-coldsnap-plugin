# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""One-shot materialization owns only its temporary verification launches."""

from dataclasses import replace
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

import sparkrun.api as api
from sparkrun.plugins.coldsnap.cli import _run_materialization_workload, build_command
from sparkrun.plugins.coldsnap.service import ColdSnapService
from test_coldsnap_plugin import _setup, _sglang_setup


def test_temporary_runs_allocate_ids_before_launch_without_changing_artifact_identity(monkeypatch, tmp_path):
    _, options, plan, sctx = _setup()
    options = replace(options, cache_dir="/model-cache", follow=True, detached=False)
    saved = tmp_path / "native.pack"
    saved.write_bytes(b"preserve this asset")
    ids, stops = [], []

    def run(actual, *, plan, sctx):
        assert actual.cluster_id_override == plan.cluster_id
        assert not actual.follow and actual.detached
        assert plan.intent_id == "b" * 16
        assert plan.recipe is options.recipe
        ids.append(plan.cluster_id)
        return SimpleNamespace(rc=0, cluster_id=plan.cluster_id)

    monkeypatch.setattr(api, "run", run)
    monkeypatch.setattr(api, "stop", lambda **kw: stops.append(kw) or SimpleNamespace(success=True))
    for _ in range(2):
        _run_materialization_workload(options, plan=plan, sctx=sctx, description="verification")
    assert len(set(ids)) == 2 and plan.cluster_id not in ids
    assert options.cluster_id_override is None and options.follow
    assert plan.placement_token == "c" * 12
    assert [stop["cluster_id"] for stop in stops] == ids
    assert all(stop["hosts"] == plan.host_list and stop["cluster"] is plan.cluster for stop in stops)
    assert all(stop["cache_dir"] == str(sctx.config.cache_dir) for stop in stops)
    assert saved.read_bytes() == b"preserve this asset"


@pytest.mark.parametrize("failure", [None, "rc", "exception", "interrupt", "exit", "identity"])
@pytest.mark.parametrize("cleanup", [None, "failure", "exception"])
def test_temporary_run_cleanup_preserves_primary_failure_and_cancellation(monkeypatch, capsys, failure, cleanup):
    _, options, plan, sctx = _setup()
    launched, stopped = [], []
    primary = {"exception": RuntimeError("preparation failed"), "interrupt": KeyboardInterrupt(), "exit": SystemExit(7)}

    def run(options, *, plan, sctx):
        launched.append(plan.cluster_id)
        if failure in primary:
            raise primary[failure]
        return SimpleNamespace(rc=23 if failure == "rc" else 0, cluster_id="unrelated-job" if failure == "identity" else plan.cluster_id)

    def stop(**kwargs):
        stopped.append(kwargs["cluster_id"])
        if cleanup == "exception":
            raise RuntimeError("SSH cleanup failed")
        return SimpleNamespace(success=cleanup is None, errors=("SSH cleanup failed",))

    monkeypatch.setattr(api, "run", run)
    monkeypatch.setattr(api, "stop", stop)
    if failure is None and cleanup is None:
        _run_materialization_workload(options, plan=plan, sctx=sctx, description="verification")
    else:
        expected = type(primary[failure]) if failure in {"interrupt", "exit"} else RuntimeError
        with pytest.raises(expected) as caught:
            _run_materialization_workload(options, plan=plan, sctx=sctx, description="verification")
        if failure in {"interrupt", "exit"}:
            assert caught.value is primary[failure]
        if failure == "rc":
            assert "exit code 23" in str(caught.value)
        if failure == "exception":
            assert "preparation failed" in str(caught.value)
        if failure == "identity":
            assert "unexpected launch identity" in str(caught.value)
        if cleanup:
            message = str(caught.value) + capsys.readouterr().err
            assert "cleanup failed" in message and "SSH cleanup failed" in message
            assert launched[0] in message
    assert stopped == launched and plan.cluster_id not in stopped and "unrelated-job" not in stopped


@pytest.mark.parametrize(
    "driver,native,residual,phases",
    [
        ("n610", "auto", "auto", ["native", "stop"]),
        ("n580", "auto", "auto", ["capture", "verify", "stop"]),
        ("n580", "required", "off", ["native", "stop"]),
        ("n580", "required", "required", ["native", "stop", "capture", "verify", "stop"]),
    ],
)
@pytest.mark.parametrize("existing", [False, True])
def test_vllm_materialize_stops_every_restore_including_reused_assets(monkeypatch, tmp_path, driver, native, residual, phases, existing):
    _, _, plan, sctx = _setup()
    events, ids = [], []
    artifact = tmp_path / "overlay.json"
    artifact.write_text("saved overlay")
    monkeypatch.setattr("sparkrun.api._context.default_sctx", lambda: sctx)
    monkeypatch.setattr(api, "plan", lambda *a, **kw: plan)
    monkeypatch.setattr(
        "sparkrun.plugins.coldsnap.compatibility.verify_coldsnap_hosts",
        lambda *a, **kw: SimpleNamespace(snapshot_driver=driver, hardware={}),
    )

    def run(options, *, plan, sctx):
        events.append("native" if options.strategy_options["materialize_native"] == "required" else "verify")
        ids.append(plan.cluster_id)
        return SimpleNamespace(rc=0, cluster_id=plan.cluster_id)

    def stop(**kwargs):
        assert kwargs["cluster_id"] == ids[-1] != plan.cluster_id
        events.append("stop")
        return SimpleNamespace(success=True)

    def capture(*a, **kw):
        events.append("capture")
        return artifact, {}, not existing

    monkeypatch.setattr(api, "run", run)
    monkeypatch.setattr(api, "stop", stop)
    monkeypatch.setattr(ColdSnapService, "materialize_local_overlay", capture)
    result = CliRunner().invoke(
        build_command(), ["materialize", "recipe.yaml", "--cluster", "c", "--native-weights", native, "--residual-overlay", residual]
    )
    assert result.exit_code == 0, result.output
    assert events == phases
    assert len(set(ids)) == len(ids)
    assert "materialization ready" in result.output
    assert result.output.rfind("stopping temporary") < result.output.index("materialization ready")
    assert artifact.read_text() == "saved overlay"


@pytest.mark.parametrize("engine,driver", [("vllm", "n580"), ("vllm", "n610"), ("sglang", "n580"), ("sglang", "n610")])
@pytest.mark.parametrize("failure", ["rc", "exception", "interrupt", "cleanup"])
def test_cli_failure_never_reports_ready_and_always_stops_attempted_restore(monkeypatch, tmp_path, engine, driver, failure):
    _, _, plan, sctx = (_setup if engine == "vllm" else _sglang_setup)()
    events = []
    monkeypatch.setattr("sparkrun.api._context.default_sctx", lambda: sctx)
    monkeypatch.setattr(api, "plan", lambda *a, **kw: plan)
    monkeypatch.setattr(
        "sparkrun.plugins.coldsnap.compatibility.verify_coldsnap_hosts",
        lambda *a, **kw: SimpleNamespace(snapshot_driver=driver, hardware={}),
    )
    monkeypatch.setattr(ColdSnapService, "materialize_local_overlay", lambda *a, **kw: (tmp_path / "overlay", {}, True))

    def sglang(*a, **kw):
        kw["verify"](tmp_path / "candidate")
        pytest.fail("failed verification was promoted")

    def run(*a, **kw):
        events.append("run")
        if failure == "exception":
            raise RuntimeError("restore failed before return")
        if failure == "interrupt":
            raise KeyboardInterrupt()
        return SimpleNamespace(rc=1 if failure == "rc" else 0)

    monkeypatch.setattr(ColdSnapService, "materialize_sglang", sglang)
    monkeypatch.setattr(api, "run", run)
    monkeypatch.setattr(
        api, "stop", lambda **kw: events.append("stop") or SimpleNamespace(success=failure != "cleanup", errors=("cleanup denied",))
    )
    result = CliRunner().invoke(build_command(), ["materialize", "recipe.yaml", "--cluster", "c"])
    assert result.exit_code != 0
    assert events == ["run", "stop"]
    assert "materialization ready" not in result.output


@pytest.mark.parametrize("engine", ["vllm", "sglang"])
def test_dry_run_never_runs_or_stops_workloads(monkeypatch, engine):
    _, _, plan, sctx = (_setup if engine == "vllm" else _sglang_setup)()
    monkeypatch.setattr("sparkrun.api._context.default_sctx", lambda: sctx)
    monkeypatch.setattr(api, "plan", lambda *a, **kw: plan)
    monkeypatch.setattr(api, "run", lambda *a, **kw: pytest.fail("dry run launched a workload"))
    monkeypatch.setattr(api, "stop", lambda *a, **kw: pytest.fail("dry run stopped a workload"))
    result = CliRunner().invoke(build_command(), ["materialize", "recipe.yaml", "--dry-run"])
    assert result.exit_code == 0, result.output
