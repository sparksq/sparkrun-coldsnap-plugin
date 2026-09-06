# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

"""Validation for runtime-v1: unknown requirements must never be silently dropped."""

from __future__ import annotations

import base64
import posixpath
from collections.abc import Mapping

from sparkrun.transports.session import HostSessionError


def _text(value):
    return isinstance(value, str) and bool(value) and "\x00" not in value


def _reference(value):
    return _text(value) and not value.startswith("-") and not any(c.isspace() for c in value)


def _path(value):
    return _text(value) and value.startswith("/") and posixpath.normpath(value) == value and ":" not in value


def _strings(value):
    return isinstance(value, list) and all(isinstance(item, str) and "\x00" not in item for item in value)


def _mapping(value):
    return isinstance(value, Mapping) and all(
        _text(key) and "=" not in key and isinstance(item, str) and "\x00" not in item for key, item in value.items()
    )


def _bytes(value):
    if not isinstance(value, str):
        return False
    try:
        base64.b64decode(value, validate=True)
        return True
    except ValueError:
        return False


def _optional(check):
    return lambda value: value == "" or check(value)


def _object(value, schema, required, name):
    if not isinstance(value, Mapping):
        raise HostSessionError(f"{name} must be an object")
    unknown = set(value) - set(schema)
    if unknown:
        raise HostSessionError(f"{name} has unknown fields: {', '.join(sorted(unknown))}")
    for field in required:
        if field not in value:
            raise HostSessionError(f"{name}.{field} is required")
    for field, item in value.items():
        if not schema[field](item):
            raise HostSessionError(f"{name}.{field} is invalid")
    return True


def _mount(value):
    return _object(value, {"source": _path, "target": _path, "read_only": lambda x: type(x) is bool}, {"source", "target"}, "mount")


def _device(value):
    return _object(value, {"source": _path, "target": _path}, {"source"}, "device")


def _list(check):
    return lambda value: isinstance(value, list) and all(check(item) for item in value)


def _BOOL(value):
    return type(value) is bool


def _NONNEGATIVE(value):
    return type(value) is int and value >= 0


def _workload(value):
    schema = {
        "name": _optional(_reference),
        "image": _reference,
        "detached": _BOOL,
        "remove_after_exit": _BOOL,
        "privileged": _BOOL,
        "seccomp_unconfined": _BOOL,
        "memlock_unlimited": _BOOL,
        "combined": _BOOL,
        "shared_memory_bytes": _NONNEGATIVE,
        "pull_policy": lambda x: x in ("", "always", "missing", "never"),
        "network": lambda x: x in ("", "default", "host", "none"),
        "gpus": _list(lambda x: _reference(x) and "," not in x and '"' not in x),
        "user": _optional(_reference),
        "entrypoint": _optional(_text),
        "environment": _mapping,
        "labels": _mapping,
        "mounts": _list(_mount),
        "devices": _list(_device),
        "command": _strings,
        "input": _bytes,
    }
    _object(value, schema, {"image"}, "workload")
    if value.get("detached") and (not value.get("name") or "input" in value or value.get("combined")):
        raise HostSessionError("detached workloads require a name and separate output, without stdin")
    return True


def _execution(value):
    return _object(
        value,
        {
            "command": lambda x: _strings(x) and bool(x) and bool(x[0]),
            "input": _bytes,
            "combined": _BOOL,
            "user": _optional(_reference),
        },
        {"command"},
        "execution",
    )


def _build(value):
    return _object(
        value,
        {
            "dockerfile": lambda x: _bytes(x) and bool(x),
            "context": _path,
            "tag": _reference,
            "contexts": lambda x: _mapping(x) and all(_path(v) for v in x.values()),
            "arguments": _mapping,
            "pull": _BOOL,
        },
        {"dockerfile", "context", "tag"},
        "build",
    )


def validate_runtime_request(request):
    actions = {
        "image-inspect": ({"image": _reference}, {"image"}),
        "image-pull": ({"image": _reference}, {"image"}),
        "image-push": ({"image": _reference}, {"image"}),
        "image-remove": ({"image": _reference}, {"image"}),
        "image-tag": ({"source": _reference, "target": _reference}, {"source", "target"}),
        "image-build": ({"build": _build}, {"build"}),
        "workload-run": ({"workload": _workload}, {"workload"}),
        "workload-remove": ({"name": _reference}, {"name"}),
        "workload-inspect": ({"name": _reference, "include_start_time": _BOOL}, {"name"}),
        "workload-exec": ({"name": _reference, "execution": _execution}, {"name", "execution"}),
        "workload-logs": ({"name": _reference, "tail": _NONNEGATIVE}, {"name"}),
        "workload-copy-from": ({"name": _reference, "path": _path, "destination": _path}, {"name", "path", "destination"}),
    }
    if not isinstance(request, Mapping) or not isinstance(request.get("action"), str) or request["action"] not in actions:
        raise HostSessionError("unsupported manager runtime action")
    schema, required = actions[request["action"]]
    _object(request, {"action": _text, **schema}, required | {"action"}, "runtime")
