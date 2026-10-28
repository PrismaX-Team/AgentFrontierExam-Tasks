# Default base images

Version 0.1.0 · built 2026-09-22

When `environment/environment.json` is `{"type": "default"}`, the agent runs
on one of two operator-maintained base images. Everything listed here is
already installed; do not repeat it in `instruction.json.environment` and do
not write a Containerfile just to obtain it.

| | CPU base image | GPU base image |
|---|---|---|
| Tag | `sie-cpu-base:0.1.0` | `sie-gpu-base:0.1.0-cu128` |
| Parent | `python:3.11-slim-bookworm` | `pytorch/pytorch:2.8.0-cuda12.8-cudnn9-devel` |
| Chosen when | `instruction.json.limitation.gpu.count` is 0 or absent | `limitation.gpu.count` ≥ 1 |

The operator picks the image from the task's resource cap; the package does
not name it. A Containerfile receives the chosen image as `SIE_BASE_IMAGE`
(`ARG SIE_BASE_IMAGE` / `FROM ${SIE_BASE_IMAGE}`), so the same Containerfile
works on both.

## Present in both images

**System**: Python 3.11 (`python3`), `pip`, `git`, `git-lfs`, `curl`, `wget`,
`tar`, `gzip`, `unzip`, `xz-utils`, `zip`, `build-essential`, `pkg-config`,
`procps`, `jq`, `rsync`, `ca-certificates`.

The CPU image ships Python 3.11.15 from Debian's slim image; the GPU image
ships Python 3.11.13 from the PyTorch conda environment. Both are 3.11.

**Python packages** (identical pins in both images):

| Package | Version | Use |
|---|---|---|
| numpy | 2.3.2 | arrays |
| pandas | 2.2.3 | tables, CSV |
| scipy | 1.17.1 | numerical routines |
| scikit-learn | 1.7.2 | splits, metrics, simple models |
| matplotlib | 3.10.8 | plotting |
| pillow | 12.3.0 | ordinary image files |
| h5py | 3.16.0 | HDF5 |
| PyYAML | 6.0.2 | YAML |
| tqdm | 4.70.1 | progress bars |
| joblib | 1.6.0 | parallelism used by scikit-learn |

NumPy is fixed at 2.3.2 because that is the version bundled with PyTorch
2.8 in the GPU image; changing it would break PyTorch there.

The full `pip freeze` is at `/opt/sie/python-packages.lock` inside the
image and the system package list at `/opt/sie/system-packages.lock`.

## GPU image only

| Item | Version |
|---|---|
| CUDA | 12.8 |
| cuDNN | 9 |
| PyTorch | 2.8.0+cu128 |

The parent is the `devel` variant, so CUDA extensions can be compiled
inside the task image.

## Deliberately not included

These are large and only some tasks need them. Add them through the task's
own Containerfile with pinned versions, and list them in
`instruction.json.environment`:

- `transformers`, `tokenizers`, `datasets`, `accelerate`, `peft`, `trl`
- `deepspeed`, `bitsandbytes`
- `flash-attn`, `vllm`
- `openai`, `boto3`

## Practical consequences

- The runtime image build has no network access. Anything not in the base
  image and not installed by your Containerfile is unavailable to the agent.
- If a required tool is not listed above, decide whether every version can
  be pinned. If yes, use
  [examples/environment-containerfile/](examples/environment-containerfile/).
  If not, stay on `type: default` and record the gap in your build notes.
- Verifiers run in the same image. Keep `verifier/` to the packages above
  unless the Containerfile adds more.

The build recipe (`Dockerfile.cpu`, `Dockerfile.gpu`,
`requirements-baseline.txt`) lives in the operator repository; this file is
updated whenever a new base image version is released.
