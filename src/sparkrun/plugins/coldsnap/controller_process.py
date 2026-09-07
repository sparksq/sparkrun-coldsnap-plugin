# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Keep the manager provider alive until an interrupted controller has exited."""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# The controller gives its adapter four minutes for remote cleanup, including
# capture-path ownership repair. Allow that to finish before closing the RPC
# provider. Normal cancellation should finish much sooner.
SHUTDOWN_GRACE_SECONDS = 300


@contextmanager
def _shutdown_signals(*, on_repeat=None):
    """Unwind on SIGTERM; allow explicit escalation during graceful cleanup."""
    previous = {}

    def terminate(signum, _frame):
        raise SystemExit(128 + signum)

    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGINT, signal.SIGTERM) if on_repeat is not None else (signal.SIGTERM,):
            previous[signum] = signal.signal(signum, on_repeat if on_repeat is not None else terminate)
    try:
        yield
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


def _signal_group(process, signum):
    try:
        # The controller owns a fresh session, so this never signals the
        # manager/terminal or another launch. Include the adapter child.
        os.killpg(process.pid, signum)
    except ProcessLookupError:
        pass


def run_controller(arguments, *, input, text, check, capture_output, env):
    """subprocess.run-compatible service boundary with graceful cancellation.

    Must be called inside the host-provider context. Do not use Popen's context
    manager: its KeyboardInterrupt path does not wait for remote cleanup.
    """
    with _shutdown_signals():
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE if capture_output else None,
            stderr=subprocess.PIPE if capture_output else None,
            text=text,
            env=env,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input)
        except BaseException as failure:
            forced = False

            def force_shutdown(_signum, _frame, *, interrupted=failure):
                nonlocal forced
                if forced:
                    return
                forced = True
                message = "ColdSnap: forcing controller termination; remote cleanup is unconfirmed and may require manual recovery"
                interrupted.add_note(message)
                logger.error(message)
                _signal_group(process, signal.SIGKILL)

            with _shutdown_signals(on_repeat=force_shutdown):
                logger.warning(
                    "ColdSnap: cancelling controller; waiting for operation-owned workload and coordinator cleanup. "
                    "Press Ctrl-C again to force termination (remote cleanup may be incomplete)."
                )
                try:
                    _signal_group(process, signal.SIGTERM)
                    # communicate's single-pipe fast path closes stdin before
                    # waiting, without enabling its resumable I/O path. A retry
                    # must not attempt to flush that already-closed stream.
                    if process.stdin is not None and process.stdin.closed:
                        process.stdin = None
                    _stdout, stderr = process.communicate(timeout=SHUTDOWN_GRACE_SECONDS)
                    if stderr:
                        logger.warning("ColdSnap cancellation diagnostics:\n%s", stderr[-4000:].strip())
                except subprocess.TimeoutExpired:
                    message = "ColdSnap controller shutdown timed out; remote cleanup is unconfirmed and may require manual recovery"
                    failure.add_note(message)
                    logger.error(message)
                    _signal_group(process, signal.SIGKILL)
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.error("ColdSnap controller process group did not finish after forced termination")
                except Exception as cleanup_error:
                    message = "ColdSnap controller shutdown failed; remote cleanup is unconfirmed: %s" % cleanup_error
                    failure.add_note(message)
                    logger.error(message)
                    _signal_group(process, signal.SIGKILL)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        logger.error("ColdSnap controller did not exit after forced termination")
            raise
        finally:
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream is not None:
                    stream.close()
        completed = subprocess.CompletedProcess(arguments, process.returncode, stdout, stderr)
        if check:
            completed.check_returncode()
        return completed
