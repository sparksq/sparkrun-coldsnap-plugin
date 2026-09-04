# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Identity-derived local storage for committed ColdSnap descriptors."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from sparkrun.core.config import resolve_sparkrun_cache_dir
from sparkrun.orchestration.job_metadata import derive_recipe_fingerprint, generate_intent_id

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows has no process-level flock
    fcntl = None


DEFAULT_ARTIFACT_GENERATIONS = 2
_GENERATION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_RETENTION_ERROR = "plugins.coldsnap.artifact_generations must be a non-negative integer or 'unlimited'"


@dataclass(frozen=True)
class ArtifactStore:
    """Canonical descriptor locations for one exact recipe configuration."""

    root: Path
    current: Path
    imported: Path
    generations: Path
    pending: Path
    overlays: Path
    overlay_pending: Path

    def generation(self, capture_id: str) -> Path:
        return self.generations / _generation_filename(capture_id)

    def capture_output(self, capture_id: str) -> Path:
        """Return the managed staging path written by a capture process."""

        return self.pending / _generation_filename(capture_id)

    def overlay(self, target_key: str) -> Path:
        return self.overlays / target_key / "artifact.json"

    def overlay_record(self, target_key: str) -> Path:
        return self.overlays / target_key / "overlay.json"

    def overlay_capture_output(self, target_key: str, operation_id: str) -> Path:
        return self.overlay_pending / target_key / _generation_filename(operation_id)


def resolve_artifact_store(*, plan, options, sctx=None, snapshot_driver: str = "n610") -> ArtifactStore:
    """Resolve the placement-independent store for a planned recipe."""

    configured_cache = None
    if sctx is not None:
        configured_cache = getattr(getattr(sctx, "config", None), "cache_dir", None)
    cache_root = resolve_sparkrun_cache_dir(configured_cache).expanduser().resolve()
    overrides = getattr(options, "overrides", None)
    intent_id = plan.intent_id or generate_intent_id(plan.recipe, overrides)
    recipe_fingerprint = derive_recipe_fingerprint(plan.recipe, overrides)
    if snapshot_driver not in {"n580", "n610"}:
        raise ValueError("snapshot_driver must be n580 or n610")
    root = cache_root / "coldsnap" / "artifacts" / intent_id / recipe_fingerprint / "drivers" / snapshot_driver
    return ArtifactStore(
        root=root,
        current=root / "current.json",
        imported=root / "imported.json",
        generations=root / "generations",
        pending=root / "pending",
        overlays=root / "overlays",
        overlay_pending=root / "overlay-pending",
    )


def resolve_generation_limit(config=None) -> int | None:
    """Resolve the user-level managed-generation limit.

    ``None`` means unlimited retention.  Recipes deliberately do not control
    this local storage policy.
    """

    value = DEFAULT_ARTIFACT_GENERATIONS
    settings_for = getattr(config, "plugin_settings", None)
    if callable(settings_for):
        settings = settings_for("coldsnap")
        if "artifact_generations" in settings:
            value = settings["artifact_generations"]
    if isinstance(value, str) and value.strip().lower() == "unlimited":
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(_RETENTION_ERROR)
    return value


def promote_generation(
    capture_output: Path,
    store: ArtifactStore,
    *,
    keep_generations: int | None = DEFAULT_ARTIFACT_GENERATIONS,
    generation_id: str | None = None,
) -> tuple[Path, ...]:
    """Validate, activate, and prune one managed descriptor generation.

    Promotion is serialized per recipe fingerprint.  The capture process
    writes into ``pending`` so another promotion cannot prune an in-flight
    capture.  Only after the committed descriptor becomes ``current.json`` do
    we remove older immutable generations.
    """

    keep_generations = _validate_generation_limit(keep_generations)
    capture_output = _absolute(capture_output)
    pending = _absolute(store.pending)
    if capture_output.parent != pending:
        raise ValueError("capture output is not in the managed ColdSnap pending directory")

    with _store_lock(store):
        payload, artifact = _read_committed_artifact(capture_output)
        capture_id = artifact.get("capture_id")
        if not isinstance(capture_id, str):
            raise RuntimeError("capture output has no valid ColdSnap capture ID")
        generation_id = generation_id or capture_id
        if capture_output != _absolute(store.capture_output(generation_id)):
            raise RuntimeError("pending descriptor path does not match its ColdSnap generation ID")

        _private_directory(store.generations)
        generation = _absolute(store.generation(generation_id))
        if os.path.lexists(generation):
            raise RuntimeError("ColdSnap artifact generation already exists: %s" % generation)
        os.replace(capture_output, generation)
        os.chmod(generation, 0o600)
        activated_ns = _next_activation_ns(store.generations)
        os.utime(generation, ns=(activated_ns, activated_ns), follow_symlinks=False)
        _fsync_directory(store.generations)

        _atomic_write(store.current, payload)
        removed = _prune_generations(store.generations, keep_generations)
        _fsync_directory(store.generations)
        return removed


