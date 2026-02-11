from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tools.opt_debug.compare_inefficiency import (
    compare_inefficiency_reports,
    write_inefficiency_diff_artifacts,
)


def run() -> None:
    parser = argparse.ArgumentParser(
        description="Compare two inefficiency reports and emit a delta report."
    )
    parser.add_argument("--base-json", type=str, required=True)
    parser.add_argument("--candidate-json", type=str, required=True)
    parser.add_argument(
        "--out-dir",
        type=str,
        default=str(REPO_ROOT / "docs" / "reports" / "optimizations" / "debug"),
    )
    parser.add_argument("--prefix", type=str, default="latest_inefficiency_diff")
    args = parser.parse_args()

    base_path = Path(args.base_json)
    cand_path = Path(args.candidate_json)
    base_report = json.loads(base_path.read_text(encoding="utf-8"))
    cand_report = json.loads(cand_path.read_text(encoding="utf-8"))

    diff = compare_inefficiency_reports(
        base_report,
        cand_report,
        metadata={"base_json": str(base_path), "candidate_json": str(cand_path)},
    )
    json_path, md_path = write_inefficiency_diff_artifacts(
        diff,
        out_dir=args.out_dir,
        prefix=args.prefix,
    )

    summary = {
        "base_json": str(base_path),
        "candidate_json": str(cand_path),
        "cycle_delta": diff["summary"]["cycle_delta"],
        "headroom_delta": diff["summary"]["headroom_delta"],
        "latest_diff_json": json_path,
        "latest_diff_md": md_path,
    }
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    run()
