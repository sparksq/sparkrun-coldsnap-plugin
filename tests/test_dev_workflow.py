# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess


ROOT = Path(__file__).resolve().parents[1]
REPOSITORY = "https://github.com/spark-arena/sparkrun.git"


def _executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    path.chmod(0o755)


def _checkout(path: Path) -> None:
    (path / ".git").mkdir(parents=True)
    (path / "src" / "sparkrun").mkdir(parents=True)
    (path / "pyproject.toml").write_text("[project]\nname = 'sparkrun'\n", encoding="utf-8")
    (path / "src" / "sparkrun" / "__init__.py").write_text("", encoding="utf-8")


def _development_tree(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    plugin = tmp_path / "plugin"
    plugin.mkdir()
    shutil.copy2(ROOT / "dev.sh", plugin / "dev.sh")

    managed = plugin / ".dev" / "sparkrun"
    _checkout(managed)

    venv_bin = plugin / ".venv" / "bin"
    _executable(venv_bin / "python", "exit 0\n")
    _executable(venv_bin / "pre-commit", "exit 0\n")
    (venv_bin / "activate").write_text(":\n", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    git_log = tmp_path / "git.log"
    _executable(
        fake_bin / "git",
        """printf '%s\\n' "$*" >> "$FAKE_GIT_LOG"
if [[ "$*" == *"remote get-url origin"* ]]; then
    printf '%s\\n' "$FAKE_GIT_ORIGIN"
fi
exit 0
""",
    )
    _executable(fake_bin / "uv", "exit 0\n")

    env = {
        **os.environ,
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
        "FAKE_GIT_LOG": str(git_log),
        "FAKE_GIT_ORIGIN": REPOSITORY,
    }
    return plugin, git_log, env


def test_repeated_source_honors_a_changed_managed_branch(tmp_path: Path):
    plugin, git_log, env = _development_tree(tmp_path)

    result = subprocess.run(
        [
            "bash",
            "-c",
            """
set -e
unset SPARKRUN_BRANCH _SPARKRUN_COLDSNAP_MANAGED_CHECKOUT
export SPARKRUN_CHECKOUT="$PLUGIN_ROOT/.dev/sparkrun"
source "$PLUGIN_ROOT/dev.sh"
export SPARKRUN_BRANCH=develop-next
source "$PLUGIN_ROOT/dev.sh"
printf 'checkout=%s\\nbranch=%s\\nmarker=%s\\n' \\
    "$SPARKRUN_CHECKOUT" "$SPARKRUN_BRANCH" "$_SPARKRUN_COLDSNAP_MANAGED_CHECKOUT"
""",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={**env, "PLUGIN_ROOT": str(plugin)},
    )

    assert result.returncode == 0, result.stderr
    assert "Using local sparkrun checkout" not in result.stdout
    assert "Updating sparkrun branch main" in result.stdout
    assert "Updating sparkrun branch develop-next" in result.stdout
    assert "branch=develop-next" in result.stdout
    assert "checkout=%s" % (plugin / ".dev" / "sparkrun") in result.stdout
    assert "marker=%s" % (plugin / ".dev" / "sparkrun") in result.stdout
    calls = git_log.read_text(encoding="utf-8")
    assert "fetch --prune origin main" in calls
    assert "fetch --prune origin develop-next" in calls


def test_a_different_explicit_checkout_still_takes_precedence(tmp_path: Path):
    plugin, git_log, env = _development_tree(tmp_path)
    explicit = tmp_path / "explicit-sparkrun"
    _checkout(explicit)

    result = subprocess.run(
        [
            "bash",
            "-c",
            """
set -e
export SPARKRUN_BRANCH=develop-next
export SPARKRUN_CHECKOUT="$EXPLICIT_CHECKOUT"
export _SPARKRUN_COLDSNAP_MANAGED_CHECKOUT="$PLUGIN_ROOT/.dev/sparkrun"
source "$PLUGIN_ROOT/dev.sh"
printf 'checkout=%s\\nmarker=%s\\n' \\
    "$SPARKRUN_CHECKOUT" "${_SPARKRUN_COLDSNAP_MANAGED_CHECKOUT-unset}"
""",
        ],
        check=False,
        capture_output=True,
        text=True,
        env={
            **env,
            "PLUGIN_ROOT": str(plugin),
            "EXPLICIT_CHECKOUT": str(explicit),
        },
    )

    assert result.returncode == 0, result.stderr
    assert "Using local sparkrun checkout: %s" % explicit in result.stdout
    assert "checkout=%s" % explicit in result.stdout
    assert "marker=unset" in result.stdout
    calls = git_log.read_text(encoding="utf-8") if git_log.exists() else ""
    assert "fetch --prune" not in calls
