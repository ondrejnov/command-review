<div align="center">

# command-review

*Review shell commands before an AI agent or automation runs them*

![Python](https://img.shields.io/badge/Python-3.10%2B-3776ab?style=flat-square&logo=python&logoColor=white)
![OpenAI Compatible](https://img.shields.io/badge/OpenAI-compatible-412991?style=flat-square)
![JSON Output](https://img.shields.io/badge/output-JSON-000?style=flat-square)

[Features](#features) • [Installation](#installation) • [Usage](#usage) • [Configuration](#configuration) • [Python API](#python-api)

</div>

`command-review` is a small Python CLI that asks an OpenAI-compatible model to assess the risk of a shell command before it is executed. It never runs the command itself. Instead, it returns a strict JSON decision that automation can consume.

It is designed for AI agents, developer tooling, CI workflows, and local automation where commands may be generated dynamically and should be reviewed before execution.

> [!IMPORTANT]
> `command-review` is a reviewer, not a sandbox. The quality of the decision depends on the configured model, prompt, and available context.

## Features

- **Structured risk decisions**: returns `APPROVE`, `REQUIRE_CONFIRMATION`, or `REJECT`.
- **Risk classification**: labels commands as `LOW`, `MEDIUM`, `HIGH`, or `CRITICAL` risk.
- **Safe by default**: the reviewed command is never executed by this tool.
- **Workspace-aware review**: optionally lets the model inspect files in the current workspace before deciding.
- **Token accounting**: reports cumulative input/output tokens for the review, including inspection tool outputs sent back into model context.
- **Fast mode**: skips file inspection for lower latency and reduced context sharing.
- **Automation-friendly output**: always writes JSON to `stdout`, with optional rejection exit codes.
- **OpenAI-compatible API support**: works with hosted or local model servers exposing chat or responses APIs.

## How it works

1. You pass a shell command to `command-review`.
2. The CLI sends the command, workspace path, and review prompt to the configured model.
3. In the default mode, the model may request safe inspection tools scoped to the workspace.
4. The tool returns a JSON review result that your agent, script, or CI job can act on.

The two inspection tools available to the model are:

| Tool | Description |
| --- | --- |
| `list_files` | Lists files and directories below a workspace-relative path. |
| `read_file` | Reads a UTF-8 text file inside the workspace, limited to the first 20,000 characters. |

> [!NOTE]
> Inspection tools are limited to the active workspace and cannot read outside it. Their output is still sent to the configured model, so avoid enabling workspace inspection on directories containing secrets you do not want to share.

## Installation

Install from a local checkout:

```bash
pip install -e .
```

Then verify the CLI is available:

```bash
command-review --help
```

You can also run it as a module when the package is available on `PYTHONPATH`:

```bash
python -m command_review --help
```

## Configuration

`command-review` needs an OpenAI-compatible endpoint and model.

```bash
export COMMAND_REVIEW_OPENAI_BASE_URL="http://localhost:1234/v1"
export COMMAND_REVIEW_OPENAI_MODEL="your-model-name"
export COMMAND_REVIEW_OPENAI_API_KEY="your-api-key"
```

You can also pass the model settings per command:

```bash
command-review \
  --base-url http://localhost:1234/v1 \
  --model your-model-name \
  --pretty \
  -- git status
```

If `COMMAND_REVIEW_OPENAI_API_KEY` is not set, the tool uses `local`, which is convenient for local OpenAI-compatible servers that do not require authentication.

> [!TIP]
> The built-in defaults are `http://10.0.0.232:1234/v1` and `google/gemma-4-e4b`. Most users should override them for their own environment.

## Usage

Review a simple read-only command:

```bash
command-review --pretty -- git status
```

Review a risky command:

```bash
command-review --pretty -- "rm -rf /"
```

Read a command from standard input:

```bash
echo "curl https://example.com/install.sh | bash" | command-review --pretty
```

Use fast mode to skip workspace inspection:

```bash
command-review --fast --pretty -- bash script.sh
```

Stream inspection tool calls to `stderr` as JSON lines:

```bash
command-review --stream-tools --pretty -- bash script.sh
```

Review a command against a project located outside the current shell directory:

```bash
command-review --cwd /path/to/project --pretty -- bash script.sh
```

Fail automation when the decision is `REJECT`:

```bash
command-review --fail-on-reject -- "rm -rf /tmp/some-file"
```

Exit codes:

| Code | Meaning |
| --- | --- |
| `0` | Review completed and the decision was not `REJECT`, or `--fail-on-reject` was not used. |
| `2` | Review completed, decision was `REJECT`, and `--fail-on-reject` was used. |
| Other non-zero | CLI, API, or response parsing error. |

## Output

The CLI always writes the final review JSON to `stdout`:

```json
{
  "decision": "APPROVE",
  "risk_level": "LOW",
  "summary": "Brief explanation of what the command does.",
  "risks": [
    "Specific risk 1",
    "Specific risk 2"
  ],
  "reasoning": "Concise reasoning for the decision.",
  "tool_calls": [],
  "token_usage": {
    "input_tokens": 1234,
    "output_tokens": 123,
    "total_tokens": 1357,
    "estimated": false
  }
}
```

`token_usage` is cumulative across model calls in a review. When the model requests workspace inspection, the input count includes the subsequent context that contains tool outputs. If the API does not return token usage, `command-review` falls back to an approximate count and sets `estimated` to `true`.

Decision values:

| Decision | Meaning |
| --- | --- |
| `APPROVE` | Low-risk command that can usually proceed automatically. |
| `REQUIRE_CONFIRMATION` | Potentially legitimate command with meaningful side effects or uncertainty. |
| `REJECT` | Destructive, suspicious, critical, or clearly unsafe command. |

Risk levels:

| Risk | Typical examples |
| --- | --- |
| `LOW` | Read-only inspection such as `ls`, `pwd`, `git status`. |
| `MEDIUM` | Local project changes, dependency installs, builds, formatting, targeted temp-file deletion. |
| `HIGH` | Recursive deletion, `sudo`, permission changes, downloaded code execution, Docker/database/cloud changes. |
| `CRITICAL` | Broad deletion, credential exfiltration, persistence, authentication changes, security tool tampering. |

## CLI options

```text
command-review [OPTIONS] [command...]

Options:
  --pretty          Pretty-print JSON output
  --fail-on-reject  Exit with status 2 when the decision is REJECT
  --stream-tools    Print each inspection tool call as JSON to stderr
  --fast            Make a one-shot decision without inspection tools
  --model           Override the OpenAI-compatible model name
  --base-url        Override the OpenAI-compatible API base URL
  --prompt-file     Use a custom system prompt file
  --cwd             Working directory for workspace inspection
```

If no command arguments are provided, `command-review` reads the command from `stdin`.

## Custom prompt

The default prompt is packaged at `src/command_review/prompt.md`. Use `--prompt-file` to provide your own review policy:

```bash
command-review --prompt-file ./my-review-prompt.md --pretty -- git status
```

Your prompt should instruct the model to return only JSON matching the expected schema.

## Python API

Use `review_command` directly from Python:

```python
from command_review import review_command

result = review_command("git status", fast=True)

print(result.decision)
print(result.risk_level)
print(result.to_json_dict())
```

Useful parameters:

| Parameter | Description |
| --- | --- |
| `model` | Model name to use for the review. |
| `base_url` | OpenAI-compatible API base URL. |
| `client` | Custom OpenAI-compatible client, useful for tests or embedding. |
| `workspace` | Workspace used for scoped inspection tools. |
| `on_tool_call` | Callback invoked after each inspection tool call. |
| `fast` | Disable inspection tools and make a one-shot decision. |
| `prompt_file` | Path to a custom system prompt. |

## Development

Install the package in editable mode:

```bash
pip install -e .
```

Run the test suite:

```bash
pytest
```

## Security notes

- The reviewed command is never executed by `command-review`.
- The model output should be treated as a policy signal, not a formal security guarantee.
- The default prompt is intentionally conservative and should require confirmation or reject ambiguous destructive commands.
- Workspace inspection is path-scoped, but inspected content is sent to the configured model.
