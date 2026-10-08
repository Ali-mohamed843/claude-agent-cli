"""Token accounting and cost estimates."""

from __future__ import annotations

from dataclasses import dataclass

# USD per million tokens: (input, output, cache read). Cache writes (5-minute TTL) cost 1.25x input.
# Check https://www.anthropic.com/pricing when you add a model; prices change.
PRICES: dict[str, tuple[float, float, float]] = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-opus-4-8": (5.00, 25.00, 0.50),
    "claude-sonnet-5-5": (2.00, 10.00, 0.20),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
}
CACHE_WRITE_MULTIPLIER = 1.25


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
        )

    @classmethod
    def from_api(cls, usage: object) -> Usage:
        """Build from a response's `usage` object. Cache fields can be None, so default them to 0."""
        return cls(
            input_tokens=getattr(usage, "input_tokens", 0) or 0,
            output_tokens=getattr(usage, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(usage, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(usage, "cache_creation_input_tokens", 0) or 0,
        )

    def cost(self, model: str) -> float | None:
        """Cost in USD, or None when we don't know the model's price."""
        if model.endswith(":free"):  # OpenRouter's free model variants
            return 0.0
        if model not in PRICES:
            return None
        input_price, output_price, cache_read_price = PRICES[model]
        dollars = (
            self.input_tokens * input_price
            + self.output_tokens * output_price
            + self.cache_read_tokens * cache_read_price
            + self.cache_write_tokens * input_price * CACHE_WRITE_MULTIPLIER
        )
        return dollars / 1_000_000

    def summary(self, model: str) -> str:
        cost = self.cost(model)
        price = f"${cost:.4f}" if cost is not None else "unknown price"
        return (
            f"in {self.input_tokens:,} · out {self.output_tokens:,} · "
            f"cache read {self.cache_read_tokens:,} · cache write {self.cache_write_tokens:,} · {price}"
        )
