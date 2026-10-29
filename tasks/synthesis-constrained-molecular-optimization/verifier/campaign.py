"""Trusted validation history and final constrained Top-10 AUC scoring."""

from __future__ import annotations

import csv
import io
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any

from molecular_contract import (
    Bundle,
    ConfigurationError,
    ContractError,
    IDENTIFIER_RE,
    RouteReplayError,
    canonical_json,
    evaluate_molecule,
    load_bundle,
    normalize_route,
    quantized,
    replay_route,
    route_fingerprint,
    sha256_json,
    standardize_smiles,
    strict_json_loads,
)


BENCHMARK_ID = "synthesis-constrained-molecular-optimization"
STATE_KEY = "synthesis_constrained_molecular_optimization_v1"
REQUEST_KEYS = {
    "schema_version",
    "request_id",
    "task_id",
    "candidate_smiles",
    "route",
    "repair_of_call",
}
STATE_KEYS = {"schema_version", "contract_sha256", "next_call_index", "tasks"}
TASK_STATE_KEYS = {
    "route_validation_used",
    "evaluation_used",
    "repair_used",
    "attempts",
}
ATTEMPT_KEYS = {"payload_sha256", "route", "evidence"}
EVIDENCE_KEYS = {
    "schema_version",
    "call_index",
    "request_id",
    "task_id",
    "route_validation_index",
    "evaluation_index",
    "candidate_smiles",
    "route_id",
    "route_fingerprint",
    "canonical_route",
    "status",
    "error_code",
    "hard_constraint_failures",
    "properties",
    "objectives",
    "utility",
    "repair_of_call",
}
CANDIDATE_COLUMNS = [
    "task_id",
    "rank",
    "canonical_smiles",
    "route_id",
    "predicted_qed",
    "predicted_logp",
    "predicted_tpsa",
    "predicted_utility",
]


class SubmissionError(ValueError):
    pass


def initial_state(bundle: Bundle) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "contract_sha256": bundle.contract_sha256,
        "next_call_index": 1,
        "tasks": {
            task_id: {
                "route_validation_used": 0,
                "evaluation_used": 0,
                "repair_used": 0,
                "attempts": [],
            }
            for task_id in bundle.task_order
        },
    }


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def route_family_fingerprint(route: dict[str, Any]) -> str:
    """Identify one deterministic synthesis plan, excluding declared products."""
    return sha256_json(
        {
            "task_id": route["task_id"],
            "starting_material_ids": route["starting_material_ids"],
            "reaction_template_ids": route["reaction_template_ids"],
        }
    )


