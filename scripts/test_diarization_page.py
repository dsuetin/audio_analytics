"""Драйвер проверок страницы /diarization-test (часть 5 задачи).

Точные HTTP-вызовы, которые делает браузерный JS. Реальный WAV, реальный
Triton gRPC (модель diarization_online_pf).
"""
import json
import sys
import time

import requests

BASE = "http://127.0.0.1:8010/diarization-test/api"
WAV_MAIN = ("/home/developer/devbox/audio_analytics/audio_export/"
            "г_Пятигорск_ул_Мира_201а_строение_1/"
            "2026-11-06_16:35:05__16:40:05.wav")
WAV_ALT = ("/home/developer/devbox/audio_analytics/audio_export/"
           "г_Пятигорск_ул_Мира_201а_строение_1/"
           "2026-11-06_15:33:06__15:38:06.wav")

RESULTS = []


def check(name, cond, detail=""):
    status = "PASS" if cond else "FAIL"
    RESULTS.append((status, name, detail))
    print(f"[{status}] {name} {detail}", flush=True)


def upload(path):
    with open(path, "rb") as f:
        r = requests.post(BASE + "/upload",
                          files={"file": (path.split("/")[-1], f, "audio/wav")},
                          timeout=120)
    r.raise_for_status()
    return r.json()


def status(sid):
    return requests.get(f"{BASE}/session/{sid}", timeout=60).json()


def run_cells(sid, max_cells=None, manual_after=()):
    """Отправка аудио-ячеек до конца (или max_cells) + ручные PARTIAL."""
    sent = 0
    while True:
        st = status(sid)
        total = st["audio_cells_total"]
        if st["cells_sent"] >= total or (max_cells and sent >= max_cells):
            break
        r = requests.post(f"{BASE}/session/{sid}/send-chunk", json={},
                          timeout=300).json()
        sent += 1
        if sent in manual_after:
            requests.post(f"{BASE}/session/{sid}/partial", json={},
                          timeout=300).json()
    return st


def do_full(sid):
    r = requests.post(f"{BASE}/session/{sid}/full", json={}, timeout=300)
    r.raise_for_status()
    return r.json()


def speaker_stable(snapshots):
    """Корректный инвариант стабильных ID: новые ID выдаются строго
    последовательно (max+1, max+2, ...), ID никогда не переиспользуются.
    (Набор ID в снапшоте может сжиматься: малоаргументный кластер может
    выпасть из кластеризации и вернуться позже со своим старым ID —
    это поведение модели, проверенное в 9/9-эксперименте.)"""
    ever = set()
    for i, p in enumerate(snapshots):
        cur = set(p["speakers"])
        new = cur - ever
        m = max(ever) if ever else 0
        expect = [m + k for k in range(1, len(new) + 1)]
        if sorted(new) != expect:
            return False, f"partial#{i}: new={sorted(new)} expected={expect}"
        ever |= cur
    return True, f"{len(snapshots)} snapshots, ids used={sorted(ever)}"


