"""Stateful validation campaign for pocket-conditioned ligand design.

The framework-owned :class:`VerifyContext` is the only persistence boundary.
This module never trusts submitter-supplied scores or route outcomes: it
standardizes molecules, replays the frozen reaction graph, and runs the frozen
CPU docking protocol itself.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import stat
from pathlib import Path
from typing import Any, Mapping

from rdkit import Chem

from chemistry_contract import (
    ContractError,
    ReactionTemplate,
    RouteReplayError,
    StartingMaterial,
    compute_properties,
    load_reaction_templates,
    load_starting_materials,
    replay_route,
    standardize_smiles,
)
from docking_contract import DockingError, run_frozen_docking


TASK_ID = "pocket-conditioned-synthesizable-ligand-design"
STATE_KEY = "pocket_conditioned_synthesizable_ligand_design"
SCHEMA_VERSION = "1.0"
REQUEST_TYPES = (
    "property_evaluation",
    "route_validation",
    "docking_evaluation",
)
EXPECTED_TARGET_IDS = ("trypsin_3ptb", "abl1_1iep", "brd4_3mxf")
REQUEST_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
CANDIDATE_KEY_RE = re.compile(r"cand_[a-z0-9_]{1,48}\Z")
SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}\Z")
RAW_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MAX_REQUEST_BYTES = 65_536

COMMON_REQUEST_KEYS = frozenset(
    {"schema_version", "request_id", "request_type", "target_id", "candidate_smiles"}
)
REQUEST_KEYS = {
    "property_evaluation": COMMON_REQUEST_KEYS,
    "route_validation": COMMON_REQUEST_KEYS | {"route"},
    "docking_evaluation": COMMON_REQUEST_KEYS
    | {"property_evaluation_id", "route_evaluation_id"},
}
ROUTE_KEYS = frozenset(
    {
        "schema_version",
        "route_id",
        "target_id",
        "candidate_key",
        "steps",
        "final_product_smiles",
    }
)
STEP_KEYS = frozenset(
    {"step_id", "reaction_template_id", "reactants", "product_smiles"}
)
CHARGED_RESPONSE_KEYS = frozenset(
    {
        "schema_version",
        "status",
        "charged",
        "evaluation_id",
        "request_id",
        "request_type",
        "target_id",
        "candidate_evaluation_smiles",
        "candidate_identity",
        "result",
        "budgets",
        "response_sha256",
    }
)
PROPERTY_RESULT_KEYS = frozenset({"passed", "violations", "properties"})
PROPERTY_KEYS = frozenset(
    {
        "molecular_weight",
        "clogp",
        "tpsa",
        "qed",
        "fraction_csp3",
        "hbd",
        "hba",
        "rotatable_bonds",
        "aromatic_rings",
        "heavy_atoms",
        "formal_charge",
    }
)
INTEGER_PROPERTY_KEYS = frozenset(
    {
        "hbd",
        "hba",
        "rotatable_bonds",
        "aromatic_rings",
        "heavy_atoms",
        "formal_charge",
    }
)
ROUTE_ACCEPTED_RESULT_KEYS = frozenset(
    {
        "passed",
        "route_id",
        "candidate_key",
        "final_product_smiles",
        "route_fingerprint",
        "step_products",
    }
)
FAILURE_RESULT_KEYS = frozenset({"passed", "error_code", "error"})
DOCKING_ACCEPTED_RESULT_KEYS = frozenset(
    {
        "passed",
        "property_evaluation_id",
        "route_evaluation_id",
        "route_fingerprint",
        "candidate_key",
        "pose_id",
        "pose_sdf",
        "pose_sha256",
        "vina_affinity_kcal_mol",
        "all_mode_affinities_kcal_mol",
        "selected_mode_index",
        "interaction_fingerprint",
        "interaction_evidence",
        "conformer_seed",
    }
)
INTERACTION_EVIDENCE_KEYS = frozenset(
    {
        "all_satisfied",
        "groups",
        "fingerprint",
        "conformer_seed",
        "force_field",
        "optimization_status",
        "selected_mode_index",
    }
)


class CampaignRequestError(ValueError):
    """A request failed before a scientific budget unit was started."""


def _canonical_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
    except (TypeError, ValueError) as error:
        raise CampaignRequestError("request is not finite canonical JSON") from error


def _sha256(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _response_hash(value: Mapping[str, Any]) -> str:
    unsigned = {key: item for key, item in value.items() if key != "response_sha256"}
    return _sha256(unsigned)


def _seal_response(value: dict[str, Any]) -> dict[str, Any]:
    if "response_sha256" in value:
        raise RuntimeError("response was already sealed")
    value["response_sha256"] = _response_hash(value)
    return value


def _identifier(value: Any, label: str) -> str:
    if not isinstance(value, str) or REQUEST_ID_RE.fullmatch(value) is None:
        raise CampaignRequestError("%s is not a valid identifier" % label)
    return value


def _exact_object(value: Any, fields: frozenset[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != fields:
        raise CampaignRequestError("%s must contain exactly the documented fields" % label)
    return value


def _load_json_line(text: str, label: str) -> Any:
    def reject_constant(value: str) -> None:
        raise ValueError("non-finite constant: %s" % value)

    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        return json.loads(
            text,
            parse_constant=reject_constant,
            object_pairs_hook=no_duplicates,
        )
    except (ValueError, RecursionError) as error:
        raise RuntimeError("%s is not strict JSON" % label) from error


def _safe_regular_file(path: Path, maximum: int) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise RuntimeError("trusted input file is missing: %s" % path.name) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError("trusted input path is unsafe: %s" % path.name)
    if metadata.st_size < 1 or metadata.st_size > maximum:
        raise RuntimeError("trusted input file has invalid size: %s" % path.name)
    try:
        return path.read_bytes()
    except OSError as error:
        raise RuntimeError("trusted input file cannot be read: %s" % path.name) from error


def load_targets(input_root: Path) -> dict[str, dict[str, Any]]:
    """Load the three public target contracts in their frozen file order."""

    payload = _safe_regular_file(input_root / "targets.jsonl", 1_000_000)
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise RuntimeError("targets.jsonl is not UTF-8") from error
    if text.startswith("\ufeff") or "\x00" in text:
        raise RuntimeError("targets.jsonl contains forbidden text encoding markers")
    rows = [line for line in text.splitlines() if line.strip()]
    if len(rows) != len(EXPECTED_TARGET_IDS):
        raise RuntimeError("targets.jsonl must contain exactly three targets")
    targets: dict[str, dict[str, Any]] = {}
    for index, line in enumerate(rows, start=1):
        value = _load_json_line(line, "targets.jsonl line %d" % index)
        if not isinstance(value, dict):
            raise RuntimeError("target record must be an object")
        target_id = value.get("target_id")
        if target_id not in EXPECTED_TARGET_IDS or target_id in targets:
            raise RuntimeError("target identity is unknown or duplicated")
        budgets = value.get("budgets")
        if not isinstance(budgets, dict) or set(budgets) != set(REQUEST_TYPES):
            raise RuntimeError("target budgets are incomplete")
        for request_type in REQUEST_TYPES:
            amount = budgets[request_type]
            if isinstance(amount, bool) or not isinstance(amount, int) or not 1 <= amount <= 100:
                raise RuntimeError("target budget is outside the frozen bounds")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("target schema version is unsupported")
        targets[str(target_id)] = value
    if tuple(targets) != EXPECTED_TARGET_IDS:
        raise RuntimeError("target order differs from the frozen scoring order")
    return targets


def compute_contract_hash(input_root: Path, targets: Mapping[str, dict[str, Any]]) -> str:
    """Bind state to every machine-read public chemistry and receptor asset."""

    hasher = hashlib.sha256()
    relative_paths = [
        "targets.jsonl",
        "starting_materials.json",
        "reaction_templates.json",
    ]
    for target in targets.values():
        relative_paths.extend(
            [str(target.get("receptor_file", "")), str(target.get("receptor_pdb_file", ""))]
        )
    for relative in relative_paths:
        if not relative or "\\" in relative or relative.startswith("/"):
            raise RuntimeError("trusted contract contains an unsafe asset path")
        path = input_root.joinpath(*relative.split("/"))
        try:
            resolved = path.resolve(strict=True)
            resolved.relative_to(input_root.resolve(strict=True))
        except (OSError, ValueError) as error:
            raise RuntimeError("trusted contract asset escapes input root") from error
        payload = _safe_regular_file(path, 5_000_000)
        hasher.update(relative.encode("utf-8"))
        hasher.update(b"\0")
        hasher.update(len(payload).to_bytes(8, "big"))
        hasher.update(payload)
    return "sha256:" + hasher.hexdigest()


def initial_state(
    input_root: Path, targets: Mapping[str, dict[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "task_id": TASK_ID,
        "contract_sha256": compute_contract_hash(input_root, targets),
        "budgets": {
            target_id: {request_type: 0 for request_type in REQUEST_TYPES}
            for target_id in EXPECTED_TARGET_IDS
        },
        "evaluations": [],
        "request_cache": {},
        "query_cache": {},
    }


def _is_finite_float(value: Any) -> bool:
    return isinstance(value, float) and math.isfinite(value)


def _validate_failure_result(result: Mapping[str, Any]) -> None:
    if (
        set(result) != FAILURE_RESULT_KEYS
        or result.get("passed") is not False
        or not isinstance(result.get("error_code"), str)
        or not 1 <= len(result["error_code"]) <= 64
        or not isinstance(result.get("error"), str)
        or not 1 <= len(result["error"]) <= 4096
    ):
        raise RuntimeError("VerifyContext failure result is malformed")


def _validate_property_result(result: Mapping[str, Any], status: str) -> None:
    if status not in {"accepted", "rejected"} or set(result) != PROPERTY_RESULT_KEYS:
        raise RuntimeError("VerifyContext property result/status is malformed")
    properties = result.get("properties")
    violations = result.get("violations")
    if (
        not isinstance(properties, dict)
        or set(properties) != PROPERTY_KEYS
        or not isinstance(violations, list)
        or len(violations) > 64
        or any(not isinstance(item, str) or not 1 <= len(item) <= 128 for item in violations)
    ):
        raise RuntimeError("VerifyContext property result is malformed")
    for name, value in properties.items():
        if name in INTEGER_PROPERTY_KEYS:
            if isinstance(value, bool) or not isinstance(value, int):
                raise RuntimeError("VerifyContext property result is malformed")
        elif not _is_finite_float(value):
            raise RuntimeError("VerifyContext property result is malformed")


def _validate_route_result(result: Mapping[str, Any], status: str) -> None:
    if status == "rejected":
        _validate_failure_result(result)
        return
    if status != "accepted" or set(result) != ROUTE_ACCEPTED_RESULT_KEYS:
        raise RuntimeError("VerifyContext route result/status is malformed")
    step_products = result.get("step_products")
    if (
        result.get("passed") is not True
        or not isinstance(result.get("route_id"), str)
        or REQUEST_ID_RE.fullmatch(result["route_id"]) is None
        or not isinstance(result.get("candidate_key"), str)
        or CANDIDATE_KEY_RE.fullmatch(result["candidate_key"]) is None
        or not isinstance(result.get("final_product_smiles"), str)
        or not 1 <= len(result["final_product_smiles"]) <= 1024
        or not isinstance(result.get("route_fingerprint"), str)
        or SHA256_RE.fullmatch(result["route_fingerprint"]) is None
        or not isinstance(step_products, list)
        or len(step_products) != 1
    ):
        raise RuntimeError("VerifyContext route result is malformed")
    step = step_products[0]
    if (
        not isinstance(step, dict)
        or set(step) != {"step_id", "product_smiles"}
        or not isinstance(step.get("step_id"), str)
        or REQUEST_ID_RE.fullmatch(step["step_id"]) is None
        or not isinstance(step.get("product_smiles"), str)
        or not 1 <= len(step["product_smiles"]) <= 1024
    ):
        raise RuntimeError("VerifyContext route step result is malformed")


def _validate_interaction_evidence(
    evidence: Any, *, conformer_seed: int, selected_mode_index: int
) -> None:
    if (
        not isinstance(evidence, dict)
        or set(evidence) != INTERACTION_EVIDENCE_KEYS
        or not isinstance(evidence.get("all_satisfied"), bool)
        or not isinstance(evidence.get("groups"), list)
        or not 1 <= len(evidence["groups"]) <= 32
        or not isinstance(evidence.get("fingerprint"), str)
        or RAW_SHA256_RE.fullmatch(evidence["fingerprint"]) is None
        or evidence.get("conformer_seed") != conformer_seed
        or isinstance(evidence.get("conformer_seed"), bool)
        or evidence.get("selected_mode_index") != selected_mode_index
        or isinstance(evidence.get("selected_mode_index"), bool)
        or not isinstance(evidence.get("force_field"), str)
        or not 1 <= len(evidence["force_field"]) <= 64
        or isinstance(evidence.get("optimization_status"), bool)
        or not isinstance(evidence.get("optimization_status"), int)
    ):
        raise RuntimeError("VerifyContext docking interaction evidence is malformed")
    observed_all = True
    seen_groups: set[str] = set()
    seen_rules: set[str] = set()
    for group in evidence["groups"]:
        if not isinstance(group, dict) or set(group) != {
            "group_id",
            "min_satisfied",
            "satisfied_count",
            "satisfied",
            "rules",
        }:
            raise RuntimeError("VerifyContext docking interaction group is malformed")
        group_id = group["group_id"]
        rules = group["rules"]
        minimum = group["min_satisfied"]
        satisfied_count = group["satisfied_count"]
        if (
            not isinstance(group_id, str)
            or not 1 <= len(group_id) <= 128
            or group_id in seen_groups
            or not isinstance(rules, list)
            or not 1 <= len(rules) <= 32
            or isinstance(minimum, bool)
            or not isinstance(minimum, int)
            or not 1 <= minimum <= len(rules)
            or isinstance(satisfied_count, bool)
            or not isinstance(satisfied_count, int)
            or not 0 <= satisfied_count <= len(rules)
            or not isinstance(group["satisfied"], bool)
        ):
            raise RuntimeError("VerifyContext docking interaction group is malformed")
        seen_groups.add(group_id)
        observed_count = 0
        for rule in rules:
            if not isinstance(rule, dict) or not {
                "interaction_id",
                "type",
                "satisfied",
            }.issubset(rule):
                raise RuntimeError("VerifyContext docking interaction rule is malformed")
            rule_id = rule["interaction_id"]
            if (
                not isinstance(rule_id, str)
                or not 1 <= len(rule_id) <= 128
                or rule_id in seen_rules
                or not isinstance(rule["type"], str)
                or not 1 <= len(rule["type"]) <= 64
                or not isinstance(rule["satisfied"], bool)
            ):
                raise RuntimeError("VerifyContext docking interaction rule is malformed")
            seen_rules.add(rule_id)
            observed_count += int(rule["satisfied"])
        observed_group = observed_count >= minimum
        if satisfied_count != observed_count or group["satisfied"] is not observed_group:
            raise RuntimeError("VerifyContext docking interaction counts are inconsistent")
        observed_all = observed_all and observed_group
    if evidence["all_satisfied"] is not observed_all:
        raise RuntimeError("VerifyContext docking interaction aggregate is inconsistent")
    unsigned = {key: value for key, value in evidence.items() if key != "fingerprint"}
    try:
        expected_fingerprint = hashlib.sha256(_canonical_bytes(unsigned)).hexdigest()
    except CampaignRequestError as error:
        raise RuntimeError("VerifyContext docking interaction evidence is not canonical") from error
    if evidence["fingerprint"] != expected_fingerprint:
        raise RuntimeError("VerifyContext docking interaction fingerprint is invalid")


def _validate_docking_result(result: Mapping[str, Any], status: str) -> None:
    if status == "tool_failure":
        _validate_failure_result(result)
        return
    if status != "accepted" or set(result) != DOCKING_ACCEPTED_RESULT_KEYS:
        raise RuntimeError("VerifyContext docking result/status is malformed")
    mode_scores = result.get("all_mode_affinities_kcal_mol")
    evidence = result.get("interaction_evidence")
    pose_sdf = result.get("pose_sdf")
    if not isinstance(pose_sdf, str):
        raise RuntimeError("VerifyContext docking result is malformed")
    try:
        pose_sdf_bytes = pose_sdf.encode("utf-8")
    except UnicodeEncodeError as error:
        raise RuntimeError("VerifyContext docking pose text is malformed") from error
    if (
        result.get("passed") is not True
        or any(
            not isinstance(result.get(name), str)
            or REQUEST_ID_RE.fullmatch(result[name]) is None
            for name in ("property_evaluation_id", "route_evaluation_id", "pose_id")
        )
        or not isinstance(result.get("route_fingerprint"), str)
        or SHA256_RE.fullmatch(result["route_fingerprint"]) is None
        or not isinstance(result.get("candidate_key"), str)
        or CANDIDATE_KEY_RE.fullmatch(result["candidate_key"]) is None
        or not 1 <= len(pose_sdf_bytes) <= 5_000_000
        or not isinstance(result.get("pose_sha256"), str)
        or RAW_SHA256_RE.fullmatch(result["pose_sha256"]) is None
        or hashlib.sha256(pose_sdf_bytes).hexdigest() != result["pose_sha256"]
        or not _is_finite_float(result.get("vina_affinity_kcal_mol"))
        or not isinstance(mode_scores, list)
        or not 1 <= len(mode_scores) <= 3
        or any(not _is_finite_float(score) for score in mode_scores)
        or isinstance(result.get("selected_mode_index"), bool)
        or not isinstance(result.get("selected_mode_index"), int)
        or not 0 <= result["selected_mode_index"] < len(mode_scores)
        or not isinstance(result.get("interaction_fingerprint"), str)
        or RAW_SHA256_RE.fullmatch(result["interaction_fingerprint"]) is None
        or isinstance(result.get("conformer_seed"), bool)
        or not isinstance(result.get("conformer_seed"), int)
    ):
        raise RuntimeError("VerifyContext docking result is malformed")
    selected_mode_index = result["selected_mode_index"]
    if result["vina_affinity_kcal_mol"] != mode_scores[selected_mode_index]:
        raise RuntimeError("VerifyContext docking selected affinity is inconsistent")
    _validate_interaction_evidence(
        evidence,
        conformer_seed=result["conformer_seed"],
        selected_mode_index=selected_mode_index,
    )
    if evidence["fingerprint"] != result["interaction_fingerprint"]:
        raise RuntimeError("VerifyContext docking interaction binding is malformed")


def _validate_result_schema(request_type: str, status: str, result: Mapping[str, Any]) -> None:
    if request_type == "property_evaluation":
        _validate_property_result(result, status)
    elif request_type == "route_validation":
        _validate_route_result(result, status)
    else:
        _validate_docking_result(result, status)


def _validate_evaluation_record(record: Any, ordinal: int) -> None:
    fields = {
        "evaluation_id",
        "ordinal",
        "request_id",
        "request_sha256",
        "request_type",
        "target_id",
        "candidate_evaluation_smiles",
        "candidate_identity",
        "scientific_key",
        "status",
        "response_sha256",
        "response",
    }
    if not isinstance(record, dict) or set(record) != fields:
        raise RuntimeError("VerifyContext evaluation record is malformed")
    if (
        isinstance(record["ordinal"], bool)
        or not isinstance(record["ordinal"], int)
        or record["ordinal"] != ordinal
    ):
        raise RuntimeError("VerifyContext evaluation order is malformed")
    if (
        not isinstance(record["evaluation_id"], str)
        or REQUEST_ID_RE.fullmatch(record["evaluation_id"]) is None
        or not isinstance(record["request_id"], str)
        or REQUEST_ID_RE.fullmatch(record["request_id"]) is None
        or not isinstance(record["request_sha256"], str)
        or SHA256_RE.fullmatch(record["request_sha256"]) is None
        or not isinstance(record["scientific_key"], str)
        or SHA256_RE.fullmatch(record["scientific_key"]) is None
        or not isinstance(record["response_sha256"], str)
        or SHA256_RE.fullmatch(record["response_sha256"]) is None
    ):
        raise RuntimeError("VerifyContext evaluation identifiers or hashes are malformed")
    if (
        not isinstance(record["request_type"], str)
        or record["request_type"] not in REQUEST_TYPES
        or not isinstance(record["target_id"], str)
        or record["target_id"] not in EXPECTED_TARGET_IDS
    ):
        raise RuntimeError("VerifyContext evaluation identity is malformed")
    if not isinstance(record["status"], str) or record["status"] not in {
        "accepted",
        "rejected",
        "tool_failure",
    }:
        raise RuntimeError("VerifyContext evaluation status is malformed")
    if any(
        not isinstance(record[field], str) or not 1 <= len(record[field]) <= 4096
        for field in ("candidate_evaluation_smiles", "candidate_identity")
    ):
        raise RuntimeError("VerifyContext candidate identity is malformed")
    response = record["response"]
    if (
        not isinstance(response, dict)
        or set(response) != CHARGED_RESPONSE_KEYS
        or response.get("schema_version") != SCHEMA_VERSION
        or response.get("charged") is not True
        or not isinstance(response.get("result"), dict)
        or not isinstance(response.get("budgets"), dict)
    ):
        raise RuntimeError("VerifyContext evaluation response is malformed")
    for field in (
        "evaluation_id",
        "request_id",
        "request_type",
        "target_id",
        "candidate_evaluation_smiles",
        "candidate_identity",
        "status",
        "response_sha256",
    ):
        if response.get(field) != record[field]:
            raise RuntimeError("VerifyContext evaluation/response binding is malformed")
    expected_pass = record["status"] == "accepted"
    if response["result"].get("passed") is not expected_pass:
        raise RuntimeError("VerifyContext result/status binding is malformed")
    _validate_result_schema(record["request_type"], record["status"], response["result"])
    try:
        observed_hash = _response_hash(response)
    except CampaignRequestError as error:
        raise RuntimeError("VerifyContext response is not canonical JSON") from error
    if record["response_sha256"] != observed_hash:
        raise RuntimeError("VerifyContext response hash is invalid")


def validate_state(
    value: Any,
    input_root: Path,
    targets: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    if value is None:
        return initial_state(input_root, targets)
    fields = {
        "schema_version",
        "task_id",
        "contract_sha256",
        "budgets",
        "evaluations",
        "request_cache",
        "query_cache",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError("VerifyContext campaign state is malformed")
    if value["schema_version"] != SCHEMA_VERSION or value["task_id"] != TASK_ID:
        raise RuntimeError("VerifyContext campaign identity is malformed")
    if value["contract_sha256"] != compute_contract_hash(input_root, targets):
        raise RuntimeError("public scientific contract changed during the run")
    budgets = value["budgets"]
    if not isinstance(budgets, dict) or set(budgets) != set(EXPECTED_TARGET_IDS):
        raise RuntimeError("VerifyContext budget table is malformed")
    evaluations = value["evaluations"]
    if not isinstance(evaluations, list) or len(evaluations) > 300:
        raise RuntimeError("VerifyContext evaluation history is malformed")
    by_id: dict[str, dict[str, Any]] = {}
    seen_request_ids: set[str] = set()
    seen_scientific_keys: set[str] = set()
    observed = {
        target_id: {request_type: 0 for request_type in REQUEST_TYPES}
        for target_id in EXPECTED_TARGET_IDS
    }
    for ordinal, record in enumerate(evaluations, start=1):
        _validate_evaluation_record(record, ordinal)
        evaluation_id = record["evaluation_id"]
        if evaluation_id in by_id:
            raise RuntimeError("VerifyContext evaluation IDs are malformed")
        if record["request_id"] in seen_request_ids:
            raise RuntimeError("VerifyContext charged request IDs are duplicated")
        if record["scientific_key"] in seen_scientific_keys:
            raise RuntimeError("VerifyContext charged scientific keys are duplicated")
        expected_digest = hashlib.sha256(
            (
                value["contract_sha256"]
                + "\0"
                + record["request_type"]
                + "\0"
                + record["target_id"]
                + "\0"
                + record["scientific_key"]
            ).encode("ascii")
        ).hexdigest()[:12]
        expected_evaluation_id = "eval_%06d_%s" % (ordinal, expected_digest)
        if evaluation_id != expected_evaluation_id:
            raise RuntimeError("VerifyContext evaluation ID derivation is inconsistent")
        by_id[evaluation_id] = record
        seen_request_ids.add(record["request_id"])
        seen_scientific_keys.add(record["scientific_key"])
        observed[record["target_id"]][record["request_type"]] += 1
        target_id = record["target_id"]
        expected_budget_snapshot = {
            request_type: {
                "used": observed[target_id][request_type],
                "limit": targets[target_id]["budgets"][request_type],
                "remaining": (
                    targets[target_id]["budgets"][request_type]
                    - observed[target_id][request_type]
                ),
            }
            for request_type in REQUEST_TYPES
        }
        if _canonical_bytes(record["response"]["budgets"]) != _canonical_bytes(
            expected_budget_snapshot
        ):
            raise RuntimeError("VerifyContext response budget snapshot is inconsistent")
    if budgets != observed:
        raise RuntimeError("VerifyContext budget counters do not match charged history")
    for target_id, counters in budgets.items():
        if not isinstance(counters, dict) or set(counters) != set(REQUEST_TYPES):
            raise RuntimeError("VerifyContext target counters are malformed")
        for request_type, used in counters.items():
            limit = targets[target_id]["budgets"][request_type]
            if isinstance(used, bool) or not isinstance(used, int) or not 0 <= used <= limit:
                raise RuntimeError("VerifyContext budget exceeds its frozen limit")
    request_cache = value["request_cache"]
    query_cache = value["query_cache"]
    if not isinstance(request_cache, dict) or not isinstance(query_cache, dict):
        raise RuntimeError("VerifyContext caches are malformed")
    if (
        not len(evaluations) <= len(request_cache) <= 1024
        or len(query_cache) != len(evaluations)
    ):
        raise RuntimeError("VerifyContext cache cardinality is malformed")
    for request_id, cached in request_cache.items():
        if (
            not isinstance(request_id, str)
            or REQUEST_ID_RE.fullmatch(request_id) is None
            or not isinstance(cached, dict)
            or set(cached) != {"request_sha256", "evaluation_id"}
            or not isinstance(cached["request_sha256"], str)
            or SHA256_RE.fullmatch(cached["request_sha256"]) is None
            or not isinstance(cached["evaluation_id"], str)
        ):
            raise RuntimeError("VerifyContext request cache is malformed")
        record = by_id.get(cached["evaluation_id"])
        if record is None:
            raise RuntimeError("VerifyContext request cache is inconsistent")
        if request_id == record["request_id"] and cached["request_sha256"] != record["request_sha256"]:
            raise RuntimeError("VerifyContext charged request cache is inconsistent")
    for scientific_key, evaluation_id in query_cache.items():
        if (
            not isinstance(scientific_key, str)
            or SHA256_RE.fullmatch(scientific_key) is None
            or not isinstance(evaluation_id, str)
        ):
            raise RuntimeError("VerifyContext scientific cache is malformed")
        record = by_id.get(evaluation_id)
        if record is None or record["scientific_key"] != scientific_key:
            raise RuntimeError("VerifyContext scientific cache is inconsistent")
    for record in evaluations:
        charged_cache = request_cache.get(record["request_id"])
        if charged_cache != {
            "request_sha256": record["request_sha256"],
            "evaluation_id": record["evaluation_id"],
        }:
            raise RuntimeError("VerifyContext charged request cache entry is missing")
        if query_cache.get(record["scientific_key"]) != record["evaluation_id"]:
            raise RuntimeError("VerifyContext charged scientific cache entry is missing")
    return copy.deepcopy(value)


def budget_snapshot(
    state: Mapping[str, Any], targets: Mapping[str, dict[str, Any]], target_id: str
) -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for request_type in REQUEST_TYPES:
        used = int(state["budgets"][target_id][request_type])
        limit = int(targets[target_id]["budgets"][request_type])
        result[request_type] = {
            "used": used,
            "limit": limit,
            "remaining": limit - used,
        }
    return result


def _constraint_result(candidate: Any, target: Mapping[str, Any]) -> dict[str, Any]:
    properties = compute_properties(candidate)
    constraints = target.get("property_constraints")
    if not isinstance(constraints, dict):
        raise RuntimeError("target property constraints are malformed")
    violations: list[str] = []
    symbols = {atom.GetSymbol() for atom in candidate.molecule.GetAtoms()}
    allowed_elements = constraints.get("allowed_elements")
    if not isinstance(allowed_elements, list) or not set(symbols).issubset(allowed_elements):
        violations.append("allowed_elements")
    maximum_heavy = constraints.get("max_heavy_atoms")
    if isinstance(maximum_heavy, bool) or not isinstance(maximum_heavy, int):
        raise RuntimeError("target max_heavy_atoms is malformed")
    if int(properties["heavy_atoms"]) > maximum_heavy:
        violations.append("max_heavy_atoms")
    mapping = {
        "molecular_weight": "molecular_weight",
        "clogp": "clogp",
        "tpsa": "tpsa",
        "qed": "qed",
        "hbd": "hbd",
        "hba": "hba",
        "rotatable_bonds": "rotatable_bonds",
        "formal_charge": "formal_charge",
    }
    for constraint_name, property_name in mapping.items():
        bounds = constraints.get(constraint_name)
        if not isinstance(bounds, dict) or not bounds or not set(bounds).issubset({"min", "max"}):
            raise RuntimeError("target property bound is malformed: %s" % constraint_name)
        value = float(properties[property_name])
        if "min" in bounds and value < float(bounds["min"]):
            violations.append("%s:min" % constraint_name)
        if "max" in bounds and value > float(bounds["max"]):
            violations.append("%s:max" % constraint_name)
    forbidden = constraints.get("forbidden_smarts")
    if not isinstance(forbidden, list):
        raise RuntimeError("target forbidden SMARTS are malformed")
    for index, smarts in enumerate(forbidden):
        query = Chem.MolFromSmarts(smarts) if isinstance(smarts, str) else None
        if query is None:
            raise RuntimeError("target forbidden SMARTS cannot be compiled")
        if candidate.molecule.HasSubstructMatch(query, useChirality=True):
            violations.append("forbidden_smarts:%d" % (index + 1))
    return {
        "passed": not violations,
        "violations": violations,
        "properties": properties,
    }


def _preflight_route(
    route: Any,
    target: Mapping[str, Any],
    candidate: Any,
    templates: Mapping[str, ReactionTemplate],
    materials: Mapping[str, StartingMaterial],
) -> dict[str, Any]:
    """Validate all non-scientific route structure before charging replay."""

    route = _exact_object(route, ROUTE_KEYS, "route")
    if route["schema_version"] != SCHEMA_VERSION:
        raise CampaignRequestError("route schema_version must be 1.0")
    route_id = _identifier(route["route_id"], "route_id")
    target_id = _identifier(route["target_id"], "route target_id")
    if target_id != target["target_id"]:
        raise CampaignRequestError("route target_id does not match request target")
    candidate_key = route["candidate_key"]
    if not isinstance(candidate_key, str) or CANDIDATE_KEY_RE.fullmatch(candidate_key) is None:
        raise CampaignRequestError("route candidate_key has invalid syntax")
    try:
        final = standardize_smiles(route["final_product_smiles"], "route final product")
    except ContractError as error:
        raise CampaignRequestError(str(error)) from error
    if route["final_product_smiles"] != final.evaluation_smiles:
        raise CampaignRequestError("route final product must be evaluation-canonical")
    if final.evaluation_smiles != candidate.evaluation_smiles:
        raise CampaignRequestError("route final product differs from request candidate")
    allowed_templates = set(target.get("allowed_reaction_template_ids", []))
    allowed_materials = set(target.get("allowed_starting_material_ids", []))
    if not allowed_templates or not allowed_materials:
        raise RuntimeError("target synthesis allowlists are malformed")
    route_constraints = target.get("route_constraints")
    if (
        not isinstance(route_constraints, dict)
        or set(route_constraints) != {"min_steps", "max_steps"}
        or route_constraints["min_steps"] != 1
        or route_constraints["max_steps"] != 1
    ):
        raise RuntimeError("target route constraints must freeze exactly one step")
    steps = route["steps"]
    if not isinstance(steps, list) or len(steps) != 1:
        raise CampaignRequestError("this target requires exactly one route step")
    seen_steps: set[str] = set()
    dependencies: dict[str, list[str]] = {}
    normalized_steps: list[dict[str, Any]] = []
    last_product = ""
    for ordinal, raw in enumerate(steps, start=1):
        step = _exact_object(raw, STEP_KEYS, "route step %d" % ordinal)
        step_id = _identifier(step["step_id"], "step_id")
        if step_id in seen_steps:
            raise CampaignRequestError("route step_id is duplicated")
        template_id = _identifier(step["reaction_template_id"], "reaction_template_id")
        template = templates.get(template_id)
        if template_id not in allowed_templates or template is None:
            raise CampaignRequestError("route reaction template is not allowed")
        refs = step["reactants"]
        if not isinstance(refs, list) or len(refs) != len(template.reactant_roles):
            raise CampaignRequestError("route reactants do not match template arity")
        normalized_refs: list[dict[str, str]] = []
        step_dependencies: list[str] = []
        for slot, reference in enumerate(refs):
            if not isinstance(reference, dict) or len(reference) != 1:
                raise CampaignRequestError("route reactant must contain one reference")
            if set(reference) == {"material_id"}:
                material_id = _identifier(reference["material_id"], "material_id")
                material = materials.get(material_id)
                if material_id not in allowed_materials or material is None:
                    raise CampaignRequestError("route starting material is not allowed")
                if template.reactant_roles[slot] not in material.roles:
                    raise CampaignRequestError("route material is in the wrong template slot")
                normalized_refs.append({"material_id": material_id})
            elif set(reference) == {"step_id"}:
                dependency = _identifier(reference["step_id"], "reactant step_id")
                if dependency not in seen_steps:
                    raise CampaignRequestError("route contains a forward or cyclic reference")
                normalized_refs.append({"step_id": dependency})
                step_dependencies.append(dependency)
            else:
                raise CampaignRequestError("route reactant reference has invalid fields")
        try:
            product = standardize_smiles(step["product_smiles"], "route step product")
        except ContractError as error:
            raise CampaignRequestError(str(error)) from error
        if step["product_smiles"] != product.evaluation_smiles:
            raise CampaignRequestError("route step product must be evaluation-canonical")
        normalized_steps.append(
            {
                "reaction_template_id": template_id,
                "reactants": normalized_refs,
                "product_smiles": product.evaluation_smiles,
            }
        )
        dependencies[step_id] = step_dependencies
        seen_steps.add(step_id)
        last_product = product.evaluation_smiles
    if last_product != final.evaluation_smiles:
        raise CampaignRequestError("last route step does not declare the final product")
    last_step_id = _identifier(steps[-1]["step_id"], "last step_id")
    ancestors = {last_step_id}
    pending = [last_step_id]
    while pending:
        selected = pending.pop()
        for dependency in dependencies[selected]:
            if dependency not in ancestors:
                ancestors.add(dependency)
                pending.append(dependency)
    if ancestors != seen_steps:
        raise CampaignRequestError("route contains an unused padding step")
    # route_id, candidate_key and cosmetic step IDs do not buy a new scientific
    # replay.  Ordered dependencies are represented by their zero-based index.
    step_index = {str(step["step_id"]): index for index, step in enumerate(steps)}
    scientific_steps = []
    for normalized in normalized_steps:
        scientific_steps.append(
            {
                **normalized,
                "reactants": [
                    {"step_index": step_index[item["step_id"]]}
                    if "step_id" in item
                    else item
                    for item in normalized["reactants"]
                ],
            }
        )
    return {
        "route_id": route_id,
        "candidate_key": candidate_key,
        "scientific_payload": {
            "target_id": target_id,
            "candidate_identity": candidate.identity_smiles,
            "steps": scientific_steps,
            "final_product_smiles": final.evaluation_smiles,
        },
    }


def _evaluation_index(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    return {record["evaluation_id"]: record for record in state["evaluations"]}


def _accepted_prerequisite(
    evaluations: Mapping[str, dict[str, Any]],
    evaluation_id: Any,
    request_type: str,
    target_id: str,
    identity: str,
    evaluation_smiles: str,
) -> dict[str, Any]:
    identifier = _identifier(evaluation_id, "%s ID" % request_type)
    record = evaluations.get(identifier)
    if (
        record is None
        or record["request_type"] != request_type
        or record["target_id"] != target_id
        or record["candidate_identity"] != identity
        or record["candidate_evaluation_smiles"] != evaluation_smiles
        or record["status"] != "accepted"
    ):
        raise CampaignRequestError(
            "%s is not an accepted evaluation for this target and molecule" % request_type
        )
    return record


def _new_evaluation_id(
    state: Mapping[str, Any], request_type: str, target_id: str, scientific_key: str
) -> str:
    ordinal = len(state["evaluations"]) + 1
    digest = hashlib.sha256(
        (state["contract_sha256"] + "\0" + request_type + "\0" + target_id + "\0" + scientific_key).encode(
            "ascii"
        )
    ).hexdigest()[:12]
    return "eval_%06d_%s" % (ordinal, digest)


def _record_evaluation(
    state: dict[str, Any],
    request: Mapping[str, Any],
    request_sha256: str,
    candidate: Any,
    scientific_key: str,
    response: dict[str, Any],
) -> None:
    record = {
        "evaluation_id": response["evaluation_id"],
        "ordinal": len(state["evaluations"]) + 1,
        "request_id": request["request_id"],
        "request_sha256": request_sha256,
        "request_type": request["request_type"],
        "target_id": request["target_id"],
        "candidate_evaluation_smiles": candidate.evaluation_smiles,
        "candidate_identity": candidate.identity_smiles,
        "scientific_key": scientific_key,
        "status": response["status"],
        "response_sha256": response["response_sha256"],
        "response": copy.deepcopy(response),
    }
    state["evaluations"].append(record)
    state["request_cache"][request["request_id"]] = {
        "request_sha256": request_sha256,
        "evaluation_id": response["evaluation_id"],
    }
    state["query_cache"][scientific_key] = response["evaluation_id"]


def _failure_response(
    *,
    request: Mapping[str, Any],
    candidate: Any,
    evaluation_id: str,
    status: str,
    result: dict[str, Any],
    state: Mapping[str, Any],
    targets: Mapping[str, dict[str, Any]],
) -> dict[str, Any]:
    return _seal_response(
        {
            "schema_version": SCHEMA_VERSION,
            "status": status,
            "charged": True,
            "evaluation_id": evaluation_id,
            "request_id": request["request_id"],
            "request_type": request["request_type"],
            "target_id": request["target_id"],
            "candidate_evaluation_smiles": candidate.evaluation_smiles,
            "candidate_identity": candidate.identity_smiles,
            "result": result,
            "budgets": budget_snapshot(state, targets, request["target_id"]),
        }
    )


def process_validation(
    request: Any,
    input_root: Path,
    current_state: Any,
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Validate one request and return ``(response, next_state_or_none)``."""

    targets = load_targets(input_root)
    state = validate_state(current_state, input_root, targets)
    if not isinstance(request, dict):
        raise CampaignRequestError("validation request must be one JSON object")
    if len(_canonical_bytes(request)) > MAX_REQUEST_BYTES:
        raise CampaignRequestError("validation request exceeds 65,536 bytes")
    request_type = request.get("request_type")
    if not isinstance(request_type, str) or request_type not in REQUEST_KEYS:
        raise CampaignRequestError("request_type is unknown")
    _exact_object(request, REQUEST_KEYS[request_type], "validation request")
    if request["schema_version"] != SCHEMA_VERSION:
        raise CampaignRequestError("schema_version must be 1.0")
    request_id = _identifier(request["request_id"], "request_id")
    target_id = _identifier(request["target_id"], "target_id")
    target = targets.get(target_id)
    if target is None:
        raise CampaignRequestError("target_id is unknown")
    request_sha256 = _sha256(request)
    cached_request = state["request_cache"].get(request_id)
    if cached_request is not None:
        if cached_request["request_sha256"] != request_sha256:
            raise CampaignRequestError("request_id was already used for different content")
        response = _evaluation_index(state)[cached_request["evaluation_id"]]["response"]
        return copy.deepcopy(response), None
    try:
        candidate = standardize_smiles(request["candidate_smiles"], "candidate_smiles")
    except ContractError as error:
        raise CampaignRequestError(str(error)) from error
    if request["candidate_smiles"] != candidate.evaluation_smiles:
        raise CampaignRequestError("candidate_smiles must be evaluation-canonical")

    templates: Mapping[str, ReactionTemplate] | None = None
    materials: Mapping[str, StartingMaterial] | None = None
    route_preflight: dict[str, Any] | None = None
    property_record: dict[str, Any] | None = None
    route_record: dict[str, Any] | None = None
    if request_type == "property_evaluation":
        scientific_payload: dict[str, Any] = {
            "request_type": request_type,
            "target_id": target_id,
            "candidate_identity": candidate.identity_smiles,
        }
    elif request_type == "route_validation":
        try:
            templates = load_reaction_templates(input_root)
            materials = load_starting_materials(input_root)
        except ContractError as error:
            raise RuntimeError("trusted synthesis inputs are invalid") from error
        route_preflight = _preflight_route(
            request["route"], target, candidate, templates, materials
        )
        scientific_payload = {
            "request_type": request_type,
            **route_preflight["scientific_payload"],
        }
    else:
        evaluations = _evaluation_index(state)
        property_record = _accepted_prerequisite(
            evaluations,
            request["property_evaluation_id"],
            "property_evaluation",
            target_id,
            candidate.identity_smiles,
            candidate.evaluation_smiles,
        )
        route_record = _accepted_prerequisite(
            evaluations,
            request["route_evaluation_id"],
            "route_validation",
            target_id,
            candidate.identity_smiles,
            candidate.evaluation_smiles,
        )
        route_result = route_record["response"].get("result")
        if not isinstance(route_result, dict) or not isinstance(
            route_result.get("route_fingerprint"), str
        ):
            raise RuntimeError("trusted route evaluation lacks a fingerprint")
        scientific_payload = {
            "request_type": request_type,
            "target_id": target_id,
            "candidate_identity": candidate.identity_smiles,
            "property_scientific_key": property_record["scientific_key"],
            "route_fingerprint": route_result["route_fingerprint"],
        }

    scientific_key = _sha256(scientific_payload)
    cached_evaluation_id = state["query_cache"].get(scientific_key)
    if cached_evaluation_id is not None:
        cached_record = _evaluation_index(state)[cached_evaluation_id]
        if cached_record["candidate_evaluation_smiles"] != candidate.evaluation_smiles:
            # Charge-parent/tautomer identity aliases cannot purchase another
            # oracle call, but their preserved evaluation graphs may have
            # different properties or docking behavior.  Reject instead of
            # returning a scientifically mismatched cached response.
            raise CampaignRequestError(
                "molecular identity was already evaluated with a different evaluation graph"
            )
        if len(state["request_cache"]) >= 1024:
            raise CampaignRequestError("request-ID cache is full")
        next_state = copy.deepcopy(state)
        next_state["request_cache"][request_id] = {
            "request_sha256": request_sha256,
            "evaluation_id": cached_evaluation_id,
        }
        return (
            copy.deepcopy(cached_record["response"]),
            next_state,
        )

    # Every charged evaluation adds a request-cache entry.  Refuse a new
    # scientific call before calculating or charging when the bounded cache
    # cannot represent its result in a subsequently valid state.
    if len(state["request_cache"]) >= 1024:
        raise CampaignRequestError("request-ID cache is full")

    used = state["budgets"][target_id][request_type]
    limit = target["budgets"][request_type]
    if used >= limit:
        return (
            _seal_response(
                {
                    "schema_version": SCHEMA_VERSION,
                    "status": "budget_exhausted",
                    "charged": False,
                    "evaluation_id": None,
                    "request_id": request_id,
                    "request_type": request_type,
                    "target_id": target_id,
                    "candidate_evaluation_smiles": candidate.evaluation_smiles,
                    "candidate_identity": candidate.identity_smiles,
                    "result": None,
                    "budgets": budget_snapshot(state, targets, target_id),
                }
            ),
            None,
        )

    # The single scientific unit is charged immediately before the relevant
    # property calculation, reaction execution, or 3D preparation/docking.
    next_state = copy.deepcopy(state)
    next_state["budgets"][target_id][request_type] += 1
    evaluation_id = _new_evaluation_id(next_state, request_type, target_id, scientific_key)

    if request_type == "property_evaluation":
        result = _constraint_result(candidate, target)
        status = "accepted" if result["passed"] else "rejected"
        response = _failure_response(
            request=request,
            candidate=candidate,
            evaluation_id=evaluation_id,
            status=status,
            result=result,
            state=next_state,
            targets=targets,
        )
    elif request_type == "route_validation":
        assert templates is not None and materials is not None and route_preflight is not None
        try:
            replay = replay_route(
                request["route"], target, candidate.evaluation_smiles, templates, materials
            )
            result = {
                "passed": True,
                "route_id": replay.route_id,
                "candidate_key": replay.candidate_key,
                "final_product_smiles": replay.final_product_smiles,
                "route_fingerprint": replay.fingerprint,
                "step_products": [
                    {"step_id": step_id, "product_smiles": product}
                    for step_id, product in replay.step_products
                ],
            }
            status = "accepted"
        except RouteReplayError as error:
            result = {"passed": False, "error_code": error.code, "error": str(error)}
            status = "rejected"
        except ContractError as error:
            # Preflight checks every structural contract. Reaching this branch
            # means the initialized RDKit replay failed after its scientific
            # unit began, so it remains charged and fails closed.
            result = {"passed": False, "error_code": error.code, "error": str(error)}
            status = "rejected"
        response = _failure_response(
            request=request,
            candidate=candidate,
            evaluation_id=evaluation_id,
            status=status,
            result=result,
            state=next_state,
            targets=targets,
        )
    else:
        assert property_record is not None and route_record is not None
        route_result = route_record["response"]["result"]
        candidate_key = str(route_result["candidate_key"])
        pose_id = "pose_" + evaluation_id.removeprefix("eval_")
        try:
            docking = run_frozen_docking(
                candidate.molecule,
                candidate.evaluation_smiles,
                target,
                input_root,
                pose_id=pose_id,
                candidate_key=candidate_key,
                docking_evaluation_id=evaluation_id,
            )
            result = {
                "passed": True,
                "property_evaluation_id": property_record["evaluation_id"],
                "route_evaluation_id": route_record["evaluation_id"],
                "route_fingerprint": route_result["route_fingerprint"],
                "candidate_key": candidate_key,
                "pose_id": pose_id,
                "pose_sdf": docking.pose_sdf,
                "pose_sha256": docking.pose_sha256,
                "vina_affinity_kcal_mol": docking.score,
                "all_mode_affinities_kcal_mol": docking.all_mode_scores,
                "selected_mode_index": docking.selected_mode_index,
                "interaction_fingerprint": docking.interaction_fingerprint,
                "interaction_evidence": docking.interaction_evidence,
                "conformer_seed": docking.conformer_seed,
            }
            status = "accepted"
        except DockingError as error:
            result = {"passed": False, "error_code": "DOCKING_FAILED", "error": str(error)}
            status = "tool_failure"
        response = _failure_response(
            request=request,
            candidate=candidate,
            evaluation_id=evaluation_id,
            status=status,
            result=result,
            state=next_state,
            targets=targets,
        )

    _record_evaluation(
        next_state,
        request,
        request_sha256,
        candidate,
        scientific_key,
        response,
    )
    return response, next_state


def invalid_response(error: str) -> dict[str, Any]:
    """Return the stable public envelope for a non-charged schema error."""

    return _seal_response(
        {
            "schema_version": SCHEMA_VERSION,
            "status": "invalid",
            "charged": False,
            "evaluation_id": None,
            "request_id": None,
            "request_type": None,
            "target_id": None,
            "candidate_evaluation_smiles": None,
            "candidate_identity": None,
            "result": {"error": str(error)[:500]},
            "budgets": None,
        }
    )


__all__ = [
    "TASK_ID",
    "STATE_KEY",
    "REQUEST_TYPES",
    "EXPECTED_TARGET_IDS",
    "CampaignRequestError",
    "load_targets",
    "compute_contract_hash",
    "initial_state",
    "validate_state",
    "budget_snapshot",
    "process_validation",
    "invalid_response",
]