def validate_state(value: Any, bundle: Bundle) -> dict[str, Any]:
    if value is None:
        return initial_state(bundle)
    if not isinstance(value, dict) or set(value) != STATE_KEYS:
        raise ConfigurationError("VerifyContext state has invalid top-level fields")
    if value["schema_version"] != "1.0" or value["contract_sha256"] != bundle.contract_sha256:
        raise ConfigurationError("VerifyContext state does not match the frozen task contract")
    if not _is_integer(value["next_call_index"]) or value["next_call_index"] < 1:
        raise ConfigurationError("VerifyContext next call index is invalid")
    tasks_state = value["tasks"]
    if not isinstance(tasks_state, dict) or set(tasks_state) != set(bundle.task_order):
        raise ConfigurationError("VerifyContext task identities are invalid")
    all_attempts = []
    request_ids = set()
    for task_id in bundle.task_order:
        task = bundle.tasks[task_id]
        state = tasks_state[task_id]
        if not isinstance(state, dict) or set(state) != TASK_STATE_KEYS:
            raise ConfigurationError("VerifyContext task state fields are invalid")
        for name in ("route_validation_used", "evaluation_used", "repair_used"):
            if not _is_integer(state[name]) or state[name] < 0:
                raise ConfigurationError("VerifyContext budget counter is invalid")
        attempts = state["attempts"]
        if not isinstance(attempts, list) or len(attempts) != state["route_validation_used"]:
            raise ConfigurationError("VerifyContext route history and counter disagree")
        if state["route_validation_used"] > task.route_validation_budget:
            raise ConfigurationError("VerifyContext exceeds the route-validation budget")
        if state["evaluation_used"] > task.evaluation_budget or state["repair_used"] > task.repair_budget:
            raise ConfigurationError("VerifyContext exceeds a scientific budget")
        evaluation_indices = []
        evaluated_candidates = set()
        route_fingerprints = set()
        route_ids: dict[str, str] = {}
        route_families = set()
        repairs = 0
        for route_index, attempt in enumerate(attempts, start=1):
            if not isinstance(attempt, dict) or set(attempt) != ATTEMPT_KEYS:
                raise ConfigurationError("VerifyContext attempt fields are invalid")
            if not isinstance(attempt["payload_sha256"], str) or not attempt["payload_sha256"].startswith("sha256:"):
                raise ConfigurationError("VerifyContext payload fingerprint is invalid")
            evidence = attempt["evidence"]
            if not isinstance(evidence, dict) or set(evidence) != EVIDENCE_KEYS:
                raise ConfigurationError("VerifyContext evidence fields are invalid")
            if evidence["schema_version"] != "1.0" or evidence["task_id"] != task_id:
                raise ConfigurationError("VerifyContext evidence identity is invalid")
            if evidence["route_validation_index"] != route_index:
                raise ConfigurationError("VerifyContext route indices are not consecutive")
            if not _is_integer(evidence["call_index"]) or evidence["call_index"] < 1:
                raise ConfigurationError("VerifyContext call index is invalid")
            if not isinstance(evidence["request_id"], str) or IDENTIFIER_RE.fullmatch(evidence["request_id"]) is None:
                raise ConfigurationError("VerifyContext request id is invalid")
            if evidence["request_id"] in request_ids:
                raise ConfigurationError("VerifyContext request ids repeat")
            request_ids.add(evidence["request_id"])
            if not isinstance(evidence["candidate_smiles"], str):
                raise ConfigurationError("VerifyContext candidate identity is invalid")
            route = attempt["route"]
            try:
                normalized_route = normalize_route(route, task, bundle)
            except ContractError as error:
                raise ConfigurationError("VerifyContext contains an invalid normalized route") from error
            if normalized_route != route or route_fingerprint(route) != evidence["route_fingerprint"]:
                raise ConfigurationError("VerifyContext route fingerprint is inconsistent")
            if evidence["canonical_route"] != route:
                raise ConfigurationError("VerifyContext evidence route is inconsistent")
            if evidence["route_id"] != route["route_id"]:
                raise ConfigurationError("VerifyContext evidence route id is inconsistent")
            try:
                molecule, canonical_candidate = standardize_smiles(
                    evidence["candidate_smiles"], "VerifyContext candidate"
                )
            except ContractError as error:
                raise ConfigurationError("VerifyContext candidate is invalid") from error
            if canonical_candidate != evidence["candidate_smiles"]:
                raise ConfigurationError("VerifyContext candidate is not canonical")
            if canonical_candidate != route["final_product"]:
                raise ConfigurationError("VerifyContext candidate and route final product disagree")
            expected_payload = {
                "schema_version": "1.0",
                "request_id": evidence["request_id"],
                "task_id": task_id,
                "candidate_smiles": canonical_candidate,
                "route": route,
                "repair_of_call": evidence["repair_of_call"],
            }
            if attempt["payload_sha256"] != sha256_json(expected_payload):
                raise ConfigurationError("VerifyContext payload fingerprint is inconsistent")
            fingerprint = evidence["route_fingerprint"]
            if fingerprint in route_fingerprints:
                raise ConfigurationError("VerifyContext repeats a route fingerprint")
            route_fingerprints.add(fingerprint)
            prior = route_ids.setdefault(route["route_id"], fingerprint)
            if prior != fingerprint:
                raise ConfigurationError("VerifyContext reuses a route id")
            evaluation_index = evidence["evaluation_index"]
            if evaluation_index is not None:
                if not _is_integer(evaluation_index):
                    raise ConfigurationError("VerifyContext evaluation index is invalid")
                evaluation_indices.append(evaluation_index)
                if evidence["candidate_smiles"] in evaluated_candidates:
                    raise ConfigurationError("VerifyContext repeats an evaluated molecule")
                evaluated_candidates.add(evidence["candidate_smiles"])
                if evidence["status"] != "evaluated" or evidence["error_code"] is not None:
                    raise ConfigurationError("VerifyContext evaluated status is inconsistent")
                if not isinstance(evidence["properties"], dict) or not isinstance(evidence["objectives"], list):
                    raise ConfigurationError("VerifyContext evaluated data is missing")
                if not isinstance(evidence["hard_constraint_failures"], list) or not _finite(evidence["utility"]):
                    raise ConfigurationError("VerifyContext evaluated values are invalid")
                if not 0 <= float(evidence["utility"]) <= 1:
                    raise ConfigurationError("VerifyContext utility is outside [0,1]")
                try:
                    replay_route(route, canonical_candidate, task, bundle)
                except RouteReplayError as error:
                    raise ConfigurationError("VerifyContext evaluated route no longer replays") from error
                calculated = evaluate_molecule(molecule, canonical_candidate, task)
                for field in ("properties", "objectives", "hard_constraint_failures", "utility"):
                    if evidence[field] != calculated[field]:
                        raise ConfigurationError("VerifyContext evaluated chemistry is inconsistent")
            else:
                if evidence["status"] != "route_rejected" or not isinstance(evidence["error_code"], str):
                    raise ConfigurationError("VerifyContext route rejection is inconsistent")
                if evidence["properties"] is not None or evidence["objectives"] is not None or evidence["utility"] != 0.0:
                    raise ConfigurationError("VerifyContext rejected route contains evaluated data")
                if evidence["hard_constraint_failures"] != []:
                    raise ConfigurationError("VerifyContext rejected route has hard-constraint data")
                try:
                    replay_route(route, canonical_candidate, task, bundle)
                except RouteReplayError as error:
                    if evidence["error_code"] != error.code:
                        raise ConfigurationError("VerifyContext route rejection code is inconsistent") from error
                else:
                    raise ConfigurationError("VerifyContext rejected route now replays successfully")
            repair = evidence["repair_of_call"]
            if repair is not None:
                if not _is_integer(repair) or repair >= evidence["call_index"]:
                    raise ConfigurationError("VerifyContext repair reference is invalid")
                repairs += 1
            family = route_family_fingerprint(route)
            if (family in route_families) != (repair is not None):
                raise ConfigurationError("VerifyContext route-family repair history is inconsistent")
            route_families.add(family)
            all_attempts.append((evidence["call_index"], task_id, attempt))
        if evaluation_indices != list(range(1, len(evaluation_indices) + 1)):
            raise ConfigurationError("VerifyContext evaluation indices are not consecutive")
        if len(evaluation_indices) != state["evaluation_used"] or repairs != state["repair_used"]:
            raise ConfigurationError("VerifyContext counters do not match history")
    all_attempts.sort(key=lambda item: item[0])
    if [item[0] for item in all_attempts] != list(range(1, value["next_call_index"])):
        raise ConfigurationError("VerifyContext global call indices are not consecutive")
    by_call = {item[0]: (item[1], item[2]) for item in all_attempts}
    for _, task_id, attempt in all_attempts:
        repair = attempt["evidence"]["repair_of_call"]
        if repair is not None:
            referenced = by_call.get(repair)
            if (
                referenced is None
                or referenced[0] != task_id
                or referenced[1]["evidence"]["status"] != "route_rejected"
                or route_family_fingerprint(referenced[1]["route"])
                != route_family_fingerprint(attempt["route"])
            ):
                raise ConfigurationError("VerifyContext repair target is inconsistent")
    return value


