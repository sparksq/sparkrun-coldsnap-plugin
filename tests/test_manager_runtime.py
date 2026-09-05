# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

import base64
import json
import os
import shutil
import subprocess
import uuid

import pytest

from sparkrun.plugins.coldsnap.host_provider import ColdSnapHostProvider
from sparkrun.plugins.coldsnap.manager_runtime import DockerManagerRuntime, RuntimeOperationError
from sparkrun.transports.session import HostCommandResult, HostSessionError, SshHostSession

from test_coldsnap_host_provider import _Session, _context, _request, _rpc


def test_workload_requirements_survive_rendering():
    session = _Session()
    runtime = DockerManagerRuntime(session)
    response = runtime.invoke(
        "node-a",
        {
            "action": "workload-run",
            "workload": {
                "name": "unit-0",
                "image": "capsule@sha256:abc",
                "detached": True,
                "network": "host",
                "gpus": ["0", "1"],
                "privileged": True,
                "seccomp_unconfined": True,
                "memlock_unlimited": True,
                "shared_memory_bytes": 32 << 30,
                "user": "0:0",
                "pull_policy": "never",
                "entrypoint": "python3",
                "environment": {"MESSAGE": "space $value; *"},
                "labels": {"owner": "unit-0"},
                "mounts": [{"source": "/data/path with spaces", "target": "/snapshot", "read_only": True}],
                "devices": [{"source": "/dev/infiniband"}],
                "command": ["rank.py", "space separated", ""],
            },
        },
    )
    args = session.calls[0][2]
    assert response["value"] == "stdout"
    for flag, value in (
        ("--gpus", '"device=0,1"'),
        ("--network", "host"),
        ("--security-opt", "seccomp=unconfined"),
        ("--ulimit", "memlock=-1:-1"),
        ("--shm-size", str(32 << 30)),
        ("--user", "0:0"),
        ("--pull", "never"),
    ):
        assert args[args.index(flag) + 1] == value
    assert "--privileged" in args
    assert "/data/path with spaces:/snapshot:ro" in args
    assert "/dev/infiniband:/dev/infiniband" in args
    assert "MESSAGE=space $value; *" in args
    assert args[-4:] == ["capsule@sha256:abc", "rank.py", "space separated", ""]


def test_run_and_exec_binary_stdin_and_output_modes():
    session = _Session()
    runtime = DockerManagerRuntime(session)
    payload = base64.b64encode(b"input\x00bytes").decode()
    runtime.invoke(
        "node-a",
        {
            "action": "workload-run",
            "workload": {
                "image": "busybox:1.37",
                "command": ["cat"],
                "remove_after_exit": True,
                "input": payload,
            },
        },
    )
    runtime.invoke(
        "node-a",
        {
            "action": "workload-exec",
            "name": "unit-0",
            "execution": {
                "command": ["cat"],
                "input": payload,
                "combined": True,
                "user": "0",
            },
        },
    )
    for call in session.calls:
        assert "-i" in call[2] and call[3] == b"input\x00bytes"
    assert not session.calls[0][4] and session.calls[1][4]


@pytest.mark.parametrize(
    "operation",
    [
        {"action": "unknown"},
        {"action": "image-pull", "image": "--all-tags"},
        {"action": "image-push", "image": "registry/x:tag", "command": ["oops"]},
        {"action": "workload-run", "workload": {"image": "base", "privileged": "false"}},
        {"action": "workload-run", "workload": {"image": "base", "network": "unimplemented"}},
        {"action": "workload-run", "workload": {"image": "base", "unknown_requirement": True}},
        {"action": "workload-run", "workload": {"image": "base", "shared_memory_bytes": True}},
        {"action": "workload-run", "workload": {"image": "base", "mounts": [{"source": "relative", "target": "/tmp"}]}},
        {"action": "workload-run", "workload": {"image": "base", "mounts": [{"source": "/tmp:rw", "target": "/tmp"}]}},
        {"action": "workload-run", "workload": {"image": "base", "input": "%%%"}},
        {"action": "workload-run", "workload": {"image": "base", "detached": True}},
        {"action": "workload-exec", "name": "unit-0", "execution": {"command": []}},
        {"action": "workload-logs", "name": "unit-0", "tail": -1},
        {"action": "workload-copy-from", "name": "unit-0", "path": "/source", "destination": "other:/dest"},
    ],
)
def test_invalid_runtime_requests_do_not_execute(operation):
    session = _Session()
    with pytest.raises(HostSessionError):
        DockerManagerRuntime(session).invoke("node-a", operation)
    assert session.calls == []


