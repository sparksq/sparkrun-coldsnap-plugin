# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

import json
import shutil
from types import SimpleNamespace
from urllib.request import Request

from sparkrun.core.recipe import Recipe
from sparkrun.plugins.coldsnap import register
from sparkrun.plugins.coldsnap.oci_artifacts import (
    _split_tagged_reference,
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
        if arguments[1:3] == ["image", "inspect"]:
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
