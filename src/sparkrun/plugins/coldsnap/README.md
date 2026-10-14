<!--
SPDX-FileCopyrightText: 2026 Scitrera LLC
SPDX-FileCopyrightText: 2026 Fox Engine Ltd
SPDX-License-Identifier: AGPL-3.0-only
SPDX-FileComment: The sparkrun additional permission in LICENSE_EXCEPTION applies.
-->

# ColdSnap plugin licensing

This directory contains sparkrun's first-party ColdSnap integration. Its code
is copyright 2026 Scitrera LLC and Fox Engine Ltd and is licensed under the
GNU Affero General Public License version 3 only.

The copyright holders grant an additional permission under AGPLv3 section 7
for linking, importing, combining, packaging, and conveying this plugin with
sparkrun. That permission keeps sparkrun's non-ColdSnap portions under their
existing license; it does not relicense this plugin or permit an unrelated
integration to avoid the AGPL. See [LICENSE_EXCEPTION](LICENSE_EXCEPTION) for
the controlling terms and [LICENSE](LICENSE) for the full AGPLv3 text.

## Diagnostic bundles

When Sparkrun diagnostic capture is enabled, the plugin contributes artifact compatibility decisions and controller results before temporary receipt cleanup. Snapshots select bounded state already available during the operation; they do not add remote probes or per-chunk reporting.

Use `sparkrun.api.capture_diagnostics(...)` around library operations, or `collect_diagnostics: true` on supported daemon jobs. The host owns redaction, bundle limits, access, and retention. Existing structured feedback continues to provide live progress. With capture disabled, snapshot builders do not run. Hosts without the optional diagnostics API retain normal plugin behavior. See the [Sparkrun diagnostics API](https://github.com/spark-arena/sparkrun/blob/main/docs/DIAGNOSTICS_API.md) for the capture interface.
