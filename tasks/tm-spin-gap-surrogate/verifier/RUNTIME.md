# Unprivileged inference runtime

This initial task version trusts internal submissions. A normal Python subprocess
runs inside the framework's offline verifier container, as the same unprivileged
user as the scorer. No root, chroot, UID switching, seccomp setup or copied Python
installation is required. This is **not** filesystem isolation: submitted code
could deliberately read verifier/reference files or alter writable scoring state.
It must obey the instruction against answer lookup. Strict isolation would need
a separate framework-managed inference container without references mounted.

The scientific inputs, reference values, split, metric formula and baseline are
unchanged by this runtime revision. `p1_scoring.py` retains complete-set checks,
record MAE and diagnostic metrics. Successful inference still records predictions
and a log through the real `VerifyContext` API.

## Invocation and limits

`solution/main.py` receives one JSON-line request containing all final structures,
followed by EOF. It must emit one newline-terminated JSON response and exit with
code zero. Existing `for line in sys.stdin` entries work without changes. Sibling
modules and frozen numerical assets are available from the solution directory;
the working directory is temporary and writable. Use paths relative to `__file__`
for model files, as in the original baselines.

The runtime reads wall time, child address-space limit, thread count, JSON byte
cap and stderr cap from `input/protocol.json`. The outer container supplies total
CPU, memory, process count and offline networking. An abnormal exit, timeout,
oversized output, malformed JSON or extra stdout fails inference. Process-group
termination and temporary-directory cleanup run on both success and failure.
Child environment variables are constructed explicitly; controller credentials
and `SIE_VERIFY_CONTEXT` are not passed through. This alone is not a security
boundary because filesystem access is shared.

Select an operator CPU numerical base image (for example the project's pinned
`sie-cpu-base:0.1.0`), rather than the minimal Python image. Baseline inference
needs NumPy; training may need the other numerical libraries. No GPU is required
for the supplied frozen baseline. The runtime does not copy installed libraries
into `/tmp`; only a small working directory is created there.

## Validation

Run `test_subprocess_runtime.py` inside the target unprivileged verifier container
for transport, failure, limits and cleanup checks. Then use the real framework
`Verifier` and `VerifyContext` to score the unchanged zero template and original
frozen baseline assets, and repeat final scoring in independent D sandboxes.
Do not replace actual operator acceptance with a context double. Historical
author isolation tests describe the old runtime and remain historical evidence.

The 7.793858458391523 MLP reference remains author-reported metadata until the
separate baseline review is resolved; the author also reports a better frozen
tree result of 5.077471566282976. Runtime compatibility does not settle baseline
selection, the remaining scientific headroom or domain approval.
