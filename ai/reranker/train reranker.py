#!/usr/bin/env python3
"""
train_reranker.py
=================
Fine-tunes a cross-encoder reranker on the output of build_reranker_dataset.py.

What it does
  1. Loads train.jsonl (query, answer, label) plus dev_eval.json / test_eval.json.
  2. Evaluates the *un-fine-tuned* base model (and optional off-the-shelf rerankers) on dev + test.
     This is your baseline: fine-tuning has to beat it to be worth shipping.
  3. Trains with BinaryCrossEntropyLoss, pos_weight = negatives per positive (from report.json).
  4. Keeps the checkpoint with the best dev NDCG@10 (cross-encoders overfit fast).
  5. Evaluates the best model on dev + test, prints a comparison table, writes results.json.

Evaluation is the realistic two-stage kind: the reranker reorders the first-stage retriever's
top-N (`documents`), and positives the retriever did not return are NOT rescued
(always_rerank_positives=False). Output includes retriever-only metrics (the "base" rows),
so you can see what reranking adds on top of retrieval.

Requires: sentence-transformers >= 4.0, datasets, torch.

Usage:
  python train_reranker.py --data-dir out --output-dir models/fin-reranker \
      --base-model cross-encoder/ms-marco-MiniLM-L6-v2 \
      --baseline-models BAAI/bge-reranker-base

Using the trained model in your RAG pipeline (stage 2 of retrieval):
  from sentence_transformers import CrossEncoder
  reranker = CrossEncoder("models/fin-reranker/final")
  ranked = reranker.rank(query, candidate_passages, top_k=5)   # [{"corpus_id": i, "score": s}, ...]
  # candidate_passages must be formatted exactly like the "answer" column used in training
  # (context prefix + chunk text).
"""
from __future__ import annotations

import argparse
import json
import logging
import math
from pathlib import Path

import torch
from datasets import load_dataset
from sentence_transformers.cross_encoder import (
    CrossEncoder,
    CrossEncoderTrainer,
    CrossEncoderTrainingArguments,
)
from sentence_transformers.cross_encoder.evaluation import CrossEncoderRerankingEvaluator
from sentence_transformers.cross_encoder.losses.BinaryCrossEntropyLoss import BinaryCrossEntropyLoss

log = logging.getLogger("train_reranker")
DEV_NAME = "fin-dev"


def load_model(name: str, max_length: int | None) -> CrossEncoder:
    """num_labels=1 -> one relevance score. ignore_mismatched_sizes lets a classifier head
    (e.g. FinBERT's 3-way sentiment head) be replaced by a fresh 1-output head."""
    kw = {"num_labels": 1}
    if max_length:
        kw["max_length"] = max_length
    try:
        return CrossEncoder(name, model_kwargs={"ignore_mismatched_sizes": True}, **kw)
    except TypeError:  # older sentence-transformers signature
        return CrossEncoder(name, automodel_args={"ignore_mismatched_sizes": True}, **kw)


def make_evaluator(samples: list[dict], name: str, batch_size: int) -> CrossEncoderRerankingEvaluator:
    return CrossEncoderRerankingEvaluator(
        samples=samples,
        batch_size=batch_size,
        name=name,
        always_rerank_positives=False,  # realistic: only rerank what the retriever found
    )


def evaluate(model: CrossEncoder, samples: list[dict], name: str, batch_size: int) -> dict:
    results = make_evaluator(samples, name, batch_size)(model)
    return {k: float(v) for k, v in results.items() if isinstance(v, (int, float))}


def pick(results: dict, suffix: str) -> float | None:
    for k, v in results.items():
        if k.endswith(suffix) and "_base_" not in k:
            return v
    return None


def pick_base(results: dict, suffix: str) -> float | None:
    for k, v in results.items():
        if k.endswith("_base_" + suffix):
            return v
    return None


def fmt(x: float | None) -> str:
    return "  n/a" if x is None else f"{100 * x:5.1f}"


