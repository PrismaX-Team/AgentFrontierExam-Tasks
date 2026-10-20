#!/usr/bin/env python3
"""FoamBench NMSE diagnostic (reported only; never part of the primary score).

The functions between the markers are copied verbatim from FoamBench
(https://github.com/NLR-Theseus/cfdllmbench, FoamBench/nmse_report.py and
score_calculation.py, BSD-3-Clause; see ../data/LICENSE-CFDLLMBench.txt).
Only FoamBench's directory-walking helpers (get_inner_run_folder,
process_all) are left out.
The test entry runs this file as a separate process with a timeout:

    python3 -B nmse.py <reference case dir> <run case dir>

and reads one JSON object {"nmse": value, "nmse_score": score} from stdout. Exceptions that
FoamBench's own report script would not catch, and non-finite values, are
reported as 9999, FoamBench's failure value.
"""
from __future__ import annotations

import json
import math
import sys

import numpy as np
import pyvista as pv


# ---- begin verbatim FoamBench/nmse_report.py -------------------------------
import os


def touch_foam_file(directory, file_name):
    foam_path = os.path.join(directory, file_name)
    with open(foam_path, 'w') as f:
        pass
    return foam_path

def calculate_nmse(array1, array2):
    return np.sum((array1 - array2) ** 2) / np.sum(array1 ** 2)

def align_and_scale_mesh(gt_mesh, meta_mesh):
    gt_bounds = np.array(gt_mesh[0].bounds).reshape(3, 2)
    meta_bounds = np.array(meta_mesh[0].bounds).reshape(3, 2)

    if np.allclose(gt_bounds, meta_bounds, atol=1e-6):
        return meta_mesh

    shift = gt_bounds[:, 0] - meta_bounds[:, 0]
    gt_size = gt_bounds[:, 1] - gt_bounds[:, 0]
    meta_size = meta_bounds[:, 1] - meta_bounds[:, 0]
    scaling = np.divide(gt_size, meta_size, out=np.ones_like(gt_size), where=meta_size != 0)

    aligned_meta_mesh = meta_mesh[0].copy()
    aligned_meta_mesh.points = (aligned_meta_mesh.points - meta_bounds[:, 0]) * scaling + gt_bounds[:, 0]
    return [aligned_meta_mesh]

def evaluate_nmse(gt_dir, llm_dir):
    fields = ["U", "p", "rho", "T"]
    gt_foam = touch_foam_file(gt_dir, os.path.basename(gt_dir) + ".foam")
    llm_foam = touch_foam_file(llm_dir, os.path.basename(llm_dir) + ".foam")

    try:
        gt_reader = pv.OpenFOAMReader(gt_foam)
        last_time = gt_reader.time_values[-1]
        gt_reader.set_active_time_value(last_time)
        meta_reader = pv.OpenFOAMReader(llm_foam)
        closest_time = min(meta_reader.time_values, key=lambda x: abs(x - last_time))
        meta_reader.set_active_time_value(closest_time)
    except:
        return 9999

    gt_mesh = gt_reader.read()
    meta_mesh = meta_reader.read()
    meta_mesh = align_and_scale_mesh(gt_mesh, meta_mesh)

    scores = []
    for field in fields:
        if field in gt_mesh[0].cell_data and field in meta_mesh[0].cell_data:
            try:
                if gt_mesh[0].n_cells != meta_mesh[0].n_cells:
                    try:
                        OG_mesh = gt_mesh.copy()
                        if field in OG_mesh[0].point_data:
                            del OG_mesh[0].point_data[field]
                        if field in OG_mesh[0].cell_data:
                            del OG_mesh[0].cell_data[field]
                        projected_mesh = [OG_mesh[0].interpolate(meta_mesh[0], sharpness=0.1, n_points=50)]
                        projected_mesh[0] = projected_mesh[0].point_data_to_cell_data()
                        meta_data = projected_mesh[0][field]
                    except:
                        scores.append(9999)
                        continue
                else:
                    meta_data = meta_mesh[0][field]

                gt_data = gt_mesh[0][field]
                score = calculate_nmse(gt_data, meta_data)
                scores.append(score)
            except:
                scores.append(9999)
    if scores:
        return np.mean(scores)
    else:
        return 9999
# ---- end verbatim FoamBench/nmse_report.py ---------------------------------


# ---- begin verbatim FoamBench/score_calculation.py -------------------------
# Helper: NMSE threshold scoring
def score_nmse(val):
    if val < 0.1:
        return 1.0
    elif val < 0.3:
        return 0.5
    else:
        return 0.0
# ---- end verbatim FoamBench/score_calculation.py ---------------------------

FAILURE = 9999.0


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: nmse.py <reference case dir> <run case dir>", file=sys.stderr)
        return 2
    try:
        value = float(evaluate_nmse(argv[1], argv[2]))
    except Exception:  # FoamBench's report script would crash here; count as failure
        value = FAILURE
    if not math.isfinite(value):
        value = FAILURE
    sys.stdout.write(json.dumps({"nmse": value, "nmse_score": score_nmse(value)}) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
