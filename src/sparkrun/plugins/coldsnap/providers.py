# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Prepare optional worker-owned model payloads before timed activation."""

from __future__ import annotations

import concurrent.futures
import hashlib
import json
import logging
import os
import re
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sparkrun.core.progress import PROGRESS
from sparkrun.plugins.coldsnap.policy import resolve_coldsnap_policy


@dataclass(frozen=True)
class StageOutcome:
    request: dict[str, Any]
    selected_mode: str
    failures: tuple[str, ...] = ()


WorkerDownloader = Callable[..., dict[str, Any]]
LocalPackResolver = Callable[..., dict[str, Any]]
PayloadVerifierResolver = str | Path | Callable[[], str | Path]
logger = logging.getLogger(__name__)
_SAFE_CAPTURE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,255}")
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}")
_DRIVER_DEFAULT_WEIGHT_MODES = {"n580": "auto", "n610": "auto"}


def _payload_validation_script(
    values: str,
    *,
    path_program: str,
    verifier: str,
    missing_is_miss: bool = False,
) -> str:
    """Discover a provider path and invoke the release-matched Go verifier."""

    missing = (
        'print("COLDSNAP_MISS " + json.dumps({"reason": "model payload is unavailable"}, sort_keys=True)); raise SystemExit(0)'
        if missing_is_miss
        else 'raise SystemExit("model payload is unavailable")'
    )
    failed = 'print("COLDSNAP_MISS " + json.dumps({"reason": detail}, sort_keys=True)); raise SystemExit(0)' if missing_is_miss else "pass"
    return """python3 - <<'COLDSNAP_PY'
import json
import os
import stat
import subprocess
from pathlib import Path

config = json.loads(%s)
config["payload_verifier"] = %s
%s
try:
    value = path.stat(follow_symlinks=False)
except OSError:
    %s
if not stat.S_ISREG(value.st_mode):
    raise SystemExit("model payload is not a regular file")
record_path = path.with_name(path.name + ".coldsnap-validation.json")
command = [
    config["payload_verifier"], "payload-verify",
    "--path", str(path),
    "--record", str(record_path),
    "--expected-sha256", config["expected_sha256"],
    "--expected-bytes", str(config["expected_bytes"]),
    "--worker", config["worker"],
]
completed = subprocess.run(command, capture_output=True, text=True, check=False)
if completed.returncode != 0:
    detail = (completed.stderr or completed.stdout or "payload verification failed").strip()
    %s
    raise SystemExit(detail)
try:
    admitted = json.loads(completed.stdout)
except (TypeError, ValueError) as error:
    raise SystemExit("Go payload verifier returned invalid JSON") from error
print("COLDSNAP_PACK " + json.dumps(admitted, sort_keys=True))
COLDSNAP_PY
""" % (json.dumps(values), json.dumps(verifier), path_program, missing, failed)


def _is_content_addressed_model_payload(value: dict[str, Any]) -> bool:
    digest = value.get("sha256")
    path = value.get("path")
    size = value.get("bytes")
    return (
        value.get("role") == "model-weight-payload"
        and isinstance(digest, str)
        and _SHA256.fullmatch(digest) is not None
        and path == "model-payloads/sha256/%s.pack" % digest.removeprefix("sha256:")
        and isinstance(size, int)
        and not isinstance(size, bool)
        and size > 0
    )


def resolve_request_weight_mode(request: dict[str, Any]) -> str:
    """Resolve an omitted mode only for sparkrun's pre-ColdSnap asset staging."""

    weights = request.get("policy", {}).get("weights", {})
    declared = weights.get("mode")
    if declared in {"auto", "native", "recovery", "cache-only-auto"}:
        return str(declared)
    if declared is not None:
        raise ValueError("ColdSnap request weight mode is invalid")
    driver = str(request.get("snapshot_driver", {}).get("id") or "")
    engine = str(request.get("launch", {}).get("engine") or "")
    if driver == "n580" and engine == "vllm":
        # Portable n580 vLLM recovery currently outperforms native hydration.
        # Recipes may still request auto or native explicitly.
        return "recovery"
    try:
        return _DRIVER_DEFAULT_WEIGHT_MODES[driver]
    except KeyError as error:
        raise ValueError("ColdSnap request snapshot driver has no weight-mode default") from error


