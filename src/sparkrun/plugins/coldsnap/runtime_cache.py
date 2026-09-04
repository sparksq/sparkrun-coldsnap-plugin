# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Bridge sparkrun's persistent runtime cache into portable ColdSnap capsules."""

from __future__ import annotations

import logging
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from sparkrun.core.launcher import resolve_effective_runtime_cache_dir
from sparkrun.core.progress import PROGRESS
from sparkrun.core.runtime_cache import (
    build_runtime_cache_mounts,
    probe_image_identity,
    resolve_runtime_cache_root,
    resolve_runtime_cache_settings,
    runtime_cache_disabled_by_env,
)
from sparkrun.orchestration.job_metadata import derive_recipe_fingerprint
from sparkrun.orchestration.primitives import build_ssh_kwargs
from sparkrun.orchestration.ssh import run_remote_script
from sparkrun.utils.shell import quote


logger = logging.getLogger(__name__)
RunRemote = Callable[..., Any]
CANONICAL_RUNTIME_CACHE_ROOT = "/var/cache/coldsnap/runtime"


@dataclass(frozen=True)
class StagedRuntimeCache:
    unit: str
    host: str
    image: str
    source: str
    path: str
    parent: str


@dataclass(frozen=True)
class CaptureRuntimeCacheStage:
    request: dict[str, Any]
    entries: tuple[StagedRuntimeCache, ...] = ()
    ssh_kwargs: dict[str, Any] | None = None


def _capture_staging_root(sparkrun_cache_dir: str) -> str:
    """Return a writable sibling for transient ColdSnap capture staging.

    The persistent runtime cache can legitimately contain root-owned files
    written by containers.  Keeping the private capture copy *under* that
    tree makes capture depend on the ownership of the ``sparkrun`` directory
    itself.  For the standard ``.../sparkrun`` layout, use the adjacent
    ``.../coldsnap`` namespace instead.  A custom cache root retains the old
    nested behavior because its owner and layout are explicitly managed by
    the operator.
    """
    cache = PurePosixPath(sparkrun_cache_dir.rstrip("/"))
    if cache.name == "sparkrun":
        return str(cache.parent / "coldsnap" / "runtime-cache-staging")
    return str(cache / "coldsnap" / "runtime-cache-staging")


