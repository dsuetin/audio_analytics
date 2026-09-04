"""Match OLD (client_id-based blocks) vs NEW (LLM segments) per store and build the diff.

Both partitions are contiguous, in the SAME per-store 0-based raw row-index space:
  - OLD: index range derived from block start_time/end_time mapped to raw rows.
  - NEW: segment.start_index / segment.end_index (produced by the new pipeline).

Matching is on row-index overlap (robust to SPLIT / MERGE / boundary drift).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import pandas as pd


REPO = Path("/home/developer/devbox/audio_analytics")


@dataclass
class OldBlock:
    store_id: str
    block_id: str
    old_client_id: str
    index_start: int
    index_end: int
    start_time: str
    end_time: str
    role: str
    mission: str
    is_sale: bool
    loss_reason: str | None
    dialog_type: str
    rows: int
    raw: dict = field(default_factory=dict)


@dataclass
class NewSeg:
    store_id: str
    segment_id: str
    index_start: int
    index_end: int
    seg_type: str          # dialog|employee|background|unknown
    role: str | None       # classification role (dialog segs) or None
    mission: str | None
    is_sale: bool | None
    loss_reason: str | None
    dialog_type: str | None
    rows: int
    old_client_ids: list = field(default_factory=list)
    raw: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Load OLD blocks (from old debug JSON, mapped to raw row index space)
# ---------------------------------------------------------------------------

def _load_raw_index(store_df: pd.DataFrame) -> list[tuple[datetime, object]]:
    df = store_df.copy()
    df["created_at"] = pd.to_datetime(df["created_at"], errors="coerce")
    df = df.dropna(subset=["created_at"])
    df = df.sort_values("created_at", kind="stable").reset_index(drop=True)
    return df


def _idx_for_time(times: list[datetime], t: datetime) -> tuple[int, int]:
    ts = [x.timestamp() for x in times]
    tstamp = t.timestamp()
    import bisect
    lo = bisect.bisect_left(ts, tstamp)
    hi = bisect.bisect_right(ts, tstamp)
    start = max(0, min(len(times) - 1, lo))
    end = max(0, min(len(times) - 1, hi - 1))
    return start, end


def load_old_blocks(rec: dict, raw_path: str) -> list[OldBlock]:
    """Old blocks mapped into the raw-store row-index space."""
    out: list[OldBlock] = []
    old_final = rec.get("old_final")
    old_debug = rec.get("old_debug")
    if not (old_final and Path(old_final).exists()):
        return out
    # build raw index per store
    sheets = pd.read_excel(raw_path, sheet_name=None)
    # find old debug (fallback to alternate filename)
    debug_path = old_debug
    if not (debug_path and Path(debug_path).exists()):
        # try the alternate legacy filename
        cand = Path(REPO / "reports" / (Path(old_final).name.replace("final_transcript_report_", "final_") ).name)
        # old_final name: final_transcript_report_DATE.xlsx -> final_DATE_debug.json
        alt = Path(REPO / "reports" /
                  Path(old_final).name.replace("final_transcript_report_", "").replace(".xlsx", "")
                  .replace(".xlsx", ""))
        for cand in (
            Path(REPO / "reports" / (Path(old_final).stem.replace("final_transcript_report_", "final_") + "_debug.json")),
        ):
            if cand.exists():
                debug_path = str(cand)
                break
    blocks = []
    if debug_path and Path(debug_path).exists():
        d = json.load(open(debug_path, encoding="utf-8"))
        blocks = d.get("blocks", [])
    raw_index = {sid: _load_raw_index(df) for sid, df in sheets.items()}
    for b in blocks:
        sid = b.get("store_id")
        df = raw_index.get(sid)
        if df is None or len(df) == 0:
            continue
        times = list(df["created_at"])
        st = pd.Timestamp(b.get("start_time"))
        et = pd.Timestamp(b.get("end_time"))
        if pd.isna(st) or pd.isna(et):
            continue
        is_, ie = _idx_for_time(list(times), st.to_pydatetime())
        _, ie2 = _idx_for_time(list(times), et.to_pydatetime())
        ie = ie2
        out.append(OldBlock(
            store_id=sid,
            block_id=b.get("block_id", ""),
            old_client_id=b.get("final_client_id") or b.get("existing_client_id") or "",
            index_start=is_,
            index_end=max(is_, ie),
            start_time=b.get("start_time", ""),
            end_time=b.get("end_time", ""),
            role=b.get("role") or "unknown",
            mission=b.get("mission") or "",
            is_sale=bool(b.get("is_sale")),
            loss_reason=b.get("loss_reason"),
            dialog_type=b.get("dialog_type") or "",
            rows=int(b.get("rows", 0)),
            raw=b,
        ))
    out.sort(key=lambda x: (x.store_id, x.index_start))
    return out


def load_new_segments(rec: dict) -> list[NewSeg]:
    dbg = rec.get("debug")
    if not (dbg and Path(dbg).exists()):
        return []
    d = json.load(open(dbg, encoding="utf-8"))
    segs: list[NewSeg] = []
    for s in d.get("segments", []):
        segs.append(NewSeg(
            store_id=s.get("store_id"),
            segment_id=s.get("segment_id", ""),
            index_start=int(s.get("start_row", 0)),
            index_end=int(s.get("end_row", 0)),
            seg_type=s.get("segment_type") or "unknown",
            role=s.get("classification_role"),
            mission=s.get("classification_mission"),
            is_sale=s.get("is_sale"),
            loss_reason=s.get("loss_reason"),
            dialog_type=None,
            rows=int(s.get("row_count", 0)),
            old_client_ids=s.get("old_client_ids", []),
            raw=s,
        ))
    segs.sort(key=lambda x: (x.store_id, x.index_start))
    return segs


# ---------------------------------------------------------------------------
# Overlap matching (bipartite, per store, greedy on index overlap)
# ---------------------------------------------------------------------------

def _overlap(a_start, a_end, b_start, b_end) -> int:
    return max(0, min(a_end, b_end) - max(a_start, b_start) + 1)


def match(old_blocks: list, new_segs: list) -> list[dict]:
    """Return a list of match records covering ALL old blocks and ALL new segments.

    change_type in {MATCHED, CHANGED, SPLIT, MERGE, ADDED, REMOVED}.
    One record per (cluster of old blocks <-> cluster of new segments).
    """
    from collections import defaultdict
    old_by_store = defaultdict(list)
    for b in old_blocks:
        old_by_store[b.store_id].append(b)
    new_by_store = defaultdict(list)
    for s in new_segs:
        new_by_store[s.store_id].append(s)

    records = []
    matched_old = id_set = set()
    matched_new = set()

    for store in set(list(old_by_store) + list(new_by_store)):
        OB = old_by_store.get(store, [])
        NS = new_by_store.get(store, [])

        # candidate edges with score = overlap (absolute index rows)
        edges = []
        for oi, o in enumerate(OB):
            for ni, n in enumerate(NS):
                ov = _overlap(o.index_start, o.index_end, n.index_start, n.index_end)
                if ov > 0:
                    norm = ov / max(1, min(o.index_end - o.index_start + 1, n.index_end - n.index_start + 1))
                    edges.append((norm * ov, ov, norm, oi, ni))
        # prefer strong overlap
        edges.sort(key=lambda e: (-e[0], -e[1]))
        pair_old = {}
        pair_new = {}
        for _, ov, norm, oi, ni in edges:
            if oi in pair_old or ni in pair_new:
                continue
            if norm < 0.15 and ov < 2:
                continue
            pair_old[oi] = ni
            pair_new[ni] = oi

        # build clusters (union-find on matched pairs)
        parent = {}
        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for oi, ni in pair_old.items():
            union(("o", oi), ("n", ni))

        clusters = defaultdict(lambda: {"old": set(), "new": set()})
        for key in list(parent):
            r = find(key)
            clusters[r][key[0]].add(key[1])
        # add unpaired
        for oi in range(len(OB)):
            if oi not in pair_old:
                r = ("o", oi)
                clusters[r]["old"].add(oi)
        for ni in range(len(NS)):
            if ni not in pair_new:
                r = ("n", ni)
                clusters[r]["new"].add(ni)

        for c in clusters.values():
            olist = [OB[i] for i in sorted(c["old"])] if c["old"] else []
            nlist = [NS[i] for i in sorted(c["new"])] if c["new"] else []
            if not olist and not nlist:
                continue
            if olist and nlist:
                # classify change
                if len(olist) == 1 and len(nlist) == 1:
                    ctype = _classify_changed(olist[0], nlist[0])
                elif len(olist) == 1 and len(nlist) > 1:
                    ctype = "SPLIT"
                elif len(olist) > 1 and len(nlist) == 1:
                    ctype = "MERGE"
                else:
                    ctype = "MERGE_SPLITS"
            elif olist and not nlist:
                ctype = "REMOVED"
            else:
                ctype = "ADDED"
            records.append({
                "store_id": store,
                "change_type": ctype,
                "old": olist,
                "new": nlist,
                "old_client_ids": [b.old_client_id for b in olist],
                "new_segment_ids": [s.segment_id for s in nlist],
                "old_index_range": (min(b.index_start for b in olist), max(b.index_end for b in olist)) if olist else None,
                "new_index_range": (min(s.index_start for s in nlist), max(s.index_end for s in nlist)) if nlist else None,
            })
    return records


def _classify_changed(ob, ns) -> str:
    """Compare classification of one old block vs one new segment."""
    def role_of(o):
        return o.role  # old role
    def new_role(n):
        if n.seg_type == "dialog":
            return n.role  # classification role
        return n.seg_type  # employee/background/unknown
    def norm(x):
        return (x or "").strip().lower()
    diffs = []
    if norm(role_of(ob)) != norm(new_role(ns)):
        diffs.append("role")
    if norm(ob.mission) != norm(ns.mission):
        diffs.append("mission")
    if bool(ob.is_sale) != bool(ns.is_sale):
        diffs.append("is_sale")
    lr_o = (ob.loss_reason or "").strip()
    lr_n = (ns.loss_reason or "").strip()
    if lr_o != lr_n:
        diffs.append("loss_reason")
    # boundary drift
    drift = abs(ob.index_start - ns.index_start) + abs(ob.index_end - ns.index_end)
    if ob.index_end - ob.index_start + 1 != ns.index_end - ns.index_start + 1:
        diffs.append("length")
    return "CHANGED" if diffs else "MATCHED"