def _stage_payload_verifier(
    local_path: str | Path,
    *,
    request: dict[str, Any],
    plan,
    state_root: str,
    ssh_kwargs: dict[str, Any],
    ownership_root: str = "",
) -> str:
    """Stage one content-addressed adapter binary on every payload host."""

    from sparkrun.transports import open_cluster_host_session

    source = Path(local_path).expanduser().resolve()
    if not source.is_file() or not os.access(source, os.X_OK):
        raise RuntimeError("ColdSnap payload verifier is not an executable file: %s" % source)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    manifest_path = source.with_name("manifest.json")
    identity = None
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text())
        if manifest.get("sha256", {}).get(source.name) != digest:
            raise RuntimeError("ColdSnap payload verifier does not match its release manifest")
        identity = {"version": manifest["version"], "commit": manifest["commit"]}
    root = str(Path(state_root) / "tools" / "payload-verifier" / digest)
    target = str(Path(root) / "coldsnap-payload-verifier")
    hosts = sorted({str(unit["host"]) for unit in request["launch"]["units"]})
    logger.log(PROGRESS, "ColdSnap: staging Go payload verifier on %d host(s)", len(hosts))
    session = open_cluster_host_session(plan.cluster, ssh_kwargs=ssh_kwargs)

    def verify_identity(host: str) -> None:
        if identity is None:
            return  # Explicit development binaries have no release manifest.
        result = session.execute(host, [target, "version", "--json"], timeout=30)
        if result.returncode != 0:
            raise RuntimeError("ColdSnap payload verifier cannot execute on %s: %s" % (
                host, (result.stderr or result.stdout).decode(errors="replace")[-1000:],
            ))
        if json.loads(result.stdout) != identity:
            raise RuntimeError("ColdSnap payload verifier release identity mismatch on %s" % host)

    def stage(host: str) -> None:
        created = session.execute(host, ["install", "-d", "-m", "0700", root], timeout=60)
        if created.returncode != 0:
            # Docker may have created a missing cache bind as root during an
            # older launch. Use sparkrun's scoped, non-interactive ownership
            # repair and retry once before reporting the actual write failure.
            from sparkrun.orchestration.sudo import ensure_remote_dir_ownership

            logger.info("ColdSnap verifier cache is not writable on %s; attempting ownership repair", host)
            ensure_remote_dir_ownership(
                ownership_root or state_root,
                [host],
                resource_label="ColdSnap cache",
                session=session,
                **ssh_kwargs,
            )
            created = session.execute(host, ["install", "-d", "-m", "0700", root], timeout=60)
        if created.returncode != 0:
            detail = (created.stderr or created.stdout or b"create verifier cache failed").decode(errors="replace")
            lines = [line for line in detail.splitlines() if line.strip()]
            identity_mismatch = [line for line in lines if line.startswith("identity_sign:") and "contents do not match public" in line]
            command_failure = "\n".join(line for line in lines if line not in identity_mismatch) or "create verifier cache failed"
            cluster_name = str(getattr(plan.cluster, "name", "") or "<cluster>")
            repair_path = ownership_root or state_root
            message = (
                "ColdSnap cache %s is not writable on %s after automatic repair. "
                "Run `sparkrun setup fix-permissions --cluster %s --cache-dir %s` once. Remote error: %s"
                % (repair_path, host, cluster_name, repair_path, command_failure[-700:])
            )
            if identity_mismatch:
                message += (
                    " Separately, SSH reports that the local id_ed25519.pub does not match its private key; "
                    "that warning did not cause this remote permission failure."
                )
            raise RuntimeError(message)
        observed = session.execute(host, ["sha256sum", target], timeout=60)
        if observed.returncode == 0 and observed.stdout.decode(errors="replace").split(maxsplit=1)[0:1] == [digest]:
            verify_identity(host)
            return
        temporary = target + ".tmp.%d" % os.getpid()
        session.upload(host, [str(source)], temporary)
        protected = session.execute(host, ["chmod", "0555", temporary], timeout=60)
        if protected.returncode != 0:
            raise RuntimeError((protected.stderr or protected.stdout or b"protect verifier failed").decode(errors="replace")[-1000:])
        verified = session.execute(host, ["sha256sum", temporary], timeout=60)
        fields = verified.stdout.decode(errors="replace").split(maxsplit=1) if verified.returncode == 0 else []
        if not fields or fields[0] != digest:
            raise RuntimeError("staged ColdSnap payload verifier failed digest verification on %s" % host)
        published = session.execute(host, ["mv", "-f", temporary, target], timeout=60)
        if published.returncode != 0:
            raise RuntimeError((published.stderr or published.stdout or b"publish verifier failed").decode(errors="replace")[-1000:])
        verify_identity(host)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(hosts), 20)) as executor:
            futures = {executor.submit(stage, host): host for host in hosts}
            for future in concurrent.futures.as_completed(futures):
                host = futures[future]
                try:
                    future.result()
                except Exception as error:
                    raise RuntimeError("stage ColdSnap payload verifier on %s: %s" % (host, error)) from error
    finally:
        session.close()
    return target


