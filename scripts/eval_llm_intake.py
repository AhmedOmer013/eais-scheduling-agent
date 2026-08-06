"""Evaluation harness for the Groq-backed LLM intake path (see EXTENSIONS.md #1).

Runs every example in `training/clinic_examples.jsonl` and
`training/restaurant_examples.jsonl` (40 labeled examples total -- the
project's own existing fixture set, not a new synthetic dataset) through:

  1. `OfflineIntake` -- free, deterministic, no network. Also the
     ground-truth source for `start_time`: the training files label dates
     as human strings ("next Tuesday"), and `OfflineIntake`'s date
     resolution is already fully unit-tested (T12), so it is a trustworthy
     "compute this the same way `--llm` mode is meant to compute it"
     reference for the LLM path to be checked against.
  2. The real Groq-backed `LLMIntake`, via an instrumented HTTP client
     that records latency and token usage per call without changing
     `OpenAICompatibleHTTPClient` itself.

Both paths are also run through a real `SchedulingAgentCore` (fresh
manifests/store per example) to capture CONFIRMED/PENDING_APPROVAL rates,
not just raw field extraction.

Never run in CI: makes real network calls against a rate-limited hosted
API. Requires `EAIS_LLM_BASE_URL` / `EAIS_LLM_MODEL` / `EAIS_LLM_API_KEY`
already set in the environment (see README's "Configuring the LLM
backend"). Paces requests to stay under Groq's free-tier 12,000
tokens/minute limit, and hard-stops if the run would exceed
`_MAX_REQUEST_FRACTION` (30%) of the observed daily request allowance,
reading the real remaining quota off Groq's `x-ratelimit-*` response
headers rather than a hardcoded guess.

Usage:
    python scripts/eval_llm_intake.py [--out results.json]
"""

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from eais_scheduling_agent import wiring  # noqa: E402
from eais_scheduling_agent.core.audit import JsonLinesAuditTrail  # noqa: E402
from eais_scheduling_agent.core.gate import StandardApprovalGate  # noqa: E402
from eais_scheduling_agent.core.orchestrator import SchedulingAgentCore  # noqa: E402
from eais_scheduling_agent.core.store import InMemoryBookingStore  # noqa: E402
from eais_scheduling_agent.intake.llm import LLMIntake  # noqa: E402
from eais_scheduling_agent.intake.offline import OfflineIntake  # noqa: E402

_TRAINING_DIR = Path(__file__).resolve().parent.parent / "training"
_EVAL_AUDIT_FILE = Path(__file__).resolve().parent.parent / "audit.eval.jsonl"

#: Fraction of Groq's free-tier daily request allowance this run is
#: allowed to use, per the explicit instruction this script exists to
#: satisfy. Checked against the real `x-ratelimit-limit-requests` header,
#: not a hardcoded number.
_MAX_REQUEST_FRACTION = 0.30

#: Comparable fields: the subset of llm.py's schema this harness knows
#: how to score directly against the training files' `expected` dict.
#: `sector`/`reason`/`time_confidence` in the training files are metadata,
#: not extractable fields, and are intentionally not in this list.
_DIRECT_FIELDS = (
    "practitioner",
    "patient_name",
    "customer_name",
    "party_size",
    "seating_preference",
    "occasion",
    "time_period",
    "action",
    "urgency",
)


def _load_dataset() -> List[dict]:
    examples = []
    for filename in ("clinic_examples.jsonl", "restaurant_examples.jsonl"):
        for line in (_TRAINING_DIR / filename).read_text(encoding="utf-8").splitlines():
            if line.strip():
                examples.append(json.loads(line))
    return examples


