import csv
import json
import subprocess
import sys
from pathlib import Path

from command_review.analyzer import ReviewResult
from command_review.calls import analyze_calls_csv, build_parser


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def write_calls_csv(path, commands):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["occurred_at", "command"])
        writer.writeheader()
        for index, command in enumerate(commands, start=1):
            writer.writerow({"occurred_at": f"2026-01-01T00:00:0{index}", "command": command})


def result(decision):
    return ReviewResult(
        decision=decision,
        risk_level="LOW",
        summary="Reviewed command.",
        risks=[],
        reasoning="Test decision.",
    )


def test_analyzes_calls_csv_incrementally_and_persists_state(tmp_path):
    csv_path = tmp_path / "calls.csv"
    state_path = tmp_path / "state.json"
    write_calls_csv(csv_path, ["git status", "rm -rf tmp"])
    decisions = iter(["APPROVE", "REQUIRE_CONFIRMATION"])
    seen_commands = []

    def reviewer(command, **kwargs):
        seen_commands.append(command)
        return result(next(decisions))

    state = analyze_calls_csv(csv_path, state_path=state_path, reviewer=reviewer)

    assert seen_commands == ["git status", "rm -rf tmp"]
    assert state["rows"] == {
        "2": {"decision": "APPROVE"},
        "3": {"decision": "REQUIRE_CONFIRMATION"},
    }
    assert state["require_confirmation"] == [
        {
            "row_number": 3,
            "decision": "REQUIRE_CONFIRMATION",
            "risk_level": "LOW",
            "summary": "Reviewed command.",
            "risks": [],
            "reasoning": "Test decision.",
            "command": "rm -rf tmp",
        }
    ]
    assert json.loads(state_path.read_text(encoding="utf-8")) == state


def test_skips_rows_already_present_in_state(tmp_path):
    csv_path = tmp_path / "calls.csv"
    state_path = tmp_path / "state.json"
    write_calls_csv(csv_path, ["git status", "npm test"])
    state_path.write_text(
        json.dumps({"rows": {"2": {"decision": "APPROVE"}}, "require_confirmation": []}),
        encoding="utf-8",
    )
    seen_commands = []

    def reviewer(command, **kwargs):
        seen_commands.append(command)
        return result("APPROVE")

    state = analyze_calls_csv(csv_path, state_path=state_path, reviewer=reviewer)

    assert seen_commands == ["npm test"]
    assert state["rows"] == {
        "2": {"decision": "APPROVE"},
        "3": {"decision": "APPROVE"},
    }


def test_calls_module_can_be_run_as_script():
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "src" / "command_review" / "calls.py"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert "command-review-calls" in result.stdout


def test_calls_parser_accepts_cwd_alias(tmp_path):
    args = build_parser().parse_args(["--cwd", str(tmp_path)])

    assert args.workspace == str(tmp_path)
