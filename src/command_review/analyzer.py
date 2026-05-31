from __future__ import annotations

from dataclasses import asdict, dataclass, field
from importlib.resources import files
import json
import os
from pathlib import Path
import re
from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from openai import OpenAI


DECISION_APPROVE = "APPROVE"
DECISION_REQUIRE_CONFIRMATION = "REQUIRE_CONFIRMATION"
DECISION_REJECT = "REJECT"

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"
RISK_CRITICAL = "CRITICAL"

DEFAULT_MODEL = "google/gemma-4-e4b"
DEFAULT_BASE_URL = "http://10.0.0.232:1234/v1"
DEFAULT_PROMPT_FILE = "prompt.md"
FAST_PROMPT_SUFFIX = (
    "\n\nFast mode: make a one-shot decision using only the command text and "
    "workspace path. Do not request or call tools."
)
REVIEW_RESPONSE_RETRY_PROMPT = (
    "Your previous response could not be parsed as a command review result: {error}\n\n"
    "Return only a valid JSON object matching the required schema. "
    "The `risks` field must always be an array of strings, even when empty. "
    "Do not include markdown, prose, or tool calls."
)

REVIEW_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {
            "type": "string",
            "enum": [DECISION_APPROVE, DECISION_REQUIRE_CONFIRMATION, DECISION_REJECT],
        },
        "risk_level": {
            "type": "string",
            "enum": [RISK_LOW, RISK_MEDIUM, RISK_HIGH, RISK_CRITICAL],
        },
        "summary": {"type": "string"},
        "risks": {"type": "array", "items": {"type": "string"}},
        "reasoning": {"type": "string"},
    },
    "required": [
        "decision",
        "risk_level",
        "summary",
        "risks",
        "reasoning",
    ],
    "additionalProperties": False,
}


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    estimated: bool = False


@dataclass(frozen=True)
class ReviewResult:
    decision: str
    risk_level: str
    summary: str
    risks: list[str]
    reasoning: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    token_usage: TokenUsage = field(default_factory=TokenUsage)

    def to_json_dict(self) -> dict[str, object]:
        return asdict(self)


class ReviewState(TypedDict):
    messages: list[dict[str, Any]]


TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories under a workspace-relative path.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative directory path. Defaults to the workspace root.",
                    }
                },
                "additionalProperties": False,
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a workspace-relative text file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Workspace-relative file path to read.",
                    }
                },
                "required": ["path"],
                "additionalProperties": False,
            },
        },
    },
]


