from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from campaign import (
    CANDIDATE_COLUMNS,
    SubmissionError,
    _read_candidates,
    _task_auc,
    _top_candidates,
    process_validation,
    public_evidence_records,
    score_submission,
    validate_state,
)
from molecular_contract import (
    ConfigurationError,
    ContractError,
    _reaction_products,
    canonical_json,
    evaluate_molecule,
    load_bundle,
    replay_route,
    standardize_smiles,
    strict_json_loads,
)


TASK_ROOT = Path(__file__).resolve().parents[2]
INPUT_ROOT = TASK_ROOT / "input"


@pytest.fixture(scope="session")
def bundle():
    return load_bundle(INPUT_ROOT)


def _route_and_product(bundle, task_id: str, final_material: str, route_id: str):
    task = bundle.tasks[task_id]
    blueprint = task.route_blueprints[0]
    reactions = blueprint["reaction_template_ids"]
    first_material = blueprint["starting_material_slots"][0][0]

    if len(reactions) == 1:
        material_ids = [first_material, final_material]
        product = _reaction_products(
            bundle.reactions[reactions[0]],
            bundle.materials[first_material].molecule,
            bundle.materials[final_material].molecule,
        )[0]
        intermediates = []
    else:
        # The public two-step blueprint permits morpholine in its second slot.
        second_material = "amine_morpholine"
        intermediate = _reaction_products(
            bundle.reactions[reactions[0]],
            bundle.materials[first_material].molecule,
            bundle.materials[second_material].molecule,
        )[0]
        intermediate_molecule, _ = standardize_smiles(intermediate)
        product = _reaction_products(
            bundle.reactions[reactions[1]],
            intermediate_molecule,
            bundle.materials[final_material].molecule,
        )[0]
        material_ids = [first_material, second_material, final_material]
        intermediates = [intermediate]

    route = {
        "route_id": route_id,
        "task_id": task_id,
        "starting_material_ids": material_ids,
        "reaction_template_ids": list(reactions),
        "intermediates": intermediates,
        "final_product": product,
    }
    return route, product


def _request(
    bundle,
    task_id: str,
    final_material: str,
    request_id: str,
    *,
    route_id: str | None = None,
    repair_of_call: int | None = None,
):
    route, product = _route_and_product(
        bundle, task_id, final_material, route_id or "route-" + request_id
    )
    return {
        "schema_version": "1.0",
        "request_id": request_id,
        "task_id": task_id,
        "candidate_smiles": product,
        "route": route,
        "repair_of_call": repair_of_call,
    }


def _validate(request, state=None):
    return process_validation(request, INPUT_ROOT, state)


