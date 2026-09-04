# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Recipe-scoped, reachability-aware ColdSnap artifact deletion."""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sparkrun.core.config import resolve_hf_token, resolve_sparkrun_cache_dir
from sparkrun.orchestration.primitives import build_ssh_kwargs
from sparkrun.plugins.coldsnap.artifacts import ArtifactStore, remove_artifact_store, resolve_artifact_store
from sparkrun.plugins.coldsnap.oci_artifacts import default_artifact_publish_reference, delete_oci_tag
from sparkrun.plugins.coldsnap.policy import resolve_coldsnap_policy
from sparkrun.transports import open_cluster_host_session, prepare_cluster_transport

logger = logging.getLogger(__name__)
_DRIVERS = ("n580", "n610")
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_PAYLOAD_PATH = re.compile(r"model-payloads/sha256/([0-9a-f]{64})\.pack")
_SAFE_TAG = re.compile(r"[^a-z0-9_.-]+")


@dataclass(frozen=True)
class ArtifactRecord:
    path: Path
    driver: str
    capture_id: str
    document: dict[str, Any]


@dataclass(frozen=True)
class NativeDeletion:
    repository: str
    revision: str
    paths: tuple[str, ...]


@dataclass(frozen=True)
class HostDeletion:
    host: str
    captures: tuple[tuple[str, str], ...]
    images: tuple[str, ...]
    payload_paths: tuple[str, ...]
    huggingface_payloads: tuple[tuple[str, str, str], ...]


