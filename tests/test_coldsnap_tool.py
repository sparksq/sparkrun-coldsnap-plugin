# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

import hashlib
import io
import json
import subprocess
import tarfile
import urllib.error
from io import BytesIO
from types import SimpleNamespace

import pytest

import sparkrun.plugins.coldsnap.tool as coldsnap_tool
from sparkrun.core.config import SparkrunConfig
from sparkrun.plugins.coldsnap.tool import (
    ColdSnapReleaseAccessError,
    ColdSnapToolError,
    ControllerTool,
    _build_controller_binaries_with_docker,
    _fetch_release,
    _source_build_toolchain,
    _settings,
    ensure_controller_tool,
    install_controller_tool,
    install_controller_tool_from_ssh,
)

VERSION = "0.3.13"
COMMIT = "a" * 40
REPOSITORY = "sparksq/coldsnap"
GO_VERSION = "1.25.14"
GO_BUILDER_IMAGE = "golang:1.25.14@sha256:699337d620559a59b4a2bb298ad59611e535d2ee755a34cf2d2a98f37578dc80"


def _config(cache_dir, controller):
    return SimpleNamespace(
        cache_dir=cache_dir,
        plugin_settings=lambda name: {"controller": controller} if name == "coldsnap" else {},
    )


def _archive(name: str, payload: bytes) -> bytes:
    result = BytesIO()
    with tarfile.open(fileobj=result, mode="w:gz") as bundle:
        member = tarfile.TarInfo("./" + name)
        member.mode = 0o755
        member.size = len(payload)
        bundle.addfile(member, io.BytesIO(payload))
    return result.getvalue()


def _controller_script(version: str = VERSION, commit: str = COMMIT) -> bytes:
    identity = json.dumps({"version": version, "commit": commit})
    return f"""#!/bin/sh
if [ "$1" = version ]; then
  printf '%s\\n' '{identity}'
  exit 0
fi
exit 9
""".encode()


def _write_source_build_contract(source, *, go_version=GO_VERSION, builder_version=GO_VERSION):
    source.mkdir(parents=True, exist_ok=True)
    (source / "go.mod").write_text("module github.com/sparksq/coldsnap\n\ngo %s\n" % go_version)
    dockerfile = source / "deploy" / "vllm" / "Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text("FROM golang:%s@sha256:%s AS coldsnap_builder\n" % (builder_version, GO_BUILDER_IMAGE.rsplit("sha256:", 1)[1]))


def test_controller_tool_selects_engine_adapter_as_payload_verifier(tmp_path):
    vllm = tmp_path / "coldsnap-vllm-adapter"
    sglang = tmp_path / "coldsnap-sglang-adapter"
    tool = ControllerTool(tmp_path / "coldsnap", vllm, VERSION, "test", sglang)

    assert tool.payload_verifier("vllm") == vllm
    assert tool.payload_verifier("sglang") == sglang
    with pytest.raises(ColdSnapToolError, match="no payload verifier"):
        tool.payload_verifier("unknown")


def _release_payloads():
    archives = {
        "coldsnap_%s_linux_amd64.tar.gz" % VERSION: _archive("coldsnap", _controller_script()),
        "coldsnap-vllm-adapter_%s_linux_amd64.tar.gz" % VERSION: _archive("coldsnap-vllm-adapter", b"#!/bin/sh\nexit 0\n"),
        "coldsnap-sglang-adapter_%s_linux_amd64.tar.gz" % VERSION: _archive("coldsnap-sglang-adapter", b"#!/bin/sh\nexit 0\n"),
        "coldsnap-criu-rpc_%s_linux_amd64.tar.gz" % VERSION: _archive("coldsnap-criu-rpc", b"#!/bin/sh\nexit 0\n"),
    }
    checksums = "".join("%s  ./%s\n" % (hashlib.sha256(payload).hexdigest(), name) for name, payload in archives.items()).encode()
    payloads = {"memory://checksums": checksums}
    assets = {"checksums.txt": "memory://checksums"}
    for name, payload in archives.items():
        url = "memory://" + name
        assets[name] = url
        payloads[url] = payload
    return assets, payloads


