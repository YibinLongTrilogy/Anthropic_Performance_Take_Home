from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
import random
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perf_takehome import KernelBuilder
from tests.frozen_problem import (  # type: ignore
    Input,
    Machine,
    N_CORES,
    Tree,
    build_mem_image,
    reference_kernel2,
)
from tools.opt_debug.lifetime_report import (
    analyze_scratch_lifetime_report,
    write_lifetime_artifacts,
)


def _parse_kwargs(raw: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid --kernel-kwargs-json: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("--kernel-kwargs-json must decode to an object")
    return parsed


def run() -> None:
    parser = argparse.ArgumentParser(
        description="Run one kernel build and emit a scratch lifetime pressure report."
    )
    parser.add_argument("--seed", type=int, default=123)
    parser.add_argument("--forest-height", type=int, default=10)
    parser.add_argument("--rounds", type=int, default=16)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument(
        "--kernel-kwargs-json",
        type=str,
        default="{}",
        help="JSON object passed to KernelBuilder, e.g. '{\"scheduler_beam_width\": 2}'.",
    )
    parser.add_argument(
        "--out-dir",
        type=str,
        default=str(REPO_ROOT / "docs" / "reports" / "optimizations" / "debug"),
    )
    args = parser.parse_args()

    kernel_kwargs = _parse_kwargs(args.kernel_kwargs_json)
    kernel_kwargs.setdefault("emit_debug", False)
    kernel_kwargs.setdefault("scheduler_profile", True)
    kernel_kwargs.setdefault("trace_phase_tags", True)

    random.seed(args.seed)
    forest = Tree.generate(args.forest_height)
    inp = Input.generate(forest, args.batch_size, args.rounds)
    mem = build_mem_image(forest, inp)

    kb = KernelBuilder(**kernel_kwargs)
    kb.build_kernel(forest.height, len(forest.values), len(inp.indices), args.rounds)

    machine = Machine(mem, kb.instrs, kb.debug_info(), n_cores=N_CORES)
    machine.enable_pause = False
    machine.enable_debug = False
    machine.run()

    for ref_mem in reference_kernel2(mem):
        pass

    inp_values_p = ref_mem[6]
    correct = (
        machine.mem[inp_values_p : inp_values_p + len(inp.values)]
        == ref_mem[inp_values_p : inp_values_p + len(inp.values)]
    )

    metadata = {
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "seed": args.seed,
        "forest_height": args.forest_height,
        "rounds": args.rounds,
        "batch_size": args.batch_size,
        "kernel_config": kernel_kwargs,
        "cycles": machine.cycle,
        "correct": correct,
    }
    report = analyze_scratch_lifetime_report(
        kb.schedule_profile(),
        scratch_map=kb.debug_info().scratch_map,
        metadata=metadata,
    )
    json_path, md_path = write_lifetime_artifacts(report, args.out_dir)

    summary = {
        "cycles": machine.cycle,
        "correct": correct,
        "max_live_overlap": report["summary"]["max_live_overlap"],
        "top_split_candidates": [
            {
                "addr": row.get("addr"),
                "label": row.get("label"),
                "live_span": row.get("live_span"),
                "peak_overlap": row.get("peak_overlap"),
            }
            for row in report.get("split_candidates", [])[:5]
        ],
        "latest_lifetime_json": json_path,
        "latest_lifetime_md": md_path,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    run()
