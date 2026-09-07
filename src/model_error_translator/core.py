"""Admission checks before a model call, and failure translation after one.

Two jobs, one rule each way. BEFORE a call: validate the request against the model manifest
and refuse with a typed code plus a sentence an artist can act on, so the bad send never
leaves the workstation. AFTER a failed call: map the raw failure payload to one typed code,
one artist sentence and one operator hint, hash the raw body into a receipt, and show the
artist none of it. A failure shape the rules do not recognise returns ABSTAIN with its
receipt id -- never a guess, because a guessed cause costs an artist a day.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

ADMISSION_CODES = (
    "UNKNOWN_MODEL",
    "MISSING_FIELD",
    "IMAGE_TOO_SMALL",
    "IMAGE_TOO_LARGE",
    "BIT_DEPTH_UNSUPPORTED",
    "PAYLOAD_TOO_LARGE",
    "MALFORMED_REQUEST",
)
FAILURE_CODES = (
    "PROXY_MODEL_FAILED",
    "PAYLOAD_REJECTED",
    "RATE_LIMITED",
    "MODEL_SERVER_ERROR",
    "MALFORMED_RESPONSE",
    "REQUEST_TIMEOUT",
    "ABSTAIN_UNRECOGNISED",
)
CODES = ADMISSION_CODES + FAILURE_CODES
DEFAULT_CHECKS = ("required_fields", "image_min_side", "image_max_side", "bit_depth", "payload_bytes")
RETRY_AFTER = re.compile(r"^\s*(\d{1,5})\s*$")


class HaltError(Exception):
    """The run stops rather than reporting on inputs it was not allowed to read."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Outcome:
    """One row's result. ADMIT, REFUSE, TRANSLATE or ABSTAIN -- there is no fifth."""

    row_id: str
    verdict: str
    codes: tuple[str, ...]
    artist: str
    operator: str
    receipt_id: str = ""
    facts: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BatchResult:
    kind: str
    source_sha: str
    outcomes: tuple[Outcome, ...]

    @property
    def counts(self) -> dict[str, int]:
        tally = {"ADMIT": 0, "REFUSE": 0, "TRANSLATE": 0, "ABSTAIN": 0}
        for outcome in self.outcomes:
            tally[outcome.verdict] += 1
        return tally

    @property
    def code_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            for code in outcome.codes:
                counts[code] = counts.get(code, 0) + 1
        return counts

    @property
    def verdict(self) -> str:
        return "GO" if all(o.verdict in ("ADMIT", "TRANSLATE") for o in self.outcomes) else "HOLD"


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _join(parts: Sequence[str]) -> str:
    parts = list(parts)
    if len(parts) <= 1:
        return "".join(parts)
    return ", ".join(parts[:-1]) + " and " + parts[-1]


def _depth_phrase(depths: Sequence[int]) -> str:
    ordered = sorted(depths)
    return _join([f"{d}-" for d in ordered[:-1]] + [f"{ordered[-1]}-bit"]).replace("- and", "- or")


def _mb(byte_count: int) -> str:
    return f"{byte_count / 1_048_576:.1f}"


def load_manifest(path: str | Path, allow_nonsynthetic: bool = False) -> dict[str, Any]:
    raw = Path(path).read_bytes()
    manifest = json.loads(raw.decode("utf-8"))
    if not allow_nonsynthetic and manifest.get("synthetic") is not True:
        raise HaltError("NOT_SYNTHETIC", "the manifest carries no synthetic marker")
    if not isinstance(manifest.get("models"), dict) or not manifest["models"]:
        raise HaltError("BAD_MANIFEST", "the manifest declares no models")
    manifest["sha256"] = sha256_of(raw)
    return manifest


def read_rows(path: str | Path, allow_nonsynthetic: bool = False) -> list[tuple[int, dict | None, bytes]]:
    """Rows that are not JSON objects come back as None and abstain later; they never crash."""
    rows: list[tuple[int, dict | None, bytes]] = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            rows.append((number, None, stripped.encode("utf-8")))
            continue
        if not isinstance(row, dict):
            rows.append((number, None, stripped.encode("utf-8")))
            continue
        if not allow_nonsynthetic and row.get("synthetic") is not True:
            raise HaltError("NOT_SYNTHETIC", f"row {number} of {Path(path).name} carries no synthetic marker")
        rows.append((number, row, stripped.encode("utf-8")))
    return rows


