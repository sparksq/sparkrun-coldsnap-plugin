# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import sparkrun.api as api
from sparkrun.core.cluster_manager import ClusterDefinition
from sparkrun.core.recipe import Recipe
from sparkrun.core.scheduler import RankAssignment, RankSlot
from sparkrun.plugins.coldsnap import register
from sparkrun.plugins.coldsnap.artifacts import resolve_artifact_store
from sparkrun.plugins.coldsnap.cli import build_command
from sparkrun.plugins.coldsnap.deletion import NativeDeletion, build_deletion_plan, execute_deletion_plan
from sparkrun.runtimes.vllm_distributed import VllmDistributedRuntime
from sparkrun.transports.session import HostCommandResult


def _setup(tmp_path: Path):
    register(None)
    recipe = Recipe.from_dict(
        {
            "recipe_version": "2",
            "model": "Qwen/Qwen3.5-0.8B",
            "model_revision": "model-commit",
            "runtime": "vllm-distributed",
            "container": "org/vllm@sha256:" + "f" * 64,
            "defaults": {"tensor_parallel": 2},
            "coldsnap": {"capsule": {"repository": "docker.io/example/capsules"}},
        }
    )
    cluster = ClusterDefinition(name="c", hosts=["h1", "h2"], cache_dir="/cache/hf")
    placement = RankAssignment(
        by_rank=(RankSlot("h1", 0), RankSlot("h2", 0)),
        hosts_used=("h1", "h2"),
    )
    plan = api.RunPlan(
        recipe=recipe,
        runtime=VllmDistributedRuntime(),
        cluster=cluster,
        candidate_hosts=("h1", "h2"),
        host_list=("h1", "h2"),
        is_solo=False,
        placement=placement,
        intent_id="b" * 16,
        placement_token="c" * 12,
        cluster_id="sparkrun_%s_%s" % ("b" * 16, "c" * 12),
    )
    options = api.RunOptions(recipe=recipe, hosts=("h1", "h2"))
    sctx = SimpleNamespace(
        config=SimpleNamespace(
            cache_dir=str(tmp_path),
            hf_cache_dir="/cache/hf",
            ssh_user=None,
            ssh_key=None,
            ssh_options=None,
        )
    )
    return options, plan, sctx


def _artifact(capture_id="capture-one", *, driver="n610", repository="org/native"):
    objects = []
    images = []
    units = []
    workers = []
    for index, host in enumerate(("h1", "h2")):
        digest = str(index + 1) * 64
        units.append({"id": "unit-%d" % index, "host": host})
        workers.append({"id": "worker-%d" % index, "unit": "unit-%d" % index})
        images.append(
            {
                "unit": "unit-%d" % index,
                "reference": "docker.io/example/capsules@sha256:" + digest,
                "digest": "sha256:" + digest,
            }
        )
        objects.append(
            {
                "role": "model-weight-payload",
                "owner": "worker/worker-%d" % index,
                "path": "model-payloads/sha256/%s.pack" % digest,
                "bytes": 100 + index,
                "sha256": "sha256:" + digest,
            }
        )
    return {
        "format": 8,
        "kind": "coldsnap-snapshot-artifact",
        "state": "committed",
        "capture_id": capture_id,
        "snapshot_driver": {"id": driver, "abi": 1},
        "launch": {"units": units, "execution": {"workers": workers}},
        "capsule": {"images": images},
        "weights": {
            "native": {"snapshot_driver": {"id": driver, "abi": 1}},
            "model_payloads": {"repository": repository, "revision": "a" * 40, "objects": objects},
        },
    }


def _write_current(store, artifact):
    store.root.mkdir(parents=True, exist_ok=True)
    store.current.write_text(json.dumps(artifact), encoding="utf-8")


def test_delete_command_exposes_safe_scope_driver_and_confirmation_options():
    command = build_command().commands["delete"]
    parameters = {parameter.name: parameter for parameter in command.params}

    assert parameters["scope"].default == "local"
    assert parameters["driver"].default == "all"
    assert parameters["yes"].default is False
    assert "coldsnap_binary" not in parameters


def test_deletion_plan_is_recipe_driver_scoped_and_preserves_shared_payload(tmp_path):
    options, plan, sctx = _setup(tmp_path)
    store = resolve_artifact_store(plan=plan, options=options, sctx=sctx, snapshot_driver="n610")
    _write_current(store, _artifact())
    retained = _artifact("capture-other")
    retained["capsule"]["images"] = retained["capsule"]["images"][:1]
    retained["weights"]["model_payloads"]["objects"] = retained["weights"]["model_payloads"]["objects"][:1]
    retained_path = tmp_path / "coldsnap" / "artifacts" / "other" / "fingerprint" / "drivers" / "n610" / "current.json"
    retained_path.parent.mkdir(parents=True)
    retained_path.write_text(json.dumps(retained), encoding="utf-8")

    deletion = build_deletion_plan(
        plan=plan,
        options=options,
        sctx=sctx,
        scope="both",
        drivers=("n610",),
    )

    assert [record.capture_id for record in deletion.records] == ["capture-one"]
    assert any("sha256:" + "1" * 64 in item for item in deletion.skipped_shared)
    assert {path for action in deletion.host_deletions for path in action.payload_paths} == {"model-payloads/sha256/%s.pack" % ("2" * 64)}
    assert deletion.capsule_tags == ("docker.io/example/capsules:capture-one-n610-unit-unit-1",)
    assert deletion.descriptor_tags[0].endswith("-n610")
    assert deletion.native_deletions == (
        NativeDeletion(
            "org/native",
            "main",
            ("model-payloads/sha256/%s.pack" % ("2" * 64),),
        ),
    )