def _materialize_submission(root: Path, bundle, state) -> None:
    selected = []
    routes = []
    for task_id in bundle.task_order:
        attempts = state["tasks"][task_id]["attempts"]
        evaluated = [
            attempt
            for attempt in attempts
            if attempt["evidence"]["evaluation_index"] is not None
        ]
        evaluated.sort(
            key=lambda attempt: (
                -float(attempt["evidence"]["utility"]),
                attempt["evidence"]["candidate_smiles"],
                attempt["evidence"]["call_index"],
            )
        )
        for rank, attempt in enumerate(evaluated[: bundle.tasks[task_id].top_k], 1):
            evidence = attempt["evidence"]
            selected.append(
                [
                    task_id,
                    str(rank),
                    evidence["candidate_smiles"],
                    evidence["route_id"],
                    str(evidence["properties"]["qed"]),
                    str(evidence["properties"]["logp"]),
                    str(evidence["properties"]["tpsa"]),
                    str(evidence["utility"]),
                ]
            )
            routes.append(attempt["route"])

    root.mkdir()
    csv_lines = [",".join(CANDIDATE_COLUMNS)] + [",".join(row) for row in selected]
    (root / "candidates.csv").write_text("\n".join(csv_lines) + "\n", encoding="utf-8")
    (root / "routes.json").write_text(
        canonical_json(
            {
                "schema_version": "1.0",
                "benchmark_id": "synthesis-constrained-molecular-optimization",
                "routes": routes,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    evidence = public_evidence_records(state, bundle)
    (root / "evidence.jsonl").write_text(
        "".join(canonical_json(item) + "\n" for item in evidence), encoding="utf-8"
    )


def _one_evaluation_per_task(bundle):
    materials = {
        "scmo_amide_001": "amine_morpholine",
        "scmo_snar_amide_002": "amine_cyclopropyl",
        "scmo_sulfonamide_003": "amine_cyclopropyl",
    }
    state = None
    for index, task_id in enumerate(bundle.task_order, 1):
        response, next_state = _validate(
            _request(bundle, task_id, materials[task_id], "valid-%d" % index), state
        )
        assert response["status"] == "evaluated"
        assert next_state is not None
        state = next_state
    return state


def test_standardization_and_route_replay_are_deterministic(bundle):
    route, product = _route_and_product(
        bundle, "scmo_amide_001", "amine_morpholine", "deterministic-route"
    )
    alternate = "O=C(N1CCOCC1)c1ccc(-c2nccs2)cc1"
    _, canonical_a = standardize_smiles(product)
    _, canonical_b = standardize_smiles(alternate)
    assert canonical_a == canonical_b == route["final_product"]

    task = bundle.tasks["scmo_amide_001"]
    first = replay_route(route, product, task, bundle)
    second = replay_route(copy.deepcopy(route), canonical_b, task, bundle)
    assert first == second == {"step_products": [product], "final_product": product}


def test_tautomerization_stereo_policy_is_pinned():
    # Stereo on a bond changed by keto/enol canonicalization is intentionally removed.
    _, enol_e = standardize_smiles("C/C(O)=C/C")
    _, enol_z = standardize_smiles("C/C(O)=C\\C")
    assert enol_e == enol_z == "CCC(C)=O"

    # The alpha stereocenter can participate in tautomerization and likewise collapses.
    _, alpha_r = standardize_smiles("CC(=O)[C@H](C)CC")
    _, alpha_s = standardize_smiles("CC(=O)[C@@H](C)CC")
    assert alpha_r == alpha_s == "CCC(C)C(C)=O"

    # An unrelated alcohol stereocenter is unaffected and remains an enantiomeric pair.
    _, alcohol_r = standardize_smiles("CC[C@H](O)C")
    _, alcohol_s = standardize_smiles("CC[C@@H](O)C")
    assert alcohol_r != alcohol_s
    assert {alcohol_r, alcohol_s} == {"CC[C@H](C)O", "CC[C@@H](C)O"}


def test_legal_call_charges_route_and_property(bundle):
    request = _request(bundle, "scmo_amide_001", "amine_morpholine", "legal-1")
    response, state = _validate(request)

    assert response["status"] == "evaluated"
    assert response["charged"] == {
        "route_validation": True,
        "evaluation": True,
        "repair": False,
    }
    assert response["budget"]["route_validation_used"] == 1
    assert response["budget"]["evaluation_used"] == 1
    assert response["evaluation"]["canonical_route"] == request["route"]
    assert state["next_call_index"] == 2
    assert validate_state(state, bundle) == state


def test_request_cache_conflict_and_canonical_duplicate_do_not_charge(bundle):
    first = _request(bundle, "scmo_amide_001", "amine_morpholine", "cache-1")
    evaluated, state = _validate(first)
    assert evaluated["status"] == "evaluated"

    cached, cached_state = _validate(copy.deepcopy(first), state)
    assert cached["status"] == "cached_evaluation"
    assert cached["replayed"] is True
    assert cached["charged"] == {
        "route_validation": False,
        "evaluation": False,
        "repair": False,
    }
    assert cached_state is None

    conflict_request = _request(
        bundle, "scmo_amide_001", "amine_cyclopropyl", "cache-1", route_id="other-route"
    )
    conflict, conflict_state = _validate(conflict_request, state)
    assert conflict["status"] == "request_id_conflict"
    assert conflict["error"]["code"] == "REQUEST_ID_CONFLICT"
    assert conflict_state is None

    equivalent = copy.deepcopy(first)
    equivalent["request_id"] = "cache-equivalent"
    equivalent["candidate_smiles"] = "O=C(N1CCOCC1)c1ccc(-c2nccs2)cc1"
    equivalent["route"]["final_product"] = equivalent["candidate_smiles"]
    duplicate, duplicate_state = _validate(equivalent, state)
    assert duplicate["status"] == "cached_evaluation"
    assert duplicate["charged"]["route_validation"] is False
    assert duplicate_state is None

    mismatched = copy.deepcopy(first)
    mismatched["request_id"] = "cache-mismatched-candidate"
    _, mismatched["candidate_smiles"] = _route_and_product(
        bundle, "scmo_amide_001", "amine_methyl", "unused-route"
    )
    rejected, rejected_state = _validate(mismatched, state)
    assert rejected["status"] == "invalid_request"
    assert rejected["error"]["code"] == "CANDIDATE_ROUTE_MISMATCH"
    assert rejected["charged"]["route_validation"] is False
    assert rejected_state is None
    assert state["tasks"]["scmo_amide_001"]["evaluation_used"] == 1


def test_failed_route_charges_only_route(bundle):
    request = _request(bundle, "scmo_amide_001", "amine_methyl", "bad-route-1")
    _, wrong_product = _route_and_product(
        bundle, "scmo_amide_001", "amine_ethyl", "unused"
    )
    request["candidate_smiles"] = wrong_product
    request["route"]["final_product"] = wrong_product

    response, state = _validate(request)
    assert response["status"] == "route_rejected"
    assert response["error"]["code"] == "FINAL_PRODUCT_MISMATCH"
    assert response["charged"] == {
        "route_validation": True,
        "evaluation": False,
        "repair": False,
    }
    task_state = state["tasks"]["scmo_amide_001"]
    assert task_state["route_validation_used"] == 1
    assert task_state["evaluation_used"] == 0
    assert task_state["attempts"][0]["evidence"]["evaluation_index"] is None


def test_hard_constraint_failure_charges_both_and_has_zero_utility(bundle):
    request = _request(bundle, "scmo_amide_001", "amine_3_methoxypropyl", "hard-fail-1")
    response, state = _validate(request)

    assert response["status"] == "evaluated"
    assert response["charged"]["route_validation"] is True
    assert response["charged"]["evaluation"] is True
    assert response["evaluation"]["hard_constraint_failures"] == [
        "minimum_seed_similarity"
    ]
    assert response["evaluation"]["utility"] == 0.0
    assert state["tasks"]["scmo_amide_001"]["evaluation_used"] == 1


def test_repair_budget_is_enforced_without_overcharge(bundle):
    task_id = "scmo_amide_001"
    initial = _request(bundle, task_id, "amine_methyl", "repair-initial")
    _, wrong_product = _route_and_product(
        bundle, task_id, "amine_ethyl", "wrong-source-1"
    )
    initial["candidate_smiles"] = wrong_product
    initial["route"]["final_product"] = wrong_product
    response, state = _validate(initial)
    assert response["status"] == "route_rejected"
    assert response["error"]["code"] == "FINAL_PRODUCT_MISMATCH"
    assert response["charged"]["repair"] is False

    # A corrected declaration in the same synthesis family is a charged repair.
    corrected = _request(
        bundle,
        task_id,
        "amine_methyl",
        "repair-corrected",
        route_id="repair-route-corrected",
        repair_of_call=1,
    )
    response, next_state = _validate(corrected, state)
    assert response["status"] == "evaluated"
    assert response["charged"] == {
        "route_validation": True,
        "evaluation": True,
        "repair": True,
    }
    state = next_state

    # A successful call is not itself a valid repair target.
    invalid_target = copy.deepcopy(initial)
    invalid_target["request_id"] = "repair-invalid-target"
    invalid_target["route"]["route_id"] = "repair-invalid-target-route"
    _, invalid_target_product = _route_and_product(
        bundle, task_id, "amine_n_propyl", "wrong-source-2"
    )
    invalid_target["candidate_smiles"] = invalid_target_product
    invalid_target["route"]["final_product"] = invalid_target_product
    invalid_target["repair_of_call"] = 2
    invalid, next_state = _validate(invalid_target, state)
    assert invalid["status"] == "invalid_request"
    assert invalid["error"]["code"] == "INVALID_REPAIR_REFERENCE"
    assert invalid["charged"]["repair"] is False
    assert next_state is None

    # Three more changed declarations consume the remaining repair allowance.
    wrong_materials = ["amine_n_propyl", "amine_isopropyl", "amine_n_butyl"]
    for index, wrong_material in enumerate(wrong_materials, 2):
        repaired = copy.deepcopy(initial)
        repaired["request_id"] = "repair-%d" % index
        repaired["route"]["route_id"] = "repair-route-%d" % index
        _, declared_product = _route_and_product(
            bundle, task_id, wrong_material, "wrong-source-%d" % index
        )
        repaired["candidate_smiles"] = declared_product
        repaired["route"]["final_product"] = declared_product
        repaired["repair_of_call"] = 1
        response, next_state = _validate(repaired, state)
        assert response["status"] == "route_rejected"
        assert response["charged"]["route_validation"] is True
        assert response["charged"]["evaluation"] is False
        assert response["charged"]["repair"] is True
        state = next_state

    before = copy.deepcopy(state)
    exhausted_request = copy.deepcopy(initial)
    exhausted_request["request_id"] = "repair-exhausted"
    exhausted_request["route"]["route_id"] = "repair-route-exhausted"
    _, exhausted_product = _route_and_product(
        bundle, task_id, "amine_tert_butyl", "wrong-source-exhausted"
    )
    exhausted_request["candidate_smiles"] = exhausted_product
    exhausted_request["route"]["final_product"] = exhausted_product
    exhausted_request["repair_of_call"] = 1
    exhausted, next_state = _validate(exhausted_request, state)
    assert exhausted["status"] == "budget_exhausted"
    assert exhausted["error"]["code"] == "REPAIR_BUDGET_EXHAUSTED"
    assert exhausted["charged"] == {
        "route_validation": False,
        "evaluation": False,
        "repair": False,
    }
    assert next_state is None
    assert state == before
    assert state["tasks"][task_id]["repair_used"] == 4


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.__setitem__("next_call_index", 99),
        lambda state: state["tasks"]["scmo_amide_001"].__setitem__(
            "evaluation_used", 0
        ),
        lambda state: state["tasks"]["scmo_amide_001"]["attempts"][0][
            "evidence"
        ].__setitem__("route_fingerprint", "sha256:" + "0" * 64),
    ],
)
def test_state_tampering_is_detected(bundle, mutation):
    _, state = _validate(
        _request(bundle, "scmo_amide_001", "amine_morpholine", "tamper-1")
    )
    damaged = copy.deepcopy(state)
    mutation(damaged)
    with pytest.raises(ConfigurationError):
        validate_state(damaged, bundle)


def test_scientific_state_tampering_is_rejected_before_more_validation(bundle):
    _, state = _validate(
        _request(bundle, "scmo_amide_001", "amine_morpholine", "science-tamper-1")
    )
    damaged = copy.deepcopy(state)
    damaged["tasks"]["scmo_amide_001"]["attempts"][0]["evidence"]["utility"] = 0.123456
    with pytest.raises(ConfigurationError):
        validate_state(damaged, bundle)


def test_state_candidate_must_equal_declared_route_final(bundle):
    _, state = _validate(
        _request(bundle, "scmo_amide_001", "amine_morpholine", "identity-tamper-1")
    )
    damaged = copy.deepcopy(state)
    damaged["tasks"]["scmo_amide_001"]["attempts"][0]["evidence"][
        "candidate_smiles"
    ] = "CC"
    with pytest.raises(ConfigurationError, match="candidate and route final product"):
        validate_state(damaged, bundle)


def test_fixed_denominator_auc_zero_padding_and_hold_forward():
    history = [{"utility": 1.0}]
    auc, curve = _task_auc(history, budget=2, top_k=2)
    assert curve == [0.0, 0.5, 0.5]
    assert auc == 0.75

    auc, curve = _task_auc([], budget=16, top_k=10)
    assert curve == [0.0] * 17
    assert auc == 0.0

    perfect = [{"utility": 1.0} for _ in range(16)]
    auc, curve = _task_auc(perfect, budget=16, top_k=10)
    assert curve == [min(checkpoint, 10) / 10 for checkpoint in range(17)]
    assert auc == 1.0


def test_exact_final_artifacts_and_two_run_scoring_are_deterministic(bundle, tmp_path):
    state = _one_evaluation_per_task(bundle)
    submission = tmp_path / "submission"
    _materialize_submission(submission, bundle, state)

    first = score_submission(INPUT_ROOT, submission, state)
    second = score_submission(INPUT_ROOT, submission, copy.deepcopy(state))
    assert canonical_json(first) == canonical_json(second)
    assert first["valid"] is True
    assert 0.0 <= first["primary_metric"]["value"] <= 1.0
    assert first["diagnostics"]["candidate_count"] == 3
    assert first["diagnostics"]["evidence_count"] == 3


def test_final_requires_one_property_evaluation_per_task(bundle, tmp_path):
    request = _request(
        bundle, "scmo_amide_001", "amine_morpholine", "only-one-task"
    )
    response, state = _validate(request)
    assert response["status"] == "evaluated"
    with pytest.raises(SubmissionError, match="at least one property evaluation"):
        score_submission(INPUT_ROOT, tmp_path / "unused-submission", state)


def test_final_artifacts_must_exactly_match_trusted_history(bundle, tmp_path):
    state = _one_evaluation_per_task(bundle)

    evidence_submission = tmp_path / "bad-evidence"
    _materialize_submission(evidence_submission, bundle, state)
    evidence_path = evidence_submission / "evidence.jsonl"
    records = [json.loads(line) for line in evidence_path.read_text().splitlines()]
    records[0]["utility"] = 0.123456
    evidence_path.write_text(
        "".join(canonical_json(item) + "\n" for item in records), encoding="utf-8"
    )
    with pytest.raises(SubmissionError, match="exactly materialize"):
        score_submission(INPUT_ROOT, evidence_submission, state)

    route_submission = tmp_path / "bad-route"
    _materialize_submission(route_submission, bundle, state)
    route_path = route_submission / "routes.json"
    document = json.loads(route_path.read_text())
    document["routes"][0]["route_id"] = "untrusted-route-id"
    route_path.write_text(canonical_json(document) + "\n", encoding="utf-8")
    with pytest.raises(SubmissionError):
        score_submission(INPUT_ROOT, route_submission, state)

    candidate_submission = tmp_path / "bad-candidate"
    _materialize_submission(candidate_submission, bundle, state)
    candidate_path = candidate_submission / "candidates.csv"
    rows = candidate_path.read_text().splitlines()
    fields = rows[1].split(",")
    fields[3] = "untrusted-route-id"
    rows[1] = ",".join(fields)
    candidate_path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(SubmissionError):
        score_submission(INPUT_ROOT, candidate_submission, state)


def test_json_nan_duplicate_keys_and_traversal_identifiers_are_rejected(bundle):
    for text in ('{"value":NaN}', '{"value":1,"value":2}'):
        with pytest.raises(ContractError) as caught:
            strict_json_loads(text, "malicious")
        assert caught.value.code == "INVALID_JSON"

    request = _request(bundle, "scmo_amide_001", "amine_methyl", "traversal-1")
    request["route"]["route_id"] = "../escape"
    response, state = _validate(request)
    assert response["status"] == "invalid_request"
    assert response["error"]["code"] == "INVALID_ROUTE_SCHEMA"
    assert response["charged"]["route_validation"] is False
    assert state is None


def test_artifact_schema_nan_and_symlink_are_rejected(bundle, tmp_path):
    state = _one_evaluation_per_task(bundle)

    nan_submission = tmp_path / "nan"
    _materialize_submission(nan_submission, bundle, state)
    rows = (nan_submission / "candidates.csv").read_text().splitlines()
    fields = rows[1].split(",")
    fields[-1] = "NaN"
    rows[1] = ",".join(fields)
    (nan_submission / "candidates.csv").write_text(
        "\n".join(rows) + "\n", encoding="utf-8"
    )
    with pytest.raises(SubmissionError, match="finite"):
        score_submission(INPUT_ROOT, nan_submission, state)

    duplicate_submission = tmp_path / "duplicate-json"
    _materialize_submission(duplicate_submission, bundle, state)
    (duplicate_submission / "routes.json").write_text(
        '{"schema_version":"1.0","schema_version":"1.0",'
        '"benchmark_id":"synthesis-constrained-molecular-optimization","routes":[]}\n',
        encoding="utf-8",
    )
    with pytest.raises(SubmissionError):
        score_submission(INPUT_ROOT, duplicate_submission, state)

    symlink_submission = tmp_path / "symlink"
    _materialize_submission(symlink_submission, bundle, state)
    target = tmp_path / "real-candidates.csv"
    os.replace(symlink_submission / "candidates.csv", target)
    (symlink_submission / "candidates.csv").symlink_to(target)
    with pytest.raises(SubmissionError, match="non-symlink"):
        score_submission(INPUT_ROOT, symlink_submission, state)

    extra_submission = tmp_path / "extra-artifact"
    _materialize_submission(extra_submission, bundle, state)
    (extra_submission / "participant.py").write_text(
        "raise RuntimeError('must not execute')\n", encoding="utf-8"
    )
    with pytest.raises(SubmissionError, match="unexpected artifact"):
        score_submission(INPUT_ROOT, extra_submission, state)


@pytest.mark.parametrize("missing_name", ["candidates.csv", "routes.json", "evidence.jsonl"])
def test_every_final_artifact_is_required(bundle, tmp_path, missing_name):
    state = _one_evaluation_per_task(bundle)
    submission = tmp_path / "missing"
    _materialize_submission(submission, bundle, state)
    (submission / missing_name).unlink()

    with pytest.raises(SubmissionError, match="exactly the three declared artifacts"):
        score_submission(INPUT_ROOT, submission, state)


@pytest.mark.parametrize(
    "csv_text",
    [
        "",
        ",".join(CANDIDATE_COLUMNS) + "\n",
        "task_id,rank,canonical_smiles\nunterminated,\"quote\n",
        ",".join(CANDIDATE_COLUMNS) + "\n\n",
    ],
)
def test_empty_or_malformed_candidate_csv_is_rejected(bundle, tmp_path, csv_text):
    submission = tmp_path / "bad-csv"
    submission.mkdir()
    (submission / "candidates.csv").write_text(csv_text, encoding="utf-8")
    with pytest.raises(SubmissionError):
        _read_candidates(submission, bundle)


@pytest.mark.parametrize(
    ("field_index", "replacement", "message"),
    [
        (0, "unknown_task", "unknown task_id"),
        (2, "not-a-smiles", "cannot be parsed"),
    ],
)
def test_candidate_unknown_task_and_invalid_smiles_are_rejected(
    bundle, tmp_path, field_index, replacement, message
):
    state = _one_evaluation_per_task(bundle)
    submission = tmp_path / "bad-row"
    _materialize_submission(submission, bundle, state)
    path = submission / "candidates.csv"
    rows = path.read_text(encoding="utf-8").splitlines()
    fields = rows[1].split(",")
    fields[field_index] = replacement
    rows[1] = ",".join(fields)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")

    with pytest.raises(SubmissionError, match=message):
        _read_candidates(submission, bundle)


def test_candidate_count_above_thirty_is_rejected_before_row_processing(bundle, tmp_path):
    submission = tmp_path / "too-many"
    submission.mkdir()
    row = "unknown,1,invalid,route,0,0,0,0"
    (submission / "candidates.csv").write_text(
        ",".join(CANDIDATE_COLUMNS) + "\n" + "\n".join([row] * 31) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(SubmissionError, match="row count"):
        _read_candidates(submission, bundle)


def test_oversized_rank_is_a_normal_submission_error(bundle, tmp_path):
    state = _one_evaluation_per_task(bundle)
    submission = tmp_path / "oversized-rank"
    _materialize_submission(submission, bundle, state)
    path = submission / "candidates.csv"
    rows = path.read_text(encoding="utf-8").splitlines()
    fields = rows[1].split(",")
    fields[1] = "9" * 5000
    rows[1] = ",".join(fields)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(SubmissionError, match="from 1 through 10"):
        _read_candidates(submission, bundle)


def test_validation_unknown_task_and_invalid_smiles_are_no_charge(bundle):
    unknown = _request(bundle, "scmo_amide_001", "amine_methyl", "unknown-task-1")
    unknown["task_id"] = "unknown_task"
    unknown["route"]["task_id"] = "unknown_task"
    response, state = _validate(unknown)
    assert response["status"] == "invalid_request"
    assert response["error"]["code"] == "UNKNOWN_TASK"
    assert response["charged"] == {
        "route_validation": False,
        "evaluation": False,
        "repair": False,
    }
    assert state is None

    invalid = _request(bundle, "scmo_amide_001", "amine_methyl", "invalid-smiles-1")
    invalid["candidate_smiles"] = "not-a-smiles"
    response, state = _validate(invalid)
    assert response["status"] == "candidate_rejected"
    assert response["error"]["code"] == "INVALID_SMILES"
    assert response["charged"]["route_validation"] is False
    assert state is None


def test_scaffold_similarity_and_forbidden_substructure_failures(bundle):
    task = bundle.tasks["scmo_amide_001"]

    unrelated_molecule, unrelated_smiles = standardize_smiles("CCO")
    unrelated = evaluate_molecule(unrelated_molecule, unrelated_smiles, task)
    assert "required_scaffold" in unrelated["hard_constraint_failures"]
    assert "minimum_seed_similarity" in unrelated["hard_constraint_failures"]
    assert unrelated["utility"] == 0.0

    thiol_molecule, thiol_smiles = standardize_smiles("Sc1ccc(-c2nccs2)cc1")
    thiol = evaluate_molecule(thiol_molecule, thiol_smiles, task)
    assert "required_scaffold" not in thiol["hard_constraint_failures"]
    assert "forbidden:free_thiol" in thiol["hard_constraint_failures"]
    assert thiol["utility"] == 0.0


@pytest.mark.parametrize(
    ("field", "replacement"),
    [
        ("reaction_template_ids", ["unknown_reaction_template"]),
        ("starting_material_ids", ["a_core_acid", "c_core_sulfonyl_chloride"]),
    ],
)
def test_unknown_template_and_disallowed_material_are_no_charge(
    bundle, field, replacement
):
    request = _request(bundle, "scmo_amide_001", "amine_methyl", "allowlist-" + field)
    request["route"][field] = replacement
    response, state = _validate(request)
    assert response["status"] == "invalid_request"
    assert response["error"]["code"] == "ROUTE_NOT_ALLOWED"
    assert response["charged"]["route_validation"] is False
    assert state is None


def test_two_step_intermediate_mismatch_charges_only_route(bundle):
    request = _request(bundle, "scmo_snar_amide_002", "amine_cyclopropyl", "bad-middle-1")
    request["route"]["intermediates"] = [
        bundle.tasks["scmo_amide_001"].seed_smiles
    ]
    response, state = _validate(request)
    assert response["status"] == "route_rejected"
    assert response["error"]["code"] == "INTERMEDIATE_MISMATCH"
    assert response["charged"] == {
        "route_validation": True,
        "evaluation": False,
        "repair": False,
    }
    assert state["tasks"]["scmo_snar_amide_002"]["route_validation_used"] == 1
    assert state["tasks"]["scmo_snar_amide_002"]["evaluation_used"] == 0


def test_evaluation_budget_exhaustion_is_atomic_and_no_charge(bundle):
    task_id = "scmo_amide_001"
    materials = bundle.tasks[task_id].route_blueprints[0]["starting_material_slots"][1]
    state = None
    for index, material in enumerate(materials[:16], 1):
        response, next_state = _validate(
            _request(bundle, task_id, material, "evaluation-limit-%d" % index), state
        )
        assert response["status"] == "evaluated"
        state = next_state

    before = copy.deepcopy(state)
    response, next_state = _validate(
        _request(bundle, task_id, materials[16], "evaluation-limit-17"), state
    )
    assert response["status"] == "budget_exhausted"
    assert response["error"]["code"] == "EVALUATION_BUDGET_EXHAUSTED"
    assert response["charged"] == {
        "route_validation": False,
        "evaluation": False,
        "repair": False,
    }
    assert next_state is None
    assert state == before

    # Property-budget preflight also prevents a route that would later fail
    # replay from consuming an otherwise-remaining route unit.
    blocked_failure = _request(
        bundle, task_id, materials[17], "evaluation-limit-failed-route"
    )
    blocked_failure["candidate_smiles"] = bundle.tasks[task_id].seed_smiles
    blocked_failure["route"]["final_product"] = bundle.tasks[task_id].seed_smiles
    response, next_state = _validate(blocked_failure, state)
    assert response["status"] == "budget_exhausted"
    assert response["error"]["code"] == "EVALUATION_BUDGET_EXHAUSTED"
    assert response["charged"]["route_validation"] is False
    assert next_state is None
    assert state == before


def test_route_budget_exhaustion_is_atomic_and_no_charge(bundle):
    task_id = "scmo_amide_001"
    materials = bundle.tasks[task_id].route_blueprints[0]["starting_material_slots"][1]
    _, wrong_product = _route_and_product(
        bundle, task_id, "amine_morpholine", "wrong-product-source"
    )
    state = None
    for index, material in enumerate(materials[:20], 1):
        request = _request(bundle, task_id, material, "route-limit-%d" % index)
        request["candidate_smiles"] = wrong_product
        request["route"]["final_product"] = wrong_product
        response, next_state = _validate(request, state)
        assert response["status"] == "route_rejected"
        assert response["charged"]["route_validation"] is True
        assert response["charged"]["evaluation"] is False
        state = next_state

    before = copy.deepcopy(state)
    request = _request(bundle, task_id, materials[20], "route-limit-21")
    request["candidate_smiles"] = wrong_product
    request["route"]["final_product"] = wrong_product
    response, next_state = _validate(request, state)
    assert response["status"] == "budget_exhausted"
    assert response["error"]["code"] == "ROUTE_BUDGET_EXHAUSTED"
    assert response["charged"]["route_validation"] is False
    assert next_state is None
    assert state == before


def test_top_candidate_ties_and_csv_task_order_are_deterministic(bundle, tmp_path):
    history = [
        {"utility": 0.5, "canonical_smiles": "C", "call_index": 1},
        {"utility": 0.8, "canonical_smiles": "N", "call_index": 3},
        {"utility": 0.8, "canonical_smiles": "N", "call_index": 2},
        {"utility": 0.8, "canonical_smiles": "CC", "call_index": 4},
    ]
    assert _top_candidates(history, 4) == [history[3], history[2], history[1], history[0]]

    state = _one_evaluation_per_task(bundle)
    submission = tmp_path / "wrong-order"
    _materialize_submission(submission, bundle, state)
    path = submission / "candidates.csv"
    rows = path.read_text(encoding="utf-8").splitlines()
    rows[1], rows[2] = rows[2], rows[1]
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(SubmissionError, match="frozen task order"):
        _read_candidates(submission, bundle)

    submission = tmp_path / "wrong-rank"
    _materialize_submission(submission, bundle, state)
    path = submission / "candidates.csv"
    rows = path.read_text(encoding="utf-8").splitlines()
    fields = rows[1].split(",")
    fields[1] = "2"
    rows[1] = ",".join(fields)
    path.write_text("\n".join(rows) + "\n", encoding="utf-8")
    with pytest.raises(SubmissionError, match="consecutive ranks"):
        _read_candidates(submission, bundle)


def test_formal_validation_and_test_clis_run_with_temporary_context_stub(
    bundle, tmp_path
):
    stub_root = tmp_path / "framework-stub"
    package = stub_root / "science_innovation_exam"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "verify_context.py").write_text(
        """\
import json
import os
from pathlib import Path

class _Context:
    def __init__(self):
        self.path = Path(os.environ["TEST_VERIFY_CONTEXT_PATH"])
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"values": {}, "logs": []}

    def _save(self):
        self.path.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")

    def get(self, key):
        return self.data["values"].get(key)

    def __setitem__(self, key, value):
        self.data["values"][key] = value
        self._save()

    def log(self, value):
        self.data["logs"].append(value)
        self._save()

class VerifyContext:
    @staticmethod
    def current():
        return _Context()
""",
        encoding="utf-8",
    )
    context_path = tmp_path / "verify-context.json"
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["TEST_VERIFY_CONTEXT_PATH"] = str(context_path)
    python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(stub_root) + (
        os.pathsep + python_path if python_path else ""
    )

    materials = {
        "scmo_amide_001": "amine_morpholine",
        "scmo_snar_amide_002": "amine_cyclopropyl",
        "scmo_sulfonamide_003": "amine_cyclopropyl",
    }
    validation_main = TASK_ROOT / "verifier" / "validation" / "run" / "main.py"
    for index, task_id in enumerate(bundle.task_order, 1):
        request_path = tmp_path / ("cli-request-%d.json" % index)
        response_path = tmp_path / ("cli-response-%d.json" % index)
        request_path.write_text(
            canonical_json(
                _request(bundle, task_id, materials[task_id], "cli-valid-%d" % index)
            )
            + "\n",
            encoding="utf-8",
        )
        completed = subprocess.run(
            [
                sys.executable,
                str(validation_main),
                "--input",
                str(INPUT_ROOT),
                "--request",
                str(request_path),
                "--response",
                str(response_path),
            ],
            check=False,
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
        )
        assert completed.returncode == 0, completed.stderr
        assert json.loads(response_path.read_text(encoding="utf-8"))["status"] == "evaluated"

    context_document = json.loads(context_path.read_text(encoding="utf-8"))
    state = context_document["values"][
        "synthesis_constrained_molecular_optimization_v1"
    ]
    submission = tmp_path / "cli-submission"
    _materialize_submission(submission, bundle, state)
    result_path = tmp_path / "cli-result.json"
    test_main = TASK_ROOT / "verifier" / "test" / "run" / "main.py"
    completed = subprocess.run(
        [
            sys.executable,
            str(test_main),
            "--input",
            str(INPUT_ROOT),
            "--submission",
            str(submission),
            "--result",
            str(result_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["valid"] is True
    assert result["status"] == "valid"
    context_document = json.loads(context_path.read_text(encoding="utf-8"))
    assert [item["phase"] for item in context_document["logs"]] == [
        "validation",
        "validation",
        "validation",
        "test",
    ]

    empty_submission = tmp_path / "cli-empty-submission"
    empty_submission.mkdir()
    invalid_result_path = tmp_path / "cli-invalid-result.json"
    completed = subprocess.run(
        [
            sys.executable,
            str(test_main),
            "--input",
            str(INPUT_ROOT),
            "--submission",
            str(empty_submission),
            "--result",
            str(invalid_result_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 0, completed.stderr
    invalid_result = json.loads(invalid_result_path.read_text(encoding="utf-8"))
    assert invalid_result["valid"] is False
    assert invalid_result["status"] == "invalid"
    assert invalid_result["primary_metric"]["value"] == -1.0


def test_validation_log_failure_preserves_charged_response_and_state(bundle, tmp_path):
    stub_root = tmp_path / "failing-log-framework"
    package = stub_root / "science_innovation_exam"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "verify_context.py").write_text(
        """\
import json
import os
from pathlib import Path

class _Context:
    def __init__(self):
        self.path = Path(os.environ["TEST_VERIFY_CONTEXT_PATH"])
        if self.path.exists():
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        else:
            self.data = {"values": {}}

    def get(self, key):
        return self.data["values"].get(key)

    def __setitem__(self, key, value):
        self.data["values"][key] = value
        self.path.write_text(json.dumps(self.data, sort_keys=True), encoding="utf-8")

    def log(self, value):
        raise RuntimeError("injected ancillary log failure")

class VerifyContext:
    @staticmethod
    def current():
        return _Context()
""",
        encoding="utf-8",
    )
    context_path = tmp_path / "failing-log-context.json"
    request_path = tmp_path / "failing-log-request.json"
    response_path = tmp_path / "failing-log-response.json"
    request_path.write_text(
        canonical_json(
            _request(
                bundle,
                "scmo_amide_001",
                "amine_morpholine",
                "failing-log-request",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["TEST_VERIFY_CONTEXT_PATH"] = str(context_path)
    prior_python_path = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = str(stub_root) + (
        os.pathsep + prior_python_path if prior_python_path else ""
    )
    completed = subprocess.run(
        [
            sys.executable,
            str(TASK_ROOT / "verifier" / "validation" / "run" / "main.py"),
            "--input",
            str(INPUT_ROOT),
            "--request",
            str(request_path),
            "--response",
            str(response_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
        timeout=30,
    )
    assert completed.returncode == 2
    response = json.loads(response_path.read_text(encoding="utf-8"))
    assert response["status"] == "evaluated"
    assert response["charged"] == {
        "route_validation": True,
        "evaluation": True,
        "repair": False,
    }
    state = json.loads(context_path.read_text(encoding="utf-8"))["values"][
        "synthesis_constrained_molecular_optimization_v1"
    ]
    assert state["tasks"]["scmo_amide_001"]["evaluation_used"] == 1
