# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "src" / "sparkrun" / "plugins" / "coldsnap"
HEADER = (
    "# SPDX-FileCopyrightText: 2026 Scitrera LLC",
    "# SPDX-FileCopyrightText: 2026 Fox Engine Ltd",
    "# SPDX-License-Identifier: AGPL-3.0-only",
    "# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.",
)


def test_source_and_tests_carry_the_plugin_license_notice():
    files = sorted(PLUGIN.glob("*.py")) + sorted((ROOT / "tests").glob("test_*.py"))
    assert files
    for path in files:
        assert tuple(path.read_text(encoding="utf-8").splitlines()[:4]) == HEADER, path.relative_to(ROOT)


def test_root_and_packaged_license_material_are_identical():
    for name in ("LICENSE", "LICENSE_EXCEPTION"):
        assert (ROOT / name).read_bytes() == (PLUGIN / name).read_bytes()


def test_project_and_vendor_manifest_describe_the_agpl_plugin():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    plugin = tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))

    assert project["project"]["license"] == "AGPL-3.0-only"
    assert plugin["name"] == "coldsnap"
    assert plugin["module"] == "sparkrun.plugins.coldsnap"
    assert plugin["feature"] == "plugins.coldsnap"
    assert plugin["source"] == "src/sparkrun/plugins/coldsnap"
