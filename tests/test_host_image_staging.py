# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Exercise the real host preparation/staging contract across supported versions."""

from copy import copy, deepcopy
from types import SimpleNamespace

import pytest

from sparkrun.core.image_preparation import ImagePreparationError
from sparkrun.plugins.coldsnap.service import prepare_capture_images
from test_coldsnap_plugin import _setup


@pytest.mark.parametrize("scoped_config", [False, True])
def test_capture_stages_builder_output_with_operation_local_transport_and_draft(monkeypatch, scoped_config):
    recipe, options, plan, sctx = _setup()
    recipe.builder = "coldsnap"
    plan.cluster.user = "target-user"
    sctx.config.ssh_user = "configured-user"
    original_distribution = deepcopy(recipe.distribution_config)
    original_image = recipe.container
    observed = {}
    identities = {"h1": "sha256:" + "b" * 64, "h2": "sha256:" + "c" * 64}
    if scoped_config:

        def for_cluster(cluster):
            assert cluster is plan.cluster
            config = copy(sctx.config)
            config.ssh_user = cluster.user
            observed["scoped"] = config
            return config

        sctx.config.for_cluster = for_cluster

    class Builder:
        def prepare(self, image, actual_recipe, hosts, **kwargs):
            assert image == original_image and actual_recipe is not recipe
            assert kwargs["builder_context"] == {"snapshot_driver": "n580", "engine": "vllm"}
            assert kwargs["ssh_kwargs"]["ssh_user"] == "target-user"
            observed["config"] = kwargs["config"]
            return "local/coldsnap:built"

    def runtime_prepare(actual_recipe, hosts, **kwargs):
        assert actual_recipe is not recipe and kwargs["config"] is observed["config"]
        actual_recipe.distribution_config.add_model("org/draft", revision="d" * 40)

    def resolve_transfer(mode, hosts, **kwargs):
        assert kwargs["ssh_kwargs"]["ssh_user"] == "target-user"
        return SimpleNamespace(mode="delegated")

    def distribute(actual_recipe, image, hosts, cache, config, dry_run, **kwargs):
        assert image == "local/coldsnap:built" and hosts == ["h1", "h2"]
        assert config is observed["config"] and config.ssh_user == "target-user"
        assert not kwargs["skip_model"]
        distribution = deepcopy(actual_recipe.distribution_config)
        if kwargs.get("container_distribution") is not None:
            distribution.containers = deepcopy(kwargs["container_distribution"])
        distribution.resolve(actual_recipe, resolved_container=image)
        assert {entry.name for entry in distribution.containers.entries} == {"local/coldsnap:built"}
        assert any(entry.name == "org/draft" and entry.revision == "d" * 40 for entry in actual_recipe.distribution_config.models.entries)
        callback = kwargs.get("after_container_sync")
        if callback:
            callback()
        return "comm-env", {}, {}, {}

    def inspect(host, command, **kwargs):
        assert "local/coldsnap:built" in command and kwargs["ssh_kwargs"]["ssh_user"] == "target-user"
        return SimpleNamespace(success=True, returncode=0, stdout=identities[host], stderr="")

    monkeypatch.setattr("sparkrun.core.bootstrap.get_builder", lambda *a: Builder())
    monkeypatch.setattr(plan.runtime, "prepare", runtime_prepare)
    monkeypatch.setattr("sparkrun.orchestration.distribution.resolve_auto_transfer_mode", resolve_transfer)
    monkeypatch.setattr("sparkrun.orchestration.distribution.distribute_from_config", distribute)
    monkeypatch.setattr("sparkrun.orchestration.primitives.run_command_on_host", inspect)
    result = prepare_capture_images(options, plan=plan, sctx=sctx, snapshot_driver="n580")
    assert result.content_images_by_node == tuple(identities.values()) and result.comm_env == "comm-env"
    assert sctx.config.ssh_user == "configured-user"
    assert recipe.distribution_config == original_distribution and recipe.container == original_image
    if scoped_config:
        assert observed["config"] is observed["scoped"]


def test_capture_fails_if_one_host_cannot_resolve_its_prepared_image(monkeypatch):
    _, options, plan, sctx = _setup()
    monkeypatch.setattr("sparkrun.orchestration.distribution.resolve_auto_transfer_mode", lambda *a, **k: SimpleNamespace(mode="local"))

    def distribute(*args, **kwargs):
        if kwargs.get("after_container_sync"):
            kwargs["after_container_sync"]()
        return None, {}, {}, {}

    monkeypatch.setattr("sparkrun.orchestration.distribution.distribute_from_config", distribute)
    monkeypatch.setattr(
        "sparkrun.orchestration.primitives.run_command_on_host",
        lambda host, *a, **k: SimpleNamespace(
            success=host == "h1",
            returncode=0 if host == "h1" else 1,
            stdout="sha256:" + "b" * 64 if host == "h1" else "",
            stderr="missing image",
        ),
    )
    with pytest.raises(ImagePreparationError, match="h2"):
        prepare_capture_images(options, plan=plan, sctx=sctx, snapshot_driver="n580")


def test_capture_uses_host_same_id_replacement_when_supported(monkeypatch):
    from inspect import signature
    from unittest.mock import create_autospec
    import sparkrun.api._run as host_run
    from sparkrun.plugins.coldsnap.service import replace_capture_workload

    _, _, plan, sctx = _setup()
    supports_current = "include_current" in signature(host_run._evict_superseded_deployments).parameters
    evict = create_autospec(host_run._evict_superseded_deployments, return_value=([plan.cluster_id], None))
    monkeypatch.setattr(host_run, "_evict_superseded_deployments", evict)
    assert replace_capture_workload(plan=plan, sctx=sctx) == (plan.cluster_id,)
    kwargs = evict.call_args.kwargs
    assert kwargs["strict"] and kwargs["intent_id"] == plan.intent_id
    assert kwargs["target_hosts"] == list(plan.host_list)
    assert kwargs.get("include_current", False) is supports_current