def remove_artifact_store(store: ArtifactStore) -> tuple[Path, ...]:
    """Remove one exact recipe/driver descriptor store under its lock.

    The caller must derive *store* through :func:`resolve_artifact_store`.
    Symlinks are unlinked rather than followed, and an unexpected filesystem
    object fails closed before the store root is removed.
    """

    root = _absolute(store.root)
    removed: list[Path] = []
    if not os.path.lexists(root):
        return ()
    metadata = root.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise RuntimeError("ColdSnap artifact store root is not a real directory: %s" % root)
    with _store_lock(store):
        for path in (
            store.current,
            store.imported,
            store.generations,
            store.pending,
            store.overlays,
            store.overlay_pending,
        ):
            target = _absolute(path)
            try:
                target.relative_to(root)
            except ValueError as error:
                raise RuntimeError("ColdSnap artifact store member escapes its root: %s" % target) from error
            if not os.path.lexists(target):
                continue
            value = target.lstat()
            if stat.S_ISDIR(value.st_mode) and not stat.S_ISLNK(value.st_mode):
                shutil.rmtree(target)
            else:
                target.unlink()
            removed.append(target)
    lock = root / ".lock"
    if os.path.lexists(lock):
        value = lock.lstat()
        if not stat.S_ISREG(value.st_mode):
            raise RuntimeError("ColdSnap artifact store lock is not a regular file: %s" % lock)
        lock.unlink()
        removed.append(lock)
    unexpected = tuple(root.iterdir())
    if unexpected:
        raise RuntimeError("ColdSnap artifact store contains unexpected entries: %s" % ", ".join(str(path) for path in unexpected))
    root.rmdir()
    removed.append(root)
    return tuple(removed)


def _generation_filename(capture_id: str) -> str:
    if not isinstance(capture_id, str) or not _GENERATION_ID.fullmatch(capture_id):
        raise ValueError("ColdSnap capture ID is not safe for an artifact filename")
    return capture_id + ".json"


def _validate_generation_limit(value: int | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(_RETENTION_ERROR)
    return value


def _read_committed_artifact(path: Path) -> tuple[bytes, dict]:
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError("capture output is not a regular file")
        payload = path.read_bytes()
        artifact = json.loads(payload)
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("cannot read committed artifact generation %s: %s" % (path, error)) from error
    if not isinstance(artifact, dict) or artifact.get("kind") != "coldsnap-snapshot-artifact" or artifact.get("state") != "committed":
        raise RuntimeError("capture output is not a committed ColdSnap artifact")
    return payload, artifact


@contextmanager
def _store_lock(store: ArtifactStore):
    root = _absolute(store.root)
    _private_directory(root)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    descriptor = os.open(root / ".lock", flags, 0o600)
    try:
        os.chmod(root / ".lock", 0o600)
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _private_directory(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _atomic_write(destination: Path, payload: bytes) -> None:
    # Keep the destination lexical: resolving an existing ``current.json``
    # symlink would replace its target rather than replacing the link itself.
    destination = _absolute(destination)
    _private_directory(destination.parent)
    descriptor, temporary = tempfile.mkstemp(prefix=".current-", suffix=".json", dir=destination.parent)
    try:
        with os.fdopen(descriptor, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
        _fsync_directory(destination.parent)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def _next_activation_ns(directory: Path) -> int:
    """Return a strictly increasing activation timestamp for this store."""

    latest = 0
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                continue
            latest = max(latest, entry.stat(follow_symlinks=False).st_mtime_ns)
    return max(time.time_ns(), latest + 1)


def _prune_generations(directory: Path, keep_generations: int | None) -> tuple[Path, ...]:
    if keep_generations is None:
        return ()
    candidates: list[tuple[int, str, Path]] = []
    with os.scandir(directory) as entries:
        for entry in entries:
            if not entry.name.endswith(".json") or not entry.is_file(follow_symlinks=False):
                continue
            metadata = entry.stat(follow_symlinks=False)
            candidates.append((metadata.st_mtime_ns, entry.name, Path(entry.path)))
    candidates.sort(reverse=True)
    removed = []
    for _activated, _name, path in candidates[keep_generations:]:
        path.unlink()
        removed.append(path)
    return tuple(removed)


def _absolute(path: Path) -> Path:
    return Path(os.path.abspath(path.expanduser()))


def _fsync_directory(path: Path) -> None:
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ArtifactStore",
    "DEFAULT_ARTIFACT_GENERATIONS",
    "promote_generation",
    "remove_artifact_store",
    "resolve_artifact_store",
    "resolve_generation_limit",
]
