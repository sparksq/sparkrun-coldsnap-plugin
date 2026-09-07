# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Resolve release-matched binaries for execution on cluster targets, not here."""

from dataclasses import dataclass
import os
from pathlib import Path

from sparkrun.plugins.coldsnap.tool import (
    ColdSnapToolError,
    _platform,
    ensure_target_tool,
    explicit_controller_environment,
    verify_target_elf,
)


@dataclass(frozen=True)
class TargetTools:
    arch: str
    verifier: Path
    criu_rpc: Path

    @property
    def environment(self) -> dict[str, str]:
        return {
            "COLDSNAP_TARGET_PAYLOAD_VERIFIER": str(self.verifier),
            "COLDSNAP_TARGET_CRIU_RPC": str(self.criu_rpc),
        }


def prepare_target_tools(*, hosts, engine, cluster, sctx, binary="") -> TargetTools:
    from sparkrun.orchestration.primitives import build_ssh_kwargs, run_script_on_host

    ssh_kwargs = build_ssh_kwargs(sctx.config)
    if getattr(cluster, "user", None):
        ssh_kwargs = {**ssh_kwargs, "ssh_user": cluster.user}
    platforms = {}
    for host in sorted(set(hosts)):
        result = run_script_on_host(host, "uname -sm", ssh_kwargs=ssh_kwargs, timeout=30)
        fields = result.stdout.strip().split() if result.success else []
        arch = {"x86_64": "amd64", "aarch64": "arm64"}.get(fields[-1] if fields else "")
        if len(fields) != 2 or fields[0] != "Linux" or arch is None:
            raise ColdSnapToolError("Cannot resolve a supported Linux target platform on %s: %s" % (host, result.stderr or result.stdout))
        platforms[host] = arch
    if len(set(platforms.values())) != 1:
        raise ColdSnapToolError("ColdSnap requires one common target CPU architecture per operation: %s" % platforms)
    arch = next(iter(platforms.values()))
    if binary:
        verifier = os.environ.get("COLDSNAP_TARGET_PAYLOAD_VERIFIER", "")
        criu_rpc = os.environ.get("COLDSNAP_TARGET_CRIU_RPC", "")
        if not verifier and not criu_rpc and ("linux", arch) == _platform():
            environment = explicit_controller_environment(binary)
            verifier = environment.get("COLDSNAP_%s_ADAPTER" % engine.upper(), "")
            criu_rpc = environment.get("COLDSNAP_CRIU_RPC", "")
        if not verifier or not criu_rpc:
            raise ColdSnapToolError(
                "Development controller requires release-matched COLDSNAP_TARGET_PAYLOAD_VERIFIER and COLDSNAP_TARGET_CRIU_RPC"
            )
        selected = TargetTools(arch, Path(verifier), Path(criu_rpc))
    else:
        tool = ensure_target_tool(sctx.config, arch)
        if tool.criu_rpc_path is None:
            raise ColdSnapToolError("Target binary bundle has no CRIU RPC helper")
        selected = TargetTools(arch, tool.payload_verifier(engine), tool.criu_rpc_path)
    verify_target_elf(selected.verifier, arch)
    verify_target_elf(selected.criu_rpc, arch)
    return selected
