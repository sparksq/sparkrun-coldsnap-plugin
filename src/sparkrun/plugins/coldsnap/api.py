# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.
"""Exact-job lifecycle API for supervisors; no CLI parsing or implicit launch."""

from __future__ import annotations

LIFECYCLE_API_VERSION = 1


def control_job(operation: str, job, *, sctx):
    import sparkrun.api as api
    from sparkrun.core.recipe import Recipe
    from sparkrun.plugins.coldsnap.service import ColdSnapService

    if operation not in {"status", "sleep", "wake"}:
        raise ValueError("Unsupported ColdSnap job operation")
    metadata = job.metadata or {}
    runtime = metadata.get("runtime_info") or {}
    if runtime.get("execution_strategy") != "coldsnap" or not runtime.get("capture_id"):
        raise ValueError("This job has no recorded ColdSnap activation receipt")
    if not job.hosts or not metadata.get("cluster") or not metadata.get("recipe_state"):
        raise ValueError("This job lacks the exact recipe and cluster metadata required for lifecycle control")
    recipe = Recipe._deserialize(metadata["recipe_state"])
    # Reuse the actual assigned serve port and parallelism, including auto-port.
    overrides = {key: metadata[key] for key in ("port", "tensor_parallel", "pipeline_parallel", "data_parallel") if key in metadata}
    options = api.RunOptions(
        recipe=recipe, hosts=tuple(job.hosts), cluster=metadata["cluster"], overrides=overrides, dry_run=True, follow=False, trust=False
    )
    plan = api.plan(options, sctx=sctx)
    if set(plan.host_list) != set(job.hosts):
        raise ValueError("ColdSnap lifecycle planning changed the recorded workload hosts")
    _, report = ColdSnapService().execute_lifecycle(
        operation,
        options,
        plan=plan,
        sctx=sctx,
        artifact=str(runtime.get("artifact") or ""),
        snapshot_driver=runtime.get("snapshot_driver") or None,
        expected_cluster_id=job.cluster_id,
        expected_capture_id=runtime["capture_id"],
    )
    return {"state": report["state"], "cluster_id": report["cluster_id"], "capture_id": report["capture_id"]}