class _InstrumentedClient:
    """Real OpenAI-compatible HTTP call, identical request shape to
    `OpenAICompatibleHTTPClient`, plus latency/token/rate-limit capture
    for evaluation purposes. Not used in production -- kept separate so
    `intake/llm.py` stays exactly as simple as its own docstring claims.
    """

    def __init__(self, base_url: str, model: str, api_key: Optional[str], timeout: float):
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._model = model
        self._api_key = api_key
        self._timeout = timeout
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, prompt: str) -> str:
        payload = json.dumps(
            {
                "model": self._model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "User-Agent": "eais-scheduling-agent-eval/1.0",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        request = urllib.request.Request(self._url, data=payload, headers=headers, method="POST")

        record: Dict[str, Any] = {"error": None, "usage": {}, "rate_headers": {}}
        start = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=self._timeout) as response:
                record["rate_headers"] = {
                    k: v for k, v in response.getheaders() if k.lower().startswith("x-ratelimit")
                }
                body = response.read().decode("utf-8")
            record["latency_s"] = time.perf_counter() - start
            envelope = json.loads(body)
            record["usage"] = envelope.get("usage", {})
            content = envelope["choices"][0]["message"]["content"]
            record["content"] = content
            self.calls.append(record)
            return content
        except Exception as exc:  # noqa: BLE001 -- mirrors OpenAICompatibleHTTPClient's contract
            record["latency_s"] = time.perf_counter() - start
            record["error"] = repr(exc)
            self.calls.append(record)
            raise


def _check_request_budget(client: _InstrumentedClient, examples_remaining: int) -> None:
    """Hard-stop if continuing would exceed 30% of Groq's real daily
    request allowance, reading the actual limit from the last response's
    `x-ratelimit-limit-requests` header rather than assuming a number.
    """
    if not client.calls:
        return
    headers = client.calls[-1]["rate_headers"]
    limit = headers.get("x-ratelimit-limit-requests")
    remaining = headers.get("x-ratelimit-remaining-requests")
    if limit is None or remaining is None:
        return
    limit, remaining = int(limit), int(remaining)
    used = limit - remaining
    budget = limit * _MAX_REQUEST_FRACTION
    if used + examples_remaining > budget:
        raise RuntimeError(
            f"Aborting: {used} requests used + {examples_remaining} remaining would "
            f"exceed {_MAX_REQUEST_FRACTION:.0%} of the {limit}/day allowance "
            f"(budget: {budget:.0f})."
        )


def _values_match(expected: Any, actual: Any) -> bool:
    if isinstance(expected, str) and isinstance(actual, str):
        return expected.strip().lower() == actual.strip().lower()
    return expected == actual


def _score_fields(expected: dict, actual_fields: dict) -> Dict[str, int]:
    """Field-level TP/FP/FN for one example's direct (non-date) fields."""
    tp = fp = fn = 0
    for field in _DIRECT_FIELDS:
        exp = expected.get(field)
        got = actual_fields.get(field)
        if exp is not None:
            if got is not None and _values_match(exp, got):
                tp += 1
            elif got is not None:
                fp += 1  # wrong value where a value was expected
                fn += 1  # ...which also means the right value is missing
            else:
                fn += 1
        else:
            if got is not None:
                fp += 1  # hallucinated a field that shouldn't be there
    return {"tp": tp, "fp": fp, "fn": fn}


def _score_start_time(expected_raw: dict, offline_fields: dict, actual_fields: dict) -> Optional[bool]:
    """Ground truth for `start_time`: `OfflineIntake`'s own resolution,
    anchored at the same reference time as the LLM call -- see module
    docstring. Returns None when no ground truth applies (i.e. the
    training label itself has no `time`, meaning omission is correct and
    is instead scored as a `_DIRECT_FIELDS`-style hallucination check by
    the caller).
    """
    if expected_raw.get("time") is None:
        return None
    expected_start = offline_fields.get("start_time")
    if expected_start is None:
        return None
    actual_start = actual_fields.get("start_time")
    return actual_start == expected_start


def _run_core(intake, sector: str, text: str, skill_packs) -> str:
    core = SchedulingAgentCore(
        manifest_dir=wiring.DEFAULT_MANIFEST_DIR,
        skill_packs=skill_packs,
        intake=intake,
        gate=StandardApprovalGate(),
        store=InMemoryBookingStore(),
        audit=JsonLinesAuditTrail(path=str(_EVAL_AUDIT_FILE)),
    )
    try:
        return core.handle(text, sector).status
    except Exception as exc:  # noqa: BLE001 -- record, don't crash the eval run
        return f"ERROR: {exc!r}"


def run_eval(pace_seconds: float = 5.0, limit: Optional[int] = None) -> dict:
    config = wiring.resolve_llm_config()
    if not config.get("api_key"):
        raise RuntimeError(
            "EAIS_LLM_API_KEY is not set -- this eval makes real hosted-LLM calls "
            "and needs real credentials. See README's 'Configuring the LLM backend'."
        )

    skill_packs = wiring.build_skill_packs()
    examples = _load_dataset()
    if limit is not None:
        examples = examples[:limit]
    client = _InstrumentedClient(**config)

    fixed_now = datetime.now()
    results = []
    totals = {"tp": 0, "fp": 0, "fn": 0}
    start_time_scored = start_time_correct = 0
    llm_path_used = 0
    llm_confirmed = offline_confirmed = 0

    for i, example in enumerate(examples):
        text = example["text"]
        sector = example["expected"]["sector"]
        expected = example["expected"]

        offline_intake = OfflineIntake(now=lambda: fixed_now)
        offline_fields = offline_intake.parse(text, sector).fields
        offline_status = _run_core(OfflineIntake(now=lambda: fixed_now), sector, text, skill_packs)
        if offline_status == "CONFIRMED":
            offline_confirmed += 1

        _check_request_budget(client, len(examples) - i)
        cached_llm = wiring.CachingIntake(
            LLMIntake(fallback=OfflineIntake(now=lambda: fixed_now), client=client, now=lambda: fixed_now)
        )
        llm_status = _run_core(cached_llm, sector, text, skill_packs)
        if llm_status == "CONFIRMED":
            llm_confirmed += 1
        llm_fields = cached_llm.parse(text, sector).fields  # cache hit, no extra call

        call_record = client.calls[-1] if client.calls else {}
        used_llm = call_record.get("error") is None and bool(call_record.get("content"))
        if used_llm:
            llm_path_used += 1

        field_score = _score_fields(expected, llm_fields)
        totals["tp"] += field_score["tp"]
        totals["fp"] += field_score["fp"]
        totals["fn"] += field_score["fn"]

        start_correct = _score_start_time(expected, offline_fields, llm_fields)
        if start_correct is not None:
            start_time_scored += 1
            if start_correct:
                start_time_correct += 1

        results.append(
            {
                "id": example["id"],
                "sector": sector,
                "text": text,
                "category": example.get("category"),
                "offline_fields": {k: str(v) for k, v in offline_fields.items()},
                "llm_fields": {k: str(v) for k, v in llm_fields.items()},
                "llm_call_used": used_llm,
                "llm_call_error": call_record.get("error"),
                "llm_latency_s": call_record.get("latency_s"),
                "llm_tokens_total": call_record.get("usage", {}).get("total_tokens"),
                "offline_decision": offline_status,
                "llm_decision": llm_status,
                "field_score": field_score,
                "start_time_scored": start_correct is not None,
                "start_time_correct": bool(start_correct),
            }
        )

        print(
            f"[{i + 1}/{len(examples)}] {example['id']}: "
            f"llm_used={used_llm} llm_decision={llm_status} "
            f"tokens={call_record.get('usage', {}).get('total_tokens')} "
            f"latency={call_record.get('latency_s', 0):.2f}s"
        )

        if i < len(examples) - 1:
            time.sleep(pace_seconds)

    n = len(examples)
    precision = totals["tp"] / (totals["tp"] + totals["fp"]) if (totals["tp"] + totals["fp"]) else None
    recall = totals["tp"] / (totals["tp"] + totals["fn"]) if (totals["tp"] + totals["fn"]) else None
    f1 = (2 * precision * recall / (precision + recall)) if precision and recall else None

    latencies = [c["latency_s"] for c in client.calls if c.get("latency_s") is not None]
    latencies_sorted = sorted(latencies)
    total_tokens = sum(c.get("usage", {}).get("total_tokens", 0) or 0 for c in client.calls)
    last_headers = client.calls[-1]["rate_headers"] if client.calls else {}

    summary = {
        "dataset_size": n,
        "requests_made": len(client.calls),
        "requests_daily_limit": last_headers.get("x-ratelimit-limit-requests"),
        "requests_remaining_after_run": last_headers.get("x-ratelimit-remaining-requests"),
        "fraction_of_daily_allowance_used": (
            len(client.calls) / int(last_headers["x-ratelimit-limit-requests"])
            if last_headers.get("x-ratelimit-limit-requests")
            else None
        ),
        "total_tokens_used": total_tokens,
        "llm_path_used_rate": llm_path_used / n,
        "llm_path_fallback_rate": 1 - (llm_path_used / n),
        "field_precision": precision,
        "field_recall": recall,
        "field_f1": f1,
        "field_tp": totals["tp"],
        "field_fp": totals["fp"],
        "field_fn": totals["fn"],
        "start_time_accuracy": (
            start_time_correct / start_time_scored if start_time_scored else None
        ),
        "start_time_scored_examples": start_time_scored,
        "offline_confirmed_rate": offline_confirmed / n,
        "llm_confirmed_rate": llm_confirmed / n,
        "latency_avg_s": sum(latencies) / len(latencies) if latencies else None,
        "latency_p50_s": latencies_sorted[len(latencies_sorted) // 2] if latencies_sorted else None,
        "latency_p95_s": (
            latencies_sorted[int(len(latencies_sorted) * 0.95)] if latencies_sorted else None
        ),
        "latency_max_s": max(latencies) if latencies else None,
        "model": config["model"],
        "base_url": config["base_url"],
        "run_timestamp": datetime.now().isoformat(),
    }

    return {"summary": summary, "results": results}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="scripts/eval_results.json", metavar="PATH")
    parser.add_argument("--pace-seconds", type=float, default=5.0)
    parser.add_argument(
        "--limit", type=int, default=None, metavar="N", help="Only run the first N examples."
    )
    args = parser.parse_args()

    report = run_eval(pace_seconds=args.pace_seconds, limit=args.limit)

    out_path = Path(args.out)
    out_path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print("\n=== Summary ===")
    for key, value in report["summary"].items():
        print(f"{key}: {value}")
    print(f"\nFull report written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
