# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

import hashlib
import json
from types import SimpleNamespace

import pytest

from sparkrun.plugins.coldsnap import target_tools, tool
from sparkrun.plugins.coldsnap.service import ColdSnapService


def elf_bytes(arch):
    data = bytearray(64)
    data[:7] = b"\x7fELF\x02\x01\x01"
    data[16:18] = (2).to_bytes(2, "little")
    data[18:20] = {"amd64": 62, "arm64": 183}[arch].to_bytes(2, "little")
    data[20:24] = (1).to_bytes(4, "little")
    data[52:54] = (64).to_bytes(2, "little")
    return bytes(data)


def bundle(root, arch):
    root.mkdir(parents=True, exist_ok=True)
    hashes = {}
    for name in tool._BINARIES:
        path = root / name
        path.write_bytes(elf_bytes(arch))
        path.chmod(0o755)
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (root / "manifest.json").write_text(json.dumps({
        "format": 1, "version": "0.3.21", "commit": "a" * 40, "platform": "linux-" + arch, "sha256": hashes,
    }))
    return tool.ControllerTool(root / "coldsnap", root / "coldsnap-vllm-adapter", "0.3.21", "test",
                               root / "coldsnap-sglang-adapter", root / "coldsnap-criu-rpc")


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_target_bundle_is_verified_without_local_execution(tmp_path, monkeypatch, arch):
    bundle(tmp_path, arch)
    monkeypatch.setattr(tool.subprocess, "run", lambda *args, **kwargs: pytest.fail("executed target binary on controller"))
    assert tool._verify_cached(tmp_path, "0.3.21", "a" * 40, target_arch=arch)
    assert tool._verify_cached(tmp_path, "0.3.21", "b" * 40, target_arch=arch) is None
    assert tool._verify_cached(tmp_path, "0.3.21", "a" * 40, target_arch="arm64" if arch == "amd64" else "amd64") is None
    (tmp_path / "coldsnap-criu-rpc").write_bytes(b"wrong")
    assert tool._verify_cached(tmp_path, "0.3.21", "a" * 40, target_arch=arch) is None


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_target_elf_rejects_wrong_machine_even_with_matching_hash(tmp_path, arch):
    bundle(tmp_path, arch)
    path = tmp_path / "coldsnap-criu-rpc"
    path.write_bytes(elf_bytes("amd64" if arch == "arm64" else "arm64"))
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    manifest["sha256"][path.name] = hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path / "manifest.json").write_text(json.dumps(manifest))
    assert tool._verify_cached(tmp_path, "0.3.21", "a" * 40, target_arch=arch) is None


@pytest.mark.parametrize("controller_arch,target_arch", [("amd64", "arm64"), ("arm64", "amd64"), ("arm64", "arm64")])
@pytest.mark.parametrize("engine", ["vllm", "sglang"])
def test_resolve_target_helpers_independently_of_controller(tmp_path, monkeypatch, controller_arch, target_arch, engine):
    selected = bundle(tmp_path, target_arch)
    monkeypatch.setattr(tool, "_platform", lambda: ("linux", controller_arch))
    calls = []
    def resolve(config, arch):
        assert arch == target_arch
        return selected
    monkeypatch.setattr(target_tools, "ensure_target_tool", resolve)
    monkeypatch.setattr("sparkrun.orchestration.primitives.build_ssh_kwargs", lambda config: {})
    def probe(host, script, **kwargs):
        assert script == "uname -sm"
        assert kwargs["ssh_kwargs"]["ssh_user"] == "cluster-user"
        calls.append(host)
        return SimpleNamespace(success=True, stdout="Linux " + {"amd64": "x86_64", "arm64": "aarch64"}[target_arch], stderr="")
    monkeypatch.setattr("sparkrun.orchestration.primitives.run_script_on_host", probe)
    result = target_tools.prepare_target_tools(hosts=["b", "a", "a"], engine=engine,
                                               cluster=SimpleNamespace(user="cluster-user"), sctx=SimpleNamespace(config=object()))
    assert calls == ["a", "b"]
    assert result.verifier == selected.payload_verifier(engine)
    assert result.environment == {
        "COLDSNAP_TARGET_PAYLOAD_VERIFIER": str(selected.payload_verifier(engine)),
        "COLDSNAP_TARGET_CRIU_RPC": str(selected.criu_rpc_path),
    }


