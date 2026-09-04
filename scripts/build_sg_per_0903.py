#!/usr/bin/env python3
"""
Experimental single-store 10-minute WAV export (READ-ONLY vs S3).

  store  = Пятигорск — Первомайская  (г_Пятигорск_ул_Первомайская_д_34)
  date   = 2026-09-03
  window = 11:30:00 .. 11:40:00  (600 s)

Input source (fresh, not a prior export):
  * PostgreSQL `transcripts` rows for the store on this date whose
    session_id-embedded audio start is inside the window, exported to CSV.
  * Each row carries its own session_id (store token is part of it).

Assembly:
  * anchor each session's audio at its true wall-clock start = the HH:MM:SS
    embedded in the session_id (NOT created_at, which carries ASR latency)
  * download the S3 WAV chunks of that session (16 kHz mono PCM16),
    concatenate them in chunk order, place on the 600 s buffer
  * clip a segment at the 600 s edge
  * gaps between utterances stay silence  -> NO foreign audio substituted
Outputs:
  * one contiguous .wav  (deliverables/Пятигорск_Первомайская_2026-09-03_11-30_11-40.wav)
  * records_<window>.json + meta.json for verification
"""
import csv
import io
import json
import os
import re
import wave
from datetime import datetime, timedelta

import boto3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_IN = "/tmp/opencode/sg_per_window.csv"          # 128 rows, store-verified
OUTDIR = os.path.join(ROOT, "deliverables")
os.makedirs(OUTDIR, exist_ok=True)

STORE = "г_Пятигорск_ул_Первомайская_д_34"
DATE = "20260903"
START = datetime(2026, 9, 3, 11, 30, 0)
TOTAL_SEC = 600
SR = 16000
CH = 1
W = 2          # 16-bit

OUT_NAME = "Пятигорск_Первомайская_2026-09-03_11-30_11-40.wav"
OUT_PATH = os.path.join(OUTDIR, OUT_NAME)

BUCKET = "audio-sessions"
client = boto3.client(
    "s3",
    endpoint_url="http://localhost:9000",
    aws_access_key_id="minioadmin",
    aws_secret_access_key="minioadmin",
    region_name="us-east-1",
)


def list_obj(prefix):
    out, tok = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix}
        if tok:
            kw["ContinuationToken"] = tok
        r = client.list_objects_v2(**kw)
        out += r.get("Contents", [])
        if r.get("IsTruncated"):
            tok = r["NextContinuationToken"]
        else:
            break
    return out


def sid_audio_start(sid):
    t = sid.split("-")[1]
    return START.replace(hour=int(t[0:2]), minute=int(t[2:4]), second=int(t[4:6]))


# ---- 1) load DB rows (already store-filtered) ----
rows = list(csv.DictReader(open(CSV_IN, encoding="utf-8")))
records = []
for r in rows:
    sid = r["session_id"]
    if STORE not in sid:                      # hard guard: must be Первомайская
        raise RuntimeError(f"foreign store in sid: {sid}")
    if sid.split("-")[0] != DATE:
        raise RuntimeError(f"wrong date in sid: {sid}")
    records.append({
        "sid": sid,
        "text": (r.get("recognition_text") or "").strip(),
        "created_msk": r["created_msk"],
        "astart": sid_audio_start(sid),       # anchor = audio start
    })
records.sort(key=lambda x: x["astart"])
print("sessions from DB in window:", len(records))

# ---- 2) download chunks, place on 600 s buffer ----
buf = bytearray(TOTAL_SEC * SR * CH * W)
total_chunks = 0
missing = []
segments = []          # (start_s, end_s, sid, audio_s)
for rec in records:
    objs = list_obj(f"audio/{rec['sid']}/")
    if not objs:
        missing.append(rec["sid"])
        rec["audio_frames"] = 0
        continue
    objs.sort(key=lambda o: (int(re.match(r"\d+", o["Key"].rsplit("/", 1)[-1]).group()), o["Key"]))
    pcm = bytearray()
    for o in objs:
        body = client.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read()
        with wave.open(io.BytesIO(body), "rb") as wf:
            assert (wf.getnchannels(), wf.getframerate(), wf.getsampwidth()) == (CH, SR, W), \
                f"unexpected wav fmt {o['Key']}"
            pcm += wf.readframes(wf.getnframes())
    total_chunks += len(objs)

    off_s = (rec["astart"] - START).total_seconds()
    off = int(off_s) * SR
    n = len(pcm) // (CH * W)
    tail = SR * TOTAL_SEC
    if off + n > tail:                       # clip beyond 600 s
        n = tail - off
        pcm = pcm[: n * CH * W]
    if n <= 0 or not pcm:
        rec["audio_frames"] = 0
        continue
    rec["audio_frames"] = n
    rec["aend"] = rec["astart"] + timedelta(seconds=n / SR)
    buf[off * CH * W: off * CH * W + len(pcm)] = pcm
    segments.append((off_s, off_s + n / SR, rec["sid"], n / SR))

