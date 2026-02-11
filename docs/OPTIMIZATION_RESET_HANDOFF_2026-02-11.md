# Optimization Reset Handoff (2026-02-11)

## Objective
- Get `tests/submission_tests.py` under `1363` cycles (north-star target: `<1361`).
- Current stable baseline in this repo state: **`1377` cycles**.

## Current Verified State
- `python perf_takehome.py Tests.test_kernel_cycles` => `CYCLES: 1377`
- `python tests/submission_tests.py` => `8/9` passing, only `test_opus45_improved_harness` fails (`<1363`).
- Working tree is clean.

## Mathematical Feasibility Argument (`<1361` is possible)
From current emitted non-debug op totals (from inefficiency diagnostics):
- `alu=2598`, `valu=7647`, `load=2601`, `store=32`, `flow=311`

With slot limits:
- `alu=12`, `valu=6`, `load=2`, `store=2`, `flow=1`

Resource lower bound:
- `LB = max(2598/12, 7647/6, 2601/2, 32/2, 311/1)`
- `LB = max(216.5, 1274.5, 1300.5, 16, 311) = 1300.5 -> 1301`

So baseline headroom is:
- `1377 - 1301 = 76` cycles

Need only `17` cycles to reach `1360`.

Constructive feasibility direction:
- Depth-3 deterministic elimination removes `512` load ops (`2 depth-3 rounds * 32 vectors/round * 8 loads`).
- Even with extra select overhead, resource bounds still permit `<1361` if flow is kept controlled and selection is VALU-biased.

This is a feasibility argument, not proof that the current scheduler settings alone will realize it.

## What Was Tested This Session (and outcomes)

### Broad Search
- Random structural+scheduler sweep (~900 unique configs): best found `1384`, did not beat baseline.
- Seed-only sweep on baseline structure over `0..6000`: best remained `1377` (17 seeds tied; best-known seed `111`).
  - Histogram head: `1377` (17), `1378` (61), `1379` (43), `1380` (223), `1381` (1090), ...

### Refactors Tested (all reverted)
- Value-store pointer chain replacing flow add_imm:
  - Regression to `1384` / worse variants to `1406`.
- Two-phase gather schedule (per chunk: gather then post-gather):
  - Correctness fixed, still regression to `1380`.
- VALU vector add for gather addresses (replacing 8 scalar ALU lane adds):
  - Regression to `1393`.
- Temporary splitting / alternating address registers:
  - Regressions (`1382`, `1404`, etc.).

Conclusion: easy scheduler/seed tweaks are exhausted; low-level local rewrites were mostly regressions.

## Root-Cause Signals from New Tooling
- Dominant blocker: `strict_dep_wait` (very large, multi-million count).
- `scheduler_choice` is effectively zero in decision report (beam choices are not the bottleneck).
- Engine pressure:
  - load and valu are near saturation
  - flow is low-average but appears in critical tail chains (especially repeated `flow.add_imm` patterns).
- Implication:
  - Need **structural DAG changes**, especially reducing load-engine work and strict chains.

## Strategy Stack For Next Agent

## High-Level Strategy
1. Reduce total load work in hot depths (`>=3`) instead of tuning scheduler knobs.
2. Convert deterministic-depth selection from flow-heavy to VALU-biased arithmetic blends.
3. Keep scratch pressure controlled so interleave does not collapse.

## Low-Level Strategy
1. Implement **depth-3 deterministic node selection** (nodes 7..14) with VALU blend logic.
2. Avoid flow select trees for depth-3 (prior attempts showed flow bottlenecks).
3. Keep compact path semantics intact (`use_compact_path_depth3plus` path rules).
4. Add round/depth-local scheduling shape changes only after deterministic depth-3 is correct.
5. Use inefficiency diff after each candidate to verify blocker movement, not only raw cycle count.

## Meta Strategy (execution discipline)
1. First build deterministic structural matrix tooling on frozen problem to compare structural options.
2. Keep one change per branch; benchmark quickly; revert aggressively on regression.
3. Only do seed retuning after structural win appears.

