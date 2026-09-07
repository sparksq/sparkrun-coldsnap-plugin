<!--
SPDX-FileCopyrightText: 2026 Scitrera LLC
SPDX-FileCopyrightText: 2026 Fox Engine Ltd
SPDX-License-Identifier: AGPL-3.0-only
-->

# ColdSnap development preview

This guide is for early testers of the ColdSnap integration before it is
included in a normal sparkrun release. The preview uses the canonical plugin
source from this repository and an official sparkrun `develop-next` checkout.
The development setup assembles them as an in-tree plugin, matching the feature
and registry provenance that the eventual vendored release will use.

This is preview software. Expect rough edges, long first-time preparation, and
changes to commands, artifacts, or recipes while qualification continues. Do
not treat successful qualification as a service-level or performance guarantee.

## What you need

- A Linux control machine with Git and
  [uv](https://docs.astral.sh/uv/) installed.
- SSH access to `git@github.com:sparksq/sparkrun-coldsnap-plugin.git`.
- A working sparkrun cluster definition or explicit target hosts.
- NVIDIA driver 580 or newer on the target hosts.
- Docker and enough local disk and network capacity for model data, runtime
  images, and per-rank OCI capsules.
- For the example below, exactly two compatible target nodes. The qualified
  Qwen3.8 27B recipe is TP2 and declares both `min_nodes: 2` and `max_nodes: 2`.

Normal model and container authentication still applies. Make sure the target
hosts can access the recipe's pinned model, runtime image, and capsule
repository before treating a failure as a ColdSnap problem.

Since plugin 0.1.3, runtime build sources are fetched and commit-verified on the
control node, then staged on the Spark head. Private sources require repository
read access and working Git credentials on the control node; no GitHub keys or
credentials are copied to the targets. For an x64 controller driving ARM64
Spark nodes, use automatic or delegated transfer mode. See
[cross-architecture controllers](README.md#cross-architecture-controllers-since-013)
for platform selection, source access, and verification limits.

Use plugin 0.1.4 or newer with ColdSnap 0.3.21 for an x64 controller and ARM64
targets. Version 0.1.3 fixed retrieval but could still overlay AMD64 helpers on
ARM64 workloads. Version 0.1.4 resolves separate target-native helpers for CRIU
and native-pack verification; no manual Docker platform override is required.

## Install the preview

```bash
git clone git@github.com:sparksq/sparkrun-coldsnap-plugin.git
cd sparkrun-coldsnap-plugin
export SPARKRUN_BRANCH=develop-next
source dev.sh
```

`dev.sh` must be sourced, not executed, because it activates the repository's
virtual environment in the current shell. It performs the following work:

1. clones or updates the official sparkrun `develop-next` checkout;
2. assembles a disposable sparkrun tree with the live ColdSnap source linked
   into its in-tree plugin namespace;
3. installs the assembled host and plugin as editable packages;
4. activates the development environment;
5. runs `sparkrun registry update`, which fetches enabled plugin-declared
   registries such as `coldsnap`; and
6. installs the development hooks.

The official checkout under `.dev/sparkrun` is not modified. The disposable
assembly is under `.dev/sparkrun-with-coldsnap` and is rebuilt whenever the
script is sourced. Local edits under `src/sparkrun/plugins/coldsnap` are linked
into that assembly and are immediately visible to Python.

Run these checks after setup:

```bash
command -v sparkrun
sparkrun --version
sparkrun coldsnap --help
sparkrun registry list
```

`command -v sparkrun` should point inside this repository's `.venv`. The
registry list should include `coldsnap` with `plugin:coldsnap` as its source.
The matched `coldsnap-vanilla` registry is intentionally hidden and disabled by
default.

In each new shell, select `develop-next` and source the setup again:

```bash
export SPARKRUN_BRANCH=develop-next
source dev.sh
```

Re-sourcing also refreshes the selected sparkrun branch, development
installation, and enabled registries.

## Recipes

Qualified preview recipes live in the
[sparkrun recipes repository](https://github.com/sparksq/sparkrun-recipes/tree/main/coldsnap-recipes).
They are ordinary sparkrun recipes with two ColdSnap-specific choices:

- `builder: coldsnap` prepares a ColdSnap-aware runtime image; and
- a top-level `coldsnap:` section identifies the capsule source and optional
  policy. The published recipes use sane defaults for everything not stated
  explicitly.

A reduced example looks like this:

```yaml
recipe_version: "2"
model: Qwen/Qwen3.8-27B-FP8
runtime: vllm-distributed
min_nodes: 2
max_nodes: 2
builder: coldsnap

coldsnap:
  capsule:
    repository: docker.io/scitrera/coldsnap-qwen38-27b-capsules
```

The qualified files also pin the model revision, runtime image digest,
topology, environment, and engine arguments. Do not simplify those pins when
trying to reproduce the published results.

## First test: Qwen3.8 27B FP8 TP2 on vLLM

First materialize the recipe for the target cluster:

```bash
sparkrun coldsnap materialize @coldsnap/qwen3.8-27b-fp8-coldsnap-tp2-vllm
```

If there is no default cluster, name one explicitly:

```bash
sparkrun coldsnap materialize \
  --cluster <cluster-name> \
  @coldsnap/qwen3.8-27b-fp8-coldsnap-tp2-vllm
```

Materialization is intentionally not fast. It can pull large container and
capsule images, obtain and unpack model data, prepare driver-specific local
state, and perform a verification restore. Its duration depends heavily on
network bandwidth, existing caches, and image distribution. Treat it as a
preparation step, not as the startup time ColdSnap is intended to improve.

`materialize` is a one-shot preparation command, not a serving launch. It stops
its temporary native-weight/verification workloads before reporting success;
native packs, residual overlays, capsules, and reusable caches remain available
for subsequent launches. Repeating the command also stops its verification
workload when the assets already exist. Failures and Ctrl+C trigger cleanup;
an unconfirmed cleanup is reported with the temporary workload's exact ID.
As with capture/run, use available target hosts: launching preparation work can
replace an overlapping deployment of the same recipe. Cleanup itself targets
only the temporary launch, never a recipe-wide stop.

During an ordinary restore, `configuring native-weight caching for restore`
describes cache-policy setup, not a separate materialize command. Explicitly
required generation is labelled `configuring required native-weight generation`.

After materialization, run the recipe like any other sparkrun recipe:

```bash
sparkrun run @coldsnap/qwen3.8-27b-fp8-coldsnap-tp2-vllm
```

Or select the same cluster explicitly:

```bash
sparkrun run \
  --cluster <cluster-name> \
  @coldsnap/qwen3.8-27b-fp8-coldsnap-tp2-vllm
```

Ordinary lifecycle commands continue to apply:

```bash
sparkrun status --cluster <cluster-name>
sparkrun stop \
  --cluster <cluster-name> \
  @coldsnap/qwen3.8-27b-fp8-coldsnap-tp2-vllm
```

## Expected startup behavior

For the Qwen3.8 27B FP8 TP2 vLLM recipe, the plugin automatically selects the
newest snapshot driver supported by every
placed host: `n580` when the lowest NVIDIA driver major is 580–609, or `n610`
when every placed host is on 610 or newer.

Actual wall time observed from `sparkrun run` also includes manager-side work
before the container starts, such as validation, artifact checks, and any cache
misses. The TTFT metric begins at Docker
`State.StartedAt` and ends at the first non-empty streamed token; manager
preparation and capsule pulls are outside that measurement.

Plugin 0.1.6 pins ColdSnap 0.3.23 in [versions.yaml](versions.yaml), including
the required `runtime-v1` manager interface introduced in plugin 0.1.1. Historical
benchmark reports and raw logs are kept outside the public source repository;
they are not timing guarantees for this release.

Plugin 0.1.1 and ColdSnap 0.3.20 passed the existing-capsule TP2 driver matrix for
Qwen3.8 27B FP8 on vLLM and SGLang and DeepSeek V4 Flash 0731 on vLLM, on
both n580 and n610. The vLLM comparison separates default safetensors loading
(`auto`), InstantTensor, ColdSnap recovery, and ColdSnap native. SGLang uses
default loading, recovery, and native; its pinned image has no InstantTensor
loader. Three page-cache-cleared samples per supported cell passed exact
response validation, for 66 samples in total. This is same-placement restore
qualification using existing immutable captures, not new capture,
materialization, sleep/wake, cross-driver placement, or NVFP4/DSpark coverage.

The n610 restores use the current `preserve-nccl-exec` default. Their first
token is served only after restored CUDA graphs are ready. The n580 path remains
eager-first with asynchronous graph preparation. For vLLM on n580, recovery is
currently comparable to native weights in TP2 startup timing, so sparkrun's default
materialization policy favors a target-local residual overlay and leaves native
weights optional.

Use ColdSnap's
[maintained benchmark harnesses](https://github.com/sparksq/coldsnap/blob/main/benchmarks/harnesses/README.md)
to measure matched cases on your own qualified deployment. The
[runtime-neutral manager guide](https://github.com/sparksq/coldsnap/blob/main/docs/runtime-neutral-managers.md)
describes the manager contract and verification boundaries. Fresh captures and
live lifecycle operations require separate qualification from this TTFT matrix.

## Materialize and launch defaults

Ordinary ColdSnap `run`/`restore` launches default native-weight **generation**
to `off` for both engines on both drivers. This is explicit in the plugin's
request, including with older controllers that default vLLM to `async`.
It does not disable staging or consumption of existing verified native packs,
model/image preparation, or use of previously materialized local state.
Ordinary launches do not automatically capture fresh local residuals.

The dedicated `sparkrun coldsnap materialize` command has different defaults:

| Runtime | Driver | Ordinary launch: generate native packs | Explicit materialize: native weights | Explicit materialize: residual/runtime state |
| --- | --- | --- | --- | --- |
| vLLM | n580 | `off` | `off` (optional) | `required`: target-local recovery residual overlay |
| vLLM | n610 | `off` | `required` | `off`: reuse the existing capsule |
| SGLang | n580 | `off` | `required` | `required`: matching local capture/replay state |
| SGLang | n610 | `off` | `required` | `required`: matching local capture/replay state |

Explicit materialization prepares or reuses compatible assets, verifies them,
then stops its temporary serving workload and exits. Normal launches stay
serving after validation. For vLLM, native preparation uses a blocking
`required` restore-time writer when a pack must be generated. SGLang instead
generates packs during capture, then verifies a restore with generation `off`;
it does not support recovery-time write-behind on either driver.

To opt into vLLM native generation on n580, pass `--native-weights required`
to `materialize`. Explicit restore still accepts `--materialize-native async`
or `required` for vLLM; SGLang rejects those restore-time modes. These overrides
do not change the ordinary-launch default.

Weight **selection** is independent: when the recipe/command omits a weight
mode, vLLM/n580 defaults to `recovery`; the other three combinations default to
`auto` (prefer compatible verified native packs, otherwise recovery). Explicit
recipe/command modes are preserved. vLLM/n580 `auto` also selects recovery when
using a materialized recovery-only residual overlay; explicit `native` uses
the portable capsule instead. See the SGLang section below for its paired-state
requirements and recovery-only option.

## Cancellation cleanup

On cancellation, the plugin keeps the host-provider socket and transport
session alive while the controller/adapter clean up their own coordinator,
endpoint files, and failed/temporary serving containers. The controller tree
runs in a separate process group so manager-only and terminal-group interrupts
both reach it through the same shutdown path. Successful ordinary launches
continue serving; cancellation does not delete reusable native packs, residual
overlays, or capsules.

The controller allows its adapter up to four minutes for remote teardown and
capture-path ownership repair. The plugin allows five minutes before forcing
termination and closing the provider. Since plugin 0.1.6,
the first Ctrl-C requests graceful cancellation; a second Ctrl-C (or repeated
SIGTERM) forces the controller process group to terminate immediately instead
of waiting out that grace. The cancellation message explains this escape hatch.
Timeouts/forced termination are reported
as cleanup unconfirmed, and cleanup failures include the exact resource and
host rather than silently reporting success. SIGKILL, manager crashes, or
unreachable hosts can still require manual operation-scoped recovery.

ColdSnap 0.3.23 also interrupts in-flight
host-provider reads/writes when their context is cancelled. This lets a capsule
pull stop waiting promptly without closing the provider needed for cleanup.
Use plugin 0.1.6 with ColdSnap 0.3.23 or newer for both cancellation fixes;
plugin 0.1.5 / ColdSnap 0.3.22 do not include them.
Closing the transport terminates its local Docker/SSH clients; it does not
delete downloaded layers or guarantee that a remote Docker daemon immediately
stops all transfer activity.

This needs the corresponding controller and plugin changes together; an older
controller can kill its adapter before remote cleanup runs. A normal Sparkrun
serving-container stop alone does not include ColdSnap's operation-specific
coordinator. See the [controller ownership contract](https://github.com/sparksq/coldsnap/blob/main/docs/operator-integration.md#cancellation-and-coordinator-ownership).

## Explicit SGLang materialization (since 0.1.2)

Plugin 0.1.2 adds `materialize` support for SGLang on n580 and n610;
this was not available in v0.1.1. It uses an explicit capture
to generate native packs and matching runtime/replay state, verifies a native
restore, stops that verification workload, and only then selects the local
artifact for subsequent runs. It does not enable asynchronous/write-behind
materialization during ordinary recovery restores.

```bash
sparkrun coldsnap materialize --cluster <cluster-name> \
  @coldsnap/qwen3.8-27b-fp8-coldsnap-tp2-sglang
sparkrun run --cluster <cluster-name> \
  @coldsnap/qwen3.8-27b-fp8-coldsnap-tp2-sglang
```

Both `auto` options request the native pack and matching local runtime state.
Use `--native-weights off --residual-overlay required` for a recovery-only
local capture. `--native-weights required --residual-overlay off` still captures
the companion capsule/metadata needed to consume the native pack: SGLang does
not splice a newly generated pack into an older capture. This is not vLLM's
n580 pre-worker-import residual optimization and makes no equivalent timing
promise. Capture may replace an existing deployment for the same recipe.

The source descriptor is left unchanged. Local results are bound to that source,
the exact driver version, hardware and rank-ordered hosts. Normal `run` selects
them only on the matching target; an explicit restore `--artifact` bypasses
local selection. Repeating `materialize` re-verifies the existing local result
without recapturing. Missing runtime assets fail verification rather than
silently reporting success. Delete the recipe's local ColdSnap artifacts to
explicitly discard a stale materialization before rebuilding it.

The 0.1.2 materialization path was qualified separately with ColdSnap 0.3.20
using Qwen3.8 27B FP8 TP2: fresh native capture, repeated materialization without
recapture, and normal native inference passed on both n580 and n610. Fresh
recovery-only materialization and normal recovery inference also passed on
n580. Normal-run checks included exact response validation and lifecycle status
against the new capture. This does not extend the earlier TTFT matrix or claim
live sleep/wake, cross-driver placement, or other-model qualification.

## Terms and identity boundaries

These names distinguish orchestration roles and stored artifacts. In
particular, a snapshot driver is a versioned process-snapshot contract; it is
not merely another name for the installed NVIDIA driver.

| Term | Meaning |
| --- | --- |
| Manager | Placement-aware caller such as sparkrun or a future Kubernetes operator. |
| Controller | The engine-neutral Go `coldsnap` executable. |
| Engine adapter | A separate Go executable, `coldsnap-vllm-adapter` or `coldsnap-sglang-adapter`, that implements engine-selected distributed operations over shared orchestration. |
| Snapshot process driver | The `n580` or `n610` Go orchestration and driver contract. This is not the runtime coordinator. |
| Rank activation controller | Driver-specific Python that sequences one rank or launch unit through CRIU, CUDA, NCCL, hydration, and readiness. |
| Runtime coordinator | The shared, short-lived Go CSKV service used only for authenticated barriers and small key/value exchange. |
| Activation runtime | The adapter-embedded, content-addressed helper pack staged per host. Its Python files are mounted read-only for capture or restore; its adapter binary also exposes the host-side native-payload verifier. |
| Launch unit | One container or process-tree activation boundary. A unit may own more than one GPU worker. |
| Worker | One accelerator-owning engine process slot inside a launch unit. |
| Group | An ordered, engine-owned rank namespace such as tensor, pipeline, data, expert, or context parallelism. |
| Capsule | One unit's OCI image containing CRIU/CUDA/NCCL state, runtime residuals, and derived-cache seed data. |
| Model payload | Optional content-addressed model bytes, owned by a worker and stored outside the capsule. |
| Residual | Driver/runtime-specific non-model bytes needed to reconstruct the captured stable allocation layout. |
| Recovery provider | The pinned original Hugging Face safetensors plus a replay plan into the captured layout. |
| Snapshot driver | A named process-snapshot implementation (`n580` or `n610`), not merely the installed NVIDIA driver number. |

The canonical and more detailed version of this table is in the
[ColdSnap architecture document](https://github.com/sparksq/coldsnap/blob/main/docs/architecture.md#terms-and-identity-boundaries).

## Troubleshooting and feedback

If `sparkrun coldsnap` is missing, confirm that the current shell is using the
development environment and then source the setup again:

```bash
command -v sparkrun
export SPARKRUN_BRANCH=develop-next
source dev.sh
```

If `@coldsnap/...` does not resolve, refresh and inspect the registries:

```bash
sparkrun registry update
sparkrun registry list
```

Registry fetching is non-interactive. If an access-controlled repository or
mirror is configured, Git must already have usable credentials for its HTTPS or
SSH URL.

For useful feedback, include:

- the plugin commit from `git rev-parse HEAD`;
- the sparkrun commit from `git -C "$SPARKRUN_CHECKOUT" rev-parse HEAD`;
- `sparkrun --version` and the installed NVIDIA driver version;
- the recipe, cluster topology, and whether materialization had warm caches;
- the complete materialize or run output, including timing details; and
- the exact failure and the first operation that did not meet expectations.

Do not include access tokens, private keys, registry credentials, or other
secrets in a report.
