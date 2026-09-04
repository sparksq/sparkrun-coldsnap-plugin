# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Verified target-compatible residual overlays for portable artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
from copy import deepcopy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from sparkrun.plugins.coldsnap.artifacts import ArtifactStore


OVERLAY_FORMAT = 3
OVERLAY_KIND = "sparkrun-coldsnap-local-residual-overlay"
PORTABLE_IDENTITY_FORMAT = 1
_MAX_DESCRIPTOR_BYTES = 8 * 1024 * 1024


def target_identity(hardware: Mapping[str, Any], hosts: Sequence[str], snapshot_driver: str) -> dict[str, Any]:
    """Return the exact rank-ordered target that owns local capsule images."""

    units: list[dict[str, Any]] = []
    for host in hosts:
        value = hardware.get(host)
        if value is None:
            raise RuntimeError("ColdSnap local overlay requires complete live hardware inventory")
        accelerators = []
        for accelerator in value.accelerators:
            accelerators.append(
                {
                    "vendor": accelerator.vendor,
                    "model": accelerator.model,
                    "count": accelerator.count,
                    "memory_gb": accelerator.memory_gb,
                    "capabilities": sorted(accelerator.capabilities),
                }
            )
        units.append(
            {
                "host": host,
                "nvidia_driver": value.driver_versions.get("nvidia", ""),
                "accelerators": accelerators,
            }
        )
    identity = {
        "format": 1,
        "kind": "sparkrun-coldsnap-local-overlay-target",
        "snapshot_driver": snapshot_driver,
        "units": units,
    }
    if any(not unit["nvidia_driver"] or not unit["accelerators"] for unit in units):
        raise RuntimeError("ColdSnap local overlay target identity is incomplete")
    return identity


def target_key(identity: Mapping[str, Any]) -> str:
    payload = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def select_local_overlay(
    store: ArtifactStore,
    portable_path: Path,
    *,
    hardware: Mapping[str, Any],
    hosts: Sequence[str],
    snapshot_driver: str,
) -> Path | None:
    """Return a verified overlay bound to this portable artifact and target."""

    identity = target_identity(hardware, hosts, snapshot_driver)
    key = target_key(identity)
    record_path = store.overlay_record(key)
    artifact_path = store.overlay(key)
    if not record_path.exists() or not artifact_path.exists():
        return None
    record = _read_json(record_path)
    portable_payload = _read_regular(portable_path)
    overlay_payload = _read_regular(artifact_path)
    portable = _decode_artifact(portable_payload, portable_path)
    overlay = _decode_artifact(overlay_payload, artifact_path)
    if (
        record.get("format") != OVERLAY_FORMAT
        or record.get("kind") != OVERLAY_KIND
        or record.get("target_key") != key
        or record.get("target") != identity
        or record.get("portable_identity") != _portable_identity(portable)
        or record.get("overlay_sha256") != _sha256(overlay_payload)
    ):
        raise RuntimeError("ColdSnap local overlay record does not match its portable artifact or target")
    if (
        record.get("portable_capture_id") != portable.get("capture_id")
        or record.get("overlay_capture_id") != overlay.get("capture_id")
        or overlay.get("snapshot_driver", {}).get("id") != snapshot_driver
        or record.get("portable_weights_retained") is not True
        or not _same_model_payloads(portable, overlay)
    ):
        raise RuntimeError("ColdSnap local overlay identity is invalid")
    return artifact_path


def promote_local_overlay(
    store: ArtifactStore,
    portable_path: Path,
    captured_path: Path,
    *,
    hardware: Mapping[str, Any],
    hosts: Sequence[str],
    snapshot_driver: str,
) -> tuple[Path | None, dict[str, Any]]:
    """Commit a derived artifact only when its residual components differ."""

    identity = target_identity(hardware, hosts, snapshot_driver)
    key = target_key(identity)
    portable_payload = _read_regular(portable_path)
    captured_payload = _read_regular(captured_path)
    portable = _decode_artifact(portable_payload, portable_path)
    overlay = _decode_artifact(captured_payload, captured_path)
    if portable.get("snapshot_driver", {}).get("id") != snapshot_driver or overlay.get("snapshot_driver", {}).get("id") != snapshot_driver:
        raise RuntimeError("ColdSnap local overlay snapshot driver differs from its target")
    overlay = _retain_portable_weights(portable, overlay)
    overlay_payload = json.dumps(overlay, sort_keys=True, separators=(",", ":")).encode("utf-8") + b"\n"
    if not _same_model_payloads(portable, overlay):
        raise RuntimeError("ColdSnap local overlay model payloads differ from the portable artifact")

    capsule_changes = _changed_owners(portable, overlay, "capsule", "objects")
    replay_changes = _changed_owners(portable, overlay, "weights", "recovery", "replay_plan")
    differs = bool(capsule_changes or replay_changes)
    record = {
        "format": OVERLAY_FORMAT,
        "kind": OVERLAY_KIND,
        "target_key": key,
        "target": identity,
        "portable_capture_id": portable.get("capture_id"),
        "portable_identity": _portable_identity(portable),
        "overlay_capture_id": overlay.get("capture_id"),
        "overlay_sha256": _sha256(overlay_payload),
        "portable_weights_retained": True,
        "changed_capsule_owners": capsule_changes,
        "changed_replay_owners": replay_changes,
    }
    if not differs:
        store.overlay(key).unlink(missing_ok=True)
        store.overlay_record(key).unlink(missing_ok=True)
        captured_path.unlink(missing_ok=True)
        return None, record

    root = store.overlay(key).parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    _atomic_write(store.overlay(key), overlay_payload)
    _atomic_write(store.overlay_record(key), json.dumps(record, sort_keys=True).encode("utf-8") + b"\n")
    captured_path.unlink(missing_ok=True)
    return store.overlay(key), record


