# Validation, chemistry, and scoring protocol

This document is normative together with `tasks.jsonl`. All comparisons include
their stated endpoints. Identifiers and field names are case-sensitive.

## Validation request and budgets

Validation accepts one JSON object of at most 65,536 UTF-8 bytes. It must contain
exactly `schema_version`, `request_id`, `task_id`, `candidate_smiles`, `route`,
and `repair_of_call`. IDs match `[A-Za-z0-9][A-Za-z0-9._-]{0,63}`. Unknown or
duplicate JSON keys, unknown fields, non-finite numbers, nesting deeper than 16,
invalid task/material/template IDs, unsafe SMILES, and invalid route dimensions
are rejected without charge.

Each task independently permits 16 successful property evaluations, 20 executed
route replays, and 4 requests with a non-null `repair_of_call`. An exact request
or the same normalized candidate/route is returned from cache without charge.
A reused `request_id` or `route_id` with different content is rejected. A new
route replay consumes one route unit even if it produces no product or a product
mismatch. Only a uniquely replayed, previously unevaluated canonical molecule
consumes one property unit. It receives utility zero when a hard constraint
fails. The request candidate must equal its declared canonical `final_product`
before replay and this identity check is free. A synthesis-plan family is the
ordered task, reaction-template IDs, and starting-material IDs, excluding
declared products and the cosmetic route ID. Changing a previously rejected
family's product declarations is a repair: it requires a fresh `route_id` and
`repair_of_call` naming an earlier `route_rejected` call in that family. A fresh
family cannot claim a repair. Each charged repair consumes one of the four
repair units.
Budget-exhausted requests consume nothing. The verifier checks every budget that
a request could require before replay, so exhausting the property budget also
prevents spending otherwise-remaining route or repair units on later attempts.
State changes are atomic and held only in the framework's trusted run-local
`VerifyContext`.

Every response is a JSON object. `status` is one of `evaluated`,
`cached_evaluation`, `candidate_rejected`, `route_rejected`,
`budget_exhausted`, `invalid_request`, `request_id_conflict`, or
`internal_error`. `charged` and `budget` report all three counters. The
`evaluation` field is the immutable evidence record for a charged route attempt;
copy it unchanged to final `evidence.jsonl`. Errors never disclose alternative
products, witnesses, or hidden final results.

## Molecular identity

RDKit 2025.09.6 performs `MolFromSmiles(..., sanitize=True)`, rejects multiple
covalent fragments instead of selecting a salt parent, and rejects dummy/query
atoms, atom maps, isotopes, and radicals. It then applies
`rdMolStandardize.Cleanup`, the default pinned `TautomerEnumerator` canonical
tautomer, `RemoveHs`, sanitization, and canonical isomeric SMILES. Assigned
stereochemistry unaffected by tautomerization is preserved. The enumerator flags
are explicitly frozen as `RemoveBondStereo=true`, `RemoveSp3Stereo=true`, and
`ReassignStereo=true`: if tautomerization changes a stereogenic bond or atom,
that stereo annotation is deliberately removed and such inputs can collapse to
one identity. Each task permits zero remaining unassigned tetrahedral
stereocenters. Formal charge is not silently neutralized and must equal zero
after standardization.

Scaffold matching uses RDKit substructure matching with chirality. Similarity is
Tanimoto similarity of 2,048-bit Morgan fingerprints with radius 2, bond types,
and chirality enabled. Descriptors are RDKit molecular weight, Crippen cLogP,
TPSA, QED, fraction Csp3, HBD, HBA, strict Lipinski rotatable-bond count,
aromatic-ring count, and formal charge. Descriptor and similarity values are
rounded half-even to eight decimal places before inclusive hard comparisons and
objective normalization.

## Linear route replay

A route contains exactly `route_id`, `task_id`, `starting_material_ids`,
`reaction_template_ids`, `intermediates`, and `final_product`. For `n` binary
steps it has `n+1` ordered material IDs and `n-1` intermediates. Step 1 consumes
material slots 0 and 1. Each later step consumes the preceding product as its
first reactant and the next material as its second reactant. The template
sequence and every material slot must match one public blueprint.

