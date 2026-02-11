# Optimization Master Summary

> **Purpose:** Single-file briefing for any agent continuing optimization work on this VLIW kernel. Read this instead of the 30+ individual reports.

## Latest Handoff

- 2026-02-11 (1381-cycle update): `docs/OPTIMIZATION_CONTINUATION_HANDOFF_2026-02-11_1381.md`
- 2026-02-11 continuation log: `docs/OPTIMIZATION_CONTINUATION_HANDOFF_2026-02-11.md`
- Read this first for the newest experiment outcomes, failed attempts, and next-step recommendations.

## The Problem

We are optimizing a **VLIW SIMD kernel** that performs parallel tree traversal on a custom simulated architecture. The kernel processes a batch of 256 inputs through 16 rounds on a binary tree of height 10 (2047 nodes).

**Each round per batch element:**
1. Look up `node_val = tree[idx]` (memory gather)
2. `val = myhash(val ^ node_val)` (6-stage hash, each stage = 2-3 ops)
3. `idx = 2*idx + (1 if val%2==0 else 2)` (branch decision)
4. Wrap `idx` if it falls off the tree

**Architecture constraints (SLOT_LIMITS per cycle):**
| Engine | Slots/cycle | Notes |
|--------|------------|-------|
| `alu` | 12 | Scalar arithmetic |
| `valu` | 6 | Vector arithmetic (VLEN=8) |
| `load` | 2 | Memory/const loads |
| `store` | 2 | Memory stores |
| `flow` | 1 | Select, jump, vselect |
| `debug` | 64 | Debug only, ignored in submission |

**Key ISA features:**
- `multiply_add(dest, a, b, c)` = `a*b + c` (single VALU slot)
- `vselect(dest, cond, a, b)` = per-lane select (single flow slot)
- `load_offset(dest, addr, offset)` = gather with static offset
- `vload/vstore` = contiguous 8-element vector load/store
- Scratch space: 1536 words (registers + constants + arrays)
- All effects take place at end of cycle (write-after-read within same cycle is safe)

**Scoring:**
- Baseline: **147,734 cycles**
- Submission test: `python tests/submission_tests.py`
- Only final `values` array is checked (not indices)

---

## Current State

| Metric | Value |
|--------|-------|
| **Cycle count** | **1381** |
| **Speedup** | **106.976x** over baseline |
| **Tests passing** | **8/9** |
| **Only failing test** | `test_opus45_improved_harness` (requires < 1363, need 18 more cycles) |

**Current bottlenecks (from latest diagnostics):**
- `strict_dep_wait`: dominant blocker (`7,467,908`)
- `engine_full`: secondary blocker (`128,810`)
- estimated schedule headroom: `53` cycles (lower bound `1328` vs measured `1381`)

---

## Optimization History (What Worked)

### Phase 1: Foundation (147,734 → 2,177 cycles) — Tasks 1-16

| Optimization | Cycles | Delta | Key Insight |
|-------------|--------|-------|-------------|
| VLIW scheduler with dependency tracking | 13,391 | -134,343 | Packs independent ops into same cycle |
| SIMD vectorization + gather | 14,415 | (combined) | Process 8 elements at once |
| Multi-group interleaving (8 groups) | 2,660 | -11,755 | Overlaps independent dependency chains |
| Pre-broadcast constants + dedup | 2,636 | -24 | Eliminate redundant const loads |
| DCE optimizer pass | 2,632 | -4 | Remove dead writes |
| **Batch in scratch across rounds** | **2,177** | **-455** | **Biggest single-task win**: load batch once, operate in scratch, store once at end |

### Phase 2: Algorithmic Depth Exploitation (2,177 → 1,486 cycles) — Opts 2-6

| Optimization | Before → After | Delta | Key Insight |
|-------------|---------------|-------|-------------|
| **Depth-aware gather elimination** | 2,123 → 1,764 | **-359** | Depths 0-2 have deterministic node sets (all start at idx=0). Preload nodes 0-6 as constants, select with arithmetic/vselect instead of memory gather. Eliminated 1536 loads. |
| **Interleave group grid search** | 1,764 → 1,566 | -198 | Increased interleave from 8→26 groups. Sweet spot found via grid search. |
| **Depth-1/2 VALU reduction** | 1,563 → 1,547 | -16 | Algebraic rewrites: single `multiply_add` instead of multiple ops for node selection |
| **Flow-branch select + retune** | 1,547 → 1,486 | -61 | Moved `branch = (val&1)+1` from VALU to `flow.vselect`, freeing VALU pressure. Retuned interleave to 25/26 split. |

