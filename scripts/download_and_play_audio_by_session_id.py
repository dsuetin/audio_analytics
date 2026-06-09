from __future__ import annotations

import io
import tempfile
import wave
import argparse
import boto3
import simpleaudio as sa


S3_ENDPOINT_URL = "http://localhost:9000"
S3_ACCESS_KEY = "minioadmin"
S3_SECRET_KEY = "minioadmin"
S3_BUCKET = "audio-sessions"


def find_latest_session(client) -> str | None:
    response = client.list_objects_v2(
        Bucket=S3_BUCKET,
        Prefix="audio/",
    )

    contents = response.get("Contents", [])

    if not contents:
        return None

    latest = max(
        contents,
        key=lambda obj: obj["LastModified"],
    )

    key = latest["Key"]

    # audio/<session_id>/000001.wav
    parts = key.split("/")

    if len(parts) < 3:
        return None

    return parts[1]
def download_and_play(client: boto3.client, session_id: str) -> None:
    
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "session_id",
        nargs="?",
        help="session id (default: latest session)",
    )
    client = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
    )
    args = parser.parse_args()

    session_id = args.session_id

    if not session_id:
        session_id = find_latest_session(client)

        if not session_id:
            print("No sessions found")
            return

        print(f"Using latest session: {session_id}")

    download_and_play(client, session_id)
if __name__ == "__main__":
    main()