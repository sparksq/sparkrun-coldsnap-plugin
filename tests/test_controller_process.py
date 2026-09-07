# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Real manager/controller/adapter processes and an actual provider socket."""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

# The subprocess test host needs the same standalone-plugin binding as pytest.
if __name__ == "__main__":
    import sparkrun.plugins

    sparkrun.plugins.__path__.insert(0, str(Path(__file__).resolve().parents[1] / "src/sparkrun/plugins"))

from sparkrun.plugins.coldsnap import controller_process
from sparkrun.plugins.coldsnap.host_provider import ColdSnapHostProvider
from test_coldsnap_host_provider import _context, _request, _rpc, _Session


def _child(mode, root):
    if mode == "stuck":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    else:

        def cleanup(_signum, _frame):
            # Ensure a second interrupt arrives while cleanup is still running.
            time.sleep(0.15)
            provider = SimpleNamespace(
                socket=os.environ["COLDSNAP_HOST_PROVIDER_SOCKET"],
                token=os.environ["COLDSNAP_HOST_PROVIDER_TOKEN"],
                request_id="operation-one",
            )
            response = _rpc(provider, operation="exec", host="node-a", arguments=["cleanup"])
            assert response["ok"]
            (root / "adapter-cleaned").write_text("provider still available")
            raise SystemExit(1)

        signal.signal(signal.SIGTERM, cleanup)
    (root / "adapter-pid").write_text(str(os.getpid()))
    print("ADAPTER_READY", flush=True)
    while True:
        time.sleep(0.05)


def _manager(mode, root):
    session = _Session()
    provider = ColdSnapHostProvider(_request(), sctx=_context(), session_factory=lambda *a, **kw: session)
    controller_process.SHUTDOWN_GRACE_SECONDS = 0.3 if mode == "stuck" else 5
    try:
        with provider:
            controller_process.run_controller(
                [sys.executable, __file__, "controller", mode, str(root)],
                input="request",
                text=True,
                check=False,
                capture_output=mode == "captured",
                env={**os.environ, **provider.environment},
            )
    finally:
        (root / "manager-report.json").write_text(
            json.dumps(
                {
                    "closed": session.closed,
                    "socket_exists": provider.socket.exists(),
                    "calls": session.calls,
                }
            )
        )


def _wait_ready(process, root, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if (root / "adapter-pid").exists():
            return
        if process.poll() is not None:
            break
        time.sleep(0.05)
    pytest.fail("manager did not launch a ready adapter")


@pytest.mark.parametrize("scope,signum", [("manager", signal.SIGINT), ("group", signal.SIGINT), ("manager", signal.SIGTERM)])
@pytest.mark.parametrize("mode", ["graceful", "captured"])
def test_cancellation_drains_adapter_before_provider_close(tmp_path, scope, signum, mode):
    process = subprocess.Popen(
        [sys.executable, __file__, "manager", mode, str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_ready(process, tmp_path)
        if scope == "group":
            os.killpg(process.pid, signum)
        else:
            process.send_signal(signum)
        time.sleep(0.05)
        process.send_signal(signal.SIGINT)  # must not abort the cleanup grace
        _, stderr = process.communicate(timeout=10)
        assert process.returncode != 0
        assert "waiting for operation-owned workload and coordinator cleanup" in stderr
        assert (tmp_path / "adapter-cleaned").is_file(), stderr
        report = json.loads((tmp_path / "manager-report.json").read_text())
        assert report["closed"] and not report["socket_exists"]
        assert report["calls"][0][2] == ["cleanup"]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


def test_stuck_controller_shutdown_is_bounded_and_warns(tmp_path):
    process = subprocess.Popen(
        [sys.executable, __file__, "manager", "stuck", str(tmp_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        _wait_ready(process, tmp_path)
        process.send_signal(signal.SIGINT)
        _, stderr = process.communicate(timeout=10)
        assert process.returncode != 0
        assert "remote cleanup is unconfirmed" in stderr
        assert not (tmp_path / "adapter-cleaned").exists()
        report = json.loads((tmp_path / "manager-report.json").read_text())
        assert report["closed"] and not report["socket_exists"]
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate(timeout=5)


@pytest.mark.parametrize("capture", [False, True])
@pytest.mark.parametrize("rc", [0, 7])
def test_normal_process_result_preserves_streams_and_exit_status(capture, rc):
    result = controller_process.run_controller(
        [sys.executable, "-c", "import sys; print(sys.stdin.read()); print('err', file=sys.stderr); sys.exit(%d)" % rc],
        input="payload",
        text=True,
        check=False,
        capture_output=capture,
        env=os.environ.copy(),
    )
    assert result.returncode == rc
    assert result.stdout == ("payload\n" if capture else None)
    assert result.stderr == ("err\n" if capture else None)


if __name__ == "__main__":
    role, selected_mode, directory = sys.argv[1:]
    destination = Path(directory)
    if role == "manager":
        _manager(selected_mode, destination)
    elif role == "controller":
        # Like the Go controller, consume the request and wait for the adapter.
        # Cancellation signals the adapter too, but must not kill it immediately.
        signal.signal(signal.SIGTERM, lambda *_: None)
        sys.stdin.read()
        child = subprocess.Popen([sys.executable, __file__, "adapter", selected_mode, str(destination)])
        raise SystemExit(child.wait())
    else:
        _child(selected_mode, destination)