### Phase 3: Micro-Optimizations (1,486 → 1,446 cycles) — Opts 8-11

| Optimization | Before → After | Delta | Key Insight |
|-------------|---------------|-------|-------------|
| Depth-2 pairwise flow select | 1,486 → 1,478 | -8 | Careful single vselect (not a tree) with explicit dependencies |
| Non-debug index traffic elimination | 1,478 → 1,474 | -4 | Submission only checks values, skip idx loads/stores |
| Compact early-depth index state | 1,474 → 1,453 | -21 | Carry path bits instead of full idx for depths 0-2 |
| Dead-setup pruning + interleave retune | 1,453 → 1,446 | -7 | Remove unused vectors in submission path, raise early interleave to 29 |

---

## What Did NOT Work (Critical Lessons)

### 1. Root Node Caching (Task 17) — REVERTED
- **Attempted:** Cache root node in scratch, conditional blend `if idx==0`
- **Result:** 2,177 → 2,351 cycles (regression)
- **Why:** Extra compare/blend ops added more overhead than the cache saved. Static scratch addressing means you can't index by runtime idx.

### 2. Depth-2 Full Flow Select Tree (Opt 7) — REVERTED
- **Attempted:** Replace entire depth-2 arithmetic with three `flow.vselect` ops
- **Result:** Broke correctness (0/9 tests passing)
- **Why:** Scheduler reordering with overlapping scratch registers creates WAR hazards. Flow-select trees are extremely fragile without explicit dependency anchoring.

### 3. Depth-3 Compact State (Opt 13) — REVERTED
- **Attempted:** Extend compact traversal through depth-3, preload nodes 7-14, 8-way flow.vselect tree
- **Result:** Correctness fixed, but regressed to ~1681 cycles
- **Why:** Three compounding problems:
  1. **Flow pressure explosion** — 8-way select needs many `flow.vselect` ops, but flow has only 1 slot/cycle
  2. **Scratch pressure** — New vectors forced lowering early interleave from 29→26
  3. **Setup overhead** — Node preloads/broadcasts didn't amortize

### 4. Vector Address Precompute (Opt 12) — REVERTED
- **Attempted:** Pre-compute and reuse vector addresses across rounds
- **Result:** Correctness broke (scheduling/dependency hazard)
- **Why:** Address-form rewrites are fragile under VLIW reordering unless dependency anchoring is explicit

### 5. Various Parameter Sweeps (Opt 12) — NO GAIN
- Searched hundreds of scheduler configs (crit_weight, engine_bias, interleave combos)
- **Result:** No config beat 1446
- **Conclusion:** Kernel is at a local optimum for current transformation set

### 6. Micro-optimizations with ~0 Impact
- `multiply_add` for hash stages initially added 1 cycle (setup overhead)
- `*2` → `<<1` replacement: no measurable change
- Scheduler tie-break by slot width: regressed by 1 cycle
- Depth-2 idx materialization VALU→FLOW trade: regressed by 1 cycle

---

## Key Architectural Patterns Discovered

### What reduces cycles
1. **Eliminate memory traffic** — Moving data to scratch, eliminating gathers for deterministic depths
2. **Increase interleave width** — More independent groups = better scheduling freedom
3. **Algebraic simplification** — Fewer ops per element, especially using `multiply_add`
4. **Engine load-balancing** — Move work from saturated engine (VALU) to idle one (flow), but carefully
5. **Dead-work elimination** — Skip idx writes, wrap checks, unused header loads in submission path

### What increases cycles (traps)
1. **Flow-heavy selection trees** — Flow has 1 slot/cycle; any tree structure serializes badly
2. **Scratch pressure** — Forces lower interleave, which reduces scheduling freedom
3. **Register/temp reuse near flow ops** — Causes scheduler WAR hazards and correctness breaks
4. **Conditional blend logic** — Compare + blend adds ops that rarely pay for themselves
5. **Pre-computation that doesn't amortize** — Setup costs for things used rarely

### The fundamental tension
The kernel is **dual-bottlenecked on VALU and load**. Any optimization that reduces one tends to increase the other or increase flow pressure. The only "free" improvements left are:
- Reducing total work (fewer ops per element)
- Better scheduling (unlikely given extensive search)
- Structural changes that reduce both VALU and load simultaneously

---

## Current Kernel Architecture (in perf_takehome.py)

