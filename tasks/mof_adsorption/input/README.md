# Large-framework MOF extrapolation: initial structure to relaxed adsorption energy

## Scientific target

Predict the relaxed adsorption energy of every test system under the original ODAC23 calculation protocol. The input is an initial, unrelaxed periodic structure containing a metal-organic framework (MOF) and CO2/H2O guests. The target is the adsorption energy of the complete system in eV, not energy per atom or mean energy per guest molecule.

This task freezes the ODAC23 IS2RE large-framework extrapolation split. It measures prediction of existing computational labels; it does not directly measure experimental adsorption capacity, selectivity, or separation performance. ODAC25 subsequently documented artifacts associated with the earlier empty-MOF reference states. This task retains the original ODAC23 labels and does not interpret them as corrected physical reference values or global-minimum energies.

## Files

| Path | Contents |
|---|---|
| `train/part-*.jsonl.gz` | Gzip JSONL shards, read in sorted filename order, containing all 162,224 official training initial structures and adsorption-energy labels; each shard is smaller than 50 MiB |
| `development.jsonl.gz` | All 3,998 official validation initial structures and labels, available for your own development evaluation |
| `test.jsonl.gz` | All 1,768 official ood_big initial structures, without energy labels |
| `submission.csv` | A blank prediction template containing all test IDs |

Each decompressed JSONL line is one JSON object. Python's standard-library `gzip` and `json` modules can read these files; PyTorch and LMDB are not required for decoding them. Read every training shard in sorted filename order. Sharding changes storage only: it does not sample, reorder, or modify records. Stream the records instead of loading all training structures at once.

## Record fields

- `system_id`: anonymous system identifier; copy it exactly into the submission.
- `framework_id`: anonymous source framework-name identifier. Different guest arrangements or initial configurations associated with the same source framework share it.
- `parent_framework_id`: anonymous parent-name identifier, derived by removing the defect suffix according to the source naming convention. It is auditable grouping information, not a claim of complete crystallographic deduplication.
- `atomic_numbers`: integer atomic numbers in the original atom order.
- `positions_angstrom`: initial Cartesian coordinates, N by 3, in angstrom.
- `cell_angstrom`: 3 by 3 cell matrix in angstrom; each row is a lattice vector.
- `pbc`: periodic boundary conditions in the three directions, all `true`.
- `source_atom_tags`: original atom groups: 0 for framework atoms, 1 for CO2 atoms, and 2 for H2O atoms.
- `nco2` and `nh2o`: numbers of CO2 and H2O guest molecules in the complete system.
- `adsorption_energy_eV`: available only in training and development records; the original ODAC23 full-system `y_relaxed` target.

The test input contains no raw source names, sid/fid identifiers, initial or total energies, target energies, relaxed coordinates, or force labels.

## Frozen split and evaluation

The test preserves the authors' `ood_big` split: 85 source framework names and 66 parent names derived using the source naming convention. Training, development, and test have no shared source framework identifiers, derived parent identifiers, or exactly identical exported initial geometries.

The authors' large-framework designation concerns the source framework unit cell. Exported simulation atom counts also depend on supercells, defects, and guests, so the training and test atom-count ranges overlap. Do not rebuild the test set by filtering exported atom counts. This task does not claim that every test structure has an unseen linker or topology.

Submit a CSV with exactly the columns `system_id,adsorption_energy_eV`, each test ID exactly once, and finite numerical energy predictions. The primary metric is mean absolute error over all 1,768 test systems, in eV; lower is better. Evaluation does not normalize targets or errors by atom or guest count, clip targets, or omit difficult cases. A method may use suitable internal transformations, but submitted values must be restored to full-system energy units.

The task has no mid-run check and provides no hidden-label scientific feedback during research. The final test entry checks the frozen submission's format and coverage and computes the official raw score. You may use the visible development labels for your own evaluation.

Use only the scientific inputs and resources explicitly allowed by the task. Do not retrieve public test answer tables, relaxed test structures, or test trajectories, and do not use test labels to select models, hyperparameters, or checkpoints. Because the underlying data are public, anonymous IDs do not establish absence of pretraining contamination. Any additional external model or dataset must have its training provenance disclosed and be approved under the organizer's resource policy.

## Attribution and license

Data source: Anuroop Sriram, Sihoon Choi, Xiaohan Yu, Logan M. Brabson, Abhishek Das, Zachary Ulissi, Matt Uyttendaele, Andrew J. Medford, and David S. Sholl, *The Open DAC 2023 Dataset and Challenges for Sorbent Discovery in Direct Air Capture*, DOI: [10.1021/acscentsci.3c01629](https://doi.org/10.1021/acscentsci.3c01629). ODAC23 is licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). This package filters initial-structure fields, anonymizes identifiers, separates test labels, and converts the data to JSONL/CSV. The original authors do not endorse this package or submitted models.
