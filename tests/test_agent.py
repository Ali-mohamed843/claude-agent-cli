"""Tests for the agent loop. A fake client replays scripted responses, so no API calls are made."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import anthropic
import pytest

from claude_agent_cli.agent import FALLBACK_BETA, Agent, TurnFailed
from claude_agent_cli.tools import ToolBox


def text_block(text: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=text)


def tool_use_block(id: str, name: str, input: Any) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


def message(content: list[SimpleNamespace], stop_reason: str, stop_details: Any = None) -> SimpleNamespace:
    usage = SimpleNamespace(
        input_tokens=100, output_tokens=50, cache_read_input_tokens=0, cache_creation_input_tokens=0
    )
    return SimpleNamespace(content=content, stop_reason=stop_reason, stop_details=stop_details, usage=usage)


class FakeStream:
    def __init__(self, response: SimpleNamespace) -> None:
        self.response = response

    def __enter__(self) -> FakeStream:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def __iter__(self) -> Any:
        for block in self.response.content:
            yield SimpleNamespace(type="content_block_start", content_block=block)
            if block.type == "text":
                yield SimpleNamespace(
                    type="content_block_delta", delta=SimpleNamespace(type="text_delta", text=block.text)
                )
            yield SimpleNamespace(type="content_block_stop")

    def get_final_message(self) -> SimpleNamespace:
        return self.response


class FakeClient:
    """Mimics `client.beta.messages.stream(...)` and records every request."""

    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self.responses = list(responses)
        self.requests: list[dict[str, Any]] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))

    def _stream(self, **params: Any) -> FakeStream:
        # Copy the message list: the agent keeps appending to the same list object.
        self.requests.append({**params, "messages": list(params["messages"])})
        return FakeStream(self.responses.pop(0))


def make_agent(
    tmp_path: Path, responses: list[SimpleNamespace], model: str = "claude-opus-5-5"
) -> tuple[Agent, FakeClient]:
    (tmp_path / "app.py").write_text("print('hi')\n", encoding="utf-8")
    client = FakeClient(responses)
    toolbox = ToolBox(root=tmp_path, confirm=lambda _command: True)
    agent = Agent(cast(anthropic.Anthropic, client), toolbox, model=model, write=lambda _text: None)
    return agent, client


def test_plain_answer(tmp_path: Path) -> None:
    agent, client = make_agent(tmp_path, [message([text_block("Hello!")], "end_turn")])
    usage = agent.send("hi")

    assert len(client.requests) == 1
    assert usage.input_tokens == 100
    assert [m["role"] for m in agent.messages] == ["user", "assistant"]


def test_tool_call_loop(tmp_path: Path) -> None:
    agent, client = make_agent(
        tmp_path,
        [
            message([tool_use_block("tu_1", "read_file", {"path": "app.py"})], "tool_use"),
            message([text_block("It prints hi.")], "end_turn"),
        ],
    )
    usage = agent.send("what does app.py do?")

    assert len(client.requests) == 2
    assert usage.output_tokens == 100  # summed over both requests
    tool_result = client.requests[1]["messages"][-1]["content"][0]
    assert tool_result["tool_use_id"] == "tu_1"
    assert "print('hi')" in tool_result["content"]
    assert "is_error" not in tool_result


def test_parallel_tool_calls_return_in_one_message(tmp_path: Path) -> None:
    agent, client = make_agent(
        tmp_path,
        [
            message(
                [
                    tool_use_block("tu_1", "read_file", {"path": "app.py"}),
                    tool_use_block("tu_2", "search_files", {"pattern": "print"}),
                ],
                "tool_use",
            ),
            message([text_block("Done.")], "end_turn"),
        ],
    )
    agent.send("look around")

    results = client.requests[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["tu_1", "tu_2"]


def test_tool_error_is_sent_back_to_claude(tmp_path: Path) -> None:
    agent, client = make_agent(
        tmp_path,
        [
            message([tool_use_block("tu_1", "read_file", {"path": "missing.py"})], "tool_use"),
            message([text_block("That file does not exist.")], "end_turn"),
        ],
    )
    agent.send("read missing.py")

    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "not found" in result["content"]


def test_truncated_tool_input_is_not_run(tmp_path: Path) -> None:
    agent, client = make_agent(
        tmp_path,
        [
            message([tool_use_block("tu_1", "run_command", {"command": "echo partial"})], "max_tokens"),
            message([text_block("Retrying.")], "end_turn"),
        ],
    )
    agent.send("run something")

    result = client.requests[1]["messages"][-1]["content"][0]
    assert result["is_error"] is True
    assert "max_tokens" in result["content"]


def test_refusal_rolls_back_the_turn(tmp_path: Path) -> None:
    refused = message([], "refusal", stop_details=SimpleNamespace(category="cyber"))
    agent, _ = make_agent(tmp_path, [message([text_block("Hi")], "end_turn"), refused])
    agent.send("hi")

    with pytest.raises(TurnFailed, match="cyber"):
        agent.send("something declined")
    assert len(agent.messages) == 2  # only the first turn remains


def test_history_is_append_only_across_turns(tmp_path: Path) -> None:
    agent, client = make_agent(
        tmp_path,
        [message([text_block("One")], "end_turn"), message([text_block("Two")], "end_turn")],
    )
    agent.send("first")
    agent.send("second")

    first, second = client.requests
    assert second["messages"][: len(first["messages"])] == first["messages"]


def test_request_uses_caching_thinking_and_fallbacks(tmp_path: Path) -> None:
    agent, client = make_agent(tmp_path, [message([text_block("Hi")], "end_turn")])
    agent.send("hi")

    request = client.requests[0]
    assert request["cache_control"] == {"type": "ephemeral"}
    assert request["thinking"]["type"] == "adaptive"
    assert request["fallbacks"] == "default"
    assert request["betas"] == [FALLBACK_BETA]


def test_no_fallbacks_for_haiku(tmp_path: Path) -> None:
    agent, client = make_agent(tmp_path, [message([text_block("Hi")], "end_turn")], model="claude-haiku-5-5")
    agent.send("hi")

    assert "fallbacks" not in client.requests[0]


def test_other_models_get_only_core_params(tmp_path: Path) -> None:
    model = "nvidia/nemotron-3-ultra-550b-a55b:free"
    agent, client = make_agent(tmp_path, [message([text_block("Hi")], "end_turn")], model=model)
    agent.send("hi")

    request = client.requests[0]
    assert set(request) == {"model", "max_tokens", "system", "tools", "messages"}
    assert all("eager_input_streaming" not in tool for tool in request["tools"])
