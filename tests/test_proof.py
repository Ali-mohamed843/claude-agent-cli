"""Tests for sealed recordings: the hash chain, signatures, redacted receipts and `receipts verify`."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from claude_agent_cli import proof
from claude_agent_cli.proof import Entry, Signer, chain_head, split_lines, verify
from claude_agent_cli.receipt import load_entries, main
from claude_agent_cli.recorder import RECEIPTS_DIR, Recorder


@pytest.fixture
def signer(signing_key_in_tmp: Path) -> Signer:
    return Signer.load_or_create()


def record(path: Path, signer: Signer | None, turns: int = 2, secret: str = "") -> Path:
    recorder = Recorder(path, signer=signer)
    recorder.event("session_start", name="demo", model="claude-opus-5-5")
    for n in range(turns):
        recorder.event("user_message", text=f"question {n}")
        recorder.event("model_response", content=[{"type": "text", "text": f"answer {n} {secret}"}])
        recorder.event("turn_end", status="ok", usage={"output_tokens": 10}, cost_usd=0.01)
    recorder.event("session_end", usage={}, cost_usd=0.02)
    recorder.close()
    return path


def lines(path: Path) -> list[str]:
    return split_lines(path.read_text(encoding="utf-8"))


def check(path: Path) -> proof.Report:
    return verify([Entry.of(line) for line in lines(path)])


def write_lines(path: Path, new_lines: list[str]) -> None:
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8", newline="")


# ---------------------------------------------------------------------------------------------- recording


def test_every_turn_and_the_end_are_sealed(tmp_path: Path, signer: Signer) -> None:
    path = record(tmp_path / "s.jsonl", signer)
    types = [json.loads(line)["type"] for line in lines(path)]
    assert types.count("seal") == 3  # two turns and the session end
    assert types[-1] == "seal"
    report = check(path)
    assert report.ok and report.seals == 3 and report.unsealed_tail == 0
    assert report.keys == [signer.fingerprint]


def test_without_a_signer_nothing_is_sealed(tmp_path: Path) -> None:
    report = check(record(tmp_path / "s.jsonl", None))
    assert not report.ok and report.seals == 0


def test_the_key_file_is_created_once(signing_key_in_tmp: Path) -> None:
    first = Signer.load_or_create()
    assert signing_key_in_tmp.exists()
    assert Signer.load_or_create().public_key == first.public_key


def test_resuming_continues_the_chain(tmp_path: Path, signer: Signer) -> None:
    path = record(tmp_path / "s.jsonl", signer, turns=1)
    recorder = Recorder(path, signer=signer)  # e.g. claude-agent --resume
    assert recorder.head == chain_head(lines(path))
    recorder.event("user_message", text="back again")
    recorder.event("turn_end", status="ok", usage={}, cost_usd=0.0)
    recorder.close()
    assert check(path).ok


def test_a_partial_last_line_does_not_break_resuming(tmp_path: Path, signer: Signer) -> None:
    path = record(tmp_path / "s.jsonl", signer, turns=1)
    with path.open("a", encoding="utf-8", newline="") as file:
        file.write('{"type": "user_mess')  # the window closed mid-write
    recorder = Recorder(path, signer=signer)
    recorder.event("turn_end", status="ok", usage={}, cost_usd=0.0)
    recorder.close()
    report = check(path)
    assert report.ok and report.unsealed_tail == 0


# ---------------------------------------------------------------------------------------------- tampering


def test_editing_a_line_breaks_the_seal(tmp_path: Path, signer: Signer) -> None:
    path = record(tmp_path / "s.jsonl", signer)
    edited = lines(path)
    edited[2] = edited[2].replace("answer 0", "a much better answer")
    write_lines(path, edited)
    report = check(path)
    assert not report.ok
    assert "between line 1 and line 4" in report.problems[0]


def test_deleting_a_failed_attempt_breaks_the_seal(tmp_path: Path, signer: Signer) -> None:
    path = record(tmp_path / "s.jsonl", signer)
    kept = [line for line in lines(path) if "question 1" not in line]
    write_lines(path, kept)
    assert not check(path).ok


def test_appending_after_the_last_seal_is_flagged(tmp_path: Path, signer: Signer) -> None:
    path = record(tmp_path / "s.jsonl", signer)
    write_lines(path, [*lines(path), json.dumps({"type": "user_message", "text": "added later"})])
    report = check(path)
    assert report.ok  # the sealed part is intact...
    assert report.unsealed_tail == 1  # ...and the extra line is reported as not covered


def test_a_forged_signature_is_rejected(tmp_path: Path, signer: Signer) -> None:
    path = record(tmp_path / "s.jsonl", signer, turns=1)
    forged = lines(path)
    index = next(i for i, line in enumerate(forged) if json.loads(line)["type"] == "seal")
    seal = json.loads(forged[index])
    seal["signature"] = "00" * 64
    forged[index] = json.dumps(seal)
    write_lines(path, forged)
    assert "invalid signature" in check(path).problems[0]


def test_resealing_with_another_key_shows_whose_key_it_is(tmp_path: Path, signer: Signer) -> None:
    """Someone else can rewrite and re-sign a recording, but then it carries their key, not yours."""
    path = record(tmp_path / "s.jsonl", signer, turns=1)
    other = Signer.load_or_create(tmp_path / "other.pem")
    rewritten = [line for line in lines(path) if json.loads(line)["type"] != "seal"]
    rewritten[2] = rewritten[2].replace("answer 0", "faked")
    fake = Recorder(tmp_path / "fake.jsonl", signer=other)
    for line in rewritten:
        fake._write(line)
    fake.seal()
    fake.close()
    report = check(tmp_path / "fake.jsonl")
    assert report.ok and report.keys == [other.fingerprint] != [signer.fingerprint]


# ---------------------------------------------------------------------------------------------- shared receipts


def share(tmp_path: Path, signer: Signer, secret: str = "") -> Path:
    folder = tmp_path / RECEIPTS_DIR
    folder.mkdir()
    record(folder / "s.jsonl", signer, secret=secret)
    assert main(["share", str(folder / "s.jsonl"), "--no-open"]) == 0
    return folder / "s.html"


def test_a_shared_page_verifies(tmp_path: Path, signer: Signer, capsys: Any) -> None:
    page = share(tmp_path, signer)
    assert "Sealed · unedited" in page.read_text(encoding="utf-8")
    assert main(["verify", str(page)]) == 0
    out = capsys.readouterr().out
    assert "Verified" in out and "your key" in out and "“demo”" in out


def test_redacted_lines_are_hidden_but_still_covered(tmp_path: Path, signer: Signer) -> None:
    secret = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123"
    page = share(tmp_path, signer, secret=secret)
    html = page.read_text(encoding="utf-8")
    assert secret not in html  # not in the view, and not in the embedded proof
    report = verify(load_entries(page))
    assert report.ok and report.redacted == 2


def test_editing_the_proof_inside_a_page_is_caught(tmp_path: Path, signer: Signer, capsys: Any) -> None:
    page = share(tmp_path, signer)
    page.write_text(page.read_text(encoding="utf-8").replace("question 1", "question 9"), encoding="utf-8")
    assert main(["verify", str(page)]) == 1
    assert "Not trustworthy" in capsys.readouterr().out


def test_unsealed_recordings_say_so(tmp_path: Path, capsys: Any) -> None:
    path = record(tmp_path / "s.jsonl", None)
    assert main(["verify", str(path)]) == 1
    assert "Not sealed" in capsys.readouterr().out


def test_key_command_shows_the_fingerprint(signer: Signer, capsys: Any) -> None:
    assert main(["key"]) == 0
    assert signer.fingerprint in capsys.readouterr().out
