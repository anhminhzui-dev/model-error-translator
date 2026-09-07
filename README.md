# model-error-translator

Refuses a bad model request before it is sent and turns a raw model failure into one artist-actionable sentence plus a hashed receipt.

[![CI](https://github.com/anhminhzui-dev/model-error-translator/actions/workflows/ci.yml/badge.svg)](https://github.com/anhminhzui-dev/model-error-translator/actions/workflows/ci.yml) [![Licence: evaluation-only](https://img.shields.io/badge/licence-evaluation--only-lightgrey)](LICENSE)

## Why this exists

> "…translates technical model behavior into validation and error messages an artist can actually act on … turning \"403: proxy model failed with [800 lines of garbage]\" into \"Your reference image needs to be at least 1024px wide and 32-bit; yours is 859px and 24-bit.\"" — Griptape (Foundry), Software Engineer, Model Integrations (job posting)

Built for this posting, in a day, to show the shape of what I would do on day one.

## To the Griptape team

This is the layer between a node graph and a hosted model. It does two things and refuses to do a third. **Before** a call it validates the request against a model manifest and refuses the bad send with a typed code and a sentence the artist can act on — the call never leaves the workstation. **After** a failed call it maps the raw failure payload to one typed code, one artist sentence and one operator hint, hashes the raw body into a receipt, and shows the artist none of it. **It never guesses**: a failure shape the rules do not recognise comes back `ABSTAIN_UNRECOGNISED` with its receipt id, because a guessed cause costs an artist a day of re-rendering the wrong thing.

Sixty seconds to check it yourself: copy the tree, run `python -m pytest -q`, then the three commands under **Try it in 60 seconds**. The first returns GO and exit 0; the other two refuse and exit 2. Standard library only — nothing to install but `pytest`.

Everything under `fixtures/` is invented for this repository. No real model, service, studio, artist or asset appears anywhere in it, and nothing here calls the network.

## What a Nodes user sees, before and after

| | before | after |
|---|---|---|
| 859 px, 24-bit reference | the call goes out, comes back `403`, and the node shows a proxy dump | **refused before the call:** "Your reference image needs to be at least 1024 px on the short side and 16- or 32-bit; yours is 859 px and 24-bit." |
| a proxy failure is reported | 60,214 bytes of HTML, stack frames and gateway traces (800 lines) in the node's error field | "The service reported it could not run synthetic-refiner-xl. The cause and completion state are unknown. Ask your pipeline TD to check before retrying; local correlation reference `rcpt-a7fa898ed8bcf340`." |
| the service reports a rate limit | `429` and a raw header block | "The service reported a rate limit. Wait about 37 seconds before retrying. Queue and completion status are unknown." |
| a request times out | no confirmed result | "The request timed out; completion is unknown. Check whether it completed before retrying." The full message also supplies a local correlation reference. |
| a shape nobody has seen before | a guess, or a stack trace | `ABSTAIN` — "Something went wrong with this send and this node will not guess at what. Give your pipeline TD local correlation reference `rcpt-82fce3cbf0ea806d`." |

The operator hint is the other half: `PROXY_MODEL_FAILED: status=403 transport=none body_bytes=60214 sha256=a7fa898e… -- local correlation reference rcpt-a7fa898ed8bcf340; provider-log lookup is not verified. Raw body omitted.` The reference is generated locally from the supplied failure payload. It is not a provider-issued request id or proof that any log retained the body. A host integration decides where to display these fields; this prototype has no queue, retry executor or remote-completion check.

## The codes

Seven refusals before the call, seven translations after it. Each one is a rule you can read, switch off, and watch the gate miss what it used to catch.

| admission code | what triggers it | failure code | what triggers it |
|---|---|---|---|
| `UNKNOWN_MODEL` | the request names a model the manifest does not publish | `PROXY_MODEL_FAILED` | HTTP 403 carrying the proxy's upstream-failure marker |
| `MISSING_FIELD` | a field the model declares required is absent or empty | `PAYLOAD_REJECTED` | HTTP 413 — the service refused the size |
| `IMAGE_TOO_SMALL` | short side below the model's minimum | `RATE_LIMITED` | HTTP 429; `Retry-After` is parsed as an integer or not used at all |
| `IMAGE_TOO_LARGE` | long side above the model's maximum | `MODEL_SERVER_ERROR` | HTTP 500, 502 or 503 |
| `BIT_DEPTH_UNSUPPORTED` | bit depth outside the model's allowed list | `MALFORMED_RESPONSE` | HTTP 200 whose body is not the JSON it claims |
| `PAYLOAD_TOO_LARGE` | request bytes above the model's per-call ceiling | `REQUEST_TIMEOUT` | no status at all; the transport gave up |
| `MALFORMED_REQUEST` | the request row is not shaped like a request → ABSTAIN | `ABSTAIN_UNRECOGNISED` | anything else, including a 403 without the marker → ABSTAIN |

The last row of each column is the point of the whole thing. A 403 that carries the proxy marker is `PROXY_MODEL_FAILED`; a 403 that does not is `ABSTAIN_UNRECOGNISED`, not a nearest-neighbour guess.

**Verdict law:** a batch is GO only if every request was admitted and every failure was translated to a known code. A refusal, or one unrecognised shape, is HOLD and exit 2.

## Try it in 60 seconds

```
$ PYTHONPATH=src python -m model_error_translator.cli check --manifest fixtures/manifest.json --requests fixtures/requests_clean.jsonl
MANIFEST: 2 models  sha=63549deb8083f1f2
CHECKED: 3 requests  admitted=3 refused=0 abstained=0
  req-clean-01  ADMIT
  req-clean-02  ADMIT
  req-clean-03  ADMIT
CODES: none
VERDICT: GO
$ echo $?
0

$ PYTHONPATH=src python -m model_error_translator.cli check --manifest fixtures/manifest.json --requests fixtures/requests_bad.jsonl
MANIFEST: 2 models  sha=63549deb8083f1f2
CHECKED: 6 requests  admitted=0 refused=5 abstained=1
  req-bad-859       REFUSE     IMAGE_TOO_SMALL,BIT_DEPTH_UNSUPPORTED
      artist:   Your reference image needs to be at least 1024 px on the short side and 16- or 32-bit; yours is 859 px and 24-bit.
      operator: IMAGE_TOO_SMALL,BIT_DEPTH_UNSUPPORTED: request req-bad-859 refused before any call went out; synthetic-refiner-xl limits short_side>=1024px long_side<=4096px bit_depth in [16, 32] payload<=8388608B.
  ... four more refusals, one ABSTAIN ...
CODES: BIT_DEPTH_UNSUPPORTED=1 IMAGE_TOO_LARGE=1 IMAGE_TOO_SMALL=1 MALFORMED_REQUEST=1 MISSING_FIELD=1 PAYLOAD_TOO_LARGE=1 UNKNOWN_MODEL=1
VERDICT: HOLD (5 refused, 1 abstained of 6)
$ echo $?
2

$ PYTHONPATH=src python -m model_error_translator.cli explain --failures fixtures/failures.jsonl --out runs/demo
TRANSLATED: 7 failures  translated=6 abstained=1
  fail-403-proxy      TRANSLATE  rcpt-a7fa898ed8bcf340  PROXY_MODEL_FAILED
  ... five more, then ...
  fail-403-unknown    ABSTAIN    rcpt-82fce3cbf0ea806d  ABSTAIN_UNRECOGNISED
CODES: ABSTAIN_UNRECOGNISED=1 MALFORMED_RESPONSE=1 MODEL_SERVER_ERROR=1 PAYLOAD_REJECTED=1 PROXY_MODEL_FAILED=1 RATE_LIMITED=1 REQUEST_TIMEOUT=1
VERDICT: HOLD (1 abstained of 7)
$ echo $?
2

$ python -m pytest -q
18 passed in 0.11s        # run 2026-09-07; includes the uncertainty and receipt regressions
```

`runs/demo/summary.json` and `runs/demo/receipts.jsonl` carry ids, codes, counts and hashes only — no body, no header values, no prompt, no path. Two runs over the same inputs write byte-identical receipts, and a test asserts it.

The suite covers all 7 admission codes and all 7 failure codes, the exact admission sentence, raw-body privacy, synthetic-input refusal, public-clean and network scans, and the recording-transport falsifier. Regression cases preserve the supplied retry interval, keep timeout completion unknown, and reject invented queue, side-effect and provider-log assurances. `test_falsifier_image_checks_disabled_send_the_859px_reference_to_the_model` switches the image checks off through the `checks=` seam and proves the 859 px reference then travels to a recording transport. These are synthetic mechanism tests, not production reliability evidence.

## How to add a model manifest

One JSON object per model id; the admission checks read nothing else, so adding a model is a data change, not a code change.

```json
{
  "manifest_version": 1,
  "synthetic": true,
  "models": {
    "your-model-id": {
      "required_fields": ["prompt", "image"],
      "min_short_side_px": 1024,
      "max_long_side_px": 4096,
      "allowed_bit_depths": [16, 32],
      "max_payload_bytes": 8388608
    }
  }
}
```

Every limit turns into its own code and its own clause in the artist sentence: several broken limits on one image produce one sentence ("needs to be at least 1024 px on the short side and 16- or 32-bit; yours is 859 px and 24-bit"), not three error dialogs. `python -m model_error_translator.cli check` is the same code path a node would call in-process — `dispatch(request, manifest, transport)` is the seam, and the transport is reached only on ADMIT.

## Boundaries

Built for one posting, in a day: this is a design sample, not maintained software. No accuracy is claimed here and none is computable from what ships here. The manifests, requests and failure payloads are invented for this repository, a synthetic deck — including the 800-line 403 body, which is synthetic garbage generated to be as unreadable as the real thing. There is no network code path and no subprocess, and a test greps `src/` to keep it that way. Every constant is a design choice of this prototype, not a validated operating point: the two model profiles, the six raw failure shapes, the choice to hash the whole payload rather than the body alone, and the decision that an unrecognised 403 abstains instead of borrowing the nearest code. 7 of 7 failure payloads here are matched or abstained by construction, because I wrote both the matchers and the payloads — the honest number is the one measured against a real proxy's failure corpus, which I do not have.

## What I would do on day one at Griptape

Ask for three things: the model manifests as they exist today, a week of real failure bodies off the proxy, and the five error messages artists complain about most. Then run this matcher table over that corpus and report one number with its denominator — the share of real failure bodies that reach a named code — and treat the abstain rate as the thing that has to fall, one named shape at a time, rather than hiding misses behind a catch-all message. Manifests move into the model integration itself so a new checkpoint ships its limits with it, and every artist-facing string gets read out loud by someone who does not know what a proxy is. What I would not ship is a translator that has never been shown to miss: the falsifier test in this repository is the habit, not the demo.

## Licence

Source-available, evaluation-only — read it, run it, quote it in a review; see `LICENSE`.