@dataclass(frozen=True)
class DeletionPlan:
    scope: str
    drivers: tuple[str, ...]
    stores: tuple[ArtifactStore, ...]
    records: tuple[ArtifactRecord, ...]
    host_deletions: tuple[HostDeletion, ...]
    capsule_tags: tuple[str, ...]
    descriptor_tags: tuple[str, ...]
    native_deletions: tuple[NativeDeletion, ...]
    skipped_shared: tuple[str, ...]
    warnings: tuple[str, ...]
    remote_state_root: str = ""

    @property
    def empty(self) -> bool:
        return not (
            any(store.root.exists() for store in self.stores)
            or self.host_deletions
            or self.capsule_tags
            or self.descriptor_tags
            or self.native_deletions
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": 1,
            "kind": "sparkrun-coldsnap-deletion-plan",
            "scope": self.scope,
            "drivers": list(self.drivers),
            "captures": [
                {"driver": record.driver, "capture_id": record.capture_id, "descriptor": str(record.path)} for record in self.records
            ],
            "local": {
                "artifact_stores": [str(store.root) for store in self.stores if store.root.exists()],
                "hosts": [
                    {
                        "host": action.host,
                        "captures": [{"capture_id": capture_id, "driver": driver} for capture_id, driver in action.captures],
                        "capsule_images": list(action.images),
                        "native_payloads": list(action.payload_paths),
                        "huggingface_native_payloads": [
                            {"repository": repository, "revision": revision, "path": path}
                            for repository, revision, path in action.huggingface_payloads
                        ],
                    }
                    for action in self.host_deletions
                ],
            },
            "published": {
                "capsule_tags": list(self.capsule_tags),
                "descriptor_tags": list(self.descriptor_tags),
                "native_payloads": [
                    {
                        "repository": deletion.repository,
                        "revision": deletion.revision,
                        "paths": list(deletion.paths),
                    }
                    for deletion in self.native_deletions
                ],
            },
            "skipped_shared": list(self.skipped_shared),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class DeletionResult:
    artifact_stores: int
    hosts: int
    capsule_tags: int
    descriptor_tags: int
    native_payloads: int


def build_deletion_plan(
    *,
    plan,
    options,
    sctx,
    scope: str = "local",
    drivers: Iterable[str] = _DRIVERS,
    native_revision: str = "",
) -> DeletionPlan:
    """Render the exact deletion set before any destructive operation."""

    if scope not in {"local", "published", "both"}:
        raise ValueError("ColdSnap deletion scope must be local, published, or both")
    selected = tuple(dict.fromkeys(str(driver) for driver in drivers))
    if not selected or any(driver not in _DRIVERS for driver in selected):
        raise ValueError("ColdSnap deletion drivers must be n580 and/or n610")
    stores = tuple(resolve_artifact_store(plan=plan, options=options, sctx=sctx, snapshot_driver=driver) for driver in selected)
    records = _target_records(stores, selected)
    retained = _retained_references(stores, sctx)
    retained_payloads = retained[0]
    retained_images = retained[1]
    selected_hosts = tuple(dict.fromkeys(str(host) for host in (getattr(plan.cluster, "hosts", ()) or plan.host_list)))
    allowed_hosts = frozenset(selected_hosts)
    captures_by_host: dict[str, set[tuple[str, str]]] = {}
    images_by_host: dict[str, set[str]] = {}
    target_payloads: dict[str, str] = {}
    huggingface_payloads: set[tuple[str, str, str]] = set()
    capsule_tags: set[str] = set()
    skipped: set[str] = set()
    warnings: set[str] = set()

    for record in records:
        units = _units(record)
        workers = _worker_hosts(record.document, units)
        for host in set(units.values()):
            # A descriptor records the address used when it was captured, but
            # cluster transport addresses can change (for example, after
            # switching between management interfaces).  The descriptor has
            # no durable alias map.  Keep exact matches targeted; otherwise
            # send the identity-scoped, idempotent deletion only to the
            # selected cluster instead of contacting the stale address.
            targets = (host,) if host in allowed_hosts else selected_hosts
            if host not in allowed_hosts and scope in {"local", "both"}:
                warnings.add(
                    f"capture {record.capture_id} records host {host} outside the selected cluster; "
                    "matching state will be checked on every selected host"
                )
            for target in targets:
                captures_by_host.setdefault(target, set()).add((record.capture_id, record.driver))
        for image in _capsule_images(record.document):
            unit = image[0]
            reference = image[1]
            digest = image[2]
            host = units.get(unit)
            if host is None:
                raise RuntimeError("ColdSnap capsule owner %s has no launch unit" % unit)
            if digest in retained_images:
                skipped.add("capsule %s remains referenced by another local artifact" % digest)
            else:
                targets = (host,) if host in allowed_hosts else selected_hosts
                for target in targets:
                    images_by_host.setdefault(target, set()).add(reference)
                repository = _published_repository(reference)
                if repository:
                    capsule_tags.add(_capsule_tag(repository, record.capture_id, record.driver, unit))
        for owner, path, digest in _model_payloads(record.document):
            target_payloads[digest] = path
            if digest in retained_payloads:
                skipped.add("native payload %s remains referenced by another local artifact" % digest)
                continue
            worker = owner.removeprefix("worker/")
            host = workers.get(worker)
            if host is None:
                raise RuntimeError("ColdSnap native payload owner %s has no execution worker" % owner)
            provider = record.document.get("weights", {}).get("model_payloads", {})
            repository = provider.get("repository") if isinstance(provider, Mapping) else None
            revision = provider.get("revision") if isinstance(provider, Mapping) else None
            if isinstance(repository, str) and repository and isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision):
                huggingface_payloads.add((repository, revision, path))

    payload_paths = tuple(sorted(path for digest, path in target_payloads.items() if digest not in retained_payloads))
    if scope in {"local", "both"} and payload_paths:
        # A published payload can be cached on a replacement host rather than
        # its capture host. Probe every selected host; absent cache files are a
        # successful no-op.
        for host in allowed_hosts:
            captures_by_host.setdefault(host, set())

    host_deletions = tuple(
        HostDeletion(
            host=host,
            captures=tuple(sorted(captures_by_host.get(host, set()))),
            images=tuple(sorted(images_by_host.get(host, set()))),
            payload_paths=payload_paths,
            huggingface_payloads=tuple(sorted(huggingface_payloads)),
        )
        for host in sorted(set(captures_by_host) | set(images_by_host))
        if captures_by_host.get(host) or images_by_host.get(host) or payload_paths
    )

    descriptor_tags: set[str] = set()
    if scope in {"published", "both"}:
        for driver in selected:
            reference = default_artifact_publish_reference(plan=plan, options=options, snapshot_driver=driver)
            if reference:
                descriptor_tags.add(reference.removeprefix("oci://"))
        if not records:
            warnings.add("no local committed descriptor is available to enumerate published capsules or native payloads")

    config = plan.recipe.plugin_item("coldsnap")
    configured_revision = str(getattr(getattr(config, "native", None), "revision", "") or "")
    mutable_revision = native_revision or (configured_revision if not re.fullmatch(r"[0-9a-f]{40}", configured_revision) else "") or "main"
    native_groups: dict[tuple[str, str], set[str]] = {}
    if scope in {"published", "both"}:
        for record in records:
            provider = record.document.get("weights", {}).get("model_payloads", {})
            repository = provider.get("repository") if isinstance(provider, Mapping) else None
            if not isinstance(repository, str) or not repository:
                continue
            for _owner, path, digest in _model_payloads(record.document):
                if digest not in retained_payloads:
                    native_groups.setdefault((repository, mutable_revision), set()).add(path)

    if scope == "local":
        capsule_tags = set()
        descriptor_tags = set()
        native_groups = {}
    if scope == "published":
        host_deletions = ()

    site_policy = resolve_coldsnap_policy(
        cluster=plan.cluster,
        sctx=sctx,
        hosts=list(plan.host_list),
        probe_remote=False,
    )
    remote_state_root = site_policy.state_root
    if site_policy.sources.get("sparkrun_cache_dir") == "remote-user-cache":
        # The remote deletion program resolves this per host. Keeping the
        # controller-side plan symbolic-free also preserves render-only use.
        remote_state_root = ""

    return DeletionPlan(
        scope=scope,
        drivers=selected,
        stores=stores,
        records=records,
        host_deletions=host_deletions,
        capsule_tags=tuple(sorted(capsule_tags)),
        descriptor_tags=tuple(sorted(descriptor_tags)),
        native_deletions=tuple(
            NativeDeletion(repository, revision, tuple(sorted(paths))) for (repository, revision), paths in sorted(native_groups.items())
        ),
        skipped_shared=tuple(sorted(skipped)),
        warnings=tuple(sorted(warnings)),
        remote_state_root=remote_state_root,
    )


