# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from sparkrun.plugins.coldsnap.artifacts import ArtifactStore
from sparkrun.plugins.coldsnap.local_overlays import (
    promote_local_materialization,
    promote_local_overlay,
    select_local_materialization,
    select_local_overlay,
    target_identity,
)


def _sglang_artifact(capture, driver="n580", native=True):
    value = _artifact(capture, capsule_digest="sha256:" + capture)
    value["snapshot_driver"]["id"] = driver
    value["launch"]["engine"] = "sglang"
    value["launch"]["units"] = [{"id": "unit-0", "index": 0, "devices": ["0"], "image_digest": "sha256:image"}]
    if native:
        value["weights"]["native"] = {"capture_id": capture}
        value["weights"]["model_payloads"]["objects"][0]["sha256"] = "sha256:" + capture
    else:
        value["weights"].pop("model_payloads")
    return value


@pytest.mark.parametrize("driver", ["n580", "n610"])
@pytest.mark.parametrize("native", [True, False])
def test_sglang_materialization_keeps_capture_paired_and_source_unchanged(tmp_path, driver, native):
    store = _store(tmp_path / "store")
    source, captured = tmp_path / "source.json", tmp_path / "captured.json"
    _write(source, _sglang_artifact("source", driver))
    generated = _sglang_artifact("generated", driver, native)
    generated["launch"]["units"][0]["image_digest"] = "sha256:rebuilt-runtime"
    _write(captured, generated)
    source_bytes, captured_bytes = source.read_bytes(), captured.read_bytes()
    kwargs = dict(hardware={"h1": _hardware()}, hosts=("h1",), snapshot_driver=driver)
    seen = []
    def verify(path):
        assert not store.overlay(target_identity_key(kwargs)).exists()
        assert path == captured
        seen.append(path)
    selected = promote_local_materialization(store, source, captured, require_native=native, verify=verify, **kwargs)
    assert seen == [captured]
    assert source.read_bytes() == source_bytes
    assert selected.read_bytes() == captured_bytes  # Never splice source native metadata into the new capture.
    assert select_local_materialization(store, source, require_native=native, **kwargs) == selected
    assert select_local_materialization(store, source, **{**kwargs, "hosts": ("h2",), "hardware": {"h2": _hardware()}}) is None
    if not native:
        assert select_local_materialization(store, source, require_native=True, **kwargs) is None
    _write(source, _sglang_artifact("different-source", driver))
    with pytest.raises(RuntimeError, match="source or target"):
        select_local_materialization(store, source, **kwargs)


def target_identity_key(kwargs):
    from sparkrun.plugins.coldsnap.local_overlays import target_key
    return target_key(target_identity(kwargs["hardware"], kwargs["hosts"], kwargs["snapshot_driver"]))


@pytest.mark.parametrize("failure", ["verify", "changed-source", "changed-capture", "model", "execution", "devices", "no-native"])
def test_sglang_failed_materialization_never_replaces_selected_capture(tmp_path, failure):
    store = _store(tmp_path / "store")
    source, captured = tmp_path / "source.json", tmp_path / "captured.json"
    _write(source, _sglang_artifact("source"))
    value = _sglang_artifact("generated")
    if failure == "model":
        value["launch"]["model"]["revision"] = "wrong"
    if failure == "execution":
        value["launch"]["execution"]["adapter"]["digest"] = "wrong"
    if failure == "devices":
        value["launch"]["units"][0]["devices"] = ["wrong"]
    if failure == "no-native":
        value["weights"].pop("native")
    _write(captured, value)
    kwargs = dict(hardware={"h1": _hardware()}, hosts=("h1",), snapshot_driver="n580")
    key = target_identity_key(kwargs)
    _write(store.overlay(key), {"old": "artifact"})
    _write(store.overlay_record(key), {"old": "record"})
    before = (store.overlay(key).read_bytes(), store.overlay_record(key).read_bytes())
    def verify(path):
        if failure == "verify":
            raise RuntimeError("exact response verification failed")
        if failure == "changed-source":
            _write(source, _sglang_artifact("concurrent"))
        if failure == "changed-capture":
            _write(path, _sglang_artifact("concurrent"))
    with pytest.raises(RuntimeError):
        promote_local_materialization(store, source, captured, require_native=True, verify=verify, **kwargs)
    assert (store.overlay(key).read_bytes(), store.overlay_record(key).read_bytes()) == before


