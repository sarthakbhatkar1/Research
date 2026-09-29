#!/usr/bin/env python3
"""
build_reranker_dataset.py
=========================
Builds a synthetic training set for a financial cross-encoder reranker.

Pipeline
--------
  chunks -> (1) add context prefix -> (2) generate + filter questions
         -> (3) round-trip check + hard-negative mining -> (4) split by document
         -> (5) write train/dev/test files + a human-review preview

Input  (--chunks): JSONL, one chunk per line:
  {"chunk_id": "acme-10q-2023q2-014", "doc_id": "acme-10q-2023q2", "chunk_idx": 14,
   "text": "The company's revenue grew by 3% ...",
   "metadata": {"company": "ACME", "period": "Q2 2023", "doc_type": "10-Q", "section": "MD&A"}}
  Required: chunk_id, doc_id, text.  Optional: chunk_idx (defaults to file order per doc), metadata.

Output (--out):
  train.jsonl / dev.jsonl / test.jsonl        {"query","answer","label"}  <- feed to CrossEncoderTrainer
  *_audit.jsonl                               same rows + ids, negative type, scores (NOT for training)
  dev_eval.json / test_eval.json              [{"query","positive":[..],"documents":[..]}] where documents = the
                                              first-stage retriever's ranked top-N (use with
                                              CrossEncoderRerankingEvaluator, always_rerank_positives=False)
  preview.md                                  read this before training
  report.json                                 counts, drop reasons, suggested pos_weight, length warnings
  cache/                                      LLM outputs, so re-runs are cheap and resumable

Why two file types: sentence-transformers treats every non-label column as model input,
so metadata columns must live in the *_audit files, never in the training file.

Install:  pip install sentence-transformers litellm tqdm

Dry run first (a few documents, to eyeball quality and cost):
  python build_reranker_dataset.py --chunks chunks.jsonl --out out_dry \
      --llm-model azure/<deployment> --limit-docs 5 --context-mode metadata

Azure example (key via AZURE_API_KEY env var):
  --llm-model azure/<deployment> \
  --llm-kwargs '{"api_base": "https://<resource>.openai.azure.com", "api_version": "<version>"}'
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import random
import re
import shutil
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("build_reranker_dataset")

# ----------------------------------------------------------------------------
# Prompts
# ----------------------------------------------------------------------------

# Adapted from Anthropic's Contextual Retrieval post (prompt asks for a short situating context).
CONTEXT_PROMPT = """<document>
{doc}
</document>
Here is the chunk we want to situate within the whole document
<chunk>
{chunk}
</chunk>
Please give a short succinct context to situate this chunk within the overall document for the purposes of improving search retrieval of the chunk. Include the company, filing type, reporting period and section whenever they can be determined. Answer only with the succinct context and nothing else."""

QUERY_SYSTEM = (
    "You write realistic search queries used to train a financial document reranker. "
    "You always answer with a single JSON object and nothing else."
)

QUERY_USER = """Passage (with source context):
<passage>
{passage}
</passage>

Write {n} different questions a financial analyst might ask, each answerable using ONLY this passage.

Rules:
- Each question must be self-contained: name the company, reporting period and metric explicitly whenever the passage context identifies them. Never write "the company", "this quarter" or "the passage".
- Do not copy phrases of 4 or more consecutive words from the passage. Paraphrase the way a real user would type.
- Vary the style across the set: numeric lookup, factual lookup, explanation or reasoning, risk or outlook.
- Do not ask about anything the passage does not state.

Return ONLY JSON: {{"queries": [{{"query": "...", "type": "numeric|factual|explanation|risk"}}]}}"""

VERIFY_PROMPT = """Passage:
<passage>
{passage}
</passage>
Question: {query}

