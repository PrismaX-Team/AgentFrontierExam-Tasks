# Unprivileged method-selection runtime

The initial release trusts internal submissions. The frozen selector runs as a
normal Python subprocess with the scorer's unprivileged user, inside the
framework's offline verifier container. Root, chroot, UID switching, seccomp setup
and copied Python installations are not required. This does not isolate the
solution from the scorer's filesystem; reference-answer lookup remains prohibited.
Strong label isolation would require a separate framework-managed container.

Scientific inputs, references, method registry, training permissions, complete-set
scoring and baseline metadata are unchanged. `p5_defined_scoring.py` remains
byte-identical, using the actual `VerifyContext` API for recommendations and logs.

## Invocation

The verifier sends one UTF-8 JSON-line `select_method` request containing all 58
target-free records and the 45 method IDs, then EOF. `solution/main.py` must emit
exactly one newline-terminated JSON object and exit with code zero. Existing
`for line in sys.stdin` entries remain compatible. Frozen sibling modules and
assets are supported; resolve asset paths relative to `__file__`.

`input/protocol.json` supplies wall-time, child address-space, numerical threads,
JSON byte and diagnostic stderr byte limits. The outer container controls total
resources and network access. Nonzero exits, timeouts, oversized requests/output,
extra stdout and invalid JSON fail inference. The process group and small writable
scratch directory are cleaned on success and failure. Provider credentials and
`SIE_VERIFY_CONTEXT` are not inherited through the constructed child environment;
shared filesystem access means this is not a security boundary. Do not copy
datasets, runtimes or model weights into RAM-backed scratch storage.

## Validation

Run `test_subprocess_runtime.py` in the actual unprivileged D container for
transport, diagnostics, sibling imports, environment filtering, bounds and cleanup.
Use a CPU numerical image with NumPy for unchanged reference-model replay; no GPU
or new electronic-structure calculation is needed. Then exercise the real SDK with
the starter, frozen density/geometry models and fixed-method selector, export all
58 recommendations and repeat the density score in a fresh sandbox and context.

Author-reported MAEs are 2.6460461241067943 (density), 3.0180903363003124
(geometry) and 3.163990678933186 (fixed method), in kcal/mol, lower is better.
Keep operator replay evidence separate from these reports. This adaptation does
not change the scientific baseline or resolve the disclosed 116-channel feature
association limitations, source screening or full electronic-input preparation.