def execute_deletion_plan(
    deletion: DeletionPlan,
    *,
    plan,
    sctx,
    session_factory=open_cluster_host_session,
    oci_deleter: Callable[[str], None] = delete_oci_tag,
    native_deleter: Callable[[NativeDeletion], None] | None = None,
    run_command=subprocess.run,
) -> DeletionResult:
    """Execute a previously rendered deletion plan."""

    native_deleter = native_deleter or _delete_huggingface_payloads
    capsule_count = descriptor_count = native_count = 0
    if deletion.scope in {"published", "both"}:
        for reference in deletion.capsule_tags:
            logger.info("ColdSnap delete: published capsule %s", reference)
            oci_deleter(reference)
            capsule_count += 1
        for native in deletion.native_deletions:
            logger.info(
                "ColdSnap delete: %d published native payload(s) from %s@%s",
                len(native.paths),
                native.repository,
                native.revision,
            )
            native_deleter(native)
            native_count += len(native.paths)
        for reference in deletion.descriptor_tags:
            logger.info("ColdSnap delete: published descriptor %s", reference)
            oci_deleter(reference)
            descriptor_count += 1

    host_count = 0
    if deletion.scope in {"local", "both"} and deletion.host_deletions:
        prepare_cluster_transport(plan.cluster, dry_run=False)
        ssh_kwargs = build_ssh_kwargs(sctx.config)
        if getattr(plan.cluster, "user", None):
            ssh_kwargs = {**ssh_kwargs, "ssh_user": plan.cluster.user}
        session = session_factory(plan.cluster, ssh_kwargs=ssh_kwargs)
        try:
            for action in deletion.host_deletions:
                payload = json.dumps(
                    {
                        "state_root": deletion.remote_state_root,
                        "captures": [list(item) for item in action.captures],
                        "payload_paths": list(action.payload_paths),
                        "huggingface_payloads": [list(item) for item in action.huggingface_payloads],
                        "hf_cache_root": str(plan.cluster.cache_dir or getattr(sctx.config, "hf_cache_dir", "")),
                    },
                    sort_keys=True,
                ).encode("utf-8")
                result = session.execute(
                    action.host,
                    ["python3", "-c", _LOCAL_DELETE_PROGRAM],
                    input_data=payload,
                    timeout=900,
                )
                if result.returncode:
                    detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
                    raise RuntimeError("delete ColdSnap state on %s failed: %s" % (action.host, detail[-2000:]))
                for image in action.images:
                    result = session.execute(action.host, ["docker", "image", "rm", image], timeout=300)
                    if result.returncode:
                        detail = (result.stderr or result.stdout).decode("utf-8", errors="replace").strip()
                        if "No such image" not in detail:
                            raise RuntimeError("delete ColdSnap capsule image on %s failed: %s" % (action.host, detail[-2000:]))
                host_count += 1
        finally:
            session.close()

    store_count = 0
    if deletion.scope in {"local", "both"}:
        for store in deletion.stores:
            if store.root.exists():
                remove_artifact_store(store)
                store_count += 1
        # Descriptor images are only controller-side caches. Remove their
        # tags after the durable descriptor store is gone; missing images are
        # expected and do not make deletion fail.
        for reference in deletion.descriptor_tags:
            run_command(["docker", "image", "rm", reference], text=True, check=False, capture_output=True)

    return DeletionResult(store_count, host_count, capsule_count, descriptor_count, native_count)