def _store(root: Path) -> ArtifactStore:
    return ArtifactStore(
        root=root,
        current=root / "current.json",
        imported=root / "imported.json",
        generations=root / "generations",
        pending=root / "pending",
        overlays=root / "overlays",
        overlay_pending=root / "overlay-pending",
    )


def _hardware(driver="610.12"):
    accelerator = SimpleNamespace(
        vendor="nvidia",
        model="gb10",
        count=1,
        memory_gb=121.0,
        capabilities=frozenset({"cuda", "rdma:roce-v2"}),
    )
    return SimpleNamespace(
        accelerators=[accelerator],
        driver_versions={"nvidia": driver},
    )


def _artifact(capture, capsule_digest="sha256:capsule", replay_digest="sha256:replay"):
    return {
        "format": 9,
        "kind": "coldsnap-snapshot-artifact",
        "state": "committed",
        "capture_id": capture,
        "request_sha256": "sha256:request",
        "snapshot_driver": {"id": "n580", "abi": 1},
        "launch": {
            "engine": "vllm",
            "model": {"id": "example/model", "revision": "revision"},
            "execution": {"adapter": {"schema": "vllm:test", "digest": "sha256:adapter"}},
        },
        "capsule": {
            "images": [
                {
                    "unit": "unit-0",
                    "reference": capsule_digest,
                    "digest": capsule_digest,
                    "root": "/opt/coldsnap/capsule",
                    "snapshot_driver": {"id": "n580", "abi": 1},
                }
            ],
            "objects": [
                {
                    "owner": "unit/unit-0",
                    "role": "oci-capsule",
                    "bytes": 100,
                    "sha256": capsule_digest,
                }
            ],
        },
        "weights": {
            "model_payloads": {
                "objects": [
                    {
                        "owner": "worker/worker-0",
                        "role": "model-weight-payload",
                        "bytes": 80,
                        "sha256": "sha256:model",
                    }
                ]
            },
            "recovery": {
                "replay_plan": [
                    {
                        "owner": "worker/worker-0",
                        "role": "safetensors-replay-plan",
                        "bytes": 20,
                        "sha256": replay_digest,
                    }
                ]
            },
        },
    }


def _write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def test_target_identity_binds_local_capsules_to_rank_ordered_hosts():
    left = target_identity({"old-a": _hardware(), "old-b": _hardware()}, ("old-a", "old-b"), "n580")
    right = target_identity({"new-a": _hardware(), "new-b": _hardware()}, ("new-a", "new-b"), "n580")
    assert left != right
    assert [unit["host"] for unit in left["units"]] == ["old-a", "old-b"]


def test_matching_residuals_do_not_create_overlay(tmp_path):
    store = _store(tmp_path / "store")
    portable = tmp_path / "portable.json"
    captured = tmp_path / "captured.json"
    _write(portable, _artifact("portable"))
    _write(captured, _artifact("local"))
    hardware = {"h1": _hardware()}

    overlay, record = promote_local_overlay(
        store,
        portable,
        captured,
        hardware=hardware,
        hosts=("h1",),
        snapshot_driver="n580",
    )

    assert overlay is None
    assert record["changed_capsule_owners"] == []
    assert record["changed_replay_owners"] == []
    assert not captured.exists()


def test_matching_residuals_remove_an_obsolete_overlay(tmp_path):
    store = _store(tmp_path / "store")
    portable = tmp_path / "portable.json"
    changed = tmp_path / "changed.json"
    matching = tmp_path / "matching.json"
    _write(portable, _artifact("portable"))
    _write(changed, _artifact("changed", capsule_digest="sha256:local"))
    _write(matching, _artifact("matching"))
    hardware = {"h1": _hardware()}

    previous, _ = promote_local_overlay(
        store,
        portable,
        changed,
        hardware=hardware,
        hosts=("h1",),
        snapshot_driver="n580",
    )
    assert previous is not None

    overlay, _ = promote_local_overlay(
        store,
        portable,
        matching,
        hardware=hardware,
        hosts=("h1",),
        snapshot_driver="n580",
    )

    assert overlay is None
    assert not previous.exists()