def stage_native_packs(
    request: dict[str, Any],
    *,
    plan,
    sctx,
    downloader: WorkerDownloader | None = None,
    local_resolver: LocalPackResolver | None = None,
    node_cache_resolver: LocalPackResolver | None = None,
    payload_verifier: PayloadVerifierResolver = "",
) -> StageOutcome:
    """Stage shared worker model payloads for native replay.

    Download and first-use SHA-256 verification occur before ColdSnap is
    invoked. Versioned validation evidence makes unchanged later staging
    metadata-only; stale evidence revalidates the existing complete object.
    """

    prepared = deepcopy(request)
    weights = prepared["policy"]["weights"]
    mode = resolve_request_weight_mode(prepared)
    if prepared.get("operation") != "restore":
        return StageOutcome(prepared, mode)
    if mode == "recovery":
        # An omitted n580 vLLM mode is a sparkrun policy decision.  Make it
        # explicit before handing the request to ColdSnap so the coordinator
        # cannot independently resolve the omission back to auto.
        weights["mode"] = "recovery"
        return StageOutcome(prepared, mode)
    if _artifact_native_capability(prepared) is False:
        return _fallback_or_raise(
            prepared,
            mode,
            ["artifact has no driver-qualified native replay provider"],
        )
    native = weights["native"]
    workers = prepared["launch"]["execution"]["workers"]
    units = {unit["id"]: unit for unit in prepared["launch"]["units"]}
    worker_count = len(workers)
    ssh_kwargs = _ssh_kwargs(plan, sctx)
    expected_packs = _artifact_model_payloads(prepared)
    site_policy = resolve_coldsnap_policy(
        cluster=plan.cluster,
        sctx=sctx,
        hosts=list(plan.host_list),
        probe_remote=node_cache_resolver is None or local_resolver is None,
    )
    state_root = site_policy.state_root
    ownership_root = site_policy.sparkrun_cache_dir if site_policy.sources.get("state_root") == "sparkrun-cache-derived" else state_root
    remote_verifier = ""

    def ensure_remote_verifier() -> str:
        nonlocal remote_verifier
        if remote_verifier:
            return remote_verifier
        local_verifier = payload_verifier() if callable(payload_verifier) else payload_verifier
        if not local_verifier:
            raise RuntimeError("ColdSnap native payload verification requires an engine adapter")
        remote_verifier = _stage_payload_verifier(
            local_verifier,
            request=prepared,
            plan=plan,
            state_root=state_root,
            ssh_kwargs=ssh_kwargs,
            ownership_root=ownership_root,
        )
        return remote_verifier

    def unavailable_verifier(**_kwargs) -> dict[str, Any]:
        raise RuntimeError("ColdSnap native payload verification requires an engine adapter")

    cache_failures: list[str] = []
    worker_ids = {str(worker["id"]) for worker in workers}
    if set(expected_packs) != worker_ids:
        expected_packs = {}
    if expected_packs:
        resolver = node_cache_resolver or (_resolve_node_cached_pack if payload_verifier else unavailable_verifier)
        verifier = ensure_remote_verifier() if node_cache_resolver is None and payload_verifier else ""
        logger.log(PROGRESS, "ColdSnap: checking node-local native cache for %d worker(s)", worker_count)
        staged: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(worker_count, 20)) as executor:
            futures = {
                executor.submit(
                    resolver,
                    worker=str(worker["id"]),
                    unit=str(worker["unit"]),
                    host=str(units[str(worker["unit"])]["host"]),
                    capture_id="",
                    snapshot_driver=str(prepared.get("snapshot_driver", {}).get("id") or ""),
                    state_root=state_root,
                    expected=expected_packs[str(worker["id"])],
                    ssh_kwargs=ssh_kwargs,
                    **({"verifier": verifier} if node_cache_resolver is None else {}),
                ): str(worker["id"])
                for worker in workers
            }
            for future in concurrent.futures.as_completed(futures):
                worker_id = futures[future]
                try:
                    record = future.result()
                    if record.get("worker") != worker_id:
                        raise ValueError("node-local result worker differs from request")
                    _validate_expected_pack(record, expected_packs[worker_id])
                    staged.append(_prepared_payload(record))
                except Exception as error:
                    cache_failures.append("worker %s local cache: %s" % (worker_id, error))
        if not cache_failures and len(staged) == worker_count:
            native["staged"] = sorted(staged, key=lambda record: str(record["worker"]))
            logger.log(PROGRESS, "ColdSnap: using verified node-local native model payloads")
            return StageOutcome(prepared, "native")
    local_inventory = _capture_local_inventory(prepared)
    expected_packs = local_inventory[3] if local_inventory is not None else expected_packs
    local_failures: list[str] = []
    if local_inventory is not None:
        capture_id, snapshot_driver, captured_hosts, expected_packs = local_inventory
        resolver = local_resolver or (_resolve_capture_local_pack if payload_verifier else unavailable_verifier)
        verifier = ensure_remote_verifier() if local_resolver is None and payload_verifier else ""
        logger.log(PROGRESS, "ColdSnap: verifying capture-local model payloads for %d worker(s)", worker_count)
        staged: list[dict[str, Any]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(worker_count, 20)) as executor:
            futures = {}
            for worker in workers:
                worker_id = str(worker["id"])
                unit_id = str(worker["unit"])
                host = str(units[unit_id]["host"])
                if captured_hosts[worker_id] != host:
                    local_failures.append(
                        "worker %s: capture-local payload is on %s, current placement is %s" % (worker_id, captured_hosts[worker_id], host)
                    )
                    continue
                futures[
                    executor.submit(
                        resolver,
                        worker=worker_id,
                        unit=unit_id,
                        host=host,
                        capture_id=capture_id,
                        snapshot_driver=snapshot_driver,
                        state_root=state_root,
                        expected=expected_packs[worker_id],
                        ssh_kwargs=ssh_kwargs,
                        **({"verifier": verifier} if local_resolver is None else {}),
                    )
                ] = worker_id
            for future in concurrent.futures.as_completed(futures):
                worker_id = futures[future]
                try:
                    record = future.result()
                    if record.get("worker") != worker_id:
                        raise ValueError("local result worker differs from request")
                    _validate_expected_pack(record, expected_packs[worker_id])
                    staged.append(_prepared_payload(record))
                except Exception as error:
                    local_failures.append("worker %s: %s" % (worker_id, error))
        if not local_failures and len(staged) == worker_count:
            native["staged"] = sorted(staged, key=lambda record: str(record["worker"]))
            logger.log(PROGRESS, "ColdSnap: using verified capture-local model payloads")
            return StageOutcome(prepared, "native")

    _fill_native_inventory_from_artifact(prepared, native)
    repository = native.get("repository") or ""
    revision = native.get("revision") or ""
    filenames = {str(worker): name for worker, name in (native.get("files_by_worker") or {}).items()}
    provider_failures: list[str] = []
    if not repository or not revision:
        provider_failures.append("model payload repository and pinned revision are not configured")
    missing = sorted(worker_ids - set(filenames))
    if missing:
        provider_failures.append("model payload provider has no object for worker(s) %s" % ", ".join(missing))
    extra = sorted(set(filenames) - worker_ids)
    if extra:
        provider_failures.append("model payload provider contains unknown worker(s) %s" % ", ".join(extra))
    if provider_failures:
        return _fallback_or_raise(prepared, mode, cache_failures + local_failures + provider_failures)

    if downloader is None:
        downloader = _download_worker_pack if payload_verifier else unavailable_verifier
        verifier = ensure_remote_verifier() if payload_verifier else ""
    else:
        verifier = ""
    logger.log(PROGRESS, "ColdSnap: staging model payloads from %s@%s", repository, revision)
    cache_dir = str(plan.cluster.cache_dir or getattr(sctx.config, "hf_cache_dir", "~/.cache/huggingface"))
    staged: list[dict[str, Any]] = []
    provider_failures = []
    offline = mode == "cache-only-auto"
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(worker_count, 20)) as executor:
        futures = {
            executor.submit(
                downloader,
                worker=str(worker["id"]),
                host=units[str(worker["unit"])]["host"],
                repository=repository,
                revision=revision,
                filename=filenames[str(worker["id"])],
                expected=expected_packs.get(str(worker["id"])),
                cache_dir=cache_dir,
                offline=offline,
                ssh_kwargs=ssh_kwargs,
                **({"verifier": verifier} if downloader is _download_worker_pack else {}),
            ): str(worker["id"])
            for worker in workers
        }
        for future in concurrent.futures.as_completed(futures):
            worker_id = futures[future]
            try:
                record = future.result()
                if record.get("worker") != worker_id:
                    raise ValueError("download result worker differs from request")
                staged.append(_prepared_payload(record))
            except Exception as error:
                provider_failures.append("worker %s: %s" % (worker_id, error))
    if provider_failures:
        return _fallback_or_raise(prepared, mode, cache_failures + local_failures + provider_failures)
    native["staged"] = sorted(staged, key=lambda record: str(record["worker"]))
    return StageOutcome(prepared, "native")