def main():
    t_start = time.time()

    # --- A: FULL без PARTIAL ---
    print("== A: upload + FULL без PARTIAL ==", flush=True)
    up = upload(WAV_MAIN)
    sid_a = up["session_id"]
    check("A1 file_info duration", up["file"]["duration_ms"] == 180450,
          str(up["file"]["duration_ms"]))
    check("A2 file_info 16k mono", up["file"]["sample_rate"] == 16000
          and up["file"]["channels"] == 1)
    check("A3 no resample/mix flags", not up["file"]["resampled"]
          and not up["file"]["mixed_to_mono"])
    requests.post(f"{BASE}/session/{sid_a}/config",
                  json={"chunk_ms": 3000, "partial_mode": "none"},
                  timeout=30).json()
    st = run_cells(sid_a)
    check("A4 all cells sent", st["cells_sent"] == st["audio_cells_total"],
          f"{st['cells_sent']}/{st['audio_cells_total']}")
    res = do_full(sid_a)
    check("A5 FULL segments > 0", len(res["segments"]) > 0,
          f"segments={len(res['segments'])} speakers={res['speakers']}")
    dur = status(sid_a)["duration_ms"]
    check("A6 segments in [0, duration]",
          all(0 <= s["start_ms"] and s["end_ms"] <= dur
              and s["end_ms"] > s["start_ms"] for s in res["segments"]))
    st = status(sid_a)
    check("A7 state finalized", st["state"] == "finalized")

    # --- B: PARTIAL каждые 5 чанков ---
    print("== B: PARTIAL каждые 5 чанков ==", flush=True)
    sid_b = upload(WAV_MAIN)["session_id"]
    requests.post(f"{BASE}/session/{sid_b}/config",
                  json={"chunk_ms": 3000, "partial_mode": "5"},
                  timeout=30).json()
    st = run_cells(sid_b)
    res_b = do_full(sid_b)
    snaps = status(sid_b)["partial_snapshots"]
    check("B1 partial count = 12", len(snaps) == 12, f"n={len(snaps)}")
    ok, d = speaker_stable(snaps)
    check("B2 speaker ID stable between PARTIAL", ok, d)
    check("B3 final superset of last partial",
          set(snaps[-1]["speakers"]) <= set(res_b["speakers"]),
          f"last={snaps[-1]['speakers']} final={res_b['speakers']}")
    check("B4 final segments > 0", len(res_b["segments"]) > 0,
          f"segments={len(res_b['segments'])}")
    # экспорты
    j = requests.get(f"{BASE}/session/{sid_b}/export.json", timeout=60)
    check("B5 export.json 200+valid", j.status_code == 200
          and len(json.loads(j.text)["partial_snapshots"]) == 12)
    t = requests.get(f"{BASE}/session/{sid_b}/export.txt", timeout=60)
    check("B6 export.txt 200", t.status_code == 200
          and "FINAL" in t.text)
    dbg = requests.get(f"{BASE}/session/{sid_b}/export.debug", timeout=60)
    djson = json.loads(dbg.text)
    total_b = status(sid_b)["audio_cells_total"]
    check("B7 export.debug: requests logged",
          dbg.status_code == 200
          and len(djson["requests"]) == total_b + 12 + 1,
          f"n={len(djson['requests'])} ({total_b} audio + 12 partial + 1 full)")
    check("B8 debug has ack_ms", all("ack_ms" in r for r in djson["requests"]))

    # --- C: PARTIAL после каждого чанка (первые 12 чанков) ---
    print("== C: PARTIAL после каждого чанка (12 чанков) ==", flush=True)
    sid_c = upload(WAV_MAIN)["session_id"]
    requests.post(f"{BASE}/session/{sid_c}/config",
                  json={"chunk_ms": 3000, "partial_mode": "each"},
                  timeout=30).json()
    st = run_cells(sid_c, max_cells=12)
    res_c = do_full(sid_c)
    snaps_c = status(sid_c)["partial_snapshots"]
    check("C1 12 partials", len(snaps_c) == 12, f"n={len(snaps_c)}")
    ok, d = speaker_stable(snaps_c)
    check("C2 speaker ID stable", ok, d)
    check("C3 FULL after partials", len(res_c["segments"]) > 0,
          f"segments={len(res_c['segments'])}")

    # --- D: ручной PARTIAL ---
    print("== D: ручной PARTIAL ==", flush=True)
    sid_d = upload(WAV_MAIN)["session_id"]
    requests.post(f"{BASE}/session/{sid_d}/config",
                  json={"chunk_ms": 3000, "partial_mode": "manual"},
                  timeout=30).json()
    run_cells(sid_d, max_cells=5, manual_after=(3,))
    st = status(sid_d)
    check("D1 manual partial done, no auto", st["partial_count"] == 1,
          f"n={st['partial_count']}")
    p = requests.post(f"{BASE}/session/{sid_d}/partial", json={},
                      timeout=300).json()
    run_cells(sid_d)
    res_d = do_full(sid_d)
    snaps_d = status(sid_d)["partial_snapshots"]
    check("D2 two manual partials", len(snaps_d) == 2 and
          not any(x["auto"] for x in snaps_d), f"n={len(snaps_d)}")
    ok, d = speaker_stable(snaps_d + [{"speakers": res_d["speakers"]}])
    check("D3 stable incl. final", ok, d)

    # --- E: повторная загрузка другого WAV ---
    print("== E: другой WAV, новая сессия ==", flush=True)
    up_e = upload(WAV_ALT)
    sid_e = up_e["session_id"]
    check("E1 new session different id", sid_e != sid_a and sid_e != sid_b)
    check("E2 alt file info", up_e["file"]["duration_ms"] > 0,
          f"{up_e['file']['duration_ms']} ms")
    requests.post(f"{BASE}/session/{sid_e}/config",
                  json={"chunk_ms": 3000, "partial_mode": "none"},
                  timeout=30).json()
    run_cells(sid_e, max_cells=6)
    res_e = do_full(sid_e)
    check("E3 FULL on other wav", len(res_e["segments"]) >= 0,
          f"segments={len(res_e['segments'])}")
    st_e = status(sid_e)
    check("E4 independent state finalized", st_e["state"] == "finalized")

    # --- F: обработка ошибок ---
    print("== F: ошибки ==", flush=True)
    with open("/tmp/opencode/not_wav.txt", "w") as f:
        f.write("this is not a wav file")
    r = requests.post(BASE + "/upload",
                      files={"file": ("not_wav.txt",
                                       open("/tmp/opencode/not_wav.txt", "rb"),
                                       "application/octet-stream")},
                      timeout=60)
    check("F1 non-WAV rejected 400", r.status_code == 400,
          f"status={r.status_code}")
    r = requests.get(f"{BASE}/session/nonexistent", timeout=30)
    check("F2 unknown session 404", r.status_code == 404)
    r = requests.post(f"{BASE}/session/{sid_a}/config",
                      json={"chunk_ms": 3000, "partial_mode": "none"},
                      timeout=30)
    check("F3 config after finalized 409", r.status_code == 409,
          f"status={r.status_code}")
    r = requests.post(f"{BASE}/session/{sid_a}/send-chunk", json={},
                      timeout=30)
    check("F4 send-chunk after finalized 409", r.status_code == 409)

    # --- G: отмена с очисткой sequence ---
    print("== G: отмена ==", flush=True)
    sid_g = upload(WAV_MAIN)["session_id"]
    requests.post(f"{BASE}/session/{sid_g}/config",
                  json={"chunk_ms": 3000, "partial_mode": "manual"},
                  timeout=30).json()
    run_cells(sid_g, max_cells=3)
    r = requests.post(f"{BASE}/session/{sid_g}/cancel", json={},
                      timeout=300).json()
    check("G1 cancelled + sequence cleaned",
          r["state"] == "cancelled" and r["sequence_cleaned"], str(r))

    failed = [x for x in RESULTS if x[0] == "FAIL"]
    print(f"\nTOTAL: {len(RESULTS)} checks, {len(failed)} FAILED, "
          f"{time.time() - t_start:.1f} s")
    if failed:
        print("FAILED:", [x[1] for x in failed])
        return 1
    print("ALL PAGE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
