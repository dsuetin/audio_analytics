"""Verify per-store coverage for all NEW results (requirement #14)."""
import json, pandas as pd
from collections import defaultdict
from pathlib import Path

REPO = Path("/home/developer/devbox/audio_analytics")
NEW = REPO / "regression_new"
REPORTS = REPO / "reports"

dates = ["2026-08-25","2026-08-26","2026-08-27","2026-08-28",
         "2026-08-29","2026-08-30","2026-08-31","2026-09-01"]
rows = []
for d in dates:
    dbg = NEW / d / "debug.json"
    inp = REPORTS / f"transcript_report_{d}.xlsx"
    if not dbg.exists() or not inp.exists():
        continue
    j = json.load(open(dbg, encoding="utf-8"))
    rows_by_store = defaultdict(list)
    for s in j["segments"]:
        rows_by_store[s["store_id"]].extend(list(range(s["start_row"], s["end_row"] + 1)))
    sh = pd.read_excel(inp, sheet_name=None)
    for store, raw in sh.items():
        nraw = len(raw)
        cov = rows_by_store.get(store, [])
        covered_set = set(cov)
        missing = [i for i in range(nraw) if i not in covered_set]
        dup = len(cov) - len(covered_set)
        rows.append({
            "date": d, "store": store[:30], "raw_rows": nraw,
            "covered": len(covered_set), "missing": len(missing),
            "duplicated": dup,
        })

df = pd.DataFrame(rows)
pd.set_option("display.width", 200)
print(df.to_string(index=False))
print("\nTOTAL missing:", df["missing"].sum(), " duplicated:", df["duplicated"].sum())