def _abstain_request(row_id: str, reason: str) -> Outcome:
    return Outcome(
        row_id,
        "ABSTAIN",
        ("MALFORMED_REQUEST",),
        "This gate could not read the required request details and has not called the model. "
        "Ask your pipeline TD to check the supplied request fields.",
        f"MALFORMED_REQUEST: {reason}.",
    )


def _is_image(value: Any) -> bool:
    return isinstance(value, dict) and all(
        isinstance(value.get(key), int) for key in ("width", "height", "bit_depth")
    )


def admit(row: dict | None, manifest: dict, checks: Sequence[str] = DEFAULT_CHECKS) -> Outcome:
    """The whole gate. Nothing here calls anything; it decides whether a call may happen."""
    if row is None:
        return _abstain_request("row", "the line is not a JSON object")
    row_id = str(row.get("request_id") or "row")
    model_id = row.get("model")
    spec = manifest["models"].get(model_id) if isinstance(model_id, str) else None
    if spec is None:
        known = ", ".join(sorted(manifest["models"]))
        return Outcome(
            row_id,
            "REFUSE",
            ("UNKNOWN_MODEL",),
            f"This node is pointed at a model the pipeline does not publish. Pick one of: {known}.",
            f"UNKNOWN_MODEL: request {row_id} named {model_id!r}; the manifest publishes {known}.",
        )
    fields, payload = row.get("fields"), row.get("payload_bytes")
    if not isinstance(fields, dict) or not isinstance(payload, int):
        return _abstain_request(row_id, "the row has no fields object or no integer payload_bytes")
    image = fields.get("image")
    if image is not None and not _is_image(image):
        return _abstain_request(row_id, "the image field is not width/height/bit_depth integers")

    codes: list[str] = []
    sentences: list[str] = []
    need: list[str] = []
    have: list[str] = []
    if "required_fields" in checks:
        missing = [name for name in spec["required_fields"] if fields.get(name) in (None, "")]
        if missing:
            codes.append("MISSING_FIELD")
            sentences.append(
                f"This send has no {_join(missing)}. {model_id} needs {_join(spec['required_fields'])}."
            )
    if image is not None:
        short_side, long_side = min(image["width"], image["height"]), max(image["width"], image["height"])
        depth = image["bit_depth"]
        if "image_min_side" in checks and short_side < spec["min_short_side_px"]:
            codes.append("IMAGE_TOO_SMALL")
            need.append(f"at least {spec['min_short_side_px']} px on the short side")
            have.append(f"{short_side} px")
        if "image_max_side" in checks and long_side > spec["max_long_side_px"]:
            codes.append("IMAGE_TOO_LARGE")
            need.append(f"at most {spec['max_long_side_px']} px on the long side")
            have.append(f"{long_side} px")
        if "bit_depth" in checks and depth not in spec["allowed_bit_depths"]:
            codes.append("BIT_DEPTH_UNSUPPORTED")
            need.append(_depth_phrase(spec["allowed_bit_depths"]))
            have.append(f"{depth}-bit")
    if need:
        sentences.append(f"Your reference image needs to be {_join(need)}; yours is {_join(have)}.")
    if "payload_bytes" in checks and payload > spec["max_payload_bytes"]:
        codes.append("PAYLOAD_TOO_LARGE")
        sentences.append(
            f"This send is {_mb(payload)} MB; {model_id} takes {_mb(spec['max_payload_bytes'])} MB at a time. "
            "Send fewer frames, or a smaller reference."
        )
    if not codes:
        return Outcome(row_id, "ADMIT", (), "", f"ADMIT: request {row_id} met every {model_id} limit.")
    ordered = tuple(sorted(codes, key=ADMISSION_CODES.index))
    limits = (
        f"short_side>={spec['min_short_side_px']}px long_side<={spec['max_long_side_px']}px "
        f"bit_depth in {sorted(spec['allowed_bit_depths'])} payload<={spec['max_payload_bytes']}B"
    )
    return Outcome(
        row_id,
        "REFUSE",
        ordered,
        " ".join(sentences),
        f"{','.join(ordered)}: request {row_id} refused before any call went out; {model_id} limits {limits}.",
        facts={"model": model_id},
    )


