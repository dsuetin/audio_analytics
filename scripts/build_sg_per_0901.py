#!/usr/bin/env python3
"""
Assemble one contiguous 10-minute WAV for:
  store  = Пятигорск — Первомайская  (г_Пятигорск_ул_Первомайская_д_34)
  date   = 2026-09-01
  window = 14:50:00 .. 15:00:00

Pipeline (report -> timestamp/transcript -> session_id -> S3):
  * read the raw transcript report (transcript_report_2026-09-01.xlsx)
  * take every row whose created_at falls in the window
  * each row carries its own session_id (store is part of the sid)
  * anchor each session's audio on the timeline by the HH:MM:SS embedded
    in its session_id (the true audio start of that session)
  * download the S3 chunks (16 kHz mono PCM16) and place them at that offset
  * gaps between utterances become silence (NO foreign material substituted)
Read-only against S3.
"""
import io
import os
import re
import json
import wave
import boto3
import openpyxl
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
REPORT = os.path.join(ROOT, "reports", "transcript_report_2026-09-01.xlsx")
OUTDIR = os.path.join(ROOT, "deliverables", "sg_per_0901")
os.makedirs(OUTDIR, exist_ok=True)

STORE = "г_Пятигорск_ул_Первомайская_д_34"
START = datetime(2026, 9, 1, 14, 50, 0)
END = datetime(2026, 9, 1, 15, 0, 0)
TOTAL_SEC = 600
SR = 16000

BUCKET = "audio-sessions"
client = boto3.client(
    "s3",
    endpoint_url="http://localhost:9000",
    aws_access_key_id="minioadmin",
    aws_secret_access_key="minioadmin",
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


# ---- 1) read report rows in window ----
wb = openpyxl.load_workbook(REPORT, read_only=True)
rows = list(wb.active.iter_rows(min_row=2, values_only=True))
wb.close()

records = []
for r in rows:
    if r[0] is None or r[7] is None:
        continue
    ts = r[0] if isinstance(r[0], datetime) else datetime.strptime(r[0], "%Y-%m-%d %H:%M:%S.%f")
    if START <= ts <= END:
        records.append({"ts": ts, "text": (r[1] or "").strip(), "sid": r[7]})

records.sort(key=lambda x: x["ts"])
print("rows in window:", len(records))

# ---- 2) anchor each session by sid-embedded time ----
for rec in records:
    sid = rec["sid"]
    assert STORE in sid, f"foreign store in sid: {sid}"
    m = re.match(r"(\d{8})-(\d{6})", sid)
    h, mi, s = int(m.group(2)[:2]), int(m.group(2)[2:4]), int(m.group(2)[4:6])
    rec["astart"] = START.replace(hour=h, minute=mi, second=s)

records.sort(key=lambda x: x["astart"])
overlaps = sum(
    1 for i in range(len(records) - 1)
    if records[i]["astart"] >= records[i + 1]["astart"]
)
print("non-monotonic/overlap (by astart):", overlaps)

# ---- 3) download audio, place on 600 s buffer ----
buf = bytearray(TOTAL_SEC * SR * 2)  # mono, 16-bit
missing_sids = []
total_chunks = 0
for rec in records:
    objs = list_obj(f"audio/{rec['sid']}/")
    if not objs:
        missing_sids.append(rec["sid"])
        rec["audio_frames"] = 0
        continue
    objs.sort(key=lambda o: o["Key"])
    pcm = bytearray()
    for o in objs:
        body = client.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read()
        with wave.open(io.BytesIO(body), "rb") as wf:
            assert wf.getframerate() == SR and wf.getnchannels() == 1 and wf.getsampwidth() == 2, \
                f"unexpected fmt {o['Key']}"
            pcm += wf.readframes(wf.getnframes())
    total_chunks += len(objs)
    off = int((rec["astart"] - START).total_seconds()) * SR
    if off + len(pcm) // 2 > SR * TOTAL_SEC:
        pcm = pcm[:(SR * TOTAL_SEC - off) * 2]
    rec["audio_frames"] = len(pcm) // 2
    rec["aend"] = rec["astart"] + timedelta(seconds=rec["audio_frames"] / SR)
    buf[off * 2: off * 2 + len(pcm)] = pcm

placed = sum(1 for r in records if r.get("audio_frames", 0) > 0)
print("sessions placed:", placed, "missing:", len(missing_sids), "chunks:", total_chunks)

# silence gaps between consecutive audio segments
seg = sorted([(r["astart"], r["aend"]) for r in records if r.get("audio_frames", 0) > 0])
gap_sum = 0.0
big_gaps = []
for i in range(len(seg) - 1):
    d = (seg[i + 1][0] - seg[i][1]).total_seconds()
    if d > 0:
        gap_sum += d
        if d > 1.0:
            big_gaps.append({"after": seg[i][1].strftime("%H:%M:%S.%f"),
                             "before": seg[i + 1][0].strftime("%H:%M:%S.%f"),
                             "seconds": round(d, 3)})
print("total silence between utterances: %.1f s" % gap_sum)
print("gaps > 1.0 s:", len(big_gaps))

# ---- 4) write single WAV ----
out_path = os.path.join(OUTDIR, "Пятигорск_Первомайская_01.09_14-50-00_15-00-00.wav")
with wave.open(out_path, "wb") as wf:
    wf.setnchannels(1)
    wf.setsampwidth(2)
    wf.setframerate(SR)
    wf.writeframes(bytes(buf))
print("WAV written:", out_path, os.path.getsize(out_path), "bytes")

# ---- 5) save records + metadata ----
rec_file = os.path.join(OUTDIR, "records_1450_1500.json")
recs = [
    {
        "timestamp": r["ts"].strftime("%Y-%m-%d %H:%M:%S.%f"),
        "audio_start": r["astart"].strftime("%H:%M:%S"),
        "audio_end": r.get("aend").strftime("%H:%M:%S.%f") if r.get("aend") else None,
        "audio_seconds": round(r.get("audio_frames", 0) / SR, 3),
        "session_id": r["sid"],
        "transcript": r["text"],
    }
    for r in records
]
with open(rec_file, "w") as f:
    json.dump(recs, f, ensure_ascii=False, indent=2)
with open(os.path.join(OUTDIR, "meta.json"), "w") as f:
    json.dump(
        {
            "store": STORE,
            "date": "2026-09-01",
            "window": [START.strftime("%H:%M:%S"), END.strftime("%H:%M:%S")],
            "rows": len(records),
            "sessions_placed": placed,
            "missing_sids": missing_sids,
            "total_chunks": total_chunks,
            "silence_between_utterances_s": round(gap_sum, 3),
            "big_gaps_gt_1s": big_gaps,
            "sample_rate": SR,
            "wav": out_path,
        },
        f,
        ensure_ascii=False,
        indent=2,
    )
print("records saved:", rec_file)
