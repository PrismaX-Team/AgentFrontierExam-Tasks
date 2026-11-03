# Branch allocation for LLM evolutionary search: data and tools

## Files

| File | Content |
|---|---|
| `example_cell.jsonl.gz` | one complete configuration: OpenEvolve × MinMaxDist × Gemma-3-12B, 10 branches (seeds 40–49) × 512 iterations, one JSON object per branch |
| `allocators.py` | the five published allocators, each implementing the `Policy` interface |
| `harness.py` | replays a policy over the example cell exactly as the scorer replays the hidden configurations |
| `policy.py` | submission template (uniform random) |

## The allocation protocol

A configuration is one (evolutionary framework, task, model) with 10 recorded branches.
For each seed, `Policy(10, seed)` spends a budget of 512 pulls:

```
arm = policy.select()                 # which branch to advance
reward = that branch's running-best fitness after its next iteration
policy.update(arm, reward)
run score = best reward seen during the 512 pulls
```

Branches are independent: a branch's next iteration does not depend on the other branches,
so a recorded branch replays exactly whatever the allocation order. A branch can be pulled
at most 512 times, which the budget never exceeds. Rewards start with the first pull, and
the running best includes the starting program, so a branch that never improves keeps
returning its initial fitness.

The configuration score is the mean run score over seeds 1–30. The primary score is the
unweighted mean over the 41 hidden configurations.

## Fitness

`fitness` is normalised so that **1.0 is the AlphaEvolve reference construction** for the
task; it can exceed 1. The tasks are OpenEvolve's Circle Packing (n = 26, sum of radii),
MinMaxDist (n = 16, (d_min / d_max)²) and Heilbronn Triangle (n = 11, minimum triangle
area). `raw = fitness × raw_multiplier` is the same curve in the task's own units.

In the hidden configurations some branches stop improving early, and some score 0
throughout.

## Fields of a branch record

| Field | Meaning |
|---|---|
| `method`, `task`, `model`, `seed` | the configuration and branch |
| `C`, `T`, `N` | budget 512, 512 iterations, one model call per iteration |
| `fitness` | running-best fitness after each iteration (512 values, non-decreasing). **This is the reward** |
| `raw`, `raw_multiplier` | the same curve in task units, and the conversion factor |
| `initial_fitness` | fitness of the starting program (rounded; `fitness` holds the exact value) |
| `step_fitness` | fitness of the program proposed at each iteration (shows rejected proposals) |
| `valid` | whether the proposed program evaluated without error; a valid program can still score 0 |
| `improved` | whether the iteration raised the running best; the first iteration is always marked |
| `improved_src` | the framework's own improvement flag (per island for OpenEvolve) |
| `parent_fitness` | fitness of the program that was modified |
| `prompt_tokens`, `completion_tokens`, `cached_tokens` | tokens of the iteration's model call |
| `flops_exact`, `flops_ub`, `cum_flops` | FLOPs of each iteration (cache-corrected, upper bound) and the running sum; `null` where not recorded |
| `flops_method`, `p_active`, `p_active_source` | which FLOPs estimate applies and the parameter count used |
| `gen`, `gen_time_s`, `finish_reason`, `timed_out`, `island` | iteration index, wall-clock seconds, completion status, island index |
| `status`, `native_gens`, `availability`, `warning`, `schema` | bookkeeping |

`null` means the framework did not record the value; it never means zero.

## Where the hidden configurations come from

The 41 scored configurations are drawn from this grid (not every combination exists):

- models: qwen3_8b, qwen3_14b, qwen3_32b, llama3_8b, gemma_12b (Gemma-3-12B), devstral_24b (Devstral Small 24B), haiku4.5
- tasks: cp (Circle Packing), mmd (MinMaxDist), ht (Heilbronn Triangle)
- frameworks: greedy_sweep (sequential refinement), openevolve, codeevolve, shinkaevolve

## Published results

| Allocator | Mean over the 41 scored configurations | Example cell (`harness.py`) |
|---|---|---|
| uniform random | 0.8523 | 0.6864 |
| round robin | 0.8566 | 0.6851 |
| UCB1, c = √2 | **0.8709** | 0.7328 |
| EXP3.P, η = 0.07, γ = 0.1 | 0.8617 | 0.8150 |
| Gaussian Thompson sampling | 0.8561 | 0.8293 |

A clairvoyant allocation that spends the whole budget on the branch that ends best reaches
0.9274 over the 41 configurations. It needs the outcome in advance and no online policy can
reach it.

## Policy rules

`policy.py` runs in an isolated process without file access. It must define `Policy` with
`__init__(self, k, seed)`, `select(self)` and `update(self, arm, reward)`. `select()` must
return a Python `int` in `[0, k)`; a `bool` or `float` is rejected.

- **Imports:** only `math`, `random`, `heapq`, `bisect`, `collections`, `itertools`,
  `functools`, `statistics`, `copy`. Their public names are available, except
  `SystemRandom`, `update_wrapper`, `wraps`, `singledispatch`, `singledispatchmethod` and any
  submodule; names starting with `_` cannot be imported.
- **Built-ins available:** `abs all any bool callable chr classmethod complex dict divmod
  enumerate filter float frozenset hasattr int isinstance issubclass iter len list map max min
  next object ord pow print property range repr reversed round set slice sorted staticmethod
  str sum super tuple zip`, the exception classes `Exception ValueError TypeError
  KeyError IndexError ZeroDivisionError ArithmeticError OverflowError RuntimeError
  StopIteration AssertionError NotImplementedError LookupError`, and `True False None
  NotImplemented`. `print` output is discarded.
- **Not allowed** in expressions anywhere in the source (defining methods such as `__init__`
  is fine): the names `eval exec compile open input breakpoint
  help globals locals vars dir getattr setattr delattr type memoryview id hash` and any
  double-underscore name other than `__name__`; the attributes `format format_map mro
  SystemRandom modules sys os builtins subclasses update_wrapper wraps singledispatch
  singledispatchmethod`, any attribute starting with `f_ gi_ co_ cr_ ag_ tb_`, and **any
  attribute starting with `_`** except `__init__` (so `super().__init__(...)` works; name
  your own attributes without a leading underscore).
- **Determinism:** before each run the module-level `random` generator is seeded with the
  run's seed. Use `random.Random(seed)` or the module functions; choices must depend only on
  the seed and the rewards. Every run is played by two independent copies of the policy in
  separate processes; if their choices ever differ (for example because they depend on object
  addresses, as in `repr(object())`), the submission is invalid.
- **Limits:** each 512-pull run must finish within 30 seconds, and the whole evaluation
  (41 configurations × 30 seeds) within 40 minutes; memory is capped at 2 GiB.
