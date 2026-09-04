"""Run the NEW offline pipeline on every discovered old file (error-tolerant)."""
import sys, json, time, traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import lib  # noqa: E402

REPO = Path("/home/developer/devbox/audio_analytics")
REPORTS = [REPO / "reports", Path("/home/developer/offline_reports")]
OUT = REPO / "regression_new"
STATE = OUT / "_run_state.json"


def main(workers: int = 3):
    OUT.mkdir(parents=True, exist_ok=True)
    pairs = lib.discover_pairs(REPORTS)
    pairs.sort(key=lambda p: p["date"])
    results = []
    t_all = time.time()
    for i, rec in enumerate(pairs, 1):
        print(f"\n{'='*70}\n[{i}/{len(pairs)}] {rec['date']}  input={rec['input']}\n"
              f"       old_final={rec.get('old_final')}\n{'='*70}", flush=True)
        res = lib.run_new_file(rec, OUT, workers=workers)
        results.append(res)
        STATE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{rec['date']}] -> {res['status']}  "
              f"rows={res['input_rows']} segs={res['segments_total']} dialog={res['dialog_segments']} "
              f"customers={res['accepted_customers']} err={res.get('llm_errors')} "
              f"({res['elapsed_sec']}s)", flush=True)
    STATE.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nALL DONE in {time.time()-t_all:.0f}s. {len(results)} files.")
    ok = sum(1 for r in results if r["status"] == "OK")
    print(f"OK={ok}  FAILED={sum(1 for r in results if r['status']=='EMPTY')}  "
          f"ERROR={sum(1 for r in results if r['status']=='ERROR')}")


if __name__ == "__main__":
    w = int(sys.argv[1]) if len(sys.argv) > 1 else 3
    try:
        main(workers=w)
    except Exception:
        traceback.print_exc()
        sys.exit(1)
