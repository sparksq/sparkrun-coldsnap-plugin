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


def _status(hosts, *, errors=None):
    entries = [
        SimpleNamespace(host=host, workloads=[SimpleNamespace(cluster_id=cid, intent_id=intent) for cid, intent in workloads])
        for host, workloads in hosts.items()
    ]
    return SimpleNamespace(hosts=entries, observation_errors=errors or {})


def test_capture_replaces_overlapping_deployments_of_its_intent_only(monkeypatch):
    from dataclasses import replace

    import sparkrun.api as api
    from sparkrun.plugins.coldsnap.service import replace_capture_workload

    _, _, plan, sctx = _setup()
    plan = replace(plan, candidate_hosts=("h1", "h2", "h3"), host_list=("h1", "h2"))
    previous = "sparkrun_%s_%s" % (plan.intent_id, "e" * 12)
    elsewhere = "sparkrun_%s_%s" % (plan.intent_id, "f" * 12)
    foreign = "sparkrun_%s_%s" % ("9" * 16, "e" * 12)
    status = _status(
        {
            "h1": [(plan.workload_id, plan.intent_id), (foreign, "9" * 16)],
            "h2": [(previous, None), (plan.workload_id, plan.intent_id)],
            "h3": [(previous, None), (elsewhere, plan.intent_id)],
        }
    )
    stops = []
    monkeypatch.setattr(api, "status", lambda hosts, **kwargs: status if hosts == ["h1", "h2", "h3"] else pytest.fail(hosts))

    def stop(**kwargs):
        stops.append(kwargs)
        return SimpleNamespace(hosts_failed=())

    monkeypatch.setattr(api, "stop", stop)
    # The plan's own id is replaced too: capture never runs the runtime's
    # same-ID cleanup. A disjoint deployment and a foreign intent survive.
    assert replace_capture_workload(plan=plan, sctx=sctx) == (plan.workload_id, previous)
    assert [(call["workload_id"], call["hosts"]) for call in stops] == [
        (plan.workload_id, ["h1", "h2"]),
        (previous, ["h2", "h3"]),
    ]
    assert all(call["discovered"] is status and call["cluster"] is plan.cluster for call in stops)


@pytest.mark.parametrize("failure", ["query", "observation", "teardown"])
def test_capture_replacement_is_strict(monkeypatch, failure):
    import sparkrun.api as api
    from sparkrun.plugins.coldsnap.service import replace_capture_workload

    _, _, plan, sctx = _setup()
    status = _status({"h1": [(plan.workload_id, plan.intent_id)], "h2": []}, errors={"h2": "down"} if failure == "observation" else None)

    def query(*_args, **_kwargs):
        if failure == "query":
            raise OSError("ssh refused")
        return status

    monkeypatch.setattr(api, "status", query)
    monkeypatch.setattr(api, "stop", lambda **_kwargs: SimpleNamespace(hosts_failed=("h1",)))
    with pytest.raises(RuntimeError, match="could not query|not confirmed on h1"):
        replace_capture_workload(plan=plan, sctx=sctx)


def test_lifecycle_resolves_exactly_one_observed_workload(monkeypatch):
    import sparkrun.api as api
    from sparkrun.plugins.coldsnap.service import resolve_running_workload

    _, _, plan, sctx = _setup()
    other = "sparkrun_%s_%s" % (plan.intent_id, "e" * 12)
    snapshots = {
        "one": _status({"h1": [(plan.workload_id, plan.intent_id)], "h2": [(plan.workload_id, None)]}),
        "none": _status({"h1": [], "h2": []}),
        "missing": _status({"h1": [(plan.workload_id, plan.intent_id)]}),
        "ambiguous": _status({"h1": [(plan.workload_id, plan.intent_id)], "h2": [(other, plan.intent_id)]}),
    }
    for name, snapshot in snapshots.items():
        monkeypatch.setattr(api, "status", lambda *_args, snapshot=snapshot, **_kwargs: snapshot)
        if name == "one":
            assert resolve_running_workload(plan.intent_id, ["h1", "h2"], cluster=plan.cluster, sctx=sctx) == plan.workload_id
            continue
        expected = {"none": api.JobNotFound, "missing": api.SparkrunError, "ambiguous": api.AmbiguousWorkload}[name]
        with pytest.raises(expected) as raised:
            resolve_running_workload(plan.intent_id, ["h1", "h2"], cluster=plan.cluster, sctx=sctx)
        if name == "ambiguous":
            assert raised.value.workload_ids == tuple(sorted((plan.workload_id, other)))