def test_install_controller_tool_verifies_and_caches_matching_pair(tmp_path, monkeypatch):
    assets, payloads = _release_payloads()
    tool = install_controller_tool(
        tmp_path,
        VERSION,
        REPOSITORY,
        "linux",
        "amd64",
        commit=COMMIT,
        release_assets=lambda _repository, _version: assets,
        fetch_bytes=payloads.__getitem__,
    )

    assert tool.version == VERSION
    assert tool.source == "download"
    assert tool.path.is_file()
    assert tool.adapter_path.is_file()
    assert tool.sglang_adapter_path.is_file()
    assert tool.criu_rpc_path.is_file()
    manifest = json.loads(tool.path.with_name("manifest.json").read_text())
    assert manifest["repository"] == REPOSITORY
    assert manifest["commit"] == COMMIT
    assert manifest["platform"] == "linux-amd64"
    assert set(manifest["sha256"]) == {
        "coldsnap",
        "coldsnap-vllm-adapter",
        "coldsnap-sglang-adapter",
        "coldsnap-criu-rpc",
    }

    monkeypatch.setattr("sparkrun.plugins.coldsnap.tool._platform", lambda: ("linux", "amd64"))
    config = _config(tmp_path, {"version": VERSION, "commit": COMMIT})
    cached = ensure_controller_tool(config)
    assert cached.source == "cache"
    assert cached.path == tool.path


def test_install_controller_tool_rejects_archive_checksum_mismatch(tmp_path):
    assets, payloads = _release_payloads()
    payloads[assets["checksums.txt"]] = b"0" * 64 + b"  coldsnap_0.3.13_linux_amd64.tar.gz\n"

    with pytest.raises(ColdSnapToolError, match="checksum mismatch"):
        install_controller_tool(
            tmp_path,
            VERSION,
            REPOSITORY,
            "linux",
            "amd64",
            commit=COMMIT,
            release_assets=lambda _repository, _version: assets,
            fetch_bytes=payloads.__getitem__,
        )


def test_private_release_404_is_classified_for_temporary_ssh_fallback(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise urllib.error.HTTPError("https://api.github.com/private", 404, "Not Found", {}, None)

    monkeypatch.setattr(coldsnap_tool, "_request_bytes", unavailable)

    with pytest.raises(ColdSnapReleaseAccessError, match="HTTP 404"):
        _fetch_release(REPOSITORY, VERSION, token="")


def test_ensure_controller_falls_back_only_for_release_access_error(tmp_path, monkeypatch):
    expected = ControllerTool(tmp_path / "coldsnap", tmp_path / "adapter", VERSION, "git-build", tmp_path / "sglang")
    calls = []
    monkeypatch.setattr(coldsnap_tool, "_platform", lambda: ("linux", "amd64"))
    monkeypatch.setattr(coldsnap_tool, "_verify_cached", lambda *_args: None)
    monkeypatch.setattr(
        coldsnap_tool,
        "install_controller_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ColdSnapReleaseAccessError("private release")),
    )
    monkeypatch.setattr(
        coldsnap_tool,
        "install_controller_tool_from_ssh",
        lambda *args, **kwargs: calls.append((args, kwargs)) or expected,
    )

    actual = ensure_controller_tool(_config(tmp_path, {"version": VERSION, "commit": COMMIT}))

    assert actual is expected
    assert calls[0][0][1:5] == (VERSION, REPOSITORY, "linux", "amd64")
    assert calls[0][1] == {"commit": COMMIT}

    monkeypatch.setattr(
        coldsnap_tool,
        "install_controller_tool",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ColdSnapToolError("checksum mismatch")),
    )
    calls.clear()
    with pytest.raises(ColdSnapToolError, match="checksum mismatch"):
        ensure_controller_tool(_config(tmp_path, {"version": VERSION, "commit": COMMIT}))
    assert calls == []


