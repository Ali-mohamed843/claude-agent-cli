"""The agent loop: send the conversation to Claude, run the tools it asks for, repeat until it answers."""

from __future__ import annotations

import json
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any, cast

import anthropic
from anthropic.types.beta import BetaContentBlock, BetaMessage, BetaMessageParam, BetaToolResultBlockParam

from . import style
from .cost import Usage
from .recorder import Recorder, content_summary
from .streamtext import StreamText
from .style import BRANCH, BULLET, MARK, fmt_seconds, paint, term_width
from .tools import TOOL_DEFINITIONS, ToolBox, ToolError, truncate

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 64_000
OTHER_MODEL_MAX_TOKENS = 16_000  # most non-Claude models cap output lower than Claude does
MAX_JSON_RETRIES = 2

# Models that accept server-side refusal fallbacks with `fallbacks: "default"`.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

INDENT = "  "  # Claude's text and thinking line up under the "● " that starts them


class TurnFailed(Exception):
    """The turn could not finish (refusal, truncation). The conversation was rolled back."""


def build_system_prompt(toolbox: ToolBox) -> str:
    # Keep this byte-identical for the whole session: any change invalidates the prompt cache.
    shell = "cmd.exe" if platform.system() == "Windows" else "/bin/sh"
    return (
        "You are a coding assistant running in the user's terminal.\n"
        f"Project root: {toolbox.root}\n"
        f"Operating system: {platform.system()}; run_command uses {shell}.\n"
        "Read files and search before answering questions about the code; do not guess file contents. "
        "Every run_command call needs the user's approval, so only run commands when reading is not enough. "
        "When the user writes @path, they mean that file in the project: read it. "
        "Keep answers short and concrete."
    )