### KernelBuilder Parameters
```python
KernelBuilder(
    emit_debug=False,           # Debug mode (submission=False)
    interleave_groups=25,       # Groups for depth 3+ (gather depths)
    interleave_groups_early=29, # Groups for depths 0-2 (no gathers)
    depth2_select_mode="flow_vselect",  # Depth-2 node selection
    idx_branch_mode="flow_vselect",     # Branch decision mode
    scheduler_crit_weight=136,          # Scheduler priority weight
    scheduler_succ_weight=3584,         # Successor-unblock weight
    scheduler_random_seed=51,           # Deterministic tie-breaking seed
)
```

### Kernel Structure
1. **Header** — Load pointers, allocate constants, preload nodes 0-6 into vectors
2. **Body per round** (16 rounds, depth cycles 0→10→0→10→...):
   - **Depth 0:** XOR with preloaded root (vec_node0), hash, store branch bit
   - **Depth 1:** Select node1/node2 via compact path bit, hash
   - **Depth 2:** 4-way select from nodes 3-6 via compact path bits + vselect, hash
   - **Depth 3+:** Memory gather via `load_offset`, hash, compact path update (`path = 2*path + bit`) in submission mode
   - Final depth: skip idx update (dead in submission mode)
3. **Epilogue** — Store values back to memory

### Submission-path optimizations (gated on `not self.emit_debug`)
- Skip loading/storing indices
- Skip n_nodes loading
- Skip wrap checks
- Use compact state for depths 0-2
- Use compact path state for depths 3+ (submission-only mode in current best config)
- Skip idx update at forest_height and final round

---

## Remaining Target

**Need: < 1363 cycles (currently 1407, gap = 44 cycles)**

### Theoretical Lower Bound Analysis
Current measured lower bounds from op counts:
- VALU min: ~1327.8 cycles
- Load min: ~1300.5 cycles
- Flow min: ~342 cycles

Current overhead above VALU minimum is ~79.2 cycles (1407 - 1327.8), leaving room for additional packing and/or work elimination.

### Most Promising Unexplored Directions

1. **VALU-centric depth-3 selection (avoid flow)**
   - Use bitwise one-hot/mux arithmetic instead of flow.vselect tree
   - E.g., `result = sum(node_i * (path == i) for i in range(8))` using VALU multiply_add
   - Risk: may still be too many VALU ops, but avoids flow bottleneck
   - Potential: eliminates 8 gather loads per depth-3 iteration

2. **Partial depth-3 optimization**
   - Don't try to eliminate ALL depth-3 gathers
   - Cache only the most frequently hit nodes (statistical approach)
   - Or optimize only half the selection tree

3. **Software pipelining across rounds**
   - Currently rounds are processed sequentially
   - Pipeline: start round N+1 hash while round N's idx update completes
   - Risk: complex dependency management

4. **Algebraic hash simplification**
   - The 6-stage hash dominates cycle count
   - Look for algebraic identities that reduce stages or ops
   - Stage patterns: 3 stages use `op1="+"`, `op2="+"`, `op3="<<"` which already have multiply_add optimization
   - Remaining 3 stages may have similar opportunities

5. **Scratch layout optimization**
   - Reorder scratch allocations to minimize register pressure
   - Phase-split allocations (reuse scratch between non-overlapping phases)

6. **Different interleave strategy per depth**
   - Currently: 29 groups for depths 0-2, 25 groups for 3+
   - Could try different widths for each specific depth

### Known Dead Ends (Don't Retry)
- Full flow.vselect trees for depth-2 or depth-3 selection
- Address precompute/reuse across rounds
- Root node caching with conditional blend
- Interleave groups >= 30 (scratch overflow/correctness issues)
- Beam scheduling and multi-start seeds as defaults (`beam_width>=2` and multi-starts were consistently worse in this codebase state)

---

## How to Validate Changes

```bash
# Quick correctness + cycle count
python perf_takehome.py Tests.test_kernel_cycles

# Full submission validation (correctness over 8 random seeds + speed thresholds)
python tests/submission_tests.py

# Debug trace (requires emit_debug=True, view in Perfetto)
python perf_takehome.py Tests.test_kernel_trace

# Diagnostics with schedule analysis
python perf_takehome.py Tests.test_kernel_cycles  # with diagnostics_out set
```

**Critical:** Always run `python tests/submission_tests.py` before considering any change a win. The correctness test runs 8 random seeds.

---

## File Map