placed = sum(1 for r in records if r.get("audio_frames", 0) > 0)
print("sessions with audio placed:", placed, "| missing audio:", len(missing),
      "| total chunks downloaded:", total_chunks)

# ---- 3) gap analysis across placed segments ----
segments.sort(key=lambda s: s[0])
big_gaps = []
prev_end = None
for (s0, s1, sid, asz) in segments:
    if prev_end is not None and s0 > prev_end:
        d = s0 - prev_end
        if d > 1.0:
            big_gaps.append({
                "after": (START + timedelta(seconds=prev_end)).strftime("%H:%M:%S"),
                "before": (START + timedelta(seconds=s0)).strftime("%H:%M:%S"),
                "seconds": round(d, 3),
            })
    prev_end = max(prev_end or 0, s1)
print("gaps > 1.0 s between placed segments:", len(big_gaps))

# ---- 4) write ONE contiguous WAV ----
with wave.open(OUT_PATH, "wb") as wf:
    wf.setnchannels(CH)
    wf.setsampwidth(W)
    wf.setframerate(SR)
    wf.writeframes(bytes(buf))
print("WAV written:", OUT_PATH, os.path.getsize(OUT_PATH), "bytes")

# ---- 5) verification record ----
rec_file = os.path.join(OUTDIR, "records_sg_0903_1130_1140.json")
recs = [
    {
        "audio_start": r["astart"].strftime("%H:%M:%S"),
        "audio_end": r.get("aend").strftime("%H:%M:%S") if r.get("aend") else None,
        "audio_seconds": round(r.get("audio_frames", 0) / SR, 3),
        "session_id": r["sid"],
        "stored_transcript_ts_msk": r["created_msk"],
        "transcript": r["text"],
    }
    for r in records
]
with open(rec_file, "w") as f:
    json.dump(recs, f, ensure_ascii=False, indent=2)

# WAV ground-truth for post-build checks
with wave.open(OUT_PATH, "rb") as wf:
    frames = wf.getnframes()
    dur = frames / wf.getframerate()
    sr_ = wf.getframerate()
    ch_ = wf.getnchannels()
    sw_ = wf.getsampwidth()

# non-zero coverage (where real audio sits)
nz_first = None
nz_last = None
step = SR // 10  # 0.1 s bins
for i in range(0, len(buf), step * CH * W):
    if any(buf[i:i + step * CH * W]):
        pos = i // (CH * W) / SR
        if nz_first is None:
            nz_first = pos
        nz_last = pos

meta = {
    "store": STORE,
    "store_label": "Пятигорск — Первомайская",
    "date": "2026-09-03",
    "window": [START.strftime("%H:%M:%S"), (START + timedelta(seconds=TOTAL_SEC)).strftime("%H:%M:%S")],
    "sessions_in_db_window": len(records),
    "sessions_with_audio": placed,
    "missing_audio_sessions": missing,
    "total_chunks_downloaded": total_chunks,
    "gaps_gt_1s": big_gaps,
    "wav": OUT_PATH,
    "wav_size_bytes": os.path.getsize(OUT_PATH),
    "wav_duration_s": round(dur, 3),
    "sample_rate": sr_,
    "channels": ch_,
    "sample_width_bytes": sw_,
    "first_audio_offset_s": round(nz_first, 3) if nz_first is not None else None,
    "first_audio_clock": (START + timedelta(seconds=nz_first)).strftime("%H:%M:%S.%f") if nz_first is not None else None,
    "last_audio_offset_s": round(nz_last, 3) if nz_last is not None else None,
    "last_audio_clock": (START + timedelta(seconds=nz_last)).strftime("%H:%M:%S.%f") if nz_last is not None else None,
}
with open(os.path.join(OUTDIR, "meta_sg_0903.json"), "w") as f:
    json.dump(meta, f, ensure_ascii=False, indent=2)

print("\n==== POST-BUILD CHECKS ====")
for k in ("wav_duration_s", "sample_rate", "channels", "sample_width_bytes",
          "total_chunks_downloaded", "sessions_with_audio", "missing_audio_sessions",
          "first_audio_clock", "last_audio_clock"):
    print(f"  {k}: {meta[k]}")
print("  gaps >1s count:", len(big_gaps))
for g in big_gaps[:8]:
    print(f"    gap {g['seconds']}s  {g['after']} -> {g['before']}")
print("records saved:", rec_file)