def _budget(task_id: str, state: dict[str, Any], bundle: Bundle) -> dict[str, int]:
    task = bundle.tasks[task_id]
    selected = state["tasks"][task_id]
    return {
        "evaluation_used": selected["evaluation_used"],
        "evaluation_limit": task.evaluation_budget,
        "evaluation_remaining": task.evaluation_budget - selected["evaluation_used"],
        "route_validation_used": selected["route_validation_used"],
        "route_validation_limit": task.route_validation_budget,
        "route_validation_remaining": task.route_validation_budget - selected["route_validation_used"],
        "repair_used": selected["repair_used"],
        "repair_limit": task.repair_budget,
        "repair_remaining": task.repair_budget - selected["repair_used"],
    }


def _response(
    request_id: str | None,
    task_id: str | None,
    status: str,
    state: dict[str, Any],
    bundle: Bundle,
    *,
    canonical_smiles: str | None = None,
    fingerprint: str | None = None,
    evidence: dict[str, Any] | None = None,
    replayed: bool = False,
    route_charged: bool = False,
    evaluation_charged: bool = False,
    repair_charged: bool = False,
    error_code: str | None = None,
    message: str | None = None,
) -> dict[str, Any]:
    candidate = None
    if canonical_smiles is not None:
        candidate = {
            "canonical_smiles": canonical_smiles,
            "route_fingerprint": fingerprint,
            "route_valid": bool(evidence and evidence["evaluation_index"] is not None),
            "hard_constraints_passed": bool(
                evidence
                and evidence["evaluation_index"] is not None
                and not evidence["hard_constraint_failures"]
            ),
            "hard_constraint_failures": evidence["hard_constraint_failures"] if evidence else [],
        }
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "task_id": task_id,
        "status": status,
        "replayed": replayed,
        "charged": {
            "route_validation": route_charged,
            "evaluation": evaluation_charged,
            "repair": repair_charged,
        },
        "budget": _budget(task_id, state, bundle) if task_id in bundle.tasks else None,
        "candidate": candidate,
        "evaluation": evidence,
        "error": None if error_code is None else {"code": error_code, "message": message or "request rejected"},
    }