def _same_model_payloads(first: Mapping[str, Any], second: Mapping[str, Any]) -> bool:
    left = _object_identities(first, "weights", "model_payloads", "objects")
    right = _object_identities(second, "weights", "model_payloads", "objects")
    return left == right


def _retain_portable_weights(portable: Mapping[str, Any], captured: Mapping[str, Any]) -> dict[str, Any]:
    """Compose target residuals with the portable artifact's weight providers."""

    portable_weights = portable.get("weights")
    captured_weights = captured.get("weights")
    if not isinstance(portable_weights, Mapping) or not isinstance(captured_weights, Mapping):
        raise RuntimeError("ColdSnap local overlay weight providers are invalid")
    if _recovery_identity(portable_weights) != _recovery_identity(captured_weights):
        raise RuntimeError("ColdSnap local overlay recovery provider differs from the portable artifact")

    expected = _object_identities(portable, "weights", "model_payloads", "objects")
    observed = _object_identities(captured, "weights", "model_payloads", "objects")
    if observed and observed != expected:
        raise RuntimeError("ColdSnap local overlay model payloads differ from the portable artifact")

    result = deepcopy(dict(captured))
    result_weights = deepcopy(dict(captured_weights))
    for provider in ("model_payloads", "native"):
        if provider in portable_weights:
            result_weights[provider] = deepcopy(portable_weights[provider])
        else:
            result_weights.pop(provider, None)
    result["weights"] = result_weights
    return result


def _recovery_identity(weights: Mapping[str, Any]) -> dict[str, Any]:
    recovery = weights.get("recovery")
    if not isinstance(recovery, Mapping):
        return {}
    return {key: deepcopy(value) for key, value in recovery.items() if key != "replay_plan"}


def _portable_identity(document: Mapping[str, Any]) -> dict[str, Any]:
    """Return capture semantics that survive transport publication.

    Capsule publication replaces local Docker image IDs with registry manifest
    digests. Those are two addresses for the same captured capsule, so neither
    belongs in an overlay's portable-artifact identity. The capture request,
    launch contract, runtime bindings, and weight payloads remain stable and
    are sufficient to reject an overlay from another capture or workload.
    """

    launch = document.get("launch")
    weights = document.get("weights")
    runtime = document.get("runtime")
    return {
        "format": PORTABLE_IDENTITY_FORMAT,
        "capture_id": document.get("capture_id"),
        "request_sha256": document.get("request_sha256"),
        "snapshot_driver": document.get("snapshot_driver"),
        "requires": document.get("requires"),
        "launch": launch,
        "compatibility": document.get("compatibility"),
        "runtime": runtime,
        "weights": weights,
        "capsule_layout": _capsule_layout(document),
    }


def _capsule_layout(document: Mapping[str, Any]) -> dict[str, Any]:
    capsule = document.get("capsule")
    if not isinstance(capsule, Mapping):
        return {}
    images = []
    for value in capsule.get("images", ()):
        if not isinstance(value, Mapping):
            continue
        images.append(
            {
                "unit": value.get("unit"),
                "root": value.get("root"),
                "snapshot_driver": value.get("snapshot_driver"),
            }
        )
    objects = []
    for value in capsule.get("objects", ()):
        if not isinstance(value, Mapping):
            continue
        objects.append(
            {
                "role": value.get("role"),
                "owner": value.get("owner"),
                "path": value.get("path"),
                "bytes": value.get("bytes"),
            }
        )
    return {"images": images, "objects": objects}


def _changed_owners(first: Mapping[str, Any], second: Mapping[str, Any], *path: str) -> list[str]:
    left = _object_identities(first, *path)
    right = _object_identities(second, *path)
    return sorted(owner for owner in set(left) | set(right) if left.get(owner) != right.get(owner))


def _object_identities(document: Mapping[str, Any], *path: str) -> dict[str, tuple[Any, Any, Any]]:
    value: Any = document
    for name in path:
        value = value.get(name, {}) if isinstance(value, Mapping) else {}
    if not isinstance(value, list):
        return {}
    result = {}
    for item in value:
        if isinstance(item, Mapping) and isinstance(item.get("owner"), str):
            result[item["owner"]] = (item.get("role"), item.get("bytes"), item.get("sha256"))
    return result


def _read_regular(path: Path) -> bytes:
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_size <= 0 or metadata.st_size > _MAX_DESCRIPTOR_BYTES:
        raise RuntimeError("ColdSnap local overlay descriptor is not a bounded regular file: %s" % path)
    return path.read_bytes()


def _decode_artifact(payload: bytes, path: Path) -> dict[str, Any]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("ColdSnap local overlay descriptor is invalid: %s" % path) from error
    if not isinstance(value, dict) or value.get("kind") != "coldsnap-snapshot-artifact" or value.get("state") != "committed":
        raise RuntimeError("ColdSnap local overlay descriptor is not a committed artifact: %s" % path)
    return value


def _read_json(path: Path) -> dict[str, Any]:
    value = _decode_json(_read_regular(path), path)
    if not isinstance(value, dict):
        raise RuntimeError("ColdSnap local overlay record is not an object: %s" % path)
    return value


def _decode_json(payload: bytes, path: Path) -> Any:
    try:
        return json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("ColdSnap local overlay JSON is invalid: %s" % path) from error


def _sha256(payload: bytes) -> str:
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".%s." % path.name, dir=path.parent)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


__all__ = [
    "promote_local_overlay",
    "select_local_overlay",
    "target_identity",
    "target_key",
]
