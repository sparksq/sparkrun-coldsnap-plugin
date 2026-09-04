# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""First-party ColdSnap recipe and CLI integration."""

from __future__ import annotations

__version__ = "0.1.0"

from sparkrun.plugins import register_cli_command, register_recipe_item
from sparkrun.plugins.coldsnap.builder import ColdSnapBuilder
from sparkrun.plugins.coldsnap.compatibility import ColdSnapCompatibilityError
from sparkrun.plugins.coldsnap.config import ColdSnapRecipeHandler
from sparkrun.plugins.coldsnap.service import ColdSnapExecutionStrategy


_HANDLER = ColdSnapRecipeHandler()
_EXECUTION_STRATEGY = ColdSnapExecutionStrategy()


def register(v) -> None:
    register_recipe_item(
        "coldsnap",
        _HANDLER,
        owner="sparkrun.plugins.coldsnap",
        execution_strategy=_EXECUTION_STRATEGY,
    )
    register_cli_command(
        lambda: __import__("sparkrun.plugins.coldsnap.cli", fromlist=["build_command"]).build_command(),
        name="coldsnap",
    )


__all__ = ["ColdSnapBuilder", "ColdSnapCompatibilityError", "__version__", "register"]
