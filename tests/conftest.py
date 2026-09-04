# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Run the plugin source against an installed or checked-out sparkrun host."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPARKRUN_CHECKOUT = os.environ.get("SPARKRUN_CHECKOUT")
if SPARKRUN_CHECKOUT:
    host_source = Path(SPARKRUN_CHECKOUT).expanduser().resolve() / "src"
    if not (host_source / "sparkrun" / "__init__.py").is_file():
        raise RuntimeError("SPARKRUN_CHECKOUT does not point to a sparkrun checkout: %s" % SPARKRUN_CHECKOUT)
    sys.path.insert(0, str(host_source))

try:
    import sparkrun.plugins
except ImportError as error:
    raise RuntimeError(
        "ColdSnap plugin tests require sparkrun; install it or set SPARKRUN_CHECKOUT"
    ) from error

plugin_parent = str(ROOT / "src" / "sparkrun" / "plugins")
if plugin_parent in sparkrun.plugins.__path__:
    sparkrun.plugins.__path__.remove(plugin_parent)
sparkrun.plugins.__path__.insert(0, plugin_parent)


@pytest.fixture(autouse=True)
def isolate_sparkrun_state(tmp_path: Path, monkeypatch):
    """Keep plugin tests away from developer configuration and network state."""
    monkeypatch.setenv("STATEFUL_ROOT", str(tmp_path / "stateful"))
    monkeypatch.setenv("SPARKRUN_NO_TELEMETRY", "1")
    monkeypatch.setenv("SPARKRUN_NO_EXTERNAL_PLUGINS", "1")

    import sparkrun.core.bootstrap as bootstrap
    import sparkrun.core.config as config
    import sparkrun.core.registry as registry

    monkeypatch.setattr(config, "DEFAULT_CONFIG_DIR", tmp_path / "config", raising=False)
    monkeypatch.setattr(config, "DEFAULT_CACHE_DIR", tmp_path / "cache" / "sparkrun", raising=False)
    monkeypatch.setattr(registry, "BOOTSTRAP_REGISTRY_URLS", [], raising=False)
    monkeypatch.setattr(registry.RegistryManager, "_clone_or_pull", lambda self, entry: False, raising=False)
    bootstrap._variables = None
    yield
    bootstrap._variables = None
