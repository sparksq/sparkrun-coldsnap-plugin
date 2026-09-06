# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Validate and import manager-neutral ColdSnap operation timing receipts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from sparkrun.core.timing import Timeline

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_CLOCKS = 128
_MAX_SPANS = 2048
_MAX_ATTRIBUTES = 32
_MAX_EVENT_BYTES = 64 << 10
_MAX_EVENTS = 8192
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")

_PROGRESS_ACTIONS: dict[str, tuple[str, str, str, int]] = {
    "artifact.read_verify": ("reading and verifying the artifact descriptor", "reading and verifying the artifact descriptor", "none", 10),
    "compatibility.verify": ("checking platform compatibility", "checking platform compatibility", "nodes", 10),
    "compatibility.verify_hosts": ("checking snapshot-driver compatibility", "checking snapshot-driver compatibility", "nodes", 10),
    "weights.select": ("selecting the weight provider", "selecting the weight provider", "none", 10),
    "weights.native_verify": ("verifying staged native model payloads", "verifying staged native model payloads", "nodes", 10),
    "capsules.prepare": ("preparing capsule images", "preparing capsule images", "nodes", 10),
    "capsule.prepare": ("preparing capsule", "preparing capsules", "unit", 20),
    "capsule.pull": ("pulling capsule", "pulling capsules", "unit", 30),
    "capsule.verify": ("verifying capsule", "verifying capsules", "unit", 20),
    "nccl.verify": ("verifying capsule NCCL providers", "verifying capsule NCCL providers", "nodes", 10),
    "restore.prepare": ("preparing restore prerequisites", "preparing restore prerequisites", "nodes", 10),
    "materialization.prepare": ("preparing native-weight materialization", "preparing native-weight materialization", "nodes", 10),
    "coordinator.start": ("starting the coordination service", "starting the coordination service", "head", 20),
    "network.port_select": ("selecting collision-free restore ports", "selecting collision-free restore ports", "nodes", 20),
    "units.launch": ("launching workload units", "launching workload units", "nodes", 10),
    "unit.launch": ("launching workload unit", "launching workload units", "unit", 20),
    "units.ready": ("waiting for restored units to become ready", "waiting for restored units to become ready", "nodes", 30),
    "materialization.verify": ("verifying materialized native weights", "verifying materialized native weights", "nodes", 20),
    "workload.logs": ("publishing workload logs", "publishing workload logs", "nodes", 10),
    "lifecycle.synchronize": ("synchronizing restored lifecycle state", "synchronizing restored lifecycle state", "nodes", 20),
    "lifecycle.persist": ("persisting workload lifecycle state", "persisting workload lifecycle state", "nodes", 10),
    "units.capture_ready": ("waiting for capture units to become ready", "waiting for capture units to become ready", "nodes", 30),
    "capture.ownership": ("normalizing captured artifact ownership", "normalizing captured artifact ownership", "nodes", 10),
    "cache.seed": ("collecting runtime cache seeds", "collecting runtime cache seeds", "nodes", 10),
    "capsules.construct": ("constructing capsule images", "constructing capsule images", "nodes", 10),
    "capsule.construct": ("constructing capsule", "constructing capsules", "unit", 20),
    "capsules.publish": ("publishing capsule images", "publishing capsule images", "nodes", 10),
    "capsule.publish": ("publishing capsule", "publishing capsules", "unit", 20),
    "native.verify_local": ("verifying capture-local native payloads", "verifying capture-local native payloads", "nodes", 10),
    "native.publish": ("publishing native model payloads", "publishing native model payloads", "nodes", 20),
    "artifact.write": ("committing the artifact descriptor", "committing the artifact descriptor", "none", 10),
}