| File | Purpose |
|------|---------|
| `perf_takehome.py` | **THE kernel to optimize** (KernelBuilder.build_kernel) |
| `problem.py` | Machine simulator, ISA definition, reference kernel |
| `tests/submission_tests.py` | Official submission tests (frozen simulator) |
| `tests/frozen_problem.py` | Frozen copy of simulator for scoring |

---

*Last updated: 2026-02-10 | Current best: 1407 cycles | Target: < 1363 cycles*

## 2026-02-10 Aggressive Follow-Up (1430 Baseline, Historical Snapshot)

This section captures the state before the compact depth3+ path and scheduler retune that reached 1407.

### Dominant Configuration (kept)
- `depth4_mode="off"` (depth-4 deterministic path is available but default-disabled)
- `scheduler_beam_width=1`
- `scheduler_multi_start_seeds=None`
- `scheduler_succ_weight=512`
- Observed reproducible submission cycles: **1430**

### Rejected/Disabled Configurations
- `depth4_mode="deterministic16"` with adaptive interleave:
  - Measured range: **2211–2326 cycles** (large regression)
  - Reason: depth-4 deterministic select creates heavy flow/VALU serialization.
- Beam scheduling (`scheduler_beam_width>=2`):
  - Best observed with beam: **1443+**; many configs far worse.
  - Reason: local slot-fill heuristic hurts global dependency ordering.
- Multi-start seeds (`scheduler_multi_start_seeds` non-empty):
  - Best observed: **1433** (no win over deterministic baseline).
  - Kept as optional tooling for diagnostics only.

### Phase-6 Queue Evidence
- Randomized combined search with acceptance gate:
  - `python tools/opt_debug/auto_optimize.py --backend random --trials 30 --seed 123 --out-dir docs/reports/optimizations/phase6_queue --accept-if-better-than 1430`
  - Result: `accepted_winner=false` (no candidate below 1430).

## 2026-02-10 High-Signal Experiment Log (1430 -> 1407)

### Goal
- Push the stable submission configuration from `1430` lower without correctness regressions.

### What Worked
- **Submission-path compact path state for depth 3+**
  - Net: `1430 -> 1427` (improvement).
  - Code points:
    - Mode gate: `perf_takehome.py:974`
    - Per-depth base vectors: `perf_takehome.py:1068`
    - Depth 3+ gather with compact path: `perf_takehome.py:1443`
    - Depth-2/3+ compact update path: `perf_takehome.py:1500`
  - Why it helped:
    - Eliminated depth 3+ `flow.vselect` branch-update usage in submission mode.
    - Reduced flow pressure and total ops in the hot loop.

- **Scheduler retuning after the structural change**
  - Net: `1427 -> 1407` (improvement).
  - Winning default tuple:
    - `scheduler_crit_weight=136`
    - `scheduler_succ_weight=3584`
    - `scheduler_random_seed=51`
    - `scheduler_beam_width=1`
  - Why it helped:
    - The compact-path DAG changed dependency geometry enough that a different priority balance produced tighter schedules.

### What Did Not Improve (This Round)
- **Scheduler local compaction post-pass**
  - A safe “pull ops earlier” pass was implemented and tested.
  - Result: no cycle gain over baseline scheduler on this kernel shape.
  - Action: removed to keep scheduler simple.

- **Beam width > 1**
  - Consistently worse than `beam_width=1` in this codebase state.
  - Kept as optional knob, not used in defaults.

- **Multi-start seed mode as default**
  - Added overhead and did not outperform best deterministic single-seed defaults for stable submission config.

- **Interleave retunes with current kernel shape**
  - Additional sweeps around `(interleave_groups=25, interleave_groups_early=29)` did not beat the new 1407 default.

### Validation Protocol Used
- Guardrail:
  - `git diff origin/main tests/` must be empty.
- Correctness + official score:
  - `python tests/submission_tests.py`
  - Result on current best: `CYCLES: 1407`, correctness pass, speed thresholds pass except `<1363`.
- Local sanity:
  - `python perf_takehome.py` (all local tests pass; debug trace path remains functional).

### Future-Agent Notes (Actionable)
- Preserve the compact depth3+ path mechanism; it is a real structural win.
- Treat scheduler tuning as coupled to DAG shape; when structure changes, re-sweep scheduler weights/seeds.
- Prioritize experiments that either:
  - reduce VALU op count in depth 3+ rounds, or
  - reduce load-engine occupancy without increasing VALU beyond saturation.
