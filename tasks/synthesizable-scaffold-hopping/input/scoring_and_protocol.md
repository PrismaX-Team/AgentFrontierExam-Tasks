# Synthesizable scaffold hopping: version 1 protocol

## Goal and interpretation

For instance `erk2`, replace the connected twenty-atom core of the supplied
ERK2 reference ligand while exactly preserving its eleven-atom sidechain,
original meta-Cl/para-F positions, attachment and native tetrahedral parity.
Submit up to ten ranked, distinct molecules and one fixed pocket-frame pose
and replayable graph route per molecule. A computation is not experimental
synthesis, measured affinity, clinical safety, or a freedom-to-operate opinion.

The single raw metric is macro verified-scaffold-hop rate@10. Each instance's
number of successful new-core clusters is divided by ten, including missing
ranks as misses, then averaged over instances. Version 1 has one instance.
All hard gates must pass; favorable diagnostics never offset a failed gate.
An empty, correctly formatted seven-file submission is a valid zero result.

## Graphs, indexing and novelty

Reference atom IDs are the zero-based SDF indices in `reference_structures.json`.
The retained IDs are `[0,1,2,3,9,10,11,12,13,29,30]`; the replaced core is exactly
their complement in the 31-atom reference. Its only retained-to-core directed
boundary is `[3,14]`. A candidate's retained mapping must cover all eleven
atoms bijectively and preserve element, charge, isotope state, aromaticity,
total hydrogen count, the entire induced bond graph and boundary bond type.
Candidate core IDs are exactly the connected nonempty complement. Every
attachment and directed boundary must be explicitly mapped, with no extras.

Mapping candidate IDs refer to the original submitted **heavy-only SDF** order,
not SMILES parse order. The full-graph local tetrahedral parity is compared
across actual matched native neighbor roles; a CIP label alone is not a proof.
New-core/unknown/enhanced/non-carbon stereo and potential E/Z are unsupported
and fail closed in this first release. Separately, the scorer infers stereo
from unchanged submitted 3D coordinates and compares the exact full graph,
charge, tautomer and stereo to the declared SMILES. SDF stereo tags, H positions,
scores and atom maps are not trusted. The scorer adds its own hydrogens without
moving heavy atoms and never aligns, optimizes, repairs or redocks a submission.

Candidate elements are C, N, O, F and Cl, with at most 100 heavy atoms. Query,
radical, isotope-labelled, disconnected and atom-mapped declarations are not
accepted. RDKit 2025.09.6 cleanup, charge-parent and complete canonical-tautomer
enumeration determine duplicate identity; search limits are 1000 tautomers and
1000 transforms, and incomplete enumeration is rejected, not truncated into
an apparent distinct graph. Salt/protonation/SMILES variants earn no duplicate
credit. Exact-state graph equality remains required for poses and routes.

Both the selected chemical core and its atom-type/bond-order-independent
generic topology must change. The old generic selected core must not embed in
the candidate selected core. The standardized Bemis–Murcko scaffold must differ,
and its Morgan radius-2/2048-bit Tanimoto similarity must be <=0.5. Thus a mere
R-group edit of the old core is not a successful hop.

Clustering uses each validly mapped selected core's generic induced 2-core,
recursively deleting degree-zero/one nodes. Among the submitted top-ten rows
with valid graph/coordinate/core preflight, join a pair when either 2-core
embeds in the other; connected components receive at most one success credit.
This conservative rule is permutation-invariant but depends on the submitted
top set. A bridging graph may merge otherwise different bases, and two cyclic
decorations without a shared submitted parent may remain split. It is not a
claim of universal medicinal-chemistry scaffold equivalence. Failed poses with
a valid core map still participate in this grouping.

## Joint spatial and pocket conditions

Reference feature roles and atom IDs are:

| Label | Family | Reference atoms |
| --- | --- | --- |
| lys54_acceptor | Acceptor | 16 |
| met108_acceptor | Acceptor | 20 |
| met108_donor | Donor | 23 |
| lys114_acceptor | Acceptor | 26 |
| hydroxyl_acceptor | Acceptor | 13 |
| hydroxyl_donor | Donor | 13 |
| retained_phenyl | Aromatic | 9,10,11,0,1,2 |

