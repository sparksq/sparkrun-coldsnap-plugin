# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ASSEMBLER = ROOT / "scripts" / "assemble-dev-host.py"


def _write_host(host: Path, *, integrated: bool = False) -> None:
    core = host / "src" / "sparkrun" / "core"
    plugins = host / "src" / "sparkrun" / "plugins"
    core.mkdir(parents=True)
    plugins.mkdir(parents=True)
    (host / "pyproject.toml").write_text("[project]\nname = 'sparkrun'\n", encoding="utf-8")
    (host / "src" / "sparkrun" / "__init__.py").write_text("", encoding="utf-8")
    feature = (
        'FEATURE_PLUGIN_COLDSNAP = register_feature(FeatureFlag(name="plugins.coldsnap", default=True))\n'
        if integrated
        else "FEATURE_EXISTING = object()\n"
    )
    mapping = (
        'IN_TREE_PLUGIN_FEATURES: dict[str, str] = {"coldsnap": "plugins.coldsnap"}\n'
        if integrated
        else "IN_TREE_PLUGIN_FEATURES: dict[str, str] = {}\n"
    )
    (core / "features.py").write_text(feature, encoding="utf-8")
    (core / "in_tree_plugins.py").write_text(mapping, encoding="utf-8")


def _write_plugin(plugin: Path) -> None:
    source = plugin / "src" / "sparkrun" / "plugins" / "coldsnap"
    source.mkdir(parents=True)
    (source / "__init__.py").write_text("LIVE_PLUGIN = True\n", encoding="utf-8")
    (plugin / "plugin.toml").write_text('schema = 1\nname = "coldsnap"\n', encoding="utf-8")


def _assemble(host: Path, plugin: Path) -> subprocess.CompletedProcess[str]:
    destination = plugin / ".dev" / "sparkrun-with-coldsnap"
    return subprocess.run(
        [
            sys.executable,
            str(ASSEMBLER),
            "--host",
            str(host),
            "--plugin-root",
            str(plugin),
            "--destination",
            str(destination),
        ],
        check=False,
        capture_output=True,
        text=True,
    )


def test_assembly_links_live_source_and_adds_only_disposable_bindings(tmp_path: Path):
    host = tmp_path / "host"
    plugin = tmp_path / "plugin"
    _write_host(host)
    _write_plugin(plugin)
    (host / "local-host-change.txt").write_text("preserved\n", encoding="utf-8")
    original_features = (host / "src/sparkrun/core/features.py").read_text(encoding="utf-8")
    original_loader = (host / "src/sparkrun/core/in_tree_plugins.py").read_text(encoding="utf-8")

    result = _assemble(host, plugin)

    assert result.returncode == 0, result.stdout + result.stderr
    assembled = plugin / ".dev" / "sparkrun-with-coldsnap"
    linked = assembled / "src/sparkrun/plugins/coldsnap"
    assert linked.is_symlink()
    assert linked.resolve() == (plugin / "src/sparkrun/plugins/coldsnap").resolve()
    assert (assembled / "local-host-change.txt").read_text(encoding="utf-8") == "preserved\n"
    assert 'name="plugins.coldsnap"' in (assembled / "src/sparkrun/core/features.py").read_text(encoding="utf-8")
    assert 'IN_TREE_PLUGIN_FEATURES["coldsnap"] = "plugins.coldsnap"' in (assembled / "src/sparkrun/core/in_tree_plugins.py").read_text(
        encoding="utf-8"
    )
    assert (host / "src/sparkrun/core/features.py").read_text(encoding="utf-8") == original_features
    assert (host / "src/sparkrun/core/in_tree_plugins.py").read_text(encoding="utf-8") == original_loader


def test_assembly_does_not_duplicate_an_upstream_binding(tmp_path: Path):
    host = tmp_path / "host"
    plugin = tmp_path / "plugin"
    _write_host(host, integrated=True)
    _write_plugin(plugin)

    result = _assemble(host, plugin)

    assert result.returncode == 0, result.stdout + result.stderr
    assembled = plugin / ".dev" / "sparkrun-with-coldsnap"
    features = (assembled / "src/sparkrun/core/features.py").read_text(encoding="utf-8")
    loader = (assembled / "src/sparkrun/core/in_tree_plugins.py").read_text(encoding="utf-8")
    assert features.count("plugins.coldsnap") == 1
    assert loader.count("plugins.coldsnap") == 1
    assert "BEGIN sparkrun-coldsnap development binding" not in features
    assert "BEGIN sparkrun-coldsnap development binding" not in loader
