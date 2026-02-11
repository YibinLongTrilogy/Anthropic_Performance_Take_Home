# Inefficiency Tooling Guide

This guide explains how to use the new dependency-focused debugging tool:

- Runner: `tools/opt_debug/run_inefficiency_report.py`
- Analyzer: `tools/opt_debug/inefficiency_report.py`
- Scheduler decision runner: `tools/opt_debug/run_scheduler_decision_report.py`
- Scratch lifetime runner: `tools/opt_debug/run_lifetime_report.py`
- Diff runner: `tools/opt_debug/run_compare_inefficiency.py`

It is designed to answer:
- Where are cycles going?
- Are we blocked by dependency chains, slot limits, or scheduler choices?
- Which scratch locations create the most schedule pressure?
- Which scheduler choices skip feasible ready work?
- Which scratch values stay live too long and overlap heavily?

## Quick Start

Run one analysis pass on the default kernel config:

```bash
python tools/opt_debug/run_inefficiency_report.py \
  --out-dir docs/reports/optimizations/debug
```

This writes:

- `docs/reports/optimizations/debug/latest_inefficiency.json`
- `docs/reports/optimizations/debug/latest_inefficiency.md`

It also prints a compact JSON summary with:

- `cycles`
- `correct`
- `estimated_headroom_cycles`
- `top_blockers`

## Additional Reports

### Scheduler Decision Trace Report

Use this when you want to understand where scheduler heuristics are leaving cycles on the table.

```bash
python tools/opt_debug/run_scheduler_decision_report.py \
  --kernel-kwargs-json '{"scheduler_beam_width": 2}' \
  --out-dir docs/reports/optimizations/debug
```

Writes:

- `latest_scheduler_decisions.json`
- `latest_scheduler_decisions.md`

Key fields:

- `feasible_not_chosen_pct`
- `global_rejections` (`beam_choice`, `slot_fragmentation`, `strict_dep_wait`, ...)
- `top_skipped_patterns` (engine/opcode combinations repeatedly skipped)

### Scratch Lifetime Report

Use this to find long-lived temporaries that constrain reordering freedom.

```bash
python tools/opt_debug/run_lifetime_report.py \
  --out-dir docs/reports/optimizations/debug
```

Writes:

- `latest_lifetimes.json`
- `latest_lifetimes.md`

Key fields:

- `max_live_overlap`
- `top_lifetimes` (ranked by pressure score)
- `split_candidates` with overlap partners

### Inefficiency Diff Report

Use this after an optimization change to compare baseline vs candidate behavior.

```bash
python tools/opt_debug/run_compare_inefficiency.py \
  --base-json docs/reports/optimizations/debug/baseline_inefficiency.json \
  --candidate-json docs/reports/optimizations/debug/latest_inefficiency.json \
  --out-dir docs/reports/optimizations/debug
```

Writes:

- `latest_inefficiency_diff.json`
- `latest_inefficiency_diff.md`

Key fields:

- `cycle_delta`
- `headroom_delta`
- per-segment cycle/headroom deltas
- scratch hotspot deltas (`tight_delta`, `near_strict_delta`, `dep_edges_delta`)

## Run With Custom Kernel Config

Use `--kernel-kwargs-json` to pass any `KernelBuilder(...)` args:

```bash
python tools/opt_debug/run_inefficiency_report.py \
  --kernel-kwargs-json '{"scheduler_beam_width": 2, "scheduler_multi_start_seeds": [11,17,23]}' \
  --out-dir docs/reports/optimizations/debug
```

Common flags:

- `--seed`
- `--forest-height`
- `--rounds`
- `--batch-size`
- `--kernel-kwargs-json`
- `--out-dir`

## What The Tool Computes

For each scheduled segment (between flow barriers), it computes:

- Dependency lower bound (from strict/weak dependency graph)
- Engine lower bound (from total slots per engine / slot limits)
- Combined lower bound (`max(dependency_lb, engine_lb)`)
- Headroom (`actual_cycles - combined_lb`)
- Per-op slack (`scheduled_cycle - earliest_possible_cycle`)
- Cycle blockers:
  - `strict_dep_wait`
  - `weak_dep_wait`
  - `engine_full`
  - `slot_fragmentation`
  - `scheduler_choice`
- Idle-slot reasons per engine:
  - `no_ready_ops`
  - `slot_fragmentation`
  - `scheduler_choice`
  - `dependency_tail`
- Scratch hotspots ranked by **tight dependency edges** (not just raw reads/writes)

## How To Read `latest_inefficiency.md`

Focus on these sections in order:

1. `Summary`
- `Estimated headroom` is your immediate schedule-quality gap.
- If this is small, gains likely require reducing total work, not scheduling tweaks.

2. `Global Blockers`
- If `strict_dep_wait` dominates: dependency chains are the main constraint.
- If `engine_full` dominates: the bottleneck engine is truly saturated.
- If `scheduler_choice` appears materially: improve scheduler search (beam width / seeds).

3. `Idle Slot Reasons`
- High `no_ready_ops` means upstream deps limit fill, not slot limits.
- High `scheduler_choice` means slots were left idle despite fit-ready ops.

4. `Top Scratch Hotspots`
- High `tight` and `near_strict` values identify addresses that serialize execution.
- This is where adding temporaries or changing value lifetimes can help.

5. `Top Slack Ops` and `Longest Dependency Chain`
- Use these to locate late-scheduled ops with large slack and trace their parent chain.
- This helps target concrete code regions causing chain extension.

## Optimization Loop (Recommended)

1. Run baseline report.
2. Change one kernel knob or transformation.
3. Re-run report with same seed/problem size.
4. Compare:
- `cycles`
- `estimated_headroom_cycles`
- `global_blockers`
- `Top Scratch Hotspots` (`tight`/`near_strict`)
5. Keep only changes that improve both correctness and cycle count.

## Programmatic Use

You can call the analyzer directly from Python:

```python
from tools.opt_debug.inefficiency_report import analyze_inefficiency_report

report = analyze_inefficiency_report(
    instrs=kb.instrs,
    schedule_profile=kb.schedule_profile(),
    scratch_map=kb.debug_info().scratch_map,
    metadata={"cycles": machine.cycle, "correct": True},
)
```

To render/write:

```python
from tools.opt_debug.inefficiency_report import write_inefficiency_artifacts

json_path, md_path = write_inefficiency_artifacts(report, "docs/reports/optimizations/debug")
```

## Sanity Check

Run:

```bash
python tools/opt_debug/selfcheck.py
```

This validates both schedule diagnostics and inefficiency diagnostics on synthetic and real kernel runs.

## Validation Reminder

Do not modify `tests/`. Verify and run submission tests with:

```bash
git diff -- tests/
python tests/submission_tests.py
```
