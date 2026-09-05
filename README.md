<!--
SPDX-FileCopyrightText: 2026 Scitrera LLC
SPDX-FileCopyrightText: 2026 Fox Engine Ltd
SPDX-License-Identifier: AGPL-3.0-only
-->

# sparkrun ColdSnap plugin

This repository is the canonical development home of sparkrun's first-party
ColdSnap integration. Released sparkrun distributions vendor an immutable
snapshot of this repository; they do not clone or install it at build time or
runtime.

Early testers should start with [DEV_PREVIEW.md](DEV_PREVIEW.md).

Release 0.1.2 adds explicit SGLang materialization through capture and verified
restore, using ColdSnap 0.3.20. See the
[materialization guide](DEV_PREVIEW.md#explicit-sglang-materialization-since-012)
for native and recovery-only preparation; ordinary recovery restores do not
gain asynchronous/write-behind materialization.

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

The disposable assembly deliberately exercises sparkrun's in-tree loader. The
plugin and its registry declarations therefore have the same in-tree provenance
and trust boundary they have after commit-pinned vendoring; the development
workflow does not route ColdSnap through `core.external_plugins`. Once the
ColdSnap binding lands upstream, the assembly recognizes it and does not add a
duplicate, while the source link continues to make local plugin edits live.

The test bootstrap makes this repository's plugin source take precedence over
the copy vendored by the selected sparkrun checkout. This keeps changes local
to this repository while exercising them against the real host implementation.

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
on Python 3.12 and 3.13 against the published Sparkrun 0.3.7 host. Each matrix job
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

The plugin resolves the release-pinned ColdSnap controller, vLLM adapter,
SGLang adapter, and CRIU RPC helper as one verified tool set. It checks, in
order:

1. the local sparkrun tool cache;
2. the pinned GitHub release and its `checksums.txt`;
3. `docker.io/scitrera/coldsnap-binaries:<version>` and its platform-specific
   bundle manifest; and
4. an exact source-tag build using Git/SSH and the source-pinned Go container.

The OCI and source-build fallbacks require Docker on the sparkrun control node.
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
commit, the target OS and architecture, and the SHA-256 digest of all four
executables before activating the cache generation.

## Runtime-neutral manager support

Plugin 0.1.1 pins ColdSnap 0.3.20. Upgrade both components together. The provider
advertises `runtime-v1` and delegates typed image/workload operations to
`DockerManagerRuntime`; `runtime_factory` permits an alternate manager backend.
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

The controller's vLLM/SGLang adapters and CRIU RPC helper must be available
beside it. The default acquisition path uses the exact controller release and
source commit pinned by this plugin. Use a separate Python environment when
testing local overrides so the working plugin installation is unaffected.

Run `tests/test_manager_runtime.py` for backend and wire-contract tests.
Set `COLDSNAP_SOURCE_ROOT` and `COLDSNAP_GO` to enable the Go/Python round trip;
also set `COLDSNAP_TEST_DOCKER_IMAGE` to a locally cached shell image (for example
`busybox:1.37`) to enable the isolated container and image-build smoke tests.
These tests create only uniquely named local test resources, clean them up,
and do not request GPUs or publish images.

## Licensing

The ColdSnap plugin is licensed under the GNU Affero General Public License
version 3 only. `LICENSE_EXCEPTION` grants an additional permission for
combining and conveying it with sparkrun; it does not relicense the plugin.