def _target_records(stores: tuple[ArtifactStore, ...], drivers: tuple[str, ...]) -> tuple[ArtifactRecord, ...]:
    selected: dict[tuple[str, str], ArtifactRecord] = {}
    for store, driver in zip(stores, drivers, strict=True):
        for path in _store_descriptors(store):
            record = _read_record(path, expected_driver=driver)
            if record is None:
                continue
            key = (record.driver, record.capture_id)
            current = selected.get(key)
            if current is None or _record_score(record.document) >= _record_score(current.document):
                selected[key] = record
    return tuple(sorted(selected.values(), key=lambda item: (item.driver, item.capture_id)))


def _store_descriptors(store: ArtifactStore) -> tuple[Path, ...]:
    paths: list[Path] = []
    for path in (store.current, store.imported):
        if path.is_file():
            paths.append(path)
    for directory in (store.generations, store.pending):
        if directory.is_dir():
            paths.extend(path for path in directory.glob("*.json") if path.is_file())
    if store.overlays.is_dir():
        paths.extend(path for path in store.overlays.glob("*/artifact.json") if path.is_file())
    return tuple(paths)


def _read_record(path: Path, *, expected_driver: str | None = None) -> ArtifactRecord | None:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("kind") != "coldsnap-snapshot-artifact" or document.get("state") != "committed":
        return None
    capture_id = document.get("capture_id")
    driver = document.get("snapshot_driver", {}).get("id")
    if not isinstance(capture_id, str) or not _SAFE_ID.fullmatch(capture_id) or driver not in _DRIVERS:
        raise RuntimeError("ColdSnap descriptor identity is invalid: %s" % path)
    if expected_driver is not None and driver != expected_driver:
        raise RuntimeError("ColdSnap descriptor driver does not match its store: %s" % path)
    return ArtifactRecord(path.resolve(), driver, capture_id, document)


def _record_score(document: dict[str, Any]) -> int:
    score = 0
    if any(_published_repository(reference) for _unit, reference, _digest in _capsule_images(document)):
        score += 1
    provider = document.get("weights", {}).get("model_payloads", {})
    if isinstance(provider, Mapping) and provider.get("repository"):
        score += 1
    return score


def _retained_references(stores: tuple[ArtifactStore, ...], sctx) -> tuple[set[str], set[str]]:
    cache_root = resolve_sparkrun_cache_dir(getattr(sctx.config, "cache_dir", None)).expanduser().resolve()
    artifact_root = cache_root / "coldsnap" / "artifacts"
    excluded = tuple(store.root.resolve() for store in stores)
    payloads: set[str] = set()
    images: set[str] = set()
    if not artifact_root.is_dir():
        return payloads, images
    for path in artifact_root.rglob("*.json"):
        resolved = path.resolve()
        if any(_is_relative_to(resolved, root) for root in excluded):
            continue
        record = _read_record(path)
        if record is None:
            continue
        payloads.update(digest for _owner, _path, digest in _model_payloads(record.document))
        images.update(digest for _unit, _reference, digest in _capsule_images(record.document))
    return payloads, images


