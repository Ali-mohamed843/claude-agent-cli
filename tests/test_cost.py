from types import SimpleNamespace

import pytest

from claude_agent_cli.cost import Usage


def test_from_api_treats_missing_cache_fields_as_zero() -> None:
    api_usage = SimpleNamespace(
        input_tokens=10, output_tokens=5, cache_read_input_tokens=None, cache_creation_input_tokens=None
    )
    assert Usage.from_api(api_usage) == Usage(10, 5, 0, 0)


def test_usage_adds_up() -> None:
    assert Usage(1, 2, 3, 4) + Usage(10, 20, 30, 40) == Usage(11, 22, 33, 44)


def test_cost_for_opus_5_5() -> None:
    # 1M of each kind: $4 input + $20 output + $0.20 cache read + $5 cache write (1.25 x $4)
    usage = Usage(1_000_000, 1_000_000, 1_000_000, 1_000_000)
    assert usage.cost("claude-opus-5-5") == pytest.approx(29.20)


def test_cost_unknown_model() -> None:
    assert Usage(100, 100).cost("some-future-model") is None
    assert "unknown price" in Usage(100, 100).summary("some-future-model")


def test_free_models_cost_nothing() -> None:
    assert Usage(1_000, 1_000).cost("nvidia/nemotron-3-ultra-550b-a55b:free") == 0.0
