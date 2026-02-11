from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from perf_takehome import do_kernel_test
from tools.opt_debug.analyze_schedule import analyze_schedule_artifacts
from tools.opt_debug.compare_inefficiency import compare_inefficiency_reports
from tools.opt_debug.inefficiency_report import analyze_inefficiency_report
from tools.opt_debug.lifetime_report import analyze_scratch_lifetime_report
from tools.opt_debug.scheduler_decision_report import analyze_scheduler_decision_report


def run() -> None:
    parser = argparse.ArgumentParser(description="Standalone sanity checks for opt_debug tooling.")
    parser.add_argument(
        "--out-dir",
        type=str,
        default="",
        help="Optional output directory to retain generated diagnostics.",
    )
    args = parser.parse_args()

    # Check 1: schedule analyzer returns expected structure on a tiny synthetic program.
    instrs = [
        {"valu": [("+", 1, 2, 3)]},
        {"load": [("const", 4, 123)], "flow": [("halt",)]},
    ]
    run_diag = analyze_schedule_artifacts(instrs, include_candidates=True)
    assert run_diag.cycle_count == 2
    assert "valu" in run_diag.engine_pressure
    assert len(run_diag.candidates) >= 1

    # Check 1b: inefficiency analyzer returns expected structure on synthetic profile data.
    synthetic_profile = {
        "segments": [
            {
                "phase": "segment:0",
                "ops": [
                    {
                        "op_id": 0,
                        "engine": "valu",
                        "slot_count": 1,
                        "reads": [2, 3],
                        "writes": [1],
                        "crit_path": 2,
                        "priority": 10,
                        "scheduled_cycle": 0,
                    },
                    {
                        "op_id": 1,
                        "engine": "load",
                        "slot_count": 1,
                        "reads": [1],
                        "writes": [4],
                        "crit_path": 1,
                        "priority": 5,
                        "scheduled_cycle": 1,
                    },
                ],
                "cycle_engine_counts": [{"valu": 1}, {"load": 1}],
                "scheduler_candidates": [{"seed": 123, "cycles": 2}],
            }
        ]
    }
    ineff_report = analyze_inefficiency_report(
        instrs=[{"valu": [("+", 1, 2, 3)]}, {"load": [("load", 4, 1)]}],
        schedule_profile=synthetic_profile,
        scratch_map={1: ("tmp", 1), 2: ("a", 1), 3: ("b", 1), 4: ("out", 1)},
    )
    assert ineff_report["summary"]["segment_count"] == 1
    assert ineff_report["summary"]["combined_lower_bound_cycles"] == 2
    assert len(ineff_report["scratch_hotspots"]) >= 1

    # Check 1c: scheduler decision report reads decision trace rows and aggregates rejections.
    synthetic_decision_profile = {
        "segments": [
            {
                "phase": "segment:0",
                "ops": synthetic_profile["segments"][0]["ops"],
                "cycle_engine_counts": [{"valu": 1}, {"load": 1}],
                "decision_trace": [
                    {
                        "cycle": 0,
                        "sampled_ops": 2,
                        "feasible_not_chosen": 1,
                        "rejections": {"beam_choice": 1},
                        "used_slots": {"valu": 1},
                        "feasible_not_chosen_ops": [{"op_id": 1, "engine": "load"}],
                    },
                    {
                        "cycle": 1,
                        "sampled_ops": 1,
                        "feasible_not_chosen": 0,
                        "rejections": {},
                        "used_slots": {"load": 1},
                        "feasible_not_chosen_ops": [],
                    },
                ],
            }
        ]
    }
    decision_report = analyze_scheduler_decision_report(synthetic_decision_profile)
    assert decision_report["summary"]["decision_cycles"] == 2
    assert decision_report["summary"]["feasible_not_chosen"] == 1

    # Check 1d: lifetime report produces pressure rows and split candidates from schedule profile.
    lifetime_report = analyze_scratch_lifetime_report(
        synthetic_profile,
        scratch_map={1: ("tmp", 1), 2: ("a", 1), 3: ("b", 1), 4: ("out", 1)},
    )
    assert lifetime_report["summary"]["address_count"] >= 1
    assert len(lifetime_report["top_lifetimes"]) >= 1

    # Check 1e: diff report can compare inefficiency payloads.
    diff_report = compare_inefficiency_reports(ineff_report, ineff_report)
    assert diff_report["summary"]["cycle_delta"] == 0

    # Check 2: kernel run can emit diagnostics artifacts and preserve expected cycles.
    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        cleanup = False
    else:
        out_dir = Path(tempfile.mkdtemp(prefix="opt_debug_selfcheck_"))
        cleanup = True

    cycles = do_kernel_test(
        10,
        16,
        256,
        diagnostics_out=str(out_dir),
        kernel_kwargs={
            "interleave_groups": 25,
            "interleave_groups_early": 26,
        },
    )
    assert cycles > 0, f"unexpected non-positive cycle count: {cycles}"

    json_path = out_dir / "latest_run.json"
    md_path = out_dir / "latest_run.md"
    assert json_path.exists(), "latest_run.json was not created"
    assert md_path.exists(), "latest_run.md was not created"

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["cycle_count"] == cycles
    assert "engine_pressure" in payload
    assert "candidates" in payload

    summary = {
        "status": "ok",
        "cycles": cycles,
        "latest_run_json": str(json_path),
        "latest_run_md": str(md_path),
        "temp_output": cleanup,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    run()
