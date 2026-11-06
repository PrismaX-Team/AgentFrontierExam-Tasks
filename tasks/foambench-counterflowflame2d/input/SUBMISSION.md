# Submitting OpenFOAM cases

`cases.json` is a JSON template mapping each case ID to its text files, not a
reference solution. Supply complete initial conditions, dictionaries and any
run script; the verifier runs them to generate the results. Hidden reference
fields and their cell numbering are not provided and must not be reconstructed.

## Required results

Each output time after the initial conditions must contain every field in that
case's `required_fields` in `problems.json`: `U` (velocity, three components), `p` (pressure) and `T` (temperature)
in every case. Density (`rho`) is not a required comparison field in this task. Missing, duplicate plain/gzip versions, malformed,
unreadable or non-finite required fields fail that case, even if other fields
look correct. Additional written field files must also be readable and finite.
Reconstruct parallel results into the main case directory before exiting.

## Cell order

OpenFOAM divides the physical region into mesh cells. A `nonuniform` volume
field has one entry per cell: entry 0 is the value for mesh cell 0, entry 1 for
cell 1, and so on, in that case's OpenFOAM mesh ordering. `U` entries are vectors
in `(Ux Uy Uz)` component order; the other required fields are scalars.
A `uniform` field assigns the same value to every cell. Face or boundary-patch
ordering is not the internal volume-cell ordering. Do not independently sort
field entries by coordinates or values. If a tool renumbers the mesh, it must
renumber all corresponding fields consistently. Standard OpenFOAM solvers do
this automatically. You do not need to match the hidden reference's numbering.

## Comparison

The verifier selects the reference's final time and the nearest candidate
output time (the earlier time wins a tie). As in the source benchmark, it aligns
candidate bounding boxes to reference bounds when they differ. Geometry,
physical units and requested end time must still follow the problem statement;
this alignment is a comparison convention, not permission to change the case.

When the cell-center coordinates match one-to-one (absolute tolerance 1e-6,
relative tolerance 1e-5), the verifier reorders the candidate values into that
spatial correspondence. Equal cell counts alone do not enable direct comparison.
For other meshes, the OpenFOAM reader converts candidate cell data to point data, which is interpolated
onto the reference mesh with the upstream Gaussian kernel (sharpness 0.1,
50 neighbors), and converted back to cell data. Reference field values are
removed from the interpolation target before this operation.

Each required field's NMSE is the sum of squared differences divided by the sum
of squared reference values; the per-case NMSE is their arithmetic mean. If a
reference field is identically zero, identical candidate values give zero error
and any nonzero difference fails the case. A case succeeds only if execution and
field validation pass and its NMSE is strictly below 0.1. The score is the number
of successful cases divided by 9. A failed case earns no partial credit for the fields it did
produce; it does not invalidate the other cases. Diagnostics record selected times, required
fields, correspondence method and per-field errors.

## Physical setup for this review run

Use the exact installed tutorial and the explicit override/inheritance rules in
`PHYSICS.json`. Its `operator_assumption` status authorizes this review run with
that fixed contract. The author has not yet confirmed the historical reference
setup; results remain provisional with respect to that scientific agreement.
`pending_author_confirmation` without an explicit operator assumption still
stops evaluation. Neither status claims author confirmation. Do not infer
missing physical values from hidden results.

## Failure attribution and diagnostics

Malformed submission JSON is invalid and unranked. Missing or bad fields,
solver errors and the configured simulation wall-clock limit fail that case
only. A broken reference, missing dependency, failed comparison subprocess or
comparison timeout is a scoring error, not evidence of a candidate failure.
Per-case summaries and bounded log tails are retained next to the result under
`diagnostics/`. Scratch calculations use that result directory, not the host
system temporary directory.