class Agent:
    def __init__(
        self,
        client: anthropic.Anthropic,
        toolbox: ToolBox,
        model: str = DEFAULT_MODEL,
        effort: str = "high",
        write: Callable[[str], object] = sys.stdout.write,
        recorder: Recorder | None = None,
    ) -> None:
        self.client = client
        self.toolbox = toolbox
        self.model = model
        self.effort = effort
        self.write = write
        self.recorder = recorder
        self.system = build_system_prompt(toolbox)
        self.messages: list[BetaMessageParam] = []
        self.session_usage = Usage()
        self._current_block = ""
        self._last_output = ""  # what was printed last: "text", "thinking" or "tool"; spacing depends on it
        self._text: StreamText | None = None  # formats the text or thinking block being streamed
        self.session_cost: float | None = 0.0  # summed per response, at the price of the model that answered

    def reset(self) -> None:
        """Start a new conversation."""
        self.messages = []
        self._record("reset")

    def switch_model(self, model: str) -> None:
        """Continue the conversation on another model.

        Thinking blocks are signed for the model that wrote them, and other providers' models don't accept
        Claude-only blocks, so earlier turns are reduced to their text, tool calls and tool results. Thinking
        from earlier turns is optional for the API, so the conversation stays valid.
        """
        if model == self.model:
            return
        self.messages = [_portable(message) for message in self.messages]
        self.model = model
        self._record("model_change", model=model)

    def _record(self, type: str, **fields: Any) -> None:
        if self.recorder:
            self.recorder.event(type, **fields)

    def send(self, user_text: str) -> Usage:
        """Run one user turn to completion. Returns the tokens it used.

        The history is append-only: we never edit earlier messages, which keeps the prompt cache
        and the model's earlier thinking valid. If the turn fails, we drop everything it added.
        """
        start = len(self.messages)
        turn_usage = Usage()
        self._last_output = ""
        self.messages.append({"role": "user", "content": user_text})
        self._record("user_message", text=user_text)
        try:
            while True:
                response = self._stream_response()
                usage = Usage.from_api(response.usage)
                turn_usage += usage
                self.session_usage += usage
                cost = usage.cost(self.model)
                self.session_cost = None if cost is None or self.session_cost is None else self.session_cost + cost
                self.messages.append({"role": "assistant", "content": response.content})
                self._record(
                    "model_response",
                    stop_reason=response.stop_reason,
                    usage=asdict(usage),
                    content=[content_summary(b) for b in response.content],
                )

                if response.stop_reason == "refusal":
                    category = response.stop_details.category if response.stop_details else None
                    raise TurnFailed(f"Claude declined this request (category: {category}).")
                if response.stop_reason == "pause_turn":
                    continue

                tool_uses = [b for b in response.content if b.type == "tool_use"]
                if not tool_uses:
                    self._record_turn_end(turn_usage, status="ok")
                    return turn_usage  # end_turn: Claude answered

                truncated = response.stop_reason == "max_tokens"
                results = [self._run_tool(block, truncated) for block in tool_uses]
                # All results go back in ONE user message, matched to their tool_use ids.
                self.messages.append({"role": "user", "content": results})
        except BaseException as e:
            del self.messages[start:]  # covers TurnFailed, API errors and Ctrl+C
            # The receipt keeps failed turns: what was tried matters even when it was rolled back.
            self._record_turn_end(turn_usage, status="failed", error=type(e).__name__, message=str(e))
            raise

    def _record_turn_end(self, turn_usage: Usage, **fields: Any) -> None:
        self._record("turn_end", usage=asdict(turn_usage), cost_usd=turn_usage.cost(self.model), **fields)

    @property
    def is_claude(self) -> bool:
        return self.model.startswith("claude-")

    def _request_params(self) -> dict[str, Any]:
        if not self.is_claude:
            # Another provider's model behind an Anthropic-compatible endpoint (e.g. OpenRouter).
            # Send only the core Messages API fields; the Claude-specific ones may be rejected.
            tools = [{k: v for k, v in t.items() if k != "eager_input_streaming"} for t in TOOL_DEFINITIONS]
            return {
                "model": self.model,
                "max_tokens": OTHER_MODEL_MAX_TOKENS,
                "system": self.system,
                "tools": tools,
                "messages": self.messages,
            }

        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": self.system,
            "tools": TOOL_DEFINITIONS,
            "messages": self.messages,
            "thinking": {"type": "adaptive", "display": "summarized"},
            "output_config": {"effort": self.effort},
            # Automatic prompt caching: the cache breakpoint follows the end of the conversation.
            "cache_control": {"type": "ephemeral"},
        }
        if self.model in FALLBACK_MODELS:
            # If a safety classifier declines, the API retries on a recommended fallback model.
            params["betas"] = [FALLBACK_BETA]
            params["fallbacks"] = "default"
        return params

    def _stream_response(self) -> BetaMessage:
        """Stream one model response to the terminal and return the complete message."""
        for attempt in range(MAX_JSON_RETRIES + 1):
            try:
                with self.client.beta.messages.stream(**self._request_params()) as stream:
                    for event in stream:
                        self._render(event)
                    message = stream.get_final_message()
                return message
            except ValueError:
                # The SDK could not parse a streamed tool input at all. No tool_use block was
                # completed, so there is nothing to answer: re-issue the same request.
                if attempt == MAX_JSON_RETRIES:
                    raise
                self.write(
                    style.end() + "\n" + paint("  ! A tool call arrived garbled; asking again.", "yellow") + "\n"
                )
        raise AssertionError("unreachable")

    def _render(self, event: Any) -> None:
        """Print stream events as they arrive.

        Claude's text starts with an orange "●", wraps at the terminal width and stays indented under it, with
        light Markdown styling; thinking is gray and italic. Tool calls are printed by `_run_tool` once their
        input is complete.
        """
        if event.type == "content_block_start":
            block = event.content_block
            self._current_block = block.type
            width = term_width() - 1
            if block.type == "thinking":
                self.write("\n" + paint(f"{MARK} Thinking", "gray", "italic") + "\n" + INDENT)
                self._text = StreamText(self.write, width, INDENT, base=("gray", "italic"))
            elif block.type == "text":
                self.write("\n" + paint(BULLET, "accent") + " ")
                self._text = StreamText(self.write, width, INDENT)
            elif block.type == "fallback":
                note = f"{block.from_.model} declined this request; continuing on {block.to.model}"
                self.write("\n" + paint(f"  ! {note}", "yellow") + "\n")
        elif event.type == "content_block_delta" and self._text is not None:
            if event.delta.type == "thinking_delta":
                self._text.feed(event.delta.thinking)
            elif event.delta.type == "text_delta":
                self._text.feed(event.delta.text)
        elif event.type == "content_block_stop" and self._text is not None:
            self._text.finish()
            self._text = None
            self.write("\n")
            self._last_output = self._current_block

    def _run_tool(self, block: BetaContentBlock, truncated: bool) -> BetaToolResultBlockParam:
        """Run one tool call, showing it as an action and its outcome:

        ● Read src/app.py
          └ 42 lines · 3 ms
        """
        assert block.type == "tool_use"
        verb, target = describe_tool(block.name, block.input)
        if self._last_output != "tool":
            self.write("\n")
        self.write(f"{paint(BULLET, 'accent')} {paint(verb, 'bold')} {target}\n")
        started = time.monotonic()
        result = self._tool_result(block, truncated)
        seconds = time.monotonic() - started
        outcome, ok = describe_result(block.name, result)
        took = paint(f" · {fmt_seconds(seconds)}", "gray") if ok and seconds >= 0.5 else ""  # only when it's news
        self.write(f"  {paint(BRANCH, 'gray')} {paint(outcome, 'gray' if ok else 'red')}{took}\n")
        self._last_output = "tool"
        self._record(
            "tool_result",
            id=block.id,
            name=block.name,
            is_error=result.get("is_error", False),
            content=result.get("content"),
            duration_ms=round(seconds * 1000),
        )
        return result

    def _tool_result(self, block: BetaContentBlock, truncated: bool) -> BetaToolResultBlockParam:
        assert block.type == "tool_use"
        if truncated:
            # max_tokens cut the tool input off. It may still parse, but it is incomplete.
            return _error_result(block.id, "Your tool input was cut off by max_tokens. Send a shorter input.")
        try:
            output = self.toolbox.run(block.name, block.input)
        except ToolError as e:
            return _error_result(block.id, str(e))
        return {"type": "tool_result", "tool_use_id": block.id, "content": truncate(output)}


