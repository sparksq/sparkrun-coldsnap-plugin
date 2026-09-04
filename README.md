<!--
SPDX-FileCopyrightText: 2026 Scitrera LLC
SPDX-FileCopyrightText: 2026 Fox Engine Ltd
SPDX-License-Identifier: AGPL-3.0-only
-->

# SparkRun ColdSnap plugin

This repository is the canonical development home of SparkRun's first-party
ColdSnap integration. Released SparkRun distributions vendor an immutable
snapshot of this repository; they do not clone or install it at build time or
runtime.

The plugin is imported by SparkRun as `sparkrun.plugins.coldsnap` and remains
subject to SparkRun's `plugins.coldsnap` feature gate. The source repository
and exact commit included by a SparkRun release are recorded in that release's
`vendor/coldsnap.lock` and packaged `VENDORED.toml` files.

## Development

The plugin currently consumes SparkRun integration seams directly, so tests
run against a SparkRun checkout:

```bash
export SPARKRUN_CHECKOUT=/path/to/sparkrun-staging
uv venv
uv pip install -e "$SPARKRUN_CHECKOUT[dev]"
uv run pytest
```

`tests/conftest.py` makes this repository's plugin source take precedence over
the copy vendored by the selected SparkRun checkout. This keeps changes local
to this repository while exercising them against the real host implementation.

Changes are made and tested here first. SparkRun then imports an approved full
commit with its `scripts/vendor-coldsnap.py` command. Files in SparkRun's
vendored source and test directories should not be edited directly.

## Licensing

The ColdSnap plugin is licensed under the GNU Affero General Public License
version 3 only. `LICENSE_EXCEPTION` grants an additional permission for
combining and conveying it with SparkRun; it does not relicense the plugin.
