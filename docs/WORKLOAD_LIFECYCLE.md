# Shared workload lifecycle provider

On a host exposing the version-1 lifecycle extension, ColdSnap registers
`WorkloadLifecycleProvider("coldsnap", matches_job, control_job)` during normal
plugin loading. Registration is owned by the loader's plugin identity. Older hosts
without the extension retain the existing ColdSnap API and CLI behavior.

Matching requires recorded `runtime_info.execution_strategy: coldsnap` and a
capture ID. `control_job(action, job, sctx=...)` accepts `status`, `sleep`, or `wake`
and reuses the exact recorded recipe, cluster, hosts, assigned port and parallelism.
It passes the expected workload and capture IDs to ColdSnap's existing lifecycle
service. Receipt verification, checkpointing and restore remain ColdSnap behavior;
the host handles caller ownership, exact revision, reservations and recovery.

The result contains `state`, `cluster_id` and `capture_id`. The host rejects a
provider response for another workload and keeps pending state when an action
fails. Provider feedback uses the supplied operation context, so daemon job events
retain structured progress and operation-scoped log forwarding.

There is no separate ColdSnap daemon namespace yet. Clients use core
`workloads.status/sleep/wake` for daemon-owned workloads, or SparkRoute's
`plugins.sparkroute.workloads.*` policy adapters for SparkRoute-owned workloads.
Recorded inventory is not a live provider health check. This addition does not
change capture formats or download/restore behavior.

The plugin regression suite covers existing lifecycle and receipt behavior. Actual
GPU restore and native-platform service containment still require the deployment
validation matrix; local daemon integration tests do not substitute for those.
