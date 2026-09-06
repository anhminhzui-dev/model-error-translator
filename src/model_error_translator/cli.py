"""Command line. Exit 0 = GO, 2 = HOLD, 1 = bad usage or a crash.

A crash is not a pass: an unhandled exception still prints a HOLD line before it leaves.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .core import (
    DEFAULT_CHECKS,
    BatchResult,
    HaltError,
    check_batch,
    explain_batch,
    load_manifest,
    receipt_row,
    render,
    summary_document,
)

EXIT_GO = 0
EXIT_USAGE = 1
EXIT_HOLD = 2


def write_receipt(out_dir: str, result: BatchResult) -> None:
    """summary.json plus receipts.jsonl. Content only -- no timestamp, no host, no machine path,
    so two runs over the same inputs are byte-identical and a receipt can be compared."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    document: dict[str, Any] = summary_document(result, __version__)
    with (out / "summary.json").open("w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(document, indent=2, sort_keys=True) + "\n")
    with (out / "receipts.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
        for outcome in result.outcomes:
            handle.write(json.dumps(receipt_row(outcome), sort_keys=True) + "\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="model-error-translator",
        description="Refuse a bad model request before it is sent, and turn a raw model failure "
        "into one typed code, one artist sentence and one operator hint.",
    )
    parser.add_argument("--version", action="version", version=__version__)
    verbs = parser.add_subparsers(dest="verb", required=True)
    check_verb = verbs.add_parser("check", help="admit or refuse requests against a model manifest")
    check_verb.add_argument("--manifest", required=True)
    check_verb.add_argument("--requests", required=True)
    explain_verb = verbs.add_parser("explain", help="translate raw failure payloads")
    explain_verb.add_argument("--failures", required=True)
    for verb in (check_verb, explain_verb):
        verb.add_argument("--out", default=None, help="directory for summary.json + receipts.jsonl")
        verb.add_argument(
            "--allow-nonsynthetic",
            action="store_true",
            help="permit rows without the synthetic marker (off by default, on purpose)",
        )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return EXIT_GO if exc.code in (0, None) else EXIT_USAGE
    try:
        if args.verb == "check":
            manifest = load_manifest(args.manifest, args.allow_nonsynthetic)
            print(f"MANIFEST: {len(manifest['models'])} models  sha={manifest['sha256'][:16]}")
            result = check_batch(manifest, args.requests, DEFAULT_CHECKS, args.allow_nonsynthetic)
        else:
            result = explain_batch(args.failures, args.allow_nonsynthetic)
        print(render(result))
        if args.out:
            write_receipt(args.out, result)
        return EXIT_GO if result.verdict == "GO" else EXIT_HOLD
    except HaltError as halt:
        print(f"HALT: {halt}")
        print(f"VERDICT: HOLD (halt: {halt.code})")
        return EXIT_HOLD
    except Exception as exc:  # a crash must refuse out loud, never pass quietly
        print(f"VERDICT: HOLD (crash: {type(exc).__name__})")
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
