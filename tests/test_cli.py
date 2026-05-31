from command_review import cli
from command_review.analyzer import ReviewResult


def test_cli_passes_cwd_to_reviewer(monkeypatch, tmp_path, capsys):
    calls = []

    def fake_review_command(command, **kwargs):
        calls.append((command, kwargs))
        return ReviewResult(
            decision="APPROVE",
            risk_level="LOW",
            summary="Reviewed command.",
            risks=[],
            reasoning="Test decision.",
        )

    monkeypatch.setattr(cli, "review_command", fake_review_command)

    exit_code = cli.main(["--cwd", str(tmp_path), "--", "git", "status"])

    assert exit_code == 0
    assert calls[0][0] == "git status"
    assert calls[0][1]["workspace"] == str(tmp_path)
    assert '"decision": "APPROVE"' in capsys.readouterr().out