class OperationTimingProgress:
    """Turn validated timing events into concise, node-aware status text."""

    def __init__(self, request: Mapping[str, Any]):
        units = request.get("launch", {}).get("units", [])
        self.hosts_by_unit = {
            str(unit.get("id")): str(unit.get("host"))
            for unit in units
            if isinstance(unit, Mapping) and unit.get("id") and unit.get("host")
        }
        self.hosts = tuple(dict.fromkeys(self.hosts_by_unit.values()))
        self.clocks: dict[str, Mapping[str, Any]] = {}
        self.depths: dict[str, int] = {}
        self.active: dict[str, dict[str, Any]] = {}
        self._sequence = 0
        self._lock = threading.Lock()

    def accept(self, event: Mapping[str, Any]) -> tuple[str, bool] | None:
        """Record one event and return ``(message, default_visible)`` when useful."""
        with self._lock:
            if event.get("event") == "clock" and isinstance(event.get("clock"), Mapping):
                clock = event["clock"]
                self.clocks[str(clock["id"])] = clock
                return None
            span = event.get("span")
            if not isinstance(span, Mapping):
                return None
            span_id = str(span["id"])
            parent = span.get("parent")
            depth = self.depths.get(str(parent), -1) + 1 if parent else 0
            self.depths[span_id] = depth
            action = _PROGRESS_ACTIONS.get(str(span.get("name")))
            if event.get("event") == "span_start":
                if action is None:
                    return (f"ColdSnap timing: started {span['name']}", False)
                self._sequence += 1
                entry = self._entry(span, action, self._sequence)
                self.active[span_id] = entry
                return ("ColdSnap: " + self._describe(entry, plural=False), True)
            if event.get("event") != "span_complete":
                return None
            entry = self.active.pop(span_id, None)
            if action is None:
                if depth <= 4:
                    return (f"ColdSnap timing: {span['name']} done ({float(span['duration_seconds']):.1f}s)", False)
                return None
            if entry is None:
                entry = self._entry(span, action, self._sequence)
            duration = float(span["duration_seconds"])
            failed = span.get("status") == "error"
            if duration < 1.0 and not failed:
                return None
            suffix = " failed" if failed else " finished"
            return (f"ColdSnap: {self._describe(entry, plural=False)}{suffix} ({duration:.1f}s)", True)

    def heartbeat_label(self, base: str) -> str:
        """Return a heartbeat label describing the most specific active work."""
        with self._lock:
            if not self.active:
                return base
            priority = max(int(entry["priority"]) for entry in self.active.values())
            candidates = [entry for entry in self.active.values() if int(entry["priority"]) == priority]
            newest = max(candidates, key=lambda entry: int(entry["sequence"]))
            matching = [entry for entry in candidates if entry["name"] == newest["name"]]
            return base + " — " + self._describe_group(matching)

    def _entry(self, span: Mapping[str, Any], action: tuple[str, str, str, int], sequence: int) -> dict[str, Any]:
        singular, plural, scope, priority = action
        attributes = span.get("attributes") if isinstance(span.get("attributes"), Mapping) else {}
        clock = self.clocks.get(str(span.get("clock")), {})
        unit = str(attributes.get("unit") or clock.get("unit") or "")
        worker = str(attributes.get("worker") or clock.get("worker") or "")
        host = str(attributes.get("host") or self.hosts_by_unit.get(unit) or "")
        return {
            "name": str(span.get("name")),
            "singular": singular,
            "plural": plural,
            "scope": scope,
            "priority": priority,
            "sequence": sequence,
            "unit": unit,
            "worker": worker,
            "host": host,
        }

    def _describe(self, entry: Mapping[str, Any], *, plural: bool) -> str:
        phrase = str(entry["plural"] if plural else entry["singular"])
        scope = str(entry["scope"])
        unit = str(entry["unit"])
        worker = str(entry["worker"])
        host = str(entry["host"])
        if scope == "unit":
            if worker:
                phrase += f" for {worker}"
            elif unit:
                phrase += f" for {unit}"
            if host:
                phrase += f" on {host}"
        elif scope == "head" and self.hosts:
            phrase += f" on {self.hosts[0]}"
        elif scope == "nodes" and self.hosts:
            phrase += self._host_scope(self.hosts)
        return phrase

    def _describe_group(self, entries: list[Mapping[str, Any]]) -> str:
        if len(entries) == 1:
            return self._describe(entries[0], plural=False)
        phrase = str(entries[0]["plural"])
        hosts = tuple(dict.fromkeys(str(entry["host"]) for entry in entries if entry["host"]))
        if hosts:
            phrase += self._host_scope(hosts)
        else:
            noun = "unit" if len(entries) == 1 else "units"
            phrase += f" for {len(entries)} {noun}"
        return phrase

    @staticmethod
    def _host_scope(hosts: tuple[str, ...]) -> str:
        if len(hosts) <= 3:
            return " on " + ", ".join(hosts)
        return f" on {len(hosts)} nodes"