def test_changed_residual_is_stored_and_selected_for_same_hosts(tmp_path):
    store = _store(tmp_path / "store")
    portable = tmp_path / "portable.json"
    captured = tmp_path / "captured.json"
    _write(portable, _artifact("portable"))
    _write(captured, _artifact("local", capsule_digest="sha256:local"))

    overlay, record = promote_local_overlay(
        store,
        portable,
        captured,
        hardware={"capture-host": _hardware()},
        hosts=("capture-host",),
        snapshot_driver="n580",
    )

    assert overlay is not None
    assert record["portable_weights_retained"] is True
    assert record["format"] == 3
    assert record["portable_identity"]["capture_id"] == "portable"
    assert record["changed_capsule_owners"] == ["unit/unit-0"]
    selected = select_local_overlay(
        store,
        portable,
        hardware={"capture-host": _hardware()},
        hosts=("capture-host",),
        snapshot_driver="n580",
    )
    assert selected == overlay
    assert (
        select_local_overlay(
            store,
            portable,
            hardware={"capture-host": _hardware("611.1")},
            hosts=("capture-host",),
            snapshot_driver="n580",
        )
        is None
    )


def test_published_capsule_addresses_preserve_portable_overlay_identity(tmp_path):
    store = _store(tmp_path / "store")
    portable = tmp_path / "portable.json"
    captured = tmp_path / "captured.json"
    value = _artifact("portable")
    _write(portable, value)
    _write(captured, _artifact("local", capsule_digest="sha256:local"))
    hardware = {"capture-host": _hardware()}

    overlay, _ = promote_local_overlay(
        store,
        portable,
        captured,
        hardware=hardware,
        hosts=("capture-host",),
        snapshot_driver="n580",
    )

    value["capsule"]["images"][0]["reference"] = "registry.example/capsule@sha256:manifest"
    value["capsule"]["images"][0]["digest"] = "sha256:manifest"
    value["capsule"]["objects"][0]["sha256"] = "sha256:manifest"
    _write(portable, value)

    assert (
        select_local_overlay(
            store,
            portable,
            hardware=hardware,
            hosts=("capture-host",),
            snapshot_driver="n580",
        )
        == overlay
    )


def test_changed_capture_semantics_reject_portable_overlay(tmp_path):
    store = _store(tmp_path / "store")
    portable = tmp_path / "portable.json"
    captured = tmp_path / "captured.json"
    value = _artifact("portable")
    _write(portable, value)
    _write(captured, _artifact("local", capsule_digest="sha256:local"))
    hardware = {"capture-host": _hardware()}

    promote_local_overlay(
        store,
        portable,
        captured,
        hardware=hardware,
        hosts=("capture-host",),
        snapshot_driver="n580",
    )
    value["launch"]["model"]["revision"] = "different-revision"
    _write(portable, value)

    with pytest.raises(RuntimeError, match="does not match its portable artifact"):
        select_local_overlay(
            store,
            portable,
            hardware=hardware,
            hosts=("capture-host",),
            snapshot_driver="n580",
        )


def test_overlay_composes_portable_model_payloads_with_recovery_only_capture(tmp_path):
    store = _store(tmp_path / "store")
    portable = tmp_path / "portable.json"
    captured = tmp_path / "captured.json"
    first = _artifact("portable")
    first["weights"]["native"] = {"capable": True, "format": "stable-va-v1"}
    second = _artifact("local", capsule_digest="sha256:local")
    del second["weights"]["model_payloads"]
    _write(portable, first)
    _write(captured, second)

    overlay, _ = promote_local_overlay(
        store,
        portable,
        captured,
        hardware={"h1": _hardware()},
        hosts=("h1",),
        snapshot_driver="n580",
    )

    assert overlay is not None
    document = json.loads(overlay.read_text(encoding="utf-8"))
    assert document["weights"]["model_payloads"] == first["weights"]["model_payloads"]
    assert document["weights"]["native"] == first["weights"]["native"]


def test_overlay_rejects_different_captured_model_payloads(tmp_path):
    store = _store(tmp_path / "store")
    portable = tmp_path / "portable.json"
    captured = tmp_path / "captured.json"
    first = _artifact("portable")
    second = _artifact("local", capsule_digest="sha256:local")
    second["weights"]["model_payloads"]["objects"][0]["sha256"] = "sha256:different"
    _write(portable, first)
    _write(captured, second)

    with pytest.raises(RuntimeError, match="model payloads differ"):
        promote_local_overlay(
            store,
            portable,
            captured,
            hardware={"h1": _hardware()},
            hosts=("h1",),
            snapshot_driver="n580",
        )
