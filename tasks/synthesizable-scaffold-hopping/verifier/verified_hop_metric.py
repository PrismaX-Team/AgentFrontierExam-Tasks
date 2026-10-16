"""Trusted local verified_hop_metric component.

Scientific parameters are supplied by the contributor-approved version-1
configuration. No submitted code, self-reported score or witness is executed.
"""

from __future__ import annotations

from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class Assessment:
    task_id: str
    rank: int
    standardized_identity: str
    cluster_key: str | None
    verified: bool


class MetricInputError(ValueError):
    pass


def aggregate(task_ids: tuple[str, ...], assessments: list[Assessment]) -> dict:
    """Count at most one successful candidate per cluster/instance.

    Missing ranks are non-hits. This helper accepts only already-selected ranks
    1..10 and rejects duplicate ranks/identities rather than silently repairing
    them. Submission-level invalid/zero handling belongs to the future frozen
    artifact protocol and is not inferred from this arithmetic helper.
    """
    if not task_ids or any(type(t) is not str or not t for t in task_ids) or len(set(task_ids)) != len(task_ids):
        raise MetricInputError("unique nonempty frozen task IDs required")
    grouped = {task: [] for task in task_ids}
    for assessment in assessments:
        if type(assessment) is not Assessment or type(assessment.task_id) is not str or assessment.task_id not in grouped:
            raise MetricInputError("assessment must belong to a frozen instance")
        if type(assessment.rank) is not int or not 1 <= assessment.rank <= 10:
            raise MetricInputError("only trusted selected ranks 1..10 are accepted")
        if type(assessment.standardized_identity) is not str or not assessment.standardized_identity:
            raise MetricInputError("trusted standardized identity required")
        if type(assessment.verified) is not bool:
            raise MetricInputError("verified must be a trusted boolean")
        if assessment.verified and (type(assessment.cluster_key) is not str or not assessment.cluster_key):
            raise MetricInputError("verified candidate needs a frozen core-cluster key")
        if assessment.cluster_key is not None and type(assessment.cluster_key) is not str:
            raise MetricInputError("invalid core-cluster key")
        grouped[assessment.task_id].append(assessment)
    rows = []
    for task in task_ids:
        ranked = sorted(grouped[task], key=lambda a: a.rank)
        if len({a.rank for a in ranked}) != len(ranked):
            raise MetricInputError("duplicate candidate rank")
        if len({a.standardized_identity for a in ranked}) != len(ranked):
            raise MetricInputError("duplicate standardized candidate identity")
        used = set()
        credited = []
        for candidate in ranked:
            if candidate.verified and candidate.cluster_key not in used:
                used.add(candidate.cluster_key)
                credited.append(candidate.rank)
        rate = Fraction(len(credited), 10)
        rows.append({"task_id": task, "verified_new_core_clusters": len(credited),
                     "credited_ranks": credited, "raw_rate_at_10": float(rate),
                     "submitted_rank_count": len(ranked), "missing_ranks": 10 - len(ranked)})
    total = Fraction(sum(row["verified_new_core_clusters"] for row in rows), 10 * len(task_ids))
    return {"metric_name": "macro_verified_scaffold_hop_rate_at_10",
            "direction": "maximize", "value": float(total),
            "exact_fraction": f"{total.numerator}/{total.denominator}", "instances": rows}
