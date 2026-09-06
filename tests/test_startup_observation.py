# SPDX-FileCopyrightText: 2026 Scitrera LLC
# SPDX-FileCopyrightText: 2026 Fox Engine Ltd
# SPDX-License-Identifier: AGPL-3.0-only
# Additional permission under AGPLv3 section 7: see src/sparkrun/plugins/coldsnap/LICENSE_EXCEPTION.

from sparkrun.plugins.coldsnap.timing import startup_observation


def test_validated_spans_become_strategy_startup_observation():
    receipt = {
        "state": "succeeded",
        "timing": {
            "clocks": [{"id": "startup-unit-0", "origin_unix_ns": 1_000_000_000}],
            "spans": [
                {
                    "name": "runtime.startup_" + name,
                    "clock": "startup-unit-0",
                    "duration_seconds": seconds,
                    "status": "ok",
                    "attributes": {
                        "measurement": "rank0-acceptance-v1",
                        "observer": "rank0",
                        "response_validated": "true",
                        "container_id": "current",
                        "first_token_field": "reasoning",
                        "observer_started_unix_ns": "1000000001",
                        "max_tokens": "64",
                        "prompt_sha256": "a" * 64,
                    },
                }
                for name, seconds in (("port_open", 1), ("http_ready", 2), ("ttft", 3))
            ],
        },
    }
    value = startup_observation(receipt)
    assert value["inference_ready"] is True
    assert value["first_token_unix_ns"] == 4_000_000_000
    assert value["port_open_unix_ns"] == 2_000_000_000
    assert value["http_ready_unix_ns"] == 3_000_000_000
    assert value["first_token_field"] == "reasoning"
    assert value["observer_started_unix_ns"] == 1_000_000_001
    assert value["max_tokens"] == 64 and value["prompt_sha256"] == "a" * 64
    # New upstream hosts accept the receipt directly; older published hosts do
    # not have this module, and the plugin's optional handoff stays compatible.
    try:
        from sparkrun.orchestration.startup import validate_observation
    except ImportError:
        pass
    else:
        assert validate_observation(value) == value
    receipt["state"] = "failed"
    assert startup_observation(receipt) == {}
    assert startup_observation(None) == {}
    assert startup_observation({"state": "succeeded", "timing": {"clocks": [], "spans": []}}) == {}
