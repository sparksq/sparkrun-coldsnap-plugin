# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Host feedback API adapter; old hosts retain their existing text output."""

from sparkrun.core.progress import PROGRESS, progress_heartbeat


class _TextFeedback:
    def __init__(self, logger):
        self.logger = logger
        self.activity_id = "legacy"

    def child(self, **_kwargs):
        return self

    def report(self, message, *, level="progress", **_fields):
        self.logger.log({"info": 20, "progress": PROGRESS, "warning": 30, "error": 40}[level], "%s", message)

    def progress(self, message, **fields):
        self.report(message, **fields)

    def heartbeat(self, label):
        return progress_heartbeat(self.logger, label)


def reporter(logger, *, sctx=None):
    try:
        from sparkrun.plugins import get_feedback
    except ImportError:
        return _TextFeedback(logger)
    return get_feedback("coldsnap", logger=logger, sctx=sctx)


def progress(logger, stage, message, *args, sctx=None, **fields):
    reporter(logger, sctx=sctx).progress(message % args if args else message, stage=stage, **fields)
