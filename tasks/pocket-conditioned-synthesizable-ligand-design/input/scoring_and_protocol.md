# Validation, docking, synthesis, and scoring protocol

This document is normative together with `targets.jsonl`,
`starting_materials.json`, and `reaction_templates.json`. Field names and IDs are
case-sensitive. All interval endpoints and distance cutoffs are inclusive.

## Scope and public instances

The three targets, in scoring order, are `trypsin_3ptb`, `abl1_1iep`, and
`brd4_3mxf`. Each target has a fixed prepared receptor, a 22 Angstrom cubic
search box, explicit property constraints, one required-interaction group, one
reaction template, and three independent
scientific budgets:

| Request type | Budget per target |
| --- | ---: |
| `property_evaluation` | 16 |
| `route_validation` | 16 |
| `docking_evaluation` | 12 |

The finite public product universe contains 20 trypsin, 35 ABL1, and 20 BRD4
one-step products (75 total). The challenge is to select which candidates
merit the scarcer docking calls and to rank the
verified candidates. This pilot evaluates only the supplied one-step amidation
space; it does not claim general retrosynthesis.

The target-specific docking acceptance values are not present in Agent-visible
input. A trusted final-test configuration looks them up by `target_id`. They
must be frozen before evaluation from same-protocol reference/pilot results;
the final verifier must fail closed if that trusted configuration is absent.
Validation reports raw docking and interaction evidence but never reports the
hidden hit decision, threshold, witness, or final score.

## Molecular parsing, standardization, and identity

RDKit 2025.09.6 is the sole molecular graph implementation. A candidate is
strictly sanitized and must be one connected covalent fragment with 1--80 heavy
atoms. Query atoms, atom maps, isotopes, radicals, and elements outside C, N, O,
F, P, S, Cl, and Br are rejected. Total formal charge must lie in `[-1,+1]`;
the tighter target-specific range is then applied.

The **evaluation graph** is produced by RDKit `Cleanup`, sanitized `RemoveHs`,
and canonical isomeric SMILES generation. Submitted candidate, intermediate,
and final-product SMILES must already equal this canonical evaluation SMILES.
The submitted charge, tautomer, and defined stereochemistry are preserved for
property calculation, reaction replay, conformer generation, and docking.

The **identity graph**, used only for duplicate detection and cache keys, is
derived from the evaluation graph by `ChargeParent`, the pinned default
`TautomerEnumerator` canonical tautomer, sanitized `RemoveHs`, and canonical
isomeric SMILES. `MaxTautomers` and `MaxTransforms` are 1000 and the enumerator
flags are `RemoveBondStereo=true`, `RemoveSp3Stereo=true`, and
`ReassignStereo=true`. Thus salts, equivalent protonation states, and canonical
tautomers cannot be used as duplicate queries or duplicate ranked candidates.

Property fields are `molecular_weight`, `clogp` (RDKit Crippen cLogP), `tpsa`,
`qed`, `fraction_csp3`, `hbd`, `hba`, `rotatable_bonds`, `aromatic_rings`,
`heavy_atoms`, and `formal_charge`. Float properties are rounded half-even to
eight decimal places before inclusive comparisons. The public constraints in
`targets.jsonl` are hard.

## Validation request API

`validation_request.json` is an illustrative array. An actual validation call
submits exactly one array member as the JSON request; the array wrapper is not
part of the API. A request is at most 65,536 UTF-8 bytes, contains no duplicate
JSON keys or non-finite numbers, and is no deeper than 24 levels. IDs are at
most 64 characters. `candidate_key` is a submitter-chosen stable local ID that
matches `cand_[a-z0-9_]{1,48}`.

Every request has exactly these common fields:

```text
schema_version, request_id, request_type, target_id, candidate_smiles
```

`schema_version` is `1.0`. The three request types add:

- `property_evaluation`: no fields.
- `route_validation`: one `route` object as defined below.
- `docking_evaluation`: `property_evaluation_id` and
  `route_evaluation_id`, both referring to accepted evaluations of the same
  target, molecular identity, and exact evaluation-canonical graph in trusted
  state.

Malformed JSON, unknown fields or IDs, an invalid molecule, a conflicting
reuse of `request_id`, or invalid prerequisite references return `invalid` and
consume no scientific budget. An exact cached request or an equivalent target,
request-type, identity, and scientific payload returns its immutable cached
evaluation and consumes no additional budget. A charge/protonation or tautomer
alias that collapses to an already queried identity but preserves a different
evaluation graph is rejected without another call rather than receiving a
scientifically mismatched cached property result. Otherwise a scientific unit is
charged atomically when its computation starts. A route replay that yields no
product and a docking process that starts but later reports a tool failure are
therefore charged; a budget check or schema rejection before execution is not.

Responses have a stable JSON object and distinguish `accepted`, `rejected`,
`invalid`, `budget_exhausted`, and `tool_failure`. They include all three budget
counters for the target. State, request-content hashes, molecular identity,
evaluation IDs, tool results, and charges reside in the framework's run-local
trusted `VerifyContext`; changing spelling, route ID, or candidate key does not
reset a scientific cache or budget.