def main() -> None:
    p = argparse.ArgumentParser(description="Fine-tune a financial cross-encoder reranker.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--data-dir", required=True, type=Path, help="output dir of build_reranker_dataset.py")
    p.add_argument("--output-dir", required=True, type=Path)
    p.add_argument("--base-model", default="cross-encoder/ms-marco-MiniLM-L6-v2",
                   help="an existing reranker needs the least data; a raw encoder (FinBERT, ModernBERT) needs more")
    p.add_argument("--baseline-models", default="", help="comma-separated off-the-shelf rerankers to compare against")
    p.add_argument("--max-length", type=int, default=None, help="tokens per (query, passage); default = model's")
    p.add_argument("--epochs", type=float, default=2)
    p.add_argument("--batch-size", type=int, default=16)
    p.add_argument("--lr", type=float, default=2e-5)
    p.add_argument("--warmup-ratio", type=float, default=0.1)
    p.add_argument("--pos-weight", type=float, default=None, help="default: negatives-per-positive from report.json")
    p.add_argument("--evals-per-epoch", type=int, default=4)
    p.add_argument("--report-to", default="none", help="none | wandb | tensorboard")
    p.add_argument("--seed", type=int, default=42)
    a = p.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # ---- data -------------------------------------------------------------------------
    train_ds = load_dataset("json", data_files=str(a.data_dir / "train.jsonl"), split="train")
    train_ds = train_ds.select_columns(["query", "answer", "label"])  # column order matters; extras would be inputs
    dev_samples = json.loads((a.data_dir / "dev_eval.json").read_text(encoding="utf-8"))
    test_samples = json.loads((a.data_dir / "test_eval.json").read_text(encoding="utf-8"))
    report = json.loads((a.data_dir / "report.json").read_text(encoding="utf-8"))
    log.info("train pairs: %d | dev queries: %d | test queries: %d", len(train_ds), len(dev_samples), len(test_samples))
    if len(dev_samples) < 100:
        log.warning("only %d dev queries: metric differences under a few points are noise. Generate more data.",
                    len(dev_samples))

    pos_weight = a.pos_weight or report["splits"]["train"]["suggested_pos_weight"]
    log.info("pos_weight = %.2f", pos_weight)

    results: dict[str, dict] = {}

    # ---- baselines (before any training) -----------------------------------------------
    names = [a.base_model] + [m.strip() for m in a.baseline_models.split(",") if m.strip()]
    for name in names:
        log.info("evaluating baseline: %s", name)
        m = load_model(name, a.max_length)
        results[f"baseline:{name}"] = {
            "dev": evaluate(m, dev_samples, DEV_NAME, a.batch_size),
            "test": evaluate(m, test_samples, "fin-test", a.batch_size),
        }
        del m

    # ---- train ---------------------------------------------------------------------------
    model = load_model(a.base_model, a.max_length)
    log.info("model max_length: %s", model.max_length)
    loss = BinaryCrossEntropyLoss(model=model, pos_weight=torch.tensor(float(pos_weight)))

    steps_per_epoch = math.ceil(len(train_ds) / a.batch_size)
    eval_steps = max(50, steps_per_epoch // a.evals_per_epoch)
    use_bf16 = torch.cuda.is_available() and torch.cuda.is_bf16_supported()
    args = CrossEncoderTrainingArguments(
        output_dir=str(a.output_dir),
        num_train_epochs=a.epochs,
        per_device_train_batch_size=a.batch_size,
        per_device_eval_batch_size=a.batch_size,
        learning_rate=a.lr,
        warmup_ratio=a.warmup_ratio,
        bf16=use_bf16,
        fp16=torch.cuda.is_available() and not use_bf16,
        eval_strategy="steps",
        eval_steps=eval_steps,
        save_strategy="steps",
        save_steps=eval_steps,  # must line up with eval_steps for load_best_model_at_end
        save_total_limit=2,
        load_best_model_at_end=True,
        metric_for_best_model=f"eval_{DEV_NAME}_ndcg@10",
        greater_is_better=True,
        logging_steps=max(10, eval_steps // 4),
        report_to=a.report_to,
        seed=a.seed,
    )
    trainer = CrossEncoderTrainer(
        model=model,
        args=args,
        train_dataset=train_ds,
        loss=loss,
        evaluator=make_evaluator(dev_samples, DEV_NAME, a.batch_size),
    )
    trainer.train()

    final_dir = a.output_dir / "final"
    model.save_pretrained(str(final_dir))
    log.info("saved best model to %s", final_dir)

    results["fine-tuned"] = {
        "dev": evaluate(model, dev_samples, DEV_NAME, a.batch_size),
        "test": evaluate(model, test_samples, "fin-test", a.batch_size),
    }
    (a.output_dir / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")

    # ---- comparison table ----------------------------------------------------------------
    print("\nTEST SET (realistic: rerank retriever top-N)      NDCG@10  MRR@10    MAP")
    first = next(iter(results.values()))["test"]
    print(f"{'first-stage retriever only':<48}{fmt(pick_base(first, 'ndcg@10'))}   {fmt(pick_base(first, 'mrr@10'))}"
          f"   {fmt(pick_base(first, 'map'))}")
    for label, r in results.items():
        t = r["test"]
        print(f"{label:<48}{fmt(pick(t, 'ndcg@10'))}   {fmt(pick(t, 'mrr@10'))}   {fmt(pick(t, 'map'))}")
    print("\nIf fine-tuned is not clearly above the best baseline, do not ship it: fix the data first "
          "(see preview.md), then retrain.")


if __name__ == "__main__":
    main()
