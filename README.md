<!--
SPDX-FileCopyrightText: 2026 Scitrera LLC
SPDX-FileCopyrightText: 2026 Fox Engine Ltd
SPDX-License-Identifier: AGPL-3.0-only
-->

# sparkrun ColdSnap plugin

ColdSnap integrates process snapshots into sparkrun to reduce model startup
work. It captures and restores vLLM and SGLang workloads on qualified Linux
NVIDIA GPU clusters, with native-weight and recovery loading paths.

The plugin provides runtime-image preparation, capture, restore, explicit
materialization, and lifecycle control through sparkrun. Ordinary launches use
existing prepared assets and default native-weight generation to `off`;
materialization prepares assets, verifies a restore, and stops its temporary
service.

Start with the [preview setup and first-run guide](DEV_PREVIEW.md). See
[materialize and launch defaults](DEV_PREVIEW.md#materialize-and-launch-defaults)
for the runtime/driver matrix and
[cancellation cleanup](DEV_PREVIEW.md#cancellation-cleanup) for interrupt behavior.
See the [release notes](https://github.com/sparksq/sparkrun-coldsnap-plugin/releases)
for version history and [versions.yaml](versions.yaml) for plugin and controller
pins.

This repository is the canonical development home of sparkrun's first-party
ColdSnap integration. Released sparkrun distributions vendor an immutable
snapshot of this repository; they do not clone or install it at build time or
runtime.

## Sparkrun integration

The plugin is imported by sparkrun as `sparkrun.plugins.coldsnap` and remains
subject to sparkrun's `plugins.coldsnap` feature gate. The source repository
and exact commit included by a sparkrun release are recorded in that release's
`vendor/coldsnap.lock` and packaged `VENDORED.toml` files.

When the host supports plugin-declared registry overlays, enabling this plugin
also contributes the recipes from
`https://github.com/sparksq/sparkrun-recipes.git`:

- `@coldsnap/...` resolves qualified ColdSnap recipes from
  `coldsnap-recipes/`. The registry is enabled but hidden from ordinary recipe
  listings.
- `@coldsnap-vanilla/...` resolves the matched controls from
  `vanilla-recipes/`. This registry is hidden and disabled by default.

These are runtime overlays, not edits to the user's `registries.yaml`. A user
can still disable, remove, trust, or repoint either registry through the normal
sparkrun registry commands.

## Host compatibility

The plugin targets Sparkrun 0.4.0 alpha and newer, with the package range
`>=0.4.0,<0.5` declared in
[plugin.toml](plugin.toml) and [pyproject.toml](pyproject.toml). The plugin declares
API version 1. Capture requires the shared
`core.image_preparation.stage_prepared_images`/`StagedImageSet` contract to stage
prepared images and resolve immutable image identities on every host. Launch
also requires the host's `RunPlan.host_hardware` and
`PreparedExecution.host_hardware` observation handoff.

Capture uses operation-local recipe and SSH settings and replaces the current
workload before submitting its containers. Both n580 and n610 request
privileged, unconfined checkpoint-controller containers through the manager
runtime; Sparkrun's default io_uring seccomp profile does not replace that
requirement.

Sparkrun probes hardware before placement and the memory-fit summary, then
ColdSnap reuses those operation-local observations for both snapshot drivers.
Standalone ColdSnap operations probe when no planning observations are available. The operation keeps configured memory budgets and physical GPU
assignments and leaves saved inventory unchanged. Reports distinguish detected
GPU/driver identity, measured or estimated memory capacity, and discovered RDMA
interfaces. Interface discovery alone does not verify peer connectivity. The
existing control-to-fabric SSH reachability check is reused for transfer routing;
it does not test the RDMA data path.

When an existing distributed vLLM capture contains the former host default
`OMP_NUM_THREADS=4`, restore and lifecycle requests retain it if the current
configuration leaves it unset. Explicit values still undergo launch-identity
validation. New captures use the current host's thread policy.

## Startup timing

Running restores report Docker-start-to-port-open, Docker-start-to-HTTP-health,
and Docker-start-to-first-nonempty-token timings, observed on rank 0. ColdSnap's
acceptance request streams and validates the final reply. When the host supports
startup observations, the plugin passes that measurement to it to avoid a
second inference. Warm restores do not report TTFT.

These durations overlap and must not be summed. Port/HTTP health can precede
real inference, and all three measurements exclude preparation before container
start. Compare matched profiles using the
[benchmark harnesses](https://github.com/sparksq/coldsnap/blob/main/benchmarks/harnesses/README.md).
Controllers without timing receipts provide no measurements. Normal Sparkrun
`--no-follow` behavior stays non-blocking.

## Development

Set up and activate the development environment from the repository root:

```bash
source dev.sh
pytest
```

By default, `dev.sh` clones or updates the official sparkrun repository at
`https://github.com/spark-arena/sparkrun.git`, selects its `main` branch, and
keeps that managed checkout under `.dev/sparkrun`. To test against a different
official branch, set `SPARKRUN_BRANCH` before sourcing the script:

```bash
export SPARKRUN_BRANCH=develop-next
source dev.sh
```

The branch is re-evaluated every time `dev.sh` is sourced, so changing
`SPARKRUN_BRANCH` and sourcing it again updates the managed checkout even
though the previous setup exported its resolved `SPARKRUN_CHECKOUT` path.

To use an existing local checkout instead, set `SPARKRUN_CHECKOUT`:

```bash
export SPARKRUN_CHECKOUT=/path/to/sparkrun
source dev.sh
```

An explicit checkout takes precedence over `SPARKRUN_BRANCH` and is never
fetched, switched, or otherwise modified by the script. `dev.sh` exports the
resolved `SPARKRUN_CHECKOUT`, creates or updates this repository's `.venv`,
and assembles a disposable host at `.dev/sparkrun-with-coldsnap`. That assembly
copies the selected host, links this checkout's live plugin source into its
`sparkrun.plugins` tree, and adds the `plugins.coldsnap` feature binding when
the selected host does not have it yet. The original host checkout is never
modified. The script installs the assembled host and this plugin as editable
packages, activates the virtual environment, updates all enabled recipe
registries (including plugin-declared registries), and installs the pre-commit
hooks. As with sparkrun's normal install and upgrade flows, a registry update
failure is reported but does not fail environment setup.

The development shell defaults `SPARKRUN_FEATURE_PLUGINS_COLDSNAP` to `1`,
enabling the live plugin before registry updates. An explicit value, such as
`0` when testing the disabled integration, is preserved.

The disposable assembly deliberately exercises sparkrun's in-tree loader. The
plugin and its registry declarations therefore have the same in-tree provenance
and trust boundary they have after commit-pinned vendoring; the development
workflow does not route ColdSnap through `core.external_plugins`. If the host
already provides the ColdSnap binding, the assembly keeps it, while the source
link makes local plugin edits live.

The test bootstrap makes this repository's plugin source take precedence over
the copy vendored by the selected sparkrun checkout. This keeps changes local
to this repository while exercising them against the real host implementation.

To run tests directly against another checkout, use an environment with that
host's dependencies and clear any previous assembled-host override:

```bash
env -u SPARKRUN_DEV_CHECKOUT SPARKRUN_CHECKOUT=/path/to/sparkrun \
  .venv/bin/python -m pytest
```

The pytest header reports the host version and source path.

`versions.yaml` is authoritative for both the plugin release version and the
default ColdSnap controller version. After changing either value, regenerate
the version-bearing files and verify that the checkout is synchronized:

```bash
python scripts/update-versions.py
python scripts/update-versions.py --check
```

Changes are made and tested here first. sparkrun then imports an approved full
commit with its `scripts/vendor-coldsnap.py` command. Files in sparkrun's
vendored source and test directories should not be edited directly.

## CI and releases

Scitrera repo-tools generates the Python test and version-check workflows from
`versions.yaml`. Its source is pinned to an immutable commit in the catalog and
both script shims. After editing the CI configuration, regenerate and check:

```bash
python scripts/generate-ci-gha.py --force
python scripts/generate-ci-gha.py --check
```

Pushes to `main` and pull requests targeting `main` run Ruff and the test suite
on Python 3.12 and 3.13 against an immutable Sparkrun 0.4 `develop-next` commit,
pinned in the install command in `versions.yaml`. The macOS workflow uses the
same host commit. Each matrix job
also checks version synchronization and generated-workflow drift. The optional
Go/Python and local Docker integration tests require the environment variables
described below and are skipped on these standard CI runners; GPU qualification
is separate.

Pushing a `v*.*.*` tag invokes `.github/workflows/release.yml`. It reuses the same
test matrix, requires the tag to match the plugin version in `versions.yaml`,
builds and checks the wheel and source distribution, and creates a GitHub release
with both distributions and SHA-256 checksums only after all gates pass. The
release workflow is repository-owned because repo-tools' generated Python
publisher also uploads to PyPI; this repository does **not** publish to PyPI.
Only the final release job receives `contents: write`; the built-in GitHub token
is sufficient, with no PyPI credentials or external publishing secrets needed.

For a release, update `versions.yaml`, run version synchronization and the checks
above, merge the reviewed commit to `main`, wait for CI, then push the matching
tag. A plugin-only CI or documentation change does not require changing the
ColdSnap controller pin. Sparkrun's commit-pinned vendoring remains the supported
distribution path; GitHub release assets do not replace that approval process.

## Controller acquisition

The plugin resolves the release-pinned ColdSnap controller and both engine
adapters as one verified tool set, plus CRIU RPC on Linux. It checks, in
order:

1. the local sparkrun tool cache;
2. the pinned GitHub release and its `checksums.txt`;
3. `docker.io/scitrera/coldsnap-binaries:<version>` and its platform-specific
   bundle manifest; and
4. an exact source-tag build using controller-side Git and the source-pinned Go container.

The Linux OCI and source-build fallbacks require Docker on the sparkrun control
node. macOS OCI acquisition reads the registry directly without Docker.
The public OCI bundle needs no registry credentials. Sites mirroring either
source may override it without changing recipes:

```yaml
plugins:
  coldsnap:
    controller:
      repository: sparksq/coldsnap
      oci_repository: docker.io/scitrera/coldsnap-binaries
```

Every acquisition path verifies the configured release version and full Git
commit, the target OS and architecture, and the SHA-256 digest of every
executable (four on Linux, three on macOS) before activating the cache generation.

## Runtime-neutral manager support

The provider advertises `runtime-v1` and delegates typed image/workload
operations to `DockerManagerRuntime`; `runtime_factory` permits an alternate
manager backend.
Sparkrun owns its workload labels and all registry credentials. Both engine
adapters and both snapshot drivers use the same boundary.

The full design and current limitations are documented in ColdSnap's
[runtime-neutral manager contract](https://github.com/sparksq/coldsnap/blob/main/docs/runtime-neutral-managers.md).
Kubernetes is not yet implemented or
qualified. Manager-side builders, OCI acquisition, cache helpers, and deletion
remain Docker-based; this is not a Docker-free Sparkrun distribution.

For integration testing, build ColdSnap locally and point a
separate development Sparkrun configuration at the sibling controller tools:

```yaml
plugins:
  coldsnap:
    controller:
      path: /path/to/coldsnap/bin/coldsnap
      download: false
```

The controller's vLLM/SGLang adapters must be available beside it, along with
CRIU RPC on Linux. The default acquisition path uses the exact controller release and
source commit pinned by this plugin. Use a separate Python environment when
testing local overrides so the working plugin installation is unaffected.

Run `tests/test_manager_runtime.py` for backend and wire-contract tests.
Set `COLDSNAP_SOURCE_ROOT` and `COLDSNAP_GO` to enable the Go/Python round trip;
also set `COLDSNAP_TEST_DOCKER_IMAGE` to a locally cached shell image (for example
`busybox:1.37`) to enable the isolated container and image-build smoke tests.
These tests create only uniquely named local test resources, clean them up,
and do not request GPUs or publish images.

## Cross-architecture controllers

### Control-node platforms

Control nodes can run Linux or macOS on AMD64 or ARM64. GPU targets must be
qualified Linux hosts. Native Windows controllers are not supported.

The managed controller and its two engine adapters are native to the control
node. macOS installs no local CRIU executable; the plugin separately acquires
Linux CRIU and payload-verifier tools for the targets, including when both the
Mac and the targets are ARM64.

GitHub release assets are tried first. The second source is the same
`docker.io/scitrera/coldsnap-binaries:<version>` index for all four platforms:
`linux/amd64`, `linux/arm64`, `darwin/amd64`, and `darwin/arm64`. macOS payloads
are downloaded directly through the registry API, not through a Linux container.
This path needs neither Docker nor ORAS; it verifies descriptor digests, platform,
release identity, file hashes, and Mach-O architecture. It supports anonymous
public pulls and existing Docker credentials for authenticated pulls.

Other operations, such as capsule-descriptor extraction and the final pinned
source-build fallback, still use Docker. On macOS those require a working Linux
Docker engine (for example Docker Desktop), plus the normal Sparkrun Git/SSH
and file-transfer tools. Runtime builds remain on the Linux GPU head. The source
fallback uses a Linux Go builder with `GOOS=darwin`, not a Darwin container.
An explicit development controller needs a separate verified Linux target bundle
or the two `COLDSNAP_TARGET_*` overrides; it cannot reuse Mac sibling binaries.

### Target platforms and source access

Control and target nodes do not need the same CPU architecture. Each resource
uses the platform where it runs:

| Resource | Platform used |
| --- | --- |
| Controller tools | Native control-node platform, such as `linux/amd64` or `darwin/arm64` |
| Remotely executed CRIU helper and payload verifier | Target platform, such as `linux/arm64` |
| Capture/runtime/NCCL images | Target Docker platform, such as `linux/arm64` |
| Descriptor-only OCI image | Its declared image platform; only `create`/`cp`, never execution |

Use automatic or delegated transfer mode for a separate x64 controller and
Spark cluster. Runtime builds execute on the Spark head. An explicitly local
cross-architecture build is rejected before pulling images; emulation is not a
substitute for the target GPU. Docker pulls, probes and builds request the
resolved platform explicitly, even when `DOCKER_DEFAULT_PLATFORM` differs.
Descriptor and controller-bundle extraction use immutable references after
resolution instead of reusing a mutable tag.

Source checkouts are fetched and commit-verified on the control node, then
staged temporarily on the build host using the cluster's existing SSH/rsync
connection. They are removed after the build. GitHub credentials and keys are
not copied or forwarded. Public sources use HTTPS. Private sources require
read access on the control node through a Git credential helper, `gh auth
login`, or existing trusted SSH access; the plugin does not disable host-key
verification or change Git's global configuration. Every failed fetch stops
before checkout or build. Public NCCL source fallback remains on the build host.

Target binaries use the same release, commit, and checksum verification as the
controller bundle, but are not executed on the control node. Target platform
probes and ELF checks reject mismatches before controller invocation; ColdSnap
also checks every target and the staged verifier's running release identity
before launching capture/restore workloads. Native-pack staging checks the
verifier's release identity remotely as well. Source-build fallback uses a
controller-native Go container with explicit `GOOS`/`GOARCH` for the target.

Both engines and drivers use this separation. Each operation requires one
common Linux target CPU architecture. A configured development controller can
use `plugins.coldsnap.controller.target_path` to point to a local directory
containing the matching target bundle and `manifest.json`. For an explicit
`--coldsnap-binary`, cross-architecture use requires both local target paths
`COLDSNAP_TARGET_PAYLOAD_VERIFIER` and `COLDSNAP_TARGET_CRIU_RPC`; use the selected
engine's target adapter as verifier.

## Supervisor lifecycle API

`sparkrun.plugins.coldsnap.api.control_job(operation, job, sctx=...)` supports
`status`, `sleep`, and `wake` for an existing receipt-backed ColdSnap job.
`LIFECYCLE_API_VERSION = 1` lets supervisors gate this integration explicitly.
The API rebuilds a dry-run plan from the saved recipe, assigned serve port,
parallelism, named cluster, and exact job hosts. It refuses a different live
job ID or capture ID before invoking native lifecycle control. It never starts
an ordinary runtime or falls back to a new launch. Supervisors must separately
verify their ownership and drain requests before changing workload state.

## Licensing

The ColdSnap plugin is licensed under the GNU Affero General Public License
version 3 only. [LICENSE_EXCEPTION](LICENSE_EXCEPTION) grants an additional
permission for combining and conveying it with sparkrun; it does not relicense
the plugin. See [LICENSE](LICENSE) for the full license text.
