# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from sparkrun.plugins.coldsnap import tool, target_tools
from test_coldsnap_tool import VERSION, COMMIT, REPOSITORY, OCI_REPOSITORY, _archive, _config, _write_source_build_contract


@pytest.mark.skipif(os.environ.get("COLDSNAP_TEST_RELEASE_DOWNLOAD") != "1", reason="opt-in published release qualification")
def test_published_macos_oci_tools_execute_natively(tmp_path):
    os_name, arch = tool._platform()
    if os_name != "darwin":
        pytest.skip("native macOS release qualification")
    selected = tool.install_controller_tool_from_oci(
        tmp_path,
        tool.DEFAULT_CONTROLLER_VERSION,
        tool.DEFAULT_RELEASE_REPOSITORY,
        tool.DEFAULT_BINARY_OCI_REPOSITORY,
        os_name,
        arch,
        commit=tool.DEFAULT_CONTROLLER_COMMIT,
    )
    assert selected.criu_rpc_path is None
    for executable in (selected.path, selected.adapter_path, selected.sglang_adapter_path):
        result = subprocess.run([str(executable), "version", "--json"], check=True, capture_output=True, text=True, timeout=30)
        assert json.loads(result.stdout) == {"version": tool.DEFAULT_CONTROLLER_VERSION, "commit": tool.DEFAULT_CONTROLLER_COMMIT}
    capabilities = subprocess.run(
        [str(selected.path), "capabilities"],
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **selected.environment},
        timeout=30,
    )
    assert isinstance(json.loads(capabilities.stdout), dict)


def macho(arch):
    data = bytearray(32)
    data[:4] = b"\xcf\xfa\xed\xfe"
    data[4:8] = {"amd64": 0x01000007, "arm64": 0x0100000C}[arch].to_bytes(4, "little")
    data[12:16] = (2).to_bytes(4, "little")
    return bytes(data)


@pytest.mark.parametrize(
    "system,machine,expected",
    [
        ("Darwin", "arm64", ("darwin", "arm64")),
        ("Darwin", "x86_64", ("darwin", "amd64")),
        ("Linux", "aarch64", ("linux", "arm64")),
        ("Linux", "AMD64", ("linux", "amd64")),
    ],
)
def test_control_platform_is_native_os_not_docker_vm(monkeypatch, system, machine, expected):
    monkeypatch.setattr(tool.platform, "system", lambda: system)
    monkeypatch.setattr(tool.platform, "machine", lambda: machine)
    assert tool._platform() == expected


def test_native_windows_remains_explicitly_unsupported(monkeypatch):
    monkeypatch.setattr(tool.platform, "system", lambda: "Windows")
    with pytest.raises(tool.ColdSnapToolError, match="Linux and macOS"):
        tool._platform()


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_github_macos_download_cache_and_linux_target_separation(tmp_path, monkeypatch, arch):
    monkeypatch.setattr(tool, "_platform", lambda: ("darwin", arch))
    calls = []

    def execute(args, **kwargs):
        calls.append((args, kwargs))
        assert Path(args[0]).parent.name == "darwin-" + arch
        assert "COLDSNAP_CRIU_RPC" not in kwargs["env"]
        return subprocess.CompletedProcess(args, 0, json.dumps({"version": VERSION, "commit": COMMIT}), "")

    monkeypatch.delenv("COLDSNAP_CRIU_RPC", raising=False)
    monkeypatch.setattr(tool.subprocess, "run", execute)
    archives = {f"{name}_{VERSION}_darwin_{arch}.tar.gz": _archive(name, macho(arch)) for name in tool._binaries("darwin")}
    payloads = {
        **archives,
        "checksums.txt": "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in archives.items()).encode(),
    }
    selected = tool.install_controller_tool(
        tmp_path,
        VERSION,
        REPOSITORY,
        "darwin",
        arch,
        commit=COMMIT,
        release_assets=lambda *args: {name: name for name in payloads},
        fetch_bytes=payloads.__getitem__,
    )
    assert selected.criu_rpc_path is None
    assert not selected.path.with_name("coldsnap-criu-rpc").exists()
    assert tool.ensure_controller_tool(_config(tmp_path, {"version": VERSION, "commit": COMMIT})).source == "cache"
    before = len(calls)
    assert tool._verify_cached(selected.path.parent, VERSION, COMMIT, target_arch=arch) is None
    assert len(calls) == before  # Never execute a Darwin tool as a Linux helper.
    wrong = selected.adapter_path
    wrong.write_bytes(macho("arm64" if arch == "amd64" else "amd64"))
    manifest_path = selected.path.with_name("manifest.json")
    manifest = json.loads(manifest_path.read_text())
    manifest["sha256"][wrong.name] = hashlib.sha256(wrong.read_bytes()).hexdigest()
    manifest_path.write_text(json.dumps(manifest))
    assert tool._verify_cached(selected.path.parent, VERSION, COMMIT) is None


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_macos_oci_install_uses_same_repository_without_docker(tmp_path, monkeypatch, arch):
    from sparkrun.plugins.coldsnap import oci_bundle

    calls = []

    def fetch(repository, version, os_name, target_arch, root):
        assert (repository, version, os_name, target_arch) == (OCI_REPOSITORY, VERSION, "darwin", arch)
        hashes = {}
        for name in tool._binaries(os_name):
            payload = macho(arch)
            (root / name).write_bytes(payload)
            hashes[name] = hashlib.sha256(payload).hexdigest()
        (root / "manifest.json").write_text(
            json.dumps(
                {
                    "format": 1,
                    "kind": "coldsnap-controller-binary-bundle",
                    "version": VERSION,
                    "commit": COMMIT,
                    "platform": "darwin-" + arch,
                    "sha256": hashes,
                }
            )
        )
        return OCI_REPOSITORY + "@sha256:" + "b" * 64

    monkeypatch.setattr(oci_bundle, "fetch_binary_bundle", fetch)
    monkeypatch.setattr(tool.shutil, "which", lambda name: pytest.fail("should not require Docker or ORAS"))
    monkeypatch.setattr(
        tool.subprocess,
        "run",
        lambda args, **kwargs: (
            calls.append(args) or subprocess.CompletedProcess(args, 0, json.dumps({"version": VERSION, "commit": COMMIT}), "")
        ),
    )
    selected = tool.install_controller_tool_from_oci(tmp_path, VERSION, REPOSITORY, OCI_REPOSITORY, "darwin", arch, commit=COMMIT)
    assert selected.source == "oci" and selected.criu_rpc_path is None
    assert len(calls) == 1 and calls[0][1:] == ["version", "--json"]


