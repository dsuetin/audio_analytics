#!/usr/bin/env python3
"""
Build 3 WAV files for store Пятигорск — Первомайская on 2026-09-03,
each covering a 3-minute calendar window starting at 14:00.
Per window:
  * pick S3 sessions whose embedded HH:MM:SS falls inside the window
  * download the chunks
  * place each session's audio at its real start time (silence in the gaps)
  * write one contiguous WAV of exactly 180 s
S3 is read-only.
"""
import io
import os
import re
import wave
import json
import boto3
from datetime import datetime, timedelta

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUTDIR = os.path.join(ROOT, "deliverables", "sg_per_2026-09-03")
os.makedirs(OUTDIR, exist_ok=True)

STORE = "Первомайская"
DAY = "2026-09-03"
WINDOW_SEC = 180
SR = 16000

WINDOWS = [
    (14, 0),   # 14:00:00 .. 14:03:00
    (14, 3),   # 14:03:00 .. 14:06:00
    (14, 6),   # 14:06:00 .. 14:09:00
]

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
        kw = {"Bucket": BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if tok:
            kw["ContinuationToken"] = tok
        r = client.list_objects_v2(**kw)
        out += r.get("Contents", [])
        if r.get("IsTruncated"):
            tok = r["NextContinuationToken"]
        else:
            break
    return out


def get_sessions(start_h, start_m):
    """All UNIQUE sessions of the store on DAY that start inside the 3-min window."""
    start = datetime(2026, 9, 3, start_h, start_m)
    end = start + timedelta(seconds=WINDOW_SEC)
    day_prefix = "audio/20260903-"
    seen = {}
    for o in list_obj(day_prefix):
        if STORE not in o["Key"]:
            continue
        parts = o["Key"].split("/")
        if len(parts) < 3:
            continue
        m = re.match(r"(\d{8})-(\d{6})-", parts[1])
        if not m:
            continue
        h, mi, s = (
            int(m.group(2)[:2]), int(m.group(2)[2:4]), int(m.group(2)[4:6]),
        )
        astart = start.replace(hour=h, minute=mi, second=s)
        if start <= astart < end:
            if parts[1] not in seen:
                seen[parts[1]] = {"sid": parts[1], "astart": astart}
    out = list(seen.values())
    out.sort(key=lambda x: x["astart"])
    return out, start


def build(start_h, start_m):
    recs, start = get_sessions(start_h, start_m)
    if not recs:
        return None
    buf = bytearray(WINDOW_SEC * SR * 2)
    chunks_total = 0
    for rec in recs:
        objs = list_obj(f"audio/{rec['sid']}/")
        objs.sort(key=lambda o: o["Key"])
        pcm = bytearray()
        for o in objs:
            body = client.get_object(Bucket=BUCKET, Key=o["Key"])["Body"].read()
            with wave.open(io.BytesIO(body), "rb") as wf:
                assert wf.getframerate() == SR and wf.getnchannels() == 1 and wf.getsampwidth() == 2
                pcm += wf.readframes(wf.getnframes())
        off = int((rec["astart"] - start).total_seconds()) * SR
        if off + len(pcm) // 2 > SR * WINDOW_SEC:
            pcm = pcm[:(SR * WINDOW_SEC - off) * 2]
        buf[off * 2: off * 2 + len(pcm)] = pcm
        rec["audio_frames"] = len(pcm) // 2
        chunks_total += len(objs)

    end_dt = start + timedelta(seconds=WINDOW_SEC)
    fname = (
        "Пятигорск_Первомайская_"
        f"{DAY}_{start.strftime('%H-%M-%S')}_{end_dt.strftime('%H-%M-%S')}.wav"
    )
    out_path = os.path.join(OUTDIR, fname)
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SR)
        wf.writeframes(bytes(buf))
    return {
        "window": [start.strftime("%H:%M:%S"), end_dt.strftime("%H:%M:%S")],
        "sessions": len(recs),
        "chunks": chunks_total,
        "audio_seconds": sum(r.get("audio_frames", 0) for r in recs) / SR,
        "wav": out_path,
        "size_bytes": os.path.getsize(out_path),
        "first_session_id": recs[0]["sid"],
        "last_session_id": recs[-1]["sid"],
        "sessions_list": [r["sid"] for r in recs],
    }


if __name__ == "__main__":
    all_meta = []
    for (h, m) in WINDOWS:
        meta = build(h, m)
        if meta is None:
            print(f"no sessions in window {h:02d}:{m:02d}")
            continue
        print(
            f"written {meta['wav']}  sessions={meta['sessions']}  "
            f"chunks={meta['chunks']}  audio={meta['audio_seconds']:.1f}s"
        )
        all_meta.append(meta)
    with open(os.path.join(OUTDIR, "meta.json"), "w") as f:
        json.dump(all_meta, f, ensure_ascii=False, indent=2)
