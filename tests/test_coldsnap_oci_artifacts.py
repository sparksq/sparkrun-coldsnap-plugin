# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

import json
import shutil
from types import SimpleNamespace
from urllib.request import Request

import pytest

from sparkrun.core.recipe import Recipe
from sparkrun.plugins.coldsnap import register
from sparkrun.plugins.coldsnap.oci_artifacts import (
    _split_tagged_reference,
    _descriptor_image,
    _resolved_reference,
    _validate_committed_artifact,
    configured_artifact_reference,
    default_artifact_publish_reference,
    delete_oci_tag,
    publish_oci_artifact,
    stage_oci_artifact,
)

_DIGEST = "sha256:" + "a" * 64


def _artifact() -> dict:
    return {
        "format": 8,
        "kind": "coldsnap-snapshot-artifact",
        "state": "committed",
        "capture_id": "capture-one",
        "snapshot_driver": {"id": "n610", "abi": 1},
        "capsule": {
            "images": [
                {
                    "unit": "unit-0",
                    "reference": "docker.io/example/capsules@" + _DIGEST,
                    "digest": _DIGEST,
                }
            ]
        },
    }


def test_artifact_reference_uses_explicit_value_or_capsule_repository_default():
    register(None)
    recipe = Recipe.from_dict(
        {
            "recipe_version": "2",
            "runtime": "vllm-distributed",
            "model": "org/model",
            "model_revision": "commit",
            "container": "docker.io/example/vllm@" + _DIGEST,
            "coldsnap": {"capsule": {"repository": "docker.io/example/capsules"}},
        }
    )
    plan = SimpleNamespace(recipe=recipe)
    options = SimpleNamespace(overrides={})

    default = default_artifact_publish_reference(plan=plan, options=options)
    assert default.startswith("oci://docker.io/example/capsules:coldsnap-artifact-")
    assert default.endswith("-n610")
    assert default_artifact_publish_reference(plan=plan, options=options, snapshot_driver="n580").endswith("-n580")
    assert configured_artifact_reference(plan=plan, options=options) == default

    explicit = "oci://docker.io/example/descriptors@" + _DIGEST
    explicit_recipe = Recipe.from_dict(
        {
            **recipe.to_dict(),
            "coldsnap": {
                **recipe.to_dict()["coldsnap"],
                "artifact": {"reference": explicit},
            },
        }
    )
    plan.recipe = explicit_recipe
    assert configured_artifact_reference(plan=plan, options=options) == explicit
    plan.recipe = recipe
    assert default_artifact_publish_reference(plan=plan, options=options) == default


def test_stage_oci_artifact_verifies_and_atomically_installs_descriptor(tmp_path):
    source = tmp_path / "source.json"
    source.write_text(json.dumps(_artifact()), encoding="utf-8")
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        stdout = ""
        if arguments[1:3] == ["manifest", "inspect"]:
            stdout = json.dumps({"Descriptor": {"digest": _DIGEST, "platform": {"os": "linux", "architecture": "arm64"}}})
        elif arguments[1:3] == ["image", "inspect"]:
            stdout = json.dumps(["docker.io/example/capsules@" + _DIGEST])
        elif arguments[1] == "cp":
            shutil.copyfile(source, arguments[-1])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    destination = tmp_path / "store" / "imported.json"
    staged = stage_oci_artifact(
        "oci://docker.io/example/capsules@" + _DIGEST,
        destination,
        run_command=run,
    )
    assert json.loads(destination.read_text(encoding="utf-8")) == _artifact()
    assert staged.resolved_reference.endswith("@" + _DIGEST)
    assert any(call[1] == "pull" for call in calls)
    assert any(call[1] == "cp" for call in calls)
    assert any(call[1:3] == ["rm", "-f"] for call in calls)
    for call in calls:
        if call[1] in {"pull", "create"}:
            assert call[call.index("--platform") + 1] == "linux/arm64"
            assert "docker.io/example/capsules@" + _DIGEST in call


def test_publish_oci_artifact_returns_immutable_reference(tmp_path):
    artifact = tmp_path / "artifact.json"
    artifact.write_text(json.dumps(_artifact()), encoding="utf-8")
    calls = []

    def run(arguments, **_kwargs):
        calls.append(arguments)
        stdout = ""
        if arguments[1:3] == ["image", "inspect"]:
            stdout = json.dumps(["docker.io/example/capsules@" + _DIGEST])
        return SimpleNamespace(returncode=0, stdout=stdout, stderr="")

    resolved = publish_oci_artifact(
        artifact,
        "oci://docker.io/example/capsules:coldsnap-artifact-test",
        run_command=run,
    )
    assert resolved == "oci://docker.io/example/capsules@" + _DIGEST
    assert any(call[1] == "build" for call in calls)
    assert any(call[1] == "push" for call in calls)
    build = next(call for call in calls if call[1] == "build")
    assert build[build.index("--platform") + 1] == "linux/amd64"


@pytest.mark.parametrize(("reference", "digest"), [("example/capsule:latest", "latest"), ("example/capsule:" + _DIGEST, _DIGEST)])
def test_descriptor_rejects_inventory_that_only_looks_digest_pinned(reference, digest):
    artifact = _artifact()
    artifact["capsule"]["images"][0].update(reference=reference, digest=digest)
    with pytest.raises(RuntimeError, match="not digest-pinned"):
        _validate_committed_artifact(artifact)


