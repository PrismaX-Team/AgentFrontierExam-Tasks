# Bundled schemas

`contract_revision`: 2026-09-15

These four files are copies of

`science_innovation_exam/src/science_innovation_exam/contracts/schemas/`

at this skill revision:

- `instruction.schema.json`
- `meta.schema.json`
- `environment.schema.json`
- `verifier_result.schema.json`

`scripts/check_package.py` uses the installed framework loader when
`science_innovation_exam` is importable. It uses these copies only as a
fallback. If a test shows they differ from the installed package, update
this directory and `contract_revision` in the same change.
