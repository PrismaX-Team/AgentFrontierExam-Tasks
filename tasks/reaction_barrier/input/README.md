# Forward electronic reaction barriers

## Scientific target

Predict the forward electronic barrier `E_TS - E_R`, in eV, for every supplied
test reaction. The reference level is omegaB97X/6-31G(d). These are released
climbing-image NEB reference paths, selected using the publisher's convergence
criteria. They are not newly verified saddle points, activation free energies,
or experimental rate constants. No additional Hessian, imaginary-frequency or
intrinsic-reaction-coordinate verification is claimed.

The source is Transition1x v4. The official groups are preserved in full:
9,561 training reactions, 225 development reactions and 287 test reactions,
covering 155, 8 and 8 source molecular formulas respectively. These are actual
reaction counts, not a claimed percentage split. The chemical scope is H, C,
N and O, with at most seven heavy atoms. Independent charge/multiplicity
labels are not provided; do not invent such source fields.

Use the supplied reaction direction and atom order. Reference and predicted
barriers may be negative; no value is clipped, rounded in advance, filtered
by size or excluded because a search failed. A failed internal calculation
still requires a finite prediction obtained without test answers.

## Data files

All three scientific data files are gzip JSON Lines. Every record contains:

- `reaction_id`: an anonymous immutable ID for that split.
- `atomic_numbers`: the common ordered element identities.
- `reactant_positions_angstrom`: N by 3 endpoint coordinates.
- `product_positions_angstrom`: N by 3 endpoint coordinates.

`train.jsonl.gz` additionally contains `transition_state_positions_angstrom`,
`reactant_energy_eV`, `product_energy_eV`, `transition_state_energy_eV` and
`forward_barrier_eV`. The barrier is the source total-energy difference,
without a thermal, zero-point, solvent or entropy correction.

`validation.jsonl.gz` and `test.jsonl.gz` contain exactly the four common
fields. Their reference energies and transition-state geometries are not
agent inputs. The source atom order is shared between the states; do not
independently remap the endpoints. Cartesian translations and rotations do
not change the target. Reaction reversal does change the forward target.

The official split was checked against actual atomic compositions, source
reaction objects, endpoint pairs and geometry signatures. These checks found
no cross-split composition, identical-geometry or same/reverse-reaction
overlap. They do not prove absence of historical pretraining exposure to this
public dataset.

## Development feedback

The task payload is a JSON object with exactly one field, `predictions_csv`.
Its value is the complete UTF-8 CSV text with the header
`reaction_id,forward_barrier_eV`, followed by one finite prediction for each
of the 225 development IDs. It is not a filename, an attachment or a partial
subset. `validation_request.json` is a blank template;
`validation_predictions.csv` is the corresponding optional editing template.
The API consumes CSV text rather than a path to that file.
`schemas/validation_request.schema.json` describes the JSON representation.
The CSV's ID, coverage, finiteness and size constraints are also checked.

This is the task-specific payload, not the framework's outer action envelope.
The framework supplies the action and request ID and passes this JSON payload
to the validation process. It does not mount final submission files there.

A valid evaluation returns `status: "ok"`, its `round`, the `remaining`
budget, `n_samples`, and a `metrics` object containing `mae_eV`, `rmse_eV`,
`p95_absolute_error_eV` and `within_1_kcal_per_mol_fraction`. It returns no
per-reaction errors, reference energies, coordinates or labels.

At most 20 valid newly executed evaluations are accepted. Identical
predictions in a new evaluation still consume one round. Malformed requests
and requests after exhaustion return an error and do not consume a round.
A network retry replayed under the same framework request ID does not
execute a second evaluation. The private framework `VerifyContext` holds the
accepted measurement history; agent-created usage files are not authoritative.

## Final submission

Provide exactly these four files:

1. `predictions.csv`: the exact header `reaction_id,forward_barrier_eV`, with
   all 287 test IDs once each. Predictions use eV and finite decimal notation,
   including negative values if produced by the method. No surrounding
   whitespace, NaN or infinity is allowed. The file is at most 2 MiB and each
   numeric field at most 128 characters. Units are interpreted literally;
   the scorer cannot infer an undeclared unit conversion from the numbers.
2. `method.md`: nonempty UTF-8 text describing the actual method, training,
   feedback use, approximations, failure handling and reproduction commands,
   at most 64 KiB.
3. `sources.json`: strict UTF-8 JSON with exactly `data`, `models` and
   `software`, each a list of nonempty source-description strings. The data
   and software lists must be nonempty; models may be empty when no external
   model was used. This file is at most 64 KiB, each list at most 256 items
   and each string at most 4096 characters. Report actual sources rather
   than leaving template guidance in the final file.
4. `research_code.zip`: reproducible source code, at most 64 MiB compressed,
   512 MiB declared uncompressed and 1024 entries. Include at least one
   regular file. Use unique relative paths; parent traversal, absolute paths,
   links, special files and encrypted members are invalid. The scorer reads
   the archive to validate it but does not extract or execute its contents.

Final scoring compares the frozen test predictions with all 287 reference
barriers. The primary result is mean absolute error in eV, lower better.
Auxiliary results are RMSE, the 95th percentile of absolute errors using
linear interpolation at `(N-1)*0.95`, and the fraction with absolute error
at most 0.0433641 eV. They do not change the primary ranking. That threshold
is approximately 1 kcal/mol; it does not establish experimental accuracy
of the DFT reference.

Missing, duplicate or extra IDs, invalid numbers and malformed or missing
artifacts make the submission invalid. A required finite metric placeholder
in an invalid result is not a valid scientific score. The final result is
not development feedback for an editable resubmission.

## Available resources and scientific restrictions

Use the operator default environment. Input decoding and scoring use the
Python standard library; NumPy, SciPy, scikit-learn and other packages in the
default image support research. The task supplies no pretrained weights or
extra software installation. Formal runtime, CPU, memory and GPU caps are
operator decisions, not inferred from a constructor's baseline timing.

Scientific training resources are restricted to the supplied data. Do not
retrieve public test-barrier tables, original held-out transition states,
extra training datasets or pretrained weights. Algorithm descriptions and
literature may be consulted. Do not run DFT or query quantum-chemistry
services for the test reactions. Hidden development/test labels may not be
read for fitting or checkpoint selection; model selection uses the declared
development feedback.

The four submission files support reproduction and source disclosure. Their
format checks do not certify an asserted training history, establish that a
generated structure is a saddle point, or replace domain and technical
review. Preserve the actual research code and workspace for audit.

## Attribution

See `ATTRIBUTION.txt` and `LICENSE-DATA.txt` for the data authors, fixed
release, source links, MIT statement and transformation notice. Only the
final-state records are exported here. The full trajectory HDF5, source-ID
mapping, construction scripts, validation/test reference states and baseline
predictions are not agent inputs. The original authors do not endorse this
task or submitted methods.

## Reference level

The measured endpoint-geometry ExtraTrees reference has test MAE 0.41108395831066713 eV. A training-global-median predictor has MAE 0.8001934932388185 eV. These are fixed display anchors, not state-of-the-art claims. The first-stage research aim is a 15% reduction relative to the measured reference, without changing the raw all-test MAE ranking. The linear display map assigns the median reference 0 and the ExtraTrees reference 1000; results may exceed 1000 or be below 0.
