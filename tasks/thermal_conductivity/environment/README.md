# Offline CHGNet and phonon research runtime

This directory is a self-contained **Linux x86_64 / CPython 3.11** dependency bundle. It installs **54 pinned research distributions plus pip 24.0** into `/opt/sie-research`. Research programs explicitly use `/opt/sie-research/bin/python`. The operator Python, its NumPy 2 environment, PATH, framework SDK and verifier interpreter remain unchanged.

## Build and installation

The runner builds `Containerfile` using its supplied `SIE_BASE_IMAGE` and this `environment/` directory as the build context. The documented operator CPU and GPU images both provide Python 3.11. The target must use x86_64 and glibc >= 2.28. The installed PyTorch is **2.4.1+cpu**, even if the operator supplies a GPU-capable base.

The build performs no network or system-package installation. It reconstructs the vendored wheels, validates every artifact and complete-wheel SHA256, creates an isolated venv without system-site-packages, then installs with `--no-index --only-binary=:all: --require-hashes`. A separate bootstrap lock freezes pip 24.0. Both bootstrap and research locks include the exact wheel hash. Build-time checks run `pip check`, import the compiled scientific libraries, verify all installed versions, verify CPU-only torch, and execute a small native spglib operation. A successful build writes `/opt/sie-research/ENVIRONMENT_VERIFICATION.json`.

The scientific stack includes CHGNet 0.3.8, pymatgen 2024.5.1, ASE 3.23.0, NumPy 1.26.4, SciPy 1.13.1, torch 2.4.1+cpu, phonopy 3.5.0, phono3py 3.30.0, spglib 2.6.0 and symfc 1.6.0. Model weights are supplied separately under task `input/model_weights`; they are not downloaded during the build or inference.

## Integrity, size and licenses

`wheel_manifest.json` records the complete dependency set, target tags, metadata, immutable artifact URLs, byte counts, full wheel SHA256, and each stored piece. Every upstream wheel hash was checked against the official PyPI version index, or the official PyTorch CPU index for torch. The 194,926,984-byte torch wheel is stored as four ordered pieces of at most 48 MiB. All task files stay below 50 MiB. `assemble_wheels.py` reconstructs and checks the full original wheel before installation.

The only locally built wheel is the tiny pure-Python `nvidia-ml-py3==7.352.0`, whose publisher provides only a source distribution. Its upstream source hash, source archive, exact build configuration and BSD three-clause notice are retained under `provenance/`. It was built offline with setuptools 75.1.0, wheel 0.44.0 and a fixed SOURCE_DATE_EPOCH. Original dependency licenses/notices remain inside their redistributed wheels; license metadata is also recorded in the manifest. The wheel is not a CUDA toolkit and the runtime does not require an NVIDIA driver.

## Validation actually performed

- Reconstructed and SHA256-verified all 55 wheel artifacts, including the split torch wheel.
- Checked all 78 active dependency edges against Linux/Python 3.11 markers and pinned versions, plus Requires-Python constraints.
- Ran the real pip 24.0 resolver offline, with hashes, targeting the Linux wheel tags: all 55 resolved successfully without using installed host packages.
- Audited 316 ELF files in 20 native wheels: all are x86_64. The highest referenced glibc symbol version is 2.27; wheel platform tags impose the stated 2.28 floor. Unbundled dependencies are the usual glibc, libgcc, libstdc++ and libz supplied by the documented operator base. No unexpected external shared library was found.
- The companion scientific evidence separately demonstrates a real CHGNet → finite-displacement FC2/FC3 → Wigner-RTA MgO calculation using these package versions on a macOS CPU host.

**Docker and native Linux execution were not available on the preparation host.** The actual container build, its native import checks, and the scientific smoke inside the final operator image remain required integration checks. The dependency and artifact audits are not represented as a completed container build.

## Offline use

Load the local checkpoint with `CHGNet.from_file(path, mlp_out_bias=False, version="0.3.0")` using `/opt/sie-research/bin/python`. For phono3py 3.30.0, the Wigner API spelling is `transport_type="MS-SMM19"`. Refer to the task's scientific instructions for the target geometry, supercells, q mesh, temperature and output schema. This environment contains no withheld reference labels or prediction archives and uses no path to another task.
