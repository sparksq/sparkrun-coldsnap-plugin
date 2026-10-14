# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Optional host diagnostics: static declarations, operation-local reporters."""

_factory = None


def register():
    global _factory
    import sparkrun.plugins as api

    if getattr(api, "DIAGNOSTICS_API_VERSION", None) != 1:
        _factory = None
        return
    _factory = api.register_plugin_diagnostics(
        [
            api.DiagnosticSection("artifact_selection"),
            api.DiagnosticSection("controller_result", priority="terminal"),
            api.DiagnosticSection("execution_result", priority="terminal"),
        ]
    )


def bind(*, feedback=None, sctx=None, workload_id=None):
    if _factory is None:
        return None
    return _factory.bind(feedback=feedback, sctx=sctx, workload_id=workload_id)
