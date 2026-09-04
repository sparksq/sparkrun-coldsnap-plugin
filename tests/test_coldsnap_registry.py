# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

import pytest

registry_defaults = pytest.importorskip(
    "sparkrun.core.registry_defaults",
    reason="plugin-declared registries require a host with registry overlay support",
)
RegistryManager = __import__("sparkrun.core.registry", fromlist=["RegistryManager"]).RegistryManager
register = __import__("sparkrun.plugins.coldsnap", fromlist=["register"]).register


def test_registration_contributes_the_recipe_repository(tmp_path):
    register(None)

    declarations = registry_defaults.iter_declared_registries()
    assert [declaration.owner for declaration in declarations] == ["coldsnap", "coldsnap"]
    assert [declaration.entry.name for declaration in declarations] == ["coldsnap", "coldsnap-vanilla"]

    qualified, vanilla = [declaration.entry for declaration in declarations]
    assert qualified.url == "https://github.com/sparksq/sparkrun-recipes.git"
    assert qualified.subpath == "coldsnap-recipes"
    assert qualified.enabled is True
    assert qualified.visible is False
    assert qualified.trusted is False

    assert vanilla.url == qualified.url
    assert vanilla.subpath == "vanilla-recipes"
    assert vanilla.enabled is False
    assert vanilla.visible is False
    assert vanilla.trusted is False

    manager = RegistryManager(config_root=tmp_path / "config", cache_root=tmp_path / "cache")
    overlaid = {entry.name: entry for entry in manager.list_registries()}
    assert overlaid["coldsnap"].declared_by == "coldsnap"
    assert overlaid["coldsnap-vanilla"].declared_by == "coldsnap"
    assert not manager._registries_path.exists()


def test_registry_registration_is_idempotent():
    register(None)
    register(None)

    assert [declaration.entry.name for declaration in registry_defaults.iter_declared_registries()] == [
        "coldsnap",
        "coldsnap-vanilla",
    ]
