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
from tools.opt_debug.scheduler_decision_report import (
    analyze_scheduler_decision_report,
    write_scheduler_decision_artifacts,
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
        description="Run one kernel build and emit a scheduler decision trace report."
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
    kernel_kwargs.setdefault("scheduler_decision_trace", True)

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
    report = analyze_scheduler_decision_report(kb.schedule_profile(), metadata=metadata)
    json_path, md_path = write_scheduler_decision_artifacts(report, args.out_dir)

    top_rejections = list(report.get("global_rejections", {}).items())[:5]
    summary = {
        "cycles": machine.cycle,
        "correct": correct,
        "decision_cycles": report["summary"]["decision_cycles"],
        "feasible_not_chosen_pct": report["summary"]["feasible_not_chosen_pct"],
        "top_rejections": top_rejections,
        "latest_scheduler_decision_json": json_path,
        "latest_scheduler_decision_md": md_path,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    run()
