# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

import hashlib
import io
import json
import tarfile
from urllib.error import HTTPError
from urllib.request import Request

import pytest

from sparkrun.plugins.coldsnap import oci_bundle as oci


def encode(value):
    return json.dumps(value).encode()


class Registry:
    def __init__(self, arch="arm64", *, entries=None, config_os="darwin", malformed=None):
        self.urls, self.requests = {}, []
        self.base = "https://registry-1.docker.io/v2/scitrera/coldsnap-binaries/"
        files = (
            entries
            if entries is not None
            else [
                ("opt/coldsnap/bin/" + name, b"payload", tarfile.REGTYPE)
                for name in ("coldsnap", "coldsnap-vllm-adapter", "coldsnap-sglang-adapter", "manifest.json")
            ]
        )
        stream = io.BytesIO()
        with tarfile.open(fileobj=stream, mode="w:gz") as tar:
            for name, data, kind in files:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.size = len(data) if kind == tarfile.REGTYPE else 0
                member.linkname = "/outside" if kind != tarfile.REGTYPE else ""
                tar.addfile(member, io.BytesIO(data))
        layer = self.add(stream.getvalue(), "application/vnd.oci.image.layer.v1.tar+gzip")
        config = self.add(encode({"os": config_os, "architecture": arch}), "application/vnd.oci.image.config.v1+json")
        manifest = self.add(
            encode({"schemaVersion": 2, "config": config, "layers": [layer]}), "application/vnd.oci.image.manifest.v1+json", "manifests"
        )
        self.digest = manifest["digest"]
        self.layer = layer
        manifest["platform"] = {"os": "darwin", "architecture": arch}
        # All four platforms share this index; unselected blobs must not be read.
        entries = [
            {**manifest, "digest": "sha256:" + "0" * 64, "platform": {"os": os_name, "architecture": cpu}}
            for os_name, cpu in (("linux", "amd64"), ("linux", "arm64"), ("darwin", "amd64" if arch == "arm64" else "arm64"))
        ]
        entries.append(manifest)
        if malformed == "duplicate":
            entries.append(manifest)
        if malformed == "null-platform":
            entries.append({"platform": None})
        self.urls[self.base + "manifests/0.3.22"] = encode({"schemaVersion": 2, "manifests": entries})

    def add(self, data, media_type, kind="blobs"):
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        self.urls[self.base + kind + "/" + digest] = data
        return {"digest": digest, "size": len(data), "mediaType": media_type}

    def open(self, request, **kwargs):
        self.requests.append(request)
        assert request.full_url in self.urls, request.full_url
        return io.BytesIO(self.urls[request.full_url])

    def fetch(self, root, arch="arm64"):
        return oci.fetch_binary_bundle("docker.io/scitrera/coldsnap-binaries", "0.3.22", "darwin", arch, root, opener=self)


@pytest.mark.parametrize("arch", ["amd64", "arm64"])
def test_four_platform_index_reads_only_selected_mac_payload(tmp_path, arch):
    registry = Registry(arch)
    assert registry.fetch(tmp_path, arch).endswith("@" + registry.digest)
    assert len(list(tmp_path.iterdir())) == 4
    assert len(registry.requests) == 4  # index, selected manifest, config, layer
    assert all("0" * 64 not in request.full_url for request in registry.requests)


@pytest.mark.parametrize("mutation", ["digest", "size", "config", "duplicate"])
def test_wrong_descriptor_or_platform_fails_closed(tmp_path, mutation):
    registry = Registry(config_os="linux" if mutation == "config" else "darwin", malformed=mutation)
    key = registry.base + "blobs/" + registry.layer["digest"]
    if mutation == "digest":
        registry.urls[key] = b"x" * len(registry.urls[key])
    elif mutation == "size":
        registry.urls[key] += b"x"
    with pytest.raises(oci.OCIBundleError):
        registry.fetch(tmp_path)


@pytest.mark.parametrize(
    "name,kind",
    [
        ("../escape", tarfile.REGTYPE),
        ("/absolute", tarfile.REGTYPE),
        ("opt/coldsnap/bin/coldsnap", tarfile.SYMTYPE),
        ("opt/coldsnap/bin/coldsnap", tarfile.LNKTYPE),
    ],
)
def test_archive_cannot_escape_or_link_to_host_files(tmp_path, name, kind):
    registry = Registry(entries=[(name, b"data", kind)])
    with pytest.raises(oci.OCIBundleError):
        registry.fetch(tmp_path)
    assert not list(tmp_path.iterdir())


def test_duplicate_file_and_missing_inventory_are_rejected(tmp_path):
    entry = ("opt/coldsnap/bin/coldsnap", b"data", tarfile.REGTYPE)
    for entries in ([entry, entry], [entry]):
        with pytest.raises(oci.OCIBundleError):
            Registry(entries=entries).fetch(tmp_path)


def test_unrelated_null_platform_does_not_break_selection(tmp_path):
    Registry(malformed="null-platform").fetch(tmp_path)


def test_archive_limits_apply_to_expanded_size(tmp_path, monkeypatch):
    monkeypatch.setattr(oci, "_TOTAL_LIMIT", 10)
    with pytest.raises(oci.OCIBundleError, match="limit"):
        Registry().fetch(tmp_path)


def test_redirect_drops_credentials_and_rejects_http():
    handler = oci._SafeRedirect()
    request = Request("https://registry.example/blob", headers={"Authorization": "Bearer secret"})
    redirected = handler.redirect_request(request, None, 302, "", {}, "https://cdn.example/blob")
    assert not redirected.has_header("Authorization")
    same_host = handler.redirect_request(request, None, 302, "", {}, "https://registry.example/other")
    assert same_host.get_header("Authorization") == "Bearer secret"
    with pytest.raises(oci.OCIBundleError, match="HTTPS"):
        handler.redirect_request(request, None, 302, "", {}, "http://registry.example/blob")


def test_untrusted_token_realm_never_receives_credentials(monkeypatch):
    monkeypatch.setattr(oci, "_docker_credential", lambda *a: pytest.fail("credential lookup before realm validation"))
    registry = oci._Registry("docker.io/scitrera/coldsnap-binaries")
    with pytest.raises(oci.OCIBundleError, match="not trusted"):
        registry._authenticate('Bearer realm="https://evil.example/token",service="docker.io"')


def test_dockerhub_authentication_uses_pull_scope_and_existing_credentials(monkeypatch):
    monkeypatch.setattr(oci, "_docker_credential", lambda *a: ("user", "secret"))
    requests = []

    class Opener:
        def open(self, request, **kwargs):
            requests.append(request)
            if "auth.docker.io" in request.full_url:
                assert "scope=repository%3Ascitrera%2Fcoldsnap-binaries%3Apull" in request.full_url
                assert request.get_header("Authorization").startswith("Basic ")
                return io.BytesIO(b'{"token":"pull-token"}')
            if request.get_header("Authorization") != "Bearer pull-token":
                raise HTTPError(
                    request.full_url,
                    401,
                    "auth",
                    {
                        "WWW-Authenticate": 'Bearer realm="https://auth.docker.io/token",service="registry.docker.io",scope="ignored:push"',
                    },
                    None,
                )
            return io.BytesIO(b"success")

    registry = oci._Registry("docker.io/scitrera/coldsnap-binaries", opener=Opener())
    assert registry.get("manifests/0.3.22", 100) == b"success"
    assert len(requests) == 3
