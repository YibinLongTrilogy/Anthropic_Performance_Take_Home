from __future__ import annotations

from collections import Counter
from pathlib import Path
import json
from typing import Any

from problem import SLOT_LIMITS


ENGINES = [engine for engine in SLOT_LIMITS if engine != "debug"]


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _segment_span_cycles(segment: dict[str, Any]) -> int:
    cycle_rows = segment.get("cycle_engine_counts", [])
    if cycle_rows:
        return len(cycle_rows)
    scheduled = [_to_int(op.get("scheduled_cycle", -1), -1) for op in segment.get("ops", [])]
    if not scheduled:
        return 0
    return max(scheduled) + 1


def _derive_findings(report: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    summary = report.get("summary", {})
    blockers = Counter(report.get("global_rejections", {}))

    sampled = summary.get("sampled_ops", 0)
    skipped = summary.get("feasible_not_chosen", 0)
    if sampled > 0:
        skipped_pct = (skipped / sampled) * 100.0
        if skipped_pct >= 4.0:
            findings.append(
                f"Beam decisions skip feasible ready ops in {skipped_pct:.1f}% of sampled cases. "
                "Increase `scheduler_beam_width` and/or multi-start seeds."
            )

    if blockers.get("slot_fragmentation", 0) > blockers.get("engine_full", 0):
        findings.append(
            "Slot fragmentation exceeds hard engine saturation. Smaller/finer op groups may unlock more packing."
        )

    if blockers.get("strict_dep_wait", 0) > (blockers.get("engine_full", 0) + blockers.get("beam_choice", 0)):
        findings.append(
            "Strict dependency waits dominate scheduler stalls. Focus on reducing long write-after-read chains."
        )

    if blockers.get("weak_dep_wait", 0) > blockers.get("beam_choice", 0):
        findings.append(
            "Weak dependencies (WAR) materially delay ready ops. Consider shortening live ranges or separating readers/writers."
        )

    if not findings:
        findings.append("No dominant scheduler-choice inefficiency signal detected.")
    return findings


def analyze_scheduler_decision_report(
    schedule_profile: dict[str, Any] | None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schedule_profile = schedule_profile or {}
    metadata = dict(metadata or {})
    segments = list(schedule_profile.get("segments", []))

    notes: list[str] = []
    segment_reports: list[dict[str, Any]] = []
    global_rejections: Counter[str] = Counter()
    skipped_op_patterns: Counter[tuple[str, str]] = Counter()
    global_engine_used_slots: Counter[str] = Counter()
    global_engine_capacity: Counter[str] = Counter()

    total_cycles = 0
    total_sampled_ops = 0
    total_feasible_not_chosen = 0

    for seg_idx, segment in enumerate(segments):
        phase = str(segment.get("phase", f"segment:{seg_idx}"))
        decision_rows = list(segment.get("decision_trace", []))
        ops = list(segment.get("ops", []))
        op_by_id: dict[int, dict[str, Any]] = {}
        for fallback_op_id, op in enumerate(ops):
            op_id = _to_int(op.get("op_id", fallback_op_id))
            op_by_id[op_id] = op

        if not decision_rows:
            notes.append(
                f"Segment `{phase}` has no `decision_trace` rows. "
                "Enable KernelBuilder(scheduler_decision_trace=True) for this analysis."
            )
            continue

        seg_rejections: Counter[str] = Counter()
        seg_sampled_ops = 0
        seg_feasible_not_chosen = 0
        seg_cycles: list[dict[str, Any]] = []

        for row in decision_rows:
            sampled_ops = _to_int(row.get("sampled_ops", 0))
            feasible_not_chosen = _to_int(row.get("feasible_not_chosen", 0))
            cycle = _to_int(row.get("cycle", 0))
            used_slots = {engine: _to_int(row.get("used_slots", {}).get(engine, 0)) for engine in ENGINES}
            free_slots = {
                engine: max(0, SLOT_LIMITS[engine] - used_slots.get(engine, 0)) for engine in ENGINES
            }

            rejections = Counter(
                {
                    str(reason): _to_int(count)
                    for reason, count in dict(row.get("rejections", {})).items()
                }
            )

            seg_rejections.update(rejections)
            global_rejections.update(rejections)
            seg_sampled_ops += sampled_ops
            total_sampled_ops += sampled_ops
            seg_feasible_not_chosen += feasible_not_chosen
            total_feasible_not_chosen += feasible_not_chosen

            for engine in ENGINES:
                global_engine_used_slots[engine] += used_slots[engine]
                global_engine_capacity[engine] += SLOT_LIMITS[engine]

            for skipped in row.get("feasible_not_chosen_ops", []):
                op_id = _to_int(skipped.get("op_id", -1), -1)
                op_meta = op_by_id.get(op_id, {})
                opcodes = op_meta.get("opcodes", [])
                opcode = str(opcodes[0]) if opcodes else "unknown"
                engine = str(skipped.get("engine", op_meta.get("engine", "unknown")))
                skipped_op_patterns[(engine, opcode)] += 1

            score = (
                feasible_not_chosen * 8
                + rejections.get("slot_fragmentation", 0) * 3
                + rejections.get("engine_full", 0)
            )
            if score > 0:
                seg_cycles.append(
                    {
                        "cycle": cycle,
                        "score": score,
                        "sampled_ops": sampled_ops,
                        "feasible_not_chosen": feasible_not_chosen,
                        "slot_fragmentation": rejections.get("slot_fragmentation", 0),
                        "engine_full": rejections.get("engine_full", 0),
                        "beam_choice": rejections.get("beam_choice", 0),
                        "used_slots": used_slots,
                        "free_slots": free_slots,
                    }
                )

        seg_cycles.sort(key=lambda row: (row["score"], row["feasible_not_chosen"]), reverse=True)
        segment_reports.append(
            {
                "segment_index": seg_idx,
                "phase": phase,
                "cycles": len(decision_rows),
                "profile_cycles": _segment_span_cycles(segment),
                "sampled_ops": seg_sampled_ops,
                "feasible_not_chosen": seg_feasible_not_chosen,
                "feasible_not_chosen_pct": (
                    (seg_feasible_not_chosen / seg_sampled_ops) * 100.0 if seg_sampled_ops > 0 else 0.0
                ),
                "rejections": dict(sorted(seg_rejections.items(), key=lambda kv: (-kv[1], kv[0]))),
                "hotspot_cycles": seg_cycles[:15],
            }
        )
        total_cycles += len(decision_rows)

    segment_reports.sort(
        key=lambda seg: (seg["feasible_not_chosen"], seg["rejections"].get("slot_fragmentation", 0)),
        reverse=True,
    )
    skipped_rows = [
        {"engine": engine, "opcode": opcode, "count": count}
        for (engine, opcode), count in skipped_op_patterns.most_common(20)
    ]
    engine_utilization = {}
    for engine in ENGINES:
        capacity = global_engine_capacity.get(engine, 0)
        used = global_engine_used_slots.get(engine, 0)
        util_pct = (used / capacity * 100.0) if capacity > 0 else 0.0
        engine_utilization[engine] = {
            "used_slots": used,
            "capacity_slots": capacity,
            "util_pct": util_pct,
        }

    report: dict[str, Any] = {
        "summary": {
            "segment_count": len(segment_reports),
            "decision_cycles": total_cycles,
            "sampled_ops": total_sampled_ops,
            "feasible_not_chosen": total_feasible_not_chosen,
            "feasible_not_chosen_pct": (
                (total_feasible_not_chosen / total_sampled_ops) * 100.0
                if total_sampled_ops > 0
                else 0.0
            ),
        },
        "global_rejections": dict(sorted(global_rejections.items(), key=lambda kv: (-kv[1], kv[0]))),
        "engine_utilization": engine_utilization,
        "segment_reports": segment_reports,
        "top_skipped_patterns": skipped_rows,
        "metadata": metadata,
        "notes": notes,
    }
    report["findings"] = _derive_findings(report)
    return report


def render_scheduler_decision_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        f"# Scheduler Decision Report ({summary.get('decision_cycles', 0)} cycles)",
        "",
        "## Summary",
        f"- Segments with trace data: {summary.get('segment_count', 0)}",
        f"- Sampled ops: {summary.get('sampled_ops', 0)}",
        f"- Feasible ops not chosen: {summary.get('feasible_not_chosen', 0)} "
        f"({summary.get('feasible_not_chosen_pct', 0.0):.2f}%)",
        "",
        "## Global Rejections",
        "| reason | count |",
        "|:---|---:|",
    ]

    rejections = report.get("global_rejections", {})
    if rejections:
        for reason, count in rejections.items():
            lines.append(f"| `{reason}` | {count} |")
    else:
        lines.append("| (none) | 0 |")

    lines.extend(
        [
            "",
            "## Engine Slot Usage (During Scheduling)",
            "| engine | used_slots | capacity_slots | utilization |",
            "|:---|---:|---:|---:|",
        ]
    )
    for engine in ENGINES:
        row = report.get("engine_utilization", {}).get(engine, {})
        lines.append(
            "| `{engine}` | {used} | {capacity} | {util:.2f}% |".format(
                engine=engine,
                used=row.get("used_slots", 0),
                capacity=row.get("capacity_slots", 0),
                util=row.get("util_pct", 0.0),
            )
        )

    lines.extend(
        [
            "",
            "## Segment Opportunity Ranking",
            "| idx | phase | cycles | sampled_ops | skipped_feasible | skipped_pct | top_rejection |",
            "|---:|:---|---:|---:|---:|---:|:---|",
        ]
    )
    segments = report.get("segment_reports", [])
    if segments:
        for seg in segments[:20]:
            rej = seg.get("rejections", {})
            top_reason = "-"
            if rej:
                top_reason, top_count = max(rej.items(), key=lambda kv: kv[1])
                top_reason = f"{top_reason} ({top_count})"
            lines.append(
                "| {idx} | `{phase}` | {cycles} | {sampled} | {skipped} | {pct:.2f}% | {top_reason} |".format(
                    idx=seg.get("segment_index", 0),
                    phase=seg.get("phase", "segment"),
                    cycles=seg.get("cycles", 0),
                    sampled=seg.get("sampled_ops", 0),
                    skipped=seg.get("feasible_not_chosen", 0),
                    pct=seg.get("feasible_not_chosen_pct", 0.0),
                    top_reason=top_reason,
                )
            )
    else:
        lines.append("| 0 | (none) | 0 | 0 | 0 | 0.00% | - |")

    lines.extend(
        [
            "",
            "## Top Skipped Feasible Op Patterns",
            "| engine | opcode | count |",
            "|:---|:---|---:|",
        ]
    )
    patterns = report.get("top_skipped_patterns", [])
    if patterns:
        for row in patterns:
            lines.append(
                f"| `{row.get('engine', 'unknown')}` | `{row.get('opcode', 'unknown')}` | {row.get('count', 0)} |"
            )
    else:
        lines.append("| (none) | (none) | 0 |")

    lines.extend(
        [
            "",
            "## Findings",
        ]
    )
    for finding in report.get("findings", []):
        lines.append(f"- {finding}")

    notes = report.get("notes", [])
    if notes:
        lines.append("")
        lines.append("## Notes")
        for note in notes:
            lines.append(f"- {note}")

    lines.append("")
    return "\n".join(lines)


def write_scheduler_decision_artifacts(
    report: dict[str, Any],
    out_dir: str | Path,
    prefix: str = "latest_scheduler_decisions",
) -> tuple[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{prefix}.json"
    md_path = out / f"{prefix}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_scheduler_decision_markdown(report), encoding="utf-8")
    return str(json_path), str(md_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Render markdown for a scheduler decision JSON report.")
    parser.add_argument("--input-json", type=str, required=True)
    parser.add_argument("--output-md", type=str, default="")
    args = parser.parse_args()

    payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    md = render_scheduler_decision_markdown(payload)
    if args.output_md:
        Path(args.output_md).write_text(md, encoding="utf-8")
    else:
        print(md)