def _normal_request(request: Any, bundle: Bundle) -> dict[str, Any]:
    if not isinstance(request, dict) or set(request) != REQUEST_KEYS:
        raise ContractError("INVALID_SCHEMA", "request must contain exactly the documented fields")
    if request["schema_version"] != "1.0":
        raise ContractError("INVALID_SCHEMA", "request schema_version must be 1.0")
    request_id = request["request_id"]
    task_id = request["task_id"]
    if not isinstance(request_id, str) or IDENTIFIER_RE.fullmatch(request_id) is None:
        raise ContractError("INVALID_REQUEST_ID", "request_id is invalid")
    if not isinstance(task_id, str) or task_id not in bundle.tasks:
        raise ContractError("UNKNOWN_TASK", "task_id is unknown")
    task = bundle.tasks[task_id]
    molecule, canonical_smiles = standardize_smiles(request["candidate_smiles"], "candidate_smiles")
    route = normalize_route(request["route"], task, bundle)
    if route["final_product"] != canonical_smiles:
        raise ContractError(
            "CANDIDATE_ROUTE_MISMATCH",
            "candidate_smiles must equal the declared canonical route final_product",
        )
    repair = request["repair_of_call"]
    if repair is not None and (not _is_integer(repair) or repair < 1):
        raise ContractError("INVALID_REPAIR_REFERENCE", "repair_of_call must be null or a positive integer")
    normalized = {
        "schema_version": "1.0",
        "request_id": request_id,
        "task_id": task_id,
        "candidate_smiles": canonical_smiles,
        "route": route,
        "repair_of_call": repair,
    }
    normalized["molecule"] = molecule
    return normalized


def _no_charge_status(error_code: str) -> str:
    if error_code == "UNKNOWN_TASK":
        return "invalid_request"
    if error_code in {"INVALID_SMILES", "MULTI_FRAGMENT", "UNSUPPORTED_ATOM", "STANDARDIZATION_FAILED"}:
        return "candidate_rejected"
    if error_code.endswith("BUDGET_EXHAUSTED"):
        return "budget_exhausted"
    if error_code == "REQUEST_ID_CONFLICT":
        return "request_id_conflict"
    return "invalid_request"