def review_command(
    command: str,
    *,
    model: str | None = None,
    base_url: str | None = None,
    client: Any | None = None,
    workspace: str | os.PathLike[str] | None = None,
    on_tool_call: Any | None = None,
    fast: bool = False,
    prompt_file: str | os.PathLike[str] | None = None,
) -> ReviewResult:
    client = client or OpenAI(
        base_url=base_url
        or os.environ.get("COMMAND_REVIEW_OPENAI_BASE_URL", DEFAULT_BASE_URL),
        api_key=os.environ.get("COMMAND_REVIEW_OPENAI_API_KEY", "local"),
    )
    model = model or os.environ.get("COMMAND_REVIEW_OPENAI_MODEL", DEFAULT_MODEL)
    workspace_path = Path(workspace or os.getcwd()).resolve()
    prompt = _load_review_prompt(prompt_file)
    if fast:
        prompt += FAST_PROMPT_SUFFIX
    messages = [
        {"role": "system", "content": prompt},
        {
            "role": "user",
            "content": (
                f"Workspace: {workspace_path}\n\n"
                f"Analyze this shell command:\n\n{command}"
            ),
        },
    ]

    if fast:
        response = _create_model_response(client, model, messages, use_tools=False)
        token_usage = _response_token_usage(response, messages, use_tools=False)
        try:
            result = _parse_review_response(response)
        except ValueError as exc:
            repair_messages = [
                *messages,
                _response_message(response),
                _review_response_retry_message(exc),
            ]
            response = _create_model_response(
                client, model, repair_messages, use_tools=False
            )
            token_usage = _combine_token_usage(
                token_usage,
                _response_token_usage(response, repair_messages, use_tools=False),
            )
            result = _parse_review_response(response)
        return ReviewResult(
            decision=result.decision,
            risk_level=result.risk_level,
            summary=result.summary,
            risks=result.risks,
            reasoning=result.reasoning,
            token_usage=token_usage,
        )

    token_usage = [TokenUsage()]
    app = _build_review_graph(client, model, workspace_path, token_usage, on_tool_call)
    state = app.invoke({"messages": messages})
    response_messages = state["messages"]

    try:
        result = _parse_review_response(response_messages[-1].get("content", ""))
    except ValueError as exc:
        repair_messages = [*response_messages, _review_response_retry_message(exc)]
        response = _create_model_response(
            client, model, repair_messages, use_tools=False
        )
        token_usage[0] = _combine_token_usage(
            token_usage[0],
            _response_token_usage(response, repair_messages, use_tools=False),
        )
        response_messages = [*repair_messages, _response_message(response)]
        result = _parse_review_response(response)
    return ReviewResult(
        decision=result.decision,
        risk_level=result.risk_level,
        summary=result.summary,
        risks=result.risks,
        reasoning=result.reasoning,
        tool_calls=_collect_tool_calls(response_messages),
        token_usage=token_usage[0],
    )


def _build_review_graph(
    client: Any,
    model: str,
    workspace: Path,
    token_usage: list[TokenUsage],
    on_tool_call: Any | None = None,
):
    graph = StateGraph(ReviewState)

    def call_model(state: ReviewState) -> ReviewState:
        response = _create_model_response(client, model, state["messages"])
        token_usage[0] = _combine_token_usage(
            token_usage[0],
            _response_token_usage(response, state["messages"], use_tools=True),
        )
        return {"messages": [*state["messages"], _response_message(response)]}

    def run_tools(state: ReviewState) -> ReviewState:
        message = state["messages"][-1]
        tool_messages = []
        for tool_call in message.get("tool_calls", []) or []:
            tool_message = _run_tool_call(tool_call, workspace)
            tool_messages.append(tool_message)
            if on_tool_call is not None:
                on_tool_call(_format_tool_call(tool_call, tool_message))
        return {"messages": [*state["messages"], *tool_messages]}

    graph.add_node("agent", call_model)
    graph.add_node("tools", run_tools)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", _next_step, {"tools": "tools", "end": END})
    graph.add_edge("tools", "agent")

    return graph.compile()


def _load_review_prompt(prompt_file: str | os.PathLike[str] | None = None) -> str:
    if prompt_file is not None:
        return Path(prompt_file).read_text(encoding="utf-8").strip()
    return (
        files("command_review")
        .joinpath(DEFAULT_PROMPT_FILE)
        .read_text(encoding="utf-8")
        .strip()
    )


def _create_model_response(
    client: Any, model: str, messages: list[dict[str, Any]], *, use_tools: bool = True
) -> Any:
    if hasattr(client, "chat"):
        kwargs = {
            "model": model,
            "messages": messages,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "command_review_result",
                    "schema": REVIEW_JSON_SCHEMA,
                    "strict": True,
                },
            },
        }
        if use_tools:
            kwargs["tools"] = TOOLS
            kwargs["tool_choice"] = "auto"
        try:
            return client.chat.completions.create(**kwargs)
        except Exception as exc:
            if not _is_structured_tooling_error(exc):
                raise
            kwargs.pop("response_format")
            return client.chat.completions.create(**kwargs)

    if hasattr(client, "responses"):
        return client.responses.create(
            model=model,
            input=messages,
            text={
                "format": {
                    "type": "json_schema",
                    "name": "command_review_result",
                    "schema": REVIEW_JSON_SCHEMA,
                    "strict": True,
                }
            },
        )

    raise TypeError("OpenAI-compatible client must expose chat or responses API.")


