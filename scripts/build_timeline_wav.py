"""
Build a 10-minute (600 s) timeline WAV per store covering 08:50:00 -> 09:00:00
on 2026-08-27 from audio-sessions MinIO.

Each VAD speech segment ("session" in the code) is placed at its real
wall-clock offset; inter-session gaps are explicit silence (never fabricates
audio). Sessions are sourced directly from S3 (not from the report), so the
output also contains the small VAD segments that had no ASR row and dropped
out of the report.

Read-only on S3: list_objects_v2 + get_object, no writes.
Existing .zip archives are preserved.

Run: python3 scripts/build_timeline_wav.py
"""

import array
import collections
import io
import os
import re
import wave
from datetime import datetime

import boto3

BUCKET = "audio-sessions"
ENDPOINT = "http://localhost:9000"
DATE = "20260827"
WINDOW_START = datetime(2026, 8, 27, 8, 50, 0)
WIN_SECONDS = 600  # 08:50:00 -> 09:00:00
SAMPLE_RATE = 16000
CHANNELS = 1
SAMP_WIDTH = 2     # 16-bit

TARGETS = [
    # (out_name, store_token in session_id parts[2])
    ("Пятигорск_Первомайская",          "г_Пятигорск_ул_Первомайская_д_34"),
    ("Пятигорск_Бештаугорское_шоссе",   "г_Пятигорск_ш_Бештаугорское_д_4"),
    ("Пятигорск_Калинина",              "г_Пятигорск_ул_Калинина_д_299"),
]
OUT_DIR = "deliverables"


def list_all(client, prefix):
    token = None
    while True:
        kw = {"Bucket": BUCKET, "Prefix": prefix, "MaxKeys": 1000}
        if token:
            kw["ContinuationToken"] = token
        r = client.list_objects_v2(**kw)
        for o in r.get("Contents", []):
            yield o
        if r.get("IsTruncated"):
            token = r.get("NextContinuationToken")
        else:
            break


def start_dt(sid):
    t = sid.split("-")[1]
    return datetime(2026, 8, 27, int(t[0:2]), int(t[2:4]), int(t[4:6]))


def get_session_chunks(client, sid):
    """Return list of (chunk_index, pcm_bytes) for a session, in order."""
    files = []
    for o in list_all(client, f"audio/{sid}/"):
        name = o["Key"].split("/", 2)[2]
        if re.fullmatch(r"\d+\.wav", name or ""):
            files.append(name)
    files.sort(key=lambda n: int(os.path.splitext(n)[0]))
    pcm_parts = []
    for name in files:
        body = client.get_object(Bucket=BUCKET, Key=f"audio/{sid}/{name}")["Body"].read()
        with io.BytesIO(body) as bio, wave.open(bio, "rb") as wf:
            params = (wf.getnchannels(), wf.getframerate(), wf.getsampwidth())
            if params != (CHANNELS, SAMPLE_RATE, SAMP_WIDTH):
                raise ValueError(f"{sid}/{name}: unexpected params {params}")
            n = wf.getnframes()
            data = wf.readframes(n)
            if len(data) != n * CHANNELS * SAMP_WIDTH:
                raise ValueError(f"{sid}/{name}: wav data {len(data)} != {n} frames * {CHANNELS*SAMP_WIDTH}")
            pcm_parts.append(data)
    if not pcm_parts:
        raise ValueError(f"{sid}: no wav chunks found")
    return pcm_parts


