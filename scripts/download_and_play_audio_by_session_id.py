from __future__ import annotations

import io
import tempfile
import wave

import boto3
import simpleaudio as sa


S3_ENDPOINT_URL = "http://localhost:9000"
S3_ACCESS_KEY = "minioadmin"
S3_SECRET_KEY = "minioadmin"
S3_BUCKET = "audio-sessions"


def download_and_play(session_id: str) -> None:
    client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
    )

    prefix = f"audio/{session_id}/"

    response = client.list_objects_v2(
        Bucket=S3_BUCKET,
        Prefix=prefix,
    )

    objects = sorted(
        response.get("Contents", []),
        key=lambda x: x["Key"],
    )

    if not objects:
        print(f"session not found: {session_id}")
        return

    pcm_parts = []

    channels = None
    sample_width = None
    sample_rate = None

    for obj in objects:
        key = obj["Key"]

        body = client.get_object(
            Bucket=S3_BUCKET,
            Key=key,
        )["Body"].read()

        with wave.open(io.BytesIO(body), "rb") as wf:
            if channels is None:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()

            pcm_parts.append(wf.readframes(wf.getnframes()))

        print(f"downloaded {key}")

    merged_pcm = b"".join(pcm_parts)

    with tempfile.NamedTemporaryFile(
        suffix=".wav",
        delete=False,
    ) as tmp:

        with wave.open(tmp, "wb") as wf:
            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(merged_pcm)

        wav_path = tmp.name

    print(f"playing: {wav_path}")

    wave_obj = sa.WaveObject.from_wave_file(wav_path)
    play_obj = wave_obj.play()
    play_obj.wait_done()


if __name__ == "__main__":
    session_id = input("session id: ").strip()
    download_and_play(session_id)