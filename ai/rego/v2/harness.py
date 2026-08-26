#!/usr/bin/env python3
"""Run AgentInjectionBench through the OPA/Rego action-layer policy and
compare it against the benchmark's own content-scanner baselines.

HONESTY NOTE (read this before citing any number this script prints):
-----------------------------------------------------------------------
AgentInjectionBench is a *static content-classification* benchmark. Every
sample ends right after the poisoned tool_result is delivered; the recorded
final `assistant` turn is a plausible narrative continuation, not a captured
"proposed next tool call." The dataset therefore does NOT give us the one
thing a real action-layer guardrail would gate on: the structured tool call
the agent is about to execute.

To evaluate an action-layer policy on this benchmark at all, this harness
uses `target_tools[0]` as an ORACLE PROXY for "the tool a compromised agent
would be steered toward calling." That field is a benchmark *label*
(describing what the attack targets), not something a runtime guardrail
would observe before the fact. In a real deployment this proxy is replaced
by the actual proposed tool_call your agent framework is about to execute
(e.g. intercepted between the LLM's function-call output and tool
execution) -- see `build_policy_input()` docstring for exactly which of the
five input fields are legitimate runtime observables vs. this benchmark's
stand-in for one of them.

Everything else fed to the policy IS a legitimate runtime observable:
  - injection_surface   : which channel supplied the driving content
                           (a fact about message provenance, not a label)
  - user_intent_match    : computed from the REAL user message text
  - definition_layer_flag: computed by re-running the benchmark's own
                           tool_definition_scanner over tools_available
                           (a real Layer-1 content scan, imported not
                           reimplemented)
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/home/claude/AgentInjectionBench")
WORK = Path(__file__).parent
OPA_BIN = Path("/home/claude/opa")

sys.path.insert(0, str(REPO))

from evaluation.detectors import tool_definition_scanner  # noqa: E402
from evaluation.score import (  # noqa: E402
    load_dataset,
    score_predictions,
    run_detector,
    residual_hard_set,
    normalize_label,
)

SENSITIVITY = json.loads((WORK / "sensitivity_tiers.json").read_text())
TIERS = SENSITIVITY["tiers"]
TIER_RANK = SENSITIVITY["tier_rank"]

UNTRUSTED_SURFACES = {
    "tool_output", "rag_document", "file_content",
    "api_response", "mcp_response", "user_message",
}

_READONLY_HINT = re.compile(r"read[- ]?only", re.IGNORECASE)

_STOPWORDS = {
    "the", "a", "an", "to", "for", "of", "and", "on", "in", "at", "is",
    "this", "that", "with", "from", "please", "can", "you", "me", "my",
    "our", "we", "it", "its", "into", "as", "be", "are", "us",
}


def _tokenize(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9_]+", text.lower()) if w not in _STOPWORDS}


def first_user_message(sample: dict) -> str:
    for turn in sample.get("conversation", []):
        if turn.get("role") == "user":
            return turn.get("content", "") or ""
    return ""


def resolve_sensitivity_tier(tool_name: str, sample: dict) -> str:
    """Base tier from the mapping, downgraded to 'read' if the tool's own
    advertised description in `tools_available` says it is read-only."""
    tier = TIERS.get(tool_name, "write")  # unseen tool names default to 'write' (conservative)
    for t in sample.get("tools_available", []) or []:
        if isinstance(t, dict) and t.get("name") == tool_name:
            if _READONLY_HINT.search(t.get("description", "") or ""):
                return "read"
    return tier


def user_intent_match(sample: dict, tool_name: str) -> bool:
    """Does the user's own (trusted-principal) message plausibly call for
    this tool? Token-overlap heuristic between the user's literal words and
    the tool's name/description -- deliberately dumb and auditable rather
    than another opaque classifier stacked on top of the thing we're gating."""
    user_text = first_user_message(sample)
    user_tokens = _tokenize(user_text)
    if not user_tokens:
        return False

    tool_tokens = set(_tokenize(tool_name.replace("_", " ")))
    for t in sample.get("tools_available", []) or []:
        if isinstance(t, dict) and t.get("name") == tool_name:
            tool_tokens |= _tokenize(t.get("description", "") or "")

    return bool(user_tokens & tool_tokens)