def _units(record: ArtifactRecord) -> dict[str, str]:
    result: dict[str, str] = {}
    units = record.document.get("launch", {}).get("units", [])
    if not isinstance(units, list):
        raise RuntimeError("ColdSnap descriptor has no launch-unit inventory: %s" % record.path)
    for unit in units:
        identifier = unit.get("id") if isinstance(unit, Mapping) else None
        host = unit.get("host") if isinstance(unit, Mapping) else None
        if not isinstance(identifier, str) or not _SAFE_ID.fullmatch(identifier) or not isinstance(host, str) or not host:
            raise RuntimeError("ColdSnap descriptor launch-unit inventory is invalid: %s" % record.path)
        result[identifier] = host
    return result


def _worker_hosts(document: dict[str, Any], units: dict[str, str]) -> dict[str, str]:
    result: dict[str, str] = {}
    workers = document.get("launch", {}).get("execution", {}).get("workers", [])
    if not isinstance(workers, list):
        return result
    for worker in workers:
        identifier = worker.get("id") if isinstance(worker, Mapping) else None
        unit = worker.get("unit") if isinstance(worker, Mapping) else None
        if isinstance(identifier, str) and _SAFE_ID.fullmatch(identifier) and unit in units:
            result[identifier] = units[str(unit)]
    return result