Features use pinned RDKit BaseFeatures plus Meeko AD4 eligibility and attached
generated-H conventions. Search for the minimum-SSE **feasible joint** feature
assignment: position RMSD <=1.0 angstrom, every displacement <=1.5 angstrom,
every pair-distance change <=1.5 angstrom, aromatic plane angle <=30 degrees.
Full reference atom-set relations (equal/subset/overlap/disjoint) are preserved;
in particular both hydroxyl roles must use the same feature atom set.

The SAME assignment must simultaneously satisfy all three actual-H contacts:

- A:LYS54 NZ (HZ1/HZ2/HZ3) donates to `lys54_acceptor`.
- A:MET108 N (H) donates to `met108_acceptor`.
- `met108_donor` donates to A:MET108 O.

For each, at least one unchanged prepared-H triple has D–A <=3.5 angstrom AND
D–H–A >=130 degrees. No independent hydrogen rotation is allowed. Lys114 is
a feature constraint, not a fourth mandatory contact. The assignment search
cap is 1,000,000 nodes; incomplete search is an execution failure, not a miss.

Integrity conditions are topology-aware RDKit heavy-atom DG bonded and
two-bond/angle lower/upper bound ratios >=0.75 and <=1.25, nonbonded lower-bound
ratio >=0.70, aromatic maximum fitted-plane deviation <=0.25 angstrom, and every
protein/ligand heavy-atom distance divided by the vdW-radius sum >=0.75. These
are selected PoseBusters-inspired checks, not its complete validation suite.

Vina 1.2.7 `score()` is recomputed twice at the submitted fixed pose, using
the supplied prepared rigid receptor, seed 20261009, one CPU, spacing0.375,
box center at the reference coordinate bounding-box midpoint and box sizes
equal to reference coordinate ranges plus12 angstrom. A pose outside that box
does not pass. No docking/search/optimization occurs in final scoring. Its proxy
must be <=-6.683 kcal/mol (native reference -8.683 plus2.0); this comparator is
not experimental binding affinity. Exact repeats must agree.

Finally, at most one alert is permitted among MW>500, RDKit Crippen logP>5,
HBD>5 and HBA>10, all recomputed from the verified full graph. This is a specified
RDKit Ro5-style heuristic, not original CLogP/MLogP equivalence or ADMET evidence.

## Computational synthesis model

`starting_materials.json` lists fourteen admitted starting graphs. Prepared
R mesylate H and common pyridone A are allowed as prepared intermediates;
upstream manufacture, commercial supply and batch purity are not asserted.
The library excludes the unverified oxetane-linker-5 graph and terminal ligands.

`reaction_templates.json` defines exactly four motif classes: six-membered
lactam N-alkylation with the specified benzylic mesylate and stereochemical
inversion; methylsulfide-to-sulfone oxidation using TWO explicit peracid roles;
primary carbon-bound amine substitution of 2-methylsulfonyl-1,3-diazine;
alcohol TBS deprotection. They are a restricted computational reaction model,
not exact recipes or predictions of experimental success for new combinations.
Every output heavy atom must have actual reactant-atom provenance. Products
are not selected by a hidden terminal-product or exact-substrate lookup table.

A route is exactly:

```json
{"route_id":"route_1","steps":[{"step_id":"step_1","template_id":"ALLOWED_ID","reactants":[{"material_id":"ALLOWED_ID"}],"intermediate_smiles":"EXACT_PRODUCT"}],"final_step_id":"step_1","final_product_smiles":"EXACT_CANDIDATE"}
```

Each reactant is exactly `{ "material_id": "..." }` or `{ "step_id": "..." }`.
Ordered roles must match the template; the oxidation has three roles. Every
step references only an earlier step, and every step contributes to the final
product. Duplicate steps, cycles, forward references, unused branches,
unknown identifiers, submitted SMARTS/code and extra fields are invalid.
The scorer independently executes every step, checks each asserted intermediate
and exact final state against the candidate. Zero products, ambiguity or an
assertion mismatch fail the route. A search cap is an execution error, not a
successful negative. Engineering bounds are 16 steps, 1000 raw product rows,
8192 SMILES characters, 128 heavy atoms for protected route intermediates.
These are distinct from candidate eligibility and from operator compute caps.

## Seven frozen artifacts

The submission directory contains exactly these seven regular files, no
subdirectories, links, special files or extra executable files. UTF-8 and
strict finite JSON/CSV are required. Duplicate JSON keys, NaN/Inf/overflow,
malformed records and unmatched/duplicate artifact joins invalidate submission.
Candidate-level failures of scientific gates are misses, not high scores.

