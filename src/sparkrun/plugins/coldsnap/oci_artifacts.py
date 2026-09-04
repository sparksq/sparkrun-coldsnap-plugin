# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Controller-side OCI transport for small ColdSnap descriptors."""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sparkrun.core.progress import PROGRESS, progress_heartbeat
from sparkrun.orchestration.job_metadata import derive_recipe_fingerprint
from sparkrun.plugins.coldsnap.config import ColdSnapRecipe

RunCommand = Callable[..., subprocess.CompletedProcess]
logger = logging.getLogger(__name__)
_CONTAINER_ARTIFACT = "/coldsnap/artifact.json"
_OCI_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")


@dataclass(frozen=True)
class StagedOCIArtifact:
    path: Path
    requested_reference: str
    resolved_reference: str


def configured_artifact_reference(*, plan, options, snapshot_driver: str = "n610") -> str:
    """Return the explicit reference or the recipe's stable default OCI tag."""

    config = plan.recipe.plugin_item("coldsnap")
    if not isinstance(config, ColdSnapRecipe):
        raise ValueError("recipe does not declare a valid top-level coldsnap item")
    if config.artifact.reference:
        return config.artifact.reference
    return default_artifact_publish_reference(plan=plan, options=options, snapshot_driver=snapshot_driver)


def default_artifact_publish_reference(*, plan, options, snapshot_driver: str = "n610") -> str:
    """Return the stable descriptor tag derived from the capsule repository."""

    config = plan.recipe.plugin_item("coldsnap")
    if not isinstance(config, ColdSnapRecipe):
        raise ValueError("recipe does not declare a valid top-level coldsnap item")
    if not config.capsule.repository:
        return ""
    if snapshot_driver not in {"n580", "n610"}:
        raise ValueError("snapshot_driver must be n580 or n610")
    fingerprint = derive_recipe_fingerprint(plan.recipe, getattr(options, "overrides", None))
    return "oci://%s:coldsnap-artifact-%s-%s" % (config.capsule.repository, fingerprint[:12], snapshot_driver)


def stage_oci_artifact(
    reference: str,
    destination: Path,
    *,
    docker: str = "docker",
    run_command: RunCommand = subprocess.run,
    snapshot_driver: str | None = None,
) -> StagedOCIArtifact:
    """Pull, verify, and atomically stage one descriptor on the controller."""

    raw = _raw_reference(reference)
    destination = destination.expanduser().resolve()
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    logger.log(PROGRESS, "ColdSnap: pulling artifact descriptor %s", reference)
    with progress_heartbeat(logger, "ColdSnap: artifact descriptor pull"):
        _checked(run_command, [docker, "pull", raw])
    resolved = _resolved_reference(raw, docker=docker, run_command=run_command)
    name = "sparkrun-coldsnap-artifact-" + uuid.uuid4().hex[:16]
    temporary_directory = Path(tempfile.mkdtemp(prefix=".coldsnap-artifact-", dir=destination.parent))
    temporary = temporary_directory / "artifact.json"
    try:
        _checked(run_command, [docker, "create", "--name", name, raw, _CONTAINER_ARTIFACT])
        _checked(run_command, [docker, "cp", name + ":" + _CONTAINER_ARTIFACT, str(temporary)])
        _read_committed_artifact(temporary, snapshot_driver=snapshot_driver)
        os.chmod(temporary, 0o600)
        os.replace(temporary, destination)
    finally:
        run_command([docker, "rm", "-f", name], text=True, check=False, capture_output=True)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        try:
            temporary_directory.rmdir()
        except OSError:
            pass
    logger.info("ColdSnap artifact descriptor resolved %s -> %s", reference, resolved)
    return StagedOCIArtifact(destination, reference, resolved)


