#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only

"""Assemble a disposable sparkrun tree with the live ColdSnap source in-tree."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[1]
ASSEMBLY_NAME = "sparkrun-with-coldsnap"
PLUGIN_MODULE_PATH = Path("src/sparkrun/plugins/coldsnap")
FEATURES_PATH = Path("src/sparkrun/core/features.py")
IN_TREE_PLUGINS_PATH = Path("src/sparkrun/core/in_tree_plugins.py")
_IGNORED_NAMES = {
    ".dev",
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
_FEATURE_PATTERN = re.compile(r"name\s*=\s*['\"]plugins\.coldsnap['\"]")
_BINDING_PATTERN = re.compile(
    r"(?:['\"]coldsnap['\"]\s*:|IN_TREE_PLUGIN_FEATURES\s*\[\s*['\"]coldsnap['\"]\s*\]\s*=)"
    r"\s*['\"]plugins\.coldsnap['\"]"
)

_FEATURE_BINDING = """

# BEGIN sparkrun-coldsnap development binding
# Added only to the disposable tree assembled by the plugin's dev.sh. The
# selected upstream checkout is never modified.
FEATURE_PLUGIN_COLDSNAP = register_feature(
    FeatureFlag(
        name="plugins.coldsnap",
        description="ColdSnap capture/restore recipes and resolved launch export",
        default=True,
    )
)
# END sparkrun-coldsnap development binding
"""

_LOADER_BINDING = """

# BEGIN sparkrun-coldsnap development binding
# Added only to the disposable tree assembled by the plugin's dev.sh.
IN_TREE_PLUGIN_FEATURES["coldsnap"] = "plugins.coldsnap"
# END sparkrun-coldsnap development binding
"""


class AssemblyError(RuntimeError):
    """The selected host cannot be assembled safely."""


def _ignore(_directory: str, names: list[str]) -> set[str]:
    return set(names).intersection(_IGNORED_NAMES)


def _append_if_missing(path: Path, pattern: re.Pattern[str], addition: str) -> None:
    contents = path.read_text(encoding="utf-8")
    if pattern.search(contents):
        return
    path.write_text(contents.rstrip() + addition + "\n", encoding="utf-8")


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def assemble(*, host: Path, plugin_root: Path, destination: Path) -> Path:
    """Build and return the disposable in-tree development checkout."""
    host = host.expanduser().resolve()
    plugin_root = plugin_root.expanduser().resolve()
    destination = destination.expanduser().resolve()
    expected_destination = (plugin_root / ".dev" / ASSEMBLY_NAME).resolve()
    if destination != expected_destination:
        raise AssemblyError("refusing to replace unexpected assembly destination: %s" % destination)
    if host == destination:
        raise AssemblyError("the source checkout and assembly destination must differ")

    plugin_source = plugin_root / PLUGIN_MODULE_PATH
    required = [
        host / "pyproject.toml",
        host / "src/sparkrun/__init__.py",
        host / FEATURES_PATH,
        host / IN_TREE_PLUGINS_PATH,
        plugin_root / "plugin.toml",
        plugin_source / "__init__.py",
    ]
    missing = [str(path) for path in required if not path.is_file()]
    if missing:
        raise AssemblyError(
            "the selected checkout does not expose the sparkrun in-tree plugin seams; "
            "select a compatible branch such as develop-next (missing: %s)" % ", ".join(missing)
        )

    temporary = destination.with_name(".%s.tmp" % destination.name)
    _remove_path(temporary)
    try:
        shutil.copytree(host, temporary, symlinks=True, ignore=_ignore)

        assembled_plugin = temporary / PLUGIN_MODULE_PATH
        _remove_path(assembled_plugin)
        assembled_plugin.parent.mkdir(parents=True, exist_ok=True)
        assembled_plugin.symlink_to(plugin_source, target_is_directory=True)

        _append_if_missing(temporary / FEATURES_PATH, _FEATURE_PATTERN, _FEATURE_BINDING)
        _append_if_missing(temporary / IN_TREE_PLUGINS_PATH, _BINDING_PATTERN, _LOADER_BINDING)

        _remove_path(destination)
        temporary.rename(destination)
    except Exception:
        _remove_path(temporary)
        raise

    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", type=Path, required=True, help="base sparkrun checkout to copy")
    parser.add_argument("--destination", type=Path, required=True, help="disposable assembled checkout")
    parser.add_argument("--plugin-root", type=Path, default=ROOT, help=argparse.SUPPRESS)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        destination = assemble(host=args.host, plugin_root=args.plugin_root, destination=args.destination)
    except (AssemblyError, OSError) as error:
        print("error: %s" % error, file=sys.stderr)
        return 1
    print("Assembled in-tree ColdSnap development host: %s" % destination)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
