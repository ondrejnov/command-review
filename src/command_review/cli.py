from __future__ import annotations

import argparse
import json
import sys

from .analyzer import DECISION_REJECT, review_command


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="command-review",
        description="Review a shell command and return a JSON risk decision for AI agents.",
    )
    parser.add_argument(
        "command",
        nargs="*",
        help="Command to review. If omitted, the command is read from stdin.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print JSON output.",
    )
    parser.add_argument(
        "--fail-on-reject",
        action="store_true",
        help="Exit with status 2 when the decision is REJECT. JSON is still printed.",
    )
    parser.add_argument(
        "--stream-tools",
        action="store_true",
        help="Print each tool call as a JSON line to stderr as it runs.",
    )
    parser.add_argument(
        "--fast",
        action="store_true",
        help="Make a one-shot decision without calling inspection tools.",
    )
    parser.add_argument(
        "--model",
        help="OpenAI-compatible model name. Defaults to COMMAND_REVIEW_OPENAI_MODEL or the built-in default.",
    )
    parser.add_argument(
        "--base-url",
        help="OpenAI-compatible API base URL. Defaults to COMMAND_REVIEW_OPENAI_BASE_URL or the built-in default.",
    )
    parser.add_argument(
        "--prompt-file",
        help="Path to a custom system prompt file. Defaults to the packaged prompt.md.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    command = " ".join(args.command).strip() if args.command else sys.stdin.read().strip()

    def print_tool_call(tool_call: dict[str, object]) -> None:
        print(json.dumps({"tool_call": tool_call}), file=sys.stderr, flush=True)

    result = review_command(
        command,
        model=args.model,
        base_url=args.base_url,
        on_tool_call=print_tool_call if args.stream_tools else None,
        fast=args.fast,
        prompt_file=args.prompt_file,
    )
    print(json.dumps(result.to_json_dict(), indent=2 if args.pretty else None))

    if args.fail_on_reject and result.decision == DECISION_REJECT:
        return 2
    return 0