## Concrete Next Steps (ordered)

1. **Build/extend structural experiment runner**
- Add `tools/opt_debug/run_structural_matrix.py` (or extend `tools/opt_debug/auto_optimize.py` with `--kernel-kwargs-json`).
- Evaluate structural toggles under frozen problem:
  - `depth3_deterministic`
  - `depth2_select_mode`
  - `idx_branch_mode`
  - `split_hash_pairs`
  - `fast_value_vector_ptrs`
  - optional `depth4_mode`, `depth4_adaptive_interleave`

2. **Implement depth-3 deterministic VALU-blend selection**
- Target region: `perf_takehome.py`, branch `elif depth == 3 and use_depth3_deterministic:`.
- Keep preloads for nodes 7..14, but replace flow-heavy select tree with VALU blends:
  - use masks from path bits (`(path >> k) & 1`)
  - use arithmetic select form `a + mask * (b - a)` (mask in `{0,1}`).
- Goal: remove depth-3 gathers with minimal flow increase.

3. **Validate correctness immediately**
- `python perf_takehome.py Tests.test_kernel_cycles`
- `python tests/submission_tests.py`
- Reject any branch with correctness failures.

4. **If depth-3 deterministic is still high, test hybrid variants**
- Apply deterministic only for one of the two depth-3 rounds (or partial lane groups) if full mode over-pressures VALU/flow.
- Keep this as a temporary exploration branch only.

5. **Run diagnostics and compare**
- Baseline report:
  - `python tools/opt_debug/run_inefficiency_report.py --out-dir docs/reports/optimizations/debug`
- Candidate report:
  - same command with `--kernel-kwargs-json '{...}'`
- Diff:
  - `python tools/opt_debug/run_compare_inefficiency.py --base-json ... --candidate-json ... --out-dir ...`
- Check:
  - cycles down
  - strict_dep_wait down
  - no catastrophic flow increase

6. **Only after structural gain, retune seed/weights around winner**
- Seed sweep around winning structure (`0..2000` initially).
- Small bounded scan of:
  - `scheduler_crit_weight`
  - `scheduler_succ_weight`
  - `interleave_groups` / `interleave_groups_early`

## Guardrails
- Do not modify `tests/`.
- Validate with `tests/submission_tests.py` each accepted change.
- Keep scratch under `SCRATCH_SIZE=1536`; watch for interleave collapse when adding vectors.
- Avoid copying public take-home solution code.

## Commands Cheat Sheet
```bash
# Baseline quick check
python perf_takehome.py Tests.test_kernel_cycles

# Full validation
python tests/submission_tests.py

# Inefficiency report
python tools/opt_debug/run_inefficiency_report.py --out-dir docs/reports/optimizations/debug

# Scheduler decisions
python tools/opt_debug/run_scheduler_decision_report.py --out-dir docs/reports/optimizations/debug

# Lifetimes
python tools/opt_debug/run_lifetime_report.py --out-dir docs/reports/optimizations/debug
```

## External Research Used (methodology references)
- LLVM MachinePipeliner / SMS concepts:
  - https://github.com/llvm/llvm-project/blob/main/llvm/lib/CodeGen/MachinePipeliner.cpp
- Software pipelining (ResMII/RecMII framing):
  - https://www.doc.ic.ac.uk/~phjk/AdvancedCompArchitecture/Exercises/Ex2-SoftwarePipelining/
- Trace scheduling (global VLIW scheduling ideas):
  - https://www.osti.gov/biblio/7061745
- Swing Modulo Scheduling reference:
  - https://upcommons.upc.edu/entities/publication/30e40185-805f-4ac5-9875-36e4c9d8f32a
- Modulo Variable Expansion reference:
  - https://dl.acm.org/doi/10.1145/192724.192731

## Handoff Summary
- Baseline is stable at `1377`.
- Scheduler-only search plateau is confirmed.
- The path forward is structural:
  - deterministic depth optimization with **VALU-biased** selection,
  - then bounded retuning.
- Start with depth-3 deterministic VALU-blend and structural matrix tooling.
