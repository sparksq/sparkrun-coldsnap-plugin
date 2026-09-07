# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Read platform-specific binary image layers without a container daemon.

Only the fixed binary-bundle directory is materialized. Registry descriptors,
config platform, layer digests and sizes are checked before extraction; the
caller then verifies the release manifest and executable identities.
"""

import base64
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, parse_http_list, parse_keqv_list

from sparkrun.plugins.coldsnap.oci_artifacts import _docker_credential


class OCIBundleError(RuntimeError):
    pass


_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_MANIFEST_LIMIT = 2 << 20
_LAYER_LIMIT = 128 << 20
_TOTAL_LIMIT = 256 << 20
_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.docker.distribution.manifest.list.v2+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)


class _SafeRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if urlsplit(newurl).scheme != "https":
            raise OCIBundleError("OCI bundle redirect must use HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is not None and urlsplit(req.full_url).netloc != urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class _Registry:
    def __init__(self, repository, *, opener=None):
        first, _, rest = repository.partition("/")
        registry = first if "." in first or ":" in first or first == "localhost" else "docker.io"
        self.repository = rest if registry == first else repository
        self.registry = "docker.io" if registry in {"index.docker.io", "registry-1.docker.io"} else registry
        host = "registry-1.docker.io" if registry in {"docker.io", "index.docker.io", "registry-1.docker.io"} else registry
        if host == "registry-1.docker.io" and "/" not in self.repository:
            self.repository = "library/" + self.repository
        self.base = "https://%s/v2/%s/" % (host, self.repository)
        self.opener = opener or build_opener(_SafeRedirect())
        self.authorization = ""

    def _read(self, request, limit):
        with self.opener.open(request, timeout=60) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise OCIBundleError("OCI bundle response exceeds its size limit")
        return data

    def _authenticate(self, challenge):
        scheme, _, values = challenge.partition(" ")
        if scheme.lower() != "bearer":
            raise OCIBundleError("OCI registry did not offer bearer authentication")
        fields = parse_keqv_list(parse_http_list(values))
        realm = fields.get("realm", "")
        parsed = urlsplit(realm)
        registry_host = urlsplit(self.base).netloc
        allowed_hosts = {registry_host}
        if registry_host == "registry-1.docker.io":
            allowed_hosts.add("auth.docker.io")
        if parsed.scheme != "https" or parsed.netloc not in allowed_hosts or parsed.query or parsed.fragment:
            raise OCIBundleError("OCI registry authentication realm is not trusted")
        query = urlencode({"service": fields.get("service", ""), "scope": "repository:%s:pull" % self.repository})
        request = Request(realm + "?" + query)
        # Public release bundles work anonymously, including without Docker.
        # For private mirrors, reuse Docker's existing credential configuration.
        try:
            username, secret = _docker_credential(self.registry)
        except RuntimeError:
            username, secret = "", ""
        if username and secret:
            basic = base64.b64encode((username + ":" + secret).encode()).decode()
            request.add_header("Authorization", "Basic " + basic)
        document = json.loads(self._read(request, _MANIFEST_LIMIT))
        token = (document.get("token") or document.get("access_token")) if isinstance(document, dict) else None
        if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
            raise OCIBundleError("OCI registry returned no valid pull token")
        self.authorization = "Bearer " + token

    def get(self, path, limit):
        request = Request(self.base + path, headers={"Accept": _ACCEPT})
        if self.authorization:
            request.add_header("Authorization", self.authorization)
        try:
            return self._read(request, limit)
        except HTTPError as error:
            if error.code != 401:
                raise
            self._authenticate(error.headers.get("WWW-Authenticate", ""))
            request.add_header("Authorization", self.authorization)
            return self._read(request, limit)

    def descriptor(self, descriptor, kind, limit):
        if not isinstance(descriptor, dict):
            raise OCIBundleError("OCI bundle descriptor must be an object")
        digest, size = descriptor.get("digest"), descriptor.get("size")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest) or type(size) is not int or not 0 <= size <= limit:
            raise OCIBundleError("OCI bundle descriptor has an invalid digest or size")
        data = self.get(kind + "/" + digest, size)
        if len(data) != size or "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise OCIBundleError("OCI bundle descriptor checksum or size mismatch")
        return data


def fetch_binary_bundle(repository: str, version: str, os_name: str, arch: str, root: Path, *, opener=None) -> str:
    """Copy only the requested platform's bundle and return its immutable ref."""
    registry = _Registry(repository, opener=opener)
    data = registry.get("manifests/" + version, _MANIFEST_LIMIT)
    for _ in range(4):
        document = json.loads(data)
        if not isinstance(document, dict) or document.get("schemaVersion") != 2:
            raise OCIBundleError("OCI bundle manifest is invalid")
        if "manifests" not in document:
            break
        entries = document["manifests"]
        if not isinstance(entries, list):
            raise OCIBundleError("OCI bundle index is invalid")
        matches = [
            entry
            for entry in entries
            if isinstance(entry, dict)
            and isinstance(entry.get("platform"), dict)
            and entry.get("platform", {}).get("os") == os_name
            and entry.get("platform", {}).get("architecture") == arch
        ]
        if len(matches) != 1:
            raise OCIBundleError("OCI bundle must have exactly one %s/%s entry" % (os_name, arch))
        data = registry.descriptor(matches[0], "manifests", _MANIFEST_LIMIT)
    else:
        raise OCIBundleError("OCI bundle index nesting exceeds its limit")
    resolved = repository + "@sha256:" + hashlib.sha256(data).hexdigest()
    config = json.loads(registry.descriptor(document.get("config"), "blobs", _MANIFEST_LIMIT))
    if not isinstance(config, dict) or config.get("os") != os_name or config.get("architecture") != arch:
        raise OCIBundleError("OCI bundle image config does not match the requested platform")
    names = {"manifest.json", "coldsnap", "coldsnap-vllm-adapter", "coldsnap-sglang-adapter"}
    if os_name == "linux":
        names.add("coldsnap-criu-rpc")
    layers = document.get("layers")
    if not isinstance(layers, list) or not 1 <= len(layers) <= 16:
        raise OCIBundleError("OCI bundle layer inventory is invalid")
    found, expanded, compressed, members = set(), 0, 0, 0
    for layer in layers:
        if not isinstance(layer, dict) or layer.get("mediaType") not in {
            "application/vnd.oci.image.layer.v1.tar",
            "application/vnd.oci.image.layer.v1.tar+gzip",
            "application/vnd.docker.image.rootfs.diff.tar.gzip",
        }:
            raise OCIBundleError("OCI bundle layer media type is unsupported")
        payload = registry.descriptor(layer, "blobs", _LAYER_LIMIT)
        compressed += len(payload)
        if compressed > _TOTAL_LIMIT:
            raise OCIBundleError("OCI bundle exceeds its total size limit")
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r|*") as archive:
            for member in archive:
                expanded += member.size
                members += 1
                path = PurePosixPath(member.name)
                if path.is_absolute() or ".." in path.parts or expanded > _TOTAL_LIMIT or members > 4096:
                    raise OCIBundleError("OCI bundle archive has unsafe paths or exceeds its limits")
                if path.parent != PurePosixPath("opt/coldsnap/bin") or path.name not in names:
                    continue
                if not member.isfile() or path.name in found:
                    raise OCIBundleError("OCI bundle contains a linked or duplicate executable")
                stream = archive.extractfile(member)
                if stream is None:
                    raise OCIBundleError("OCI bundle member is unreadable")
                (root / path.name).write_bytes(stream.read())
                found.add(path.name)
    if found != names:
        raise OCIBundleError("OCI bundle is missing required files")
    return resolved
