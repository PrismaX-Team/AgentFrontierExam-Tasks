"""Score complete-set vertical gaps while keeping references outside inference."""
import math

from frozen_graph_contract import ProtocolError, scientific_number


def inference(hidden, references, solution, context, session_type):
    """Validate every final prediction, then compute record and formula MAE."""
    records = hidden["records"]
    sample_ids = {record["sample_id"] for record in records}
    if not records or len(sample_ids) != len(records):
        raise ProtocolError("trusted test IDs incomplete or nonunique")

    with session_type(solution) as session:
        response = session.call(dict(action="predict", records=records))
    if set(response) != {"predictions"} or not isinstance(response["predictions"], list):
        raise ProtocolError("prediction response must contain exactly the predictions list")

    predictions = {}
    for row in response["predictions"]:
        if not isinstance(row, dict) or set(row) != {"sample_id", "predicted_gap_kcal_mol"}:
            raise ProtocolError("invalid prediction fields")
        if not isinstance(row["sample_id"], str) or row["sample_id"] in predictions:
            raise ProtocolError("invalid or duplicate prediction ID")
        predictions[row["sample_id"]] = scientific_number(row["predicted_gap_kcal_mol"])
    if set(predictions) != sample_ids:
        raise ProtocolError("missing or extra prediction IDs")

    truth = {
        row["sample_id"]: scientific_number(row["gap_kcal_mol"])
        for row in references["records"]
    }
    if len(truth) != len(references["records"]) or set(truth) != sample_ids:
        raise ProtocolError("trusted reference IDs incomplete or nonunique")
    absolute_errors = {
        sample_id: abs(predictions[sample_id] - truth[sample_id])
        for sample_id in sample_ids
    }
    if any(not math.isfinite(error) for error in absolute_errors.values()):
        raise ProtocolError("prediction error overflow")

    groups = {}
    for row in records:
        groups.setdefault(row["group_id"], []).append(row["sample_id"])
    group_mae = {
        group_id: math.fsum(absolute_errors[sample_id] / len(members) for sample_id in members)
        for group_id, members in groups.items()
    }
    count = len(sample_ids)
    scale = max(absolute_errors.values())
    # Scaling before squaring avoids overflow without changing the RMSE definition.
    rmse = 0.0 if scale == 0 else scale * math.sqrt(
        math.fsum((error / scale) ** 2 / count for error in absolute_errors.values())
    )

    context["fixed_geometry_spin_v1:predictions"] = response["predictions"]
    context.log(dict(
        phase="test_predictions",
        predictions=response["predictions"],
        group_sizes={group_id: len(members) for group_id, members in groups.items()},
        group_mae=group_mae,
    ))
    return dict(
        mae_kcal_mol=(math.fsum(error / count for error in absolute_errors.values()), "kcal/mol"),
        rmse_kcal_mol=(rmse, "kcal/mol"),
        macro_parent_mae_kcal_mol=(
            math.fsum(error / len(group_mae) for error in group_mae.values()), "kcal/mol"
        ),
        worst_parent_mae_kcal_mol=(max(group_mae.values()), "kcal/mol"),
        parent_group_count=(len(groups), "groups"),
    )