## Reaction-route replay

A route has exactly:

```text
schema_version, route_id, target_id, candidate_key, steps,
final_product_smiles
```

The generic route schema permits one to eight ordered steps, but every public
target in this pilot freezes `route_constraints.min_steps` and `max_steps` to
one; a longer or empty route is rejected before reaction execution. A step has
exactly `step_id`, `reaction_template_id`, `reactants`, and `product_smiles`.
Each ordered
`reactants` member has exactly one of `material_id` or `step_id`. Material IDs
must be allowed by the target. A referenced step must precede the consuming
step; cycles, forward references, and unused padding steps are rejected. The
reactant list follows the template's `reactant_roles` order. The last step must
produce `final_product_smiles`, which must equal the request candidate after
evaluation standardization.

The verifier resolves trusted SMARTS and material SMILES; submitted SMARTS or
code is never executed. It requires the expected reaction-site matches and at
most the frozen `max_unique_products`. Every RDKit outcome is sanitized,
standardized as an evaluation graph, and deduplicated. Zero or ambiguous unique
products, a declared intermediate mismatch, a final mismatch, or a
target-disallowed reactant/template rejects the route. A step reference passes
the preceding trusted replay product, never merely the submitted declaration.

The only public template is a task-authored carboxylic-acid/primary-amine graph
rewrite. The leaving hydroxyl atom is intentionally unmapped. A successful
replay is computational graph evidence,
not a prediction of experimental reagents, conditions, selectivity, yield,
availability, safety, or laboratory synthesis.

## Frozen 3D protocol

The evaluation graph is hydrogenated and embedded with RDKit ETKDGv3. Let
`material` be the UTF-8 bytes of
`pocket_docking_v1`, one zero byte, `target_id`, one zero byte, and the canonical
evaluation SMILES, concatenated in that order. The seed is exactly
`int.from_bytes(hashlib.sha256(material).digest()[:4], "big") % 2147483646 + 1`.
No clock, filesystem order, process ID, or Python hash is used. Geometry is
optimized with MMFF94s when all parameters exist and UFF otherwise. Failure to
embed or optimize is a `tool_failure`, never a hit.

Meeko 0.8.0 assigns Gasteiger charges and writes the ligand PDBQT. AutoDock Vina
Python 1.2.7 uses scoring function `vina`, one CPU, seed 20260916,
exhaustiveness 4, three modes, and the exact receptor, center, and box recorded
for the target. Mode affinities and heavy-atom coordinates are rounded
half-even to 0.001. Selection minimizes affinity, then the heavy-atom coordinate
signature, then the returned zero-based mode index. Docking affinity is in
kcal/mol and lower is better. NaN, infinity, an absent pose, or an
external-process error fails closed.

Interaction rules are evaluated from the selected ligand pose and the prepared
receptor PDB, not from submitter labels. Distances are Euclidean Angstrom
distances between the named protein atom and a chemically typed ligand atom.
For a protein-donor hydrogen bond, the angle is donor--hydrogen--acceptor. Every
interaction group is satisfied when at least its `min_satisfied` rules pass:

- `trypsin_3ptb`: a positively charged ligand nitrogen is within 3.6 Angstrom
  of ASP A:189 OD1 or OD2.
- `abl1_1iep`: a ligand acceptor forms the frozen protein-donor hydrogen-bond
  geometry with the MET A:318 backbone N/H. A THR A:315 polar-contact rule is
  listed as optional scientific context; it is not a required gate or part of
  the raw score.
- `brd4_3mxf`: either the ASN A:140 ND2 donor geometry passes all three frozen
  distance/angle limits, or a ligand C/Cl/Br atom is within 4.5 Angstrom of a
  listed TYR A:97 aromatic side-chain atom.

These are deliberately narrow geometric benchmark rules. They are not a full
physical model of binding, solvation, entropy, induced fit, or proton-transfer
chemistry. A Vina score and geometric contact do not establish biological
activity.

## Required final artifacts

The final submission contains exactly the following six regular files. No
symlinks, archives, executable code, extra path, path traversal, or network
reference is accepted.

### `candidates.csv`

The exact header is:

```text
target_id,rank,candidate_key,canonical_smiles,reported_qed,reported_clogp,reported_vina_affinity_kcal_mol,route_id,pose_id,property_evaluation_id,route_evaluation_id,docking_evaluation_id
```

There are at most ten rows per target and 30 total. Ranks are contiguous from
one within a target. `candidate_key` is globally unique and is copied verbatim
across the CSV, SDF, route, and interaction record for that candidate. Route
IDs are also globally unique across all targets. Each
row reports QED, cLogP, and Vina affinity as plain finite decimals. These
self-reports are evidence fields only: the verifier independently recomputes
them and rejects disagreement, and never uses them as scoring oracles. Each
evaluation ID must identify an actually charged or cached accepted evaluation
for the same target, molecular identity, and evaluation-canonical graph. Within
a target, identity SMILES,
pose IDs, candidate keys, and ranks are unique. Fewer than ten rows
are permitted and missing ranks score as misses.

### `poses.sdf`

