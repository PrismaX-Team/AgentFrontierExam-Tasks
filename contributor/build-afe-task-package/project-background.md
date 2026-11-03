# Project background

`contract_revision`: 2026-09-15

Agent Frontier Exam (AFE) measures whether a research agent can investigate
and improve an unsolved scientific problem. A research agent is an AI that can
look things up, write and run code, read results, and try again. The exam does
not ask the agent to guess a planted answer or replay a fixed workflow.

The product name is Agent Frontier Exam. Older names
(LiveDiscoveryBench, LiveSciResearchBench) are only historical path names.

## Four Live meanings

- **Live Question**: the problem comes from a real research frontier. The best
  result anyone can reach is still unknown, and a better result would still
  matter scientifically. This is the core of the project.
- **Live Environment**: the agent may use the public internet and current
  research tools, inside a controlled compute box (limits on CPU, memory,
  time, and permissions).
- **Live Metric**: the scoring program always returns the same raw number for
  the same frozen submission. Public display scores use anchors that were
  fixed before a release. Adding a new agent later must not rewrite old raw
  scores or change the scoring program.
- **Live Community**: domain experts and engineers propose and build tasks;
  the project team admits them and runs them under control.

Openness must come from “we do not yet know how good a result can be”, never
from “the scoring answer has not been produced yet”. A task that needs a
future wet-lab measurement, a future observation, a future label, a human
judge, or an organizer callback at scoring time is ineligible.

## Three states that must stay separate

| State | Question |
|---|---|
| Scientific | Is the question important, still improvable, and would a better score mean a real scientific gain? |
| Task-package | Is the five-part package built to the contract, stable, and independently checkable? |
| Score-now | After the agent finishes, can the fixed scoring program compute the final result locally, right now? |

“Scientifically promising”, “a prototype folder exists”, and “a hidden score
can be computed now” are three different facts. Do not collapse them into
“suitable” or “passed”.

## Admission (science), not package format

A task is admitted only if all four hold:

1. **Scientific importance**: a better result would advance a real research
   question or an important scientific computing bottleneck.
2. **Verifier credibility**: the scoring algorithm and every input it needs
   are already fixed; scoring is local, offline, and deterministic (the same
   inputs always give the same number).
3. **Frontier headroom**: the best achievable result is still unknown, with
   credible evidence that meaningful improvement remains.
4. **Long-term sustainability**: data, software, and the scoring path stay
   available without fragile services, secret accounts, or one-off external
   behavior.

Verifier engineering status (separate from the four checks above):

- `ready`: science and engineering wrapper are both done.
- `adaptable`: the scientific algorithm and every scoring input already exist;
  only a thin deterministic wrapper, pinning, or attack tests remain.
- `fatal`: a metric still needs inventing, a scientific input is missing,
  labels are still in the future, or a human must look at candidate results.
  Fatal tasks are not admitted.

CPU, GPU, RAM, storage, download size, and wall time are **not** admission
criteria. Report them separately, with evidence and uncertainty, for the
operator to accept.

## Contribution path

1. Someone submits a proposal (a structured idea: why the question matters,
   what the agent sees and submits, how it would be scored, and any license
   limits).
2. A domain expert reviews that idea against the four admission checks.
3. After approval, the contributor builds the five-part package and opens a
   pull request that cites the proposal.
4. Format CI checks structure. Domain and technical reviewers check science
   and implementation. Approval of the proposal is not approval of the package.

This skill starts at step 3. It does not re-admit the science and it does not
replace human review.
