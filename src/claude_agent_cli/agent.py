"""The agent loop: send the conversation to Claude, run the tools it asks for, repeat until it answers."""

from __future__ import annotations

import json
import platform
import sys
import time
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

import anthropic
from anthropic.types.beta import BetaContentBlock, BetaMessage, BetaMessageParam, BetaToolResultBlockParam

from .cost import Usage
from .recorder import Recorder, content_summary
from .tools import TOOL_DEFINITIONS, ToolBox, ToolError, truncate

DEFAULT_MODEL = "claude-opus-5-5"
MAX_TOKENS = 64_000
OTHER_MODEL_MAX_TOKENS = 16_000  # most non-Claude models cap output lower than Claude does
MAX_JSON_RETRIES = 2

# Models that accept server-side refusal fallbacks with `fallbacks: "default"`.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1"}
FALLBACK_BETA = "server-side-fallback-2026-07-01"

DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RESET = "\033[0m"


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

    def reset(self) -> None:
        """Start a new conversation."""
        self.messages = []
        self._record("reset")

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
        self.messages.append({"role": "user", "content": user_text})
        self._record("user_message", text=user_text)
        try:
            while True:
                response = self._stream_response()
                usage = Usage.from_api(response.usage)
                turn_usage += usage
                self.session_usage += usage
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
                self.write("\n")
                return message
            except ValueError:
                # The SDK could not parse a streamed tool input at all. No tool_use block was
                # completed, so there is nothing to answer: re-issue the same request.
                if attempt == MAX_JSON_RETRIES:
                    raise
                self.write(f"\n{YELLOW}[malformed tool input, retrying]{RESET}\n")
        raise AssertionError("unreachable")

    def _render(self, event: Any) -> None:
        """Print stream events as they arrive: thinking dimmed, text plain, tool calls in cyan."""
        if event.type == "content_block_start":
            block = event.content_block
            self._current_block = block.type
            if block.type == "thinking":
                self.write(f"{DIM}[thinking] ")
            elif block.type == "tool_use":
                self.write(f"\n{CYAN}→ {block.name}{RESET} ")
            elif block.type == "fallback":
                self.write(f"\n{YELLOW}[{block.from_.model} declined; continuing on {block.to.model}]{RESET}\n")
        elif event.type == "content_block_delta":
            if event.delta.type == "thinking_delta":
                self.write(event.delta.thinking)
            elif event.delta.type == "text_delta":
                self.write(event.delta.text)
        elif event.type == "content_block_stop":
            self.write(f"{RESET}\n" if self._current_block == "thinking" else RESET)

    def _run_tool(self, block: BetaContentBlock, truncated: bool) -> BetaToolResultBlockParam:
        assert block.type == "tool_use"
        self.write(f"{DIM}{json.dumps(block.input, ensure_ascii=False)}{RESET}\n")
        started = time.monotonic()
        result = self._tool_result(block, truncated)
        self._record(
            "tool_result",
            id=block.id,
            name=block.name,
            is_error=result.get("is_error", False),
            content=result.get("content"),
            duration_ms=round((time.monotonic() - started) * 1000),
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
            self.write(f"{YELLOW}  ✗ {e}{RESET}\n")
            return _error_result(block.id, str(e))
        self.write(f"{DIM}  ✓ {len(output):,} characters{RESET}\n")
        return {"type": "tool_result", "tool_use_id": block.id, "content": truncate(output)}


def _error_result(tool_use_id: str, message: str) -> BetaToolResultBlockParam:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": message, "is_error": True}
