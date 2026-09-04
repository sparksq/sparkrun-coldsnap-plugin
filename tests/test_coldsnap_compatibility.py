# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from __future__ import annotations

from types import SimpleNamespace

import pytest

from sparkrun.core.hardware import AcceleratorSpec, HostHardware
from sparkrun.plugins.coldsnap.compatibility import (
    ColdSnapCompatibilityError,
    check_coldsnap_host_compatibility,
    select_snapshot_driver,
    verify_coldsnap_hosts,
)


def _hardware(*, vendor="nvidia", capabilities=frozenset({"cuda"}), driver="610.22.03"):
    versions = {"nvidia": driver} if driver is not None else {}
    return HostHardware(
        accelerators=[
            AcceleratorSpec(
                vendor=vendor,
                model="gb10",
                capabilities=capabilities,
            )
        ],
        driver_versions=versions,
    )


def test_host_compatibility_accepts_driver_580_or_newer():
    assert check_coldsnap_host_compatibility("node-a", _hardware(driver="580.0")) == []
    assert check_coldsnap_host_compatibility("node-a", _hardware(driver="590.48.01")) == []
    assert check_coldsnap_host_compatibility("node-a", _hardware(driver="610.0")) == []
    assert check_coldsnap_host_compatibility("node-a", _hardware(driver="611.12.3")) == []


@pytest.mark.parametrize(
    ("hardware", "message"),
    [
        (_hardware(vendor="amd"), "no NVIDIA accelerator"),
        (_hardware(capabilities=frozenset()), "does not advertise CUDA"),
        (_hardware(driver=None), "did not report"),
        (_hardware(driver="unknown"), "unparseable"),
        (_hardware(driver="579.48.01"), "requires major version 580"),
    ],
)
def test_host_compatibility_rejects_unsupported_hosts(hardware, message):
    assert message in check_coldsnap_host_compatibility("node-a", hardware)[0]


def test_verify_live_probes_all_placed_hosts(monkeypatch):
    plan = SimpleNamespace(
        host_list=("node-a", "node-b"),
        cluster=SimpleNamespace(user="drew"),
    )
    sctx = SimpleNamespace(
        config=SimpleNamespace(ssh_user=None, ssh_key=None, ssh_options=None),
    )
    calls = []

    def probe(hosts, *, ssh_kwargs):
        calls.append((hosts, ssh_kwargs))
        return {host: _hardware() for host in hosts}

    monkeypatch.setattr("sparkrun.plugins.coldsnap.compatibility.probe_hosts", probe)
    receipt = verify_coldsnap_hosts(plan, sctx)

    assert receipt.verified is True
    assert receipt.snapshot_driver == "n610"
    assert calls == [(["node-a", "node-b"], {"ssh_user": "drew", "ssh_key": None, "ssh_options": None})]


def test_verify_aggregates_live_probe_failures(monkeypatch):
    plan = SimpleNamespace(
        host_list=("old", "missing"),
        cluster=SimpleNamespace(user=None),
    )
    sctx = SimpleNamespace(
        config=SimpleNamespace(ssh_user=None, ssh_key=None, ssh_options=None),
    )
    monkeypatch.setattr(
        "sparkrun.plugins.coldsnap.compatibility.probe_hosts",
        lambda *_args, **_kwargs: {"old": _hardware(driver="579.1")},
    )

    with pytest.raises(ColdSnapCompatibilityError, match="old.*579.1") as error:
        verify_coldsnap_hosts(plan, sctx)
    assert "missing" in str(error.value)


def test_verify_dry_run_is_non_invasive(monkeypatch):
    monkeypatch.setattr(
        "sparkrun.plugins.coldsnap.compatibility.probe_hosts",
        lambda *_args, **_kwargs: pytest.fail("dry run probed hosts"),
    )
    receipt = verify_coldsnap_hosts(SimpleNamespace(), SimpleNamespace(), dry_run=True)
    assert receipt.verified is False
    assert receipt.hardware == {}
    assert receipt.snapshot_driver == "n610"


@pytest.mark.parametrize(
    ("drivers", "selected"),
    [
        (("610.1", "610.2"), "n610"),
        (("580.1", "580.2"), "n580"),
        (("610.1", "580.2"), "n580"),
    ],
)
def test_verify_selects_newest_driver_supported_by_every_host(monkeypatch, drivers, selected):
    plan = SimpleNamespace(host_list=("node-a", "node-b"), cluster=SimpleNamespace(user=None))
    sctx = SimpleNamespace(config=SimpleNamespace(ssh_user=None, ssh_key=None, ssh_options=None))
    monkeypatch.setattr(
        "sparkrun.plugins.coldsnap.compatibility.probe_hosts",
        lambda *_args, **_kwargs: {
            "node-a": _hardware(driver=drivers[0]),
            "node-b": _hardware(driver=drivers[1]),
        },
    )
    assert verify_coldsnap_hosts(plan, sctx).snapshot_driver == selected


def test_select_snapshot_driver_accepts_one_shot_host_iterables():
    hardware = {
        "node-a": _hardware(driver="610.1"),
        "node-b": _hardware(driver="610.2"),
    }

    assert select_snapshot_driver(hardware, iter(("node-a", "node-b"))) == "n610"


def test_select_snapshot_driver_rejects_an_empty_placement():
    with pytest.raises(ColdSnapCompatibilityError, match="incomplete host inventory"):
        select_snapshot_driver({}, ())