def test_macos_source_build_runs_linux_builder_but_produces_only_darwin_tools(tmp_path, monkeypatch):
    source, output = tmp_path / "source", tmp_path / "out"
    _write_source_build_contract(source)
    monkeypatch.setattr(tool, "_platform", lambda: ("darwin", "arm64"))
    monkeypatch.setattr(tool.shutil, "which", lambda name: "/usr/bin/docker")

    def run(args, **kwargs):
        assert args[args.index("--platform") + 1] == "linux/arm64"
        assert "GOOS=darwin" in args and "GOARCH=amd64" in args
        assert "coldsnap-criu-rpc" not in args[-1]
        for name in tool._binaries("darwin"):
            (output / name).write_bytes(macho("amd64"))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(tool.subprocess, "run", run)
    tool._build_controller_binaries_with_docker(source, output, VERSION, COMMIT, "darwin", "amd64")


def test_macos_explicit_controller_does_not_require_criu_and_cannot_supply_linux_targets(tmp_path, monkeypatch):
    monkeypatch.setattr(tool, "_platform", lambda: ("darwin", "arm64"))
    for name in tool._binaries("darwin"):
        path = tmp_path / name
        path.write_bytes(macho("arm64"))
        path.chmod(0o755)
    config = _config(tmp_path, {"path": str(tmp_path / "coldsnap"), "version": VERSION})
    assert tool.ensure_controller_tool(config).criu_rpc_path is None
    with pytest.raises(tool.ColdSnapToolError, match="release-matched Linux target bundle"):
        tool.ensure_target_tool(config, "arm64")


def test_explicit_macos_controller_never_reuses_sibling_adapter_on_linux(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(target_tools, "_platform", lambda: ("darwin", "arm64"))
    monkeypatch.delenv("COLDSNAP_TARGET_PAYLOAD_VERIFIER", raising=False)
    monkeypatch.delenv("COLDSNAP_TARGET_CRIU_RPC", raising=False)
    monkeypatch.setattr(target_tools, "explicit_controller_environment", lambda *a: pytest.fail("reused Mach-O as ELF"))
    monkeypatch.setattr("sparkrun.orchestration.primitives.build_ssh_kwargs", lambda c: {})
    monkeypatch.setattr(
        "sparkrun.orchestration.primitives.run_script_on_host",
        lambda *a, **kw: SimpleNamespace(success=True, stdout="Linux aarch64", stderr=""),
    )
    with pytest.raises(tool.ColdSnapToolError, match="COLDSNAP_TARGET_PAYLOAD_VERIFIER"):
        target_tools.prepare_target_tools(
            hosts=["spark"], engine="vllm", cluster=None, sctx=SimpleNamespace(config=object()), binary="/mac/coldsnap"
        )