def test_ssh_fallback_installs_verified_source_built_tools(tmp_path, monkeypatch):
    remote = "git@github.com:sparksq/coldsnap.git"

    def clone(source, repository, version, commit):
        source.mkdir(parents=True)
        assert (repository, version, commit) == (REPOSITORY, VERSION, COMMIT)
        return remote

    def build(_source, output, version, commit, os_name, arch):
        output.mkdir(parents=True)
        assert (version, commit, os_name, arch) == (VERSION, COMMIT, "linux", "amd64")
        (output / "coldsnap").write_bytes(_controller_script())
        (output / "coldsnap-vllm-adapter").write_text("#!/bin/sh\nexit 0\n")
        (output / "coldsnap-sglang-adapter").write_text("#!/bin/sh\nexit 0\n")
        (output / "coldsnap-criu-rpc").write_text("#!/bin/sh\nexit 0\n")
        return GO_VERSION, GO_BUILDER_IMAGE

    monkeypatch.setattr(coldsnap_tool, "_clone_pinned_source", clone)
    monkeypatch.setattr(coldsnap_tool, "_clone_pinned_go_criu", lambda _destination: None)
    monkeypatch.setattr(coldsnap_tool, "_build_controller_binaries_with_docker", build)

    tool = install_controller_tool_from_ssh(
        tmp_path,
        VERSION,
        REPOSITORY,
        "linux",
        "amd64",
        commit=COMMIT,
    )

    assert tool.source == "git-build"
    manifest = json.loads(tool.path.with_name("manifest.json").read_text())
    assert manifest["source_build"] == {
        "remote": remote,
        "tag": "v0.3.13",
        "go_version": GO_VERSION,
        "builder_image": GO_BUILDER_IMAGE,
    }
    assert manifest["archives"] == {}


def test_temporary_source_build_uses_pinned_dockerized_go_toolchain(tmp_path, monkeypatch):
    source = tmp_path / "source"
    output = tmp_path / "out"
    _write_source_build_contract(source)
    seen = []
    monkeypatch.setattr(coldsnap_tool.shutil, "which", lambda name: "/usr/bin/docker" if name == "docker" else None)

    def run(arguments, **_kwargs):
        seen.append(arguments)
        output.mkdir(exist_ok=True)
        for name in (
            "coldsnap",
            "coldsnap-vllm-adapter",
            "coldsnap-sglang-adapter",
            "coldsnap-criu-rpc",
        ):
            (output / name).write_text("binary")
        return subprocess.CompletedProcess(arguments, 0, "", "")

    monkeypatch.setattr(coldsnap_tool.subprocess, "run", run)

    go_version, builder_image = _build_controller_binaries_with_docker(source, output, VERSION, COMMIT, "linux", "amd64")

    command = seen[0]
    assert command[:3] == ["/usr/bin/docker", "run", "--rm"]
    assert go_version == GO_VERSION
    assert builder_image == GO_BUILDER_IMAGE
    assert GO_BUILDER_IMAGE in command
    assert "GOOS=linux" in command
    assert "GOARCH=amd64" in command
    assert "COLDSNAP_BUILD_VERSION=0.3.13" in command
    assert "go build -trimpath" in command[-1]


def test_source_build_toolchain_rejects_go_mod_builder_mismatch(tmp_path):
    source = tmp_path / "source"
    _write_source_build_contract(source, builder_version="1.25.13")

    with pytest.raises(ColdSnapToolError, match="go.mod requires 1.25.14.*provides 1.25.13"):
        _source_build_toolchain(source)


