# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Manager-owned runtime implementations for ColdSnap typed operations."""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any, Protocol

from sparkrun.transports.session import HostCommandResult, HostSessionError

from .runtime_contract import validate_runtime_request


class ManagerRuntime(Protocol):
    def invoke(self, host: str, request: Mapping[str, Any]) -> dict[str, Any]: ...


class RuntimeOperationError(HostSessionError):
    def __init__(self, message: str, *, code: str = "runtime_failed"):
        super().__init__(message)
        self.code = code


class DockerManagerRuntime:
    """Realize ColdSnap workload and OCI requests with a host Docker engine."""

    def __init__(self, session, *, command: str = "docker"):
        self.session = session
        self.command = command

    def invoke(self, host: str, request: Mapping[str, Any]) -> dict[str, Any]:
        validate_runtime_request(request)
        action = request["action"]
        if action == "image-inspect":
            result = self._execute(
                host,
                [
                    self.command,
                    "image",
                    "inspect",
                    request["image"],
                    "--format",
                    '{"id":{{json .Id}},"repo_digests":{{json .RepoDigests}},"size":{{json .Size}}}',
                ],
                "image inspection",
            )
            try:
                info = json.loads(result.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HostSessionError("Docker image inspection returned invalid JSON") from error
            return {"image": info}
        if action in {"image-pull", "image-push"}:
            self.session.docker_registry(
                host,
                action.removeprefix("image-"),
                request["image"],
            )
            return {}
        if action == "image-tag":
            self._execute(
                host,
                [self.command, "tag", request["source"], request["target"]],
                "image tag",
            )
            return {}
        if action == "image-remove":
            self._execute(host, [self.command, "image", "rm", request["image"]], "image removal")
            return {}
        if action == "image-build":
            return self._build(host, request["build"])
        if action == "workload-run":
            return self._run(host, request["workload"])
        if action == "workload-remove":
            try:
                self._execute(host, [self.command, "rm", "-f", request["name"]], "workload removal")
            except RuntimeOperationError as error:
                if error.code != "not_found":
                    raise
            return {}
        if action == "workload-inspect":
            result = self._execute(
                host,
                [
                    self.command,
                    "inspect",
                    "--format",
                    '{"id":{{json .Id}},"image":{{json .Config.Image}},"state":{{json .State.Status}},'
                    '"exit_code":{{json .State.ExitCode}},"running":{{json .State.Running}},'
                    '"paused":{{json .State.Paused}},"labels":{{json .Config.Labels}}}',
                    request["name"],
                ],
                "workload inspection",
            )
            try:
                info = json.loads(result.stdout)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise HostSessionError("Docker workload inspection returned invalid JSON") from error
            return {"workload": info}
        if action == "workload-exec":
            return self._exec(host, request["name"], request["execution"])
        if action == "workload-logs":
            arguments = [self.command, "logs"]
            tail = request.get("tail", 0)
            if tail:
                arguments.extend(["--tail", str(tail)])
            arguments.append(request["name"])
            result = self._execute(host, arguments, "workload logs", combined=True)
            return {"output": _bytes(result.stdout)}
        if action == "workload-copy-from":
            name = request["name"]
            source = request["path"]
            destination = request["destination"]
            self._execute(host, [self.command, "cp", f"{name}:{source}", destination], "workload copy")
            return {}
        raise HostSessionError(f"unsupported manager runtime action {action!r}")

    def _build(self, host: str, build: Mapping[str, Any]) -> dict[str, Any]:
        # invoke() is the validation boundary; these helpers only translate
        # admitted requirements, keeping the wire schema in runtime_contract.
        contexts = build.get("contexts") or {}
        arguments = [self.command, "build"]
        arguments.append("--pull=true" if build.get("pull") else "--pull=false")
        for key in sorted(contexts):
            arguments.extend(["--build-context", f"{key}={contexts[key]}"])
        for key, value in sorted((build.get("arguments") or {}).items()):
            arguments.extend(["--build-arg", f"{key}={value}"])
        arguments.extend(
            [
                "--file",
                "-",
                "--tag",
                build["tag"],
                build["context"],
            ]
        )
        self._execute(host, arguments, "image build", input_data=base64.b64decode(build["dockerfile"], validate=True))
        return {}

    def _run(self, host: str, workload: Mapping[str, Any]) -> dict[str, Any]:
        arguments = [self.command, "run"]
        if "input" in workload:
            arguments.append("-i")
        if workload.get("detached") is True:
            arguments.append("-d")
        if workload.get("remove_after_exit") is True:
            arguments.append("--rm")
        name = workload.get("name")
        if name:
            arguments.extend(["--name", name])
        pull_policy = workload.get("pull_policy")
        if pull_policy:
            arguments.extend(["--pull", pull_policy])
        network = workload.get("network")
        if network and network != "default":
            arguments.extend(["--network", network])
        gpus = workload.get("gpus") or []
        if gpus:
            selection = "device=" + ",".join(gpus)
            # Docker parses this option as CSV, independently of shell quoting.
            arguments.extend(["--gpus", '"' + selection + '"' if len(gpus) > 1 else selection])
        if workload.get("privileged") is True:
            arguments.append("--privileged")
        if workload.get("seccomp_unconfined") is True:
            arguments.extend(["--security-opt", "seccomp=unconfined"])
        if workload.get("memlock_unlimited") is True:
            arguments.extend(["--ulimit", "memlock=-1:-1"])
        shared_memory = workload.get("shared_memory_bytes", 0)
        if shared_memory:
            arguments.extend(["--shm-size", str(shared_memory)])
        user = workload.get("user")
        if user:
            arguments.extend(["--user", user])
        for key, value in sorted((workload.get("labels") or {}).items()):
            arguments.extend(["--label", f"{key}={value}"])
        for key, value in sorted((workload.get("environment") or {}).items()):
            arguments.extend(["-e", f"{key}={value}"])
        for mount in workload.get("mounts") or []:
            value = f"{mount['source']}:{mount['target']}"
            if mount.get("read_only") is True:
                value += ":ro"
            arguments.extend(["-v", value])
        for device in workload.get("devices") or []:
            source = device["source"]
            target = device.get("target") or source
            arguments.extend(["--device", f"{source}:{target}"])
        entrypoint = workload.get("entrypoint")
        if entrypoint:
            arguments.extend(["--entrypoint", entrypoint])
        arguments.append(workload["image"])
        arguments.extend(workload.get("command") or [])
        input_data = _decode_optional_bytes(workload.get("input"))
        result = self._execute(
            host,
            arguments,
            "workload run",
            input_data=input_data,
            combined=workload.get("combined") is True,
        )
        response = {"output": _bytes(result.stdout)}
        if workload.get("detached") is True:
            response["value"] = result.stdout.decode("utf-8", errors="strict").strip()
        return response

    def _exec(self, host: str, name: str, execution: Mapping[str, Any]) -> dict[str, Any]:
        command = execution["command"]
        input_data = _decode_optional_bytes(execution.get("input"))
        arguments = [self.command, "exec"]
        if input_data is not None:
            arguments.append("-i")
        user = execution.get("user")
        if user:
            arguments.extend(["--user", user])
        arguments.extend([name, *command])
        result = self._execute(
            host,
            arguments,
            "workload execution",
            input_data=input_data,
            combined=execution.get("combined") is True,
        )
        return {"output": _bytes(result.stdout)}

    def _execute(
        self,
        host: str,
        arguments: list[str],
        label: str,
        *,
        input_data: bytes | None = None,
        combined: bool = False,
    ) -> HostCommandResult:
        result = self.session.execute(host, arguments, input_data=input_data, combined=combined)
        if result.returncode:
            detail = (result.stdout if combined else result.stderr).decode("utf-8", errors="replace").strip()
            suffix = f": {detail}" if detail else ""
            lower_detail = detail.lower()
            if label == "workload copy" and "could not find the file" in lower_detail:
                code = "path_not_found"
            elif "no such container:" in lower_detail or "no such image:" in lower_detail:
                code = "not_found"
            else:
                code = "runtime_failed"
            raise RuntimeOperationError(
                f"{label} on {host} exited {result.returncode}{suffix}",
                code=code,
            )
        return result


def _decode_optional_bytes(value: str | None) -> bytes | None:
    if value is None:
        return None
    return base64.b64decode(value, validate=True)


def _bytes(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


__all__ = ["DockerManagerRuntime", "ManagerRuntime", "RuntimeOperationError"]
