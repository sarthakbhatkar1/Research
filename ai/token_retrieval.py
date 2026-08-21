#!/usr/bin/env python3
"""
Count tokens in the `retrieved_contexts` column of one or more Excel files,
processed concurrently. Adds token-count columns and writes a new file per
input, plus prints a summary.

Usage:
    python count_context_tokens.py file1.xlsx file2.xlsx ...
    python count_context_tokens.py --folder ./data
    python count_context_tokens.py file1.xlsx --column retrieved_contexts --model gpt-4o --workers 4
"""

import argparse
import ast
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
from litellm import token_counter


def parse_contexts(raw):
    """retrieved_contexts is stored as a stringified list, e.g. "['a', 'b']".
    Falls back to treating it as a single plain string if parsing fails."""
    if pd.isna(raw):
        return []
    if isinstance(raw, list):
        return raw
    try:
        val = ast.literal_eval(str(raw))
        return val if isinstance(val, list) else [str(val)]
    except (ValueError, SyntaxError):
        return [str(raw)]


def count_row_tokens(contexts, model):
    if not contexts:
        return 0, 0
    per_chunk = [token_counter(model=model, text=c) for c in contexts]
    return sum(per_chunk), len(contexts)


def process_file(path: Path, column: str, model: str, output_dir: Path):
    df = pd.read_excel(path)
    if column not in df.columns:
        raise ValueError(f"Column '{column}' not found in {path.name}. Found: {list(df.columns)}")

    token_counts, chunk_counts = [], []
    for raw in df[column]:
        contexts = parse_contexts(raw)
        total_tokens, n_chunks = count_row_tokens(contexts, model)
        token_counts.append(total_tokens)
        chunk_counts.append(n_chunks)

    df[f"{column}_token_count"] = token_counts
    df[f"{column}_num_chunks"] = chunk_counts

    out_path = output_dir / f"{path.stem}_tokens{path.suffix}"
    df.to_excel(out_path, index=False)

    return {
        "file": path.name,
        "output": out_path.name,
        "rows": len(df),
        "total_tokens": sum(token_counts),
        "avg_tokens_per_row": round(sum(token_counts) / len(df), 1) if len(df) else 0,
        "max_tokens_row": max(token_counts) if token_counts else 0,
    }


def main():
    parser = argparse.ArgumentParser(description="Count tokens in retrieved_contexts columns across Excel files.")
    parser.add_argument("files", nargs="*", help="Excel file paths (.xlsx)")
    parser.add_argument("--folder", help="Process every .xlsx file in this folder instead of listing files")
    parser.add_argument("--column", default="retrieved_contexts", help="Column to count tokens for (default: retrieved_contexts)")
    parser.add_argument("--model", default="gpt-4o", help="Model name for tokenizer selection (default: gpt-4o)")
    parser.add_argument("--workers", type=int, default=4, help="Number of files to process concurrently (default: 4)")
    parser.add_argument("--output-dir", default=".", help="Where to write output files (default: current dir)")
    args = parser.parse_args()

    if args.folder:
        paths = sorted(Path(args.folder).glob("*.xlsx"))
    else:
        paths = [Path(f) for f in args.files]

    if not paths:
        print("No input files given. Pass file paths or --folder.")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_file, p, args.column, args.model, output_dir): p
            for p in paths
        }
        for fut in as_completed(futures):
            path = futures[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                print(f"FAILED: {path.name} -> {e}")

    print("\n--- Summary ---")
    for r in results:
        print(
            f"{r['file']}: {r['rows']} rows, {r['total_tokens']:,} total tokens, "
            f"avg {r['avg_tokens_per_row']}/row, max {r['max_tokens_row']} in a single row "
            f"-> saved as {r['output']}"
        )


if __name__ == "__main__":
    main()