def native_pack_status(
    request: dict[str, Any],
    *,
    plan,
    sctx,
    probe_remote: bool = True,
) -> tuple[dict[str, Any], ...]:
    """Return non-mutating materialization state for every execution worker."""

    expected = _artifact_model_payloads(request)
    workers = request.get("launch", {}).get("execution", {}).get("workers", [])
    units = {str(unit["id"]): unit for unit in request.get("launch", {}).get("units", []) if isinstance(unit, dict) and unit.get("id")}
    worker_ids = {str(worker.get("id") or "") for worker in workers if isinstance(worker, dict)}
    if not worker_ids or set(expected) != worker_ids:
        raise ValueError("committed artifact has no complete worker model-payload inventory")
    policy = resolve_coldsnap_policy(
        cluster=plan.cluster,
        sctx=sctx,
        hosts=list(plan.host_list),
        probe_remote=probe_remote,
    )
    if not probe_remote:
        return tuple(
            {
                "worker": worker_id,
                "host": str(units[str(worker["unit"])]["host"]),
                "state": "uninspected",
                "state_root": policy.state_root,
                "path": str(expected[worker_id]["path"]),
                "bytes": int(expected[worker_id]["bytes"]),
                "sha256": str(expected[worker_id]["sha256"]),
            }
            for worker in workers
            for worker_id in (str(worker["id"]),)
        )

    ssh_kwargs = _ssh_kwargs(plan, sctx)
    results: list[dict[str, Any]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(workers), 20)) as executor:
        futures = {}
        for worker in workers:
            worker_id = str(worker["id"])
            unit = units.get(str(worker["unit"]))
            if unit is None:
                raise ValueError("execution worker references an unknown launch unit")
            host = str(unit["host"])
            futures[
                executor.submit(
                    _read_native_pack_status,
                    worker=worker_id,
                    host=host,
                    state_root=policy.state_root,
                    expected=expected[worker_id],
                    ssh_kwargs=ssh_kwargs,
                )
            ] = (worker_id, host)
        for future in concurrent.futures.as_completed(futures):
            worker_id, host = futures[future]
            try:
                record = future.result()
            except Exception as error:
                record = {
                    "worker": worker_id,
                    "host": host,
                    "state": "failed",
                    "reason": "status_probe_failed",
                    "error": str(error),
                }
            results.append(record)
    return tuple(sorted(results, key=lambda value: str(value["worker"])))