def _error_result(tool_use_id: str, message: str) -> BetaToolResultBlockParam:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": message, "is_error": True}


def describe_tool(name: str, args: object) -> tuple[str, str]:
    """A tool call as an action: ("Read", "src/app.py")."""
    args = args if isinstance(args, dict) else {}
    if name == "read_file":
        return "Read", str(args.get("path", ""))
    if name == "search_files":
        where = f" {paint('in', 'gray')} {args['glob']}" if args.get("glob") else ""
        return "Search", f"“{args.get('pattern', '')}”{where}"
    if name == "run_command":
        return "Run", str(args.get("command", ""))
    return name, json.dumps(args, ensure_ascii=False)


def describe_result(name: str, result: BetaToolResultBlockParam) -> tuple[str, bool]:
    """A short outcome for the line under a tool call, and whether it went well."""
    content = result.get("content")
    text = content if isinstance(content, str) else ""
    if result.get("is_error"):
        if text == "The user declined to run this command.":
            return "Not run: you declined", False
        return f"✗ {text.splitlines()[0] if text else 'failed'}", False
    if name == "read_file":
        lines = text.count("\n") + 1 if text else 0
        return f"{lines:,} line{'s' if lines != 1 else ''}", True
    if name == "search_files":
        if text == "No matches.":
            return "no matches", True
        matches = sum(1 for line in text.splitlines() if not line.startswith("[stopped"))
        return f"{matches:,} match{'es' if matches != 1 else ''}", True
    if name == "run_command":
        first, _, rest = text.partition("\n")
        code = first.removeprefix("exit code: ")
        lines = rest.count("\n") + 1 if rest.strip() else 0
        output = f"{lines:,} line{'s' if lines != 1 else ''} of output" if lines else "no output"
        return f"exit {code} · {output}", code == "0"
    return "done", True


def _portable(message: BetaMessageParam) -> BetaMessageParam:
    """A message with only plain text, tool_use and tool_result blocks, as dicts any model accepts."""
    content = message["content"]
    if isinstance(content, str):
        return message
    blocks: list[dict[str, Any]] = []
    for block in cast(list[Any], content):
        if isinstance(block, dict):
            data: dict[str, Any] = block
        else:  # an SDK content block from a live response
            data = block.model_dump() if hasattr(block, "model_dump") else dict(vars(block))
        kind = data.get("type")
        if kind == "text":
            blocks.append({"type": "text", "text": data.get("text", "")})
        elif kind == "tool_use":
            blocks.append({"type": "tool_use", "id": data["id"], "name": data["name"], "input": data.get("input", {})})
        elif kind == "tool_result":
            blocks.append({k: v for k, v in data.items() if k in {"type", "tool_use_id", "content", "is_error"}})
    if not blocks:  # e.g. a response that was only thinking
        blocks.append({"type": "text", "text": "(no text)"})
    return cast(BetaMessageParam, {"role": message["role"], "content": blocks})
