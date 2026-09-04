# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Resolve manager-owned, cluster-local ColdSnap policy."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


_IO_MODES = frozenset({"auto", "buffered", "direct", "mmap", "torch"})


@dataclass(frozen=True)
class ResolvedColdSnapPolicy:
    sparkrun_cache_dir: str
    state_root: str
    recovery_read: str
    sources: dict[str, str]

    def receipt(self) -> dict[str, Any]:
        return {
            "sparkrun_cache_dir": self.sparkrun_cache_dir,
            "state_root": self.state_root,
            "io": {"recovery_read": self.recovery_read},
            "sources": dict(self.sources),
        }


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a mapping")
    return dict(value)


def _settings(config, cluster) -> tuple[dict[str, Any], dict[str, str]]:
    def plugin_settings(owner, name: str) -> dict[str, Any]:
        resolver = getattr(owner, "plugin_settings", None)
        if callable(resolver):
            return _mapping(resolver(name), f"plugins.{name}")
        plugins = getattr(owner, "plugins", {})
        if not isinstance(plugins, dict):
            return {}
        return _mapping(plugins.get(name), f"plugins.{name}")

    merged: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for source, values in (
        ("user-plugin", plugin_settings(config, "coldsnap")),
        ("cluster-plugin", plugin_settings(cluster, "coldsnap")),
    ):
        for key, value in values.items():
            if key not in {"state_root", "io"}:
                # User-level ColdSnap settings also own controller acquisition
                # and artifact retention. Those are consumed by their own
                # resolvers and are intentionally outside site storage policy.
                if source == "user-plugin":
                    continue
                raise ValueError(f"cluster plugins.coldsnap.{key} is unsupported")
            if key == "io":
                current = _mapping(merged.get("io"), "plugins.coldsnap.io")
                for io_key, io_value in _mapping(value, "plugins.coldsnap.io").items():
                    if io_key != "recovery_read":
                        raise ValueError(f"plugins.coldsnap.io.{io_key} is unsupported")
                    current[io_key] = io_value
                    sources["io.recovery_read"] = source
                merged["io"] = current
            else:
                merged[key] = value
                sources[key] = source
    return merged, sources


def _absolute(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if not value or not path.is_absolute() or str(path) != value or "\x00" in value:
        raise ValueError(f"{label} must be an absolute normalized remote path")
    return value


def _probe_remote_sparkrun_cache(cluster, sctx, hosts: list[str]) -> str:
    cached = getattr(cluster, "_coldsnap_remote_sparkrun_cache", "")
    if cached:
        return str(cached)
    from sparkrun.orchestration.primitives import build_ssh_kwargs, run_script_on_host

    ssh_kwargs = build_ssh_kwargs(sctx.config)
    cluster_user = getattr(cluster, "user", None)
    if cluster_user:
        ssh_kwargs = {**ssh_kwargs, "ssh_user": cluster_user}
    program = """python3 - <<'COLDSNAP_PY'
import json, os
from pathlib import Path
base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")).resolve() / "sparkrun"
print("COLDSNAP_CACHE " + json.dumps(str(base)))
COLDSNAP_PY
"""
    roots: dict[str, str] = {}
    for host in hosts:
        result = run_script_on_host(host, program, ssh_kwargs=ssh_kwargs, timeout=30)
        if not result.success:
            raise RuntimeError(
                "resolve remote sparkrun cache on %s: %s" % (host, result.stderr.strip() or result.stdout.strip() or "probe failed")
            )
        line = next((value for value in result.stdout.splitlines() if value.startswith("COLDSNAP_CACHE ")), "")
        if not line:
            raise RuntimeError("remote sparkrun cache probe returned no path on %s" % host)
        roots[host] = _absolute(str(json.loads(line.removeprefix("COLDSNAP_CACHE "))), "remote sparkrun cache")
    unique = set(roots.values())
    if len(unique) != 1:
        raise RuntimeError("ColdSnap requires one common remote state path; resolved %s" % roots)
    resolved = unique.pop()
    cluster._coldsnap_remote_sparkrun_cache = resolved
    return resolved


def resolve_coldsnap_policy(*, cluster, sctx, hosts: list[str], probe_remote: bool = True) -> ResolvedColdSnapPolicy:
    settings, sources = _settings(sctx.config, cluster)
    cluster_cache = getattr(cluster, "sparkrun_cache_dir", None)
    if cluster_cache:
        sparkrun_cache = _absolute(str(cluster_cache), "cluster sparkrun_cache_dir")
        sources["sparkrun_cache_dir"] = "cluster"
    elif probe_remote:
        sparkrun_cache = _probe_remote_sparkrun_cache(cluster, sctx, hosts)
        sources["sparkrun_cache_dir"] = "remote-user-cache"
    else:
        sparkrun_cache = "${XDG_CACHE_HOME:-$HOME/.cache}/sparkrun"
        sources["sparkrun_cache_dir"] = "remote-user-cache"
    configured_state = settings.get("state_root")
    if configured_state:
        state_root = _absolute(str(configured_state), "plugins.coldsnap.state_root")
    elif probe_remote or cluster_cache:
        state_root = str(PurePosixPath(sparkrun_cache) / "coldsnap")
        sources["state_root"] = "sparkrun-cache-derived"
    else:
        state_root = sparkrun_cache + "/coldsnap"
        sources["state_root"] = "sparkrun-cache-derived"
    recovery_read = str(_mapping(settings.get("io"), "plugins.coldsnap.io").get("recovery_read") or "auto")
    if recovery_read not in _IO_MODES:
        raise ValueError("plugins.coldsnap.io.recovery_read is unsupported")
    sources.setdefault("io.recovery_read", "coldsnap-default")
    return ResolvedColdSnapPolicy(sparkrun_cache, state_root, recovery_read, sources)


__all__ = ["ResolvedColdSnapPolicy", "resolve_coldsnap_policy"]
