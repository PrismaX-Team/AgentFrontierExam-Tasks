"""Trusted local validation_ledger component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Callable


class RequestRejected(ValueError):
    """An ordinary no-charge request rejection."""


def canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
        ).encode("ascii")
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise RequestRejected("request is not finite canonical JSON") from exc


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _clone(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except RecursionError as exc:
        raise RequestRejected("JSON value is too deeply nested to copy safely") from exc


def _digest(value: Any) -> bool:
    return type(value) is str and re.fullmatch(r"[0-9a-f]{64}", value) is not None


def _request_id(value: Any) -> bool:
    return type(value) is str and 0 < len(value) <= 128


def _configuration(task_digest: str, limits: dict[str, int], error_type: type[ValueError]) -> None:
    if not _digest(task_digest):
        raise error_type("task digest must be a lowercase SHA-256 hex string")
    if type(limits) is not dict or not limits or any(
        type(key) is not str or not key or type(value) is not int or value < 0
        for key, value in limits.items()
    ):
        raise error_type("limits must be nonempty nonnegative integer counters")


def _response(value: Any) -> bytes:
    if type(value) is not dict or type(value.get("status")) is not str or not value["status"]:
        raise RequestRejected("trusted response must contain a nonempty status string")
    return canonical_bytes(value)


def empty_state(task_digest: str, limits: dict[str, int]) -> dict[str, Any]:
    _configuration(task_digest, limits, ValueError)
    return {
        "version": 1,
        "task_digest": task_digest,
        "counts": {key: 0 for key in limits},
        "request_cache": {},
        "science_cache": {},
        "history": [],
    }


def validate_state(state: dict[str, Any], task_digest: str, limits: dict[str, int]) -> None:
    _configuration(task_digest, limits, RequestRejected)
    if type(state) is not dict or set(state) != {
        "version", "task_digest", "counts", "request_cache", "science_cache", "history"
    }:
        raise RequestRejected("trusted state shape mismatch")
    if type(state["version"]) is not int or state["version"] != 1 or not _digest(state["task_digest"]) or state["task_digest"] != task_digest:
        raise RequestRejected("trusted state is bound to another task version")
    if type(state["counts"]) is not dict or set(state["counts"]) != set(limits):
        raise RequestRejected("trusted counter shape mismatch")
    if any(type(value) is not int or value < 0 or value > limits[key]
           for key, value in state["counts"].items()):
        raise RequestRejected("trusted counter out of range")
    if type(state["request_cache"]) is not dict or type(state["science_cache"]) is not dict:
        raise RequestRejected("trusted cache shape mismatch")
    if type(state["history"]) is not list:
        raise RequestRejected("trusted history shape mismatch")
    history_by_key = {}
    actual_counts = {kind: 0 for kind in limits}
    for ordinal, item in enumerate(state["history"], start=1):
        if type(item) is not dict or set(item) != {"ordinal", "kind", "science_key", "result_sha256"}:
            raise RequestRejected("trusted history record shape mismatch")
        if type(item["ordinal"]) is not int or item["ordinal"] != ordinal:
            raise RequestRejected("trusted history ordinal mismatch")
        kind = item["kind"]
        science_key = item["science_key"]
        if type(kind) is not str or kind not in limits or not _digest(science_key) or not _digest(item["result_sha256"]):
            raise RequestRejected("trusted history identity mismatch")
        if science_key in history_by_key:
            raise RequestRejected("trusted history repeats a scientific key")
        history_by_key[science_key] = item
        actual_counts[kind] += 1
    if actual_counts != state["counts"]:
        raise RequestRejected("trusted counter/history mismatch")
    if any(not _digest(key) for key in state["science_cache"]) or set(state["science_cache"]) != set(history_by_key):
        raise RequestRejected("trusted science cache/history mismatch")
    for science_key, response in state["science_cache"].items():
        _response(response)
        if fingerprint(response) != history_by_key[science_key]["result_sha256"]:
            raise RequestRejected("trusted scientific response hash mismatch")
    for request_id, cached in state["request_cache"].items():
        if not _request_id(request_id) or type(cached) is not dict or set(cached) != {"exact_key", "science_key", "kind", "response"}:
            raise RequestRejected("trusted request cache record shape mismatch")
        science_key = cached["science_key"]
        kind = cached["kind"]
        if not _digest(cached["exact_key"]) or not _digest(science_key) or science_key not in history_by_key or type(kind) is not str or kind != history_by_key[science_key]["kind"]:
            raise RequestRejected("trusted request cache scientific reference mismatch")
        if _response(cached["response"]) != canonical_bytes(state["science_cache"][science_key]):
            raise RequestRejected("trusted request cache response mismatch")
    canonical_bytes(state)


def evaluate_request(
    request: dict[str, Any],
    state: dict[str, Any],
    *,
    task_digest: str,
    limits: dict[str, int],
    preflight: Callable[[str, Any], Any],
    evaluator: Callable[[str, Any], dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return (response, next_state); never mutate caller state in place.

    ``preflight`` returns a semantic query value without cosmetic request IDs.
    It must do all cheap format/allowlist checks and raise RequestRejected for
    ordinary rejection. ``evaluator`` is called only on a new, budgeted query;
    a returned scientific failure is charged, while an exception propagates
    so that the framework can roll back VerifyContext.
    """
    validate_state(state, task_digest, limits)
    if type(request) is not dict or set(request) != {"request_id", "kind", "payload"}:
        raise RequestRejected("request must have request_id, kind, payload")
    request_id = request["request_id"]
    kind = request["kind"]
    if not _request_id(request_id):
        raise RequestRejected("invalid request_id")
    if type(kind) is not str or kind not in limits:
        raise RequestRejected("unknown scientific query kind")
    exact_key = fingerprint(request)
    previous = state["request_cache"].get(request_id)
    if previous is not None:
        if previous["exact_key"] != exact_key or previous["kind"] != kind:
            raise RequestRejected("request_id reused with different request content")
        return _clone(previous["response"]), _clone(state)

    semantic_query = preflight(kind, _clone(request["payload"]))
    science_key = fingerprint({"kind": kind, "query": semantic_query})
    cached = state["science_cache"].get(science_key)
    if cached is not None:
        next_state = _clone(state)
        next_state["request_cache"][request_id] = {
            "exact_key": exact_key, "science_key": science_key, "kind": kind,
            "response": _clone(cached),
        }
        validate_state(next_state, task_digest, limits)
        return _clone(cached), next_state

    if state["counts"][kind] >= limits[kind]:
        raise RequestRejected("scientific query budget exhausted")
    query_bytes = canonical_bytes(semantic_query)
    evaluation_query = _clone(semantic_query)
    result = evaluator(kind, evaluation_query)
    if canonical_bytes(evaluation_query) != query_bytes:
        raise RuntimeError("scientific evaluator mutated its query")
    try:
        _response(result)
    except RequestRejected as exc:
        raise RuntimeError("scientific evaluator returned an invalid response") from exc
    next_state = _clone(state)
    next_state["counts"][kind] += 1
    next_state["science_cache"][science_key] = _clone(result)
    next_state["request_cache"][request_id] = {
        "exact_key": exact_key, "science_key": science_key, "kind": kind,
        "response": _clone(result),
    }
    next_state["history"].append({
        "ordinal": len(next_state["history"]) + 1,
        "kind": kind,
        "science_key": science_key,
        "result_sha256": fingerprint(result),
    })
    validate_state(next_state, task_digest, limits)
    return _clone(result), next_state