def _response_token_usage(
    response: Any, messages: list[dict[str, Any]], *, use_tools: bool
) -> TokenUsage:
    usage = getattr(response, "usage", None)
    if usage is not None:
        input_tokens = _usage_value(usage, "prompt_tokens", "input_tokens")
        output_tokens = _usage_value(usage, "completion_tokens", "output_tokens")
        total_tokens = _usage_value(usage, "total_tokens")
        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            total_tokens=total_tokens or input_tokens + output_tokens,
            estimated=False,
        )

    input_payload: dict[str, Any] = {"messages": messages}
    if use_tools:
        input_payload["tools"] = TOOLS
    input_tokens = _estimate_token_count(input_payload)
    output_tokens = _estimate_token_count(_response_payload(response))
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
        estimated=True,
    )


def _combine_token_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    input_tokens = left.input_tokens + right.input_tokens
    output_tokens = left.output_tokens + right.output_tokens
    return TokenUsage(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
        estimated=left.estimated or right.estimated,
    )


def _usage_value(usage: Any, *names: str) -> int:
    for name in names:
        value = (
            usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        )
        if isinstance(value, int):
            return value
    return 0


def _response_payload(response: Any) -> Any:
    choices = getattr(response, "choices", None)
    if isinstance(choices, list) and choices:
        return _response_message(response)

    return _response_text(response)


def _estimate_token_count(value: Any) -> int:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return len(re.findall(r"\w+|[^\w\s]", text, flags=re.UNICODE))


def _is_structured_tooling_error(exc: Exception) -> bool:
    status_code = getattr(exc, "status_code", None)
    message = str(exc).lower()
    return status_code == 400 and (
        "structured output" in message or "lazy grammar" in message
    )


def _next_step(state: ReviewState) -> Literal["tools", "end"]:
    message = state["messages"][-1]
    return "tools" if message.get("tool_calls") else "end"


def _response_message(response: Any) -> dict[str, Any]:
    choices = getattr(response, "choices", None)
    if isinstance(choices, list) and choices:
        message = getattr(choices[0], "message", None)
        content = getattr(message, "content", None) or ""
        tool_calls = getattr(message, "tool_calls", None)
        result: dict[str, Any] = {"role": "assistant", "content": content}
        if tool_calls:
            result["tool_calls"] = [
                _serialize_tool_call(tool_call) for tool_call in tool_calls
            ]
        return result

    return {"role": "assistant", "content": _response_text(response)}


def _review_response_retry_message(exc: ValueError) -> dict[str, str]:
    return {
        "role": "user",
        "content": REVIEW_RESPONSE_RETRY_PROMPT.format(error=str(exc)),
    }


def _serialize_tool_call(tool_call: Any) -> dict[str, Any]:
    function = getattr(tool_call, "function", None)
    return {
        "id": getattr(tool_call, "id", ""),
        "type": getattr(tool_call, "type", "function"),
        "function": {
            "name": getattr(function, "name", ""),
            "arguments": getattr(function, "arguments", "{}"),
        },
    }


def _run_tool_call(tool_call: dict[str, Any], workspace: Path) -> dict[str, Any]:
    function = tool_call.get("function", {})
    name = function.get("name")
    try:
        arguments = json.loads(function.get("arguments") or "{}")
    except json.JSONDecodeError:
        arguments = {}

    try:
        if name == "list_files":
            content = _tool_list_files(workspace, arguments.get("path", "."))
        elif name == "read_file":
            content = _tool_read_file(workspace, arguments.get("path", ""))
        else:
            content = f"Unknown tool: {name}"
    except ValueError as exc:
        content = str(exc)

    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", ""),
        "name": name,
        "content": content,
    }


