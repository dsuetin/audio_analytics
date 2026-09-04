import io
import os
import re
import wave
import zipfile
from datetime import datetime

DELIV = "deliverables"


def parse_start(session_id: str):
    parts = session_id.split("-")
    t = parts[1]
    return datetime(2026, 8, 27, int(t[0:2]), int(t[2:4]), int(t[4:6]))


def main():
    for fname in sorted(os.listdir(DELIV)):
        if not fname.endswith(".zip"):
            continue
        zip_path = os.path.join(DELIV, fname)
        out_path = os.path.join(DELIV, fname[:-4] + ".wav")

        zf = zipfile.ZipFile(zip_path)

        # group chunk PCM by session, keep chunk order
        sessions = {}  # session_id -> list of (chunk_idx, pcm_bytes)
        first_meta = None
        for name in zf.namelist():
            dirname, base = name.split("/", 1)
            m = re.fullmatch(r"(\d+)\.wav", base)
            if not m:
                continue
            blob = zf.read(name)
            with io.BytesIO(blob) as bio, wave.open(bio, "rb") as wf:
                meta = (wf.getnchannels(), wf.getframerate(), wf.getsampwidth())
                pcm = wf.readframes(wf.getnframes())
            if first_meta is None:
                first_meta = meta
            else:
                assert meta == first_meta, f"param mismatch in {name}: {meta} != {first_meta}"
            sessions.setdefault(dirname, []).append((int(m.group(1)), pcm))

        # within session sort by chunk index; sessions ordered by start time
        ordered = sorted(sessions.items(), key=lambda kv: parse_start(kv[0]))
        total_frames = 0
        channels, framerate, sampwidth = first_meta

        with wave.open(out_path, "wb") as outw:
            outw.setnchannels(channels)
            outw.setsampwidth(sampwidth)
            outw.setframerate(framerate)
            for session_id, chunks in ordered:
                for _, pcm in sorted(chunks, key=lambda c: c[0]):
                    outw.writeframes(pcm)
                    total_frames += len(pcm) // (channels * sampwidth)

        dur = total_frames / framerate
        size = os.path.getsize(out_path)
        print(
            f"{os.path.basename(out_path)}: "
            f"sessions={len(ordered)} channels={channels} rate={framerate}hz "
            f"width={sampwidth}B duration={dur:.1f}s ({dur/60:.2f} min) size={size}B"
        )


if __name__ == "__main__":
    main()