def process_validation(
    request: Any,
    input_root: Path,
    current_state: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    bundle = load_bundle(input_root)
    state = validate_state(current_state, bundle)
    hinted_request_id = request.get("request_id") if isinstance(request, dict) and isinstance(request.get("request_id"), str) else None
    hinted_task = request.get("task_id") if isinstance(request, dict) and isinstance(request.get("task_id"), str) else None
    try:
        normalized = _normal_request(request, bundle)
    except ContractError as error:
        return _response(
            hinted_request_id,
            hinted_task,
            _no_charge_status(error.code),
            state,
            bundle,
            error_code=error.code,
            message=str(error),
        ), None
    task_id = normalized["task_id"]
    task = bundle.tasks[task_id]
    task_state = state["tasks"][task_id]
    route = normalized["route"]
    fingerprint = route_fingerprint(route)
    payload_for_hash = {key: value for key, value in normalized.items() if key != "molecule"}
    payload_hash = sha256_json(payload_for_hash)
    all_attempts = [
        attempt
        for selected_task in bundle.task_order
        for attempt in state["tasks"][selected_task]["attempts"]
    ]
    for attempt in all_attempts:
        evidence = attempt["evidence"]
        if evidence["request_id"] == normalized["request_id"]:
            if attempt["payload_sha256"] != payload_hash:
                return _response(
                    normalized["request_id"], task_id, "request_id_conflict", state, bundle,
                    canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
                    error_code="REQUEST_ID_CONFLICT", message="request_id was already used for a different payload",
                ), None
            return _response(
                normalized["request_id"], task_id, "cached_evaluation", state, bundle,
                canonical_smiles=evidence["candidate_smiles"], fingerprint=evidence["route_fingerprint"],
                evidence=evidence, replayed=True,
            ), None
    for attempt in task_state["attempts"]:
        evidence = attempt["evidence"]
        if evidence["route_fingerprint"] == fingerprint:
            if evidence["candidate_smiles"] == normalized["candidate_smiles"]:
                return _response(
                    normalized["request_id"], task_id, "cached_evaluation", state, bundle,
                    canonical_smiles=evidence["candidate_smiles"], fingerprint=fingerprint,
                    evidence=evidence, replayed=True,
                ), None
            return _response(
                normalized["request_id"], task_id, "invalid_request", state, bundle,
                canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
                error_code="CANDIDATE_ROUTE_MISMATCH",
                message="candidate_smiles conflicts with a previously replayed identical route",
            ), None
        if route["route_id"] == attempt["route"]["route_id"] and fingerprint != evidence["route_fingerprint"]:
            return _response(
                normalized["request_id"], task_id, "invalid_request", state, bundle,
                canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
                error_code="ROUTE_ID_CONFLICT", message="route_id was already used for a different route",
            ), None
        if evidence["evaluation_index"] is not None and evidence["candidate_smiles"] == normalized["candidate_smiles"]:
            return _response(
                normalized["request_id"], task_id, "candidate_rejected", state, bundle,
                canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
                error_code="DUPLICATE_CANDIDATE", message="canonical candidate was already evaluated",
            ), None
    repair = normalized["repair_of_call"]
    family = route_family_fingerprint(route)
    family_attempts = [
        attempt
        for attempt in task_state["attempts"]
        if route_family_fingerprint(attempt["route"]) == family
    ]
    if repair is not None:
        referenced = [
            attempt for attempt in task_state["attempts"] if attempt["evidence"]["call_index"] == repair
        ]
        if (
            len(referenced) != 1
            or referenced[0]["evidence"]["status"] != "route_rejected"
            or route_family_fingerprint(referenced[0]["route"]) != family
        ):
            return _response(
                normalized["request_id"], task_id, "invalid_request", state, bundle,
                canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
                error_code="INVALID_REPAIR_REFERENCE",
                message="repair_of_call must reference an earlier rejected route in the same synthesis plan",
            ), None
        if task_state["repair_used"] >= task.repair_budget:
            return _response(
                normalized["request_id"], task_id, "budget_exhausted", state, bundle,
                canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
                error_code="REPAIR_BUDGET_EXHAUSTED", message="no repair budget remains",
            ), None
    elif family_attempts:
        return _response(
            normalized["request_id"], task_id, "invalid_request", state, bundle,
            canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
            error_code="REPAIR_REFERENCE_REQUIRED",
            message="a changed declaration for a previously rejected synthesis plan requires repair_of_call",
        ), None
    if task_state["route_validation_used"] >= task.route_validation_budget:
        return _response(
            normalized["request_id"], task_id, "budget_exhausted", state, bundle,
            canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
            error_code="ROUTE_BUDGET_EXHAUSTED", message="no route-validation budget remains",
        ), None
    if task_state["evaluation_used"] >= task.evaluation_budget:
        return _response(
            normalized["request_id"], task_id, "budget_exhausted", state, bundle,
            canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
            error_code="EVALUATION_BUDGET_EXHAUSTED", message="no property-evaluation budget remains",
        ), None

    next_state = json.loads(canonical_json(state))
    next_task_state = next_state["tasks"][task_id]
    call_index = next_state["next_call_index"]
    route_index = next_task_state["route_validation_used"] + 1
    next_state["next_call_index"] += 1
    next_task_state["route_validation_used"] += 1
    if repair is not None:
        next_task_state["repair_used"] += 1
    try:
        replay_route(route, normalized["candidate_smiles"], task, bundle)
    except RouteReplayError as error:
        evidence = {
            "schema_version": "1.0",
            "call_index": call_index,
            "request_id": normalized["request_id"],
            "task_id": task_id,
            "route_validation_index": route_index,
            "evaluation_index": None,
            "candidate_smiles": normalized["candidate_smiles"],
            "route_id": route["route_id"],
            "route_fingerprint": fingerprint,
            "canonical_route": route,
            "status": "route_rejected",
            "error_code": error.code,
            "hard_constraint_failures": [],
            "properties": None,
            "objectives": None,
            "utility": 0.0,
            "repair_of_call": repair,
        }
        next_task_state["attempts"].append(
            {"payload_sha256": payload_hash, "route": route, "evidence": evidence}
        )
        validate_state(next_state, bundle)
        return _response(
            normalized["request_id"], task_id, "route_rejected", next_state, bundle,
            canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
            evidence=evidence, route_charged=True, repair_charged=repair is not None,
            error_code=error.code, message=str(error),
        ), next_state

    evaluation = evaluate_molecule(
        normalized["molecule"], normalized["candidate_smiles"], task
    )
    evaluation_index = next_task_state["evaluation_used"] + 1
    next_task_state["evaluation_used"] += 1
    evidence = {
        "schema_version": "1.0",
        "call_index": call_index,
        "request_id": normalized["request_id"],
        "task_id": task_id,
        "route_validation_index": route_index,
        "evaluation_index": evaluation_index,
        "candidate_smiles": normalized["candidate_smiles"],
        "route_id": route["route_id"],
        "route_fingerprint": fingerprint,
        "canonical_route": route,
        "status": "evaluated",
        "error_code": None,
        "hard_constraint_failures": evaluation["hard_constraint_failures"],
        "properties": evaluation["properties"],
        "objectives": evaluation["objectives"],
        "utility": evaluation["utility"],
        "repair_of_call": repair,
    }
    next_task_state["attempts"].append(
        {"payload_sha256": payload_hash, "route": route, "evidence": evidence}
    )
    validate_state(next_state, bundle)
    return _response(
        normalized["request_id"], task_id, "evaluated", next_state, bundle,
        canonical_smiles=normalized["candidate_smiles"], fingerprint=fingerprint,
        evidence=evidence, route_charged=True, evaluation_charged=True,
        repair_charged=repair is not None,
    ), next_state


def public_evidence_records(state: dict[str, Any], bundle: Bundle) -> list[dict[str, Any]]:
    records = [
        attempt["evidence"]
        for task_id in bundle.task_order
        for attempt in state["tasks"][task_id]["attempts"]
    ]
    return sorted(records, key=lambda item: item["call_index"])


def _read_submission_text(root: Path, name: str, maximum: int) -> str:
    path = root / name
    try:
        info = path.lstat()
    except OSError as error:
        raise SubmissionError("missing required artifact: %s" % name) from error
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise SubmissionError("artifact must be a regular non-symlink file: %s" % name)
    if not 0 < info.st_size <= maximum:
        raise SubmissionError("artifact byte size is invalid: %s" % name)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise SubmissionError("artifact must be UTF-8 text: %s" % name) from error
    if text.startswith("\ufeff") or "\x00" in text:
        raise SubmissionError("artifact contains a BOM or NUL: %s" % name)
    return text


def _read_candidates(root: Path, bundle: Bundle) -> list[dict[str, Any]]:
    text = _read_submission_text(root, "candidates.csv", 65_536)
    if "\r" in text or any(not line for line in text.split("\n")[:-1]):
        raise SubmissionError("candidates.csv must use LF lines without blank records")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if reader.fieldnames != CANDIDATE_COLUMNS:
            raise SubmissionError("candidates.csv header is invalid")
        raw_rows = list(reader)
    except csv.Error as error:
        raise SubmissionError("candidates.csv is malformed") from error
    if not bundle.task_order or not len(bundle.task_order) <= len(raw_rows) <= 10 * len(bundle.task_order):
        raise SubmissionError("candidates.csv has an invalid row count")
    output = []
    seen_smiles = set()
    seen_routes = set()
    per_task: dict[str, list[int]] = {task_id: [] for task_id in bundle.task_order}
    for index, row in enumerate(raw_rows, start=1):
        if None in row or any(value is None or value.strip() == "" for value in row.values()):
            raise SubmissionError("candidate row %d contains an empty or extra field" % index)
        task_id = row["task_id"].strip()
        if task_id not in bundle.tasks:
            raise SubmissionError("candidate row %d has an unknown task_id" % index)
        rank_text = row["rank"].strip()
        if re.fullmatch(r"(?:[1-9]|10)", rank_text) is None:
            raise SubmissionError("candidate rank must be a base-10 integer from 1 through 10")
        rank = int(rank_text)
        try:
            _, canonical = standardize_smiles(row["canonical_smiles"].strip(), "canonical_smiles")
        except ContractError as error:
            raise SubmissionError(str(error)) from error
        if canonical != row["canonical_smiles"].strip():
            raise SubmissionError("candidate SMILES is not in the frozen canonical form")
        route_id = row["route_id"].strip()
        if IDENTIFIER_RE.fullmatch(route_id) is None:
            raise SubmissionError("candidate route_id is invalid")
        route_key = (task_id, route_id)
        if canonical in seen_smiles or route_key in seen_routes:
            raise SubmissionError("candidate molecule or route reference repeats")
        predictions = {}
        for field in CANDIDATE_COLUMNS[4:]:
            try:
                number = float(row[field])
            except ValueError as error:
                raise SubmissionError("candidate predictions must be numeric") from error
            if not math.isfinite(number) or abs(number) > 1_000_000:
                raise SubmissionError("candidate predictions must be finite and bounded")
            predictions[field] = number
        seen_smiles.add(canonical)
        seen_routes.add(route_key)
        per_task[task_id].append(rank)
        output.append(
            {
                "task_id": task_id,
                "rank": rank,
                "canonical_smiles": canonical,
                "route_id": route_id,
                **predictions,
            }
        )
    for task_id, ranks in per_task.items():
        if ranks != list(range(1, len(ranks) + 1)) or not ranks:
            raise SubmissionError("candidate rows must be grouped by task with consecutive ranks")
    expected_task_sequence = [
        task_id
        for task_id in bundle.task_order
        for _ in per_task[task_id]
    ]
    if [item["task_id"] for item in output] != expected_task_sequence:
        raise SubmissionError("candidate rows must be grouped in the frozen task order")
    return output


def _read_routes(root: Path, bundle: Bundle) -> dict[tuple[str, str], dict[str, Any]]:
    text = _read_submission_text(root, "routes.json", 1_000_000)
    try:
        document = strict_json_loads(text, "routes.json")
    except ContractError as error:
        raise SubmissionError(str(error)) from error
    if not isinstance(document, dict) or set(document) != {"schema_version", "benchmark_id", "routes"}:
        raise SubmissionError("routes.json fields are invalid")
    if document["schema_version"] != "1.0" or document["benchmark_id"] != BENCHMARK_ID:
        raise SubmissionError("routes.json identity is invalid")
    if not isinstance(document["routes"], list) or len(document["routes"]) > 10 * len(bundle.task_order):
        raise SubmissionError("routes.json route count is invalid")
    output = {}
    for index, raw in enumerate(document["routes"], start=1):
        if not isinstance(raw, dict) or not isinstance(raw.get("task_id"), str) or raw["task_id"] not in bundle.tasks:
            raise SubmissionError("route %d has an unknown task" % index)
        try:
            normalized = normalize_route(raw, bundle.tasks[raw["task_id"]], bundle)
        except ContractError as error:
            raise SubmissionError("route %d: %s" % (index, error)) from error
        if raw != normalized:
            raise SubmissionError("route %d is not in canonical form" % index)
        key = (normalized["task_id"], normalized["route_id"])
        if key in output:
            raise SubmissionError("routes.json repeats a route id within one task")
        output[key] = normalized
    return output


def _read_evidence(root: Path, bundle: Bundle) -> list[dict[str, Any]]:
    text = _read_submission_text(root, "evidence.jsonl", 524_288)
    lines = text.splitlines()
    if not lines or len(lines) > sum(bundle.tasks[item].route_validation_budget for item in bundle.task_order):
        raise SubmissionError("evidence.jsonl has an invalid line count")
    output = []
    for index, line in enumerate(lines, start=1):
        if not line or len(line.encode("utf-8")) > 16_384:
            raise SubmissionError("evidence line size is invalid")
        try:
            value = strict_json_loads(line, "evidence line %d" % index)
        except ContractError as error:
            raise SubmissionError(str(error)) from error
        if not isinstance(value, dict) or set(value) != EVIDENCE_KEYS:
            raise SubmissionError("evidence line %d fields are invalid" % index)
        output.append(value)
    return output


def _recompute_histories(
    state: dict[str, Any], bundle: Bundle
) -> dict[str, list[dict[str, Any]]]:
    histories: dict[str, list[dict[str, Any]]] = {task_id: [] for task_id in bundle.task_order}
    for task_id in bundle.task_order:
        task = bundle.tasks[task_id]
        for attempt in state["tasks"][task_id]["attempts"]:
            evidence = attempt["evidence"]
            try:
                replay_route(attempt["route"], evidence["candidate_smiles"], task, bundle)
            except RouteReplayError as error:
                if evidence["evaluation_index"] is not None or error.code != evidence["error_code"]:
                    raise ConfigurationError("trusted route history no longer replays deterministically") from error
                continue
            if evidence["evaluation_index"] is None:
                raise ConfigurationError("trusted rejected route now replays successfully")
            molecule, canonical = standardize_smiles(evidence["candidate_smiles"], "trusted candidate")
            calculated = evaluate_molecule(molecule, canonical, task)
            for field in ("properties", "objectives", "hard_constraint_failures", "utility"):
                if calculated[field] != evidence[field]:
                    raise ConfigurationError("trusted scientific history does not recompute exactly")
            histories[task_id].append(
                {
                    "evaluation_index": evidence["evaluation_index"],
                    "call_index": evidence["call_index"],
                    "canonical_smiles": canonical,
                    "route_id": evidence["route_id"],
                    "route_fingerprint": evidence["route_fingerprint"],
                    "route": attempt["route"],
                    "properties": calculated["properties"],
                    "hard_constraint_failures": calculated["hard_constraint_failures"],
                    "utility": calculated["utility"],
                }
            )
    return histories


def _task_auc(history: list[dict[str, Any]], budget: int, top_k: int) -> tuple[float, list[float]]:
    utilities = [float(item["utility"]) for item in history]
    curve = []
    for checkpoint in range(0, budget + 1):
        observed = utilities[: min(checkpoint, len(utilities))]
        selected = sorted(observed, reverse=True)[:top_k]
        curve.append(sum(selected) / top_k)
    area = sum(
        (curve[index - 1] + curve[index]) / 2.0
        for index in range(1, len(curve))
    ) / budget
    ideal_curve = [min(checkpoint, top_k) / top_k for checkpoint in range(budget + 1)]
    ideal_area = sum(
        (ideal_curve[index - 1] + ideal_curve[index]) / 2.0
        for index in range(1, len(ideal_curve))
    ) / budget
    if ideal_area <= 0:
        raise ConfigurationError("the normalized AUC ceiling is invalid")
    normalized_area = min(1.0, max(0.0, area / ideal_area))
    return quantized(normalized_area, 12), [quantized(value, 12) for value in curve]


def _top_candidates(history: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    return sorted(
        history,
        key=lambda item: (-float(item["utility"]), item["canonical_smiles"], item["call_index"]),
    )[:top_k]


def score_submission(
    input_root: Path,
    submission_root: Path,
    current_state: Any,
) -> dict[str, Any]:
    bundle = load_bundle(input_root)
    state = validate_state(current_state, bundle)
    if any(state["tasks"][task_id]["evaluation_used"] < 1 for task_id in bundle.task_order):
        raise SubmissionError("final scoring requires at least one property evaluation in every task")
    try:
        root_info = submission_root.lstat()
    except OSError as error:
        raise SubmissionError("submission root is missing") from error
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise SubmissionError("submission root must be a real directory")
    expected_artifacts = {"candidates.csv", "routes.json", "evidence.jsonl"}
    try:
        artifact_names = []
        with os.scandir(submission_root) as entries:
            for entry in entries:
                artifact_names.append(entry.name)
                if len(artifact_names) > len(expected_artifacts):
                    raise SubmissionError("submission root contains an unexpected artifact")
    except OSError as error:
        raise SubmissionError("submission root cannot be enumerated") from error
    if len(artifact_names) != len(expected_artifacts) or set(artifact_names) != expected_artifacts:
        raise SubmissionError("submission root must contain exactly the three declared artifacts")
    candidates = _read_candidates(submission_root, bundle)
    routes = _read_routes(submission_root, bundle)
    evidence = _read_evidence(submission_root, bundle)
    expected_evidence = public_evidence_records(state, bundle)
    if canonical_json(evidence) != canonical_json(expected_evidence):
        raise SubmissionError("evidence.jsonl must exactly materialize the trusted charged-call history")
    histories = _recompute_histories(state, bundle)
    candidate_keys = {(item["task_id"], item["route_id"]) for item in candidates}
    if set(routes) != candidate_keys:
        raise SubmissionError("routes.json must contain exactly one route for each candidate")
    rows_by_task = {
        task_id: [item for item in candidates if item["task_id"] == task_id]
        for task_id in bundle.task_order
    }
    for task_id in bundle.task_order:
        task = bundle.tasks[task_id]
        expected = _top_candidates(histories[task_id], task.top_k)
        submitted = rows_by_task[task_id]
        if not expected or len(submitted) != len(expected):
            raise SubmissionError("candidates.csv must materialize each task's evaluated Top-10")
        for rank, (row, reference) in enumerate(zip(submitted, expected), start=1):
            if row["rank"] != rank or row["canonical_smiles"] != reference["canonical_smiles"] or row["route_id"] != reference["route_id"]:
                raise SubmissionError("candidate ranking does not equal the verifier-recomputed Top-10")
            route = routes[(task_id, row["route_id"])]
            if route != reference["route"] or route_fingerprint(route) != reference["route_fingerprint"]:
                raise SubmissionError("submitted route differs from the trusted evaluated route")
            try:
                replay_route(route, row["canonical_smiles"], task, bundle)
            except RouteReplayError as error:
                raise SubmissionError("submitted route does not replay") from error
    aucs = []
    curves = {}
    final_means = []
    hard_passes = 0
    evaluation_total = 0
    route_total = 0
    for task_id in bundle.task_order:
        task = bundle.tasks[task_id]
        history = histories[task_id]
        auc, curve = _task_auc(history, task.evaluation_budget, task.top_k)
        aucs.append(auc)
        curves[task_id] = curve
        selected = _top_candidates(history, task.top_k)
        final_means.append(sum(float(item["utility"]) for item in selected) / task.top_k)
        hard_passes += sum(not item["hard_constraint_failures"] for item in history)
        evaluation_total += len(history)
        route_total += state["tasks"][task_id]["route_validation_used"]
    raw_score = quantized(sum(aucs) / len(aucs), 8)
    final_top10 = quantized(sum(final_means) / len(final_means), 8)
    route_success_rate = quantized(evaluation_total / route_total if route_total else 0.0, 8)
    hard_pass_rate = quantized(hard_passes / evaluation_total if evaluation_total else 0.0, 8)
    return {
        "schema_version": "1.0",
        "task_id": BENCHMARK_ID,
        "valid": True,
        "status": "valid",
        "primary_metric": {
            "name": "macro_constrained_top10_auc",
            "value": raw_score,
            "direction": "maximize",
        },
        "metrics": [
            {"name": "macro_constrained_top10_auc", "value": raw_score, "unit": None},
            {"name": "macro_final_top10_utility", "value": final_top10, "unit": None},
            {"name": "route_replay_success_rate", "value": route_success_rate, "unit": None},
            {"name": "hard_constraint_pass_rate", "value": hard_pass_rate, "unit": None},
            {"name": "charged_property_evaluations", "value": float(evaluation_total), "unit": "count"},
            {"name": "charged_route_validations", "value": float(route_total), "unit": "count"},
        ],
        "error": None,
        "diagnostics": {
            "per_task_auc": dict(zip(bundle.task_order, aucs)),
            "per_task_curve": curves,
            "candidate_count": len(candidates),
            "evidence_count": len(evidence),
        },
    }


def public_result(details: dict[str, Any]) -> dict[str, Any]:
    return {
        key: details[key]
        for key in (
            "schema_version",
            "task_id",
            "valid",
            "status",
            "primary_metric",
            "metrics",
            "error",
        )
    }


def invalid_result(message: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "task_id": BENCHMARK_ID,
        "valid": False,
        "status": "invalid",
        "primary_metric": {
            "name": "macro_constrained_top10_auc",
            "value": -1.0,
            "direction": "maximize",
        },
        "metrics": [
            {"name": "macro_constrained_top10_auc", "value": -1.0, "unit": None}
        ],
        "error": message,
    }