1. `candidates.csv` has exactly this header, without a BOM:
   `task_id,rank,canonical_smiles,new_scaffold_smiles,route_id,self_reported_mw,self_reported_logp,self_reported_tpsa,self_reported_hbd,self_reported_hba,self_reported_qed`.
   Instance is `erk2`, rank is unique1–10, route IDs are unique ASCII identifiers
   starting with a letter. `new_scaffold_smiles` must equal the recomputed
   standardized Murcko SMILES. Property cells must be finite numbers but are
   never used as measurements. Standardized duplicate candidates invalidate
   the submission rather than earn extra credit.
2. `aligned_poses.sdf`: exactly one heavy-only pose for each row, with `TASK_ID`
   and `RANK` properties matching the CSV. Coordinates are angstroms in the
   supplied fixed protein frame. No additional poses; empty file for no rows.
3. `scaffold_mapping.json`: `{"schema_version":"1.0","candidates":[...]}`.
   Each candidate has exactly `task_id`, integer `rank`,
   `reference_scaffold_smiles`, `candidate_scaffold_smiles`,
   `retained_atom_pairs`, `replaced_reference_atom_ids`,
   `candidate_core_atom_ids`, `attachment_atom_pairs`, `exit_vector_atom_pairs`.
   Pairs are `[reference_id,candidate_id]`; each exit-vector item is exactly
   `{"reference_atom_ids":[3,14],"candidate_atom_ids":[retained_id,core_port_id]}`.
4. `synthesis_routes.json`: `{"schema_version":"1.0","routes":[...]}` with
   exactly one matching route per CSV route ID and the route shape above.
5. `interaction_evidence.json`: `{"schema_version":"1.0","candidates":[...]}`;
   each entry is exactly `{"task_id":"erk2","rank":1,"features":[],"contacts":[]}`
   with arrays available for diagnostic claims. Claims are never authoritative;
   all features, H contacts, geometry and scores are recomputed.
6. `evidence.jsonl`: zero or more diagnostic objects, each exactly `kind`,
   `task_id`, integer `rank`, `artifact`, `note`. Other fields are strings;
   task/rank must match a submitted candidate and artifact a required filename.
   Record actual execution/errors/retries/alternatives. This editable log is
   not the trusted feedback history and cannot erase or create charged calls.
7. `summary.json`: exactly `{"schema_version":"1.0","recommendations":[],"budget_use":{}}`;
   the array/object may contain diagnostic rationale and claimed budget use.
   They cannot override any gate or the trusted context history.

Per-file byte caps, in the same order, are262144,2097152,524288,2097152,524288,
2097152,262144. These bounded parser limits are not official CPU/RAM/run caps.

## Research-time validation

Request is a JSON object of at most1048576 bytes with exactly `request_id`,
`kind`, `payload`. `request_id` is a nonempty string of at most128 characters;
reuse with different exact request content is rejected. Kind is `structure`,
`pose` or `route`, with separate trusted budgets100,50,20.

Every payload has `task_id` and `canonical_smiles`. For `structure` and `pose`,
also provide `pose_sdf` (one heavy-only SDF record as text) and `scaffold_mapping`
using the same mapping fields as above; indices address that record. Structure
feedback reports core, scaffold and recomputed properties without Vina. Pose
feedback additionally recomputes fixed-frame geometry, same-assignment contacts
and binding proxy. For `route`, the only additional field is `route`, with the
route object above; graph-route feedback needs no pose.

Malformed/unsupported intake, unknown IDs and ordinary rejection spend no
scientific budget. A successfully executed negative measurement does spend its
kind's budget. Canonically identical exact-state graphs, routes (including
cosmetic route/step renaming), and graph-linked exact coordinate/map queries
return the trusted cached observation without another science charge. Near
poses are not rounded into the same request. Exhausted new queries reject.
Replies include status, charged and remaining counters; only final `test`
writes the official raw metric. The trusted VerifyContext stores counters,
history and caches. Test reads that history, never participant budget files.
Framework process rollback on crashes/timeouts is an operator integration
responsibility, not implemented by trusting participant data. These counters
limit feedback calls, not arbitrary agent-local computations.

All scoring is local/offline. Reference/parameters and prepared receptor are
agent-visible. No witness ligands, positive-control poses, measured baseline
outputs, hidden experimental affinity or future human judgement enter input.
