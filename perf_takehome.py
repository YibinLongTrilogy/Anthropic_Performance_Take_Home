"""
# Anthropic's Original Performance Engineering Take-home (Release version)

Copyright Anthropic PBC 2026. Permission is granted to modify and use, but not
to publish or redistribute your solutions so it's hard to find spoilers.

# Task

- Optimize the kernel (in KernelBuilder.build_kernel) as much as possible in the
  available time, as measured by test_kernel_cycles on a frozen separate copy
  of the simulator.

Validate your results using `python tests/submission_tests.py` without modifying
anything in the tests/ folder.

We recommend you look through problem.py next.
"""

from collections import defaultdict
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import unittest

from problem import (
    Engine,
    DebugInfo,
    SLOT_LIMITS,
    VLEN,
    N_CORES,
    SCRATCH_SIZE,
    Machine,
    Tree,
    Input,
    HASH_STAGES,
    reference_kernel,
    build_mem_image,
    reference_kernel2,
)


def analyze_utilization(instrs, slot_limits=SLOT_LIMITS, include_debug=False):
    engines = [engine for engine in slot_limits if engine != "debug"]
    totals = {engine: 0 for engine in engines}
    mins = {engine: None for engine in engines}
    maxs = {engine: 0 for engine in engines}
    cycle_count = 0

    for instr in instrs:
        has_non_debug = any(name != "debug" for name in instr.keys())
        if not has_non_debug and not include_debug:
            continue
        cycle_count += 1
        for engine in engines:
            count = len(instr.get(engine, []))
            totals[engine] += count
            if mins[engine] is None or count < mins[engine]:
                mins[engine] = count
            if count > maxs[engine]:
                maxs[engine] = count

    stats = {"cycle_count": cycle_count, "engines": {}}
    for engine in engines:
        avg = totals[engine] / cycle_count if cycle_count else 0
        util_pct = (avg / slot_limits[engine] * 100) if cycle_count else 0
        stats["engines"][engine] = {
            "total": totals[engine],
            "avg": avg,
            "min": mins[engine] if mins[engine] is not None else 0,
            "max": maxs[engine],
            "util_pct": util_pct,
            "limit": slot_limits[engine],
        }
    return stats


def format_utilization(stats):
    lines = [f"Slot utilization over {stats['cycle_count']} cycles:"]
    for engine in (engine for engine in SLOT_LIMITS if engine != "debug"):
        data = stats["engines"][engine]
        lines.append(
            f"{engine}: avg {data['avg']:.2f}/{data['limit']} "
            f"({data['util_pct']:.1f}%), min {data['min']}, max {data['max']}"
        )
    return "\n".join(lines)


def print_utilization(instrs, include_debug=False):
    stats = analyze_utilization(instrs, include_debug=include_debug)
    print(format_utilization(stats))


def _non_debug_cycle_slot_counts(instrs, slot_limits=SLOT_LIMITS, include_debug=False):
    engines = [engine for engine in slot_limits if engine != "debug"]
    cycles = []
    for instr in instrs:
        has_non_debug = any(name != "debug" for name in instr.keys())
        if not has_non_debug and not include_debug:
            continue
        cycles.append({engine: len(instr.get(engine, [])) for engine in engines})
    return cycles


def analyze_schedule(instrs, metadata=None, include_debug=False):
    metadata = metadata or {}
    util = analyze_utilization(instrs, include_debug=include_debug)
    cycle_rows = _non_debug_cycle_slot_counts(instrs, include_debug=include_debug)

    engine_pressure = {}
    for engine in (engine for engine in SLOT_LIMITS if engine != "debug"):
        limit = SLOT_LIMITS[engine]
        idle_slots = sum(limit - row[engine] for row in cycle_rows)
        saturation_cycles = sum(
            1 for row in cycle_rows if row[engine] == limit and row[engine] > 0
        )
        nonzero_cycles = sum(1 for row in cycle_rows if row[engine] > 0)
        engine_pressure[engine] = {
            **util["engines"][engine],
            "idle_slots": idle_slots,
            "saturation_cycles": saturation_cycles,
            "active_cycles": nonzero_cycles,
        }

    bottlenecks = []
    for engine, stats in sorted(
        engine_pressure.items(), key=lambda item: item[1]["util_pct"], reverse=True
    ):
        if stats["util_pct"] >= 70:
            bottlenecks.append(
                {
                    "engine": engine,
                    "reason": f"{engine} utilization {stats['util_pct']:.1f}%",
                    "util_pct": stats["util_pct"],
                }
            )
        elif stats["saturation_cycles"] > 0 and util["cycle_count"] > 0:
            sat_pct = (stats["saturation_cycles"] / util["cycle_count"]) * 100
            if sat_pct >= 25:
                bottlenecks.append(
                    {
                        "engine": engine,
                        "reason": f"{engine} saturated for {sat_pct:.1f}% cycles",
                        "util_pct": stats["util_pct"],
                    }
                )

    phases = []
    profile = metadata.get("schedule_profile") or {}
    for segment in profile.get("segments", []):
        cycles = segment.get("cycle_engine_counts", [])
        phase_stats = {}
        n_cycles = len(cycles)
        for engine in (engine for engine in SLOT_LIMITS if engine != "debug"):
            limit = SLOT_LIMITS[engine]
            total = sum(row.get(engine, 0) for row in cycles)
            avg = total / n_cycles if n_cycles else 0
            phase_stats[engine] = {
                "avg": avg,
                "util_pct": (avg / limit * 100) if n_cycles else 0,
            }
        phases.append(
            {
                "phase": segment.get("phase", "segment"),
                "cycles": n_cycles,
                "engine_utilization": phase_stats,
            }
        )

    critical_path_top = []
    all_ops = []
    for segment in profile.get("segments", []):
        phase = segment.get("phase", "segment")
        for op in segment.get("ops", []):
            all_ops.append((phase, op))
    for phase, op in sorted(
        all_ops,
        key=lambda item: (item[1].get("crit_path", 0), item[1].get("slot_count", 0)),
        reverse=True,
    )[:40]:
        critical_path_top.append(
            {
                "phase": phase,
                "op_id": op.get("op_id"),
                "engine": op.get("engine"),
                "slot_count": op.get("slot_count"),
                "crit_path": op.get("crit_path"),
                "cycle": op.get("scheduled_cycle"),
            }
        )

    return {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "cycle_count": util["cycle_count"],
        "engine_pressure": engine_pressure,
        "bottlenecks": bottlenecks,
        "phases": phases,
        "critical_path_top": critical_path_top,
        "metadata": metadata,
    }


def format_schedule_report(report):
    lines = [
        f"# Kernel Diagnostics ({report['cycle_count']} cycles)",
        "",
        "## Engine Pressure",
    ]
    for engine in (engine for engine in SLOT_LIMITS if engine != "debug"):
        stats = report["engine_pressure"][engine]
        lines.append(
            f"- `{engine}`: avg {stats['avg']:.2f}/{stats['limit']} "
            f"({stats['util_pct']:.1f}%), saturated cycles {stats['saturation_cycles']}, "
            f"idle slots {stats['idle_slots']}"
        )
    lines.append("")
    lines.append("## Bottlenecks")
    if report["bottlenecks"]:
        for bottleneck in report["bottlenecks"]:
            lines.append(f"- {bottleneck['reason']}")
    else:
        lines.append("- No engine crossed the bottleneck thresholds.")

    lines.append("")
    lines.append("## Critical Path Hotspots")
    if report["critical_path_top"]:
        for node in report["critical_path_top"][:12]:
            lines.append(
                f"- phase `{node['phase']}` op#{node['op_id']} "
                f"engine `{node['engine']}` crit_path={node['crit_path']} cycle={node['cycle']}"
            )
    else:
        lines.append("- Scheduler profiling not enabled for this run.")

    if report["phases"]:
        lines.append("")
        lines.append("## Phase Breakdown")
        for phase in report["phases"]:
            parts = []
            for engine in (engine for engine in SLOT_LIMITS if engine != "debug"):
                util_pct = phase["engine_utilization"][engine]["util_pct"]
                parts.append(f"{engine} {util_pct:.1f}%")
            lines.append(
                f"- `{phase['phase']}`: {phase['cycles']} cycles ({', '.join(parts)})"
            )

    return "\n".join(lines) + "\n"


def write_diagnostics_artifacts(report, diagnostics_out):
    out_path = Path(diagnostics_out)
    if out_path.suffix.lower() == ".json":
        json_path = out_path
        md_path = out_path.with_suffix(".md")
    else:
        out_path.mkdir(parents=True, exist_ok=True)
        json_path = out_path / "latest_run.json"
        md_path = out_path / "latest_run.md"

    json_path.parent.mkdir(parents=True, exist_ok=True)
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(format_schedule_report(report), encoding="utf-8")
    return str(json_path), str(md_path)