def _read_native_pack_status(
    *,
    worker: str,
    host: str,
    state_root: str,
    expected: dict[str, Any],
    ssh_kwargs: dict[str, Any],
) -> dict[str, Any]:
    from sparkrun.orchestration.primitives import run_script_on_host

    values = json.dumps(
        {
            "worker": worker,
            "host": host,
            "state_root": state_root,
            "path": expected["path"],
            "expected_bytes": int(expected["bytes"]),
            "expected_sha256": str(expected["sha256"]),
        },
        sort_keys=True,
    )
    script = r"""python3 - <<'COLDSNAP_PY'
import json
import stat
from pathlib import Path

config = json.loads(%s)
state_root = Path(config["state_root"]).resolve()
payload_root = (state_root / "model-payloads").resolve()
path = (state_root / config["path"]).resolve()
try:
    path.relative_to(payload_root)
except ValueError:
    raise SystemExit("model payload path escapes the cache root")
status_path = payload_root / ".status" / (config["worker"] + ".json")
record_path = path.with_name(path.name + ".coldsnap-validation.json")

result = {
    "worker": config["worker"],
    "host": config["host"],
    "state_root": str(state_root),
    "path": str(path),
    "bytes": config["expected_bytes"],
    "sha256": config["expected_sha256"],
}
status_value = None
try:
    candidate = json.loads(status_path.read_text(encoding="utf-8"))
    if isinstance(candidate, dict):
        status_value = candidate
except (OSError, ValueError):
    pass

try:
    value = path.stat(follow_symlinks=False)
except OSError:
    value = None
if value is not None:
    if not stat.S_ISREG(value.st_mode):
        result.update(state="corrupt", reason="payload_not_regular")
    elif value.st_size != config["expected_bytes"]:
        result.update(state="corrupt", reason="payload_size_mismatch", observed_bytes=value.st_size)
    else:
        identity = {"device": value.st_dev, "inode": value.st_ino, "size": value.st_size, "mtime_ns": value.st_mtime_ns}
        record = None
        try:
            record_stat = record_path.stat(follow_symlinks=False)
            if stat.S_ISREG(record_stat.st_mode) and not record_stat.st_mode & 0o077:
                record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        if (
            isinstance(record, dict)
            and record.get("format") == 1
            and record.get("kind") == "coldsnap-payload-validation"
            and record.get("provider") == "sha256-cache-v1"
            and record.get("blob") == path.name
            and record.get("expected") == {"bytes": config["expected_bytes"], "sha256": config["expected_sha256"]}
            and record.get("content_identity") == identity
        ):
            result.update(
                state="ready",
                reason="cached_validation",
                validation={"provider": "sha256-cache-v1", "content_evidence": "cached-full-sha256", **identity},
            )
        else:
            result.update(state="revalidation-needed", reason="validation_missing_or_stale")
elif status_value is not None:
    state = str(status_value.get("state") or "")
    same_object = (
        status_value.get("path") == str(path)
        and status_value.get("bytes") == config["expected_bytes"]
        and status_value.get("sha256") == config["expected_sha256"]
    )
    if not same_object:
        result.update(state="unavailable", reason="status_identity_mismatch")
    elif state in {"scheduled", "writing", "verifying", "publishing-validation", "revalidation-needed", "failed"}:
        result.update({key: value for key, value in status_value.items() if key not in {"path", "bytes", "sha256"}})
        result["state"] = state
        result.setdefault("reason", "materialization_" + state)
    elif state == "ready":
        result.update(state="corrupt", reason="ready_payload_missing")
    else:
        result.update(state="unavailable", reason="unknown_materialization_state")
else:
    result.update(state="unavailable", reason="payload_and_status_absent")

print("COLDSNAP_NATIVE_STATUS " + json.dumps(result, sort_keys=True))
COLDSNAP_PY
""" % json.dumps(values)
    completed = run_script_on_host(host, script, ssh_kwargs=ssh_kwargs, timeout=60)
    if not completed.success:
        detail = completed.stderr.strip() or completed.stdout.strip() or "native status probe failed"
        raise RuntimeError(detail[-1000:])
    encoded = next(
        (
            line.removeprefix("COLDSNAP_NATIVE_STATUS ")
            for line in completed.stdout.splitlines()
            if line.startswith("COLDSNAP_NATIVE_STATUS ")
        ),
        None,
    )
    if encoded is None:
        raise RuntimeError("native status probe returned no record")
    result = json.loads(encoded)
    if result.get("worker") != worker or result.get("host") != host:
        raise RuntimeError("native status probe returned a mismatched worker identity")
    return result