@pytest.mark.parametrize("outputs", [("Linux x86_64", "Linux aarch64"), ("Linux riscv64", "Linux riscv64"), ("Darwin arm64", "Darwin arm64")])
def test_incompatible_targets_fail_before_acquisition(monkeypatch, outputs):
    monkeypatch.setattr("sparkrun.orchestration.primitives.build_ssh_kwargs", lambda config: {})
    monkeypatch.setattr("sparkrun.orchestration.primitives.run_script_on_host",
                        lambda host, *args, **kwargs: SimpleNamespace(success=True, stdout=outputs[int(host)], stderr=""))
    monkeypatch.setattr(target_tools, "ensure_target_tool", lambda *args: pytest.fail("acquired before target admission"))
    with pytest.raises(tool.ColdSnapToolError):
        target_tools.prepare_target_tools(hosts=["0", "1"], engine="vllm", cluster=SimpleNamespace(), sctx=SimpleNamespace(config=object()))


@pytest.mark.parametrize("operation", ["capture", "restore", "publish-native"])
@pytest.mark.parametrize("engine", ["vllm", "sglang"])
def test_service_keeps_local_and_target_executables_separate(tmp_path, monkeypatch, operation, engine):
    from contextlib import nullcontext
    from sparkrun.plugins.coldsnap import service as service_module
    local = bundle(tmp_path / "local", "amd64")
    remote = bundle(tmp_path / "remote", "arm64")
    selected = target_tools.TargetTools("arm64", remote.payload_verifier(engine), remote.criu_rpc_path)
    monkeypatch.setattr(service_module, "resolve_coldsnap_policy", lambda **kwargs: SimpleNamespace(state_root="/state", recovery_read="direct"))
    seen = []
    service = ColdSnapService(
        tool_resolver=lambda config: local,
        target_tool_resolver=lambda **kwargs: selected,
        host_provider_factory=lambda *args, **kwargs: nullcontext(None),
        run_command=lambda args, **kwargs: seen.append((args, kwargs)) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    service._invoke({"id": "test", "operation": operation, "launch": {"engine": engine, "units": [{"host": "spark"}], "execution": {"workers": []}}},
                    prepare_only=False, capture_output=False, sctx=SimpleNamespace(config=object()), cluster=object())
    args, kwargs = seen[0]
    assert args[0] == str(local.path)
    assert kwargs["env"]["COLDSNAP_VLLM_ADAPTER"] == str(local.adapter_path)
    assert kwargs["env"]["COLDSNAP_CRIU_RPC"] == str(local.criu_rpc_path)
    assert kwargs["env"]["COLDSNAP_TARGET_PAYLOAD_VERIFIER"] == str(remote.payload_verifier(engine))
    assert kwargs["env"]["COLDSNAP_TARGET_CRIU_RPC"] == str(remote.criu_rpc_path)


def test_target_acquisition_keeps_order_and_target_platform(tmp_path, monkeypatch):
    calls = []
    config = SimpleNamespace(cache_dir=tmp_path, plugin_settings=lambda _: {"controller": {"version": "0.3.21", "commit": "a" * 40}})
    monkeypatch.setattr(tool, "_verify_cached", lambda *args, **kwargs: None)
    def github(*args, **kwargs):
        calls.append("github")
        assert args[3:5] == ("linux", "arm64") and kwargs["target_tools"]
        raise tool.ColdSnapToolError("not accessible")
    def oci(*args, **kwargs):
        calls.append("oci")
        assert args[4:6] == ("linux", "arm64") and kwargs["target_tools"]
        raise tool.ColdSnapToolError("not accessible")
    def source(*args, **kwargs):
        calls.append("source")
        assert args[3:5] == ("linux", "arm64") and kwargs["target_tools"]
        return "target-bundle"
    monkeypatch.setattr(tool, "install_controller_tool", github)
    monkeypatch.setattr(tool, "install_controller_tool_from_oci", oci)
    monkeypatch.setattr(tool, "install_controller_tool_from_ssh", source)
    assert tool.ensure_target_tool(config, "arm64") == "target-bundle"
    assert calls == ["github", "oci", "source"]
