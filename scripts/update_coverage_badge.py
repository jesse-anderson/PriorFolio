# SPDX-License-Identifier: GPL-3.0-only
"""Render the measured line coverage of all three application modules."""
import argparse
import json
from pathlib import Path

EXPECTED = {"priorfolio.py", "priorfolio_exports.py", "priorfolio_gui.py"}


def badge(report):
    files = report["files"]
    if {Path(name).name for name in files} != EXPECTED or len(files) != len(EXPECTED):
        raise ValueError("Coverage must include exactly the CLI, exports and GUI modules.")
    covered = sum(item["summary"]["covered_lines"] for item in files.values())
    total = sum(item["summary"]["num_statements"] for item in files.values())
    if not total or not 0 <= covered <= total:
        raise ValueError("Coverage totals are invalid.")
    percent = 100 * covered / total
    color = "brightgreen" if percent >= 90 else "green" if percent >= 80 else "yellow" if percent >= 65 else "red"
    return {"schemaVersion": 1, "label": "test coverage", "message": f"{percent:.2f}%", "color": color}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".github/coverage-badge.json"))
    args = parser.parse_args()
    result = badge(json.loads(args.report.read_text(encoding="utf-8")))
    args.output.write_text(json.dumps(result) + "\n", encoding="utf-8")
    print(result["message"])