def _ssh_kwargs(plan, sctx) -> dict[str, Any]:
    from sparkrun.orchestration.primitives import build_ssh_kwargs

    ssh_kwargs = build_ssh_kwargs(sctx.config)
    if plan.cluster.user:
        ssh_kwargs = {**ssh_kwargs, "ssh_user": plan.cluster.user}
    return ssh_kwargs


def _artifact_native_capability(request: dict[str, Any]) -> bool | None:
    artifact_path = Path(str(request.get("artifact") or "")).expanduser()
    if not artifact_path.is_file():
        return None
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        weights = artifact["weights"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if not isinstance(weights, dict):
        return None
    return isinstance(weights.get("native"), dict)


def _artifact_model_payloads(request: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Extract the manager's staging projection, not a second artifact schema.

    ColdSnap's Go ``internal/snapshot`` package remains authoritative for full
    format, semantic, topology, compatibility, and digest admission during
    prepare-only. This parser deliberately recognizes only the object identity
    needed to place per-worker payload bytes early.
    """

    artifact_path = Path(str(request.get("artifact") or "")).expanduser()
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(artifact["weights"]["native"], dict):
            return {}
        packs = artifact["weights"]["model_payloads"]["objects"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return {}
    if not isinstance(packs, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for pack in packs:
        if not isinstance(pack, dict) or not _is_content_addressed_model_payload(pack):
            return {}
        owner = pack.get("owner")
        if not isinstance(owner, str) or not owner.startswith("worker/"):
            return {}
        worker = owner.removeprefix("worker/")
        if not worker or worker in result:
            return {}
        result[worker] = pack
    return result


def _resolve_node_cached_pack(
    *,
    worker: str,
    unit: str,
    host: str,
    capture_id: str,
    snapshot_driver: str,
    state_root: str,
    expected: dict[str, Any],
    ssh_kwargs: dict,
    verifier: str,
) -> dict[str, Any]:
    from sparkrun.orchestration.primitives import run_script_on_host

    values = json.dumps(
        {
            "worker": worker,
            "state_root": state_root,
            "path": expected["path"],
            "expected_bytes": int(expected["bytes"]),
            "expected_sha256": str(expected["sha256"]),
        },
        sort_keys=True,
    )
    script = _payload_validation_script(
        values,
        verifier=verifier,
        path_program="""state_root = Path(config["state_root"]).resolve()
path = (state_root / config["path"]).resolve()
try:
    path.relative_to((state_root / "model-payloads").resolve())
except ValueError:
    raise SystemExit("node-local model payload path escapes the cache root")""",
        missing_is_miss=True,
    )
    result = run_script_on_host(host, script, ssh_kwargs=ssh_kwargs, timeout=120)
    if not result.success:
        detail = result.stderr.strip() or result.stdout.strip() or "node-local model payload is unavailable"
        raise RuntimeError(detail[-1000:])
    marker = next(
        (line.removeprefix("COLDSNAP_PACK ") for line in result.stdout.splitlines() if line.startswith("COLDSNAP_PACK ")),
        None,
    )
    if marker is None:
        miss = next(
            (line.removeprefix("COLDSNAP_MISS ") for line in result.stdout.splitlines() if line.startswith("COLDSNAP_MISS ")),
            None,
        )
        if miss is not None:
            try:
                reason = str(json.loads(miss).get("reason") or "node-local model payload is unavailable")
            except (AttributeError, TypeError, ValueError):
                reason = "node-local model payload is unavailable"
            raise RuntimeError(reason)
        raise RuntimeError("node-local model payload check returned no inventory")
    record = json.loads(marker)
    _validate_record(record)
    return record


def _capture_local_inventory(
    request: dict[str, Any],
) -> tuple[str, str, dict[str, str], dict[str, dict[str, Any]]] | None:
    artifact_path = Path(str(request.get("artifact") or "")).expanduser()
    if not artifact_path.is_file():
        return None
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        capture_id = artifact["capture_id"]
        snapshot_driver = artifact["snapshot_driver"]["id"]
        artifact_units = artifact["launch"]["units"]
        artifact_workers = artifact["launch"]["execution"]["workers"]
        native = artifact["weights"]["native"]
        packs = artifact["weights"]["model_payloads"]["objects"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return None
    if (
        artifact.get("kind") != "coldsnap-snapshot-artifact"
        or artifact.get("state") != "committed"
        or not isinstance(capture_id, str)
        or snapshot_driver not in {"n580", "n610"}
        or request.get("snapshot_driver", {}).get("id") != snapshot_driver
        or _SAFE_CAPTURE_ID.fullmatch(capture_id) is None
        or not isinstance(artifact_units, list)
        or not isinstance(artifact_workers, list)
        or not isinstance(native, dict)
        or not isinstance(packs, list)
    ):
        return None
    unit_hosts: dict[str, str] = {}
    for entry in artifact_units:
        if not isinstance(entry, dict):
            return None
        unit = entry.get("id")
        host = entry.get("host")
        if not isinstance(unit, str) or not unit or not isinstance(host, str) or not host:
            return None
        unit_hosts[unit] = host
    captured_hosts: dict[str, str] = {}
    for entry in artifact_workers:
        if not isinstance(entry, dict):
            return None
        worker = entry.get("id")
        unit = entry.get("unit")
        if not isinstance(worker, str) or not worker or unit not in unit_hosts:
            return None
        captured_hosts[worker] = unit_hosts[unit]
    expected_packs: dict[str, dict[str, Any]] = {}
    for pack in packs:
        if not isinstance(pack, dict) or not _is_content_addressed_model_payload(pack):
            return None
        owner = pack.get("owner")
        if not isinstance(owner, str) or not owner.startswith("worker/") or not owner.removeprefix("worker/"):
            return None
        expected_packs[owner.removeprefix("worker/")] = pack
    if set(captured_hosts) != set(expected_packs):
        return None
    return capture_id, snapshot_driver, captured_hosts, expected_packs


def _resolve_capture_local_pack(
    *,
    worker: str,
    unit: str,
    host: str,
    capture_id: str,
    snapshot_driver: str,
    state_root: str,
    expected: dict[str, Any],
    ssh_kwargs: dict,
    verifier: str,
) -> dict[str, Any]:
    from sparkrun.orchestration.primitives import run_script_on_host

    values = json.dumps(
        {
            "worker": worker,
            "unit": unit,
            "capture_id": capture_id,
            "snapshot_driver": snapshot_driver,
            "state_root": state_root,
            "expected_bytes": int(expected.get("bytes") or 0),
            "expected_sha256": str(expected.get("sha256") or ""),
        },
        sort_keys=True,
    )
    script = _payload_validation_script(
        values,
        verifier=verifier,
        path_program="""state_root = Path(config["state_root"]).resolve()
capture_root = (state_root / "captures" / config["capture_id"] / "drivers" / config["snapshot_driver"]).resolve()
try:
    capture_root.relative_to((state_root / "captures").resolve())
except ValueError:
    raise SystemExit("capture-local state path escapes the ColdSnap capture root")
hydration = capture_root / "units" / config["unit"] / "hydration"
matches = []
for manifest_path in hydration.glob("*/manifest.json"):
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
    candidate = manifest_path.parent / "model-weights.pack"
    if manifest.get("worker_id") == config["worker"] and candidate.is_file():
        matches.append(candidate.resolve())
if len(matches) != 1:
    raise SystemExit("expected exactly one capture-local model payload under %s; found %d" % (hydration, len(matches)))
path = matches[0]""",
    )
    result = run_script_on_host(
        host,
        script,
        ssh_kwargs=ssh_kwargs,
        timeout=3600,
    )
    if not result.success:
        detail = result.stderr.strip() or result.stdout.strip() or "remote local-pack verification failed"
        raise RuntimeError(detail[-1000:])
    marker = next(
        (line.removeprefix("COLDSNAP_PACK ") for line in result.stdout.splitlines() if line.startswith("COLDSNAP_PACK ")),
        None,
    )
    if marker is None:
        raise RuntimeError("remote local-pack verification did not return an inventory")
    record = json.loads(marker)
    _validate_record(record)
    return record


def _validate_expected_pack(record: dict[str, Any], expected: dict[str, Any]) -> None:
    _validate_record(record)
    if int(record["bytes"]) != int(expected.get("bytes") or 0):
        raise ValueError("capture-local model payload size differs from committed artifact")
    if str(record["sha256"]) != str(expected.get("sha256") or ""):
        raise ValueError("capture-local model payload digest differs from committed artifact")


def _fallback_or_raise(request: dict[str, Any], mode: str, failures: list[str]) -> StageOutcome:
    if mode == "native":
        raise RuntimeError("native ColdSnap model payload staging failed: " + "; ".join(failures))
    weights = request["policy"]["weights"]
    weights["mode"] = "recovery"
    native = weights.get("native")
    if isinstance(native, dict):
        # Native transport and staging hints are mutually dependent.  In
        # particular, an unpublished artifact can still describe its
        # capture-local objects without naming a repository.  Once auto mode
        # falls back to safetensors those hints are irrelevant, and retaining
        # only files_by_worker makes the otherwise valid recovery request fail
        # ColdSnap's strict policy validation.  Keep materialize so
        # ``materialize --native-weights required`` can populate the
        # node-local cache from the recovery load.
        for key in ("repository", "revision", "files_by_worker", "staged"):
            native.pop(key, None)
    return StageOutcome(request, "recovery", tuple(failures))


def _fill_native_inventory_from_artifact(request: dict[str, Any], native: dict[str, Any]) -> None:
    """Let a distributed recipe carry only the committed artifact descriptor.

    The artifact supplies its pinned HF repository/revision and content-addressed
    worker object paths, avoiding a second metadata file or recipe-owned paths.
    """
    artifact_path = Path(str(request.get("artifact") or "")).expanduser()
    if not artifact_path.is_file():
        return
    try:
        artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
        if not isinstance(artifact["weights"]["native"], dict):
            return
        provider = artifact["weights"]["model_payloads"]
        packs = provider["objects"]
    except (OSError, KeyError, TypeError, json.JSONDecodeError):
        return
    if not isinstance(provider, dict) or not isinstance(packs, list):
        return
    native["repository"] = str(provider.get("repository") or "")
    native["revision"] = str(provider.get("revision") or "")
    files: dict[str, str] = {}
    for pack in packs:
        if not isinstance(pack, dict) or not _is_content_addressed_model_payload(pack):
            return
        owner = pack.get("owner")
        path = pack.get("path")
        if not isinstance(owner, str) or not owner.startswith("worker/") or not isinstance(path, str) or not path:
            return
        files[owner.removeprefix("worker/")] = path
    native["files_by_worker"] = files


def _download_worker_pack(
    *,
    worker: str,
    host: str,
    repository: str,
    revision: str,
    filename: str,
    expected: dict[str, Any] | None,
    cache_dir: str,
    offline: bool,
    ssh_kwargs: dict,
    verifier: str,
) -> dict[str, Any]:
    from sparkrun.orchestration.primitives import run_script_on_host

    values = json.dumps(
        {
            "worker": worker,
            "repository": repository,
            "revision": revision,
            "filename": filename,
            "expected_bytes": int((expected or {}).get("bytes") or 0),
            "expected_sha256": str((expected or {}).get("sha256") or ""),
            "cache_dir": cache_dir,
            "offline": offline,
        },
        sort_keys=True,
    )
    if expected is None:
        raise ValueError("downloaded native payload requires committed object identity")
    script = _payload_validation_script(
        values,
        verifier=verifier,
        path_program="""from huggingface_hub import hf_hub_download
cache_dir = str(Path(config["cache_dir"]).expanduser())
path = Path(hf_hub_download(
    repo_id=config["repository"],
    revision=config["revision"],
    filename=config["filename"],
    cache_dir=os.path.join(cache_dir, "hub"),
    local_files_only=config["offline"],
)).resolve()""",
    )
    result = run_script_on_host(
        host,
        script,
        ssh_kwargs=ssh_kwargs,
        timeout=3600,
    )
    if not result.success:
        detail = result.stderr.strip() or result.stdout.strip() or "remote download failed"
        raise RuntimeError(detail[-1000:])
    marker = next(
        (line.removeprefix("COLDSNAP_PACK ") for line in result.stdout.splitlines() if line.startswith("COLDSNAP_PACK ")),
        None,
    )
    if marker is None:
        raise RuntimeError("remote download did not return a pack inventory")
    record = json.loads(marker)
    _validate_record(record)
    return record


def _validate_record(record: dict[str, Any]) -> None:
    digest = str(record.get("sha256") or "")
    path = str(record.get("path") or "")
    if (
        record.get("format") != 1
        or record.get("kind") != "coldsnap-payload-validation-result"
        or record.get("decision") != "accept"
        or not path.startswith("/")
        or int(record.get("bytes") or 0) <= 0
        or not digest.startswith("sha256:")
        or len(digest) != 71
        or any(character not in "0123456789abcdef" for character in digest[7:])
    ):
        raise ValueError("remote model payload inventory is invalid")
    validation = record.get("validation")
    if (
        not isinstance(validation, dict)
        or validation.get("record") != path + ".coldsnap-validation.json"
        or validation.get("provider") != "sha256-cache-v1"
        or not validation.get("content_evidence")
        or isinstance(validation.get("device"), bool)
        or not isinstance(validation.get("device"), int)
        or validation["device"] <= 0
        or isinstance(validation.get("inode"), bool)
        or not isinstance(validation.get("inode"), int)
        or validation["inode"] <= 0
        or validation.get("size") != int(record["bytes"])
        or isinstance(validation.get("mtime_ns"), bool)
        or not isinstance(validation.get("mtime_ns"), int)
        or validation["mtime_ns"] <= 0
        or int(validation.get("bytes_hashed") or 0) < 0
    ):
        raise ValueError("remote model payload validation evidence is invalid")


def _prepared_payload(record: dict[str, Any]) -> dict[str, Any]:
    """Project a verifier result envelope into ColdSnap's strict request type."""

    _validate_record(record)
    return {
        "worker": record["worker"],
        "path": record["path"],
        "bytes": record["bytes"],
        "sha256": record["sha256"],
        "validation": dict(record["validation"]),
    }


__all__ = ["StageOutcome", "stage_native_packs"]
