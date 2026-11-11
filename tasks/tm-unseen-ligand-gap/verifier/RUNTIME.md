# Unprivileged inference runtime

This initial version trusts internal submissions. A normal Python subprocess runs
as the same unprivileged user as the scorer, inside the framework's offline
verifier container. No root, chroot, UID switching, seccomp setup or copied Python
installation is required. This is not filesystem isolation: submitted code can
access the scorer's filesystem and must obey the instruction against reference
answer lookup. Strong label isolation would require a separate framework-managed
inference container without reference files mounted.

The data, partition, reference labels, scientific scoring and baseline metadata
are unchanged. `frozen_graph_contract.py` retains complete-set validation,
sample-weighted mean absolute error and the auxiliary ligand/order diagnostics.
Accepted predictions and logs still use the real `VerifyContext` API.

## Invocation and limits

`solution/main.py` receives one JSON-line predict request containing all 127 final
target-free records, followed by EOF. It must emit exactly one newline-terminated
JSON response and exit with code zero. Existing `for line in sys.stdin` entries
remain compatible. Frozen sibling modules and model assets are available; use
paths relative to `__file__` for assets. The working directory is temporary and
writable. Only small scratch files belong there, not runtime or dataset copies.

`input/protocol.json` supplies the wall-time, child address-space, numerical thread,
JSON byte and stderr byte limits. The outer container controls total resources
and networking. Nonzero exit, timeout, oversized input/output, extra stdout or
malformed JSON fails inference. Process-group termination and scratch cleanup run
on success and failure. The child environment is explicitly constructed; provider
credentials and `SIE_VERIFY_CONTEXT` are not inherited. Shared filesystem access
means this environment filtering is not a security boundary.

Use an operator CPU numerical base image, such as the project's pinned
`sie-cpu-base:0.1.0`, rather than minimal Python. Frozen reference inference needs
NumPy. No GPU or new electronic-structure calculation is needed. Libraries are
used in place rather than copied into RAM-backed temporary storage.

## Validation

Run `test_subprocess_runtime.py` inside the actual unprivileged D container for
transport, limits, diagnostics, sibling imports, environment filtering and cleanup.
Then use the real framework verifier/SDK to score the zero template and the
unchanged frozen neural, kernel and constant references, export all 127 accepted
predictions, and repeat the neural score in an independent sandbox/context.

Author-reported reference errors are 5.263004864168261 (neural ensemble),
5.846056702019194 (kernel method) and 19.356138664589047 (training-mean constant).
Keep these distinguished from operator replay evidence. This runtime change does
not establish a new scientific baseline, alter training permissions, resolve the
external training-report protocol-hash discrepancy or complete formal dual review.
