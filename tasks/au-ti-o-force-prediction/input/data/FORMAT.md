# Data format

All JSONL records use one object per line. The training set is split across `train-001.jsonl` and `train-002.jsonl` (384 records each); read both files to obtain the complete training set. This file split does not change the data partition. There are 768 train records, 92 labelled development records, and 92 unlabelled test records.

`species` is an ordered list of chemical symbols. `positions` contains matching Cartesian triplets. `cell` contains three lattice vectors as rows. `pbc` is a three-boolean list. Public labelled records additionally contain `energy` (total energy) and `forces` (ordered Cartesian triplets).

Numerical values retain the original file scale. See `units.json` before any physical conversion. The supplier describes the energy and force labels as atomic units; no original exporter or CP2K output has been verified. JSONL deliberately avoids implicit unit conventions of chemistry file readers.

The submission has exactly `id`, `energy`, and `forces`. The provided template uses null placeholders. These are not predictions and will be rejected until replaced. Model training and model choice are the participant's responsibility. No trained model or benchmark performance is supplied.

Exact duplicate geometries cannot cross splits. The split does not guarantee separation of similar frames or independent trajectories. No facet labels are supplied or inferred. Small composition groups with fewer than ten distinct geometries stay in training.
