"""Every code gets a test, and the gate gets a test that makes it miss.

A checker that has never been shown to miss something certifies nothing, so one test here
switches the image checks off through the `checks=` seam and proves the 859 px reference then
travels all the way to the model call. The public-clean scanner is treated the same way: five
forbidden shapes are planted in memory and each must fire before a clean tree means anything.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from model_error_translator.cli import main, write_receipt
from model_error_translator.core import (
    ADMISSION_CODES,
    DEFAULT_CHECKS,
    FAILURE_CODES,
    HaltError,
    admit,
    check_batch,
    dispatch,
    explain_batch,
    load_manifest,
    read_rows,
    translate,
)

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "fixtures" / "manifest.json"
CLEAN = ROOT / "fixtures" / "requests_clean.jsonl"
BAD = ROOT / "fixtures" / "requests_bad.jsonl"
FAILURES = ROOT / "fixtures" / "failures.jsonl"
POSTING_SENTENCE = (
    "Your reference image needs to be at least 1024 px on the short side and 16- or 32-bit; "
    "yours is 859 px and 24-bit."
)
SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "runs"}
# Shapes, never names: no machine, drive, user or person is written out here.
PUBLIC_CLEAN = (
    ("drive_letter_root", re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/]")),
    ("windows_user_home", re.compile(r"[\\/][Uu]sers[\\/]")),
    ("posix_user_home", re.compile(r"[\\/]home[\\/][A-Za-z0-9._-]+")),
    ("accuracy_claim", re.compile(r"\b(?:MAE|accuracy|band)\s*[:=]?\s*[0-9]", re.IGNORECASE)),
    ("consumer_mailbox", re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|outlook|yahoo)\.[A-Za-z]{2,}")),
)
# Built by concatenation so the forbidden literal never appears in this file.
PLANTS = (
    ("drive_letter_root", "D" + ":" + "\\" + "work" + "\\" + "notes.txt"),
    ("windows_user_home", "\\" + "Users" + "\\" + "someone" + "\\"),
    ("posix_user_home", "/" + "home" + "/someone/notes"),
    ("accuracy_claim", "acc" + "uracy = 0.93"),
    ("consumer_mailbox", "someone" + "@" + "gmail" + ".com"),
)
NETWORK_SHAPES = (
    re.compile(r"\b(?:import|from)\s+(?:urllib|socket|ssl|subprocess|http\b|requests\b)"),
    re.compile(r"\burlopen\s*\("),
    re.compile(r"\bsubprocess\."),
)


def _rows(path):
    return [row for _, row, _ in read_rows(path)]


def _by_id(result, row_id):
    return next(o for o in result.outcomes if o.row_id == row_id)


def _shipped_files():
    for path in sorted(ROOT.rglob("*")):
        if path.is_file() and not any(part in SKIP_DIRS for part in path.parts):
            yield path


def _scan(text):
    return sorted({name for name, pattern in PUBLIC_CLEAN if pattern.search(text)})


def test_clean_requests_are_all_admitted_and_the_batch_is_go():
    result = check_batch(load_manifest(MANIFEST), CLEAN)
    assert result.verdict == "GO"
    assert result.counts == {"ADMIT": 3, "REFUSE": 0, "TRANSLATE": 0, "ABSTAIN": 0}
    assert all(outcome.codes == () and outcome.artist == "" for outcome in result.outcomes)


def test_each_seeded_bad_request_fires_the_codes_its_own_row_names():
    result = check_batch(load_manifest(MANIFEST), BAD)
    assert result.verdict == "HOLD"
    assert result.counts == {"ADMIT": 0, "REFUSE": 5, "TRANSLATE": 0, "ABSTAIN": 1}
    for row in _rows(BAD):
        assert _by_id(result, row["request_id"]).codes == tuple(row["expect_codes"]), row["request_id"]
    assert set(result.code_counts) == set(ADMISSION_CODES)


def test_the_refusal_reads_like_the_sentence_the_posting_asked_for():
    outcome = _by_id(check_batch(load_manifest(MANIFEST), BAD), "req-bad-859")
    assert outcome.verdict == "REFUSE"
    assert outcome.artist == POSTING_SENTENCE
    assert "859" in outcome.operator and "IMAGE_TOO_SMALL" in outcome.operator


def test_every_failure_shape_maps_to_the_one_code_its_row_names():
    result = explain_batch(FAILURES)
    assert result.counts == {"ADMIT": 0, "REFUSE": 0, "TRANSLATE": 6, "ABSTAIN": 1}
    for row in _rows(FAILURES):
        assert _by_id(result, row["failure_id"]).codes == (row["expect_code"],), row["failure_id"]
    assert set(result.code_counts) == set(FAILURE_CODES)
    assert result.verdict == "HOLD"
    assert "about 37 seconds" in _by_id(result, "fail-429-busy").artist


@pytest.mark.parametrize("seconds", [0, 37])
def test_rate_limit_preserves_retry_interval_without_inventing_a_queue(seconds):
    outcome = translate({"status": 429, "headers": {"Retry-After": str(seconds)}})
    assert outcome.codes == ("RATE_LIMITED",)
    assert f"about {seconds} seconds" in outcome.artist
    assert "unknown" in outcome.artist.lower()
    assert "queued" not in outcome.artist.lower() and "nothing is lost" not in outcome.artist.lower()


def test_timeout_requires_checking_unknown_completion_before_retry():
    outcome = translate({"transport": "timeout"})
    assert outcome.codes == ("REQUEST_TIMEOUT",)
    assert "completion is unknown" in outcome.artist.lower()
    assert "before retrying" in outcome.artist.lower()
    assert "dropped" not in outcome.artist.lower() and "nothing was written" not in outcome.artist.lower()


def test_failure_messages_preserve_uncertainty_and_local_receipt_privacy():
    for row in _rows(FAILURES):
        outcome = translate(row)
        assert "local correlation reference" in outcome.operator
        assert outcome.receipt_id in outcome.operator
        assert "proxy log by receipt" not in outcome.operator
        if row["body"]:
            assert row["body"] not in outcome.operator
        for claim in ("Nothing in your scene caused it", "This one is ours to fix", "It is logged as"):
            assert claim not in outcome.artist


def test_an_unrecognised_failure_shape_abstains_with_a_receipt_and_never_guesses():
    outcome = _by_id(explain_batch(FAILURES), "fail-403-unknown")
    assert outcome.verdict == "ABSTAIN" and outcome.codes == ("ABSTAIN_UNRECOGNISED",)
    assert outcome.receipt_id.startswith("rcpt-") and len(outcome.receipt_id) == 21
    assert "will not guess" in outcome.artist
    for guess in ("proxy", "policy", "denied", "rate", "too large"):
        assert guess not in outcome.artist.lower()


def test_the_artist_sentence_never_carries_a_character_of_the_raw_body():
    result = explain_batch(FAILURES)
    for row in _rows(FAILURES):
        artist = _by_id(result, row["failure_id"]).artist
        body = row["body"]
        assert len(artist) < 320
        for start in range(0, max(len(body) - 24, 0), 17):
            assert body[start : start + 24] not in artist
        for line in {line.strip() for line in body.splitlines() if len(line.strip()) > 12}:
            assert line not in artist


def test_falsifier_image_checks_disabled_send_the_859px_reference_to_the_model():
    manifest = load_manifest(MANIFEST)
    row = next(r for r in _rows(BAD) if r["request_id"] == "req-bad-859")
    reached: list[str] = []

    def transport(request):  # the stand-in for the hosted call; it records that it was reached
        reached.append(request["request_id"])
        return {"sent": True}

    guarded, response = dispatch(row, manifest, transport, DEFAULT_CHECKS)
    assert guarded.verdict == "REFUSE" and response is None and reached == []
    weakened = tuple(c for c in DEFAULT_CHECKS if c not in ("image_min_side", "bit_depth"))
    unguarded, response = dispatch(row, manifest, transport, weakened)
    assert unguarded.verdict == "ADMIT" and unguarded.codes == ()
    assert response == {"sent": True} and reached == ["req-bad-859"]


def test_a_malformed_request_abstains_rather_than_being_forced_into_a_code():
    manifest = load_manifest(MANIFEST)
    for broken in ({"synthetic": True, "request_id": "x", "model": "synthetic-refiner-xl"}, None):
        outcome = admit(broken, manifest)
        assert outcome.verdict == "ABSTAIN" and outcome.codes == ("MALFORMED_REQUEST",)


def test_malformed_admission_does_not_invent_fault_attribution():
    reached = []
    outcome, response = dispatch(None, load_manifest(MANIFEST), reached.append)
    assert outcome.verdict == "ABSTAIN" and response is None and reached == []
    assert "fault is in the wiring" not in outcome.artist
    assert "not in your image" not in outcome.artist


def test_a_row_without_the_synthetic_marker_halts_the_run(tmp_path):
    path = tmp_path / "live.jsonl"
    path.write_text('{"request_id": "x", "model": "synthetic-refiner-xl"}\n', encoding="utf-8")
    with pytest.raises(HaltError) as caught:
        check_batch(load_manifest(MANIFEST), path)
    assert caught.value.code == "NOT_SYNTHETIC"
    assert check_batch(load_manifest(MANIFEST), path, allow_nonsynthetic=True).verdict == "HOLD"


def test_receipts_carry_the_hash_of_the_body_and_two_runs_are_byte_identical(tmp_path):
    for name in ("first", "second"):
        write_receipt(str(tmp_path / name), explain_batch(FAILURES))
    text = (tmp_path / "first" / "receipts.jsonl").read_text(encoding="utf-8")
    assert text == (tmp_path / "second" / "receipts.jsonl").read_text(encoding="utf-8")
    assert (tmp_path / "first" / "summary.json").read_bytes() == (
        tmp_path / "second" / "summary.json"
    ).read_bytes()
    rows = [json.loads(line) for line in text.strip().splitlines()]
    assert len(rows) == 7 and all(len(row["body_sha256"]) == 64 for row in rows)
    for leaked in ("proxy model failed", "Traceback", "upstream", "retry-after", "queue_depth", "html"):
        assert leaked not in text
    assert json.loads((tmp_path / "first" / "summary.json").read_text(encoding="utf-8"))["verdict"] == "HOLD"


def test_public_clean_scanner_fires_on_every_planted_shape_then_the_tree_is_clean():
    for expected, planted in PLANTS:
        assert _scan(planted) == [expected], planted
    findings = {
        path.relative_to(ROOT).as_posix(): _scan(path.read_text(encoding="utf-8", errors="ignore"))
        for path in _shipped_files()
    }
    assert {name: hits for name, hits in findings.items() if hits} == {}
    assert len(findings) >= 12


def test_no_network_or_process_capable_import_exists_anywhere_under_src():
    for path in sorted((ROOT / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        for shape in NETWORK_SHAPES:
            assert not shape.search(text), (path.name, shape.pattern)


def test_cli_returns_zero_on_go_two_on_hold_and_one_on_a_crash(capsys, tmp_path):
    assert main(["check", "--manifest", str(MANIFEST), "--requests", str(CLEAN)]) == 0
    assert "VERDICT: GO" in capsys.readouterr().out
    code = main(["check", "--manifest", str(MANIFEST), "--requests", str(BAD)])
    assert code == 2 and "VERDICT: HOLD (5 refused, 1 abstained of 6)" in capsys.readouterr().out
    code = main(["explain", "--failures", str(FAILURES), "--out", str(tmp_path / "r")])
    assert code == 2 and "VERDICT: HOLD (1 abstained of 7)" in capsys.readouterr().out
    assert main(["check", "--manifest", str(tmp_path / "gone.json"), "--requests", str(CLEAN)]) == 1
    assert "VERDICT: HOLD (crash: FileNotFoundError)" in capsys.readouterr().out
