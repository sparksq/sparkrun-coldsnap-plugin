# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

from pathlib import Path
import tomllib

import yaml

from sparkrun.plugins.coldsnap import __version__ as plugin_version
from sparkrun.plugins.coldsnap.tool import DEFAULT_CONTROLLER_VERSION


ROOT = Path(__file__).resolve().parents[1]


def test_generated_versions_match_the_authoritative_catalog():
    versions = yaml.safe_load((ROOT / "versions.yaml").read_text(encoding="utf-8"))
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = tomllib.loads((ROOT / "plugin.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == plugin_version == str(versions["sparkrun-coldsnap-plugin"])
    assert DEFAULT_CONTROLLER_VERSION == str(versions["coldsnap"])
    assert "version" not in manifest
