"""Tamper-evident recordings: a hash chain over every recorded line, sealed with the user's own signing key.

How it works:
- Every line of a recording is chained to everything before it:
      head_0 = 64 zeros
      head_i = SHA-256(head_{i-1} bytes + SHA-256(line_i) bytes)
  so each head commits to every earlier line, in order.
- After each turn and at the end of a session, the recorder appends a "seal" line: the current head, signed
  with an Ed25519 key that lives on the user's machine (~/.claude-agent/signing-key.pem).
- A shared receipt can redact a line by publishing only SHA-256(line) instead of the line. The chain still
  computes, so the seals still cover the hidden content: redacting hides text, it can't hide edits.

What a valid seal proves: nothing in the recording was added, removed, reordered or changed after it was sealed,
by anyone who doesn't hold the signing key. It does not prove the key's owner didn't write a fake session from
scratch; a key is only as trustworthy as the person it belongs to (publish your fingerprint, e.g. on GitHub).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

GENESIS = "0" * 64
KEY_FILE = Path.home() / ".claude-agent" / "signing-key.pem"
PROOF_VERSION = 1


def line_hash(line: str) -> str:
    return hashlib.sha256(line.encode("utf-8")).hexdigest()


def link(head: str, content_hash: str) -> str:
    return hashlib.sha256(bytes.fromhex(head) + bytes.fromhex(content_hash)).hexdigest()


def chain_head(lines: list[str]) -> str:
    head = GENESIS
    for line in lines:
        head = link(head, line_hash(line))
    return head


def fingerprint(public_key_hex: str) -> str:
    """A short, readable ID for a public key, e.g. '3F2A 9C1B 77D0 41E8'."""
    digest = hashlib.sha256(bytes.fromhex(public_key_hex)).hexdigest()[:16].upper()
    return " ".join(digest[i : i + 4] for i in range(0, 16, 4))


def split_lines(text: str) -> list[str]:
    """Recording lines, exactly as hashed. JSON never contains a raw newline, so "\\n" is the only separator.

    (str.splitlines() would also split on characters like U+2028, which JSON may contain unescaped.)
    """
    return [line for line in text.replace("\r\n", "\n").split("\n") if line]


# ---------------------------------------------------------------------------------------------- signing


class Signer:
    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        raw = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self.public_key = raw.hex()
        self.fingerprint = fingerprint(self.public_key)

    @classmethod
    def load(cls, path: Path | None = None) -> Signer | None:
        path = path or KEY_FILE
        if not path.exists():
            return None
        key = serialization.load_pem_private_key(path.read_bytes(), password=None)
        if not isinstance(key, Ed25519PrivateKey):
            raise ValueError(f"{path} is not an Ed25519 key")
        return cls(key)

    @classmethod
    def load_or_create(cls, path: Path | None = None) -> Signer:
        path = path or KEY_FILE
        existing = cls.load(path)
        if existing is not None:
            return existing
        key = Ed25519PrivateKey.generate()
        pem = key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        # Create the file readable by its owner only (on Windows the user profile already restricts access).
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as file:
            file.write(pem)
        return cls(key)

    def sign(self, head: str) -> str:
        return self._key.sign(bytes.fromhex(head)).hex()


def signature_valid(public_key_hex: str, head: str, signature_hex: str) -> bool:
    try:
        Ed25519PublicKey.from_public_bytes(bytes.fromhex(public_key_hex)).verify(
            bytes.fromhex(signature_hex), bytes.fromhex(head)
        )
        return True
    except (InvalidSignature, ValueError):
        return False


# ---------------------------------------------------------------------------------------------- verifying


@dataclass
class Entry:
    """One recording line: the line itself, or only its hash when the line was redacted."""

    hash: str
    line: str | None = None

    @classmethod
    def of(cls, line: str) -> Entry:
        return cls(line_hash(line), line)


@dataclass
class Report:
    events: int = 0
    seals: int = 0
    redacted: int = 0
    keys: list[str] = field(default_factory=list)  # fingerprints, in the order they first sealed
    sealed_through: int = -1  # index of the last line covered by a valid seal
    broken_after: int | None = None  # a seal failed: something after this line index (up to the seal) changed
    broken_at: int | None = None  # index of the first failing seal
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.seals > 0 and not self.problems

    @property
    def unsealed_tail(self) -> int:
        """Lines after the last valid seal (e.g. the session was closed mid-turn)."""
        return self.events - 1 - self.sealed_through if self.seals else self.events


def verify(entries: list[Entry]) -> Report:
    report = Report(events=len(entries))
    head = GENESIS
    last_good = -1
    for index, entry in enumerate(entries):
        if entry.line is None:
            report.redacted += 1
        elif line_hash(entry.line) != entry.hash:
            report.problems.append(f"Line {index + 1} doesn't match its own hash.")
        else:
            seal = _as_seal(entry.line)
            if seal is not None and report.broken_at is None:
                report.seals += 1
                key = fingerprint(seal["key"]) if _is_hex(seal["key"], 32) else "invalid key"
                if seal["head"] != head:
                    report.broken_at, report.broken_after = index, last_good
                    report.problems.append(_broken_message(last_good, index))
                elif not signature_valid(seal["key"], head, seal["signature"]):
                    report.broken_at, report.broken_after = index, last_good
                    report.problems.append(f"Seal on line {index + 1} has an invalid signature.")
                else:
                    if key not in report.keys:
                        report.keys.append(key)
                    report.sealed_through = index  # the seal line itself is covered by the next seal
                    last_good = index
        head = link(head, entry.hash)
    return report


def _broken_message(last_good: int, seal_index: int) -> str:
    start = last_good + 2  # 1-based line numbers, after the last good seal
    return f"Something between line {start} and line {seal_index} was changed, added or removed after sealing."


def _as_seal(line: str) -> dict[str, str] | None:
    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None
    if not isinstance(event, dict) or event.get("type") != "seal":
        return None
    fields = {k: event.get(k) for k in ("head", "key", "signature")}
    if not all(isinstance(v, str) for v in fields.values()):
        return None
    return {k: str(v) for k, v in fields.items()}


def _is_hex(value: str, length: int) -> bool:
    try:
        return len(bytes.fromhex(value)) == length
    except ValueError:
        return False


# ---------------------------------------------------------------------------------------------- receipt bundles


def bundle(entries: list[Entry]) -> dict[str, Any]:
    """The proof embedded in a receipt page: every line, or its hash when redacted."""
    return {
        "version": PROOF_VERSION,
        "entries": [{"line": e.line} if e.line is not None else {"hash": e.hash} for e in entries],
    }


def entries_from_bundle(data: dict[str, Any]) -> list[Entry]:
    entries = []
    for item in data.get("entries", []):
        if isinstance(item.get("line"), str):
            entries.append(Entry.of(item["line"]))
        elif isinstance(item.get("hash"), str) and _is_hex(item["hash"], 32):
            entries.append(Entry(item["hash"].lower()))
        else:
            raise ValueError("The proof in this file is malformed.")
    return entries