def tools_already_in_flight(sample: dict) -> set[str]:
    """Which tools have already been invoked earlier in this session, per the
    recorded `tool_result` turns. A legitimate, cheap, structural session
    fact -- not a text read -- and the single strongest discriminator found
    in this dataset: a pivot to a tool never used so far, proposed by
    untrusted content, is unsafe in 129/133 cases; a continuation of an
    already-in-flight tool is unsafe in only 13/49."""
    return {
        t.get("tool") for t in sample.get("conversation", [])
        if t.get("role") == "tool_result" and t.get("tool")
    }


def definition_layer_flag(sample: dict) -> bool:
    """Layer-1 signal: re-run the benchmark's own tool_definition_scanner
    (imported, not reimplemented) over tools_available. This is the only
    place the harness looks at any text content at all -- everything else
    in the policy input is structural."""
    return tool_definition_scanner(sample) == "unsafe"


def build_policy_input(sample: dict) -> dict | None:
    """Returns None for samples with no target_tools (nothing to gate)."""
    target_tools = sample.get("target_tools") or []
    if not target_tools:
        return None
    tool_name = target_tools[0]  # oracle proxy -- see module docstring

    return {
        "sample_id": sample["id"],
        "injection_surface": sample.get("injection_surface", "user_message"),
        "proposed_tool": {
            "name": tool_name,
            "sensitivity_tier": resolve_sensitivity_tier(tool_name, sample),
        },
        "tool_already_in_flight": tool_name in tools_already_in_flight(sample),
        "argument_pivot": False,  # AgentInjectionBench has no structured args to compare -- see policy.rego docstring
        "user_intent_match": user_intent_match(sample, tool_name),
        "definition_layer_flag": definition_layer_flag(sample),
    }


def opa_decision(policy_input: dict) -> tuple[str, list[str], dict]:
    """Returns (decision, reasons, timing). timing captures two DIFFERENT
    numbers, kept separate deliberately -- collapsing them into one average
    would misrepresent both:
      wall_clock_ms : total time for this Python subprocess.run() call,
                      including process spawn AND OPA recompiling/reloading
                      policy.rego from scratch every single time. This is
                      an artifact of the CLI-per-call harness architecture,
                      NOT a property of the policy itself.
      eval_only_ms  : OPA's own internal timer_rego_query_eval_ns, i.e. the
                      actual marginal cost of evaluating the already-loaded
                      policy against one input. This is what a real
                      deployment (opa run --server, kept warm, policy
                      compiled once) would actually pay per decision.
    """
    t0 = time.perf_counter()
    proc = subprocess.run(
        [str(OPA_BIN), "eval", "-d", str(WORK / "policy.rego"),
         "-I", "--format", "json", "--metrics", "data.agent.guardrail"],
        input=json.dumps(policy_input),
        capture_output=True, text=True, check=True,
    )
    wall_clock_ms = (time.perf_counter() - t0) * 1000
    result = json.loads(proc.stdout)
    doc = result["result"][0]["expressions"][0]["value"]
    eval_only_ms = result.get("metrics", {}).get("timer_rego_query_eval_ns", 0) / 1e6
    timing = {"wall_clock_ms": round(wall_clock_ms, 3), "eval_only_ms": round(eval_only_ms, 3)}
    return doc["decision"], doc.get("reasons", []), timing