Can this question be answered fully and specifically using ONLY the passage above?
Return ONLY JSON: {{"answerable": true or false, "reason": "under 10 words"}}"""


# ----------------------------------------------------------------------------
# Data model + small helpers
# ----------------------------------------------------------------------------

@dataclass
class Chunk:
    chunk_id: str
    doc_id: str
    chunk_idx: int
    text: str
    metadata: dict = field(default_factory=dict)
    context: str = ""

    @property
    def passage(self) -> str:
        """What the reranker will see: context prefix + chunk text (train == serve)."""
        return f"{self.context}\n{self.text}" if self.context else self.text


def progress(it, total=None, desc=""):
    try:
        from tqdm import tqdm
        return tqdm(it, total=total, desc=desc)
    except ImportError:
        return it


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_cache(path: Path) -> dict[str, dict]:
    return {r["chunk_id"]: r for r in read_jsonl(path)}


def append_cache(path: Path, rec: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1:
        raise ValueError("no JSON object in LLM output")
    return json.loads(text[s:e + 1])


def ngram_overlap(query: str, text: str, n: int = 4) -> float:
    """Fraction of the query's word n-grams that appear verbatim in the text (copy detector)."""
    qt = re.findall(r"\w+", query.lower())
    tt = re.findall(r"\w+", text.lower())
    if len(qt) < n or len(tt) < n:
        return 0.0
    tg = {tuple(tt[i:i + n]) for i in range(len(tt) - n + 1)}
    qg = [tuple(qt[i:i + n]) for i in range(len(qt) - n + 1)]
    return sum(g in tg for g in qg) / len(qg)


def n_hard(ex: dict) -> int:
    """Negatives that came from mining (random/easy negatives don't count toward --min-negatives)."""
    return sum(n["type"] != "random" for n in ex["negatives"])


def run_parallel(items, fn, workers: int, desc: str):
    """Yield (item, result); result is None if fn raised (error is logged, run continues)."""
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(fn, it): it for it in items}
        for f in progress(as_completed(futs), total=len(futs), desc=desc):
            try:
                yield futs[f], f.result()
            except Exception as e:  # noqa: BLE001 - keep the batch going
                log.warning("%s failed for %s: %s", desc, getattr(futs[f], "chunk_id", "?"), e)
                yield futs[f], None


# ----------------------------------------------------------------------------
# Loading + document windows
# ----------------------------------------------------------------------------

def load_chunks(path: Path) -> list[Chunk]:
    chunks, per_doc = [], Counter()
    with open(path, encoding="utf-8") as f:
        for ln, line in enumerate(f, 1):
            if not line.strip():
                continue
            r = json.loads(line)
            for k in ("chunk_id", "doc_id", "text"):
                if k not in r:
                    raise ValueError(f"{path}:{ln} missing required field '{k}'")
            idx = r.get("chunk_idx")
            if idx is None:
                idx = per_doc[r["doc_id"]]
            per_doc[r["doc_id"]] += 1
            chunks.append(Chunk(str(r["chunk_id"]), str(r["doc_id"]), int(idx), r["text"], r.get("metadata") or {}))
    ids = [c.chunk_id for c in chunks]
    if len(ids) != len(set(ids)):
        dupes = [k for k, v in Counter(ids).items() if v > 1][:5]
        raise ValueError(f"chunk_id must be unique; duplicates e.g. {dupes}")
    return chunks


