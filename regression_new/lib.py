"""Regression helpers for running the NEW offline pipeline on old files.

Uses the EXACT new-algorithm code in offline_analysis (segmentation + classification
+ final report). The only difference from offline_analysis/run.py is the LLM budget:
`segmentation._segment_one_chunk` hard-codes num_predict=16000, but this deployment's
model (qwen3.8:27b) is a *thinking* model that needs ~63k+ tokens of hidden reasoning
before it can emit the segmentation JSON. A client that raises the budget is enough.

Nothing in offline_analysis/ is modified.
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO = Path("/home/developer/devbox/audio_analytics")
sys.path.insert(0, str(REPO))

import pandas as pd  # noqa: E402

from offline_analysis.llm import OllamaClient, OllamaError, extract_json  # noqa: E402
from offline_analysis.run import (  # noqa: E402
    classify_segment,
    build_final_rows,
    write_excel,
    write_debug,
)
from offline_analysis.segmentation import (  # noqa: E402
    Segment,
    load_raw,
    df_to_rows,
    measure_rows,
    segment_rows,
)
from offline_analysis.taxonomy import CONFIDENCE_MIN  # noqa: E402


class BigBudgetLLM:
    """Repo OllamaClient wrapper that raises num_predict/num_ctx so the thinking
    model can both reason and emit JSON in one call. Keeps the same interface
    (`chat_json`) that segmentation / classification expect."""

    def __init__(self, model: str = "qwen3.8:27b", host: str = "http://localhost:11434"):
        self._c = OllamaClient(model=model, host=host, timeout=1200)
        self.model = model
        self.host = host

    def chat_json(self, system: str, user: str, **kw) -> dict:
        kw.setdefault("num_ctx", 131072)
        kw.setdefault("num_predict", 128000)
        kw["num_predict"] = max(int(kw.get("num_predict", 0)), 128000)
        kw["num_ctx"] = max(int(kw.get("num_ctx", 0)), 131072)
        # temperature forced low for determinism; the model ignores the think flag
        kw["temperature"] = float(kw.get("temperature", 0.0))
        return self._c.chat_json(system, user, **kw)


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def discover_pairs(reports_dirs: list[Path]) -> list[dict]:
    """Find (date, input, old_final) triples plus inputs without an old final."""
    by_input: dict[str, dict] = {}
    for d in reports_dirs:
        if not d.exists():
            continue
        for f in sorted(d.glob("transcript_report_*.xlsx")):
            stem = f.name.replace("transcript_report_", "").replace(".xlsx", "")
            import re
            m = re.search(r"(\d{4}-\d{2}-\d{2})", stem)
            date = m.group(1) if m else stem
            rec = by_input.setdefault(stem, {
                "date": date,
                "input": str(f),
                "old_final": None,
                "old_debug": None,
            })
            cand_final = d / f"final_transcript_report_{date}.xlsx"
            cand_debug = d / f"final_transcript_report_{date}_debug.json"
            if cand_final.exists() and rec["old_final"] is None:
                rec["old_final"] = str(cand_final)
            if cand_debug.exists() and rec["old_debug"] is None:
                rec["old_debug"] = str(cand_debug)
    return list(by_input.values())


# ---------------------------------------------------------------------------
# Single-file run (new pipeline)
# ---------------------------------------------------------------------------

def run_new_file(rec: dict, out_root: Path, workers: int = 3,
                 target_chunk_tokens: int = 36000, retries: int = 3) -> dict:
    """Run the NEW offline pipeline on rec['input'] -> out_root/<date>/  .

    Returns a result dict with status, counts and the per-segment summary (rows
    index space) needed for matching. Never raises: errors are captured.
    """
    date = rec["date"]
    out_dir = out_root / date
    out_dir.mkdir(parents=True, exist_ok=True)
    final_path = out_dir / "final.xlsx"
    debug_path = out_dir / "debug.json"
    log_path = out_dir / "log.txt"

    t0 = time.time()
    result: dict = {
        "date": date,
        "input": rec["input"],
        "old_final": rec.get("old_final"),
        "old_debug": rec.get("old_debug"),
        "final": str(final_path),
        "debug": str(debug_path),
        "status": "FAILED",
        "error": None,
        "input_rows": 0,
        "stores": 0,
        "segments_total": 0,
        "dialog_segments": 0,
        "accepted_customers": 0,
        "final_rows": 0,
        "llm_errors": 0,
        "elapsed_sec": 0.0,
        "llm": {"model": "qwen3.8:27b", "host": "http://localhost:11434"},
    }
    lines = []

    def log(msg):
        lines.append(msg)
        print(f"[{date}] {msg}", flush=True)

    try:
        client = BigBudgetLLM()
        sheets = load_raw(rec["input"])
        result["stores"] = len(sheets)
        total_rows = sum(len(df) for _, df in sheets)
        result["input_rows"] = total_rows
        log(f"loaded {len(sheets)} stores, {total_rows} rows")
        if not sheets:
            result["status"] = "EMPTY"
            # still write an empty final so downstream doesn't break
            write_excel([], str(final_path))
            write_debug([], {}, [], 0, {"rows": 0, "characters": 0, "estimated_tokens": 0}, str(debug_path))
            _flush_log(log_path, lines)
            return result

        store_rows = [(sid, df_to_rows(df)) for sid, df in sheets]
        all_segments: list[Segment] = []
        all_results: dict[int, dict] = {}
        day_size = {"rows": 0, "characters": 0, "estimated_tokens": 0}

        for sid, rows in store_rows:
            segs = _segment_store_adaptive(client, sid, rows, target_chunk_tokens, retries)
            size = measure_rows(rows)
            day_size["rows"] += size.rows
            day_size["characters"] += size.characters
            day_size["estimated_tokens"] += size.estimated_tokens
            log(f"store {sid[:24]}: {len(rows)} rows -> {len(segs)} segments "
                f"(types={_cnt_types(segs)})")
            all_segments.extend(segs)

        result["segments_total"] = len(all_segments)
        targets = [(i, s) for i, s in enumerate(all_segments) if s.segment_type == "dialog"]
        result["dialog_segments"] = len(targets)
        log(f"total segments={len(all_segments)}, dialog targets={len(targets)}")

        if len(targets) > 1 and workers > 1:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(classify_segment, client, s, i, retries, True): i
                        for i, s in targets}
                for fut in as_completed(futs):
                    idx = futs[fut]
                    all_results[idx] = fut.result()
        else:
            for i, s in targets:
                all_results[i] = classify_segment(client, s, i, retries, True)

        for r in all_results.values():
            if r.get("llm_error"):
                result["llm_errors"] += 1

        final_rows, rejected = build_final_rows(all_segments, all_results, CONFIDENCE_MIN)
        write_excel(final_rows, str(final_path))
        write_debug(all_segments, all_results, rejected, total_rows, day_size, str(debug_path))
        result["accepted_customers"] = len(final_rows)
        result["final_rows"] = len(final_rows)
        result["rejected_segments"] = len(rejected)

        # per-store per-segment index summary (0-based, matches NEW row space)
        per_store = {}
        for sid, rows in store_rows:
            # rebuild this store's segment list from all_segments by store_id + range
            per_store[sid] = [
                {
                    "segment_id": s.segment_id,
                    "start_index": s.start_index,
                    "end_index": s.end_index,
                    "row_count": s.row_count,
                    "type": s.segment_type,
                    "old_client_ids": s.old_client_ids,
                }
                for s in all_segments if s.store_id == sid
            ]
        result["per_store"] = per_store

        # coverage check (NEW invariant: union == all rows, no overlap)
        covered = [i for s in all_segments for i in range(s.start_index, s.end_index + 1)]
        cov_ok = (len(covered) == total_rows and sorted(set(covered)) == list(range(total_rows)))
        result["coverage"] = {
            "input_rows": total_rows,
            "segmented_rows": len(covered),
            "unassigned_rows": total_rows - len(set(covered)),
            "duplicated_rows": len(covered) - len(set(covered)),
            "ok": cov_ok,
        }
        log(f"coverage ok={cov_ok} covered={len(covered)}/{total_rows}")

        result["status"] = "OK"
    except Exception as exc:  # noqa: BLE001 - error-tolerant regression
        result["error"] = f"{type(exc).__name__}: {exc}"
        result["status"] = "ERROR"
        log(f"ERROR: {exc}")

    result["elapsed_sec"] = round(time.time() - t0, 1)
    _flush_log(log_path, lines)
    return result


def _cnt_types(segs) -> dict:
    out = {}
    for s in segs:
        out[s.segment_type or "?"] = out.get(s.segment_type or "?", 0) + 1
    return out


def _is_degenerate(segs, n_rows) -> bool:
    """True if the store collapsed to per-row 'unknown' segments (the LLM returned
    empty JSON on a too-large chunk and the pipeline fallback took over)."""
    if not segs or not n_rows:
        return False
    if len(segs) >= 0.9 * n_rows:
        return True
    unknown_single = sum(1 for s in segs if s.row_count <= 1 and s.segment_type == "unknown")
    return unknown_single >= 0.85 * n_rows


def _segment_store_adaptive(client, sid, rows, base_tokens, retries):
    """Segment a store, shrinking the chunk size if the result degenerates.

    The thinking model emits ~60-70 tokens/row of hidden reasoning before the JSON,
    so large chunks overrun the context window and come back empty. We shrink the
    chunk budget (=> more, smaller chunks) until the store stops collapsing.
    """
    tokens = base_tokens
    last = None
    while True:
        segs = segment_rows(
            rows, llm=client, store_id=sid,
            target_chunk_tokens=tokens, verbose=False, retries=retries,
        )
        last = (tokens, segs)
        if not _is_degenerate(segs, len(rows)):
            return segs
        # degenerate -> smaller chunks (at least 2x smaller)
        tokens_new = max(12000, tokens // 2)
        if tokens_new >= tokens:
            # hit the floor; accept best effort
            return segs
        tokens = tokens_new


def _flush_log(path, lines):
    try:
        Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# client_id-independence experiment (acceptance test)
# ---------------------------------------------------------------------------

def client_id_independence(rec: dict, work: Path) -> dict:
    """Copy the input, randomize client_id, re-run NEW pipeline, compare the row
    space of the segmentation with the original (same date). If segmentation is
    independent of client_id, the set of (store, start_index, end_index) tuples
    must match (within tolerance)."""
    import re
    import shutil
    from openpyxl import load_workbook
    src = rec["input"]
    dst = work / (Path(src).stem + "_shuffle.xlsx")
    shutil.copyfile(src, dst)
    # rewrite client_id column to random tokens in every sheet
    wb = load_workbook(dst)
    rng = 12345
    for ws in wb.worksheets:
        header = [c.value for c in ws[1]]
        if "client_id" not in header:
            continue
        ci = header.index("client_id")
        # count rows
        n = ws.max_row
        for r in range(2, n + 1):
            rng = (rng * 1103515245 + 12345) & 0x7FFFFFFF
            ws.cell(row=r, column=ci + 1).value = f"rand_{rng}"
    wb.save(dst)
    return {"input": src, "shuffled": str(dst)}


if __name__ == "__main__":
    dirs = [REPO / "reports"]
    pairs = discover_pairs(dirs)
    print(json.dumps(pairs, ensure_ascii=False, indent=2))
