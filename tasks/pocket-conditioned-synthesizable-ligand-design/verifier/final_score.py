"""Offline final-artifact validation and macro verified-hit-rate scoring."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import re
import stat
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_EVEN
from pathlib import Path
from typing import Any, Iterable, Mapping

from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem

from campaign import (
    EXPECTED_TARGET_IDS,
    REQUEST_TYPES,
    TASK_ID,
    _constraint_result,
    load_targets,
    validate_state,
)
from chemistry_contract import (
    ContractError,
    compute_properties,
    load_reaction_templates,
    load_starting_materials,
    replay_route,
    standardize_smiles,
    strict_json_load,
)
from docking_contract import (
    DockingError,
    evaluate_required_interactions,
    parse_receptor_pdb,
    receptor_paths,
    run_frozen_docking,
)


ARTIFACT_LIMITS = {
    "candidates.csv": 128 * 1024,
    "poses.sdf": 5 * 1024 * 1024,
    "synthesis_routes.json": 1024 * 1024,
    "interaction_evidence.json": 1024 * 1024,
    "evidence.jsonl": 2 * 1024 * 1024,
    "summary.json": 64 * 1024,
}
CANDIDATE_HEADER = (
    "target_id",
    "rank",
    "candidate_key",
    "canonical_smiles",
    "reported_qed",
    "reported_clogp",
    "reported_vina_affinity_kcal_mol",
    "route_id",
    "pose_id",
    "property_evaluation_id",
    "route_evaluation_id",
    "docking_evaluation_id",
)
CANDIDATE_KEY_RE = re.compile(r"cand_[a-z0-9_]{1,48}\Z")
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}\Z")
DECIMAL_TEXT_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")
MAX_CANDIDATES = 30
MAX_PER_TARGET = 10
MAX_POSE_ATOMS = 256
MAX_ABS_POSE_COORDINATE_ANGSTROM = 10_000.0
POSE_RMSD_TOLERANCE_ANGSTROM = 0.05
INVALID_RAW_FALLBACK = -1.0


class SubmissionError(ValueError):
    """A final artifact violates the structural or trusted-state contract."""


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
        raise SubmissionError("artifact contains a non-canonical JSON value") from error


def _round8(value: float) -> float:
    if not math.isfinite(value):
        raise SubmissionError("score is non-finite")
    return float(Decimal(str(value)).quantize(Decimal("0.00000001"), rounding=ROUND_HALF_EVEN))


def _reported_number(value: str, label: str, lower: float, upper: float) -> float:
    if len(value) > 32 or DECIMAL_TEXT_RE.fullmatch(value) is None:
        raise SubmissionError("%s must be a plain finite decimal" % label)
    try:
        parsed = float(Decimal(value))
    except (ValueError, ArithmeticError) as error:
        raise SubmissionError("%s must be a plain finite decimal" % label) from error
    if not math.isfinite(parsed) or not lower <= parsed <= upper:
        raise SubmissionError("%s is outside its reporting bounds" % label)
    return parsed


def _regular_file(path: Path, maximum: int, *, allow_empty: bool = False) -> bytes:
    try:
        metadata = path.lstat()
    except OSError as error:
        raise SubmissionError("missing required artifact: %s" % path.name) from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise SubmissionError("artifact must be a regular non-symlink file: %s" % path.name)
    minimum = 0 if allow_empty else 1
    if metadata.st_size < minimum or metadata.st_size > maximum:
        raise SubmissionError("artifact size is outside its limit: %s" % path.name)
    try:
        return path.read_bytes()
    except OSError as error:
        raise SubmissionError("artifact cannot be read: %s" % path.name) from error


def validate_submission_layout(submission_root: Path) -> dict[str, bytes]:
    try:
        metadata = submission_root.lstat()
    except OSError as error:
        raise SubmissionError("submission directory is missing") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise SubmissionError("submission root must be a real directory")
    try:
        entries = list(submission_root.iterdir())
    except OSError as error:
        raise SubmissionError("submission directory cannot be listed") from error
    if {entry.name for entry in entries} != set(ARTIFACT_LIMITS):
        raise SubmissionError("submission must contain exactly the six documented artifacts")
    payloads: dict[str, bytes] = {}
    for name, maximum in ARTIFACT_LIMITS.items():
        payloads[name] = _regular_file(
            submission_root / name,
            maximum,
            allow_empty=name in {"poses.sdf", "evidence.jsonl"},
        )
    return payloads


def _utf8(payload: bytes, label: str) -> str:
    try:
        text = payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise SubmissionError("%s must be strict UTF-8" % label) from error
    if text.startswith("\ufeff") or "\x00" in text:
        raise SubmissionError("%s contains a BOM or NUL" % label)
    return text


def parse_candidates(payload: bytes) -> list[dict[str, Any]]:
    text = _utf8(payload, "candidates.csv")
    try:
        reader = csv.reader(io.StringIO(text, newline=""), strict=True)
        rows = list(reader)
    except csv.Error as error:
        raise SubmissionError("candidates.csv is malformed") from error
    if not rows or tuple(rows[0]) != CANDIDATE_HEADER:
        raise SubmissionError("candidates.csv has the wrong exact header")
    if any(not row for row in rows[1:]):
        raise SubmissionError("candidates.csv contains a blank record")
    if len(rows) - 1 > MAX_CANDIDATES:
        raise SubmissionError("candidates.csv exceeds 30 candidates")
    output: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    seen_route_ids: set[str] = set()
    seen_by_target: dict[str, dict[str, set[Any]]] = defaultdict(
        lambda: {
            "identity": set(),
            "route_id": set(),
            "pose_id": set(),
            "rank": set(),
        }
    )
    for line_number, values in enumerate(rows[1:], start=2):
        if len(values) != len(CANDIDATE_HEADER):
            raise SubmissionError("candidates.csv row %d has the wrong field count" % line_number)
        if any(not value or value != value.strip() for value in values):
            raise SubmissionError("candidates.csv row %d has blank/padded fields" % line_number)
        row = dict(zip(CANDIDATE_HEADER, values))
        target_id = row["target_id"]
        if target_id not in EXPECTED_TARGET_IDS:
            raise SubmissionError("candidates.csv row has an unknown target_id")
        if row["rank"] not in {str(value) for value in range(1, MAX_PER_TARGET + 1)}:
            raise SubmissionError("candidate rank must be in 1..10")
        rank = int(row["rank"])
        candidate_key = row["candidate_key"]
        if CANDIDATE_KEY_RE.fullmatch(candidate_key) is None or candidate_key in seen_keys:
            raise SubmissionError("candidate_key is invalid or globally duplicated")
        for name in (
            "route_id",
            "pose_id",
            "property_evaluation_id",
            "route_evaluation_id",
            "docking_evaluation_id",
        ):
            if IDENTIFIER_RE.fullmatch(row[name]) is None:
                raise SubmissionError("candidate %s is not a valid identifier" % name)
        if row["route_id"] in seen_route_ids:
            raise SubmissionError("route_id must be globally unique")
        try:
            candidate = standardize_smiles(row["canonical_smiles"], "candidate canonical_smiles")
        except ContractError as error:
            raise SubmissionError(str(error)) from error
        if row["canonical_smiles"] != candidate.evaluation_smiles:
            raise SubmissionError("candidate SMILES is not evaluation-canonical")
        reported_qed = _reported_number(row["reported_qed"], "reported_qed", 0.0, 1.0)
        reported_clogp = _reported_number(
            row["reported_clogp"], "reported_clogp", -20.0, 20.0
        )
        reported_vina = _reported_number(
            row["reported_vina_affinity_kcal_mol"],
            "reported_vina_affinity_kcal_mol",
            -100.0,
            100.0,
        )
        target_seen = seen_by_target[target_id]
        unique_values = {
            "identity": candidate.identity_smiles,
            "route_id": row["route_id"],
            "pose_id": row["pose_id"],
            "rank": rank,
        }
        if any(value in target_seen[name] for name, value in unique_values.items()):
            raise SubmissionError("candidate identity, route, pose, or rank is duplicated")
        for name, value in unique_values.items():
            target_seen[name].add(value)
        seen_keys.add(candidate_key)
        seen_route_ids.add(row["route_id"])
        output.append(
            {
                **row,
                "rank": rank,
                "reported_qed": reported_qed,
                "reported_clogp": reported_clogp,
                "reported_vina_affinity_kcal_mol": reported_vina,
                "candidate": candidate,
            }
        )
    for target_id in EXPECTED_TARGET_IDS:
        ranks = seen_by_target[target_id]["rank"]
        if ranks and ranks != set(range(1, len(ranks) + 1)):
            raise SubmissionError("candidate ranks must be contiguous from one per target")
    return output


def _parse_one_sdf(text: str, label: str) -> Chem.Mol:
    supplier = Chem.ForwardSDMolSupplier(
        io.BytesIO(text.encode("utf-8")),
        sanitize=True,
        removeHs=False,
        strictParsing=True,
    )
    molecules = list(supplier)
    if len(molecules) != 1 or molecules[0] is None:
        raise SubmissionError("%s must contain exactly one valid SDF record" % label)
    return molecules[0]


def parse_poses(payload: bytes, rows: list[dict[str, Any]]) -> dict[str, Chem.Mol]:
    if not rows:
        if payload:
            raise SubmissionError("poses.sdf must be exactly empty when no candidates are submitted")
        return {}
    _utf8(payload, "poses.sdf")
    if not (
        payload.endswith(b"$$$$")
        or payload.endswith(b"$$$$\n")
        or payload.endswith(b"$$$$\r\n")
    ):
        raise SubmissionError("poses.sdf must end at a complete SDF record terminator")
    try:
        supplier = Chem.ForwardSDMolSupplier(
            io.BytesIO(payload), sanitize=True, removeHs=False, strictParsing=True
        )
        molecules = list(supplier)
    except Exception as error:
        raise SubmissionError("poses.sdf could not be parsed") from error
    if len(molecules) != len(rows) or any(molecule is None for molecule in molecules):
        raise SubmissionError("poses.sdf record count or syntax is invalid")
    by_key: dict[str, Chem.Mol] = {}
    required_properties = {
        "target_id",
        "candidate_key",
        "pose_id",
        "docking_evaluation_id",
    }
    row_by_key = {row["candidate_key"]: row for row in rows}
    for molecule in molecules:
        assert molecule is not None
        if molecule.GetNumAtoms() > MAX_POSE_ATOMS:
            raise SubmissionError("pose exceeds the total-atom limit")
        if molecule.GetNumConformers() != 1:
            raise SubmissionError("each pose must contain exactly one conformer")
        conformer = molecule.GetConformer()
        if not conformer.Is3D():
            raise SubmissionError("each pose must be marked as three-dimensional")
        for atom_index in range(molecule.GetNumAtoms()):
            point = conformer.GetAtomPosition(atom_index)
            if not all(math.isfinite(value) for value in (point.x, point.y, point.z)):
                raise SubmissionError("pose coordinates must be finite")
            if any(
                abs(value) > MAX_ABS_POSE_COORDINATE_ANGSTROM
                for value in (point.x, point.y, point.z)
            ):
                raise SubmissionError("pose coordinate exceeds the absolute bound")
        if molecule.GetNumHeavyAtoms() > 80:
            raise SubmissionError("pose exceeds the heavy-atom limit")
        if any(not molecule.HasProp(name) for name in required_properties):
            raise SubmissionError("pose lacks a required scalar identifier")
        properties = {name: molecule.GetProp(name) for name in required_properties}
        candidate_key = properties["candidate_key"]
        row = row_by_key.get(candidate_key)
        if row is None or candidate_key in by_key:
            raise SubmissionError("pose candidate_key is missing or duplicated")
        for name in required_properties - {"candidate_key"}:
            if properties[name] != row[name]:
                raise SubmissionError("pose identifier does not match candidates.csv")
        try:
            graph = Chem.RemoveHs(Chem.Mol(molecule), sanitize=True)
            graph_smiles = Chem.MolToSmiles(graph, canonical=True, isomericSmiles=True)
            standardized = standardize_smiles(graph_smiles, "pose graph")
        except Exception as error:
            raise SubmissionError("pose graph cannot be standardized") from error
        if standardized.evaluation_smiles != row["canonical_smiles"]:
            raise SubmissionError("pose graph does not match the candidate")
        by_key[candidate_key] = molecule
    return by_key


def _load_strict_json_from_payload(
    submission_root: Path, name: str, maximum: int, depth: int
) -> Any:
    try:
        return strict_json_load(submission_root / name, maximum, depth)
    except ContractError as error:
        raise SubmissionError("%s is not valid bounded JSON" % name) from error


def parse_routes(
    submission_root: Path, rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    document = _load_strict_json_from_payload(
        submission_root, "synthesis_routes.json", ARTIFACT_LIMITS["synthesis_routes.json"], 24
    )
    if not isinstance(document, dict) or set(document) != {"schema_version", "routes"}:
        raise SubmissionError("synthesis_routes.json has an invalid top-level schema")
    routes = document["routes"]
    if document["schema_version"] != "1.0" or not isinstance(routes, list):
        raise SubmissionError("synthesis_routes.json has an invalid version or routes list")
    if len(routes) != len(rows):
        raise SubmissionError("synthesis route count differs from candidate count")
    output: dict[str, dict[str, Any]] = {}
    for route in routes:
        if not isinstance(route, dict):
            raise SubmissionError("synthesis route must be an object")
        route_id = route.get("route_id")
        if not isinstance(route_id, str) or route_id in output:
            raise SubmissionError("synthesis route_id is missing or duplicated")
        output[route_id] = route
    if set(output) != {row["route_id"] for row in rows}:
        raise SubmissionError("synthesis route IDs do not exactly cover candidates")
    return output


def parse_interactions(
    submission_root: Path, rows: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    document = _load_strict_json_from_payload(
        submission_root,
        "interaction_evidence.json",
        ARTIFACT_LIMITS["interaction_evidence.json"],
        32,
    )
    if not isinstance(document, dict) or set(document) != {"schema_version", "records"}:
        raise SubmissionError("interaction_evidence.json has an invalid top-level schema")
    records = document["records"]
    if document["schema_version"] != "1.0" or not isinstance(records, list):
        raise SubmissionError("interaction evidence has an invalid version or records list")
    if len(records) != len(rows):
        raise SubmissionError("interaction record count differs from candidate count")
    fields = {
        "candidate_key",
        "target_id",
        "pose_id",
        "docking_evaluation_id",
        "interaction_fingerprint",
        "all_satisfied",
        "groups",
    }
    output: dict[str, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict) or set(record) != fields:
            raise SubmissionError("interaction record has invalid exact fields")
        candidate_key = record["candidate_key"]
        if not isinstance(candidate_key, str) or candidate_key in output:
            raise SubmissionError("interaction candidate_key is missing or duplicated")
        if not isinstance(record["all_satisfied"], bool) or not isinstance(record["groups"], list):
            raise SubmissionError("interaction result fields are malformed")
        output[candidate_key] = record
    if set(output) != {row["candidate_key"] for row in rows}:
        raise SubmissionError("interaction records do not exactly cover candidates")
    return output


def _json_line(text: str, line_number: int) -> Any:
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
        value = json.loads(
            text, parse_constant=reject_constant, object_pairs_hook=no_duplicates
        )
        pending = [(value, 0)]
        while pending:
            item, depth = pending.pop()
            if depth > 8:
                raise ValueError("excessive JSON nesting")
            if isinstance(item, float) and not math.isfinite(item):
                # Python's JSON decoder accepts exponent overflow such as
                # ``1e999`` as infinity without invoking parse_constant.
                raise ValueError("non-finite numeric value")
            if isinstance(item, dict):
                pending.extend((child, depth + 1) for child in item.values())
            elif isinstance(item, list):
                pending.extend((child, depth + 1) for child in item)
        return value
    except (ValueError, RecursionError) as error:
        raise SubmissionError("evidence.jsonl line %d is invalid" % line_number) from error


def validate_evidence_jsonl(payload: bytes, evaluations: list[dict[str, Any]]) -> None:
    text = _utf8(payload, "evidence.jsonl")
    records = [
        _json_line(line, line_number)
        for line_number, line in enumerate(text.splitlines(), start=1)
        if line.strip()
    ]
    if len(records) != len(evaluations):
        raise SubmissionError("evidence.jsonl does not exactly cover charged evaluations")
    fields = {
        "schema_version",
        "evaluation_id",
        "request_id",
        "request_type",
        "target_id",
        "candidate_identity",
        "status",
        "response_sha256",
    }
    for ordinal, (record, trusted) in enumerate(zip(records, evaluations), start=1):
        if not isinstance(record, dict) or set(record) != fields:
            raise SubmissionError("evidence.jsonl line %d has invalid fields" % ordinal)
        expected = {
            "schema_version": "1.0",
            "evaluation_id": trusted["evaluation_id"],
            "request_id": trusted["request_id"],
            "request_type": trusted["request_type"],
            "target_id": trusted["target_id"],
            "candidate_identity": trusted["candidate_identity"],
            "status": trusted["status"],
            "response_sha256": trusted["response_sha256"],
        }
        if record != expected:
            raise SubmissionError("evidence.jsonl differs from trusted call order/content")


def validate_summary(
    submission_root: Path,
    rows: list[dict[str, Any]],
    state: Mapping[str, Any],
) -> None:
    document = _load_strict_json_from_payload(
        submission_root, "summary.json", ARTIFACT_LIMITS["summary.json"], 16
    )
    fields = {"schema_version", "target_summaries", "budget_usage", "notes"}
    if not isinstance(document, dict) or set(document) != fields:
        raise SubmissionError("summary.json has invalid exact fields")
    if document["schema_version"] != "1.0":
        raise SubmissionError("summary.json schema_version must be 1.0")
    notes = document["notes"]
    if not isinstance(notes, str) or len(notes) > 4096 or "\x00" in notes:
        raise SubmissionError("summary notes are invalid")
    target_summaries = document["target_summaries"]
    budget_usage = document["budget_usage"]
    if not isinstance(target_summaries, list) or not isinstance(budget_usage, list):
        raise SubmissionError("summary target/budget tables must be arrays")
    if len(target_summaries) != 3 or len(budget_usage) != 3:
        raise SubmissionError("summary must contain all three targets exactly once")
    counts = {target_id: 0 for target_id in EXPECTED_TARGET_IDS}
    for row in rows:
        counts[row["target_id"]] += 1
    expected_summaries = [
        {"target_id": target_id, "submitted_count": counts[target_id]}
        for target_id in EXPECTED_TARGET_IDS
    ]
    if target_summaries != expected_summaries:
        raise SubmissionError("summary target counts differ from candidates.csv")
    expected_budgets = [
        {
            "target_id": target_id,
            "property_evaluation_used": state["budgets"][target_id]["property_evaluation"],
            "route_validation_used": state["budgets"][target_id]["route_validation"],
            "docking_evaluation_used": state["budgets"][target_id]["docking_evaluation"],
        }
        for target_id in EXPECTED_TARGET_IDS
    ]
    if budget_usage != expected_budgets:
        raise SubmissionError("summary budget usage differs from trusted state")


def load_score_config(verifier_root: Path) -> dict[str, Any]:
    path = verifier_root / "test" / "data" / "score_config.json"
    try:
        value = strict_json_load(path, 128 * 1024, 16)
    except ContractError as error:
        raise RuntimeError("trusted score config is absent or invalid") from error
    fields = {
        "schema_version",
        "task_id",
        "primary_metric_name",
        "direction",
        "aggregation",
        "target_order",
        "threshold_tolerance_kcal_mol",
        "invalid_raw_value",
        "targets",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError("trusted score config has invalid exact fields")
    if (
        value["schema_version"] != "1.0"
        or value["task_id"] != TASK_ID
        or value["primary_metric_name"] != "macro_verified_hit_rate_at_10"
        or value["direction"] != "maximize"
        or value["aggregation"] != "macro_average_zero_padded_at_10"
        or value["target_order"] != list(EXPECTED_TARGET_IDS)
    ):
        raise RuntimeError("trusted score config identity is invalid")
    tolerance = value["threshold_tolerance_kcal_mol"]
    invalid = value["invalid_raw_value"]
    if (
        isinstance(tolerance, bool)
        or not isinstance(tolerance, (int, float))
        or not math.isfinite(tolerance)
        or not 0 <= tolerance <= 0.1
        or isinstance(invalid, bool)
        or not isinstance(invalid, (int, float))
        or not math.isfinite(invalid)
        or invalid >= 0
    ):
        raise RuntimeError("trusted score numeric constants are invalid")
    target_config = value["targets"]
    if not isinstance(target_config, dict) or set(target_config) != set(EXPECTED_TARGET_IDS):
        raise RuntimeError("trusted score target table is invalid")
    target_fields = {"vina_affinity_threshold_kcal_mol"}
    for target_id, record in target_config.items():
        if not isinstance(record, dict) or set(record) != target_fields:
            raise RuntimeError("trusted target score record is invalid")
        threshold = record["vina_affinity_threshold_kcal_mol"]
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(threshold):
            raise RuntimeError("trusted target threshold is invalid")
    return value


def _evaluation(
    by_id: Mapping[str, dict[str, Any]],
    evaluation_id: str,
    request_type: str,
    row: Mapping[str, Any],
) -> dict[str, Any]:
    record = by_id.get(evaluation_id)
    if (
        record is None
        or record["request_type"] != request_type
        or record["target_id"] != row["target_id"]
        or record["candidate_identity"] != row["candidate"].identity_smiles
        or record["candidate_evaluation_smiles"] != row["canonical_smiles"]
        or record["status"] != "accepted"
    ):
        raise SubmissionError("candidate references an unaccepted or mismatched evaluation")
    return record


def _atom_order_signature(molecule: Chem.Mol) -> tuple[Any, ...]:
    atoms = tuple(
        (atom.GetAtomicNum(), atom.GetFormalCharge(), atom.GetIsAromatic())
        for atom in molecule.GetAtoms()
    )
    bonds = tuple(
        sorted(
            (
                min(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                max(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()),
                str(bond.GetBondType()),
                bond.GetIsAromatic(),
            )
            for bond in molecule.GetBonds()
        )
    )
    return atoms, bonds


def direct_heavy_atom_rmsd(left: Chem.Mol, right: Chem.Mol) -> float:
    if _atom_order_signature(left) != _atom_order_signature(right):
        raise SubmissionError("pose atom order or bond graph differs from trusted pose")
    if left.GetNumConformers() != 1 or right.GetNumConformers() != 1:
        raise SubmissionError("pose comparison requires one conformer")
    left_conf = left.GetConformer()
    right_conf = right.GetConformer()
    squared: list[float] = []
    for atom in left.GetAtoms():
        if atom.GetAtomicNum() == 1:
            continue
        index = atom.GetIdx()
        one = left_conf.GetAtomPosition(index)
        two = right_conf.GetAtomPosition(index)
        squared.append((one.x - two.x) ** 2 + (one.y - two.y) ** 2 + (one.z - two.z) ** 2)
    if not squared:
        raise SubmissionError("pose has no heavy atoms")
    value = math.sqrt(sum(squared) / len(squared))
    if not math.isfinite(value):
        raise SubmissionError("pose RMSD is non-finite")
    return value


def _same_json(left: Any, right: Any) -> bool:
    return _canonical_bytes(left) == _canonical_bytes(right)


def _diversity(rows: Iterable[dict[str, Any]]) -> float:
    fingerprints = [
        AllChem.GetMorganGenerator(radius=2, fpSize=2048).GetFingerprint(row["candidate"].molecule)
        for row in rows
    ]
    if len(fingerprints) < 2:
        return 0.0
    distances = []
    for index, one in enumerate(fingerprints):
        for two in fingerprints[index + 1 :]:
            distances.append(1.0 - float(DataStructs.TanimotoSimilarity(one, two)))
    return _round8(sum(distances) / len(distances))


def score_submission(
    input_root: Path,
    submission_root: Path,
    verifier_root: Path,
    current_state: Any,
) -> dict[str, Any]:
    payloads = validate_submission_layout(submission_root)
    targets = load_targets(input_root)
    state = validate_state(current_state, input_root, targets)
    config = load_score_config(verifier_root)
    rows = parse_candidates(payloads["candidates.csv"])
    poses = parse_poses(payloads["poses.sdf"], rows)
    routes = parse_routes(submission_root, rows)
    interactions = parse_interactions(submission_root, rows)
    validate_evidence_jsonl(payloads["evidence.jsonl"], state["evaluations"])
    validate_summary(submission_root, rows, state)
    try:
        templates = load_reaction_templates(input_root)
        materials = load_starting_materials(input_root)
    except ContractError as error:
        raise RuntimeError("trusted chemistry inputs are invalid") from error
    evaluations = {record["evaluation_id"]: record for record in state["evaluations"]}
    hit_counts = {target_id: 0 for target_id in EXPECTED_TARGET_IDS}
    scores_by_target: dict[str, list[float]] = {target_id: [] for target_id in EXPECTED_TARGET_IDS}
    interaction_passes = 0
    property_passes = 0
    route_passes = 0
    row_diagnostics: list[dict[str, Any]] = []

    for row in rows:
        target_id = row["target_id"]
        target = targets[target_id]
        property_record = _evaluation(
            evaluations, row["property_evaluation_id"], "property_evaluation", row
        )
        route_record = _evaluation(
            evaluations, row["route_evaluation_id"], "route_validation", row
        )
        docking_record = _evaluation(
            evaluations, row["docking_evaluation_id"], "docking_evaluation", row
        )
        property_result = property_record["response"]["result"]
        route_result = route_record["response"]["result"]
        docking_result = docking_record["response"]["result"]
        if not all(isinstance(value, dict) for value in (property_result, route_result, docking_result)):
            raise SubmissionError("trusted accepted evaluation lacks a result object")
        if docking_result.get("property_evaluation_id") != row["property_evaluation_id"]:
            raise SubmissionError("docking/property evaluation binding differs")
        if docking_result.get("route_evaluation_id") != row["route_evaluation_id"]:
            raise SubmissionError("docking/route evaluation binding differs")
        if (
            route_result.get("candidate_key") != row["candidate_key"]
            or docking_result.get("candidate_key") != row["candidate_key"]
            or route_result.get("route_id") != row["route_id"]
            or docking_result.get("pose_id") != row["pose_id"]
        ):
            raise SubmissionError("candidate key, route, or pose differs from trusted evaluation")

        constraint_result = _constraint_result(row["candidate"], target)
        property_ok = bool(constraint_result["passed"])
        if not _same_json(constraint_result, property_result):
            raise SubmissionError("recomputed properties differ from trusted evaluation")
        if (
            abs(row["reported_qed"] - float(constraint_result["properties"]["qed"]))
            > 1e-8
            or abs(
                row["reported_clogp"]
                - float(constraint_result["properties"]["clogp"])
            )
            > 1e-8
        ):
            raise SubmissionError("self-reported molecular properties differ from recomputation")
        property_passes += int(property_ok)

        try:
            route_steps = routes[row["route_id"]].get("steps")
            route_constraints = target.get("route_constraints")
            if (
                route_constraints != {"min_steps": 1, "max_steps": 1}
                or not isinstance(route_steps, list)
                or len(route_steps) != 1
            ):
                raise SubmissionError("target and submitted route must require exactly one step")
            replay = replay_route(
                routes[row["route_id"]],
                target,
                row["canonical_smiles"],
                templates,
                materials,
            )
        except ContractError as error:
            raise SubmissionError("submitted synthesis route does not replay") from error
        if (
            replay.route_id != row["route_id"]
            or replay.candidate_key != row["candidate_key"]
            or replay.fingerprint != route_result.get("route_fingerprint")
            or replay.fingerprint != docking_result.get("route_fingerprint")
        ):
            raise SubmissionError("submitted route differs from trusted replay")
        route_passes += 1

        submitted_interaction = interactions[row["candidate_key"]]
        expected_identifiers = {
            "candidate_key": row["candidate_key"],
            "target_id": target_id,
            "pose_id": row["pose_id"],
            "docking_evaluation_id": row["docking_evaluation_id"],
        }
        if any(submitted_interaction[name] != value for name, value in expected_identifiers.items()):
            raise SubmissionError("interaction identifiers differ from candidates.csv")
        trusted_evidence = docking_result.get("interaction_evidence")
        if not isinstance(trusted_evidence, dict):
            raise SubmissionError("trusted docking interaction evidence is missing")
        if (
            submitted_interaction["interaction_fingerprint"]
            != docking_result.get("interaction_fingerprint")
            or submitted_interaction["interaction_fingerprint"]
            != trusted_evidence.get("fingerprint")
            or submitted_interaction["all_satisfied"]
            != trusted_evidence.get("all_satisfied")
            or not _same_json(submitted_interaction["groups"], trusted_evidence.get("groups"))
        ):
            raise SubmissionError("submitted interaction evidence differs from trusted result")

        submitted_pose = poses[row["candidate_key"]]
        trusted_pose_text = docking_result.get("pose_sdf")
        if not isinstance(trusted_pose_text, str):
            raise SubmissionError("trusted docking pose is missing")
        if hashlib.sha256(trusted_pose_text.encode("utf-8")).hexdigest() != docking_result.get(
            "pose_sha256"
        ):
            raise RuntimeError("trusted pose hash is inconsistent")
        trusted_pose = _parse_one_sdf(trusted_pose_text, "trusted pose")
        trusted_rmsd = direct_heavy_atom_rmsd(submitted_pose, trusted_pose)
        if trusted_rmsd > POSE_RMSD_TOLERANCE_ANGSTROM:
            raise SubmissionError("submitted pose coordinates differ from trusted validation pose")

        _, receptor_pdb_path = receptor_paths(input_root, target)
        receptor = parse_receptor_pdb(receptor_pdb_path)
        recomputed_submitted_interaction = evaluate_required_interactions(
            submitted_pose, receptor, target["required_interactions"]
        )
        if (
            recomputed_submitted_interaction["all_satisfied"]
            != trusted_evidence.get("all_satisfied")
            or not _same_json(
                recomputed_submitted_interaction["groups"], trusted_evidence.get("groups")
            )
        ):
            raise SubmissionError("interaction geometry does not match the bound pose")

        try:
            rerun = run_frozen_docking(
                row["candidate"].molecule,
                row["canonical_smiles"],
                target,
                input_root,
                pose_id=row["pose_id"],
                candidate_key=row["candidate_key"],
                docking_evaluation_id=row["docking_evaluation_id"],
            )
        except DockingError as error:
            raise SubmissionError("frozen final docking rerun failed") from error
        rerun_rmsd = direct_heavy_atom_rmsd(rerun.pose, trusted_pose)
        if rerun_rmsd > POSE_RMSD_TOLERANCE_ANGSTROM:
            raise SubmissionError("final docking rerun is not deterministic within tolerance")
        submitted_rerun_rmsd = direct_heavy_atom_rmsd(submitted_pose, rerun.pose)
        if submitted_rerun_rmsd > POSE_RMSD_TOLERANCE_ANGSTROM:
            raise SubmissionError("submitted pose differs from the final docking rerun")
        tolerance = float(config["threshold_tolerance_kcal_mol"])
        trusted_score = float(docking_result.get("vina_affinity_kcal_mol"))
        if abs(row["reported_vina_affinity_kcal_mol"] - trusted_score) > 1e-9:
            raise SubmissionError("self-reported Vina affinity differs from trusted evaluation")
        if abs(rerun.score - trusted_score) > tolerance:
            raise SubmissionError("final docking affinity differs from validation")
        if (
            rerun.interaction_fingerprint != trusted_evidence.get("fingerprint")
            or not _same_json(rerun.interaction_evidence, trusted_evidence)
        ):
            raise SubmissionError("final docking interaction evidence differs from validation")

        threshold = float(
            config["targets"][target_id]["vina_affinity_threshold_kcal_mol"]
        )
        interaction_ok = bool(rerun.interaction_evidence["all_satisfied"])
        affinity_ok = rerun.score <= threshold + tolerance
        hit = property_ok and interaction_ok and affinity_ok
        hit_counts[target_id] += int(hit)
        interaction_passes += int(interaction_ok)
        scores_by_target[target_id].append(rerun.score)
        row_diagnostics.append(
            {
                "target_id": target_id,
                "rank": row["rank"],
                "candidate_key": row["candidate_key"],
                "verified_hit": hit,
                "property_ok": property_ok,
                "route_ok": True,
                "interaction_ok": interaction_ok,
                "affinity_ok": affinity_ok,
                "vina_affinity_kcal_mol": rerun.score,
                "pose_rmsd_to_validation": _round8(rerun_rmsd),
                "submitted_pose_rmsd_to_validation": _round8(trusted_rmsd),
                "submitted_pose_rmsd_to_rerun": _round8(submitted_rerun_rmsd),
            }
        )

    per_target = {
        target_id: _round8(hit_counts[target_id] / 10.0)
        for target_id in EXPECTED_TARGET_IDS
    }
    raw = _round8(sum(per_target.values()) / len(EXPECTED_TARGET_IDS))
    submitted_count = len(rows)
    total_calls = len(state["evaluations"])
    total_hits = sum(hit_counts.values())
    mean_scores = {
        target_id: (
            _round8(sum(values) / len(values)) if values else None
        )
        for target_id, values in scores_by_target.items()
    }
    diagnostics = {
        "submitted_count": submitted_count,
        "verified_hit_count": total_hits,
        "per_target_hit_rate_at_10": per_target,
        "per_target_mean_vina_affinity_kcal_mol": mean_scores,
        "validity": _round8(1.0 if submitted_count else 0.0),
        "uniqueness": _round8(1.0 if submitted_count else 0.0),
        "property_pass_rate": _round8(property_passes / submitted_count) if submitted_count else 0.0,
        "route_replay_success_rate": _round8(route_passes / submitted_count) if submitted_count else 0.0,
        "interaction_satisfaction_rate": _round8(interaction_passes / submitted_count) if submitted_count else 0.0,
        "mean_pairwise_morgan_distance": _diversity(rows),
        "charged_scientific_calls": total_calls,
        "calls_per_verified_hit": _round8(total_calls / total_hits) if total_hits else None,
        "false_success_count": sum(
            1
            for item in row_diagnostics
            if not item["verified_hit"] and (item["affinity_ok"] or item["interaction_ok"])
        ),
        "rows": row_diagnostics,
    }
    metrics = [
        {
            "name": "macro_verified_hit_rate_at_10",
            "value": raw,
            "unit": "fraction",
        }
    ]
    for target_id in EXPECTED_TARGET_IDS:
        metrics.append(
            {
                "name": "%s_verified_hit_rate_at_10" % target_id,
                "value": per_target[target_id],
                "unit": "fraction",
            }
        )
    metrics.extend(
        [
            {"name": "verified_hit_count", "value": float(total_hits), "unit": "count"},
            {
                "name": "route_replay_success_rate",
                "value": diagnostics["route_replay_success_rate"],
                "unit": "fraction",
            },
            {
                "name": "interaction_satisfaction_rate",
                "value": diagnostics["interaction_satisfaction_rate"],
                "unit": "fraction",
            },
        ]
    )
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": True,
        "status": "valid",
        "primary_metric": {
            "name": config["primary_metric_name"],
            "value": raw,
            "direction": config["direction"],
        },
        "metrics": metrics,
        "error": None,
        "diagnostics": diagnostics,
    }


def invalid_result(message: str, config: dict[str, Any] | None = None) -> dict[str, Any]:
    name = "macro_verified_hit_rate_at_10"
    placeholder = INVALID_RAW_FALLBACK
    if config is not None:
        name = str(config["primary_metric_name"])
        placeholder = float(config["invalid_raw_value"])
    return {
        "schema_version": "1.0",
        "task_id": TASK_ID,
        "valid": False,
        "status": "invalid",
        "primary_metric": {"name": name, "value": placeholder, "direction": "maximize"},
        "metrics": [{"name": name, "value": placeholder, "unit": "fraction"}],
        "error": str(message)[:1000],
    }


def public_result(details: Mapping[str, Any]) -> dict[str, Any]:
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


__all__ = [
    "SubmissionError",
    "load_score_config",
    "score_submission",
    "invalid_result",
    "public_result",
    "direct_heavy_atom_rmsd",
]
