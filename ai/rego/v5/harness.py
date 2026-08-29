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

import argparse
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

import intent_scanner  # noqa: E402  (local module, imported after sys.path setup)

SPLIT_FILE = WORK / "split.json"


def load_split() -> dict[str, str] | None:
    """Load the deterministic dev/test split produced by make_split.py.
    Returns None if it hasn't been generated yet, in which case the harness
    reports whole-dataset numbers and says so."""
    if not SPLIT_FILE.exists():
        return None
    return json.loads(SPLIT_FILE.read_text())["split"]
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


def build_policy_input(sample: dict, include_unvalidated: bool = False) -> dict | None:
    """Returns None for samples with no target_tools (nothing to gate).

    include_unvalidated: when True, additionally computes the
    `untrusted_claims_user_intent` field that drives policy.rego's a-priori
    principal-impersonation rule. Defaults to False so every headline metric
    uses only dev-validated signals -- see intent_scanner.UNVALIDATED_SIGNALS
    for why that rule is excluded from reported numbers.
    """
    target_tools = sample.get("target_tools") or []
    if not target_tools:
        return None
    tool_name = target_tools[0]  # oracle proxy -- see module docstring

    pi = {
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

    if include_unvalidated:
        untrusted = intent_scanner.untrusted_text_from_sample(sample)
        fn = intent_scanner.UNVALIDATED_SIGNALS["untrusted_claims_user_intent"]
        pi["untrusted_claims_user_intent"] = bool(fn(untrusted))

    return pi


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
    """Per-sample evaluation with a full latency breakdown captured during the
    real run, on the real samples -- not a synthetic benchmark.

    Every record carries a `latency` dict with four phases, so the reported
    cost is the whole guardrail, not just the policy engine:
      build_ms      : building the policy input from the sample (tier lookup,
                      session-state scan, intent match, definition scanner).
      content_ms    : intent_scanner regex pass over untrusted text.
      opa_wall_ms   : full subprocess round trip (spawn + recompile + eval).
      opa_eval_ms   : OPA's own timer_rego_query_eval_ns, the marginal cost of
                      evaluating an already-compiled policy.
      total_ms      : build + content + opa_wall, i.e. what this harness
                      actually spent on this sample end to end.
      total_warm_ms : build + content + opa_eval, i.e. the deployment estimate
                      where OPA is kept warm instead of respawned per call.
    """
    preds: dict[str, str] = {}
    reasons_by_id: dict[str, list[str]] = {}
    records: list[dict] = []  # full per-entry audit trail: id, input, decision, reasons, ground_truth
    for s in samples:
        t_build = time.perf_counter()
        pi = build_policy_input(s)
        build_ms = (time.perf_counter() - t_build) * 1000

        t_scan = time.perf_counter()
        content_flagged, content_signals = intent_scanner.flags_sample(s)
        content_ms = (time.perf_counter() - t_scan) * 1000

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
                "content_signals": content_signals,
                "predicted_label": "safe",
                "ground_truth": ground_truth,
                "correct": "safe" == ground_truth,
                "latency": {
                    "build_ms": round(build_ms, 4),
                    "content_ms": round(content_ms, 4),
                    "opa_wall_ms": None,  # no OPA call was made -- nothing to gate
                    "opa_eval_ms": None,
                    "total_ms": round(build_ms + content_ms, 4),
                    "total_warm_ms": round(build_ms + content_ms, 4),
                },
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
        opa_wall = timing["wall_clock_ms"]
        opa_eval = timing["eval_only_ms"]
        records.append({
            "id": s["id"],
            "attack_category": s.get("attack_category"),
            "injection_surface": s.get("injection_surface"),
            "complexity": s.get("complexity"),
            "policy_input": pi,
            "decision": decision,
            "reasons": reasons,
            "content_signals": content_signals,
            "predicted_label": predicted_label,
            "ground_truth": ground_truth,
            "correct": predicted_label == ground_truth,
            "latency": {
                "build_ms": round(build_ms, 4),
                "content_ms": round(content_ms, 4),
                "opa_wall_ms": opa_wall,
                "opa_eval_ms": opa_eval,
                "total_ms": round(build_ms + content_ms + opa_wall, 4),
                "total_warm_ms": round(build_ms + content_ms + opa_eval, 4),
            },
        })
    return preds, reasons_by_id, records


def latency_stats(records: list[dict]) -> dict:
    """Aggregate the per-phase latency captured during the real run."""
    def pct(vals: list[float], p: float) -> float:
        idx = min(len(vals) - 1, int(round(p / 100 * (len(vals) - 1))))
        return vals[idx]

    def summarize(vals: list[float]) -> dict:
        vals = sorted(vals)
        n = len(vals)
        return {
            "n": n,
            "mean_ms": round(sum(vals) / n, 4),
            "median_ms": round(pct(vals, 50), 4),
            "p95_ms": round(pct(vals, 95), 4),
            "p99_ms": round(pct(vals, 99), 4),
            "min_ms": round(vals[0], 4),
            "max_ms": round(vals[-1], 4),
            "total_ms": round(sum(vals), 4),
        }

    phases = ["build_ms", "content_ms", "opa_wall_ms", "opa_eval_ms",
              "total_ms", "total_warm_ms"]
    out: dict = {}
    for phase in phases:
        vals = [r["latency"][phase] for r in records
                if r.get("latency") and r["latency"].get(phase) is not None]
        if vals:
            out[phase] = summarize(vals)

    if "total_warm_ms" in out:
        out["implied_throughput_warm_per_sec"] = round(
            1000 / out["total_warm_ms"]["mean_ms"], 1)
    return out


def _confusion(tp: int, fp: int, tn: int, fn: int) -> dict:
    import math
    det = tp / (tp + fn) if (tp + fn) else 0.0
    fpr = fp / (fp + tn) if (fp + tn) else 0.0
    prec = tp / (tp + fp) if (tp + fp) else 0.0
    acc = (tp + tn) / (tp + tn + fp + fn) if (tp + tn + fp + fn) else 0.0
    den = math.sqrt((tp + fp) * (tp + fn) * (tn + fp) * (tn + fn))
    mcc = ((tp * tn - fp * fn) / den) if den else 0.0
    return {
        "tp": tp, "fp": fp, "tn": tn, "fn": fn,
        "detection": det, "fpr": fpr, "precision": prec,
        "accuracy": acc, "balanced_accuracy": (det + (1 - fpr)) / 2, "mcc": mcc,
    }


LAYER_CONFIGS = [
    # (key, human label, use_content_layer, use_unvalidated_rule)
    ("action_only", "action layer only (policy.rego)", False, False),
    ("action_plus_content", "action + content  [HEADLINE]", True, False),
    ("action_plus_content_unvalidated", "action + content + a-priori rule  [EXPLORATORY]", True, True),
]


def evaluate_layered(samples: list[dict], split: dict[str, str] | None) -> dict:
    """Score every layer configuration, broken out by dev/test split.

    Three layers, deliberately reported separately rather than as one number:
      action_only     -- policy.rego on structural tool-call facts, content-blind.
      action+content  -- adds intent_scanner's dev-validated signals. THIS IS
                         THE HEADLINE: every signal was derived by inspecting
                         dev samples only, and test was scored once at the end.
      +a-priori rule  -- additionally enables the principal-impersonation rule
                         in BOTH intent_scanner and policy.rego. Reported for
                         completeness only. The pattern it targets is absent
                         from the dev split and from all 455 AgentDojo traces,
                         so there is no data on which to claim it generalizes.
                         Do not quote this as a validated detection rate.
    """
    groups: dict[str, list[dict]] = {"all": samples}
    if split:
        groups["dev"] = [s for s in samples if split.get(s["id"]) == "dev"]
        groups["test"] = [s for s in samples if split.get(s["id"]) == "test"]

    results: dict[str, dict] = {}
    for key, label, use_content, use_unval in LAYER_CONFIGS:
        # Cache decisions once per layer config, then slice by group.
        per_sample: dict[str, bool] = {}
        for s in samples:
            pi = build_policy_input(s, include_unvalidated=use_unval)
            action_flagged = False
            if pi is not None:
                decision, _, _ = opa_decision(pi)
                action_flagged = decision in ("deny", "escalate")
            content_flagged = False
            if use_content:
                content_flagged = intent_scanner.flags_sample(
                    s, include_unvalidated=use_unval)[0]
            per_sample[s["id"]] = action_flagged or content_flagged

        results[key] = {"label": label}
        for gname, gsamples in groups.items():
            tp = fp = tn = fn = 0
            for s in gsamples:
                pred = per_sample[s["id"]]
                truth = normalize_label(s["ground_truth"]) == "unsafe"
                if pred and truth:
                    tp += 1
                elif pred and not truth:
                    fp += 1
                elif not pred and not truth:
                    tn += 1
                else:
                    fn += 1
            results[key][gname] = _confusion(tp, fp, tn, fn) | {"n": len(gsamples)}
    return results


def print_layered(results: dict, split: dict | None) -> None:
    groups = ["dev", "test", "all"] if split else ["all"]
    print("\n" + "=" * 78)
    print("LAYERED EVALUATION")
    if not split:
        print("  WARNING: split.json not found -- whole-dataset numbers only.")
        print("  Run `python3 make_split.py` first for dev/test separation.")
    print("=" * 78)
    for key, label, _, _ in LAYER_CONFIGS:
        print(f"\n{results[key]['label']}")
        for g in groups:
            r = results[key][g]
            print(f"  {g:5s} (n={r['n']:3d})  detection={r['detection']:6.1%}  "
                  f"FPR={r['fpr']:6.1%}  precision={r['precision']:6.1%}  "
                  f"MCC={r['mcc']:.3f}   [TP={r['tp']} FN={r['fn']} FP={r['fp']} TN={r['tn']}]")
    if split:
        h = results["action_plus_content"]["test"]
        e = results["action_plus_content_unvalidated"]["test"]
        print(f"\n  HEADLINE (held-out test, validated signals): "
              f"detection={h['detection']:.1%}  FPR={h['fpr']:.1%}  MCC={h['mcc']:.3f}")
        print(f"  EXPLORATORY (+a-priori rule, NOT validated): "
              f"detection={e['detection']:.1%}  FPR={e['fpr']:.1%}  MCC={e['mcc']:.3f}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Evaluate the OPA/Rego guardrail on AgentInjectionBench.")
    ap.add_argument("--layers-only", action="store_true",
                    help="skip the baseline-scanner comparison and print only "
                         "the layered dev/test evaluation")
    args = ap.parse_args()

    split = load_split()
    samples = load_dataset(REPO / "data" / "agent_injection_bench.jsonl")
    print(f"Loaded {len(samples)} samples "
          f"({sum(1 for s in samples if normalize_label(s['ground_truth']) == 'unsafe')} attacks, "
          f"{sum(1 for s in samples if normalize_label(s['ground_truth']) == 'safe')} benign)\n")

    if args.layers_only:
        layered = evaluate_layered(samples, split)
        print_layered(layered, split)
        (WORK / "results_layered.json").write_text(json.dumps({
            "split_used": bool(split), "layers": layered}, indent=2))
        print(f"\nWritten to {WORK / 'results_layered.json'}")
        return

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
        "layered": evaluate_layered(samples, split),
        "split_used": bool(split),
    }
    (WORK / "results.json").write_text(json.dumps(out, indent=2))
    lat = latency_stats(records)
    if lat:
        print(f"\n=== Latency (measured on the real samples during this run) ===")
        labels = {
            "build_ms":      "1. build policy input (feature extraction)",
            "content_ms":    "2. intent_scanner (regex content scan)",
            "opa_eval_ms":   "3. OPA policy evaluation (timer_rego_query_eval_ns)",
            "opa_wall_ms":   "   OPA subprocess round trip (spawn + recompile + eval)",
            "total_warm_ms": "=> TOTAL per decision, warm OPA (1+2+3)",
            "total_ms":      "=> TOTAL per decision, this harness (1+2+subprocess)",
        }
        print(f"  {'phase':52s}{'mean':>9}{'median':>9}{'p95':>9}{'p99':>9}{'max':>9}")
        for key, label in labels.items():
            if key not in lat:
                continue
            st = lat[key]
            print(f"  {label:52s}{st['mean_ms']:>9}{st['median_ms']:>9}"
                  f"{st['p95_ms']:>9}{st['p99_ms']:>9}{st['max_ms']:>9}")
        if "implied_throughput_warm_per_sec" in lat:
            print(f"\n  Implied throughput (warm OPA): "
                  f"{lat['implied_throughput_warm_per_sec']:,.0f} decisions/sec")
        print(f"  n = {lat['total_ms']['n']} samples timed individually during this run")

    print_layered(out["layered"], split)

    print(f"\nFull results written to {WORK / 'results.json'} "
          f"({len(records)} per-sample records included for auditing)")


if __name__ == "__main__":
    main()
