#!/usr/bin/env python3
"""Stub `command` provider for tests/test_classify.py - reads a prompt on
stdin (like a real `reg-radar classify --provider command` invocation would
feed a subscription CLI) and writes a JSON classification response to
stdout. Behaviour is controlled by the CLASSIFY_STUB_MODE env var so the
same script covers both the happy path and every validation-rejection
case, without a separate script per scenario.
"""
import json
import os
import re
import sys

_GOOD_VALUE = {"document_type": "press_release", "instrument": "mica"}


def main() -> int:
    prompt = sys.stdin.read()
    match = re.search(r'taxonomy axis "([^"]+)"|on the "([^"]+)" taxonomy axis', prompt)
    axis = next((g for g in (match.groups() if match else ()) if g), "document_type")
    mode = os.environ.get("CLASSIFY_STUB_MODE", "valid")

    if mode == "valid":
        payload = {"axis": axis, "values": [{"value": _GOOD_VALUE.get(axis, "press_release"), "confidence": 0.8}],
                   "reasoning": "stub"}
    elif mode == "empty":
        payload = {"axis": axis, "values": [], "reasoning": "stub: nothing confident"}
    elif mode == "bad_axis":
        payload = {"axis": "not_a_real_axis", "values": [{"value": "press_release", "confidence": 0.8}]}
    elif mode == "bad_value":
        payload = {"axis": axis, "values": [{"value": "not_a_real_value", "confidence": 0.8}]}
    elif mode == "bad_cardinality":
        payload = {"axis": axis, "values": [
            {"value": "press_release", "confidence": 0.8},
            {"value": "warning", "confidence": 0.7},
        ]}
    elif mode == "bad_confidence":
        payload = {"axis": axis, "values": [{"value": _GOOD_VALUE.get(axis, "press_release"), "confidence": 1.5}]}
    else:
        raise SystemExit(f"unknown CLASSIFY_STUB_MODE {mode!r}")

    print(json.dumps(payload))
    return 0


if __name__ == "__main__":
    sys.exit(main())