@pytest.mark.parametrize("arch", ["arm64", "amd64"])
def test_data_only_descriptor_is_extracted_by_digest_with_an_explicit_foreign_platform(tmp_path, monkeypatch, arch):
    monkeypatch.setenv("DOCKER_DEFAULT_PLATFORM", "linux/amd64" if arch == "arm64" else "linux/arm64")
    raw = "docker.io/example/capsules:mutable"
    pinned = raw.rsplit(":", 1)[0] + "@" + _DIGEST
    calls = []
    def run(arguments, **_kwargs):
        calls.append(arguments)
        output = ""
        if arguments[1:3] == ["manifest", "inspect"]:
            assert arguments[-1] == raw
            output = json.dumps({"Descriptor": {"digest": _DIGEST, "platform": {"os": "linux", "architecture": arch}}})
        elif arguments[1] in {"pull", "create"}:
            assert pinned in arguments and raw not in arguments
            assert arguments[arguments.index("--platform") + 1] == "linux/" + arch
        elif arguments[1:3] == ["image", "inspect"]:
            output = json.dumps([pinned])
        elif arguments[1] == "cp":
            from pathlib import Path
            Path(arguments[-1]).write_text(json.dumps(_artifact()))
        return SimpleNamespace(returncode=0, stdout=output, stderr="")
    stage_oci_artifact("oci://" + raw, tmp_path / "artifact.json", run_command=run)
    assert not any(command[1] in {"run", "start"} for command in calls)


def test_descriptor_manifest_list_skips_attestations_and_selects_a_pinned_data_image():
    entries = [
        {"Descriptor": {"digest": "sha256:" + "b" * 64, "platform": {"os": "linux", "architecture": "arm64"}}},
        {"Descriptor": {"digest": "sha256:" + "c" * 64, "platform": {"os": "unknown", "architecture": "unknown"}}},
        {"Descriptor": {"digest": _DIGEST, "platform": {"os": "linux", "architecture": "amd64"}}},
    ]
    image, platform = _descriptor_image(
        "registry.example:5000/example/capsules:tag", docker="docker",
        run_command=lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout=json.dumps(entries)),
    )
    assert image == "registry.example:5000/example/capsules@" + _DIGEST
    assert platform == "linux/amd64"


@pytest.mark.parametrize("manifest", [{}, [], {"Descriptor": {"digest": _DIGEST, "platform": {"os": "unknown"}}}])
def test_descriptor_rejects_a_manifest_without_a_real_supported_image(manifest):
    with pytest.raises(RuntimeError, match="no supported Linux image"):
        _descriptor_image("example/capsules:tag", docker="docker", run_command=lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout=json.dumps(manifest),
        ))


def test_descriptor_cannot_substitute_a_different_requested_single_manifest_digest():
    manifest = {"Descriptor": {"digest": _DIGEST, "platform": {"os": "linux", "architecture": "arm64"}}}
    with pytest.raises(RuntimeError, match="does not match its requested digest"):
        _descriptor_image("example/capsules@sha256:" + "b" * 64, docker="docker", run_command=lambda *_a, **_k: SimpleNamespace(
            returncode=0, stdout=json.dumps(manifest),
        ))


def test_resolved_descriptor_uses_the_requested_repository_not_an_unrelated_alias():
    inventory = ["other.example/capsules@" + _DIGEST, "example/capsules@sha256:" + "b" * 64]
    resolved = _resolved_reference("docker.io/example/capsules:tag", docker="docker", run_command=lambda *_a, **_k: SimpleNamespace(
        returncode=0, stdout=json.dumps(inventory),
    ))
    assert resolved == "oci://example/capsules@sha256:" + "b" * 64


def test_oci_deletion_requires_an_exact_tag_and_normalizes_docker_hub():
    assert _split_tagged_reference("scitrera/capsules:capture") == (
        "docker.io",
        "scitrera/capsules",
        "capture",
    )
    assert _split_tagged_reference("ghcr.io/sparksq/capsules:capture") == (
        "ghcr.io",
        "sparksq/capsules",
        "capture",
    )


def test_docker_hub_tag_deletion_uses_controller_credentials(monkeypatch):
    monkeypatch.setattr(
        "sparkrun.plugins.coldsnap.oci_artifacts._docker_credential",
        lambda _registry: ("owner", "secret"),
    )
    requests: list[Request] = []

    class Response:
        def __init__(self, status, body=b""):
            self.status = status
            self.headers = {}
            self.body = body

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return self.body

    def opener(request, timeout):
        assert timeout == 30
        requests.append(request)
        if request.full_url.endswith("/users/login/"):
            return Response(200, b'{"token":"credential-token"}')
        return Response(204)

    delete_oci_tag("docker.io/scitrera/capsules:capture-one", opener=opener)

    assert [request.method for request in requests] == ["POST", "DELETE"]
    assert requests[-1].full_url.endswith("/repositories/scitrera/capsules/tags/capture-one/")
    assert requests[-1].headers["Authorization"] == "JWT credential-token"
