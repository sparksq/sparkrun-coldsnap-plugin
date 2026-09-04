# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Live host compatibility gate shared by every ColdSnap operation."""

from __future__ import annotations

from dataclasses import dataclass
import logging
import re
from typing import TYPE_CHECKING, Mapping

from sparkrun.core.hardware import HostHardware
from sparkrun.core.hardware_probe import probe_hosts
from sparkrun.core.progress import PROGRESS
from sparkrun.orchestration.primitives import build_ssh_kwargs

if TYPE_CHECKING:
    from sparkrun.api._models import RunPlan
    from sparkrun.core.context import SparkrunContext


SNAPSHOT_DRIVER_N580 = "n580"
SNAPSHOT_DRIVER_N610 = "n610"
SNAPSHOT_DRIVER_MINIMUMS = {
    SNAPSHOT_DRIVER_N580: 580,
    SNAPSHOT_DRIVER_N610: 610,
}
MINIMUM_NVIDIA_DRIVER_MAJOR = min(SNAPSHOT_DRIVER_MINIMUMS.values())
_DRIVER_MAJOR = re.compile(r"^(\d+)(?:\.|$)")
logger = logging.getLogger(__name__)


class ColdSnapCompatibilityError(RuntimeError):
    """Raised before ColdSnap mutates state on an unsupported host."""


@dataclass(frozen=True)
class ColdSnapHardwareReceipt:
    hardware: Mapping[str, HostHardware]
    verified: bool
    snapshot_driver: str


def check_coldsnap_host_compatibility(
    host: str,
    hardware: HostHardware,
    *,
    minimum_driver_major: int = MINIMUM_NVIDIA_DRIVER_MAJOR,
) -> list[str]:
    """Return concrete incompatibilities for one host."""
    nvidia = [accelerator for accelerator in hardware.accelerators if accelerator.vendor.lower() == "nvidia"]
    if not nvidia:
        return ["host %r has no NVIDIA accelerator" % host]
    if not any("cuda" in accelerator.capabilities for accelerator in nvidia):
        return ["host %r has NVIDIA hardware but does not advertise CUDA capability" % host]

    version = hardware.driver_versions.get("nvidia", "").strip()
    if not version:
        return ["host %r did not report an NVIDIA driver version" % host]
    match = _DRIVER_MAJOR.match(version)
    if match is None:
        return ["host %r reported an unparseable NVIDIA driver version %r" % (host, version)]
    if int(match.group(1)) < minimum_driver_major:
        return ["host %r has NVIDIA driver %s; ColdSnap requires major version %d or newer" % (host, version, minimum_driver_major)]
    return []


def select_snapshot_driver(hardware: Mapping[str, HostHardware], hosts) -> str:
    """Select the newest snapshot driver supported by every placed host."""

    host_list = tuple(hosts)
    majors: list[int] = []
    for host in host_list:
        detected = hardware.get(host)
        if detected is None:
            continue
        match = _DRIVER_MAJOR.match(detected.driver_versions.get("nvidia", "").strip())
        if match is not None:
            majors.append(int(match.group(1)))
    if not host_list or len(majors) != len(host_list):
        raise ColdSnapCompatibilityError("ColdSnap could not select a snapshot driver from incomplete host inventory")
    floor = min(majors)
    if floor >= SNAPSHOT_DRIVER_MINIMUMS[SNAPSHOT_DRIVER_N610]:
        return SNAPSHOT_DRIVER_N610
    if floor >= SNAPSHOT_DRIVER_MINIMUMS[SNAPSHOT_DRIVER_N580]:
        return SNAPSHOT_DRIVER_N580
    raise ColdSnapCompatibilityError("ColdSnap requires NVIDIA driver 580 or newer")


def verify_coldsnap_hosts(
    plan: RunPlan,
    sctx: SparkrunContext,
    *,
    dry_run: bool = False,
    snapshot_driver: str | None = None,
) -> ColdSnapHardwareReceipt:
    """Live-probe every placed host and enforce ColdSnap's hardware floor."""
    if dry_run:
        selected = snapshot_driver or SNAPSHOT_DRIVER_N610
        if selected not in SNAPSHOT_DRIVER_MINIMUMS:
            raise ColdSnapCompatibilityError("unknown ColdSnap snapshot driver %r" % selected)
        return ColdSnapHardwareReceipt(hardware={}, verified=False, snapshot_driver=selected)

    ssh_kwargs = build_ssh_kwargs(sctx.config)
    if plan.cluster.user:
        ssh_kwargs = {**ssh_kwargs, "ssh_user": plan.cluster.user}
    hardware = probe_hosts(list(plan.host_list), ssh_kwargs=ssh_kwargs)
    errors: list[str] = []
    for host in plan.host_list:
        detected = hardware.get(host)
        if detected is None:
            errors.append("host %r returned no hardware inventory" % host)
            continue
        minimum = SNAPSHOT_DRIVER_MINIMUMS.get(snapshot_driver, MINIMUM_NVIDIA_DRIVER_MAJOR)
        errors.extend(check_coldsnap_host_compatibility(host, detected, minimum_driver_major=minimum))
    if errors:
        raise ColdSnapCompatibilityError(
            "ColdSnap requires NVIDIA CUDA hardware with NVIDIA driver %d or newer:\n  - %s"
            % (SNAPSHOT_DRIVER_MINIMUMS.get(snapshot_driver, MINIMUM_NVIDIA_DRIVER_MAJOR), "\n  - ".join(errors))
        )
    selected = snapshot_driver or select_snapshot_driver(hardware, plan.host_list)
    logger.log(PROGRESS, "ColdSnap: selected snapshot driver %s", selected)
    return ColdSnapHardwareReceipt(hardware=hardware, verified=True, snapshot_driver=selected)


__all__ = [
    "ColdSnapCompatibilityError",
    "ColdSnapHardwareReceipt",
    "MINIMUM_NVIDIA_DRIVER_MAJOR",
    "SNAPSHOT_DRIVER_MINIMUMS",
    "SNAPSHOT_DRIVER_N580",
    "SNAPSHOT_DRIVER_N610",
    "check_coldsnap_host_compatibility",
    "select_snapshot_driver",
    "verify_coldsnap_hosts",
]