There is exactly one 3D record per CSV row and no other record. Each record has
finite coordinates with absolute value at most 10,000 Angstrom, at most 256
total atoms and 80 heavy atoms, the same bond graph as the canonical candidate,
and scalar properties `target_id`, `candidate_key`, `pose_id`, and
`docking_evaluation_id`. These values must exactly match the CSV and trusted
docking record. The pose must be the frozen selected mode returned by that
evaluation. A combined SDF need not preserve the original single-record bytes,
but after graph/atom binding its heavy-atom coordinates in the receptor frame
must match the official deterministic rerun with direct RMSD at most 0.05
Angstrom, without rigid-body alignment. Changing coordinates or relabeling a
pose fails the binding check. A docking response exposes `evaluation_id`,
`pose_id`, its single-record `pose_sdf`, `pose_sha256`,
`vina_affinity_kcal_mol`, interaction evidence and fingerprint, and
`response_sha256` so an Agent can materialize artifacts without inventing
scientific values.

For a zero-row submission, `poses.sdf` is exactly zero bytes. Otherwise its
last bytes are the final `$$$$` record-terminator line followed immediately
by EOF, LF, or CRLF, with no trailing whitespace, bytes, or unterminated
records. The
submitted pose, trusted validation pose, and final deterministic rerun pose
must also agree pairwise within the same direct 0.05 Angstrom RMSD tolerance.

### `synthesis_routes.json`

The top-level object is exactly `{"schema_version":"1.0","routes":[...]}`
apart from whitespace. There is exactly one route per CSV row. Route IDs,
target IDs, candidate keys, final products, and trusted route evaluation IDs
are cross-checked; the route is replayed again from trusted inputs.

### `interaction_evidence.json`

The top-level object is `{"schema_version":"1.0","records":[...]}`. There
is one record per CSV row with exactly `candidate_key`, `target_id`, `pose_id`,
`docking_evaluation_id`, `interaction_fingerprint`, `all_satisfied`, and
`groups`. It is a copy of validation evidence, not a self-attestation. The test
verifier recomputes groups from the bound pose and rejects a fingerprint or
field mismatch.

### `evidence.jsonl`

Each nonblank line is one object with exactly `schema_version`,
`evaluation_id`, `request_id`, `request_type`, `target_id`,
`candidate_identity`, `status`, and `response_sha256`. Lines exactly cover all
trusted charged evaluations in global call order, including rejections and tool
failures. Missing, invented, reordered, or modified evidence invalidates the
artifact set. Cached responses are represented by their original charged
evaluation, not duplicated.

### `summary.json`

The top level is exactly `schema_version`, `target_summaries`, `budget_usage`,
and `notes`. Each target summary contains `target_id` and `submitted_count`.
Both arrays contain all three targets exactly once in the documented scoring
order. Each budget item contains `target_id`, `property_evaluation_used`,
`route_validation_used`, and `docking_evaluation_used`. Counts are checked
against trusted state; `notes` is a bounded informational string and never
affects scoring.

File limits are 128 KiB for CSV, 5 MiB for SDF, 1 MiB each for routes and
interaction evidence, 2 MiB for JSONL evidence, and 64 KiB for the summary.
JSON duplicate keys, excessive depth, invalid UTF-8, NaN/Inf, blank CSV fields,
extra CSV columns, and SDF parse or coordinate failures are rejected before
scientific scoring.

## Final test and primary metric

The final test rereads trusted validation history, reparses and standardizes
every molecule, replays every route, verifies the selected pose binding, and
recomputes properties and interactions. It never trusts submitted scores,
booleans, fingerprints, or predictions; it never executes participant code and
does not access the network. Final testing does not mutate scientific budgets.

A ranked row is a **verified hit** only when all of these conditions hold:

1. the molecular graph is valid and unique for the target;
2. no forbidden SMARTS matches and every public target property bound passes;
3. the one-step synthesis route replays exactly from target-allowed inputs;
4. property, route, and docking evaluations exist in trusted state and bind the
   same target, molecular identity, route, pose, and candidate key;
5. the recomputed Vina affinity is at or better than the frozen target-specific
   trusted threshold within its frozen tolerance;
6. the target's required interaction group passes from recomputed geometry;
7. all six artifacts agree.

For target `t`:

```text
hit_rate_at_10(t) = verified hits among submitted ranks 1..10 / 10
```

Unsubmitted positions and scientifically failing rows contribute zero. The
primary raw score is the macro-average over the three targets:

```text
raw = (hit_rate_at_10(trypsin_3ptb)
     + hit_rate_at_10(abl1_1iep)
     + hit_rate_at_10(brd4_3mxf)) / 3
```

The valid-score range is `[0,1]`, higher is better, and the metric transform is
`linear_maximize`. Per-target rates and the raw score are rounded half-even to
eight decimals. A structurally invalid artifact set returns `valid:false` and
the repository's non-ranking invalid sentinel rather than a misleading zero.

Diagnostics may report validity, uniqueness, replay success, mean Vina
affinity, interaction satisfaction, QED, diversity, calls per verified hit, and
false-success count. They never compensate for a failed hard condition.
