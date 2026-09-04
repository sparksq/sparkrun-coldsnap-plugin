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

To use an existing local checkout instead, set `SPARKRUN_CHECKOUT`:

```bash
export SPARKRUN_CHECKOUT=/path/to/sparkrun
source dev.sh
```

An explicit checkout takes precedence over `SPARKRUN_BRANCH` and is never
fetched, switched, or otherwise modified by the script. `dev.sh` exports the
resolved `SPARKRUN_CHECKOUT`, creates or updates this repository's `.venv`,
installs the selected sparkrun checkout and this plugin as editable packages,
activates the virtual environment, and installs the pre-commit hooks.

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

## Licensing

The ColdSnap plugin is licensed under the GNU Affero General Public License
version 3 only. `LICENSE_EXCEPTION` grants an additional permission for
combining and conveying it with sparkrun; it does not relicense the plugin.
