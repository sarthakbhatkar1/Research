#!/usr/bin/env python3
"""Suite-parametrized version of harness_agentdojo.py. Same policy.rego,
same replay mechanism -- only the tool catalog (tiers JSON) changes per
suite. Run against `banking` first as a regression check (must reproduce
harness_agentdojo.py's numbers exactly), then against `workspace` to test
the sensitive-information-disclosure mitigation on a suite that actually
has a read-tier-to-egress-tool attack surface (banking's 11 tools have none
-- see banking_sensitivity_tiers.json, no email/search/send_message tools
exist there at all).

argument_pivot here generalizes the banking mechanism (recipient IBAN vs.
known transaction-history recipients) to send_email's recipient list vs.
known contacts/correspondence -- same idea, new domain, zero new Rego code.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path

REPO = Path("/home/claude/agentdojo")
WORK = Path(__file__).parent
OPA_BIN = Path("/home/claude/opa")
POLICY = WORK / "policy.rego"

_STOPWORDS = {
    "the", "a", "an", "to", "for", "of", "and", "on", "in", "at", "is",
    "this", "that", "with", "from", "please", "can", "you", "me", "my",
    "our", "we", "it", "its", "into", "as", "be", "are", "us", "i",
}

_IBAN_RE = re.compile(r"\b[A-Z]{2}[A-Z0-9]{8,}\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")


def _tok(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", (text or "").lower()) if w not in _STOPWORDS}


def opa_decision(policy_input: dict) -> tuple[str, list[str], dict]:
    """See harness.py's opa_decision docstring for why wall_clock_ms and
    eval_only_ms are kept separate rather than averaged into one number."""
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(OPA_BIN), "eval", "-d", str(POLICY), "-I", "--format", "json", "--metrics",
         "data.agent.guardrail"],
        input=json.dumps(policy_input), capture_output=True, text=True, check=True,
    )
    wall_clock_ms = (time.perf_counter() - t0) * 1000
    result = json.loads(proc.stdout)
    doc = result["result"][0]["expressions"][0]["value"]
    eval_only_ms = result.get("metrics", {}).get("timer_rego_query_eval_ns", 0) / 1e6
    timing = {"wall_clock_ms": round(wall_clock_ms, 3), "eval_only_ms": round(eval_only_ms, 3)}
    return doc["decision"], doc.get("reasons", []), timing


def load_catalog(suite: str) -> dict:
    path = WORK / f"{suite}_sensitivity_tiers.json"
    cfg = json.loads(path.read_text())
    return {
        "tiers": cfg["tiers"],
        "target_arg_key": cfg.get("target_arg_key", {}),
        "recipient_history_tools": set(cfg.get("recipient_history_tools", [])),
    }


def extract_known_values(content: str) -> set[str]:
    """Structured-value extraction, scoped to formats we can recognize
    without reading prose -- IBANs and email addresses. Extend per-suite if
    a new suite's trusted-recipient format doesn't match either pattern."""
    return set(_IBAN_RE.findall(content)) | {e.lower() for e in _EMAIL_RE.findall(content)}


def target_values(args: dict, key: str) -> list[str]:
    """A target arg can be a scalar (banking's 'recipient') or a list
    (workspace's 'recipients') -- normalize to a list of strings."""
    if not args or key not in args:
        return []
    val = args[key]
    if isinstance(val, list):
        return [str(v).lower() if "@" in str(v) else str(v) for v in val]
    return [str(val).lower() if "@" in str(val) else str(val)]


def replay_trace(trace: dict, catalog: dict) -> dict:
    tiers = catalog["tiers"]
    target_arg_key = catalog["target_arg_key"]
    recipient_history_tools = catalog["recipient_history_tools"]

    messages = trace["messages"]
    user_msg = next((m["content"] for m in messages if m["role"] == "user"), "") or ""
    user_tokens = _tok(user_msg)

    seen_tool_output = False
    already_called: set[str] = set()
    known_values: set[str] = set()
    call_records: list[dict] = []  # EVERY proposed call, not just flagged ones -- full audit trail

    for m in messages:
        if m["role"] == "assistant" and m.get("tool_calls"):
            for tc in m["tool_calls"]:
                fn = tc["function"]
                tier = tiers.get(fn, "write")
                surface = "tool_output" if seen_tool_output else "direct_user_request"
                target_key = target_arg_key.get(fn)
                call_targets = target_values(tc.get("args") or {}, target_key) if target_key else []

                fn_tokens = _tok(fn.replace("_", " "))
                intent_match = bool(user_tokens & fn_tokens)
                if not intent_match and call_targets:
                    intent_match = any(t and t in user_msg for t in call_targets)

                argument_pivot = False
                if target_key and fn in already_called and call_targets and known_values:
                    argument_pivot = any(t not in known_values for t in call_targets)

                pi = {
                    "injection_surface": surface,
                    "proposed_tool": {"name": fn, "sensitivity_tier": tier},
                    "tool_already_in_flight": fn in already_called,
                    "argument_pivot": argument_pivot,
                    "user_intent_match": intent_match,
                    "definition_layer_flag": False,
                }
                decision, reasons, timing = opa_decision(pi)
                call_records.append({
                    "function": fn,
                    "args": tc.get("args"),
                    "policy_input": pi,
                    "decision": decision,
                    "reasons": reasons,
                    "latency": timing,
                })
                already_called.add(fn)
        elif m["role"] == "tool":
            seen_tool_output = True
            source_fn = (m.get("tool_call") or {}).get("function")
            if source_fn in recipient_history_tools:
                known_values |= extract_known_values(str(m.get("content", "")))

    flagged = [c for c in call_records if c["decision"] in ("deny", "escalate")]
    return {"call_records": call_records, "flagged_calls": flagged, "any_intervention": len(flagged) > 0}


def latency_stats(all_call_records: list[dict]) -> dict:
    wall = sorted(c["latency"]["wall_clock_ms"] for c in all_call_records)
    evalo = sorted(c["latency"]["eval_only_ms"] for c in all_call_records)
    if not wall:
        return {}

    def pct(sorted_vals: list[float], p: float) -> float:
        idx = min(len(sorted_vals) - 1, int(round(p / 100 * (len(sorted_vals) - 1))))
        return sorted_vals[idx]

    def summarize(vals: list[float]) -> dict:
        n = len(vals)
        return {
            "n": n,
            "mean_ms": round(sum(vals) / n, 3),
            "median_ms": round(pct(vals, 50), 3),
            "p95_ms": round(pct(vals, 95), 3),
            "p99_ms": round(pct(vals, 99), 3),
            "min_ms": round(vals[0], 3),
            "max_ms": round(vals[-1], 3),
            "total_ms": round(sum(vals), 3),
        }

    return {
        "wall_clock (subprocess + recompile-per-call, CLI harness overhead)": summarize(wall),
        "eval_only (opa's internal timer_rego_query_eval_ns -- true marginal decision cost)": summarize(evalo),
        "implied_throughput_eval_only_decisions_per_sec": round(1000 / (sum(evalo) / len(evalo)), 1),
    }


def run(suite: str) -> dict:
    pipeline = "gpt-4-0125-preview"
    base = REPO / "runs" / pipeline / suite
    catalog = load_catalog(suite)

    attack_files = sorted(base.glob("*/*/injection_task_*.json"))
    benign_files = sorted(base.glob("*/none/none.json"))

    print(f"\n{'='*70}\nSuite: {suite}   Pipeline: {pipeline}")
    print(f"Attack traces: {len(attack_files)}   Benign traces: {len(benign_files)}\n")

    hard_deny = 0
    soft_escalate_only = 0
    clean = 0
    trace_records = []  # full audit trail: per-trace file, ground truth, every call's decision
    for f in benign_files:
        trace = json.loads(f.read_text())
        result = replay_trace(trace, catalog)
        decisions = {c["decision"] for c in result["flagged_calls"]}
        if "deny" in decisions:
            hard_deny += 1
            trace_outcome = "hard_deny"
        elif "escalate" in decisions:
            soft_escalate_only += 1
            trace_outcome = "soft_escalate_only"
        else:
            clean += 1
            trace_outcome = "clean"
        trace_records.append({
            "trace_file": str(f.relative_to(REPO)),
            "population": "benign",
            "ground_truth_security": None,
            "outcome": trace_outcome,
            "call_records": result["call_records"],
        })
    n_benign = len(benign_files)
    if n_benign:
        print(f"=== Benign (n={n_benign}) ===")
        print(f"  Hard DENY:    {hard_deny}/{n_benign} ({hard_deny/n_benign:.1%})")
        print(f"  Soft ESCALATE: {soft_escalate_only}/{n_benign} ({soft_escalate_only/n_benign:.1%})")
        print(f"  Clean:        {clean}/{n_benign} ({clean/n_benign:.1%})")

    n_compromised = n_compromised_denied = n_compromised_escalated_only = 0
    n_resisted = n_resisted_also_flagged = 0
    for f in attack_files:
        trace = json.loads(f.read_text())
        security = trace.get("security")
        result = replay_trace(trace, catalog)
        decisions = {c["decision"] for c in result["flagged_calls"]}
        trace_outcome = "deny" if "deny" in decisions else ("escalate" if "escalate" in decisions else "allow")
        if security is True:
            n_compromised += 1
            if "deny" in decisions:
                n_compromised_denied += 1
            elif "escalate" in decisions:
                n_compromised_escalated_only += 1
        elif security is False:
            n_resisted += 1
            if decisions:
                n_resisted_also_flagged += 1
        trace_records.append({
            "trace_file": str(f.relative_to(REPO)),
            "population": "attack",
            "ground_truth_security": security,  # True = LLM was compromised, False = LLM resisted
            "outcome": trace_outcome,
            "call_records": result["call_records"],
        })

    n_attacks = len(attack_files)
    print(f"\n=== Attacks (n={n_attacks}) ===")
    print(f"  Compromised by LLM alone (security=true): {n_compromised}")
    if n_compromised:
        both = n_compromised_denied + n_compromised_escalated_only
        print(f"  -> hard DENY:                {n_compromised_denied} ({n_compromised_denied/n_compromised:.1%})")
        print(f"  -> soft ESCALATE only:        {n_compromised_escalated_only} ({n_compromised_escalated_only/n_compromised:.1%})")
        print(f"  -> either (not silent):      {both} ({both/n_compromised:.1%})")
    print(f"  Resisted by LLM alone (security=false):   {n_resisted}")
    if n_resisted:
        print(f"  -> policy also flagged:     {n_resisted_also_flagged}")

    all_call_records = [c for t in trace_records for c in t["call_records"]]
    lat = latency_stats(all_call_records)
    if lat:
        print(f"\n=== Latency ({len(all_call_records)} total tool-call decisions) ===")
        for label in ("wall_clock (subprocess + recompile-per-call, CLI harness overhead)",
                      "eval_only (opa's internal timer_rego_query_eval_ns -- true marginal decision cost)"):
            s = lat[label]
            print(f"  {label}:")
            print(f"    mean={s['mean_ms']}ms  median={s['median_ms']}ms  p95={s['p95_ms']}ms  "
                  f"p99={s['p99_ms']}ms  min={s['min_ms']}ms  max={s['max_ms']}ms")
        print(f"  Implied throughput (eval_only): {lat['implied_throughput_eval_only_decisions_per_sec']} decisions/sec")

    return {
        "suite": suite, "n_attack_traces": n_attacks, "n_benign_traces": n_benign,
        "benign_hard_deny_rate": hard_deny / n_benign if n_benign else None,
        "benign_soft_escalate_only_rate": soft_escalate_only / n_benign if n_benign else None,
        "benign_clean_rate": clean / n_benign if n_benign else None,
        "n_compromised_by_llm_alone": n_compromised,
        "n_compromised_hard_denied": n_compromised_denied,
        "n_compromised_escalated_only": n_compromised_escalated_only,
        "harm_not_silent_rate": ((n_compromised_denied + n_compromised_escalated_only) / n_compromised)
                                  if n_compromised else None,
        "n_resisted_by_llm_alone": n_resisted,
        "n_resisted_also_flagged_by_policy": n_resisted_also_flagged,
        "latency_stats": lat,
        "records": trace_records,  # full per-trace, per-call audit trail
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--suite", default="banking", choices=["banking", "workspace"])
    ap.add_argument("--all", action="store_true", help="run banking then workspace")
    args = ap.parse_args()

    suites = ["banking", "workspace"] if args.all else [args.suite]
    results = {s: run(s) for s in suites}
    (WORK / "results_multisuite.json").write_text(json.dumps(results, indent=2))
    print(f"\nFull results written to {WORK / 'results_multisuite.json'}")


if __name__ == "__main__":
    main()