class OperationTimingEventStream:
    """Incrementally validate one advisory ColdSnap timing NDJSON stream."""

    def __init__(self, request: Mapping[str, Any], on_event: Callable[[Mapping[str, Any]], None] | None = None):
        self.operation_id = request.get("id")
        self.request_sha256: str | None = None
        self.sequence = 0
        self.started = False
        self.completed = False
        self.clocks: dict[str, dict[str, Any]] = {}
        self.spans: dict[str, bool] = {}
        self.parents: dict[str, str | None] = {}
        self.span_starts: dict[str, dict[str, Any]] = {}
        self.timing_sha256: str | None = None
        self.state: str | None = None
        self.on_event = on_event

    def accept(self, line: str) -> Mapping[str, Any]:
        if len(line.encode("utf-8")) > _MAX_EVENT_BYTES:
            raise RuntimeError("ColdSnap timing event exceeds its size limit")
        try:
            event = json.loads(line, object_pairs_hook=_reject_duplicate_keys)
        except (TypeError, ValueError) as error:
            raise RuntimeError("ColdSnap timing event is not valid JSON") from error
        if not isinstance(event, dict):
            raise RuntimeError("ColdSnap timing event envelope is invalid")  # noqa: TRY004 -- protocol error
        digest = event.get("request_sha256")
        if (
            event.get("format") != 1
            or event.get("kind") != "coldsnap-timing-event"
            or event.get("operation_id") != self.operation_id
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
            or self.completed
            or not isinstance(event.get("sequence"), int)
            or isinstance(event.get("sequence"), bool)
            or event.get("sequence") != self.sequence + 1
            or event.get("sequence") > _MAX_EVENTS
        ):
            raise RuntimeError("ColdSnap timing event ordering or identity is invalid")
        if self.request_sha256 is None:
            self.request_sha256 = digest
        elif digest != self.request_sha256:
            raise RuntimeError("ColdSnap timing event request identity changed")
        self.sequence += 1
        kind = event.get("event")
        if kind == "stream_start":
            if self.started or self.sequence != 1 or set(event) != _EVENT_BASE_KEYS:
                raise RuntimeError("ColdSnap timing stream start is invalid")
            self.started = True
        elif not self.started:
            raise RuntimeError("ColdSnap timing event stream has no start")
        elif kind == "clock":
            self._accept_clock(event)
        elif kind in {"span_start", "span_complete"}:
            self._accept_span(event, completed=kind == "span_complete")
        elif kind == "stream_complete":
            self._accept_complete(event)
        else:
            raise RuntimeError("ColdSnap timing event type is unsupported")
        if self.on_event is not None:
            self.on_event(event)
        return event

    def finish(self, receipt: Mapping[str, Any]) -> None:
        if not self.completed:
            raise RuntimeError("ColdSnap timing event stream ended before completion")
        if self.request_sha256 != receipt.get("request_sha256"):
            raise RuntimeError("ColdSnap timing event stream does not match the final receipt")
        timing = receipt.get("timing")
        encoded = json.dumps(timing, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        if self.timing_sha256 != digest or self.state != receipt.get("state"):
            raise RuntimeError("ColdSnap timing event stream completion does not match the final receipt")

    def _accept_clock(self, event: Mapping[str, Any]) -> None:
        clock = event.get("clock")
        if set(event) != _EVENT_BASE_KEYS | {"clock"} or not _valid_clock(clock) or clock["id"] in self.clocks:
            raise RuntimeError("ColdSnap timing clock event is invalid")
        self.clocks[clock["id"]] = clock

    def _accept_span(self, event: Mapping[str, Any], *, completed: bool) -> None:
        span = event.get("span")
        if set(event) != _EVENT_BASE_KEYS | {"span"} or not _valid_event_span(span, completed=completed):
            raise RuntimeError("ColdSnap timing span event is invalid")
        span_id = span["id"]
        parent = span.get("parent")
        if span["clock"] not in self.clocks or (parent is not None and parent not in self.spans):
            raise RuntimeError("ColdSnap timing span references unavailable context")
        known = span_id in self.spans
        if (not completed and known) or (completed and known and self.spans[span_id]):
            raise RuntimeError("ColdSnap timing span lifecycle is invalid")
        if completed and known and self.parents[span_id] != parent:
            raise RuntimeError("ColdSnap timing span parent changed")
        if completed and known and not _same_span_lifecycle(self.span_starts[span_id], span):
            raise RuntimeError("ColdSnap timing span fields changed")
        self.spans[span_id] = completed
        self.parents[span_id] = parent
        if not completed:
            self.span_starts[span_id] = span

    def _accept_complete(self, event: Mapping[str, Any]) -> None:
        digest = event.get("timing_sha256")
        if (
            set(event) != _EVENT_BASE_KEYS | {"state", "timing_sha256"}
            or event.get("state") not in {"succeeded", "failed"}
            or not isinstance(digest, str)
            or not _DIGEST.fullmatch(digest)
            or not self.clocks
            or not self.spans
            or not all(self.spans.values())
        ):
            raise RuntimeError("ColdSnap timing stream completion is invalid")
        self.completed = True
        self.state = str(event["state"])
        self.timing_sha256 = digest


_EVENT_BASE_KEYS = {"format", "kind", "sequence", "event", "operation_id", "request_sha256"}


def _reject_duplicate_keys(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError("duplicate JSON object key")
        result[key] = value
    return result


def follow_operation_timing_events(
    path: Path,
    request: Mapping[str, Any],
    stop: threading.Event,
    on_event: Callable[[Mapping[str, Any]], None] | None = None,
) -> OperationTimingEventStream:
    """Follow a controller-owned append-only stream until the caller stops it."""
    stream = OperationTimingEventStream(request, on_event=on_event)
    file = None
    try:
        while not stop.is_set() or file is not None:
            if file is None:
                try:
                    file = path.open("r", encoding="utf-8")
                except FileNotFoundError:
                    if stop.wait(0.05):
                        break
                    continue
            position = file.tell()
            line = file.readline(_MAX_EVENT_BYTES + 2)
            if line:
                if not line.endswith("\n"):
                    if len(line) > _MAX_EVENT_BYTES:
                        raise RuntimeError("ColdSnap timing event exceeds its size limit")
                    if stop.is_set():
                        raise RuntimeError("ColdSnap timing event stream ended with a partial record")
                    file.seek(position)
                    time.sleep(0.05)
                    continue
                stream.accept(line[:-1])
                continue
            if stop.is_set():
                break
            time.sleep(0.05)
    finally:
        if file is not None:
            file.close()
    return stream


def _valid_clock(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and {"id", "source", "origin_unix_ns"} <= set(value) <= {"id", "source", "origin_unix_ns", "unit", "worker"}
        and _safe_id(value.get("id"))
        and _safe_text(value.get("source"), 64)
        and isinstance(value.get("origin_unix_ns"), int)
        and not isinstance(value.get("origin_unix_ns"), bool)
        and value["origin_unix_ns"] > 0
        and ("unit" not in value or _safe_id(value.get("unit")))
        and ("worker" not in value or _safe_id(value.get("worker")))
    )


def _valid_event_span(value: Any, *, completed: bool) -> bool:
    if not isinstance(value, dict):
        return False
    required = {"id", "name", "clock", "start_offset_seconds"}
    allowed = required | {"parent", "attributes"}
    if completed:
        required |= {"duration_seconds", "status"}
        allowed |= {"duration_seconds", "status"}
    attributes = value.get("attributes", {})
    return (
        required <= set(value) <= allowed
        and _safe_id(value.get("id"))
        and ("parent" not in value or _safe_id(value.get("parent")))
        and _safe_text(value.get("name"), 128)
        and _safe_id(value.get("clock"))
        and _finite_nonnegative(value.get("start_offset_seconds"))
        and (not completed or (_finite_nonnegative(value.get("duration_seconds")) and value.get("status") in {"ok", "error"}))
        and isinstance(attributes, dict)
        and len(attributes) <= _MAX_ATTRIBUTES
        and all(_safe_id(key) and _safe_text(item, 512) for key, item in attributes.items())
    )


def _same_span_lifecycle(start: Mapping[str, Any], completed: Mapping[str, Any]) -> bool:
    ignored = {"duration_seconds", "status"}
    completed_lifecycle = {key: value for key, value in completed.items() if key not in ignored}
    start_lifecycle = dict(start)
    if completed.get("status") == "error":
        completed_attributes = dict(completed_lifecycle.get("attributes") or {})
        start_attributes = dict(start_lifecycle.get("attributes") or {})
        completed_attributes.pop("error", None)
        start_attributes.pop("error", None)
        if completed_attributes:
            completed_lifecycle["attributes"] = completed_attributes
        else:
            completed_lifecycle.pop("attributes", None)
        if start_attributes:
            start_lifecycle["attributes"] = start_attributes
        else:
            start_lifecycle.pop("attributes", None)
    return completed_lifecycle == start_lifecycle


def read_operation_receipt(path: Path, request: Mapping[str, Any], returncode: int) -> dict[str, Any] | None:
    """Read a controller receipt when present and fail closed on malformed data."""
    try:
        payload = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        receipt = json.loads(payload)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("ColdSnap controller returned an invalid operation receipt") from error
    if not isinstance(receipt, dict) or receipt.get("kind") != "coldsnap-operation-receipt":
        raise RuntimeError("ColdSnap controller returned an unsupported operation receipt")
    if receipt.get("format") != 2:
        raise RuntimeError("ColdSnap controller operation receipt format is unsupported")
    expected = {
        "operation_id": request.get("id"),
        "operation": request.get("operation"),
        "engine": request.get("launch", {}).get("engine"),
        "snapshot_driver": request.get("snapshot_driver", {}).get("id"),
    }
    if any(receipt.get(key) != value for key, value in expected.items()):
        raise RuntimeError("ColdSnap controller operation receipt does not match its request")
    expected_state = "succeeded" if returncode == 0 else "failed"
    if receipt.get("state") != expected_state or not _finite_nonnegative(receipt.get("duration_seconds")):
        raise RuntimeError("ColdSnap controller operation receipt result is invalid")
    _validate_timing(receipt.get("timing"))
    return receipt


def startup_observation(receipt: Mapping[str, Any] | None) -> dict[str, Any]:
    """Project already-validated controller spans into the host readiness contract."""
    if receipt is None or receipt.get("state") != "succeeded":
        return {}
    timing = receipt["timing"]
    clocks = {clock["id"]: clock for clock in timing["clocks"]}
    spans = {
        span["name"]: span
        for span in timing["spans"]
        if span.get("status") == "ok" and span.get("attributes", {}).get("measurement") == "rank0-acceptance-v1"
    }
    first = spans.get("runtime.startup_ttft")
    if first is None:
        return {}
    clock = clocks[first["clock"]]
    attributes = first["attributes"]
    if attributes.get("observer") != "rank0" or attributes.get("response_validated") != "true":
        return {}
    result = {
        "format": 1,
        "measurement": "rank0-acceptance-v1",
        "observer": "rank0",
        "container_id": attributes["container_id"],
        "container_started_unix_ns": clock["origin_unix_ns"],
        "first_token_field": attributes["first_token_field"],
        "inference_ready": True,
        "response_validated": True,
        "http_ready_path": "/health",
    }
    for key in ("observer_started_unix_ns", "max_tokens"):
        value = attributes.get(key, "")
        if value.isdecimal() and int(value) > 0:
            result[key] = int(value)
    if attributes.get("prompt_sha256"):
        result["prompt_sha256"] = attributes["prompt_sha256"]
    for name, key in (
        ("runtime.startup_ttft", "first_token_unix_ns"),
        ("runtime.startup_port_open", "port_open_unix_ns"),
        ("runtime.startup_http_ready", "http_ready_unix_ns"),
    ):
        span = spans.get(name)
        if span is not None and span["clock"] == first["clock"]:
            result[key] = clock["origin_unix_ns"] + round(span["duration_seconds"] * 1e9)
    return result


def import_operation_timing(timeline: Timeline | None, receipt: Mapping[str, Any] | None, parent: int | None) -> int:
    if timeline is None or receipt is None or parent is None:
        return 0
    timing = receipt["timing"]
    clocks = {clock["id"]: clock for clock in timing["clocks"]}
    pending = {span["id"]: span for span in timing["spans"]}
    imported: dict[str, int] = {}
    while pending:
        progressed = False
        for span_id, span in list(pending.items()):
            foreign_parent = span.get("parent")
            if foreign_parent and foreign_parent not in imported:
                continue
            clock = clocks[span["clock"]]
            wall_start = clock["origin_unix_ns"] / 1_000_000_000 + span["start_offset_seconds"]
            attributes = dict(span.get("attributes") or {})
            attributes.update(
                {
                    key: value
                    for key, value in {
                        "timing_source": clock["source"],
                        "unit": clock.get("unit"),
                        "worker": clock.get("worker"),
                    }.items()
                    if value and key not in attributes
                }
            )
            imported[span_id] = timeline.add_span(
                f"coldsnap.{span['name']}",
                clock=_clock_name(clock),
                duration_s=span["duration_seconds"],
                wall_start=wall_start,
                parent=imported.get(foreign_parent, parent),
                status=span["status"],
                **attributes,
            )
            del pending[span_id]
            progressed = True
        if not progressed:
            raise RuntimeError("ColdSnap controller timing span tree cannot be imported")
    return len(imported)


def _clock_name(clock: Mapping[str, Any]) -> str:
    source = str(clock["source"])
    if source == "controller":
        return "coldsnap:controller"
    identity = clock.get("worker") or clock.get("unit") or clock["id"]
    return f"coldsnap:{source}:{identity}"


def _validate_timing(value: Any) -> None:
    if not isinstance(value, dict) or value.get("format") != 1:
        raise RuntimeError("ColdSnap controller timing envelope is invalid")
    clocks = value.get("clocks")
    spans = value.get("spans")
    if not isinstance(clocks, list) or not 1 <= len(clocks) <= _MAX_CLOCKS:
        raise RuntimeError("ColdSnap controller timing clock inventory is invalid")
    if not isinstance(spans, list) or not 1 <= len(spans) <= _MAX_SPANS:
        raise RuntimeError("ColdSnap controller timing span inventory is invalid")
    clocks_by_id: dict[str, dict[str, Any]] = {}
    for clock in clocks:
        if (
            not isinstance(clock, dict)
            or not _safe_id(clock.get("id"))
            or clock["id"] in clocks_by_id
            or not _safe_text(clock.get("source"), 64)
            or not isinstance(clock.get("origin_unix_ns"), int)
            or isinstance(clock.get("origin_unix_ns"), bool)
            or clock["origin_unix_ns"] <= 0
            or (clock.get("unit") is not None and not _safe_id(clock.get("unit")))
            or (clock.get("worker") is not None and not _safe_id(clock.get("worker")))
        ):
            raise RuntimeError("ColdSnap controller timing clock is invalid")
        clocks_by_id[clock["id"]] = clock
    spans_by_id: dict[str, dict[str, Any]] = {}
    for span in spans:
        attributes = span.get("attributes", {}) if isinstance(span, dict) else None
        if (
            not isinstance(span, dict)
            or not _safe_id(span.get("id"))
            or span["id"] in spans_by_id
            or not _safe_text(span.get("name"), 128)
            or span.get("clock") not in clocks_by_id
            or not _finite_nonnegative(span.get("start_offset_seconds"))
            or not _finite_nonnegative(span.get("duration_seconds"))
            or span.get("status") not in {"ok", "error"}
            or not isinstance(attributes, dict)
            or len(attributes) > _MAX_ATTRIBUTES
            or any(not _safe_id(key) or not _safe_text(item, 512) for key, item in attributes.items())
        ):
            raise RuntimeError("ColdSnap controller timing span is invalid")
        spans_by_id[span["id"]] = span
    for span in spans:
        parent = span.get("parent")
        if parent is not None and (not _safe_id(parent) or parent == span["id"] or parent not in spans_by_id):
            raise RuntimeError("ColdSnap controller timing span parent is invalid")
        seen = {span["id"]}
        while parent is not None:
            if parent in seen:
                raise RuntimeError("ColdSnap controller timing span tree contains a cycle")
            seen.add(parent)
            parent = spans_by_id[parent].get("parent")


def _safe_id(value: Any) -> bool:
    return isinstance(value, str) and bool(_SAFE_ID.fullmatch(value))


def _safe_text(value: Any, maximum: int) -> bool:
    return (
        isinstance(value, str)
        and value == value.strip()
        and 0 < len(value) <= maximum
        and all(ord(character) >= 0x20 and ord(character) != 0x7F for character in value)
    )


def _finite_nonnegative(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