def stage_capture_runtime_cache(
    request: dict[str, Any],
    *,
    options,
    plan,
    sctx,
    images_by_node=(),
    runner: RunRemote = run_remote_script,
) -> CaptureRuntimeCacheStage:
    """Copy each launch unit's shared cache leaf into a private writable tree.

    ColdSnap mounts the private tree at its canonical container cache root.
    Capture warmup may therefore add files without mutating the shared source,
    and the adapter can safely bake the completed tree into the OCI capsule.
    """
    cache_policy = request.get("policy", {}).get("cache", {})
    if request.get("operation") != "capture" or not cache_policy.get("seed", False):
        return CaptureRuntimeCacheStage(request=request)
    if not any(
        path == CANONICAL_RUNTIME_CACHE_ROOT or CANONICAL_RUNTIME_CACHE_ROOT.startswith(str(path).rstrip("/") + "/")
        for path in cache_policy.get("paths", ())
    ):
        return CaptureRuntimeCacheStage(request=request)

    runtime_cache_override = getattr(options, "runtime_cache", None)
    cli_override = None if runtime_cache_override is None else {"enabled": runtime_cache_override}
    settings = resolve_runtime_cache_settings(
        runtime=plan.runtime,
        config=sctx.config,
        cluster=plan.cluster,
        recipe=plan.recipe,
        cli_override=cli_override,
        env_disabled=runtime_cache_disabled_by_env(),
    )
    if not settings.enabled:
        return CaptureRuntimeCacheStage(request=request)

    ssh_kwargs = build_ssh_kwargs(sctx.config)
    if plan.cluster.user:
        ssh_kwargs = {**ssh_kwargs, "ssh_user": plan.cluster.user}
    operation_id = str(request["id"])
    fingerprint = derive_recipe_fingerprint(plan.recipe, options.overrides)
    cache_dirs: dict[str, str] = {}
    entries_list: list[StagedRuntimeCache] = []
    for unit in request["launch"]["units"]:
        host = str(unit["host"])
        if host not in cache_dirs:
            cache_dirs[host] = resolve_effective_runtime_cache_dir(
                [host],
                ssh_kwargs,
                sctx.config,
                dry_run=False,
                cluster=plan.cluster,
            )
        cache_dir = cache_dirs[host]
        root = resolve_runtime_cache_root(settings, cache_dir)
        image = str(unit.get("image") or plan.recipe.container or "")
        mounts = build_runtime_cache_mounts(
            runtime=plan.runtime,
            recipe=plan.recipe,
            settings=settings,
            root=root,
            image=image,
            image_identity=(probe_image_identity(image, [host], ssh_kwargs, dry_run=False) if settings.key_by_image and image else None),
            fingerprint=fingerprint,
        )
        if mounts is None:
            return CaptureRuntimeCacheStage(request=request)
        parent = "%s/%s" % (_capture_staging_root(cache_dir), operation_id)
        unit_id = str(unit["id"])
        entries_list.append(
            StagedRuntimeCache(
                unit=unit_id,
                host=host,
                image=image,
                source=mounts.leaf,
                path="%s/units/%s" % (parent, unit_id),
                parent=parent,
            )
        )
    entries = tuple(entries_list)

    def stage_one(entry: StagedRuntimeCache):
        script = "\n".join(
            [
                "set -eu",
                "source=%s" % quote(entry.source),
                "destination=%s" % quote(entry.path),
                "parent=%s" % quote(entry.parent),
                'if [ -e "$destination" ]; then',
                '  echo "ColdSnap runtime-cache staging destination already exists: $destination" >&2',
                "  exit 73",
                "fi",
                'if ! install -d -m 0700 -- "$parent" "$destination"; then',
                '  echo "ColdSnap runtime-cache staging root is not writable by $(id -un): $parent" >&2',
                "  exit 73",
                "fi",
                'if [ -d "$source" ]; then',
                # Container-written shared caches may be root-owned. Preserve
                # content and modes, but make the private copy belong to the
                # SSH user so it remains removable after capture.
                '  cp -a --no-preserve=ownership --reflink=auto -- "$source"/. "$destination"/',
                "fi",
            ]
        )
        result = runner(
            entry.host,
            script,
            timeout=1800,
            quiet=True,
            allow_local=True,
            session_guard=True,
            **ssh_kwargs,
        )
        if result.returncode:
            detail = str(result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                "stage ColdSnap runtime cache for unit %s on %s failed%s"
                % (entry.unit, entry.host, ": " + detail[-1000:] if detail else "")
            )
        return entry

    logger.log(PROGRESS, "ColdSnap: staging runtime cache for %d launch unit(s)", len(entries))
    for entry in entries:
        logger.info("ColdSnap runtime-cache unit %s on %s: %s -> %s", entry.unit, entry.host, entry.source, entry.path)

    try:
        with ThreadPoolExecutor(max_workers=max(1, min(32, len(entries)))) as executor:
            futures = {executor.submit(stage_one, entry): entry for entry in entries}
            for future in as_completed(futures):
                future.result()
    except Exception:
        partial = CaptureRuntimeCacheStage(
            request=request,
            # A failed copy may already have created its destination, and the
            # executor waits for sibling futures before leaving the context.
            # Every path is unique to this operation, so clean the complete
            # planned inventory rather than only futures observed as done.
            entries=entries,
            ssh_kwargs=ssh_kwargs,
        )
        cleanup_capture_runtime_cache(partial, runner=runner)
        raise

    logger.log(PROGRESS, "ColdSnap: runtime cache staged for %d launch unit(s)", len(entries))

    staged_request = deepcopy(request)
    staged_request["policy"]["cache"]["staged"] = [{"unit": entry.unit, "path": entry.path} for entry in entries]
    return CaptureRuntimeCacheStage(
        request=staged_request,
        entries=entries,
        ssh_kwargs=ssh_kwargs,
    )


def cleanup_capture_runtime_cache(
    stage: CaptureRuntimeCacheStage,
    *,
    runner: RunRemote = run_remote_script,
) -> None:
    """Best-effort removal of the exact private trees created for a capture."""
    if not stage.entries:
        return
    ssh_kwargs = stage.ssh_kwargs or {}

    def cleanup_one(entry: StagedRuntimeCache):
        script = "\n".join(
            [
                "set -eu",
                "destination=%s" % quote(entry.path),
                "parent=%s" % quote(entry.parent),
                "image=%s" % quote(entry.image),
                'if ! rm -rf -- "$destination"; then',
                # The ColdSnap rank controller must be root for CRIU. Current
                # adapters normalize the bind before returning, but retain a
                # manager-side recovery path for interrupted operations.
                '  owner="$(id -u):$(id -g)"',
                '  docker run --rm --pull never --network none --user 0:0 --volume "$destination:/run/coldsnap-managed" --entrypoint chown "$image" -R "$owner" /run/coldsnap-managed',
                '  rm -rf -- "$destination"',
                "fi",
                'rmdir -- "$parent" 2>/dev/null || true',
            ]
        )
        result = runner(
            entry.host,
            script,
            timeout=300,
            quiet=True,
            allow_local=True,
            **ssh_kwargs,
        )
        if result.returncode:
            logger.warning(
                "Could not remove ColdSnap runtime-cache staging tree for unit %s on %s: %s",
                entry.unit,
                entry.host,
                str(result.stderr or result.stdout or "").strip()[-1000:],
            )

    with ThreadPoolExecutor(max_workers=max(1, min(32, len(stage.entries)))) as executor:
        futures = [executor.submit(cleanup_one, entry) for entry in stage.entries]
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                logger.warning("Could not remove a ColdSnap runtime-cache staging tree", exc_info=True)


__all__ = [
    "CANONICAL_RUNTIME_CACHE_ROOT",
    "CaptureRuntimeCacheStage",
    "StagedRuntimeCache",
    "cleanup_capture_runtime_cache",
    "stage_capture_runtime_cache",
]