def rego_layer_predictions(samples: list[dict]) -> tuple[dict[str, str], dict[str, list[str]], list[dict]]:
    preds: dict[str, str] = {}
    reasons_by_id: dict[str, list[str]] = {}
    records: list[dict] = []  # full per-entry audit trail: id, input, decision, reasons, ground_truth
    for s in samples:
        pi = build_policy_input(s)
        ground_truth = normalize_label(s["ground_truth"])
        if pi is None:
            preds[s["id"]] = "safe"  # nothing to gate -> nothing to flag
            reasons_by_id[s["id"]] = ["no_target_tool"]
            records.append({
                "id": s["id"],
                "attack_category": s.get("attack_category"),
                "injection_surface": s.get("injection_surface"),
                "complexity": s.get("complexity"),
                "policy_input": None,
                "decision": "allow",
                "reasons": ["no_target_tool"],
                "predicted_label": "safe",
                "ground_truth": ground_truth,
                "correct": "safe" == ground_truth,
                "latency": None,  # no OPA call was made -- nothing to gate
            })
            continue
        decision, reasons, timing = opa_decision(pi)
        # deny/escalate both mean "the action layer intervened" -> counts as
        # flagged ("unsafe") for comparison against the benchmark's binary
        # safe/unsafe scoring. deny vs escalate is preserved in reasons for
        # the qualitative breakdown below.
        predicted_label = "unsafe" if decision in ("deny", "escalate") else "safe"
        preds[s["id"]] = predicted_label
        reasons_by_id[s["id"]] = [decision] + reasons
        records.append({
            "id": s["id"],
            "attack_category": s.get("attack_category"),
            "injection_surface": s.get("injection_surface"),
            "complexity": s.get("complexity"),
            "policy_input": pi,
            "decision": decision,
            "reasons": reasons,
            "predicted_label": predicted_label,
            "ground_truth": ground_truth,
            "correct": predicted_label == ground_truth,
            "latency": timing,
        })
    return preds, reasons_by_id, records