@pytest.mark.parametrize("message,accepted", [("Error: No such container: unit-0", True), ("permission denied", False)])
def test_remove_is_idempotent_only_for_absent_workload(message, accepted):
    class FailedSession(_Session):
        def execute(self, host, arguments, **kwargs):
            return HostCommandResult(host, 1, b"", message.encode())

    runtime = DockerManagerRuntime(FailedSession())
    if accepted:
        assert runtime.invoke("node-a", {"action": "workload-remove", "name": "unit-0"}) == {}
    else:
        with pytest.raises(RuntimeOperationError, match="permission denied"):
            runtime.invoke("node-a", {"action": "workload-remove", "name": "unit-0"})


def test_runtime_capabilities_cannot_bypass_registry_authority():
    session = _Session()
    with ColdSnapHostProvider(
        _request(), sctx=_context(), session_factory=lambda *a, **kw: session, capabilities=("runtime-v1",)
    ) as provider:
        denied = _rpc(provider, operation="runtime", host="node-a", runtime={"action": "image-push", "image": "registry/x:tag"})
    assert not denied["ok"] and "oci-push" in denied["error"]
    assert session.calls == []


def test_provider_injects_labels_and_supports_non_docker_backend():
    calls = []

    class AlternateRuntime:
        def __init__(self, session):
            pass

        def invoke(self, host, request):
            calls.append((host, request))
            return {"value": "pod-uid"}

    request = _request()
    request["workload"] = {"cluster_id": "job-1", "model": "org/model", "runtime": "vllm-distributed"}
    session = _Session()
    with ColdSnapHostProvider(
        request, sctx=_context(), session_factory=lambda *a, **kw: session, runtime_factory=AlternateRuntime
    ) as provider:
        result = _rpc(
            provider,
            operation="runtime",
            host="node-a",
            runtime={
                "action": "workload-run",
                "workload": {
                    "name": "unit-0",
                    "image": "capsule",
                    "detached": True,
                    "labels": {"io.sparksq.coldsnap.workload": "job-1", "io.sparksq.coldsnap.rank": "0"},
                },
            },
        )
    assert result["ok"] and result["runtime"]["value"] == "pod-uid"
    labels = calls[0][1]["workload"]["labels"]
    assert labels["sparkrun.cluster_id"] == "job-1" and labels["sparkrun.model"] == "org/model"
    assert session.calls == []


def test_image_build_uses_manager_backend_and_preserves_license_inputs():
    session = _Session()
    dockerfile = b'FROM pinned\nLABEL org.opencontainers.image.licenses="AGPL-3.0-only"\n'
    DockerManagerRuntime(session).invoke(
        "node-a",
        {
            "action": "image-build",
            "build": {
                "dockerfile": base64.b64encode(dockerfile).decode(),
                "context": "/artifact",
                "tag": "capsule:local",
                "contexts": {"seed": "/cache"},
                "arguments": {"SOURCE_DATE_EPOCH": "0"},
                "pull": True,
            },
        },
    )
    args = session.calls[0][2]
    assert args[:3] == ["docker", "build", "--pull=true"]
    assert "seed=/cache" in args and "SOURCE_DATE_EPOCH=0" in args
    assert session.calls[0][3] == dockerfile


