from __future__ import annotations

from pathlib import Path
import json
from typing import Any


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _top_reason(blockers: dict[str, int] | None) -> str:
    blockers = blockers or {}
    if not blockers:
        return "-"
    reason, count = max(blockers.items(), key=lambda kv: kv[1])
    return f"{reason} ({count})"


def _counter_delta(base: dict[str, Any], candidate: dict[str, Any]) -> dict[str, int]:
    keys = set(base) | set(candidate)
    out = {}
    for key in sorted(keys):
        out[key] = _to_int(candidate.get(key, 0)) - _to_int(base.get(key, 0))
    return out


def compare_inefficiency_reports(
    base_report: dict[str, Any],
    candidate_report: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    metadata = dict(metadata or {})
    base_summary = dict(base_report.get("summary", {}))
    cand_summary = dict(candidate_report.get("summary", {}))

    base_cycles = _to_int(base_report.get("metadata", {}).get("cycles", base_summary.get("non_debug_cycles", 0)))
    cand_cycles = _to_int(
        candidate_report.get("metadata", {}).get("cycles", cand_summary.get("non_debug_cycles", 0))
    )
    cycle_delta = cand_cycles - base_cycles

    base_headroom = _to_int(base_summary.get("total_headroom_cycles", 0))
    cand_headroom = _to_int(cand_summary.get("total_headroom_cycles", 0))
    headroom_delta = cand_headroom - base_headroom

    blocker_deltas = _counter_delta(
        base_report.get("global_blockers", {}),
        candidate_report.get("global_blockers", {}),
    )

    engine_keys = sorted(
        set(base_report.get("global_idle_slots_by_reason", {}))
        | set(candidate_report.get("global_idle_slots_by_reason", {}))
    )
    idle_slot_deltas: dict[str, dict[str, int]] = {}
    for engine in engine_keys:
        idle_slot_deltas[engine] = _counter_delta(
            base_report.get("global_idle_slots_by_reason", {}).get(engine, {}),
            candidate_report.get("global_idle_slots_by_reason", {}).get(engine, {}),
        )

    base_segments = list(base_report.get("segment_reports", []))
    cand_segments = list(candidate_report.get("segment_reports", []))
    segment_deltas: list[dict[str, Any]] = []
    for idx in range(max(len(base_segments), len(cand_segments))):
        base_seg = base_segments[idx] if idx < len(base_segments) else {}
        cand_seg = cand_segments[idx] if idx < len(cand_segments) else {}
        phase = str(cand_seg.get("phase", base_seg.get("phase", f"segment:{idx}")))
        base_seg_cycles = _to_int(base_seg.get("cycles", 0))
        cand_seg_cycles = _to_int(cand_seg.get("cycles", 0))
        base_seg_headroom = _to_int(base_seg.get("headroom_cycles", 0))
        cand_seg_headroom = _to_int(cand_seg.get("headroom_cycles", 0))

        segment_deltas.append(
            {
                "segment_index": idx,
                "phase": phase,
                "base_cycles": base_seg_cycles,
                "candidate_cycles": cand_seg_cycles,
                "cycle_delta": cand_seg_cycles - base_seg_cycles,
                "base_headroom": base_seg_headroom,
                "candidate_headroom": cand_seg_headroom,
                "headroom_delta": cand_seg_headroom - base_seg_headroom,
                "base_top_blocker": _top_reason(base_seg.get("blockers", {})),
                "candidate_top_blocker": _top_reason(cand_seg.get("blockers", {})),
                "base_p95_slack": float(base_seg.get("slack", {}).get("p95", 0.0)),
                "candidate_p95_slack": float(cand_seg.get("slack", {}).get("p95", 0.0)),
                "p95_slack_delta": float(cand_seg.get("slack", {}).get("p95", 0.0))
                - float(base_seg.get("slack", {}).get("p95", 0.0)),
            }
        )
    segment_deltas.sort(key=lambda row: (abs(row["cycle_delta"]), abs(row["headroom_delta"])), reverse=True)

    base_hotspots = {int(row.get("addr", -1)): row for row in base_report.get("scratch_hotspots", [])}
    cand_hotspots = {int(row.get("addr", -1)): row for row in candidate_report.get("scratch_hotspots", [])}
    hotspot_deltas: list[dict[str, Any]] = []
    for addr in sorted(set(base_hotspots) | set(cand_hotspots)):
        if addr < 0:
            continue
        base_row = base_hotspots.get(addr, {})
        cand_row = cand_hotspots.get(addr, {})
        row = {
            "addr": addr,
            "label": cand_row.get("label", base_row.get("label", f"scratch[{addr}]")),
            "tight_delta": _to_int(cand_row.get("tight_edges", 0)) - _to_int(base_row.get("tight_edges", 0)),
            "near_strict_delta": _to_int(cand_row.get("near_strict_edges", 0))
            - _to_int(base_row.get("near_strict_edges", 0)),
            "dep_edges_delta": _to_int(cand_row.get("dep_edges", 0))
            - _to_int(base_row.get("dep_edges", 0)),
            "reads_delta": _to_int(cand_row.get("reads", 0)) - _to_int(base_row.get("reads", 0)),
            "writes_delta": _to_int(cand_row.get("writes", 0)) - _to_int(base_row.get("writes", 0)),
        }
        row["impact_score"] = (
            abs(row["tight_delta"]) * 8
            + abs(row["near_strict_delta"]) * 3
            + abs(row["dep_edges_delta"])
        )
        hotspot_deltas.append(row)
    hotspot_deltas.sort(key=lambda row: row["impact_score"], reverse=True)

    findings: list[str] = []
    if cycle_delta < 0:
        findings.append(f"Candidate improves total cycles by {-cycle_delta}.")
    elif cycle_delta > 0:
        findings.append(f"Candidate regresses total cycles by {cycle_delta}.")
    else:
        findings.append("Candidate and baseline have equal total cycles.")

    if headroom_delta < 0:
        findings.append(
            f"Estimated schedule headroom improved by {-headroom_delta} cycles (lower residual headroom)."
        )
    elif headroom_delta > 0:
        findings.append(f"Estimated schedule headroom worsened by {headroom_delta} cycles.")

    scheduler_choice_delta = blocker_deltas.get("scheduler_choice", 0) + blocker_deltas.get("beam_choice", 0)
    if scheduler_choice_delta < 0:
        findings.append("Scheduler-choice blockers decreased.")
    elif scheduler_choice_delta > 0:
        findings.append("Scheduler-choice blockers increased.")

    report: dict[str, Any] = {
        "summary": {
            "base_cycles": base_cycles,
            "candidate_cycles": cand_cycles,
            "cycle_delta": cycle_delta,
            "base_headroom_cycles": base_headroom,
            "candidate_headroom_cycles": cand_headroom,
            "headroom_delta": headroom_delta,
        },
        "blocker_deltas": blocker_deltas,
        "idle_slot_deltas": idle_slot_deltas,
        "segment_deltas": segment_deltas,
        "hotspot_deltas": hotspot_deltas[:40],
        "metadata": metadata,
        "findings": findings,
    }
    return report


def render_inefficiency_diff_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        "# Inefficiency Diff Report",
        "",
        "## Summary",
        f"- Baseline cycles: {summary.get('base_cycles', 0)}",
        f"- Candidate cycles: {summary.get('candidate_cycles', 0)}",
        f"- Cycle delta (candidate - baseline): {summary.get('cycle_delta', 0):+d}",
        f"- Headroom delta (candidate - baseline): {summary.get('headroom_delta', 0):+d}",
        "",
        "## Blocker Deltas",
        "| reason | delta |",
        "|:---|---:|",
    ]

    blocker_deltas = report.get("blocker_deltas", {})
    if blocker_deltas:
        for reason, delta in blocker_deltas.items():
            lines.append(f"| `{reason}` | {delta:+d} |")
    else:
        lines.append("| (none) | +0 |")

    lines.extend(
        [
            "",
            "## Segment Deltas",
            "| idx | phase | cycle_delta | headroom_delta | p95_slack_delta | baseline_top_blocker | candidate_top_blocker |",
            "|---:|:---|---:|---:|---:|:---|:---|",
        ]
    )
    segments = report.get("segment_deltas", [])
    if segments:
        for row in segments[:20]:
            lines.append(
                "| {idx} | `{phase}` | {cycle_delta:+d} | {headroom_delta:+d} | {slack_delta:+.1f} | {base_blocker} | {cand_blocker} |".format(
                    idx=row.get("segment_index", 0),
                    phase=row.get("phase", "segment"),
                    cycle_delta=row.get("cycle_delta", 0),
                    headroom_delta=row.get("headroom_delta", 0),
                    slack_delta=row.get("p95_slack_delta", 0.0),
                    base_blocker=row.get("base_top_blocker", "-"),
                    cand_blocker=row.get("candidate_top_blocker", "-"),
                )
            )
    else:
        lines.append("| 0 | (none) | +0 | +0 | +0.0 | - | - |")

    lines.extend(
        [
            "",
            "## Idle Slot Deltas",
            "| engine | no_ready_ops | slot_fragmentation | scheduler_choice | dependency_tail |",
            "|:---|---:|---:|---:|---:|",
        ]
    )
    idle_slot_deltas = report.get("idle_slot_deltas", {})
    if idle_slot_deltas:
        for engine in sorted(idle_slot_deltas):
            row = idle_slot_deltas.get(engine, {})
            lines.append(
                "| `{engine}` | {no_ready:+d} | {frag:+d} | {choice:+d} | {tail:+d} |".format(
                    engine=engine,
                    no_ready=_to_int(row.get("no_ready_ops", 0)),
                    frag=_to_int(row.get("slot_fragmentation", 0)),
                    choice=_to_int(row.get("scheduler_choice", 0)),
                    tail=_to_int(row.get("dependency_tail", 0)),
                )
            )
    else:
        lines.append("| (none) | +0 | +0 | +0 | +0 |")

    lines.extend(
        [
            "",
            "## Scratch Hotspot Deltas",
            "| addr | label | tight_delta | near_strict_delta | dep_edges_delta |",
            "|---:|:---|---:|---:|---:|",
        ]
    )
    hotspots = report.get("hotspot_deltas", [])
    if hotspots:
        for row in hotspots[:20]:
            lines.append(
                "| {addr} | `{label}` | {tight:+d} | {near:+d} | {dep:+d} |".format(
                    addr=row.get("addr", 0),
                    label=row.get("label", "unknown"),
                    tight=row.get("tight_delta", 0),
                    near=row.get("near_strict_delta", 0),
                    dep=row.get("dep_edges_delta", 0),
                )
            )
    else:
        lines.append("| 0 | (none) | +0 | +0 | +0 |")

    lines.append("")
    lines.append("## Findings")
    for finding in report.get("findings", []):
        lines.append(f"- {finding}")
    lines.append("")
    return "\n".join(lines)


def write_inefficiency_diff_artifacts(
    report: dict[str, Any],
    out_dir: str | Path,
    prefix: str = "latest_inefficiency_diff",
) -> tuple[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{prefix}.json"
    md_path = out / f"{prefix}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_inefficiency_diff_markdown(report), encoding="utf-8")
    return str(json_path), str(md_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Render markdown for an inefficiency diff JSON report.")
    parser.add_argument("--input-json", type=str, required=True)
    parser.add_argument("--output-md", type=str, default="")
    args = parser.parse_args()

    payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    md = render_inefficiency_diff_markdown(payload)
    if args.output_md:
        Path(args.output_md).write_text(md, encoding="utf-8")
    else:
        print(md)
