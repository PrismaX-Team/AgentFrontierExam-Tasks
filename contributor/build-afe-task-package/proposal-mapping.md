# Proposal fields to package fields

`contract_revision`: 2026-09-17

A proposal is an approved idea. It is not a package. Copy wording when it is
already exact; do not invent the missing half. One approved proposal becomes
one package. Several proposals in one workspace become several packages.

The package is English. Translate agent-facing text from a Chinese proposal.
Ask the contributor to confirm a title you translated.

| Proposal field | Typical destination | Notes |
|---|---|---|
| `title` | `meta.json.title` and the first line of `instruction.json.instructions` | Keep one English title. |
| `domain` | `meta.json.domain` | Short field name, not a paragraph. |
| `scientificQuestion`, `importance`, `unresolvedEvidence`, `currentFrontier`, `measurableHeadroom` | `instruction.json.instructions` (compressed) and `build/` notes | The agent needs the task, not the full admission essay. Keep the essay in `proposal/` or `build/`. |
| `agentInputs` | `instruction.json.input` plus files under `input/` | Listing a file in the proposal does not create it. Ask for the actual data. |
| `submissionArtifacts` | `instruction.json.submission` plus templates under `input/` | One array item per frozen file. Default `has_template: true`. |
| `environment` | `instruction.json.environment` and `environment/environment.json` | No extra software, or names without frozen versions → `[]` and `type: default`; list unpinned names in `build/`. Switch to Containerfile only when every extra package has a name and a pin; then fill this array with those same names and versions. |
| `hardConstraints` | `instruction.json.requirement` and, if they are run caps, `limitation` | Isolation rules the framework already enforces stay out. |
| `agentResources` | `build/` resource note first; `limitation` only after the operator confirms | Proposal estimates are not official caps. |
| `rawMetric` | `meta.json.metric.description` and the test program | The description is words. The program is the authority. |
| `metricExplanation` | `meta.json.metric.type`; add `transform` only if the type needs extra constants | `linear_*` / `log_ratio_*` omit `transform` and put the two anchors in `baseline`. Leave `type` as `null` until the mapping is known. |
| `usageRestrictions` | `instruction.json.requirement` when the agent must obey them; otherwise `build/` | Do not hide a redistribution ban. |
| Contributor identity fields | not in the package | Stay on the proposal / PR. |
| Consents | not in the package | Already recorded on the proposal. |

`task_sources` is filled from the construction prototype and the datasets
that actually enter `input/` or the verifier, not from every paper in
`unresolvedEvidence`. There is no `source_id`.

`baseline.score` and `baseline.bottom_line_score` are **not** proposal
fields. Replace the template placeholders when you have a measured public
method or an expert estimate. Leave `existing_baseline: false` and empty
`name` / `source` for an estimate.

Hidden labels, oracle tables, and private constants mentioned in the
proposal go under `verifier/test/data/`, never under `input/`.

Mid-run checks stay if the proposal already has them. Do not add a
validation protocol, and do not drop those checks from the English write-up
while the request schema is still missing.