def _capsule_images(document: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    images = document.get("capsule", {}).get("images", [])
    if not isinstance(images, list):
        return ()
    result: list[tuple[str, str, str]] = []
    for image in images:
        unit = image.get("unit") if isinstance(image, Mapping) else None
        reference = image.get("reference") if isinstance(image, Mapping) else None
        digest = image.get("digest") if isinstance(image, Mapping) else None
        if not isinstance(unit, str) or not _SAFE_ID.fullmatch(unit) or not isinstance(reference, str) or not isinstance(digest, str):
            raise RuntimeError("ColdSnap capsule inventory is invalid")
        if not _DIGEST.fullmatch(digest) or not (reference == digest or reference.endswith("@" + digest)):
            raise RuntimeError("ColdSnap capsule identity is invalid")
        result.append((unit, reference, digest))
    return tuple(result)


def _model_payloads(document: dict[str, Any]) -> tuple[tuple[str, str, str], ...]:
    objects = document.get("weights", {}).get("model_payloads", {}).get("objects", [])
    if not isinstance(objects, list):
        return ()
    result: list[tuple[str, str, str]] = []
    for value in objects:
        if not isinstance(value, Mapping) or value.get("role") != "model-weight-payload":
            continue
        owner = value.get("owner")
        path = value.get("path")
        digest = value.get("sha256")
        match = _PAYLOAD_PATH.fullmatch(path) if isinstance(path, str) else None
        if (
            not isinstance(owner, str)
            or not owner.startswith("worker/")
            or not _SAFE_ID.fullmatch(owner.removeprefix("worker/"))
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
            or match is None
            or digest != "sha256:" + match.group(1)
        ):
            raise RuntimeError("ColdSnap native model-payload inventory is invalid")
        result.append((owner, path, digest))
    return tuple(result)


def _published_repository(reference: str) -> str:
    if "@sha256:" not in reference:
        return ""
    repository = reference.rpartition("@")[0]
    return repository if repository and not any(character in repository for character in " \t\r\n\x00") else ""


def _capsule_tag(repository: str, capture_id: str, driver: str, unit: str) -> str:
    slug = _SAFE_TAG.sub("-", capture_id.lower()).strip(".-") or "capture"
    slug = slug[:100]
    unit_slug = _SAFE_TAG.sub("-", unit.lower()).strip(".-") or "unit"
    driver_slug = _SAFE_TAG.sub("-", driver.lower()).strip(".-") or "driver"
    return "%s:%s-%s-unit-%s" % (repository, slug, driver_slug, unit_slug)


def _delete_huggingface_payloads(deletion: NativeDeletion) -> None:
    token = resolve_hf_token()
    if not token:
        raise RuntimeError("ColdSnap published native deletion requires Hugging Face authentication")
    from huggingface_hub import CommitOperationDelete, HfApi

    HfApi(token=token).create_commit(
        repo_id=deletion.repository,
        repo_type="model",
        revision=deletion.revision,
        operations=[CommitOperationDelete(path_in_repo=path) for path in deletion.paths],
        commit_message="Delete ColdSnap native payloads",
    )


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


_LOCAL_DELETE_PROGRAM = r"""import json
import os
from pathlib import Path
import re
import shutil
import sys

config = json.load(sys.stdin)
configured = config.get("state_root")
if configured:
    state_root = Path(configured).expanduser().resolve()
else:
    state_root = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")).resolve() / "sparkrun" / "coldsnap"
capture_base = (state_root / "captures").resolve()
payload_base = (state_root / "model-payloads").resolve()
safe_id = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
payload_path = re.compile(r"model-payloads/sha256/[0-9a-f]{64}\.pack")
repository_id = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

for capture_id, driver in config.get("captures", []):
    if not safe_id.fullmatch(capture_id) or driver not in {"n580", "n610"}:
        raise SystemExit("invalid ColdSnap capture deletion identity")
    target = (capture_base / capture_id / "drivers" / driver).resolve()
    try:
        target.relative_to(capture_base)
    except ValueError:
        raise SystemExit("ColdSnap capture deletion escapes its state root")
    if target.is_symlink():
        raise SystemExit("ColdSnap capture deletion target is a symlink")
    if target.is_dir():
        shutil.rmtree(target)
    for parent in (target.parent, target.parent.parent):
        try:
            parent.rmdir()
        except OSError:
            pass

for relative in config.get("payload_paths", []):
    if not isinstance(relative, str) or not payload_path.fullmatch(relative):
        raise SystemExit("invalid ColdSnap model-payload deletion path")
    target = (state_root / relative).resolve()
    try:
        target.relative_to(payload_base)
    except ValueError:
        raise SystemExit("ColdSnap model-payload deletion escapes its cache root")
    target.unlink(missing_ok=True)
    target.with_name(target.name + ".coldsnap-validation.json").unlink(missing_ok=True)

hf_cache_root = config.get("hf_cache_root")
if hf_cache_root:
    hub_root = (Path(hf_cache_root).expanduser().resolve() / "hub").resolve()
    for repository, revision, relative in config.get("huggingface_payloads", []):
        if (
            not isinstance(repository, str)
            or not repository_id.fullmatch(repository)
            or not isinstance(revision, str)
            or not re.fullmatch(r"[0-9a-f]{40}", revision)
            or not isinstance(relative, str)
            or not payload_path.fullmatch(relative)
        ):
            raise SystemExit("invalid Hugging Face native-payload deletion identity")
        repository_root = (hub_root / ("models--" + repository.replace("/", "--"))).resolve()
        snapshots = (repository_root / "snapshots").resolve()
        target = (snapshots / revision / relative).resolve(strict=False)
        lexical = snapshots / revision / relative
        try:
            lexical.resolve(strict=False).relative_to(repository_root)
        except ValueError:
            raise SystemExit("Hugging Face native-payload deletion escapes its cache root")
        blob = None
        if lexical.is_symlink():
            resolved = lexical.resolve()
            try:
                resolved.relative_to((repository_root / "blobs").resolve())
                blob = resolved
            except ValueError:
                raise SystemExit("Hugging Face native-payload symlink escapes its blob cache")
        lexical.unlink(missing_ok=True)
        lexical.with_name(lexical.name + ".coldsnap-validation.json").unlink(missing_ok=True)
        if blob is not None and blob.exists():
            retained = False
            if snapshots.is_dir():
                for candidate in snapshots.rglob("*"):
                    if candidate.is_symlink() and candidate.resolve() == blob:
                        retained = True
                        break
            if not retained:
                blob.unlink(missing_ok=True)
        parent = lexical.parent
        while parent != snapshots:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

print(json.dumps({
    "captures": len(config.get("captures", [])),
    "payloads": len(config.get("payload_paths", [])),
    "huggingface_payloads": len(config.get("huggingface_payloads", [])),
}))
"""


__all__ = [
    "DeletionPlan",
    "DeletionResult",
    "HostDeletion",
    "NativeDeletion",
    "build_deletion_plan",
    "execute_deletion_plan",
]
