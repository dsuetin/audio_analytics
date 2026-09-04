#!/usr/bin/env python3
"""
Experimental multi-window WAV export (READ-ONLY vs S3).
Store = Пятигорск — Первомайская (г_Пятигорск_ул_Первомайская_д_34)
Date  = 2026-09-03
Windows = 3 selected 4-min intervals (by text density + gap analysis).
Source = PostgreSQL transcripts table (not a prior export).
Output = 3 individual WAV files in deliverables/.
"""
import csv, io, json, os, re, wave
from datetime import datetime, timedelta
import boto3

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_IN = "/tmp/opencode/sg_per_1200plus.csv"
OUTDIR = os.path.join(ROOT, "deliverables")
os.makedirs(OUTDIR, exist_ok=True)

STORE = "г_Пятигорск_ул_Первомайская_д_34"
SR = 16000; CH = 1; SW = 2
BUCKET = "audio-sessions"

client = boto3.client(
    "s3", endpoint_url="http://localhost:9000",
    aws_access_key_id="minioadmin", aws_secret_access_key="minioadmin",
    region_name="us-east-1",
)

def h2s(v):
    v=int(v); return (v//10000)*3600+((v%10000)//100)*60+(v%100)
def s2h(v):
    return f"{v//3600:02d}:{(v%3600)//60:02d}:{v%60:02d}"

def list_obj(prefix):
    out, tok = [], None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix}
        if tok: kw["ContinuationToken"] = tok
        r = client.list_objects_v2(**kw)
        out += r.get("Contents", [])
        if r.get("IsTruncated"): tok = r["NextContinuationToken"]
        else: break
    return out

# Load data
rows = list(csv.DictReader(open(CSV_IN, encoding="utf-8")))
data = []
for r in rows:
    sid = r["session_id"]
    assert STORE in sid, f"foreign store: {sid}"
    data.append({
        "start": h2s(r["substr_sid_time"]),
        "sid": sid,
        "text": r["recognition_text"],
    })
data.sort(key=lambda x: x["start"])

# Three windows
WINDOWS = [
    (12*3600+12*60, 12*3600+16*60),    # 12:12:00-12:16:00
    (12*3600+19*60, 12*3600+23*60),    # 12:19:00-12:23:00
    (13*3600+25*60, 13*3600+29*60),    # 13:25:00-13:29:00
]

results = []
for wi, (s0, s1) in enumerate(WINDOWS, 1):
    dur_s = s1 - s0
    buf = bytearray(dur_s * SR * CH * SW)
    sess_in = [d for d in data if s0 <= d["start"] < s1]
    total_chunks = 0
    missing = []
    placed = 0
    segments = []

    for sess in sess_in:
        sid = sess["sid"]
        objs = list_obj(f"audio/{sid}/")
        if not objs:
            missing.append(sid)
            continue
        objs.sort(key=lambda o: (int(re.match(r"\d+", o["Key"].rsplit("/",1)[-1]).group()), o["Key"]))
        pcm = bytearray()
        for o in objs:
            body = client.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read()
            with wave.open(io.BytesIO(body), "rb") as wf:
                assert (wf.getnchannels(), wf.getframerate(), wf.getsampwidth()) == (CH, SR, SW), o["Key"]
                pcm += wf.readframes(wf.getnframes())
        total_chunks += len(objs)

        offset_in_window = sess["start"] - s0
        off = offset_in_window * SR
        n = len(pcm) // (CH * SW)
        if off + n > dur_s * SR:
            n = dur_s * SR - off
            pcm = pcm[:n * CH * SW]
        if n <= 0:
            continue
        buf[off * CH * SW : off * CH * SW + n * CH * SW] = pcm
        placed += 1
        segments.append((offset_in_window, offset_in_window + n/SR))

    fname = f"Пятигорск_Первомайская_2026-09-03_{s2h(s0).replace(':','-')}_{s2h(s1).replace(':','-')}.wav"
    fpath = os.path.join(OUTDIR, fname)
    with wave.open(fpath, "wb") as wf:
        wf.setnchannels(CH); wf.setsampwidth(SW); wf.setframerate(SR)
        wf.writeframes(bytes(buf))

    with wave.open(fpath, "rb") as wf:
        frames = wf.getnframes(); dur = frames / wf.getframerate()

    audio_s = sum(r[1]-r[0] for r in segments)
    max_gap = 0
    if len(segments) > 1:
        segments.sort()
        for i in range(1, len(segments)):
            g = segments[i][0] - segments[i-1][1]
            if g > 0: max_gap = max(max_gap, g)

    meta = {
        "window_index": wi,
        "store": STORE,
        "date": "2026-09-03",
        "window_start": s2h(s0),
        "window_end": s2h(s1),
        "duration_s": dur,
        "sessions": len(sess_in),
        "sessions_placed": placed,
        "missing_audio": missing,
        "total_chunks": total_chunks,
        "audio_coverage_s": round(audio_s, 2),
        "silence_s": round(dur - audio_s, 2),
        "first_sid": sess_in[0]["sid"],
        "last_sid": sess_in[-1]["sid"],
        "max_gap_s": round(max_gap, 2),
        "sample_rate": SR,
        "channels": CH,
        "sample_width_bits": SW * 8,
        "wav_path": fpath,
        "wav_size_bytes": os.path.getsize(fpath),
    }
    results.append(meta)
    print(f"\n=== Window {wi}: {s2h(s0)} - {s2h(s1)} ===")
    print(f"  File: {fpath}")
    print(f"  Sessions: {placed}/{len(sess_in)}   Chunks: {total_chunks}")
    print(f"  Audio: {audio_s:.1f}s / {dur}s   Silence: {dur-audio_s:.1f}s   Max gap: {max_gap:.1f}s")
    print(f"  Missing: {len(missing)}")

print("\n\n===== SUMMARY =====")
for m in results:
    print(f"  {m['window_start']} - {m['window_end']}  "
          f"sess={m['sessions']}  chunks={m['total_chunks']}  "
          f"audio={m['audio_coverage_s']}s  file={os.path.basename(m['wav_path'])}")

with open(os.path.join(OUTDIR, "meta_sg_0903_multi.json"), "w") as f:
    json.dump(results, f, ensure_ascii=False, indent=2)
print("\nMetadata saved to deliverables/meta_sg_0903_multi.json")
