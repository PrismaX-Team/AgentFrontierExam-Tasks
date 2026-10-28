# Crystal stability from initial geometries

## Scientific input and target

`test_structures.jsonl.gz` contains 215,488 initial periodic WBM structures in the fixed unique-prototype subset. Each line contains only `material_id`, integer `atomic_numbers`, Cartesian `positions_angstrom`, row-vector `cell_angstrom`, and `pbc=[true,true,true]`. Positions and cells are in angstrom. These are initial coordinates, not reference DFT-relaxed geometries. Anonymous IDs are immutable; row order does not affect scoring.

Predict final MP2020-corrected formation energy in eV/atom. The evaluator compares your prediction with a frozen Materials Project reference hull energy: at or below the hull means predicted stable. True stability uses the released signed distance at or below zero, including negative distances. No energies/distances are rounded before classification. F1 is `2 TP/(2 TP+FP+FN)`, or zero when its denominator is zero. All samples are scored; no outlier or successful-relaxation filter applies. Test hull energies and labels are unavailable during research.

`submission.csv` provides every test ID with a blank prediction. Return exactly `material_id,e_form_eV_per_atom`, one finite signed prediction per ID. Blank, duplicate, missing, unknown or nonfinite entries invalidate the submission. Physically poor finite predictions receive their numerical score without unit conversion.

## Allowed training resources

`training_compositions.jsonl.gz` contains 154,718 released MP GGA/GGA+U compositions and formation energies in eV/atom. Fields are `material_id`, unique `atomic_numbers`, positive `stoichiometry` coefficients and `e_form_eV_per_atom`. Normalize stoichiometry to fractions when needed. No structures or forces accompany this auxiliary table. Training/test compositions may overlap; do not claim unseen-composition evaluation. You may create internal development splits from visible training data.

`elemental_reference_energies.json` provides 89 archived elemental energy zeroes in eV/atom. Formation energy is corrected energy per atom minus composition-weighted elemental reference energies. No candidate-specific corrections or reference hull energies are provided.

The allowed CHGNet 0.3.0 model has 412,525 parameters, pretrained on MP GGA/GGA+U energy, force, stress and magnetic-moment trajectories. Outputs include energy in eV/atom, forces in eV/angstrom and stress in GPa. Its energy training already includes MP2020 corrections. Do not apply those corrections again to CHGNet predictions. Any internally developed raw-energy method must establish and disclose its own consistent correction convention.

Invoke `/opt/sie-research/bin/python` for the isolated scientific runtime. Load `model_weights/chgnet_0.3.0_e29f68s314m37.pth.tar` using `CHGNet.from_file(checkpoint_path, mlp_out_bias=False, version="0.3.0")`. Select CPU when no GPU is allocated. No remote model download is required.

Possible research steps are constructing periodic structures, relaxing coordinates/cells with the model, extracting resulting energy per atom and subtracting provided elemental reference energies. The visible MP table supports composition-based calibration and internal assessment. Relaxation failures, force convergence, energy calibration and uncertainty are legitimate research directions. Retain a finite label-free fallback and disclose convergence settings. Do not use hidden reference hulls or test scores to choose them.

## Scope and public-data limitations

This concerns frozen zero-temperature DFT-PBE(+U) thermodynamic stability, not experimental synthesizability, finite-temperature stability or safety. CHGNet pretraining may overlap the MP training data and chemical systems. Public WBM labels, anonymous IDs and the unique-prototype mask do not establish that a model has never encountered test answers. This package must not be described as secret or contamination-free. There are no mid-run scientific checks.

## Attribution and license

Data derive from the WBM benchmark by Hai-Chen Wang, Silvana Botti and Miguel A. L. Marques, and MP data redistributed with Matbench Discovery. Source data are CC BY 4.0: https://creativecommons.org/licenses/by/4.0/ . Dataset DOI: https://doi.org/10.6084/m9.figshare.22715158 . WBM paper: https://doi.org/10.1038/s41524-020-00481-6 . Matbench Discovery paper: https://doi.org/10.1038/s42256-025-01055-1 . Materials Project: https://doi.org/10.1063/1.4812323 . Retain attribution, links and this modification notice.

Changes: retained the fixed 215,488-entry unique-prototype mask; anonymized IDs; exported initial geometry/species only; removed candidate reference energies/distances and relaxed structures from test input; reduced auxiliary MP data to compositions/formation energies and elemental references. No new reference first-principles calculations were performed. The checkpoint/software notice is BSD-3-Clause; its underlying training corpus is not redistributed or relicensed. Original authors do not endorse this package or submitted models.