def test_configured_controller_requires_and_exports_sibling_adapter(tmp_path):
    controller = tmp_path / "coldsnap"
    adapter = tmp_path / "coldsnap-vllm-adapter"
    sglang_adapter = tmp_path / "coldsnap-sglang-adapter"
    criu_rpc = tmp_path / "coldsnap-criu-rpc"
    controller.write_bytes(_controller_script())
    adapter.write_text("#!/bin/sh\nexit 0\n")
    sglang_adapter.write_text("#!/bin/sh\nexit 0\n")
    criu_rpc.write_text("#!/bin/sh\nexit 0\n")
    controller.chmod(0o755)
    adapter.chmod(0o755)
    sglang_adapter.chmod(0o755)
    criu_rpc.chmod(0o755)
    config = _config(tmp_path / "cache", {"path": str(controller), "version": VERSION, "download": False})

    tool = ensure_controller_tool(config)

    assert tool.source == "config"
    assert tool.path == controller
    assert tool.environment == {
        "COLDSNAP_VLLM_ADAPTER": str(adapter),
        "COLDSNAP_SGLANG_ADAPTER": str(sglang_adapter),
        "COLDSNAP_CRIU_RPC": str(criu_rpc),
    }


def test_disabled_download_reports_missing_pinned_release(tmp_path):
    config = _config(tmp_path, {"version": VERSION, "download": False})
    with pytest.raises(ColdSnapToolError, match="not cached"):
        ensure_controller_tool(config)


def test_cached_controller_with_different_commit_is_rejected(tmp_path):
    assets, payloads = _release_payloads()
    install_controller_tool(
        tmp_path,
        VERSION,
        REPOSITORY,
        "linux",
        "amd64",
        commit=COMMIT,
        release_assets=lambda _repository, _version: assets,
        fetch_bytes=payloads.__getitem__,
    )
    config = _config(tmp_path, {"version": VERSION, "commit": "b" * 40, "download": False})

    with pytest.raises(ColdSnapToolError, match="not cached"):
        ensure_controller_tool(config)


def test_controller_settings_are_read_from_plugin_namespace(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        "plugins:\n"
        "  coldsnap:\n"
        "    controller:\n"
        "      version: 0.3.13\n"
        "      commit: %s\n"
        "      repository: internal/coldsnap\n"
        "      download: false\n" % COMMIT
    )

    config = SparkrunConfig(config_path=config_file)

    assert _settings(config) == (VERSION, COMMIT, "internal/coldsnap", "", False)


def test_service_binds_managed_adapter_environment(tmp_path):
    from sparkrun.plugins.coldsnap.service import ColdSnapService

    controller = tmp_path / "coldsnap"
    adapter = tmp_path / "coldsnap-vllm-adapter"
    seen = []

    def run_command(arguments, **kwargs):
        seen.append((arguments, kwargs))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    service = ColdSnapService(
        run_command=run_command,
        tool_resolver=lambda _config: ControllerTool(controller, adapter, VERSION, "cache"),
    )
    service._invoke(
        {"id": "restore-one", "operation": "restore", "launch": {"engine": "vllm", "units": [], "execution": {"workers": []}}},
        prepare_only=False,
        capture_output=False,
        sctx=SimpleNamespace(config=object()),
    )

    assert seen[0][0][0] == str(controller)
    assert seen[0][1]["env"]["COLDSNAP_VLLM_ADAPTER"] == str(adapter)


def test_service_passes_controller_hf_token_only_for_native_publication(tmp_path, monkeypatch):
    from sparkrun.plugins.coldsnap.service import ColdSnapService

    controller = tmp_path / "coldsnap"
    adapter = tmp_path / "coldsnap-vllm-adapter"
    seen = []
    monkeypatch.setattr("sparkrun.plugins.coldsnap.service.resolve_hf_token", lambda: "hf_controller_token")

    service = ColdSnapService(
        run_command=lambda arguments, **kwargs: seen.append((arguments, kwargs)) or SimpleNamespace(returncode=0, stdout="", stderr=""),
        tool_resolver=lambda _config: ControllerTool(controller, adapter, VERSION, "cache"),
    )
    service._invoke(
        {
            "id": "publish-native-one",
            "operation": "publish-native",
            "launch": {"engine": "vllm", "units": [], "execution": {"workers": []}},
        },
        prepare_only=False,
        capture_output=False,
        sctx=SimpleNamespace(config=object()),
    )

    assert seen[0][1]["env"]["HF_TOKEN"] == "hf_controller_token"
    assert seen[0][1]["env"]["COLDSNAP_VLLM_ADAPTER"] == str(adapter)
