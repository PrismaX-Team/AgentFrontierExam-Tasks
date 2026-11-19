# Unprivileged persistent strategy runtime

The final verifier starts one fresh ordinary Python subprocess per condition.
It keeps stdin open across initialize/choose/feedback requests and terminates
the process group and removes its scratch directory when the condition ends.
No root, chroot, UID switching, syscall filtering or runtime-library copy is
required. Sibling modules and installed numerical packages remain available.

The outer verifier container provides offline execution and total CPU/RAM limits.
`input/protocol.json` supplies cumulative wall/CPU time, address-space, numerical
thread and per-line byte limits, plus a cumulative stderr byte cap. Both output
pipes are drained without unbounded buffering. A diagnostic tail accompanies
process failures. Each call requires one newline-terminated JSON object; malformed,
duplicate-key, nonfinite, extra or oversized responses fail the submission.

This is the trusted-internal execution mode: solution and scorer share a user
and filesystem. Clearing inherited environment variables does not hide reference
files or the SDK/context filesystem from deliberately inspecting code. Only the
provided inputs and authorized feedback are permitted by the scientific protocol.
It is not a replacement security boundary for adversarial untrusted submissions.

Scientific inputs, labels, initial conditions, budgets, threshold, query accounting
and scoring formulas are unchanged. `frozen_graph_contract.py` changes only its
JSON-loader import and a comment about the scratch directory. The development
verifier and the submitted strategy interface are unchanged.

Acceptance is run in the real D-cluster verifier as UID65532, 2 CPU/8192MiB, offline:
`python3 -B /verifier/test/run/test_subprocess_runtime.py --result /output/runtime-tests.json`.
Tests cover persistent state and per-condition reset, environment/sibling imports,
malformed/extra output, stream/request caps, process failure, cumulative timeout
and cleanup, plus trusted replay accounting, strict +/-5 boundaries, cache hits,
rejected requests and normal early stopping. Actual SDK template/baseline replay,
independent repeated scoring and retained results are recorded in the operator's
PR32 review handoff; this file does not claim a run before evidence exists.
