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

Plugin 0.1.1 pins ColdSnap 0.3.20 in [versions.yaml](versions.yaml), including
the required `runtime-v1` manager interface. Upgrade both together. Historical
benchmark reports and raw logs are kept outside the public source repository;
they are not timing guarantees for this release.

The paired releases passed the existing-capsule TP2 driver matrix for
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