def _collect_tool_calls(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tool_outputs = {
        message.get("tool_call_id", ""): message.get("content", "")
        for message in messages
        if message.get("role") == "tool"
    }
    calls = []
    for message in messages:
        for tool_call in message.get("tool_calls", []) or []:
            tool_call_id = tool_call.get("id", "")
            calls.append(
                _format_tool_call(tool_call, tool_outputs.get(tool_call_id, ""))
            )
    return calls


def _format_tool_call(
    tool_call: dict[str, Any], tool_message: dict[str, Any] | str
) -> dict[str, Any]:
    function = tool_call.get("function", {})
    raw_arguments = function.get("arguments") or "{}"
    try:
        arguments: Any = json.loads(raw_arguments)
    except json.JSONDecodeError:
        arguments = raw_arguments

    output = (
        tool_message.get("content", "")
        if isinstance(tool_message, dict)
        else tool_message
    )
    return {
        "id": tool_call.get("id", ""),
        "name": function.get("name", ""),
        "arguments": arguments,
        "output": output,
    }


def _tool_list_files(workspace: Path, path: str) -> str:
    target = _resolve_workspace_path(workspace, path or ".")
    if not target.exists():
        return f"Path does not exist: {path}"
    if not target.is_dir():
        return f"Path is not a directory: {path}"

    entries = []
    for child in sorted(target.iterdir(), key=lambda item: item.name)[:200]:
        suffix = "/" if child.is_dir() else ""
        entries.append(f"{child.relative_to(workspace)}{suffix}")
    return "\n".join(entries) if entries else "Directory is empty."


def _tool_read_file(workspace: Path, path: str) -> str:
    target = _resolve_workspace_path(workspace, path)
    if not target.exists():
        return f"File does not exist: {path}"
    if not target.is_file():
        return f"Path is not a file: {path}"

    try:
        return target.read_text(encoding="utf-8")[:20_000]
    except UnicodeDecodeError:
        return f"File is not valid UTF-8 text: {path}"


def _resolve_workspace_path(workspace: Path, path: str) -> Path:
    target = (workspace / path).resolve()
    if target != workspace and workspace not in target.parents:
        raise ValueError(f"Tool path escapes workspace: {path}")
    return target


def _parse_review_response(response: Any) -> ReviewResult:
    content = response if isinstance(response, str) else _response_text(response)
    try:
        payload = json.loads(content)
    except json.JSONDecodeError as exc:
        try:
            payload = json.loads(_extract_json_object(content))
        except ValueError as extract_exc:
            raise ValueError("OpenAI response was not valid JSON.") from extract_exc

    return ReviewResult(
        decision=_expect_str(payload, "decision"),
        risk_level=_expect_str(payload, "risk_level"),
        summary=_expect_str(payload, "summary"),
        risks=_expect_str_list(payload, "risks"),
        reasoning=_expect_str(payload, "reasoning"),
    )


def _response_text(response: Any) -> str:
    choices = getattr(response, "choices", None)
    if isinstance(choices, list):
        for choice in choices:
            message = getattr(choice, "message", None)
            content = getattr(message, "content", None)
            if isinstance(content, str) and content:
                return content

    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text:
        return output_text

    output = getattr(response, "output", None)
    if isinstance(output, list):
        for item in output:
            content = getattr(item, "content", None)
            if isinstance(content, list):
                for part in content:
                    text = getattr(part, "text", None)
                    if isinstance(text, str) and text:
                        return text

    raise ValueError("OpenAI response did not contain text output.")


def _extract_json_object(content: str) -> str:
    start = content.find("{")
    if start == -1:
        raise ValueError("OpenAI response did not contain a JSON object.")

    depth = 0
    in_string = False
    escape = False
    for index, char in enumerate(content[start:], start=start):
        if escape:
            escape = False
            continue
        if char == "\\" and in_string:
            escape = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]

    raise ValueError("OpenAI response JSON object was incomplete.")


def _expect_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise ValueError(f"OpenAI response field `{key}` must be a string.")
    return value


def _expect_str_list(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"OpenAI response field `{key}` must be a list of strings.")
    return value
