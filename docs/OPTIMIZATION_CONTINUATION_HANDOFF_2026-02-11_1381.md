# Optimization Continuation Handoff (2026-02-11, 1381 cycles)

## Snapshot
- Date: 2026-02-11
- Target file: `perf_takehome.py`
- Current default result: `1381` cycles
- Submission status: `8/9` tests passing (`<1363` still failing)
- Remaining gap: `18` cycles

## What Was Changed (and kept)
The current `1381` default is from two active defaults in `KernelBuilder.__init__`:

1. `scheduler_random_seed` changed to `523`  
   - `perf_takehome.py:284`
2. `fast_value_vector_ptrs` added and defaulted to `True`  
   - `perf_takehome.py:287`, `perf_takehome.py:319`

Core structural change:
- Added a submission-path-only fast value-load address progression that uses an ALU pointer chain instead of repeated `flow.add_imm` for vector value loads.
- Implemented in `perf_takehome.py:1617` to `perf_takehome.py:1638`.

Net impact:
- Previous stable default: `1407`
- New stable default: `1381`
- Improvement: `-26` cycles

## Why This Worked
The inefficiency reports showed a long strict dependency chain driven by repeated address materialization for value vector traffic.  
The load-only pointer chain change:
- Removed many repeated `flow.add_imm` address setups in the value-load prologue.
- Shifted that work to ALU increments on a dedicated pointer register.
- Reduced critical-chain pressure enough that scheduler seed optimization had more leverage.

Observed decomposition:
- Structural fast-load-pointer change alone: around `1390` with older seed settings.
- Retuned scheduler seed (`523`): down to `1381`.

## Problems Solved During Implementation
1. Constant materialization ordering bug (important)
- Initial fast-pointer attempt used `header_scratch_const(VLEN)` after `body = list(header)` had already happened.
- That created an uninitialized stride-constant use at runtime.
- Fix: emit the VLEN constant directly into `body` when needed in the fast path.

2. Over-aggressive pointer variants regressed
- Two-pointer load path and store-pointer path versions were tested and often regressed.
- Best stable variant is specifically load-prologue pointer chaining only, with stores left baseline.

3. Deterministic depth-3 refactor remained non-winning
- A balanced select-tree variant for depth-3 deterministic mode was explored.
- It was not kept as part of the winning default path because it did not improve submission cycles.

## Pitfalls Avoided
1. Did not touch `tests/`.
2. Did not rely on debug-mode behavior for submission-path optimization decisions.
3. Did not assume reduced instruction count always wins; multiple low-level rewrites regressed despite looking cheaper.
4. Did not treat random-seed gains as globally portable; seed wins are graph-shape-sensitive.

## Repro Commands
Run from repo root:

```bash
python perf_takehome.py Tests.test_kernel_cycles
python tests/submission_tests.py
```

Expected currently:
- `CYCLES: 1381` in both paths
- `tests/submission_tests.py` fails only `test_opus45_improved_harness` (`<1363`)

## Random Search vs Grid Search (Can we do grid search?)
Yes, grid search is possible and useful now, but only if scoped.  
A naive full grid is too large because seed is high-cardinality.

### Practical recommendation
Use a **2-stage search**:

1. Stage A (random seed mining on fixed structure)
- Keep structure fixed at the winning load-pointer variant.
- Search many seeds quickly.
- Keep top K seeds (for example K=32).

2. Stage B (bounded grid on structural knobs with top-K seeds)
- Grid over low-cardinality knobs only:
  - `interleave_groups`: `[24, 25, 26]`
  - `interleave_groups_early`: `[28, 29]`
  - `scheduler_crit_weight`: `[96, 128, 136, 152, 176]`
  - `scheduler_succ_weight`: `[3072, 3584, 4096, 5120]`
  - `idx_branch_mode`: `["flow_vselect", "alu_branch"]`
  - `depth2_select_mode`: `["flow_vselect", "alu_blend"]`
  - `split_hash_pairs`: `[True, False]`
  - seeds: top K from Stage A

Grid size example with `K=32`:
- `3 * 2 * 5 * 4 * 2 * 2 * 2 * 32 = 30,720` trials
- Large but feasible as a batch if parallelized and cached.

### Why not full seed grid?
If seeds are `0..5000`, multiply by 5001 and search explodes.
Random-first seed mining is far more compute-efficient for this problem.

## Suggested Next Work (for an agent)
1. Keep `fast_value_vector_ptrs=True` as the structural baseline.
2. Build a robust search runner that:
- evaluates with frozen submission semantics,
- records cycle + config + correctness,
- auto-skips duplicate configs,
- supports resume from JSONL log.
3. Prioritize:
- seed mining first,
- then bounded grids around the best seeds.
4. Only accept changes that beat `1381` and preserve full correctness.

## Minimal Resume Checklist
1. Confirm current baseline:
```bash
python perf_takehome.py Tests.test_kernel_cycles
```
2. Confirm submission output:
```bash
python tests/submission_tests.py
```
3. If baseline is not `1381`, inspect defaults in:
- `perf_takehome.py:284`
- `perf_takehome.py:287`
- `perf_takehome.py:1617`