Submitted SMARTS or code is never executed. The verifier resolves trusted
materials and templates, requires exactly one reaction-site match in each
ordered reactant, and requires exactly one raw one-product outcome per step.
Every outcome is sanitized and standardized; a zero, ambiguous, fragmented, or
over-cap result is rejected. Each replayed intermediate, final product, and the
candidate must match exactly after standardization. The route fingerprint is
SHA-256 of canonical JSON excluding only the cosmetic `route_id`.

This is deterministic graph-rewrite evidence, not experimental route validation.

## Hard constraints and utility

Route replay, one connected molecule, the required scaffold, minimum seed
similarity, allowed elements, MW/cLogP/TPSA intervals, HBD/HBA/rotatable-bond
maxima, zero formal charge, zero unassigned stereocenters, and absence of every
listed forbidden SMARTS are hard constraints. Any failure makes utility zero.

Each task defines five desirabilities and positive weights summing to one:

- Maximize: `clip((x-zero_at)/(one_at-zero_at), 0, 1)`.
- Target: `clip(1-|x-target|/tolerance, 0, 1)`.
- Range: linear from `zero_lower` to `one_lower`, one through `one_upper`, then
  linear down to `zero_upper`, clipped to `[0,1]`.

For a hard-valid molecule, utility is the weighted geometric mean
`exp(sum(weight_j * ln(desirability_j)))`. A zero desirability makes utility
zero. Each clipped desirability and the final utility are rounded half-even to
12 decimal places. The two lead-relative objectives deliberately trade
activity-retention similarity against structural novelty; the former is only a
ligand-based surrogate.

## Final artifacts and primary metric

`candidates.csv` uses the exact template header. Predictions must be finite and
bounded but never affect validation or scoring. Within each task, rows must be
the best `min(10, evaluated-count)` evaluated candidates, ranked by recomputed
utility descending, canonical SMILES ascending, then original charged call
order. A valid final submission requires at least one charged property
evaluation in every task. Tasks appear in `tasks.jsonl` order and ranks start at
one.

`routes.json` contains `schema_version: 1.0`, the benchmark ID, and exactly one
canonical route for each candidate. `evidence.jsonl` contains every charged-call
evaluation object, including route failures and repairs, in global `call_index`
order. File caps are 64 KiB for candidates, 1 MB for routes, and 512 KiB for
evidence; at most 30 candidate/routes rows and 60 evidence lines are read.
Symlinks, archives, participant code, file references, path traversal, blank or
extra CSV fields, duplicate molecules/routes/ranks, NaN/Inf, and oversized
numbers are rejected.

For task `t`, let `u_1...u_n` be utilities in charged property-evaluation order,
including zeros for hard failures. At each fixed integer checkpoint `q=0...16`:

`Top10_t(q) = sum(top 10 of u_1...u_min(q,n)) / 10`.

Missing entries therefore contribute zero. If fewer than 16 evaluations were
used, the best-so-far curve remains constant after the last one. With
`Top10_t(0)=0`:

`area_t = sum_{q=1..16} (Top10_t(q-1)+Top10_t(q))/2 / 16`.

Under the fixed `/10` zero padding, a perfect sequence of sixteen unit-utility
candidates has `area_t = 0.6875`: it ramps from zero through checkpoint 10 and
then remains one. The normalized `AUC_t = area_t / 0.6875`, so its theoretical
bounds are `[0,1]` (a particular finite instance need not attain
one). Each `AUC_t` and diagnostic curve value is rounded half-even to
12 decimal places. The raw score is `(AUC_1 + AUC_2 + AUC_3) / 3`, rounded
half-even to eight decimal places. Higher is better. Invalid artifact sets have
`valid:false` and the non-ranking raw sentinel `-1.0`. Test rereads trusted
history, replays and recomputes rather than trusting validation flags or
submitted predictions. It never mutates scientific budget state.