def test_local_image_build_and_copy_smoke(tmp_path):
    image = os.environ.get("COLDSNAP_TEST_DOCKER_IMAGE")
    if not image:
        pytest.skip("set COLDSNAP_TEST_DOCKER_IMAGE to a locally cached shell image")
    tag = "coldsnap-runtime-test:" + uuid.uuid4().hex[:12]
    name = "coldsnap-runtime-copy-" + uuid.uuid4().hex[:12]
    (tmp_path / "license.txt").write_text("runtime contract test fixture; not a distributable capsule\n")
    session = SshHostSession()
    runtime = DockerManagerRuntime(session)
    built = started = False
    try:
        runtime.invoke(
            "localhost",
            {
                "action": "image-build",
                "build": {
                    "context": str(tmp_path),
                    "tag": tag,
                    "dockerfile": base64.b64encode((f"FROM {image}\nCOPY license.txt /license.txt\n").encode()).decode(),
                },
            },
        )
        built = True
        inspection = runtime.invoke("localhost", {"action": "image-inspect", "image": tag})
        assert inspection["image"]["id"].startswith("sha256:")
        runtime.invoke(
            "localhost",
            {
                "action": "workload-run",
                "workload": {
                    "name": name,
                    "image": tag,
                    "detached": True,
                    "network": "none",
                    "pull_policy": "never",
                    "entrypoint": "sleep",
                    "command": ["60"],
                },
            },
        )
        started = True
        destination = tmp_path / "copied-license.txt"
        runtime.invoke("localhost", {"action": "workload-copy-from", "name": name, "path": "/license.txt", "destination": str(destination)})
        assert destination.read_bytes() == (tmp_path / "license.txt").read_bytes()
    finally:
        try:
            if started:
                runtime.invoke("localhost", {"action": "workload-remove", "name": name})
            if built:
                runtime.invoke("localhost", {"action": "image-remove", "image": tag})
        finally:
            session.close()


@pytest.mark.parametrize("real_docker", [False, True], ids=["recording", "docker-smoke"])
def test_go_runtime_round_trip(tmp_path, real_docker):
    source = os.environ.get("COLDSNAP_SOURCE_ROOT")
    go = os.environ.get("COLDSNAP_GO") or shutil.which("go")
    image = os.environ.get("COLDSNAP_TEST_DOCKER_IMAGE") if real_docker else "contract-image:local"
    if not source or not go or not image:
        pytest.skip("set COLDSNAP_SOURCE_ROOT, COLDSNAP_GO, and optionally COLDSNAP_TEST_DOCKER_IMAGE")
    name = "coldsnap-runtime-test-" + uuid.uuid4().hex[:12]

    class ContractSession(_Session):
        def execute(self, host, arguments, *, input_data=None, combined=False, timeout=None):
            self.calls.append(arguments)
            if arguments[1:3] == ["image", "inspect"]:
                output = json.dumps({"id": "image-id", "size": 42}).encode()
            elif arguments[1] == "run":
                output = b"workload-id\n"
            elif arguments[1] == "inspect":
                output = json.dumps(
                    {
                        "id": "workload-id",
                        "image": image,
                        "state": "running",
                        "running": True,
                        "labels": {"sparkrun.cluster_id": "runtime-contract"},
                    }
                ).encode()
            elif arguments[1] == "exec":
                output = input_data
            elif arguments[1] == "logs":
                output = b"runtime-ready\n"
            elif arguments[1] == "cp":
                return HostCommandResult(host, 1, b"", b"Could not find the file /missing in container " + name.encode())
            else:
                assert arguments[1] == "rm"
                output = b""
            return HostCommandResult(host, 0, output, b"")

    session = SshHostSession() if real_docker else ContractSession()
    request = _request()
    request["launch"]["units"] = [{"id": "unit-0", "host": "localhost"}]
    request["workload"] = {"cluster_id": "runtime-contract"}
    with ColdSnapHostProvider(request, sctx=_context(), session_factory=lambda *a, **kw: session) as provider:
        completed = subprocess.run(
            [
                go,
                "test",
                "./internal/hostprovider",
                "-run",
                "^TestManagerRuntimeCrossLanguageContract$",
                "-count=1",
                "-v",
            ],
            cwd=source,
            env={
                **os.environ,
                "COLDSNAP_TEST_HOST_PROVIDER_SOCKET": str(provider.socket),
                "COLDSNAP_TEST_HOST_PROVIDER_TOKEN": provider.token,
                "COLDSNAP_TEST_HOST_PROVIDER_SESSION": provider.request_id,
                "COLDSNAP_TEST_HOST_PROVIDER_HOST": "localhost",
                "COLDSNAP_TEST_WORKLOAD_NAME": name,
                "COLDSNAP_TEST_WORKLOAD_IMAGE": image,
                "COLDSNAP_TEST_COPY_TARGET": str(tmp_path / "missing"),
            },
            capture_output=True,
            text=True,
            timeout=60,
        )
    assert completed.returncode == 0, completed.stdout + completed.stderr
