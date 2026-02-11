from __future__ import annotations

from collections import defaultdict
from pathlib import Path
import json
from typing import Any


def _to_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _build_addr_labels(scratch_map: dict[int, tuple[str, int]] | None) -> dict[int, str]:
    if not scratch_map:
        return {}
    labels: dict[int, str] = {}
    for addr, entry in sorted(scratch_map.items()):
        if not isinstance(entry, tuple) or len(entry) != 2:
            continue
        name, length = entry
        if int(length) <= 1:
            labels[int(addr)] = str(name)
            continue
        for offset in range(int(length)):
            labels[int(addr) + offset] = f"{name}[{offset}]"
    return labels


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
    max_overlap = summary.get("max_live_overlap", 0)
    if max_overlap >= 16:
        findings.append(
            f"Peak live overlap is {max_overlap}. Reducing long-lived temporaries can directly improve scheduling freedom."
        )

    candidates = report.get("split_candidates", [])
    if candidates:
        top = candidates[0]
        findings.append(
            f"Top split candidate is `{top['label']}` (addr {top['addr']}) with span {top['live_span']} "
            f"and peak overlap {top['peak_overlap']}."
        )
    else:
        mutable = report.get("top_mutable_lifetimes", [])
        if mutable:
            top = mutable[0]
            findings.append(
                f"Top mutable lifetime hotspot is `{top['label']}` (addr {top['addr']}) with span "
                f"{top['live_span']} and {top['write_count']} writes."
            )
        else:
            findings.append(
                "Most long-lived values are write-once constants/pointers; focus on reducing mutable temporary overlap."
            )

    return findings