def publish_oci_artifact(
    artifact: Path,
    reference: str,
    *,
    docker: str = "docker",
    run_command: RunCommand = subprocess.run,
    snapshot_driver: str | None = None,
) -> str:
    """Package and push an accepted descriptor, returning an immutable OCI ref."""

    raw = _raw_reference(reference)
    if "@" in raw:
        raise ValueError("ColdSnap artifact publication destination must be an OCI tag")
    payload = artifact.expanduser().resolve().read_bytes()
    _validate_committed_artifact(json.loads(payload), snapshot_driver=snapshot_driver)
    logger.log(PROGRESS, "ColdSnap: publishing artifact descriptor %s", reference)
    with tempfile.TemporaryDirectory(prefix="sparkrun-coldsnap-artifact-") as directory:
        context = Path(directory)
        (context / "artifact.json").write_bytes(payload)
        (context / "Dockerfile").write_text(
            'FROM scratch\nCOPY artifact.json /coldsnap/artifact.json\nLABEL io.sparksq.coldsnap.artifact="true"\n',
            encoding="utf-8",
        )
        with progress_heartbeat(logger, "ColdSnap: artifact descriptor publish"):
            _checked(run_command, [docker, "build", "--quiet", "--tag", raw, str(context)])
            _checked(run_command, [docker, "push", raw])
    resolved = _resolved_reference(raw, docker=docker, run_command=run_command)
    logger.log(PROGRESS, "ColdSnap artifact descriptor published: %s", resolved)
    return resolved


def delete_oci_tag(reference: str, *, opener=urlopen) -> None:
    """Delete one exact mutable OCI tag using controller-side credentials.

    Docker Hub exposes tag deletion through its Hub API. Other registries use
    the Distribution API's manifest deletion endpoint after resolving the tag
    to a digest. The caller must provide a tag, never a repository digest, so a
    recipe cannot accidentally turn a shared digest into an ambiguous delete.
    """

    raw = reference.removeprefix("oci://")
    registry, repository, tag = _split_tagged_reference(raw)
    username, secret = _docker_credential(registry)
    if registry == "docker.io":
        _delete_docker_hub_tag(repository, tag, username, secret, opener=opener)
        return
    _delete_distribution_tag(registry, repository, tag, username, secret, opener=opener)


def _split_tagged_reference(reference: str) -> tuple[str, str, str]:
    if not reference or any(character in reference for character in "@ \t\r\n\x00"):
        raise ValueError("ColdSnap OCI deletion requires a tagged repository reference")
    first, separator, rest = reference.partition("/")
    if separator and ("." in first or ":" in first or first == "localhost"):
        registry = first.lower()
        repository_and_tag = rest
    else:
        registry = "docker.io"
        repository_and_tag = reference
    colon = repository_and_tag.rfind(":")
    slash = repository_and_tag.rfind("/")
    if colon <= slash:
        raise ValueError("ColdSnap OCI deletion requires an explicit tag")
    repository = repository_and_tag[:colon]
    tag = repository_and_tag[colon + 1 :]
    if registry in {"index.docker.io", "registry-1.docker.io"}:
        registry = "docker.io"
    if registry == "docker.io" and "/" not in repository:
        repository = "library/" + repository
    if not repository or any(part in {"", ".", ".."} for part in repository.split("/")) or not _OCI_TAG.fullmatch(tag):
        raise ValueError("ColdSnap OCI deletion reference is invalid")
    return registry, repository, tag


