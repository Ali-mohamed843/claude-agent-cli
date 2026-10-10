"""Shared test setup."""

from __future__ import annotations

from pathlib import Path

import pytest

from claude_agent_cli import proof


@pytest.fixture(autouse=True)
def signing_key_in_tmp(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Never create or read the real signing key in the user's home folder."""
    path = tmp_path_factory.mktemp("keys") / "signing-key.pem"
    monkeypatch.setattr(proof, "KEY_FILE", path)
    return path
