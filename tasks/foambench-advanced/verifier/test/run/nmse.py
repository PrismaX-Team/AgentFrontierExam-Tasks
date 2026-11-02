#!/usr/bin/env python3
"""FoamBench NMSE, adapted to require complete fields and spatial correspondence.

Derived from NLR-Theseus/cfdllmbench FoamBench/nmse_report.py and
score_calculation.py (BSD-3-Clause; ../data/LICENSE-CFDLLMBench.txt).
The upstream time selection, bounding-box alignment, Gaussian interpolation
parameters and success threshold are retained. Array order alone never
establishes cell correspondence. Diagnostics distinguish bad reference data
(exit 2) from failed candidate results (JSON with nmse=9999).
"""
from __future__ import annotations
import json
import math
import sys
from pathlib import Path
import numpy as np
import pyvista as pv
from vtkmodules.vtkCommonDataModel import vtkStaticPointLocator

FAILURE = 9999.0
FIELDS = ("U", "p", "rho", "T")
COORD_ATOL = 1e-6
COORD_RTOL = 1e-5
INTERPOLATION_POINTS = 50
INTERPOLATION_SHARPNESS = 0.1


def calculate_nmse(reference, candidate):
    reference = np.asarray(reference, dtype=np.float64)
    candidate = np.asarray(candidate, dtype=np.float64)
    if reference.shape != candidate.shape or not np.isfinite(candidate).all():
        raise ValueError("field shape mismatch or non-finite candidate values")
    if not np.isfinite(reference).all():
        raise ValueError("non-finite reference values")
    numerator = float(np.sum((reference - candidate) ** 2))
    denominator = float(np.sum(reference ** 2))
    if denominator == 0:
        return 0.0 if numerator == 0 else FAILURE
    value = numerator / denominator
    return value if math.isfinite(value) else FAILURE


def align_and_scale_mesh(reference, candidate):
    gt = np.asarray(reference.bounds).reshape(3, 2)
    src = np.asarray(candidate.bounds).reshape(3, 2)
    if np.allclose(gt, src, atol=COORD_ATOL, rtol=COORD_RTOL):
        return candidate, False
    size = src[:, 1] - src[:, 0]
    scale = np.divide(gt[:, 1] - gt[:, 0], size, out=np.ones(3), where=size != 0)
    result = candidate.copy()
    result.points = (result.points - src[:, 0]) * scale + gt[:, 0]
    return result, True


def compare_meshes(reference, candidate, required_fields):
    for mesh, label in ((reference, "reference"), (candidate, "candidate")):
        if mesh.n_cells == 0 or not np.isfinite(mesh.points).all():
            raise ValueError(f"{label}: empty or non-finite mesh")
        for field in required_fields:
            if field not in mesh.cell_data:
                raise ValueError(f"{label}: missing required field {field}")
            data = np.asarray(mesh.cell_data[field])
            shape = (mesh.n_cells, 3) if field == "U" else (mesh.n_cells,)
            if data.shape != shape or not np.isfinite(data).all():
                raise ValueError(f"{label}: invalid shape or values in {field}")
    candidate, aligned = align_and_scale_mesh(reference, candidate)
    target_centers = reference.cell_centers().points
    source_centers = candidate.cell_centers().points
    order = None
    if reference.n_cells == candidate.n_cells:
        cloud = pv.PolyData(source_centers)
        locator = vtkStaticPointLocator()
        locator.SetDataSet(cloud)
        locator.BuildLocator()
        indices = np.asarray([locator.FindClosestPoint(p) for p in target_centers])
        if (len(np.unique(indices)) == reference.n_cells and
                np.allclose(target_centers, source_centers[indices],
                            atol=COORD_ATOL, rtol=COORD_RTOL)):
            order = indices
    scores = {}
    projected = None
    if order is None:
        # VTK interpolation reads POINT arrays. Convert source cell fields
        # explicitly; never retain reference arrays on the target mesh.
        source = candidate.copy()
        source.clear_data()
        for field in required_fields:
            source.cell_data[field] = candidate.cell_data[field]
        source = source.cell_data_to_point_data()
        for field in required_fields:
            if field in candidate.point_data:
                source.point_data[field] = candidate.point_data[field]
        target = reference.copy()
        target.clear_data()
        projected = target.interpolate(source, sharpness=INTERPOLATION_SHARPNESS,
                                       n_points=INTERPOLATION_POINTS).point_data_to_cell_data()
    for field in required_fields:
        values = candidate.cell_data[field][order] if order is not None else projected.cell_data[field]
        scores[field] = calculate_nmse(reference.cell_data[field], values)
    value = float(np.mean(list(scores.values())))
    return value, {"field_nmse": scores, "mapping": "matched_cell_centers" if order is not None else "spatial_interpolation",
                   "bounds_aligned": aligned, "reference_cells": reference.n_cells, "candidate_cells": candidate.n_cells}


def read_case(directory, target_time=None):
    path = Path(directory) / "review.foam"
    path.touch()
    reader = pv.OpenFOAMReader(str(path))
    # Keep the original reader-generated point arrays for upstream interpolation.
    reader.cell_to_point_creation = True
    times = sorted(t for t in reader.time_values if math.isfinite(t))
    if not times:
        raise ValueError("no finite output time")
    selected = times[-1] if target_time is None else min(times, key=lambda t: abs(t-target_time))
    reader.set_active_time_value(selected)
    blocks = reader.read()
    mesh = blocks["internalMesh"]
    if mesh is None or mesh.n_cells == 0:
        raise ValueError("missing internal mesh")
    return mesh, selected


def evaluate_nmse(gt_dir, llm_dir, required_fields=None):
    reference, final_time = read_case(gt_dir)
    required = required_fields or [f for f in FIELDS if f in reference.cell_data]
    candidate, selected = read_case(llm_dir, final_time)
    value, _ = compare_meshes(reference, candidate, required)
    return value


def score_nmse(value):
    return 1.0 if value < 0.1 else 0.5 if value < 0.3 else 0.0


def main(argv):
    if len(argv) != 4:
        print("usage: nmse.py reference candidate U,p[,rho,T]", file=sys.stderr)
        return 2
    required = argv[3].split(",")
    try:
        reference, final_time = read_case(argv[1])
        if not required or any(f not in FIELDS for f in required):
            raise ValueError("invalid required fields")
        # Validate the reference independently of the candidate.
        compare_meshes(reference, reference, required)
    except Exception as error:
        print(f"reference/configuration failure: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    diagnostics = {"reference_time": final_time, "required_fields": required}
    try:
        candidate, selected = read_case(argv[2], final_time)
        diagnostics["candidate_time"] = selected
        value, details = compare_meshes(reference, candidate, required)
        diagnostics.update(details)
    except Exception as error:
        value = FAILURE
        diagnostics["reason"] = f"{type(error).__name__}: {error}"
    print(json.dumps({"nmse": value, "nmse_score": score_nmse(value), **diagnostics}, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