class DocStore:
    """Rebuilds each document from its chunks and serves a bounded window around any chunk."""

    def __init__(self, chunks: list[Chunk], max_chars: int):
        self.max_chars, self.text, self.off = max_chars, {}, {}
        by_doc = defaultdict(list)
        for c in chunks:
            by_doc[c.doc_id].append(c)
        for doc_id, cs in by_doc.items():
            cs.sort(key=lambda c: c.chunk_idx)
            pos, parts = 0, []
            for c in cs:
                self.off[c.chunk_id] = pos
                parts.append(c.text)
                pos += len(c.text) + 2
            self.text[doc_id] = "\n\n".join(parts)

    def window(self, c: Chunk) -> str:
        t = self.text[c.doc_id]
        if len(t) <= self.max_chars:
            return t
        start = max(0, min(self.off[c.chunk_id] - self.max_chars // 2, len(t) - self.max_chars))
        return t[start:start + self.max_chars]


# ----------------------------------------------------------------------------
# LLM (via LiteLLM, so it works with your existing proxy / Azure setup)
# ----------------------------------------------------------------------------

def make_llm(a):
    import litellm
    litellm.drop_params = True
    extra = json.loads(a.llm_kwargs) if a.llm_kwargs else {}

    def call(messages, max_tokens: int = 600) -> str:
        kw = dict(model=a.llm_model, messages=messages, max_tokens=max_tokens, **extra)
        if a.temperature is not None:  # only sent when you ask for it
            kw["temperature"] = a.temperature
        last = None
        for attempt in range(4):
            try:
                r = litellm.completion(**kw)
                return (r.choices[0].message.content or "").strip()
            except Exception as e:  # noqa: BLE001
                last = e
                time.sleep(2 ** attempt)
        raise RuntimeError(f"LLM call failed after retries: {last}")

    return call


# ----------------------------------------------------------------------------
# Stage 1: context prefix
# ----------------------------------------------------------------------------

def metadata_context(c: Chunk, a) -> str:
    """Deterministic prefix from metadata (free; works well if your graph nodes already carry it)."""
    m = c.metadata
    bits = [str(m[k]) for k in (a.meta_entity_key, "doc_type", a.meta_period_key) if m.get(k)]
    if not bits:
        return ""
    ctx = "This chunk is from " + ", ".join(bits) + "."
    if m.get("section"):
        ctx += f" Section: {m['section']}."
    return ctx


def stage_context(chunks: list[Chunk], a, llm, store: DocStore) -> None:
    if a.context_mode == "none":
        return
    if a.context_mode == "metadata":
        for c in chunks:
            c.context = metadata_context(c, a)
        if not any(c.context for c in chunks):
            log.warning("context-mode=metadata but no chunk has the metadata keys; passages are un-contextualized")
        return

    cache_path = a.out / "cache" / "contexts.jsonl"
    cache = read_cache(cache_path)
    todo = [c for c in chunks if c.chunk_id not in cache]
    log.info("contextualize: %d cached, %d to generate", len(chunks) - len(todo), len(todo))

    def work(c: Chunk) -> str:
        msg = CONTEXT_PROMPT.format(doc=store.window(c), chunk=c.text)
        return llm([{"role": "user", "content": msg}], max_tokens=200)

    for c, ctx in run_parallel(todo, work, a.workers, "contextualize"):
        if ctx:
            cache[c.chunk_id] = {"chunk_id": c.chunk_id, "context": ctx}
            append_cache(cache_path, cache[c.chunk_id])
    for c in chunks:
        c.context = cache.get(c.chunk_id, {}).get("context", "")


# ----------------------------------------------------------------------------
# Stage 2: synthetic questions (+ cheap filters, optional LLM answerability check)
# ----------------------------------------------------------------------------

def gen_queries_for_chunk(c: Chunk, a, llm) -> dict:
    raw = llm(
        [{"role": "system", "content": QUERY_SYSTEM},
         {"role": "user", "content": QUERY_USER.format(passage=c.passage, n=a.queries_per_chunk)}],
        max_tokens=700,
    )
    items = parse_json(raw).get("queries", [])
    kept, rejected, seen = [], Counter(), set()
    for it in items:
        q = " ".join(str(it.get("query", "")).split())
        nwords = len(q.split())
        norm = q.lower()
        if not (a.min_q_words <= nwords <= a.max_q_words):
            rejected["bad_length"] += 1
        elif norm in seen:
            rejected["dup_within_chunk"] += 1
        elif ngram_overlap(q, c.text) > a.max_copy_overlap:
            rejected["copies_passage"] += 1
        else:
            if a.verify_answerable:
                v = parse_json(llm([{"role": "user", "content": VERIFY_PROMPT.format(passage=c.passage, query=q)}],
                                   max_tokens=80))
                if not v.get("answerable"):
                    rejected["not_answerable"] += 1
                    continue
            seen.add(norm)
            kept.append({"query": q, "type": str(it.get("type", ""))})
    return {"queries": kept, "rejected": dict(rejected)}


def stage_queries(chunks: list[Chunk], a, llm):
    cache_path = a.out / "cache" / "queries.jsonl"
    cache = read_cache(cache_path)
    eligible = [c for c in chunks if len(c.text.split()) >= a.min_chunk_words]
    todo = [c for c in eligible if c.chunk_id not in cache]
    log.info("queries: %d eligible chunks (of %d), %d cached, %d to generate",
             len(eligible), len(chunks), len(eligible) - len(todo), len(todo))

    for c, res in run_parallel(todo, lambda c: gen_queries_for_chunk(c, a, llm), a.workers, "queries"):
        if res is not None:
            cache[c.chunk_id] = {"chunk_id": c.chunk_id, **res}
            append_cache(cache_path, cache[c.chunk_id])

    queries, rejected = [], Counter()
    for c in eligible:
        rec = cache.get(c.chunk_id)
        if not rec:
            continue
        rejected.update(rec.get("rejected", {}))
        for i, q in enumerate(rec["queries"]):
            queries.append({"query_id": f"{c.chunk_id}::q{i}", "chunk_id": c.chunk_id,
                            "query": q["query"], "type": q.get("type", "")})

    # Identical question text from different chunks = ambiguous supervision -> drop every copy.
    counts = Counter(q["query"].lower() for q in queries)
    dup_total = sum(1 for q in queries if counts[q["query"].lower()] > 1)
    if dup_total:
        rejected["dup_across_chunks"] += dup_total
        queries = [q for q in queries if counts[q["query"].lower()] == 1]
    return queries, rejected


# ----------------------------------------------------------------------------
# Stage 3: round-trip check + hard negatives
# ----------------------------------------------------------------------------

def stage_mine(chunks: list[Chunk], queries: list[dict], a):
    import torch
    from sentence_transformers import SentenceTransformer, util

    rng = random.Random(a.seed)
    model = SentenceTransformer(a.embed_model, device=a.device)

    def enc(texts, prompt):
        kw = {"prompt": prompt} if prompt else {}
        return model.encode(texts, batch_size=64, normalize_embeddings=True,
                            convert_to_tensor=True, show_progress_bar=True, **kw)

    log.info("encoding %d passages and %d queries", len(chunks), len(queries))
    c_emb = enc([c.passage for c in chunks], a.passage_prompt)
    q_emb = enc([q["query"] for q in queries], a.query_prompt)

    idx_of = {c.chunk_id: i for i, c in enumerate(chunks)}
    text_hash = [hashlib.md5(c.text.encode("utf-8")).hexdigest() for c in chunks]
    by_entity, by_period = defaultdict(list), defaultdict(list)
    for i, c in enumerate(chunks):
        if c.metadata.get(a.meta_entity_key) is not None:
            by_entity[c.metadata[a.meta_entity_key]].append(i)
        if c.metadata.get(a.meta_period_key) is not None:
            by_period[c.metadata[a.meta_period_key]].append(i)
    if not by_entity or not by_period:
        log.warning("no '%s'/'%s' metadata found: metadata negatives will be skipped",
                    a.meta_entity_key, a.meta_period_key)

    top_k = max(a.range_max, a.roundtrip_k, a.eval_top_n) + 1  # +1 leaves room for the positive itself
    hits = util.semantic_search(q_emb, c_emb, top_k=top_k)

    def acceptable(j, score, pos, pos_score, pos_hash) -> bool:
        cj = chunks[j]
        if text_hash[j] == pos_hash:
            return False  # identical text = same content
        if score >= a.neg_ceiling_ratio * pos_score:
            return False  # too close to the positive: likely also a correct answer
        if a.exclude_adjacent and cj.doc_id == pos.doc_id and abs(cj.chunk_idx - pos.chunk_idx) <= 1:
            return False  # neighbouring chunk often continues the same answer
        return True

    stats, examples = Counter(), []
    for qi, q in enumerate(progress(queries, desc="mining")):
        p = idx_of[q["chunk_id"]]
        pos = chunks[p]
        h = hits[qi]
        ranked = [x["corpus_id"] for x in h]
        if p not in ranked[:a.roundtrip_k]:
            stats["drop_roundtrip"] += 1
            continue
        pos_score = float(next(x["score"] for x in h if x["corpus_id"] == p))
        pos_hash = text_hash[p]

        cands = [(x["corpus_id"], float(x["score"])) for x in h if x["corpus_id"] != p]
        window = [(j, s) for j, s in cands[a.range_min:a.range_max]
                  if acceptable(j, s, pos, pos_score, pos_hash)]
        picked = window[:a.n_neg_embedding] if a.neg_sampling == "top" \
            else rng.sample(window, min(a.n_neg_embedding, len(window)))
        negs = [{"chunk_id": chunks[j].chunk_id, "type": "embedding", "score": s} for j, s in picked]
        chosen = {p} | {j for j, _ in picked}

        # Finance-specific negatives: same company/other period, or same period/other company.
        ent, per = pos.metadata.get(a.meta_entity_key), pos.metadata.get(a.meta_period_key)
        if a.n_neg_metadata and ent is not None and per is not None:
            pool = {j for j in by_entity[ent] if chunks[j].metadata.get(a.meta_period_key) != per}
            pool |= {j for j in by_period[per] if chunks[j].metadata.get(a.meta_entity_key) != ent}
            pool -= chosen
            if pool:
                idx = torch.tensor(sorted(pool), device=c_emb.device)
                sc = (c_emb[idx] @ q_emb[qi]).tolist()
                scored = sorted(zip(idx.tolist(), sc), key=lambda t: -t[1])
                scored = [(j, s) for j, s in scored if acceptable(j, s, pos, pos_score, pos_hash)]
                top = scored[:a.meta_top_pool]
                for j, s in rng.sample(top, min(a.n_neg_metadata, len(top))):
                    negs.append({"chunk_id": chunks[j].chunk_id, "type": "metadata", "score": s})

        if len(negs) < a.min_negatives:
            stats["drop_no_negatives"] += 1
            continue

        # Easy negatives: training on hard negatives only can hurt easy cases, so mix in random ones.
        if a.n_neg_random:
            have = {n["chunk_id"] for n in negs} | {pos.chunk_id}
            added = 0
            for j in rng.sample(range(len(chunks)), min(len(chunks), a.n_neg_random * 10)):
                s = float(c_emb[j] @ q_emb[qi])
                if chunks[j].chunk_id in have or not acceptable(j, s, pos, pos_score, pos_hash):
                    continue
                negs.append({"chunk_id": chunks[j].chunk_id, "type": "random", "score": s})
                have.add(chunks[j].chunk_id)
                added += 1
                if added >= a.n_neg_random:
                    break

        examples.append({"query_id": q["query_id"], "query": q["query"], "query_type": q["type"],
                         "positive": pos.chunk_id, "pos_score": pos_score, "negatives": negs,
                         "retrieved": [chunks[x["corpus_id"]].chunk_id for x in h[:a.eval_top_n]]})

    if a.fn_cross_encoder and examples:
        stats["fn_filtered_negatives"] += apply_fn_filter(examples, {c.chunk_id: c for c in chunks}, a)
        before = len(examples)
        examples = [e for e in examples if n_hard(e) >= a.min_negatives]
        stats["drop_no_negatives"] += before - len(examples)
    return examples, stats


def apply_fn_filter(examples: list[dict], by_id: dict[str, Chunk], a) -> int:
    """Second opinion on false negatives: drop negatives a pretrained cross-encoder scores >= the positive."""
    from sentence_transformers.cross_encoder import CrossEncoder

    ce = CrossEncoder(a.fn_cross_encoder, device=a.device)
    pairs, refs = [], []
    for ei, ex in enumerate(examples):
        pairs.append((ex["query"], by_id[ex["positive"]].passage))
        refs.append((ei, None))
        for ni, n in enumerate(ex["negatives"]):
            pairs.append((ex["query"], by_id[n["chunk_id"]].passage))
            refs.append((ei, ni))
    scores = ce.predict(pairs, batch_size=32, show_progress_bar=True)

    pos_ce = {ei: float(s) for (ei, ni), s in zip(refs, scores) if ni is None}
    for (ei, ni), s in zip(refs, scores):
        if ni is not None:
            examples[ei]["negatives"][ni]["ce_score"] = float(s)
    dropped = 0
    for ei, ex in enumerate(examples):
        keep = []
        for n in ex["negatives"]:
            if n["ce_score"] >= pos_ce[ei] - a.fn_margin:
                dropped += 1
            else:
                keep.append(n)
        ex["negatives"], ex["pos_ce_score"] = keep, pos_ce[ei]
    return dropped


# ----------------------------------------------------------------------------
# Stage 4/5: split by document + write outputs
# ----------------------------------------------------------------------------

def assign_splits(examples: list[dict], by_id: dict[str, Chunk], a) -> dict[str, list[dict]]:
    def group(ex) -> str:
        c = by_id[ex["positive"]]
        return c.doc_id if a.split_key == "doc_id" else str(c.metadata.get(a.split_key, c.doc_id))

    groups = sorted({group(ex) for ex in examples})
    if len(groups) < 10:
        log.warning("only %d split groups: dev/test will be tiny and noisy. Use more documents.", len(groups))
    random.Random(a.seed).shuffle(groups)
    n = len(groups)
    n_test, n_dev = round(n * a.test_frac), round(n * a.dev_frac)
    if n >= 3:
        n_test = max(1, n_test) if a.test_frac > 0 else 0
        n_dev = max(1, n_dev) if a.dev_frac > 0 else 0
    test, dev = set(groups[:n_test]), set(groups[n_test:n_test + n_dev])

    out = {"train": [], "dev": [], "test": []}
    for ex in examples:
        g = group(ex)
        out["test" if g in test else "dev" if g in dev else "train"].append(ex)
    return out


def snippet(text: str, n: int = 380) -> str:
    t = " ".join(text.split())
    return t if len(t) <= n else t[:n] + " ..."


def write_outputs(examples: list[dict], chunks: list[Chunk], queries: list[dict],
                  rejected: Counter, stats: Counter, a) -> None:
    by_id = {c.chunk_id: c for c in chunks}
    splits = assign_splits(examples, by_id, a)
    rng = random.Random(a.seed)

    report = {"counts": {"chunks": len(chunks), "queries_after_generation_filters": len(queries),
                         "queries_kept_after_mining": len(examples)},
              "query_rejections": dict(rejected), "mining_drops": dict(stats), "splits": {}}

    for name, exs in splits.items():
        pairs, audit, eval_rows = [], [], []
        neg_types = Counter()
        for ex in exs:
            pos = by_id[ex["positive"]]
            meta = {k: pos.metadata.get(k) for k in (a.meta_entity_key, a.meta_period_key)}
            pairs.append({"query": ex["query"], "answer": pos.passage, "label": 1.0})
            audit.append({"split": name, "query_id": ex["query_id"], "chunk_id": pos.chunk_id, "label": 1.0,
                          "negative_type": None, "score": ex["pos_score"], **meta})
            for n in ex["negatives"]:
                pairs.append({"query": ex["query"], "answer": by_id[n["chunk_id"]].passage, "label": 0.0})
                audit.append({"split": name, "query_id": ex["query_id"], "chunk_id": n["chunk_id"], "label": 0.0,
                              "negative_type": n["type"], "score": n["score"], **meta})
                neg_types[n["type"]] += 1
            eval_rows.append({"query": ex["query"], "positive": [pos.passage],
                              "documents": [by_id[cid].passage for cid in ex["retrieved"]]})
        write_jsonl(a.out / f"{name}.jsonl", pairs)
        write_jsonl(a.out / f"{name}_audit.jsonl", audit)
        if name != "train":
            (a.out / f"{name}_eval.json").write_text(json.dumps(eval_rows, ensure_ascii=False, indent=1), encoding="utf-8")
        n_neg = sum(len(e["negatives"]) for e in exs)
        report["splits"][name] = {"queries": len(exs), "pairs": len(pairs), "negative_types": dict(neg_types),
                                  "suggested_pos_weight": round(n_neg / len(exs), 2) if exs else None}

    used = {ex["positive"] for ex in examples} | {n["chunk_id"] for ex in examples for n in ex["negatives"]}
    est_tokens = [len(by_id[i].passage.split()) * 1.35 for i in used]
    if est_tokens:
        report["passages_est_over_480_tokens"] = round(sum(t > 480 for t in est_tokens) / len(est_tokens), 3)
    report["query_types"] = dict(Counter(e["query_type"] for e in examples))
    report["config"] = vars(a)
    (a.out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    write_jsonl(a.out / "contextualized_chunks.jsonl",
                ({"chunk_id": c.chunk_id, "doc_id": c.doc_id, "context": c.context, "text": c.text,
                  "metadata": c.metadata} for c in chunks))

    # Human-review preview
    lines = ["# Dataset preview: read before training", "",
             "For each example check: (1) does the POSITIVE really answer the query? "
             "(2) is any NEG actually also a correct answer (false negative)?", ""]
    for ex in rng.sample(examples, min(a.preview, len(examples))):
        lines += [f"## {ex['query']}", f"*type: {ex['query_type']} | positive score: {ex['pos_score']:.3f}*", "",
                  f"**POSITIVE** `{ex['positive']}`", f"> {snippet(by_id[ex['positive']].passage)}", ""]
        for n in ex["negatives"]:
            lines += [f"**NEG [{n['type']}, {n['score']:.3f}]** `{n['chunk_id']}`",
                      f"> {snippet(by_id[n['chunk_id']].passage)}", ""]
    (a.out / "preview.md").write_text("\n".join(lines), encoding="utf-8")

    log.info("done. train=%d dev=%d test=%d queries -> %s",
             len(splits["train"]), len(splits["dev"]), len(splits["test"]), a.out)
    if report.get("passages_est_over_480_tokens", 0) > 0.1:
        log.warning("%.0f%% of passages look longer than ~480 tokens: a 512-token base model will truncate them. "
                    "Re-chunk, or use a longer-context base model.", 100 * report["passages_est_over_480_tokens"])


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Build a synthetic reranker training set.",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    g = p.add_argument_group("input/output")
    g.add_argument("--chunks", required=True, type=Path, help="chunks JSONL (see module docstring)")
    g.add_argument("--out", required=True, type=Path)
    g.add_argument("--fresh", action="store_true", help="delete cached LLM outputs first")
    g.add_argument("--limit-docs", type=int, default=0, help="dry run: use only N random documents (0 = all)")
    g.add_argument("--preview", type=int, default=15, help="examples written to preview.md")
    g.add_argument("--seed", type=int, default=42)

    g = p.add_argument_group("llm")
    g.add_argument("--llm-model", required=True, help="LiteLLM model string, e.g. azure/<deployment>")
    g.add_argument("--llm-kwargs", default="", help="JSON of extra litellm kwargs (api_base, api_version, ...)")
    g.add_argument("--temperature", type=float, default=None, help="omit to use the model default")
    g.add_argument("--workers", type=int, default=8)

    g = p.add_argument_group("context prefix")
    g.add_argument("--context-mode", choices=["llm", "metadata", "none"], default="metadata")
    g.add_argument("--max-doc-chars", type=int, default=120_000, help="document window sent to the LLM")
    g.add_argument("--meta-entity-key", default="company")
    g.add_argument("--meta-period-key", default="period")

    g = p.add_argument_group("question generation")
    g.add_argument("--queries-per-chunk", type=int, default=3)
    g.add_argument("--min-chunk-words", type=int, default=40, help="shorter chunks stay in the corpus but get no questions")
    g.add_argument("--min-q-words", type=int, default=5)
    g.add_argument("--max-q-words", type=int, default=50)
    g.add_argument("--max-copy-overlap", type=float, default=0.6, help="drop questions copying the passage")
    g.add_argument("--verify-answerable", action="store_true", help="extra LLM call per question")

    g = p.add_argument_group("mining")
    g.add_argument("--embed-model", default="sentence-transformers/all-MiniLM-L6-v2")
    g.add_argument("--query-prompt", default="", help="e.g. the retrieval instruction some embedders need")
    g.add_argument("--passage-prompt", default="")
    g.add_argument("--device", default=None)
    g.add_argument("--roundtrip-k", type=int, default=20, help="drop questions whose source chunk is not in top-k")
    g.add_argument("--range-min", type=int, default=0)
    g.add_argument("--range-max", type=int, default=40, help="negatives come from ranks [range-min, range-max)")
    g.add_argument("--neg-sampling", choices=["top", "random"], default="random")
    g.add_argument("--n-neg-embedding", type=int, default=3)
    g.add_argument("--n-neg-metadata", type=int, default=2)
    g.add_argument("--n-neg-random", type=int, default=1, help="easy negatives mixed in with the hard ones")
    g.add_argument("--eval-top-n", type=int, default=30, help="retriever depth stored for dev/test reranking eval")
    g.add_argument("--meta-top-pool", type=int, default=10)
    g.add_argument("--neg-ceiling-ratio", type=float, default=0.95,
                   help="negative score must be below this ratio of the positive score")
    g.add_argument("--exclude-adjacent", action=argparse.BooleanOptionalAction, default=True)
    g.add_argument("--min-negatives", type=int, default=1)
    g.add_argument("--fn-cross-encoder", default="", help="optional pretrained cross-encoder to catch false negatives")
    g.add_argument("--fn-margin", type=float, default=0.0)

    g = p.add_argument_group("split")
    g.add_argument("--split-key", default="doc_id", help="doc_id or a metadata key (e.g. company) to split by")
    g.add_argument("--dev-frac", type=float, default=0.1)
    g.add_argument("--test-frac", type=float, default=0.1)
    return p.parse_args()


def main() -> None:
    a = parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    a.out.mkdir(parents=True, exist_ok=True)
    if a.fresh:
        shutil.rmtree(a.out / "cache", ignore_errors=True)

    chunks = load_chunks(a.chunks)
    if a.limit_docs:
        docs = sorted({c.doc_id for c in chunks})
        keep = set(random.Random(a.seed).sample(docs, min(a.limit_docs, len(docs))))
        chunks = [c for c in chunks if c.doc_id in keep]
    log.info("loaded %d chunks from %d documents", len(chunks), len({c.doc_id for c in chunks}))

    llm = make_llm(a)
    store = DocStore(chunks, a.max_doc_chars)
    stage_context(chunks, a, llm, store)
    queries, rejected = stage_queries(chunks, a, llm)
    log.info("%d questions after generation filters; rejections: %s", len(queries), dict(rejected))
    examples, stats = stage_mine(chunks, queries, a)
    log.info("%d examples after mining; drops: %s", len(examples), dict(stats))
    if not examples:
        raise SystemExit("no examples survived; check preview inputs, --roundtrip-k and metadata keys")
    write_outputs(examples, chunks, queries, rejected, stats, a)


if __name__ == "__main__":
    main()