class KernelBuilder:
    def __init__(
        self,
        emit_debug: bool = False,
        interleave_groups: int = 25,
        interleave_groups_early: int | None = 29,
        depth2_select_mode: str = "flow_vselect",
        depth3_deterministic: bool = False,
        depth4_mode: str = "off",
        depth4_adaptive_interleave: bool = True,
        idx_branch_mode: str = "flow_vselect",
        trace_phase_tags: bool = False,
        scheduler_profile: bool = False,
        scheduler_crit_weight: int = 112,
        scheduler_engine_bias: dict[str, int] | None = None,
        split_hash_pairs: bool = True,
        scheduler_succ_weight: int = 4096,
        scheduler_random_seed: int | None = 111,
        scheduler_multi_start_seeds: tuple[int, ...] | list[int] | None = None,
        scheduler_beam_width: int = 1,
        fast_value_vector_ptrs: bool = True,
    ):
        self.instrs = []
        self.scratch = {}
        self.scratch_debug = {}
        self.scratch_ptr = 0
        self.const_map = {}
        self.emit_debug = emit_debug
        self.interleave_groups = interleave_groups
        self.interleave_groups_early = (
            interleave_groups if interleave_groups_early is None else interleave_groups_early
        )
        self.depth2_select_mode = depth2_select_mode
        self.depth3_deterministic = depth3_deterministic
        if depth4_mode not in {"off", "deterministic16"}:
            raise ValueError(f"Unsupported depth4_mode={depth4_mode}")
        self.depth4_mode = depth4_mode
        self.depth4_adaptive_interleave = depth4_adaptive_interleave
        self.idx_branch_mode = idx_branch_mode
        self.trace_phase_tags = trace_phase_tags
        self.scheduler_profile_enabled = scheduler_profile
        self.scheduler_crit_weight = scheduler_crit_weight
        self.scheduler_engine_bias = scheduler_engine_bias or {}
        self.split_hash_pairs = split_hash_pairs
        self.scheduler_succ_weight = scheduler_succ_weight
        self.scheduler_random_seed = scheduler_random_seed
        self.scheduler_multi_start_seeds = (
            tuple(scheduler_multi_start_seeds)
            if scheduler_multi_start_seeds is not None
            else None
        )
        self.scheduler_beam_width = max(1, int(scheduler_beam_width))
        self.fast_value_vector_ptrs = fast_value_vector_ptrs
        self._schedule_segments = []

    def debug_info(self):
        return DebugInfo(scratch_map=self.scratch_debug)

    def schedule_profile(self):
        return {"segments": deepcopy(self._schedule_segments)}

    def _slot_reads_writes(self, engine, slot):
        reads = set()
        writes = set()

        def add_range(base, length):
            for i in range(length):
                writes.add(base + i)

        def add_read_range(base, length):
            for i in range(length):
                reads.add(base + i)

        if engine == "alu":
            _, dest, a1, a2 = slot
            reads.update([a1, a2])
            writes.add(dest)
        elif engine == "valu":
            match slot:
                case ("vbroadcast", dest, src):
                    reads.add(src)
                    add_range(dest, VLEN)
                case ("multiply_add", dest, a, b, c):
                    add_read_range(a, VLEN)
                    add_read_range(b, VLEN)
                    add_read_range(c, VLEN)
                    add_range(dest, VLEN)
                case (_, dest, a1, a2):
                    add_read_range(a1, VLEN)
                    add_read_range(a2, VLEN)
                    add_range(dest, VLEN)
        elif engine == "load":
            match slot:
                case ("load", dest, addr):
                    reads.add(addr)
                    writes.add(dest)
                case ("load_offset", dest, addr, offset):
                    reads.add(addr + offset)
                    writes.add(dest + offset)
                case ("vload", dest, addr):
                    reads.add(addr)
                    add_range(dest, VLEN)
                case ("const", dest, _):
                    writes.add(dest)
        elif engine == "store":
            match slot:
                case ("store", addr, src):
                    reads.update([addr, src])
                case ("vstore", addr, src):
                    reads.add(addr)
                    add_read_range(src, VLEN)
        elif engine == "flow":
            match slot:
                case ("select", dest, cond, a, b):
                    reads.update([cond, a, b])
                    writes.add(dest)
                case ("add_imm", dest, a, _):
                    reads.add(a)
                    writes.add(dest)
                case ("vselect", dest, cond, a, b):
                    add_read_range(cond, VLEN)
                    add_read_range(a, VLEN)
                    add_read_range(b, VLEN)
                    add_range(dest, VLEN)
                case ("halt",):
                    pass
                case ("pause",):
                    pass
                case ("trace_write", val):
                    reads.add(val)
                case ("cond_jump", cond, _):
                    reads.add(cond)
                case ("cond_jump_rel", cond, _):
                    reads.add(cond)
                case ("jump", _):
                    pass
                case ("jump_indirect", addr):
                    reads.add(addr)
                case ("coreid", dest):
                    writes.add(dest)
        elif engine == "debug":
            match slot:
                case ("compare", loc, _):
                    reads.add(loc)
                case ("vcompare", loc, _):
                    add_read_range(loc, VLEN)
                case ("comment", _):
                    pass

        return reads, writes

    def _is_barrier(self, engine: Engine, slot: tuple) -> bool:
        if engine != "flow":
            return False
        return slot[0] in {
            "halt",
            "pause",
            "cond_jump",
            "cond_jump_rel",
            "jump",
            "jump_indirect",
        }

    def _slot_side_effect(self, engine: Engine, slot: tuple) -> bool:
        if engine == "store":
            return True
        if engine == "flow":
            return True
        if engine == "debug":
            return self.emit_debug
        return False

    def _optimize_slots(self, slots: list[tuple[Engine, tuple]]):
        if not slots:
            return []

        live = set()
        optimized = []

        for engine, slot in reversed(slots):
            if isinstance(slot, list):
                kept = []
                reads_all = set()
                writes_all = set()
                for subslot in slot:
                    reads, writes = self._slot_reads_writes(engine, subslot)
                    side_effect = self._slot_side_effect(engine, subslot)
                    if side_effect or (writes & live):
                        kept.append(subslot)
                        reads_all.update(reads)
                        writes_all.update(writes)
                if not kept:
                    continue
                live.difference_update(writes_all)
                live.update(reads_all)
                optimized.append((engine, kept))
            else:
                reads, writes = self._slot_reads_writes(engine, slot)
                side_effect = self._slot_side_effect(engine, slot)
                if side_effect or (writes & live):
                    live.difference_update(writes)
                    live.update(reads)
                    optimized.append((engine, slot))

        optimized.reverse()
        return optimized

    def _schedule_vliw(
        self,
        slots: list[tuple[Engine, tuple]],
        phase_tag: str | None = None,
        random_seed: int | None = None,
    ):
        if not slots:
            return [], None

        ops = []
        for engine, slot in slots:
            if isinstance(slot, list):
                reads = set()
                writes = set()
                for subslot in slot:
                    sub_reads, sub_writes = self._slot_reads_writes(engine, subslot)
                    reads.update(sub_reads)
                    writes.update(sub_writes)
                slot_list = slot
                slot_count = len(slot)
            else:
                reads, writes = self._slot_reads_writes(engine, slot)
                slot_list = [slot]
                slot_count = 1
            ops.append((engine, slot_list, reads, writes, slot_count))

        n_ops = len(ops)
        strict_succs = [set() for _ in range(n_ops)]
        weak_succs = [set() for _ in range(n_ops)]
        strict_pred_count = [0] * n_ops
        weak_pred_count = [0] * n_ops

        last_write = [-1] * SCRATCH_SIZE
        # Track ALL readers since last write to correctly handle WAR deps.
        # Using only the last reader can miss dependencies when the scheduler
        # reorders earlier readers past a subsequent writer.
        readers_since_write: list[list[int]] = [[] for _ in range(SCRATCH_SIZE)]

        for i, (_, _, reads, writes, _) in enumerate(ops):
            for addr in reads:
                lw = last_write[addr]
                if lw != -1 and i not in strict_succs[lw]:
                    strict_succs[lw].add(i)
                    strict_pred_count[i] += 1
                readers_since_write[addr].append(i)
            for addr in writes:
                lw = last_write[addr]
                if lw != -1 and i not in strict_succs[lw]:
                    strict_succs[lw].add(i)
                    strict_pred_count[i] += 1
                for lr in readers_since_write[addr]:
                    if lr != i and i not in weak_succs[lr]:
                        weak_succs[lr].add(i)
                        weak_pred_count[i] += 1
                last_write[addr] = i
                readers_since_write[addr] = []

        import heapq

        # Compute critical path length for each op (longest path to any terminal)
        # using reverse topological order
        crit_path = [1] * n_ops  # each op has at least length 1
        # Process in reverse topological order (reverse of original index order
        # works since dependencies only go forward)
        for i in range(n_ops - 1, -1, -1):
            max_succ = 0
            for s in strict_succs[i]:
                if crit_path[s] > max_succ:
                    max_succ = crit_path[s]
            for s in weak_succs[i]:
                if crit_path[s] > max_succ:
                    max_succ = crit_path[s]
            crit_path[i] = 1 + max_succ

        op_priority = [0] * n_ops
        for i, (engine, _, _, _, _) in enumerate(ops):
            succ_count = len(strict_succs[i]) + len(weak_succs[i])
            op_priority[i] = (
                crit_path[i] * self.scheduler_crit_weight
                + succ_count * self.scheduler_succ_weight
                + self.scheduler_engine_bias.get(engine, 0)
            )

        if random_seed is not None:
            rng = random.Random(random_seed)
            for i in range(n_ops):
                op_priority[i] += rng.randint(0, self.scheduler_crit_weight // 4)

        succ_count = [len(strict_succs[i]) + len(weak_succs[i]) for i in range(n_ops)]

        ready_heap = []
        for i in range(n_ops):
            if strict_pred_count[i] == 0 and weak_pred_count[i] == 0:
                heapq.heappush(ready_heap, (-op_priority[i], i))

        max_strict_pred_cycle = [-1] * n_ops
        max_weak_pred_cycle = [-1] * n_ops
        scheduled = [False] * n_ops

        instrs = []
        cycle_engine_counts = []
        scheduled_cycle = [-1] * n_ops
        cycle = 0
        remaining = n_ops

        while remaining > 0:
            bundle = {}
            engine_counts = defaultdict(int)
            deferred = []
            scheduled_any = False

            while ready_heap:
                # Limited lookahead: score a small frontier of ready ops and choose
                # the one that best fills slots while unblocking successors.
                sampled = []
                sample_target = self.scheduler_beam_width
                for _ in range(sample_target):
                    if not ready_heap:
                        break
                    sampled.append(heapq.heappop(ready_heap))

                feasible = []
                for _, i in sampled:
                    if scheduled[i]:
                        continue
                    if max_strict_pred_cycle[i] + 1 > cycle:
                        deferred.append((-op_priority[i], i))
                        continue
                    if max_weak_pred_cycle[i] > cycle:
                        deferred.append((-op_priority[i], i))
                        continue
                    engine, _, _, _, slot_count = ops[i]
                    if engine_counts[engine] + slot_count > SLOT_LIMITS[engine]:
                        deferred.append((-op_priority[i], i))
                        continue
                    feasible.append(i)

                if not feasible:
                    continue

                def candidate_score(op_idx: int):
                    engine, _, _, _, slot_count = ops[op_idx]
                    remaining_slots = SLOT_LIMITS[engine] - engine_counts[engine]
                    slot_fill = min(remaining_slots, slot_count)
                    return (
                        slot_fill,
                        succ_count[op_idx],
                        op_priority[op_idx],
                    )

                i = max(feasible, key=candidate_score)
                for _, idx in sampled:
                    if idx != i and not scheduled[idx]:
                        deferred.append((-op_priority[idx], idx))

                engine, slot_list, _, _, slot_count = ops[i]

                scheduled_any = True
                scheduled[i] = True
                scheduled_cycle[i] = cycle
                remaining -= 1

                engine_counts[engine] += slot_count
                bundle.setdefault(engine, []).extend(slot_list)

                for succ in strict_succs[i]:
                    strict_pred_count[succ] -= 1
                    if max_strict_pred_cycle[succ] < cycle:
                        max_strict_pred_cycle[succ] = cycle
                    if strict_pred_count[succ] == 0 and weak_pred_count[succ] == 0:
                        heapq.heappush(ready_heap, (-op_priority[succ], succ))

                for succ in weak_succs[i]:
                    weak_pred_count[succ] -= 1
                    if max_weak_pred_cycle[succ] < cycle:
                        max_weak_pred_cycle[succ] = cycle
                    if strict_pred_count[succ] == 0 and weak_pred_count[succ] == 0:
                        heapq.heappush(ready_heap, (-op_priority[succ], succ))

            if not scheduled_any:
                raise RuntimeError("VLIW scheduler deadlock (no schedulable ops)")

            instrs.append(bundle)
            cycle_engine_counts.append(dict(engine_counts))
            cycle += 1

            if deferred:
                heapq.heapify(deferred)
                ready_heap = deferred
            else:
                ready_heap = []

        profile_segment = None
        if self.scheduler_profile_enabled:
            ops_meta = []
            for i, (engine, slot_list, reads, writes, slot_count) in enumerate(ops):
                ops_meta.append(
                    {
                        "op_id": i,
                        "engine": engine,
                        "slot_count": slot_count,
                        "reads": sorted(reads),
                        "writes": sorted(writes),
                        "crit_path": crit_path[i],
                        "priority": op_priority[i],
                        "scheduled_cycle": scheduled_cycle[i],
                    }
                )
            profile_segment = {
                "phase": phase_tag or "segment",
                "ops": ops_meta,
                "cycle_engine_counts": cycle_engine_counts,
                "scheduler_seed": random_seed,
                "scheduler_beam_width": self.scheduler_beam_width,
            }

        return instrs, profile_segment

    def _schedule_segment(
        self,
        segment: list[tuple[Engine, tuple]],
        tag: str,
    ) -> list[dict[str, list[tuple]]]:
        if not segment:
            return []
        if self.scheduler_multi_start_seeds is None:
            seeds = [self.scheduler_random_seed]
        else:
            seeds = list(self.scheduler_multi_start_seeds)
            if self.scheduler_random_seed is not None:
                seeds = [self.scheduler_random_seed] + seeds
            # Preserve order while deduplicating.
            seeds = list(dict.fromkeys(seeds))

        best_instrs = None
        best_profile = None
        best_cycles = None
        candidate_runs = []
        for seed in seeds:
            instrs, profile = self._schedule_vliw(segment, phase_tag=tag, random_seed=seed)
            cycles = len(instrs)
            candidate_runs.append({"seed": seed, "cycles": cycles})
            if best_cycles is None or cycles < best_cycles:
                best_cycles = cycles
                best_instrs = instrs
                best_profile = profile

        if self.scheduler_profile_enabled and best_profile is not None:
            best_profile["scheduler_candidates"] = candidate_runs
            self._schedule_segments.append(best_profile)
        return best_instrs or []

    def build(
        self,
        slots: list[tuple[Engine, tuple]],
        vliw: bool = False,
        phase_tag: str | None = None,
        optimize: bool = True,
    ):
        # Simple slot packing that just uses one slot per instruction bundle
        if not vliw:
            instrs = []
            for engine, slot in slots:
                if isinstance(slot, list):
                    instrs.append({engine: slot})
                else:
                    instrs.append({engine: [slot]})
            return instrs

        if not self.emit_debug:
            slots = [(engine, slot) for engine, slot in slots if engine != "debug"]

        instrs = []
        segment = []
        segment_idx = 0
        for engine, slot in slots:
            if self._is_barrier(engine, slot):
                tag = phase_tag if phase_tag else "segment"
                instrs.extend(
                    self._schedule_segment(
                        self._optimize_slots(segment) if optimize else segment,
                        tag=f"{tag}:{segment_idx}",
                    )
                )
                segment = []
                segment_idx += 1
                instrs.append({engine: [slot]})
            else:
                segment.append((engine, slot))

        tag = phase_tag if phase_tag else "segment"
        instrs.extend(
            self._schedule_segment(
                self._optimize_slots(segment) if optimize else segment,
                tag=f"{tag}:{segment_idx}",
            )
        )
        return instrs

    def add(self, engine, slot):
        if engine == "debug" and not self.emit_debug:
            return
        self.instrs.append({engine: [slot]})

    def alloc_scratch(self, name=None, length=1):
        addr = self.scratch_ptr
        if name is not None:
            self.scratch[name] = addr
            self.scratch_debug[addr] = (name, length)
        self.scratch_ptr += length
        assert self.scratch_ptr <= SCRATCH_SIZE, "Out of scratch space"
        return addr

    def scratch_const(self, val, name=None):
        if val not in self.const_map:
            addr = self.alloc_scratch(name)
            self.add("load", ("const", addr, val))
            self.const_map[val] = addr
        return self.const_map[val]

    def build_hash(self, val_hash_addr, tmp1, tmp2, round, i):
        slots = []

        for hi, (op1, val1, op2, op3, val3) in enumerate(HASH_STAGES):
            slots.append(
                (
                    "alu",
                    [
                        (op1, tmp1, val_hash_addr, self.scratch_const(val1)),
                        (op3, tmp2, val_hash_addr, self.scratch_const(val3)),
                    ],
                )
            )
            slots.append(("alu", (op2, val_hash_addr, tmp1, tmp2)))
            slots.append(("debug", ("compare", val_hash_addr, (round, i, "hash_stage", hi))))

        return slots

    def build_hash_vec(self, val_hash_addr, tmp1, tmp2, round, i_base, vec_const_map):
        slots = []

        for hi, (op1, val1, op2, op3, val3) in enumerate(HASH_STAGES):
            if op1 == "+" and op2 == "+" and op3 == "<<":
                # Algebraic simplification: a*(1<<shift) + (a + const)
                # = a*((1<<shift)+1) + const → single multiply_add
                combined_mul = (1 << val3) + 1
                combined_mul_vec = vec_const_map[combined_mul]
                slots.append(
                    (
                        "valu",
                        ("multiply_add", val_hash_addr, val_hash_addr, combined_mul_vec, vec_const_map[val1]),
                    )
                )
            elif op2 == "+" and op3 == "<<":
                slots.append(
                    ("valu", (op1, tmp1, val_hash_addr, vec_const_map[val1]))
                )
                mul_const = vec_const_map[1 << val3]
                slots.append(
                    (
                        "valu",
                        ("multiply_add", val_hash_addr, val_hash_addr, mul_const, tmp1),
                    )
                )
            else:
                if self.split_hash_pairs:
                    slots.append(("valu", (op1, tmp1, val_hash_addr, vec_const_map[val1])))
                    slots.append(("valu", (op3, tmp2, val_hash_addr, vec_const_map[val3])))
                else:
                    slots.append(
                        (
                            "valu",
                            [
                                (op1, tmp1, val_hash_addr, vec_const_map[val1]),
                                (op3, tmp2, val_hash_addr, vec_const_map[val3]),
                            ],
                        )
                    )
                slots.append(("valu", (op2, val_hash_addr, tmp1, tmp2)))
            slots.append(
                (
                    "debug",
                    (
                        "vcompare",
                        val_hash_addr,
                        [
                            (round, i_base + vi, "hash_stage", hi)
                            for vi in range(VLEN)
                        ],
                    ),
                )
            )

        return slots

    def build_kernel(
        self, forest_height: int, n_nodes: int, batch_size: int, rounds: int
    ):
        """
        Like reference_kernel2 but building actual instructions.
        Vectorized inner loop using SIMD VALU + gather via load_offset.
        """
        self._schedule_segments = []
        tmp1 = self.alloc_scratch("tmp1")
        tmp2 = self.alloc_scratch("tmp2")
        tmp3 = self.alloc_scratch("tmp3")

        # Scalar fallback for non-multiple batch sizes to avoid VLIW scheduling issues.
        if batch_size % VLEN != 0:
            init_vars = [
                ("n_nodes", 1),
                ("forest_values_p", 4),
                ("inp_indices_p", 5),
                ("inp_values_p", 6),
            ]
            for name, _ in init_vars:
                self.alloc_scratch(name, 1)
            for name, header_idx in init_vars:
                self.add("load", ("const", tmp1, header_idx))
                self.add("load", ("load", self.scratch[name], tmp1))

            zero_const = self.scratch_const(0)
            one_const = self.scratch_const(1)
            two_const = self.scratch_const(2)

            if self.emit_debug:
                self.instrs.append({"flow": [("pause",)]})
            self.add("debug", ("comment", "Starting loop"))

            tmp_idx = self.alloc_scratch("tmp_idx")
            tmp_val = self.alloc_scratch("tmp_val")
            tmp_node_val = self.alloc_scratch("tmp_node_val")
            tmp_addr = self.alloc_scratch("tmp_addr")

            for round in range(rounds):
                for i in range(batch_size):
                    i_const = self.scratch_const(i)
                    # idx = mem[inp_indices_p + i]
                    self.add("alu", ("+", tmp_addr, self.scratch["inp_indices_p"], i_const))
                    self.add("load", ("load", tmp_idx, tmp_addr))
                    self.add("debug", ("compare", tmp_idx, (round, i, "idx")))
                    # val = mem[inp_values_p + i]
                    self.add("alu", ("+", tmp_addr, self.scratch["inp_values_p"], i_const))
                    self.add("load", ("load", tmp_val, tmp_addr))
                    self.add("debug", ("compare", tmp_val, (round, i, "val")))
                    # node_val = mem[forest_values_p + idx]
                    self.add("alu", ("+", tmp_addr, self.scratch["forest_values_p"], tmp_idx))
                    self.add("load", ("load", tmp_node_val, tmp_addr))
                    self.add("debug", ("compare", tmp_node_val, (round, i, "node_val")))
                    # val = myhash(val ^ node_val)
                    self.add("alu", ("^", tmp_val, tmp_val, tmp_node_val))
                    for engine, slot in self.build_hash(tmp_val, tmp1, tmp2, round, i):
                        if isinstance(slot, list):
                            for subslot in slot:
                                self.add(engine, subslot)
                        else:
                            self.add(engine, slot)
                    self.add("debug", ("compare", tmp_val, (round, i, "hashed_val")))
                    # idx = 2*idx + (1 if val % 2 == 0 else 2)
                    self.add("alu", ("%", tmp1, tmp_val, two_const))
                    self.add("alu", ("==", tmp1, tmp1, zero_const))
                    self.add("flow", ("select", tmp3, tmp1, one_const, two_const))
                    self.add("alu", ("*", tmp_idx, tmp_idx, two_const))
                    self.add("alu", ("+", tmp_idx, tmp_idx, tmp3))
                    self.add("debug", ("compare", tmp_idx, (round, i, "next_idx")))
                    # idx = 0 if idx >= n_nodes else idx
                    self.add("alu", ("<", tmp1, tmp_idx, self.scratch["n_nodes"]))
                    self.add("flow", ("select", tmp_idx, tmp1, tmp_idx, zero_const))
                    self.add("debug", ("compare", tmp_idx, (round, i, "wrapped_idx")))
                    # mem[inp_indices_p + i] = idx
                    self.add("alu", ("+", tmp_addr, self.scratch["inp_indices_p"], i_const))
                    self.add("store", ("store", tmp_addr, tmp_idx))
                    # mem[inp_values_p + i] = val
                    self.add("alu", ("+", tmp_addr, self.scratch["inp_values_p"], i_const))
                    self.add("store", ("store", tmp_addr, tmp_val))

            if self.emit_debug:
                self.instrs.append({"flow": [("pause",)]})
            return

        header = []  # collect header ops for VLIW scheduling

        # Allocate a never-written scratch address for use as add_imm zero source.
        # Scratch is initialized to 0 by the Machine, so reading this always yields 0.
        # This lets us replace load.const with flow.add_imm in submission mode.
        zero_base = self.alloc_scratch("zero_base") if not self.emit_debug else None

        # Scratch space addresses (header indices are fixed in build_mem_image)
        # In non-debug submission mode, indices always start at zero and only
        # final values are validated, so we avoid idx memory traffic.
        use_idx_mem = self.emit_debug
        use_compact_depth_state = (
            not self.emit_debug and self.depth2_select_mode == "flow_vselect"
        )
        use_depth3_deterministic = (
            not self.emit_debug and self.depth3_deterministic and use_compact_depth_state
        )
        use_depth4_deterministic = (
            not self.emit_debug
            and self.depth4_mode == "deterministic16"
            and use_compact_depth_state
        )
        # Submission-only mode: keep path state beyond depth-2 to avoid
        # depth>=3 flow.vselect branch updates. Disabled when deterministic
        # depth branches are active because those branches expect full idx.
        use_compact_path_depth3plus = (
            use_compact_depth_state
            and not use_depth3_deterministic
            and not use_depth4_deterministic
        )
        # Wrap checks are only required in debug mode where idx traces are validated.
        need_wrap_checks = self.emit_debug
        init_vars = [
            ("forest_values_p", 4),
            ("inp_values_p", 6),
        ]
        if need_wrap_checks:
            init_vars.insert(0, ("n_nodes", 1))
        if use_idx_mem:
            init_vars.append(("inp_indices_p", 5))
        # Use separate tmp addresses for each header load to avoid WAW serialization
        header_tmp_addrs = []
        for name, _ in init_vars:
            self.alloc_scratch(name, 1)
            header_tmp_addrs.append(self.alloc_scratch())
        for idx_i, (name, header_idx) in enumerate(init_vars):
            htmp = header_tmp_addrs[idx_i]
            if zero_base is not None:
                header.append(("flow", ("add_imm", htmp, zero_base, header_idx)))
            else:
                header.append(("load", ("const", htmp, header_idx)))
            header.append(("load", ("load", self.scratch[name], htmp)))

        # scratch_const replacement for header: allocate and emit to header list
        def header_scratch_const(val, name=None):
            if val not in self.const_map:
                addr = self.alloc_scratch(name)
                self.const_map[val] = addr
                if zero_base is not None:
                    header.append(("flow", ("add_imm", addr, zero_base, val)))
                else:
                    header.append(("load", ("const", addr, val)))
            return self.const_map[val]

        zero_const = header_scratch_const(0)
        one_const = header_scratch_const(1)
        two_const = header_scratch_const(2)

        vec_const_map = {}

        def alloc_vec_const(val, name=None):
            if val in vec_const_map:
                return vec_const_map[val]
            addr = self.alloc_scratch(name, length=VLEN)
            scalar_addr = header_scratch_const(val)
            header.append(("valu", ("vbroadcast", addr, scalar_addr)))
            vec_const_map[val] = addr
            return addr

        vec_one = alloc_vec_const(1, "vec_one")
        vec_two = alloc_vec_const(2, "vec_two")
        if use_compact_depth_state:
            vec_three = None
            vec_seven = alloc_vec_const(7, "vec_seven")
            vec_fifteen = alloc_vec_const(15, "vec_fifteen") if use_depth4_deterministic else None
        else:
            vec_three = alloc_vec_const(3, "vec_three")
            vec_seven = None
            vec_fifteen = None

        for hi, (op1, val1, op2, op3, val3) in enumerate(HASH_STAGES):
            alloc_vec_const(val1, f"hash_c1_{hi}")
            alloc_vec_const(val3, f"hash_c3_{hi}")
            if op2 == "+" and op3 == "<<":
                if op1 == "+":
                    # Algebraic simplification: a*(1<<shift) + (a + const)
                    # = a*((1<<shift)+1) + const → single multiply_add
                    combined_mul = (1 << val3) + 1
                    alloc_vec_const(combined_mul, f"hash_combined_mul_{hi}")
                else:
                    mul_val = 1 << val3
                    if mul_val not in vec_const_map:
                        mul_const = self.alloc_scratch(f"hash_mul_{hi}", VLEN)
                        header.append(
                            ("valu",
                            ("<<", mul_const, vec_one, vec_const_map[val3]))
                        )
                        vec_const_map[mul_val] = mul_const

        if need_wrap_checks:
            vec_n_nodes = self.alloc_scratch("vec_n_nodes", VLEN)
            header.append(("valu", ("vbroadcast", vec_n_nodes, self.scratch["n_nodes"])))
        else:
            vec_n_nodes = None
        vec_forest_base = self.alloc_scratch("vec_forest_base", VLEN)
        header.append(("valu", ("vbroadcast", vec_forest_base, self.scratch["forest_values_p"])))
        vec_forest_depth_bases = {}
        if use_compact_path_depth3plus:
            required_depths = sorted(
                {
                    round_i % (forest_height + 1)
                    for round_i in range(rounds)
                    if (round_i % (forest_height + 1)) >= 3
                }
            )
            if required_depths:
                # Recurrence for absolute depth bases:
                # base_{d+1} = 2*base_d + (1 - forest_base), with base_3=forest_base+7.
                # This avoids extra flow.add_imm + vbroadcast pairs per depth.
                vec_forest_base_adj = self.alloc_scratch("vec_forest_base_adj", VLEN)
                header.append(("valu", ("-", vec_forest_base_adj, vec_one, vec_forest_base)))
                max_depth = required_depths[-1]
                base_vec = self.alloc_scratch("vec_forest_base_d3", VLEN)
                header.append(("valu", ("+", base_vec, vec_forest_base, vec_seven)))
                if 3 in required_depths:
                    vec_forest_depth_bases[3] = base_vec
                for depth in range(4, max_depth + 1):
                    next_base_vec = self.alloc_scratch(f"vec_forest_base_d{depth}", VLEN)
                    header.append(
                        (
                            "valu",
                            (
                                "multiply_add",
                                next_base_vec,
                                base_vec,
                                vec_two,
                                vec_forest_base_adj,
                            ),
                        )
                    )
                    base_vec = next_base_vec
                    if depth in required_depths:
                        vec_forest_depth_bases[depth] = base_vec
        node0 = self.alloc_scratch("node0")
        node1 = self.alloc_scratch("node1")
        node2 = self.alloc_scratch("node2")
        node3 = self.alloc_scratch("node3")
        node4 = self.alloc_scratch("node4")
        node5 = self.alloc_scratch("node5")
        node6 = self.alloc_scratch("node6")
        node_addrs = [self.alloc_scratch() for _ in range(6)]
        header.append(("load", ("load", node0, self.scratch["forest_values_p"])))
        header.append(("alu", ("+", node_addrs[0], self.scratch["forest_values_p"], one_const)))
        header.append(("alu", ("+", node_addrs[1], self.scratch["forest_values_p"], two_const)))
        header.append(("alu", ("+", node_addrs[2], self.scratch["forest_values_p"], header_scratch_const(3))))
        header.append(("alu", ("+", node_addrs[3], self.scratch["forest_values_p"], header_scratch_const(4))))
        header.append(("alu", ("+", node_addrs[4], self.scratch["forest_values_p"], header_scratch_const(5))))
        header.append(("alu", ("+", node_addrs[5], self.scratch["forest_values_p"], header_scratch_const(6))))
        header.append(("load", ("load", node1, node_addrs[0])))
        header.append(("load", ("load", node2, node_addrs[1])))
        header.append(("load", ("load", node3, node_addrs[2])))
        header.append(("load", ("load", node4, node_addrs[3])))
        header.append(("load", ("load", node5, node_addrs[4])))
        header.append(("load", ("load", node6, node_addrs[5])))
        vec_node0 = self.alloc_scratch("vec_node0", VLEN)
        vec_node1 = self.alloc_scratch("vec_node1", VLEN)
        vec_node2 = self.alloc_scratch("vec_node2", VLEN)
        vec_node3 = self.alloc_scratch("vec_node3", VLEN)
        vec_node4 = self.alloc_scratch("vec_node4", VLEN)
        vec_node5 = self.alloc_scratch("vec_node5", VLEN)
        vec_node6 = self.alloc_scratch("vec_node6", VLEN)
        header.append(("valu", ("vbroadcast", vec_node0, node0)))
        header.append(("valu", ("vbroadcast", vec_node1, node1)))
        header.append(("valu", ("vbroadcast", vec_node2, node2)))
        header.append(("valu", ("vbroadcast", vec_node3, node3)))
        header.append(("valu", ("vbroadcast", vec_node4, node4)))
        header.append(("valu", ("vbroadcast", vec_node5, node5)))
        header.append(("valu", ("vbroadcast", vec_node6, node6)))
        if use_depth3_deterministic:
            vec_depth3_nodes = []
            node_addr = self.alloc_scratch()
            header.append(
                ("alu", ("+", node_addr, self.scratch["forest_values_p"], header_scratch_const(7)))
            )
            for off in range(7, 15):
                node = self.alloc_scratch(f"node{off}")
                header.append(("load", ("load", node, node_addr)))
                vec_node = self.alloc_scratch(f"vec_node{off}", VLEN)
                header.append(("valu", ("vbroadcast", vec_node, node)))
                vec_depth3_nodes.append(vec_node)
                if off < 14:
                    header.append(("alu", ("+", node_addr, node_addr, one_const)))
        else:
            vec_depth3_nodes = []
        if use_depth4_deterministic:
            vec_depth4_nodes = []
            node_addr4 = self.alloc_scratch()
            for off in range(15, 31):
                node = self.alloc_scratch(f"node{off}")
                header.append(
                    ("alu", ("+", node_addr4, self.scratch["forest_values_p"], header_scratch_const(off)))
                )
                header.append(("load", ("load", node, node_addr4)))
                vec_node = self.alloc_scratch(f"vec_node{off}", VLEN)
                header.append(("valu", ("vbroadcast", vec_node, node)))
                vec_depth4_nodes.append(vec_node)
        else:
            vec_depth4_nodes = []
        if use_compact_depth_state:
            vec_node21_diff = None
            vec_node43_diff = None
            vec_node65_diff = None
            vec_node1_minus_diff = None
        else:
            vec_node21_diff = self.alloc_scratch("vec_node_diff_2_1", VLEN)
            vec_node43_diff = self.alloc_scratch("vec_node_diff_4_3", VLEN)
            vec_node65_diff = self.alloc_scratch("vec_node_diff_6_5", VLEN)
            vec_node1_minus_diff = self.alloc_scratch("vec_node_1_minus_diff_21", VLEN)
            header.append(("valu", ("-", vec_node21_diff, vec_node2, vec_node1)))
            header.append(("valu", ("-", vec_node43_diff, vec_node4, vec_node3)))
            header.append(("valu", ("-", vec_node65_diff, vec_node6, vec_node5)))
            header.append(("valu", ("-", vec_node1_minus_diff, vec_node1, vec_node21_diff)))
        idx_arr = self.alloc_scratch("idx_arr", batch_size)
        val_arr = self.alloc_scratch("val_arr", batch_size)

        # Pause instructions are matched up with yield statements in the reference
        # kernel to let you debug at intermediate steps. The testing harness in this
        # file requires these match up to the reference kernel's yields, but the
        # submission harness ignores them.
        header_phase_tag = "header" if self.trace_phase_tags else None
        body_phase_tag = "kernel_body" if self.trace_phase_tags else None
        if self.emit_debug:
            # Schedule header separately before the pause barrier
            # Header outputs are consumed in a later segment, so skip local DCE here.
            header_instrs = self.build(
                header, vliw=True, phase_tag=header_phase_tag, optimize=False
            )
            self.instrs.extend(header_instrs)
            self.instrs.append({"flow": [("pause",)]})
        # Any debug engine instruction is ignored by the submission simulator
        self.add("debug", ("comment", "Starting loop"))

        # In non-debug mode, merge header into body for joint VLIW scheduling
        body = list(header) if not self.emit_debug else []

        # Scalar scratch registers (tail handling)
        tmp_node_val = self.alloc_scratch("tmp_node_val")
        tmp_addr = self.alloc_scratch("tmp_addr")
        tmp_addr_b = self.alloc_scratch("tmp_addr_b")

        interleave_groups = self.interleave_groups
        interleave_groups_early = self.interleave_groups_early
        if use_depth4_deterministic and self.depth4_adaptive_interleave:
            min_groups = 8
            while (
                self.scratch_ptr + 24 * max(interleave_groups, interleave_groups_early)
                > SCRATCH_SIZE
            ):
                if (
                    interleave_groups_early >= interleave_groups
                    and interleave_groups_early > min_groups
                ):
                    interleave_groups_early -= 1
                elif interleave_groups > min_groups:
                    interleave_groups -= 1
                else:
                    break
        group_regs = []
        max_groups = max(interleave_groups, interleave_groups_early)
        for g in range(max_groups):
            group_regs.append(
                {
                    "vec_node_val": self.alloc_scratch(f"vec_node_val_g{g}", VLEN),
                    "vec_addr": self.alloc_scratch(f"vec_addr_g{g}", VLEN),
                    "vec_val_save": self.alloc_scratch(f"vec_val_save_g{g}", VLEN),
                }
            )
        vec_depth3_select_tmp = (
            self.alloc_scratch("vec_depth3_select_tmp", VLEN)
            if use_depth3_deterministic
            else None
        )

        vec_count = (batch_size // VLEN) * VLEN
        def emit_vector_group_ops(round, i, regs, depth):
            keys = [(round, i + vi, "idx") for vi in range(VLEN)]

            vec_idx = idx_arr + i
            vec_val = val_arr + i
            vec_node_val = regs["vec_node_val"]
            vec_addr = regs["vec_addr"]
            vec_val_save = regs["vec_val_save"]
            vec_tmp1 = vec_addr
            vec_tmp2 = vec_node_val

            body.append(("debug", ("vcompare", vec_idx, keys)))
            body.append(
                (
                    "debug",
                    (
                        "vcompare",
                        vec_val,
                        [(round, i + vi, "val") for vi in range(VLEN)],
                    ),
                )
            )
            if depth == 0:
                body.append(
                    (
                        "debug",
                        (
                            "vcompare",
                            vec_node0,
                            [(round, i + vi, "node_val") for vi in range(VLEN)],
                        ),
                    )
                )
                # val = myhash(val ^ node_val)
                body.append(("valu", ("^", vec_val, vec_val, vec_node0)))
                body.extend(
                    self.build_hash_vec(
                        vec_val, vec_tmp1, vec_tmp2, round, i, vec_const_map
                    )
                )
            elif depth == 1:
                if use_compact_depth_state:
                    # Depth-0 writes b0=(val&1) into vec_idx; select node1/node2 directly.
                    body.append(
                        ("flow", ("vselect", vec_node_val, vec_idx, vec_node2, vec_node1))
                    )
                else:
                    # node_val = node1 + (idx - 1) * (node2 - node1)
                    body.append(
                        ("valu", ("multiply_add", vec_node_val, vec_idx, vec_node21_diff, vec_node1_minus_diff))
                    )
                body.append(
                    (
                        "debug",
                        (
                            "vcompare",
                            vec_node_val,
                            [(round, i + vi, "node_val") for vi in range(VLEN)],
                        ),
                    )
                )
                # val = myhash(val ^ node_val)
                body.append(("valu", ("^", vec_val, vec_val, vec_node_val)))
                body.extend(
                    self.build_hash_vec(
                        vec_val, vec_tmp1, vec_tmp2, round, i, vec_const_map
                    )
                )
            elif depth == 2:
                if use_compact_depth_state:
                    # vec_idx carries path = 2*b0 + b1 in [0, 3].
                    body.append(("valu", ("&", vec_addr, vec_idx, vec_one)))  # low bit (b1)
                    body.append(("valu", ("&", vec_node_val, vec_idx, vec_two)))  # high bit (2*b0)
                    body.append(("flow", ("vselect", vec_val_save, vec_addr, vec_node4, vec_node3)))
                    body.append(("flow", ("vselect", vec_addr, vec_addr, vec_node6, vec_node5)))
                    body.append(("flow", ("vselect", vec_node_val, vec_node_val, vec_addr, vec_val_save)))
                else:
                    # node_val from nodes 3..6 using idx in [3, 6]
                    body.append(("valu", ("-", vec_addr, vec_idx, vec_three)))  # path
                    body.append(("valu", ("&", vec_val_save, vec_addr, vec_one)))  # b0
                    body.append(("valu", (">>", vec_node_val, vec_addr, vec_one)))  # b1
                    body.append(
                        ("valu", ("multiply_add", vec_addr, vec_val_save, vec_node43_diff, vec_node3))
                    )  # v01
                    body.append(
                        ("valu", ("multiply_add", vec_val_save, vec_val_save, vec_node65_diff, vec_node5))
                    )  # v23
                    if self.depth2_select_mode == "flow_vselect":
                        body.append(
                            ("flow", ("vselect", vec_node_val, vec_node_val, vec_val_save, vec_addr))
                        )  # node_val
                    elif self.depth2_select_mode == "alu_blend":
                        # node_val = v23 + (1 - b1) * (v01 - v23), where b1 is 0 or 1.
                        body.append(("valu", ("-", vec_addr, vec_addr, vec_val_save)))  # v01-v23
                        body.append(("valu", ("-", vec_node_val, vec_one, vec_node_val)))  # 1-b1
                        body.append(
                            (
                                "valu",
                                ("multiply_add", vec_node_val, vec_node_val, vec_addr, vec_val_save),
                            )
                        )
                    else:
                        raise ValueError(f"Unknown depth2_select_mode={self.depth2_select_mode}")
                body.append(
                    (
                        "debug",
                        (
                            "vcompare",
                            vec_node_val,
                            [(round, i + vi, "node_val") for vi in range(VLEN)],
                        ),
                    )
                )
                # val = myhash(val ^ node_val)
                body.append(("valu", ("^", vec_val, vec_val, vec_node_val)))
                body.extend(
                    self.build_hash_vec(
                        vec_val, vec_tmp1, vec_tmp2, round, i, vec_const_map
                    )
                )
            elif depth == 3 and use_depth3_deterministic:
                # Depth-3 idx is deterministic in [7, 14]. Use a balanced
                # select tree over nodes 7..14 driven by path bits.
                # path = idx - 7 in [0, 7]
                body.append(("valu", ("-", vec_val_save, vec_idx, vec_seven)))
                # b1 = path & 2 (used for both subtree merges)
                body.append(("valu", ("&", vec_node_val, vec_val_save, vec_two)))
                # Lower subtree over nodes 7..10 using b0
                body.append(("valu", ("&", vec_addr, vec_val_save, vec_one)))
                body.append(
                    ("flow", ("vselect", vec_idx, vec_addr, vec_depth3_nodes[1], vec_depth3_nodes[0]))
                )
                body.append(
                    ("flow", ("vselect", vec_addr, vec_addr, vec_depth3_nodes[3], vec_depth3_nodes[2]))
                )
                body.append(("flow", ("vselect", vec_idx, vec_node_val, vec_addr, vec_idx)))
                # Upper subtree over nodes 11..14 using b0 and then b1
                body.append(("valu", ("&", vec_addr, vec_val_save, vec_one)))
                body.append(
                    (
                        "flow",
                        (
                            "vselect",
                            vec_depth3_select_tmp,
                            vec_addr,
                            vec_depth3_nodes[7],
                            vec_depth3_nodes[6],
                        ),
                    )
                )
                body.append(
                    ("flow", ("vselect", vec_addr, vec_addr, vec_depth3_nodes[5], vec_depth3_nodes[4]))
                )
                body.append(
                    (
                        "flow",
                        (
                            "vselect",
                            vec_addr,
                            vec_node_val,
                            vec_depth3_select_tmp,
                            vec_addr,
                        ),
                    )
                )
                # b2 = path >> 2 in {0,1}; choose upper vs lower subtree.
                body.append(("valu", (">>", vec_node_val, vec_val_save, vec_two)))
                body.append(("flow", ("vselect", vec_node_val, vec_node_val, vec_addr, vec_idx)))
                body.append(
                    (
                        "debug",
                        (
                            "vcompare",
                            vec_node_val,
                            [(round, i + vi, "node_val") for vi in range(VLEN)],
                        ),
                    )
                )
                # val = myhash(val ^ node_val)
                body.append(("valu", ("^", vec_val, vec_val, vec_node_val)))
                body.extend(
                    self.build_hash_vec(
                        vec_val, vec_tmp1, vec_tmp2, round, i, vec_const_map
                    )
                )
                # Restore full idx from saved path for shared idx update logic.
                body.append(("valu", ("+", vec_idx, vec_val_save, vec_seven)))
            elif depth == 4 and use_depth4_deterministic:
                # Depth-4 idx is deterministic in [15, 30]. Use a submission-only
                # deterministic select over preloaded node vectors.
                body.append(("valu", ("-", vec_addr, vec_idx, vec_fifteen)))
                body.append(
                    ("flow", ("vselect", vec_val_save, vec_one, vec_addr, vec_addr))
                )
                body.append(
                    ("flow", ("vselect", vec_node_val, vec_one, vec_depth4_nodes[0], vec_depth4_nodes[0]))
                )
                body.append(
                    ("flow", ("vselect", vec_idx, vec_one, vec_one, vec_one))
                )
                for path_i, vec_node in enumerate(vec_depth4_nodes[1:]):
                    body.append(
                        ("valu", ("==", vec_addr, vec_val_save, vec_idx))
                    )
                    body.append(
                        ("flow", ("vselect", vec_node_val, vec_addr, vec_node, vec_node_val))
                    )
                    if path_i < len(vec_depth4_nodes[1:]) - 1:
                        body.append(("valu", ("+", vec_idx, vec_idx, vec_one)))
                body.append(
                    (
                        "debug",
                        (
                            "vcompare",
                            vec_node_val,
                            [(round, i + vi, "node_val") for vi in range(VLEN)],
                        ),
                    )
                )
                body.append(("valu", ("^", vec_val, vec_val, vec_node_val)))
                body.extend(
                    self.build_hash_vec(
                        vec_val, vec_tmp1, vec_tmp2, round, i, vec_const_map
                    )
                )
                body.append(("valu", ("+", vec_idx, vec_val_save, vec_fifteen)))
            else:
                # node_val = mem[forest_values_p + idx] (gather)
                if use_compact_path_depth3plus and depth >= 3:
                    body.append(
                        (
                            "alu",
                            [
                                (
                                    "+",
                                    vec_addr + vi,
                                    vec_forest_depth_bases[depth] + vi,
                                    vec_idx + vi,
                                )
                                for vi in range(VLEN)
                            ],
                        )
                    )
                else:
                    body.append(
                        (
                            "alu",
                            [
                                ("+", vec_addr + vi, vec_forest_base + vi, vec_idx + vi)
                                for vi in range(VLEN)
                            ],
                        )
                    )
                for offset in range(VLEN):
                    body.append(("load", ("load_offset", vec_node_val, vec_addr, offset)))
                body.append(
                    (
                        "debug",
                        (
                            "vcompare",
                            vec_node_val,
                            [(round, i + vi, "node_val") for vi in range(VLEN)],
                        ),
                    )
                )
                # val = myhash(val ^ node_val)
                body.append(("valu", ("^", vec_val, vec_val, vec_node_val)))
                body.extend(
                    self.build_hash_vec(
                        vec_val, vec_tmp1, vec_tmp2, round, i, vec_const_map
                    )
                )
            body.append(
                (
                    "debug",
                    (
                        "vcompare",
                        vec_val,
                        [(round, i + vi, "hashed_val") for vi in range(VLEN)],
                    ),
                )
            )
            # idx update:
            # - depth 0: idx is always 0 before update, so we can write branch directly
            # - depth == forest_height: next round (depth 0) overwrites idx, so we can skip the update
            # If this is the final round in non-debug submission mode, idx
            # updates are unnecessary because only values are validated.
            if not self.emit_debug and (depth == forest_height or round == rounds - 1):
                return

            if use_compact_depth_state and depth == 0:
                # Carry b0 in vec_idx for depth-1.
                body.append(("valu", ("&", vec_idx, vec_val, vec_one)))
                return

            if use_compact_depth_state and depth == 1:
                # Transition to path state: vec_idx = 2*b0 + b1.
                body.append(("valu", ("&", vec_addr, vec_val, vec_one)))
                body.append(("valu", ("multiply_add", vec_idx, vec_idx, vec_two, vec_addr)))
                return

            if use_compact_depth_state and depth == 2:
                if use_compact_path_depth3plus:
                    # Keep path state for depth-3+: path = 2*path + b2.
                    body.append(("valu", ("&", vec_addr, vec_val, vec_one)))
                    body.append(("valu", ("multiply_add", vec_idx, vec_idx, vec_two, vec_addr)))
                else:
                    # Materialize idx for depth-3: idx = 7 + 2*path + b2.
                    body.append(("valu", ("&", vec_addr, vec_val, vec_one)))
                    body.append(("valu", ("multiply_add", vec_idx, vec_idx, vec_two, vec_seven)))
                    body.append(("valu", ("+", vec_idx, vec_idx, vec_addr)))
                return

            if use_compact_path_depth3plus and depth >= 3:
                # For depth>=3, vec_idx carries path and updates as path'=2*path+b.
                body.append(("valu", ("&", vec_tmp1, vec_val, vec_one)))
                body.append(
                    ("valu", ("multiply_add", vec_idx, vec_idx, vec_two, vec_tmp1))
                )
                return

            # idx = 2*idx + (1 if val % 2 == 0 else 2)
            # => branch = (val & 1) + 1
            if depth == 0:
                body.append(("valu", ("&", vec_tmp1, vec_val, vec_one)))
                if self.idx_branch_mode == "flow_vselect":
                    body.append(("flow", ("vselect", vec_idx, vec_tmp1, vec_two, vec_one)))
                elif self.idx_branch_mode == "alu_branch":
                    body.append(("valu", ("+", vec_idx, vec_tmp1, vec_one)))
                else:
                    raise ValueError(f"Unknown idx_branch_mode={self.idx_branch_mode}")
            else:
                body.append(("valu", ("&", vec_tmp1, vec_val, vec_one)))
                if self.idx_branch_mode == "flow_vselect":
                    body.append(("flow", ("vselect", vec_tmp2, vec_tmp1, vec_two, vec_one)))
                elif self.idx_branch_mode == "alu_branch":
                    body.append(("valu", ("+", vec_tmp2, vec_tmp1, vec_one)))
                else:
                    raise ValueError(f"Unknown idx_branch_mode={self.idx_branch_mode}")
                body.append(
                    ("valu", ("multiply_add", vec_idx, vec_idx, vec_two, vec_tmp2))
                )
            body.append(
                (
                    "debug",
                    (
                        "vcompare",
                        vec_idx,
                        [(round, i + vi, "next_idx") for vi in range(VLEN)],
                    ),
                )
            )
            # idx = 0 if idx >= n_nodes else idx (only needed at wrap depth)
            if need_wrap_checks and depth == forest_height:
                body.append(("valu", ("<", vec_tmp1, vec_idx, vec_n_nodes)))
                body.append(("valu", ("*", vec_idx, vec_idx, vec_tmp1)))
            body.append(
                (
                    "debug",
                    (
                        "vcompare",
                        vec_idx,
                        [(round, i + vi, "wrapped_idx") for vi in range(VLEN)],
                    ),
                )
            )

        # Pre-allocate offset constant scratch addresses and emit loads into body
        # so VLIW scheduler can pair them (instead of single-load cycles via self.add)
        # In submission mode (zero_base is not None), we use flow.add_imm instead,
        # which eliminates both the scratch allocation and the load ops.
        offset_addrs = {}
        if zero_base is None:
            all_offsets = set()
            for base in range(0, vec_count, VLEN):
                all_offsets.add(base)
            for i in range(vec_count, batch_size):
                all_offsets.add(i)
            for off in sorted(all_offsets):
                if off not in self.const_map:
                    addr = self.alloc_scratch()
                    self.const_map[off] = addr
                offset_addrs[off] = self.const_map[off]
                body.append(("load", ("const", offset_addrs[off], off)))

        use_fast_value_vector_ptrs = (
            self.fast_value_vector_ptrs
            and zero_base is not None
            and not use_idx_mem
            and vec_count > 0
        )
        vec_stride_const = None
        val_load_ptr = None
        if use_fast_value_vector_ptrs:
            if VLEN not in self.const_map:
                addr = self.alloc_scratch()
                self.const_map[VLEN] = addr
                body.append(("flow", ("add_imm", addr, zero_base, VLEN)))
            vec_stride_const = self.const_map[VLEN]
            val_load_ptr = self.alloc_scratch("val_load_ptr")
            body.append(("alu", ("+", val_load_ptr, self.scratch["inp_values_p"], zero_const)))
            for base in range(0, vec_count, VLEN):
                body.append(("load", ("vload", val_arr + base, val_load_ptr)))
                if base + VLEN < vec_count:
                    body.append(("alu", ("+", val_load_ptr, val_load_ptr, vec_stride_const)))
        else:
            for base in range(0, vec_count, VLEN):
                if use_idx_mem:
                    if zero_base is not None:
                        body.append(("flow", ("add_imm", tmp_addr, self.scratch["inp_indices_p"], base)))
                    else:
                        body.append(
                            ("alu", ("+", tmp_addr, self.scratch["inp_indices_p"], offset_addrs[base]))
                        )
                    body.append(("load", ("vload", idx_arr + base, tmp_addr)))
                if zero_base is not None:
                    body.append(("flow", ("add_imm", tmp_addr_b, self.scratch["inp_values_p"], base)))
                else:
                    body.append(
                        ("alu", ("+", tmp_addr_b, self.scratch["inp_values_p"], offset_addrs[base]))
                    )
                body.append(("load", ("vload", val_arr + base, tmp_addr_b)))

        for i in range(vec_count, batch_size):
            if use_idx_mem:
                if zero_base is not None:
                    body.append(("flow", ("add_imm", tmp_addr, self.scratch["inp_indices_p"], i)))
                else:
                    body.append(
                        ("alu", ("+", tmp_addr, self.scratch["inp_indices_p"], offset_addrs[i]))
                    )
                body.append(("load", ("load", idx_arr + i, tmp_addr)))
            if zero_base is not None:
                body.append(("flow", ("add_imm", tmp_addr_b, self.scratch["inp_values_p"], i)))
            else:
                body.append(
                    ("alu", ("+", tmp_addr_b, self.scratch["inp_values_p"], offset_addrs[i]))
                )
            body.append(("load", ("load", val_arr + i, tmp_addr_b)))

        for round in range(rounds):
            depth = round % (forest_height + 1)
            regs_list = (
                group_regs[:interleave_groups_early]
                if depth <= 2
                else group_regs[:interleave_groups]
            )
            for base in range(0, vec_count, VLEN * len(regs_list)):
                for g, regs in enumerate(regs_list):
                    i = base + g * VLEN
                    if i >= vec_count:
                        continue
                    emit_vector_group_ops(round, i, regs, depth)

            for i in range(vec_count, batch_size):
                idx_addr = idx_arr + i
                val_addr = val_arr + i
                body.append(("debug", ("compare", idx_addr, (round, i, "idx"))))
                body.append(("debug", ("compare", val_addr, (round, i, "val"))))
                # node_val = mem[forest_values_p + idx]
                body.append(("alu", ("+", tmp_addr, self.scratch["forest_values_p"], idx_addr)))
                body.append(("load", ("load", tmp_node_val, tmp_addr)))
                body.append(("debug", ("compare", tmp_node_val, (round, i, "node_val"))))
                # val = myhash(val ^ node_val)
                body.append(("alu", ("^", val_addr, val_addr, tmp_node_val)))
                body.extend(self.build_hash(val_addr, tmp1, tmp2, round, i))
                body.append(("debug", ("compare", val_addr, (round, i, "hashed_val"))))
                # idx update
                if not self.emit_debug and (depth == forest_height or round == rounds - 1):
                    continue

                # idx = 2*idx + (1 if val % 2 == 0 else 2)
                # => branch = (val & 1) + 1
                if depth == 0:
                    body.append(("alu", ("&", idx_addr, val_addr, one_const)))
                    body.append(("alu", ("+", idx_addr, idx_addr, one_const)))
                else:
                    body.append(("alu", ("&", tmp1, val_addr, one_const)))
                    body.append(("alu", ("+", tmp3, tmp1, one_const)))
                    body.append(("alu", ("<<", idx_addr, idx_addr, one_const)))
                    body.append(("alu", ("+", idx_addr, idx_addr, tmp3)))
                body.append(("debug", ("compare", idx_addr, (round, i, "next_idx"))))
                # idx wrap is only required in debug mode where idx traces are checked.
                if need_wrap_checks and depth == forest_height:
                    body.append(("alu", ("<", tmp1, idx_addr, self.scratch["n_nodes"])))
                    body.append(("alu", ("*", idx_addr, idx_addr, tmp1)))
                body.append(("debug", ("compare", idx_addr, (round, i, "wrapped_idx"))))

        for base in range(0, vec_count, VLEN):
            if use_idx_mem:
                if zero_base is not None:
                    body.append(("flow", ("add_imm", tmp_addr, self.scratch["inp_indices_p"], base)))
                else:
                    body.append(
                        ("alu", ("+", tmp_addr, self.scratch["inp_indices_p"], offset_addrs[base]))
                    )
                body.append(("store", ("vstore", tmp_addr, idx_arr + base)))
            if zero_base is not None:
                body.append(("flow", ("add_imm", tmp_addr_b, self.scratch["inp_values_p"], base)))
                body.append(("store", ("vstore", tmp_addr_b, val_arr + base)))
            else:
                body.append(
                    ("alu", ("+", tmp_addr_b, self.scratch["inp_values_p"], offset_addrs[base]))
                )
                body.append(("store", ("vstore", tmp_addr_b, val_arr + base)))

        for i in range(vec_count, batch_size):
            if use_idx_mem:
                if zero_base is not None:
                    body.append(("flow", ("add_imm", tmp_addr, self.scratch["inp_indices_p"], i)))
                else:
                    body.append(
                        ("alu", ("+", tmp_addr, self.scratch["inp_indices_p"], offset_addrs[i]))
                    )
                body.append(("store", ("store", tmp_addr, idx_arr + i)))
            if zero_base is not None:
                body.append(("flow", ("add_imm", tmp_addr_b, self.scratch["inp_values_p"], i)))
            else:
                body.append(
                    ("alu", ("+", tmp_addr_b, self.scratch["inp_values_p"], offset_addrs[i]))
                )
            body.append(("store", ("store", tmp_addr_b, val_arr + i)))

        body_instrs = self.build(body, vliw=True, phase_tag=body_phase_tag)
        self.instrs.extend(body_instrs)
        # Required to match with the yield in reference_kernel2
        if self.emit_debug:
            self.instrs.append({"flow": [("pause",)]})

BASELINE = 147734

def do_kernel_test(
    forest_height: int,
    rounds: int,
    batch_size: int,
    seed: int = 123,
    trace: bool = False,
    prints: bool = False,
    utilization: bool = False,
    debug_mode: bool = False,
    diagnostics_out: str | None = None,
    kernel_kwargs: dict | None = None,
):
    print(f"{forest_height=}, {rounds=}, {batch_size=}")
    random.seed(seed)
    forest = Tree.generate(forest_height)
    inp = Input.generate(forest, batch_size, rounds)
    mem = build_mem_image(forest, inp)

    builder_kwargs = dict(kernel_kwargs or {})
    if (trace or debug_mode) and "emit_debug" not in builder_kwargs:
        builder_kwargs["emit_debug"] = True
    if diagnostics_out is not None and "scheduler_profile" not in builder_kwargs:
        builder_kwargs["scheduler_profile"] = True
    if diagnostics_out is not None and "trace_phase_tags" not in builder_kwargs:
        builder_kwargs["trace_phase_tags"] = True
    kb = KernelBuilder(**builder_kwargs)
    kb.build_kernel(forest.height, len(forest.values), len(inp.indices), rounds)
    # print(kb.instrs)
    if utilization:
        print_utilization(kb.instrs)

    value_trace = {}
    machine = Machine(
        mem,
        kb.instrs,
        kb.debug_info(),
        n_cores=N_CORES,
        value_trace=value_trace,
        trace=trace,
    )
    machine.prints = prints
    round_aligned_mode = trace or debug_mode or kb.emit_debug
    if round_aligned_mode:
        for i, ref_mem in enumerate(reference_kernel2(mem, value_trace)):
            machine.run()
            inp_values_p = ref_mem[6]
            if prints:
                print(machine.mem[inp_values_p : inp_values_p + len(inp.values)])
                print(ref_mem[inp_values_p : inp_values_p + len(inp.values)])
            assert (
                machine.mem[inp_values_p : inp_values_p + len(inp.values)]
                == ref_mem[inp_values_p : inp_values_p + len(inp.values)]
            ), f"Incorrect result on round {i}"
            inp_indices_p = ref_mem[5]
            if prints:
                print(machine.mem[inp_indices_p : inp_indices_p + len(inp.indices)])
                print(ref_mem[inp_indices_p : inp_indices_p + len(inp.indices)])
            # Updating these in memory isn't required, but you can enable this check for debugging
            # assert machine.mem[inp_indices_p:inp_indices_p+len(inp.indices)] == ref_mem[inp_indices_p:inp_indices_p+len(inp.indices)]
    else:
        machine.run()
        for ref_mem in reference_kernel2(mem, value_trace):
            pass
        inp_values_p = ref_mem[6]
        if prints:
            print(machine.mem[inp_values_p : inp_values_p + len(inp.values)])
            print(ref_mem[inp_values_p : inp_values_p + len(inp.values)])
        assert (
            machine.mem[inp_values_p : inp_values_p + len(inp.values)]
            == ref_mem[inp_values_p : inp_values_p + len(inp.values)]
        ), "Incorrect output values"

    print("CYCLES: ", machine.cycle)
    print("Speedup over baseline: ", BASELINE / machine.cycle)

    if diagnostics_out is not None:
        metadata = {
            "cycles": machine.cycle,
            "seed": seed,
            "forest_height": forest_height,
            "rounds": rounds,
            "batch_size": batch_size,
            "kernel_config": builder_kwargs,
            "schedule_profile": kb.schedule_profile(),
        }
        try:
            from tools.opt_debug.analyze_schedule import (
                analyze_schedule_artifacts,
                render_run_markdown,
            )

            run_diag = analyze_schedule_artifacts(
                kb.instrs,
                schedule_profile=kb.schedule_profile(),
                metadata=metadata,
                include_candidates=True,
            )
            out_dir = Path(diagnostics_out)
            out_dir.mkdir(parents=True, exist_ok=True)
            json_path = out_dir / "latest_run.json"
            md_path = out_dir / "latest_run.md"
            json_path.write_text(
                json.dumps(run_diag.to_dict(), indent=2, sort_keys=True),
                encoding="utf-8",
            )
            md_path.write_text(render_run_markdown(run_diag), encoding="utf-8")
            json_path, md_path = str(json_path), str(md_path)
        except Exception:
            diagnostics = analyze_schedule(kb.instrs, metadata=metadata)
            json_path, md_path = write_diagnostics_artifacts(diagnostics, diagnostics_out)
        print(f"Diagnostics written to {json_path} and {md_path}")

    return machine.cycle


class Tests(unittest.TestCase):
    def test_ref_kernels(self):
        """
        Test the reference kernels against each other
        """
        random.seed(123)
        for i in range(10):
            f = Tree.generate(4)
            inp = Input.generate(f, 10, 6)
            mem = build_mem_image(f, inp)
            reference_kernel(f, inp)
            for _ in reference_kernel2(mem, {}):
                pass
            assert inp.indices == mem[mem[5] : mem[5] + len(inp.indices)]
            assert inp.values == mem[mem[6] : mem[6] + len(inp.values)]

    def test_kernel_trace(self):
        # Full-scale example for performance testing
        do_kernel_test(10, 16, 256, trace=True, prints=False)

    # Passing this test is not required for submission, see submission_tests.py for the actual correctness test
    # You can uncomment this if you think it might help you debug
    # def test_kernel_correctness(self):
    #     for batch in range(1, 3):
    #         for forest_height in range(3):
    #             do_kernel_test(
    #                 forest_height + 2, forest_height + 4, batch * 16 * VLEN * N_CORES
    #             )

    def test_kernel_cycles(self):
        do_kernel_test(10, 16, 256)


# To run all the tests:
#    python perf_takehome.py
# To run a specific test:
#    python perf_takehome.py Tests.test_kernel_cycles
# To view a hot-reloading trace of all the instructions:  **Recommended debug loop**
# NOTE: The trace hot-reloading only works in Chrome. In the worst case if things aren't working, drag trace.json onto https://ui.perfetto.dev/
#    python perf_takehome.py Tests.test_kernel_trace
# Then run `python watch_trace.py` in another tab, it'll open a browser tab, then click "Open Perfetto"
# You can then keep that open and re-run the test to see a new trace.

# To run the proper checks to see which thresholds you pass:
#    python tests/submission_tests.py

if __name__ == "__main__":
    unittest.main()