def analyze_scratch_lifetime_report(
    schedule_profile: dict[str, Any] | None,
    scratch_map: dict[int, tuple[str, int]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schedule_profile = schedule_profile or {}
    metadata = dict(metadata or {})
    labels = _build_addr_labels(scratch_map)
    segments = list(schedule_profile.get("segments", []))

    notes: list[str] = []
    if not segments:
        notes.append("No schedule profile segments present.")

    addr_reads: dict[int, list[int]] = defaultdict(list)
    addr_writes: dict[int, list[int]] = defaultdict(list)
    addr_reader_ops: dict[int, set[tuple[str, int]]] = defaultdict(set)
    addr_writer_ops: dict[int, set[tuple[str, int]]] = defaultdict(set)
    addr_segments: dict[int, set[str]] = defaultdict(set)

    total_cycles = 0
    for seg_idx, segment in enumerate(segments):
        phase = str(segment.get("phase", f"segment:{seg_idx}"))
        span = _segment_span_cycles(segment)
        ops = list(segment.get("ops", []))

        for fallback_op_id, op in enumerate(ops):
            op_id = _to_int(op.get("op_id", fallback_op_id))
            scheduled_cycle = _to_int(op.get("scheduled_cycle", -1), -1)
            if scheduled_cycle < 0:
                continue
            global_cycle = total_cycles + scheduled_cycle
            reads = [_to_int(addr) for addr in op.get("reads", [])]
            writes = [_to_int(addr) for addr in op.get("writes", [])]

            for addr in reads:
                addr_reads[addr].append(global_cycle)
                addr_reader_ops[addr].add((phase, op_id))
                addr_segments[addr].add(phase)
            for addr in writes:
                addr_writes[addr].append(global_cycle)
                addr_writer_ops[addr].add((phase, op_id))
                addr_segments[addr].add(phase)

        total_cycles += span

    all_addrs = sorted(set(addr_reads) | set(addr_writes))
    intervals: dict[int, tuple[int, int]] = {}
    lifetime_rows: list[dict[str, Any]] = []

    for addr in all_addrs:
        reads = sorted(addr_reads.get(addr, []))
        writes = sorted(addr_writes.get(addr, []))
        first_read = reads[0] if reads else None
        last_read = reads[-1] if reads else None
        first_write = writes[0] if writes else None
        last_write = writes[-1] if writes else None

        starts = [value for value in (first_read, first_write) if value is not None]
        ends = [value for value in (last_read, last_write) if value is not None]
        if not starts or not ends:
            continue

        live_start = min(starts)
        live_end = max(ends)
        if live_end < live_start:
            continue
        intervals[addr] = (live_start, live_end)

        tail_cycles = 0
        if last_read is not None and last_write is not None:
            tail_cycles = max(0, last_read - last_write)

        lifetime_rows.append(
            {
                "addr": addr,
                "label": labels.get(addr, f"scratch[{addr}]"),
                "live_start": live_start,
                "live_end": live_end,
                "live_span": (live_end - live_start + 1),
                "first_read_cycle": first_read,
                "last_read_cycle": last_read,
                "first_write_cycle": first_write,
                "last_write_cycle": last_write,
                "read_count": len(reads),
                "write_count": len(writes),
                "reader_ops": len(addr_reader_ops.get(addr, set())),
                "writer_ops": len(addr_writer_ops.get(addr, set())),
                "segment_count": len(addr_segments.get(addr, set())),
                "tail_after_last_write": tail_cycles,
            }
        )

    max_cycle = max((end for _, end in intervals.values()), default=max(0, total_cycles - 1))
    overlap: list[int] = [0] * (max_cycle + 2)
    for start, end in intervals.values():
        overlap[start] += 1
        overlap[end + 1] -= 1
    active = 0
    for cycle in range(max_cycle + 1):
        active += overlap[cycle]
        overlap[cycle] = active

    peaks = sorted(range(max_cycle + 1), key=lambda cycle: overlap[cycle], reverse=True)
    overlap_peaks: list[dict[str, Any]] = []
    for cycle in peaks[:20]:
        live_count = overlap[cycle]
        if live_count <= 0:
            break
        active_addrs = [
            addr for addr, (start, end) in intervals.items() if start <= cycle <= end
        ]
        active_addrs.sort(
            key=lambda addr: (
                intervals[addr][1] - intervals[addr][0] + 1,
                addr,
            ),
            reverse=True,
        )
        overlap_peaks.append(
            {
                "cycle": cycle,
                "live_values": live_count,
                "top_labels": [labels.get(addr, f"scratch[{addr}]") for addr in active_addrs[:10]],
            }
        )

    lifetime_by_addr = {row["addr"]: row for row in lifetime_rows}
    for addr, (start, end) in intervals.items():
        row = lifetime_by_addr.get(addr)
        if row is None:
            continue
        peak_overlap = max(overlap[start : end + 1]) if end >= start else 0
        row["peak_overlap"] = peak_overlap
        row["pressure_score"] = (
            row["live_span"] * max(1, peak_overlap - 1)
            + row["write_count"] * 8
            + row["tail_after_last_write"] * 4
            + row["read_count"]
        )

    lifetime_rows.sort(
        key=lambda row: (
            row.get("pressure_score", 0),
            row["live_span"],
            row.get("peak_overlap", 0),
            row["write_count"],
        ),
        reverse=True,
    )

    mutable_rows = [row for row in lifetime_rows if row["write_count"] >= 2]

    split_candidates: list[dict[str, Any]] = []
    top_pressure = mutable_rows[:40]
    interval_items = list(intervals.items())
    for row in top_pressure:
        addr = row["addr"]
        start, end = intervals[addr]
        partners: list[tuple[int, int]] = []
        for other_addr, (other_start, other_end) in interval_items:
            if other_addr == addr:
                continue
            overlap_cycles = max(0, min(end, other_end) - max(start, other_start) + 1)
            if overlap_cycles <= 0:
                continue
            partners.append((overlap_cycles, other_addr))
        partners.sort(reverse=True)
        partner_rows = [
            {
                "addr": other_addr,
                "label": labels.get(other_addr, f"scratch[{other_addr}]"),
                "overlap_cycles": overlap_cycles,
            }
            for overlap_cycles, other_addr in partners[:4]
        ]

        if row["live_span"] < 10:
            continue
        if row.get("peak_overlap", 0) < 6:
            continue

        reason_parts = []
        if row["tail_after_last_write"] > 0:
            reason_parts.append("reader tail after final write")
        if row["write_count"] >= 2:
            reason_parts.append("multi-write temporary")
        if row["segment_count"] > 1:
            reason_parts.append("cross-segment lifetime")

        candidate = {
            **row,
            "top_overlap_partners": partner_rows,
            "reason": ", ".join(reason_parts) if reason_parts else "long high-overlap lifetime",
        }
        split_candidates.append(candidate)

    split_candidates.sort(
        key=lambda row: (row["pressure_score"], row["tail_after_last_write"], row["live_span"]),
        reverse=True,
    )

    report: dict[str, Any] = {
        "summary": {
            "address_count": len(lifetime_rows),
            "segment_count": len(segments),
            "scheduled_cycles": total_cycles,
            "max_live_overlap": max(overlap[: max_cycle + 1], default=0),
            "max_mutable_live_overlap": max((row.get("peak_overlap", 0) for row in mutable_rows), default=0),
            "avg_live_span": (
                (sum(row["live_span"] for row in lifetime_rows) / len(lifetime_rows))
                if lifetime_rows
                else 0.0
            ),
            "max_live_span": max((row["live_span"] for row in lifetime_rows), default=0),
        },
        "overlap_peaks": overlap_peaks,
        "top_lifetimes": lifetime_rows[:40],
        "top_mutable_lifetimes": mutable_rows[:40],
        "split_candidates": split_candidates[:25],
        "metadata": metadata,
        "notes": notes,
    }
    report["findings"] = _derive_findings(report)
    return report


def render_lifetime_markdown(report: dict[str, Any]) -> str:
    summary = report.get("summary", {})
    lines = [
        f"# Scratch Lifetime Report ({summary.get('scheduled_cycles', 0)} scheduled cycles)",
        "",
        "## Summary",
        f"- Addresses analyzed: {summary.get('address_count', 0)}",
        f"- Segments: {summary.get('segment_count', 0)}",
        f"- Max live overlap: {summary.get('max_live_overlap', 0)}",
        f"- Max mutable overlap: {summary.get('max_mutable_live_overlap', 0)}",
        f"- Avg live span: {summary.get('avg_live_span', 0.0):.2f} cycles",
        f"- Max live span: {summary.get('max_live_span', 0)} cycles",
        "",
        "## Peak Overlap Cycles",
        "| cycle | live_values | top_labels |",
        "|---:|---:|:---|",
    ]

    overlap_peaks = report.get("overlap_peaks", [])
    if overlap_peaks:
        for row in overlap_peaks[:12]:
            labels = ", ".join(f"`{label}`" for label in row.get("top_labels", [])[:6])
            lines.append(
                f"| {row.get('cycle', 0)} | {row.get('live_values', 0)} | {labels if labels else '-'} |"
            )
    else:
        lines.append("| 0 | 0 | - |")

    lines.extend(
        [
            "",
            "## Top Mutable Lifetime Pressure",
            "| addr | label | span | peak_overlap | reads | writes | tail_after_write |",
            "|---:|:---|---:|---:|---:|---:|---:|",
        ]
    )
    mutable_rows = report.get("top_mutable_lifetimes", [])
    if mutable_rows:
        for row in mutable_rows[:20]:
            lines.append(
                "| {addr} | `{label}` | {span} | {peak} | {reads} | {writes} | {tail} |".format(
                    addr=row.get("addr", 0),
                    label=row.get("label", "unknown"),
                    span=row.get("live_span", 0),
                    peak=row.get("peak_overlap", 0),
                    reads=row.get("read_count", 0),
                    writes=row.get("write_count", 0),
                    tail=row.get("tail_after_last_write", 0),
                )
            )
    else:
        lines.append("| 0 | (none) | 0 | 0 | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "## Top Lifetime Pressure",
            "| addr | label | span | peak_overlap | reads | writes | tail_after_write |",
            "|---:|:---|---:|---:|---:|---:|---:|",
        ]
    )
    for row in report.get("top_lifetimes", [])[:20]:
        lines.append(
            "| {addr} | `{label}` | {span} | {peak} | {reads} | {writes} | {tail} |".format(
                addr=row.get("addr", 0),
                label=row.get("label", "unknown"),
                span=row.get("live_span", 0),
                peak=row.get("peak_overlap", 0),
                reads=row.get("read_count", 0),
                writes=row.get("write_count", 0),
                tail=row.get("tail_after_last_write", 0),
            )
        )
    if not report.get("top_lifetimes"):
        lines.append("| 0 | (none) | 0 | 0 | 0 | 0 | 0 |")

    lines.extend(
        [
            "",
            "## Split Candidates",
            "| addr | label | reason | span | peak_overlap | top_partners |",
            "|---:|:---|:---|---:|---:|:---|",
        ]
    )
    candidates = report.get("split_candidates", [])
    if candidates:
        for row in candidates[:15]:
            partners = ", ".join(
                f"`{p.get('label', 'unknown')}`({p.get('overlap_cycles', 0)})"
                for p in row.get("top_overlap_partners", [])[:3]
            )
            lines.append(
                "| {addr} | `{label}` | {reason} | {span} | {peak} | {partners} |".format(
                    addr=row.get("addr", 0),
                    label=row.get("label", "unknown"),
                    reason=row.get("reason", "-"),
                    span=row.get("live_span", 0),
                    peak=row.get("peak_overlap", 0),
                    partners=partners if partners else "-",
                )
            )
    else:
        lines.append("| 0 | (none) | - | 0 | 0 | - |")

    lines.append("")
    lines.append("## Findings")
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


def write_lifetime_artifacts(
    report: dict[str, Any],
    out_dir: str | Path,
    prefix: str = "latest_lifetimes",
) -> tuple[str, str]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{prefix}.json"
    md_path = out / f"{prefix}.md"
    json_path.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    md_path.write_text(render_lifetime_markdown(report), encoding="utf-8")
    return str(json_path), str(md_path)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Render markdown for a lifetime JSON report.")
    parser.add_argument("--input-json", type=str, required=True)
    parser.add_argument("--output-md", type=str, default="")
    args = parser.parse_args()

    payload = json.loads(Path(args.input_json).read_text(encoding="utf-8"))
    md = render_lifetime_markdown(payload)
    if args.output_md:
        Path(args.output_md).write_text(md, encoding="utf-8")
    else:
        print(md)