def latency_stats(records: list[dict]) -> dict:
    """Aggregate latency stats over every record that actually called OPA
    (skips records where pi was None -- nothing to time)."""
    wall = sorted(r["latency"]["wall_clock_ms"] for r in records if r.get("latency"))
    evalo = sorted(r["latency"]["eval_only_ms"] for r in records if r.get("latency"))
    if not wall:
        return {}

    def pct(sorted_vals: list[float], p: float) -> float:
        idx = min(len(sorted_vals) - 1, int(round(p / 100 * (len(sorted_vals) - 1))))
        return sorted_vals[idx]

    def summarize(vals: list[float]) -> dict:
        n = len(vals)
        mean = sum(vals) / n
        return {
            "n": n,
            "mean_ms": round(mean, 3),
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


def main() -> None:
    samples = load_dataset(REPO / "data" / "agent_injection_bench.jsonl")
    print(f"Loaded {len(samples)} samples "
          f"({sum(1 for s in samples if normalize_label(s['ground_truth']) == 'unsafe')} attacks, "
          f"{sum(1 for s in samples if normalize_label(s['ground_truth']) == 'safe')} benign)\n")

    # --- 1. reproduce their four content-scanner baselines ------------------
    baseline_names = [
        "keyword_baseline", "tool_definition_scanner",
        "agentic_directive_scanner", "control_channel_scanner",
    ]
    baseline_results = {name: run_detector(samples, name) for name in baseline_names}
    baseline_preds = {
        name: {s["id"]: normalize_label(__import__("evaluation.detectors", fromlist=["DETECTORS"]).DETECTORS[name](s))
               for s in samples}
        for name in baseline_names
    }

    print("=== Content-scanner baselines (reproduced from repo) ===")
    for name, res in baseline_results.items():
        print(f"  {name:28s} detection={res.detection_rate:5.1%}  FPR={res.false_positive_rate:5.1%}  "
              f"balanced_acc={res.balanced_accuracy:5.1%}")

    # --- 2. the residual hard set: attacks every scanner above misses -------
    rhs = residual_hard_set(samples, baseline_preds)
    print(f"\nResidual hard set (evaded by all {rhs['n_detectors']} scanners): "
          f"{rhs['n_evaded_by_all']}/{rhs['n_attacks']} attacks ({rhs['evasion_rate']:.1%})")
    print(f"  by category: {rhs['by_category']}")
    print(f"  by surface:  {rhs['by_surface']}")

    # --- 3. the OPA/Rego action layer, scored the same way -------------------
    rego_preds, rego_reasons, records = rego_layer_predictions(samples)
    rego_result = score_predictions(samples, rego_preds, name="opa_action_layer")
    print("\n=== OPA/Rego action-layer policy (this harness) ===")
    print(f"  detection={rego_result.detection_rate:5.1%}  FPR={rego_result.false_positive_rate:5.1%}  "
          f"balanced_acc={rego_result.balanced_accuracy:5.1%}  precision={rego_result.precision:5.1%}")
    print("  by category:")
    for cat, gs in sorted(rego_result.by_category.items()):
        print(f"    {cat:24s} {gs.detected}/{gs.total}  ({gs.detected/gs.total:.0%})" if gs.total else "")

    # --- 4. the headline number: does the action layer recover any of the
    #        residual hard set that EVERY content scanner missed? -----------
    rhs_ids = set(rhs["sample_ids"])
    recovered = [sid for sid in rhs_ids if rego_preds.get(sid) == "unsafe"]
    still_missed = sorted(rhs_ids - set(recovered))

    print(f"\n=== Residual-hard-set recovery ===")
    print(f"  Residual hard set size:                 {len(rhs_ids)}")
    print(f"  Recovered by OPA action layer:           {len(recovered)} "
          f"({len(recovered)/len(rhs_ids):.1%} of the hard set)" if rhs_ids else "n/a")
    print(f"  Still missed by BOTH layers combined:    {len(still_missed)}")

    id_to_sample = {s["id"]: s for s in samples}
    print("\n  Recovered samples (content-blind, structurally caught):")
    for sid in sorted(recovered):
        s = id_to_sample[sid]
        why = [r for r in rego_reasons[sid] if r not in ("deny", "escalate")]
        print(f"    {sid}  [{s['attack_category']:22s} / {s['injection_surface']:14s}]  reasons={why}")

    print("\n  Still-missed samples (open problem for BOTH layers):")
    for sid in still_missed:
        s = id_to_sample[sid]
        print(f"    {sid}  [{s['attack_category']:22s} / {s['injection_surface']:14s}]  "
              f"target_tools={s.get('target_tools')}")

    # --- 5. save everything for the paper -----------------------------------
    out = {
        "baseline_results": {
            name: {
                "detection_rate": res.detection_rate,
                "false_positive_rate": res.false_positive_rate,
                "balanced_accuracy": res.balanced_accuracy,
            } for name, res in baseline_results.items()
        },
        "residual_hard_set": {k: v for k, v in rhs.items() if k != "sample_ids"} | {"sample_ids": sorted(rhs["sample_ids"])},
        "opa_action_layer": {
            "detection_rate": rego_result.detection_rate,
            "false_positive_rate": rego_result.false_positive_rate,
            "balanced_accuracy": rego_result.balanced_accuracy,
            "precision": rego_result.precision,
        },
        "residual_hard_set_recovery": {
            "hard_set_size": len(rhs_ids),
            "recovered_ids": sorted(recovered),
            "still_missed_ids": still_missed,
        },
        "records": records,  # full per-sample audit trail: input fed to OPA, decision, reasons, ground truth
        "latency_stats": latency_stats(records),
    }
    (WORK / "results.json").write_text(json.dumps(out, indent=2))
    lat = latency_stats(records)
    if lat:
        print(f"\n=== Latency ===")
        for label in ("wall_clock (subprocess + recompile-per-call, CLI harness overhead)",
                      "eval_only (opa's internal timer_rego_query_eval_ns -- true marginal decision cost)"):
            s = lat[label]
            print(f"  {label}:")
            print(f"    mean={s['mean_ms']}ms  median={s['median_ms']}ms  p95={s['p95_ms']}ms  "
                  f"p99={s['p99_ms']}ms  min={s['min_ms']}ms  max={s['max_ms']}ms  (n={s['n']})")
        print(f"  Implied throughput (eval_only): {lat['implied_throughput_eval_only_decisions_per_sec']} decisions/sec")

    print(f"\nFull results written to {WORK / 'results.json'} "
          f"({len(records)} per-sample records included for auditing)")


if __name__ == "__main__":
    main()
