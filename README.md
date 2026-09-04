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

## Licensing

The ColdSnap plugin is licensed under the GNU Affero General Public License
version 3 only. `LICENSE_EXCEPTION` grants an additional permission for
combining and conveying it with sparkrun; it does not relicense the plugin.
