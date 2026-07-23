import argparse
import io
import tempfile
import wave

import boto3
import pandas as pd
import simpleaudio as sa


S3_ENDPOINT_URL = "http://localhost:9000"
S3_ACCESS_KEY = "minioadmin"
S3_SECRET_KEY = "minioadmin"
S3_BUCKET = "audio-sessions"


def list_all_objects(client, prefix):
    token = None

    while True:

        kwargs = {
            "Bucket": S3_BUCKET,
            "Prefix": prefix,
        }

        if token:
            kwargs["ContinuationToken"] = token

        response = client.list_objects_v2(**kwargs)

        for obj in response.get("Contents", []):
            yield obj

        if not response.get("IsTruncated"):
            break

        token = response["NextContinuationToken"]


def load_session_ids(report_file: str, store_id: str, client_id: str):

    df = pd.read_excel(report_file)

    row = df[
        (df["store_id"].astype(str) == str(store_id))
        &
        (df["client_id"].astype(str) == str(client_id))
    ]

    if row.empty:
        raise RuntimeError(
            f"client not found: store={store_id}, client={client_id}"
        )

    session_ids = (
        row.iloc[0]["session_ids"]
        .split(",")
    )

    return [
        s.strip()
        for s in session_ids
        if s.strip()
    ]


def download_session_pcm(client, session_id):

    prefix = f"audio/{session_id}/"

    objects = sorted(
        list(list_all_objects(client, prefix)),
        key=lambda x: x["Key"],
    )

    if not objects:
        print(f"Session {session_id} not found")
        return b"", None, None, None

    pcm = []

    channels = None
    sample_width = None
    sample_rate = None

    for obj in objects:

        body = client.get_object(
            Bucket=S3_BUCKET,
            Key=obj["Key"],
        )["Body"].read()

        with wave.open(io.BytesIO(body), "rb") as wf:

            if channels is None:
                channels = wf.getnchannels()
                sample_width = wf.getsampwidth()
                sample_rate = wf.getframerate()

            pcm.append(
                wf.readframes(
                    wf.getnframes()
                )
            )

    return (
        b"".join(pcm),
        channels,
        sample_width,
        sample_rate,
    )


def play_pcm(pcm, channels, sample_width, sample_rate):

    with tempfile.NamedTemporaryFile(
        suffix=".wav",
        delete=False,
    ) as tmp:

        with wave.open(tmp, "wb") as wf:

            wf.setnchannels(channels)
            wf.setsampwidth(sample_width)
            wf.setframerate(sample_rate)
            wf.writeframes(pcm)

        path = tmp.name

    print(path)

    wave_obj = sa.WaveObject.from_wave_file(path)
    play = wave_obj.play()
    play.wait_done()


def main():

    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--report",
        required=True,
        help="path to final report xlsx",
    )

    parser.add_argument(
        "--store",
        required=True,
    )

    parser.add_argument(
        "--client",
        required=True,
    )
    args = parser.parse_args()
    session_ids = load_session_ids(
        args.report,
        args.store,
        args.client,
    )

    print("Found sessions:")

    for s in session_ids:
        print(" ", s)

    s3 = boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT_URL,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
    )

    merged_pcm = b""

    channels = None
    sample_width = None
    sample_rate = None

    for session_id in session_ids:

        pcm, ch, sw, sr = download_session_pcm(
            s3,
            session_id,
        )

        if not pcm:
            continue

        merged_pcm += pcm

        if channels is None:
            channels = ch
            sample_width = sw
            sample_rate = sr

    if not merged_pcm:
        print("Nothing downloaded")
        return

    play_pcm(
        merged_pcm,
        channels,
        sample_width,
        sample_rate,
    )


if __name__ == "__main__":
    main()