def silent_runs(buf, step_ms=50, min_gap=1.0):
    """Return list of (start_sec, end_sec) of all-zero step_ms bins >= min_gap s."""
    step = (SAMPLE_RATE * step_ms // 1000) * CHANNELS * SAMP_WIDTH  # bytes per bin
    zero = b"\x00" * step
    n_bins = len(buf) // step
    runs = []
    i = 0
    while i < n_bins:
        if bytes(buf[i * step:(i + 1) * step]) == zero:
            j = i
            while j < n_bins and bytes(buf[j * step:(j + 1) * step]) == zero:
                j += 1
            if (j - i) * step_ms / 1000.0 >= min_gap:
                runs.append((i * step_ms / 1000.0, j * step_ms / 1000.0))
            i = j
        else:
            i += 1
    return runs


def build(meta_name, store_token, client):
    # 1) find all S3 sessions of this store in [08:50,09:00)
    sessions = sorted(
        {
            o["Key"].split("/", 1)[1].rstrip("/").split("/", 1)[0]
            for o in list_all(client, f"audio/{DATE}-")
            if (o["Key"].split("/", 1)[1].rstrip("/").split("/", 1)[0].split("-")[2] == store_token
                and "085000" <= o["Key"].split("-", 2)[1][:6] <= "090000")
        },
        key=lambda s: start_dt(s),
    )

    total_pcm = 0
    total_chunks = 0
    BUF_SAMPLES = WIN_SECONDS * SAMPLE_RATE
    buf = bytearray(BUF_SAMPLES * CHANNELS * SAMP_WIDTH)
    segments = []  # (start_sample, n_samples, sid)
    overlaps = []
    for sid in sessions:
        parts = get_session_chunks(client, sid)
        pcm = b"".join(parts)
        offset = (start_dt(sid) - WINDOW_START).total_seconds()
        start = int(offset * SAMPLE_RATE)
        n = len(pcm) // (CHANNELS * SAMP_WIDTH)
        # clip head before 08:50:00
        if start < 0:
            cut = -start
            pcm = pcm[cut * CHANNELS * SAMP_WIDTH:]
            n = len(pcm) // (CHANNELS * SAMP_WIDTH)
            start = 0
        # clip tail after 09:00:00 (do NOT extend the buffer)
        if start + n > BUF_SAMPLES:
            n = BUF_SAMPLES - start
            pcm = pcm[: n * CHANNELS * SAMP_WIDTH]
        if n <= 0 or not pcm:
            continue
        # overlap detection against earlier segments (all in samples)
        for (ps, pn, psid) in segments:
            if start < ps + pn and ps < start + n:
                overlaps.append((psid, sid, max(ps, start), min(ps + pn, start + n) - max(ps, start)))
        buf[start * CHANNELS * SAMP_WIDTH: (start + n) * CHANNELS * SAMP_WIDTH] = pcm
        segments.append((start, n, sid))
        total_pcm += len(pcm)
        total_chunks += len(parts)

    out_path = os.path.join(OUT_DIR, f"{meta_name}_27.08_08-50_09-00.wav")
    with wave.open(out_path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(SAMP_WIDTH)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(buf))

    runs = silent_runs(bytes(buf))
    return {
        "name": meta_name,
        "token": store_token,
        "sessions": len(segments),
        "chunks": total_chunks,
        "audio_seconds": total_pcm / (SAMPLE_RATE * CHANNELS * SAMP_WIDTH),
        "out": out_path,
        "size": os.path.getsize(out_path),
        "first_offset": (min(s[0] for s in segments) / SAMPLE_RATE if segments else None),
        "last_end": (max((s[0] + s[1]) for s in segments) / SAMPLE_RATE if segments else None),
        "silence_runs": runs,
        "overlaps": overlaps,
    }


def main():
    client = boto3.client(
        "s3",
        endpoint_url=ENDPOINT,
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        region_name="us-east-1",
    )
    all_results = []
    for meta_name, store_token in TARGETS:
        print(f"\n=== {meta_name} ({store_token}) ===")
        r = build(meta_name, store_token, client)
        all_results.append(r)
        print(f"  sessions: {r['sessions']}  chunks: {r['chunks']}  audio: {r['audio_seconds']:.1f}s")
        print(f"  overlaps: {len(r['overlaps'])}", "sample:", r["overlaps"][:2])
        print(f"  first speech at offset {r['first_offset']:.2f} s ({WINDOW_START.strftime('%H:%M:%S')} + {r['first_offset']:.1f})")
        print(f"  last  speech {r['last_end']:.2f} s (i.e. ~{WINDOW_START.strftime('%H:%M:%S')} + {r['last_end']:.1f}s)")
        sil = r["silence_runs"]
        print(f"  silence runs >=1s: {len(sil)}")
        for a, b in sil[:15]:
            print(f"    {int(a):3d}s -> {int(b):3d}s (gap {b-a:.1f}s)")
        if len(sil) > 15:
            print(f"    ... {len(sil)-15} more")
        print(f"  WAV: {r['out']}  ({r['size']:,} bytes)")

    print("\n\n===== FINAL =====")
    for r in all_results:
        print(f"{r['name']}: sessions={r['sessions']} chunks={r['chunks']} audio={r['audio_seconds']:.1f}s "
              f"silences={len(r['silence_runs'])} file={r['size']:,} B")


if __name__ == "__main__":
    main()
