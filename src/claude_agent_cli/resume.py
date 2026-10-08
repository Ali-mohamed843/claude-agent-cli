"""Resume a recorded session: rebuild the conversation from its JSONL file so the chat can continue.

A recording is not a byte-exact copy of the API conversation, so only what can be replayed safely comes back:
- Only completed turns. Failed and interrupted turns were rolled back when they happened, so they stay out.
- Thinking blocks are dropped. The API needs their signatures to accept them again, and the recording keeps
  only the summary. Thinking from earlier turns is optional, so the conversation stays valid without it.
- `/reset` in the recording clears everything before it, exactly as it did live.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, cast

from anthropic.types.beta import BetaMessageParam

from .cost import Usage
from .recorder import display_name


@dataclass
class Restored:
    messages: list[BetaMessageParam] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    cost: float | None = 0.0  # summed from each turn, at the price of the model it ran on
    turns: int = 0  # completed turns in the restored conversation
    skipped_turns: int = 0  # failed or unfinished turns left out
    name: str = ""
    model: str | None = None
    elapsed_ms: int = 0
    last_question: str | None = None
    last_answer: str | None = None
    unanswered: str | None = None  # the last message, when its turn failed or never finished


def restore(events: list[dict[str, Any]]) -> Restored:
    restored = Restored(name=display_name(events))
    turn: list[dict[str, Any]] | None = None  # messages of the turn in progress
    results: list[dict[str, Any]] = []  # tool results waiting to be sent back as one user message
    broken = False  # the turn has something we can't replay

    def flush_results() -> None:
        if results and turn is not None:
            turn.append({"role": "user", "content": list(results)})
            results.clear()

    for e in events:
        kind = e["type"]
        restored.elapsed_ms = max(restored.elapsed_ms, e.get("elapsed_ms", 0))
        if kind in {"session_start", "session_resume", "model_change"} and e.get("model"):
            restored.model = e["model"]
        elif kind == "reset":
            restored.messages, restored.turns, restored.unanswered = [], 0, None
            turn = None
        elif kind == "user_message":
            if turn is not None:
                restored.skipped_turns += 1  # previous turn never ended: crashed or window closed
            turn, broken = [{"role": "user", "content": e.get("text", "")}], False
            restored.unanswered = str(e.get("text", ""))  # until this turn completes
            results.clear()
        elif kind == "model_response" and turn is not None:
            flush_results()
            content = [replayable(block) for block in e.get("content", [])]
            content = [block for block in content if block is not None]
            if content:
                turn.append({"role": "assistant", "content": content})
            else:
                broken = True  # e.g. a response with only thinking: an empty assistant message is invalid
        elif kind == "tool_result" and turn is not None:
            result = {"type": "tool_result", "tool_use_id": e.get("id", ""), "content": e.get("content", "")}
            if e.get("is_error"):
                result["is_error"] = True
            results.append(result)
        elif kind == "turn_end" and turn is not None:
            results.clear()
            usage = e.get("usage", {})
            restored.usage += Usage(
                usage.get("input_tokens", 0),
                usage.get("output_tokens", 0),
                usage.get("cache_read_tokens", 0),
                usage.get("cache_write_tokens", 0),
            )
            cost = e.get("cost_usd")
            restored.cost = None if cost is None or restored.cost is None else restored.cost + cost
            if e.get("status") == "ok" and not broken and turn[-1]["role"] == "assistant":
                restored.messages.extend(cast(list[BetaMessageParam], turn))
                restored.turns += 1
                restored.last_question = str(turn[0]["content"])
                restored.last_answer = final_text(turn[-1])
                restored.unanswered = None
            else:
                restored.skipped_turns += 1
            turn = None
    if turn is not None:
        restored.skipped_turns += 1
    return restored


def replayable(block: dict[str, Any]) -> dict[str, Any] | None:
    if block.get("type") == "text" and block.get("text"):
        return {"type": "text", "text": block["text"]}
    if block.get("type") == "tool_use":
        return {"type": "tool_use", "id": block["id"], "name": block["name"], "input": block.get("input", {})}
    return None  # thinking (no signature), fallback notices, unknown blocks


def final_text(message: dict[str, Any]) -> str | None:
    content = message["content"]
    if isinstance(content, str):
        return content
    texts = [b["text"] for b in content if isinstance(b, dict) and b.get("type") == "text"]
    return "\n".join(texts) or None