def test_deletion_plan_includes_target_local_overlay_capsules(tmp_path):
    options, plan, sctx = _setup(tmp_path)
    store = resolve_artifact_store(
        plan=plan,
        options=options,
        sctx=sctx,
        snapshot_driver="n580",
    )
    overlay = store.overlay("target-key")
    overlay.parent.mkdir(parents=True)
    overlay.write_text(
        json.dumps(_artifact("capture-overlay", driver="n580")),
        encoding="utf-8",
    )

    deletion = build_deletion_plan(
        plan=plan,
        options=options,
        sctx=sctx,
        scope="local",
        drivers=("n580",),
    )

    assert [record.capture_id for record in deletion.records] == ["capture-overlay"]
    assert {image for action in deletion.host_deletions for image in action.images} == {
        "docker.io/example/capsules@sha256:" + "1" * 64,
        "docker.io/example/capsules@sha256:" + "2" * 64,
    }


def test_deletion_plan_routes_stale_capture_host_aliases_through_selected_cluster(tmp_path):
    options, plan, sctx = _setup(tmp_path)
    store = resolve_artifact_store(plan=plan, options=options, sctx=sctx, snapshot_driver="n610")
    artifact = _artifact()
    for index, unit in enumerate(artifact["launch"]["units"]):
        unit["host"] = f"stale-host-{index}"
    _write_current(store, artifact)

    deletion = build_deletion_plan(
        plan=plan,
        options=options,
        sctx=sctx,
        scope="local",
        drivers=("n610",),
    )

    assert {action.host for action in deletion.host_deletions} == {"h1", "h2"}
    assert all(action.captures == (("capture-one", "n610"),) for action in deletion.host_deletions)
    assert all(len(action.images) == 2 for action in deletion.host_deletions)
    assert all("stale-host" not in action.host for action in deletion.host_deletions)
    assert len(deletion.warnings) == 2
    assert all("checked on every selected host" in warning for warning in deletion.warnings)


class _Session:
    provider_name = "test"

    def __init__(self):
        self.calls = []
        self.closed = False

    def execute(self, host, arguments, **kwargs):
        self.calls.append((host, arguments, kwargs))
        return HostCommandResult(host, 0, b"{}\n", b"")

    def close(self):
        self.closed = True


def test_local_deletion_runs_host_actions_before_removing_descriptor_store(tmp_path, monkeypatch):
    options, plan, sctx = _setup(tmp_path)
    store = resolve_artifact_store(plan=plan, options=options, sctx=sctx, snapshot_driver="n610")
    _write_current(store, _artifact())
    deletion = build_deletion_plan(
        plan=plan,
        options=options,
        sctx=sctx,
        scope="local",
        drivers=("n610",),
    )
    session = _Session()
    monkeypatch.setattr("sparkrun.plugins.coldsnap.deletion.prepare_cluster_transport", lambda *_args, **_kwargs: None)

    result = execute_deletion_plan(
        deletion,
        plan=plan,
        sctx=sctx,
        session_factory=lambda *_args, **_kwargs: session,
        run_command=lambda *_args, **_kwargs: SimpleNamespace(returncode=0),
    )

    assert result.artifact_stores == 1
    assert result.hosts == 2
    assert not store.root.exists()
    assert session.closed
    assert any(call[1][:3] == ["docker", "image", "rm"] for call in session.calls)


def test_published_deletion_uses_injected_controller_credentialed_deleters(tmp_path):
    options, plan, sctx = _setup(tmp_path)
    store = resolve_artifact_store(plan=plan, options=options, sctx=sctx, snapshot_driver="n610")
    _write_current(store, _artifact())
    deletion = build_deletion_plan(
        plan=plan,
        options=options,
        sctx=sctx,
        scope="published",
        drivers=("n610",),
        native_revision="cleanup",
    )
    oci = []
    native = []

    result = execute_deletion_plan(
        deletion,
        plan=plan,
        sctx=sctx,
        oci_deleter=oci.append,
        native_deleter=native.append,
    )

    assert result.capsule_tags == 2
    assert result.descriptor_tags == 1
    assert result.native_payloads == 2
    assert oci[-1] in deletion.descriptor_tags
    assert native == list(deletion.native_deletions)
    assert store.root.exists()