def dispatch(row: dict, manifest: dict, transport: Callable[[dict], Any], checks: Sequence[str] = DEFAULT_CHECKS):
    """The seam the falsifier test pulls on: the transport is reached only on ADMIT."""
    outcome = admit(row, manifest, checks)
    if outcome.verdict != "ADMIT":
        return outcome, None
    return outcome, transport(row)


def raw_payload(row: dict) -> bytes:
    """The bytes a receipt is taken over: what the service actually sent back, nothing of ours."""
    return json.dumps(
        {
            "status": row.get("status"),
            "headers": row.get("headers") or {},
            "transport": row.get("transport"),
            "body": row.get("body") or "",
        },
        sort_keys=True,
    ).encode("utf-8")


def classify(status: Any, transport: str | None, body: str) -> str:
    """Ordered rules, fail-closed default. An unmatched shape is ABSTAIN, never a nearest guess."""
    low = body.lower()
    if status is None:
        return "REQUEST_TIMEOUT" if "timeout" in (transport or "").lower() else "ABSTAIN_UNRECOGNISED"
    if status == 403 and "proxy model failed" in low:
        return "PROXY_MODEL_FAILED"
    if status == 413:
        return "PAYLOAD_REJECTED"
    if status == 429:
        return "RATE_LIMITED"
    if status in (500, 502, 503):
        return "MODEL_SERVER_ERROR"
    if status == 200:
        try:
            json.loads(body)
        except (json.JSONDecodeError, TypeError):
            return "MALFORMED_RESPONSE"
    return "ABSTAIN_UNRECOGNISED"


def _retry_seconds(headers: dict) -> int | None:
    for key, value in headers.items():
        if key.lower() == "retry-after":
            match = RETRY_AFTER.match(str(value))
            return int(match.group(1)) if match else None
    return None


def translate(row: dict | None) -> Outcome:
    """Raw failure payload in, one code plus two sentences out. The body is hashed, never quoted."""
    if row is None:
        return Outcome(
            "row",
            "ABSTAIN",
            ("ABSTAIN_UNRECOGNISED",),
            "Something went wrong with this send and this node will not guess at what.",
            "ABSTAIN_UNRECOGNISED: the failure line is not a JSON object.",
        )
    row_id = str(row.get("failure_id") or "row")
    raw = raw_payload(row)
    receipt = "rcpt-" + sha256_of(raw)[:16]
    status, headers = row.get("status"), row.get("headers") or {}
    body = row.get("body") or ""
    code = classify(status, row.get("transport"), body)
    model_id = row.get("model", "this model")
    seconds = _retry_seconds(headers)
    wait = f"about {seconds} seconds" if seconds is not None else "a short while"
    sent = f"reported request size {_mb(row['request_bytes'])} MB" if isinstance(row.get("request_bytes"), int) else "request size unavailable"
    artist = {
        "PROXY_MODEL_FAILED": (
            f"The service reported it could not run {model_id}. The cause and completion state are "
            "unknown. Ask your pipeline TD to check before retrying; "
            f"local correlation reference {receipt}."
        ),
        "PAYLOAD_REJECTED": (
            f"The service refused this send as too large ({sent}). Send fewer frames, or "
            "drop the reference to a smaller resolution, and try again."
        ),
        "RATE_LIMITED": (
            f"The service reported a rate limit. Wait {wait} before retrying. "
            "Queue and completion status are unknown."
        ),
        "MODEL_SERVER_ERROR": (
            "The service returned a server error. Completion is unknown; check before retrying. "
            f"Give your pipeline TD local correlation reference {receipt}."
        ),
        "MALFORMED_RESPONSE": (
            "The service returned a response this node could not parse. Check the result before "
            f"retrying; local correlation reference {receipt}."
        ),
        "REQUEST_TIMEOUT": (
            "The request timed out; completion is unknown. Check whether it completed before retrying. "
            f"Give your pipeline TD local correlation reference {receipt}."
        ),
        "ABSTAIN_UNRECOGNISED": (
            "Something went wrong with this send and this node will not guess at what. "
            f"Give your pipeline TD local correlation reference {receipt}."
        ),
    }[code]
    operator = (
        f"{code}: status={status if status is not None else 'none'} transport={row.get('transport') or 'none'} "
        f"body_bytes={len(body.encode('utf-8'))} sha256={sha256_of(raw)[:32]} -- "
        f"local correlation reference {receipt}; provider-log lookup is not verified. Raw body omitted."
    )
    verdict = "ABSTAIN" if code == "ABSTAIN_UNRECOGNISED" else "TRANSLATE"
    facts = {"status": status, "body_bytes": len(body.encode("utf-8")), "body_sha256": sha256_of(raw)}
    return Outcome(row_id, verdict, (code,), artist, operator, receipt, facts)


