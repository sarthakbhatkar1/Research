#!/usr/bin/env python3
"""
make_split.py -- deterministic stratified dev/test split for AgentInjectionBench.

WHY THIS EXISTS
---------------
Every policy refinement made so far (the high-tier continuation removal, the
argument-value intent match, the sensitivity-tier configs) was derived by
inspecting failures on the SAME samples the reported numbers come from. That
is, in ML terms, tuning on the test set: it makes every headline metric
optimistically biased and is the first thing a reviewer will flag.

This script fixes that going forward by partitioning the dataset once:
  - dev  (~70%): the ONLY portion allowed to be inspected when diagnosing
                 failures or changing policy.rego.
  - test (~30%): touched only to produce final reported numbers. Never
                 inspected sample-by-sample to motivate a rule change.

PROPERTIES
----------
Deterministic: the split is derived from a SHA-256 hash of each sample's ID,
not a random seed. Re-running this on any machine, in any Python version,
after any dataset reordering, produces the identical split. That means the
split itself is reproducible by a reviewer without shipping a seed file.

Stratified: samples are bucketed by (attack_category, ground_truth) and the
~30% test fraction is taken within each bucket independently. A naive global
hash split left multi_turn_stateful with only 2 test samples -- too few to
say anything about the multi-turn evasion finding, which is one of the more
interesting results. Stratifying keeps every category proportionally
represented in both halves.

USAGE
-----
    python3 make_split.py            # writes split.json, prints the balance
    python3 make_split.py --verify   # re-derives and checks split.json matches
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path("/home/claude/AgentInjectionBench")
WORK = Path(__file__).parent
SPLIT_FILE = WORK / "split.json"
TEST_FRACTION = 0.30

sys.path.insert(0, str(REPO))
from evaluation.score import load_dataset, normalize_label  # noqa: E402


def stable_rank(sample_id: str) -> int:
    """Deterministic pseudo-random ordering key, stable across machines and
    Python versions (unlike hash(), which is salted per-process)."""
    return int(hashlib.sha256(sample_id.encode()).hexdigest(), 16)


def make_split(samples: list[dict], test_fraction: float = TEST_FRACTION) -> dict[str, str]:
    """Stratify by (attack_category, ground_truth), then within each stratum
    take the lowest-hash test_fraction of samples as test. Deterministic and
    proportional per stratum."""
    strata: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for s in samples:
        key = (s.get("attack_category", "unknown"), normalize_label(s["ground_truth"]))
        strata[key].append(s)

    split: dict[str, str] = {}
    for key, group in strata.items():
        ordered = sorted(group, key=lambda s: stable_rank(s["id"]))
        n_test = round(len(ordered) * test_fraction)
        # Guarantee at least 1 test sample per stratum where the stratum is
        # big enough to spare one -- otherwise a small category contributes
        # nothing to the reported numbers at all.
        if n_test == 0 and len(ordered) >= 2:
            n_test = 1
        for i, s in enumerate(ordered):
            split[s["id"]] = "test" if i < n_test else "dev"
    return split


def report(samples: list[dict], split: dict[str, str]) -> None:
    dev = [s for s in samples if split[s["id"]] == "dev"]
    test = [s for s in samples if split[s["id"]] == "test"]

    print(f"dev:  {len(dev)} samples")
    print(f"test: {len(test)} samples  ({len(test)/len(samples):.1%})")
    print()
    print(f"{'':28s} {'dev':>12s} {'test':>12s}")
    print(f"{'label balance':28s}")
    for label in ("unsafe", "safe"):
        d = sum(1 for s in dev if normalize_label(s["ground_truth"]) == label)
        t = sum(1 for s in test if normalize_label(s["ground_truth"]) == label)
        print(f"  {label:26s} {d:12d} {t:12d}")
    print()
    print(f"{'attack_category':28s}")
    all_cats = sorted({s.get("attack_category", "unknown") for s in samples})
    dev_cats = Counter(s.get("attack_category", "unknown") for s in dev)
    test_cats = Counter(s.get("attack_category", "unknown") for s in test)
    for cat in all_cats:
        print(f"  {cat:26s} {dev_cats[cat]:12d} {test_cats[cat]:12d}")
    print()
    print(f"{'complexity':28s}")
    dev_cx = Counter(s.get("complexity", "unknown") for s in dev)
    test_cx = Counter(s.get("complexity", "unknown") for s in test)
    for cx in sorted({s.get("complexity", "unknown") for s in samples}):
        print(f"  {cx:26s} {dev_cx[cx]:12d} {test_cx[cx]:12d}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true",
                    help="re-derive the split and confirm it matches split.json")
    args = ap.parse_args()

    samples = load_dataset()
    split = make_split(samples)

    if args.verify:
        if not SPLIT_FILE.exists():
            print("split.json does not exist -- run without --verify first.")
            raise SystemExit(1)
        saved = json.loads(SPLIT_FILE.read_text())["split"]
        if saved == split:
            print(f"VERIFIED: re-derived split matches split.json exactly ({len(split)} samples).")
            raise SystemExit(0)
        diffs = [k for k in split if saved.get(k) != split[k]]
        print(f"MISMATCH: {len(diffs)} samples differ, e.g. {diffs[:5]}")
        raise SystemExit(1)

    report(samples, split)
    SPLIT_FILE.write_text(json.dumps({
        "_comment": ("Deterministic stratified split. dev = allowed to inspect when "
                     "tuning policy.rego. test = reported numbers only, never "
                     "inspected to motivate a rule change. Re-derivable via "
                     "make_split.py --verify."),
        "test_fraction": TEST_FRACTION,
        "n_dev": sum(1 for v in split.values() if v == "dev"),
        "n_test": sum(1 for v in split.values() if v == "test"),
        "split": split,
    }, indent=2))
    print(f"\nWritten to {SPLIT_FILE}")


if __name__ == "__main__":
    main()