def _docker_credential(registry: str) -> tuple[str, str]:
    config_path = Path(os.environ.get("DOCKER_CONFIG") or (Path.home() / ".docker")) / "config.json"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Docker credentials are unavailable for %s" % registry) from error
    candidates = [registry]
    if registry == "docker.io":
        candidates.extend(["https://index.docker.io/v1/", "index.docker.io", "registry-1.docker.io"])
    if not isinstance(config, dict):
        raise RuntimeError("Docker credential configuration is invalid")
    auths = config.get("auths")
    if isinstance(auths, dict):
        for candidate in candidates:
            entry = auths.get(candidate)
            encoded = entry.get("auth") if isinstance(entry, dict) else None
            if not isinstance(encoded, str) or not encoded:
                continue
            try:
                decoded = base64.b64decode(encoded).decode("utf-8")
                username, separator, secret = decoded.partition(":")
            except (ValueError, UnicodeDecodeError):
                continue
            if separator and username and secret:
                return username, secret
    helper = config.get("credHelpers", {}).get(registry) if isinstance(config.get("credHelpers"), dict) else None
    helper = helper or config.get("credsStore")
    executable = shutil.which("docker-credential-%s" % helper) if isinstance(helper, str) and helper else None
    if executable:
        server = "https://index.docker.io/v1/" if registry == "docker.io" else registry
        result = subprocess.run(
            [executable, "get"],
            input=server + "\n",
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        try:
            credential = json.loads(result.stdout)
        except json.JSONDecodeError:
            credential = {}
        username = credential.get("Username") if isinstance(credential, dict) else None
        secret = credential.get("Secret") if isinstance(credential, dict) else None
        if result.returncode == 0 and isinstance(username, str) and username and isinstance(secret, str) and secret:
            return username, secret
    raise RuntimeError("Docker credentials are unavailable for %s" % registry)


def _delete_docker_hub_tag(repository: str, tag: str, username: str, secret: str, *, opener) -> None:
    login = Request(
        "https://hub.docker.com/v2/users/login/",
        data=json.dumps({"username": username, "password": secret}).encode("utf-8"),
        headers={"Content-Type": "application/json", "User-Agent": "sparkrun-coldsnap"},
        method="POST",
    )
    status, _headers, body = _http(login, opener=opener)
    try:
        token = json.loads(body).get("token")
    except (AttributeError, json.JSONDecodeError):
        token = None
    if status not in {200, 201} or not isinstance(token, str) or not token:
        raise RuntimeError("Docker Hub authentication failed for published ColdSnap deletion")
    request = Request(
        "https://hub.docker.com/v2/repositories/%s/tags/%s/" % (repository, tag),
        headers={"Authorization": "JWT " + token, "User-Agent": "sparkrun-coldsnap"},
        method="DELETE",
    )
    status, _headers, _body = _http(request, opener=opener)
    if status not in {202, 204, 404}:
        raise RuntimeError("Docker Hub refused ColdSnap tag deletion with HTTP %d" % status)


def _delete_distribution_tag(
    registry: str,
    repository: str,
    tag: str,
    username: str,
    secret: str,
    *,
    opener,
) -> None:
    base = "https://%s/v2/%s/manifests/" % (registry, repository)
    accept = ", ".join(
        (
            "application/vnd.oci.image.manifest.v1+json",
            "application/vnd.oci.image.index.v1+json",
            "application/vnd.docker.distribution.manifest.v2+json",
            "application/vnd.docker.distribution.manifest.list.v2+json",
        )
    )
    request = Request(base + tag, headers={"Accept": accept, "User-Agent": "sparkrun-coldsnap"}, method="HEAD")
    status, headers, _body = _http(request, opener=opener)
    authorization = ""
    if status == 401:
        authorization = _registry_bearer(headers.get("WWW-Authenticate", ""), username, secret, opener=opener)
        request = Request(
            base + tag,
            headers={"Accept": accept, "Authorization": authorization, "User-Agent": "sparkrun-coldsnap"},
            method="HEAD",
        )
        status, headers, _body = _http(request, opener=opener)
    if status == 404:
        return
    digest = headers.get("Docker-Content-Digest", "")
    if status != 200 or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise RuntimeError("OCI registry could not resolve ColdSnap tag %s" % tag)
    delete_headers = {"Accept": accept, "User-Agent": "sparkrun-coldsnap"}
    if authorization:
        delete_headers["Authorization"] = authorization
    status, _headers, _body = _http(Request(base + digest, headers=delete_headers, method="DELETE"), opener=opener)
    if status not in {202, 204, 404}:
        raise RuntimeError("OCI registry refused ColdSnap manifest deletion with HTTP %d" % status)


def _registry_bearer(challenge: str, username: str, secret: str, *, opener) -> str:
    if not challenge.lower().startswith("bearer "):
        raise RuntimeError("OCI registry did not offer bearer authentication")
    values = dict(re.findall(r'(\w+)="([^"]*)"', challenge[7:]))
    realm = values.pop("realm", "")
    if not realm.startswith("https://"):
        raise RuntimeError("OCI registry authentication realm is invalid")
    separator = "&" if "?" in realm else "?"
    token_request = Request(
        realm + separator + urlencode(values),
        headers={
            "Authorization": "Basic " + base64.b64encode((username + ":" + secret).encode("utf-8")).decode("ascii"),
            "User-Agent": "sparkrun-coldsnap",
        },
    )
    status, _headers, body = _http(token_request, opener=opener)
    try:
        payload = json.loads(body)
        token = payload.get("token") or payload.get("access_token")
    except (AttributeError, json.JSONDecodeError):
        token = None
    if status != 200 or not isinstance(token, str) or not token:
        raise RuntimeError("OCI registry bearer authentication failed")
    return "Bearer " + token


def _http(request: Request, *, opener) -> tuple[int, Any, bytes]:
    try:
        with opener(request, timeout=30) as response:
            return int(response.status), response.headers, response.read()
    except HTTPError as error:
        return int(error.code), error.headers, error.read()


def _raw_reference(reference: str) -> str:
    if not reference.startswith("oci://") or len(reference) == len("oci://"):
        raise ValueError("ColdSnap artifact reference must use oci://")
    return reference.removeprefix("oci://")


def _resolved_reference(raw: str, *, docker: str, run_command: RunCommand) -> str:
    completed = _checked(
        run_command,
        [docker, "image", "inspect", "--format", "{{json .RepoDigests}}", raw],
        capture_output=True,
    )
    try:
        digests = json.loads(completed.stdout)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Docker returned an invalid ColdSnap artifact digest inventory") from error
    if not isinstance(digests, list) or not digests or not all(isinstance(item, str) for item in digests):
        raise RuntimeError("ColdSnap artifact image has no immutable repository digest")
    if "@" in raw:
        expected = raw.rpartition("@")[2]
        matching = [item for item in digests if item.endswith("@" + expected)]
        if not matching:
            raise RuntimeError("pulled ColdSnap artifact does not match its requested digest")
        return "oci://" + matching[0]
    return "oci://" + digests[0]


def _read_committed_artifact(path: Path, *, snapshot_driver: str | None = None) -> dict[str, Any]:
    try:
        artifact = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("ColdSnap OCI descriptor is not valid JSON") from error
    _validate_committed_artifact(artifact, snapshot_driver=snapshot_driver)
    return artifact


def _validate_committed_artifact(artifact: Any, *, snapshot_driver: str | None = None) -> None:
    if not isinstance(artifact, dict) or artifact.get("kind") != "coldsnap-snapshot-artifact" or artifact.get("state") != "committed":
        raise RuntimeError("OCI image does not contain a committed ColdSnap artifact")
    driver = artifact.get("snapshot_driver")
    driver_id = driver.get("id") if isinstance(driver, dict) else None
    if driver_id not in {"n580", "n610"}:
        raise RuntimeError("ColdSnap OCI descriptor has no supported snapshot driver")
    if snapshot_driver is not None and driver_id != snapshot_driver:
        raise RuntimeError("ColdSnap OCI descriptor uses snapshot driver %s, not selected driver %s" % (driver_id, snapshot_driver))
    try:
        images = artifact["capsule"]["images"]
    except (KeyError, TypeError) as error:
        raise RuntimeError("ColdSnap OCI descriptor has no capsule inventory") from error
    if not isinstance(images, list) or not images:
        raise RuntimeError("ColdSnap OCI descriptor has no capsule inventory")
    seen_units: set[str] = set()
    for image in images:
        unit = image.get("unit") if isinstance(image, dict) else None
        if not isinstance(unit, str) or not unit or unit in seen_units:
            raise RuntimeError("ColdSnap OCI descriptor capsule units are invalid")
        seen_units.add(unit)
        reference = image.get("reference")
        digest = image.get("digest")
        if not isinstance(reference, str) or not isinstance(digest, str) or not reference.endswith(digest):
            raise RuntimeError("ColdSnap OCI descriptor capsules are not digest-pinned")


def _checked(run_command: RunCommand, arguments: list[str], *, capture_output: bool = False):
    completed = run_command(arguments, text=True, check=False, capture_output=capture_output)
    if completed.returncode:
        detail = str(getattr(completed, "stderr", "") or "").strip()
        raise RuntimeError(
            "ColdSnap OCI artifact command failed: %s%s" % (" ".join(arguments[:3]), ": " + detail[-2000:] if detail else "")
        )
    return completed


__all__ = [
    "StagedOCIArtifact",
    "configured_artifact_reference",
    "delete_oci_tag",
    "default_artifact_publish_reference",
    "publish_oci_artifact",
    "stage_oci_artifact",
]