def check_batch(
    manifest: dict,
    requests_path: str | Path,
    checks: Sequence[str] = DEFAULT_CHECKS,
    allow_nonsynthetic: bool = False,
) -> BatchResult:
    rows = read_rows(requests_path, allow_nonsynthetic)
    outcomes = tuple(admit(row, manifest, checks) for _, row, _ in rows)
    return BatchResult("check", sha256_of(Path(requests_path).read_bytes()), outcomes)


def explain_batch(failures_path: str | Path, allow_nonsynthetic: bool = False) -> BatchResult:
    rows = read_rows(failures_path, allow_nonsynthetic)
    outcomes = tuple(translate(row) for _, row, _ in rows)
    return BatchResult("explain", sha256_of(Path(failures_path).read_bytes()), outcomes)


def receipt_row(outcome: Outcome) -> dict[str, Any]:
    """Ids, codes, counts and hashes only. No body, no header values, no prompt, no file path."""
    return {
        "row_id": outcome.row_id,
        "verdict": outcome.verdict,
        "codes": list(outcome.codes),
        "receipt_id": outcome.receipt_id,
        "status": outcome.facts.get("status"),
        "body_bytes": outcome.facts.get("body_bytes"),
        "body_sha256": outcome.facts.get("body_sha256"),
    }


def summary_document(result: BatchResult, version: str) -> dict[str, Any]:
    return {
        "tool_version": version,
        "kind": result.kind,
        "source_sha256": result.source_sha,
        "rows": len(result.outcomes),
        "counts": result.counts,
        "code_counts": result.code_counts,
        "verdict": result.verdict,
    }


def render(result: BatchResult) -> str:
    counts = result.counts
    head = (
        f"CHECKED: {len(result.outcomes)} requests  admitted={counts['ADMIT']} "
        f"refused={counts['REFUSE']} abstained={counts['ABSTAIN']}"
        if result.kind == "check"
        else f"TRANSLATED: {len(result.outcomes)} failures  translated={counts['TRANSLATE']} "
        f"abstained={counts['ABSTAIN']}"
    )
    lines = [head]
    width = max((len(o.row_id) for o in result.outcomes), default=1)
    for outcome in result.outcomes:
        tail = ",".join(outcome.codes) if outcome.codes else ""
        receipt = f"  {outcome.receipt_id}" if outcome.receipt_id else ""
        lines.append(f"  {outcome.row_id.ljust(width)}  {outcome.verdict.ljust(9)}{receipt}  {tail}".rstrip())
        if outcome.artist:
            lines.append(f"      artist:   {outcome.artist}")
            lines.append(f"      operator: {outcome.operator}")
    codes = result.code_counts
    lines.append(
        "CODES: " + (" ".join(f"{code}={codes[code]}" for code in sorted(codes)) if codes else "none")
    )
    if result.verdict == "GO":
        lines.append("VERDICT: GO")
    elif result.kind == "check":
        lines.append(
            f"VERDICT: HOLD ({counts['REFUSE']} refused, {counts['ABSTAIN']} abstained "
            f"of {len(result.outcomes)})"
        )
    else:
        lines.append(f"VERDICT: HOLD ({counts['ABSTAIN']} abstained of {len(result.outcomes)})")
    return "\n".join(lines)
