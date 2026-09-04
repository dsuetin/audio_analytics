import json
import os
import zipfile
import boto3

REPORT = "reports/final_transcript_report_2026-08-27_debug.json"
OUT_DIR = "deliverables"

BUCKET = "audio-sessions"
DATE = "20260827"
T_START = "085000"  # inclusive
T_END = "090000"    # inclusive

TARGETS = {
    "Пятигорск_Первомайская": "г_Пятигорск_ул_Первомайская_д_34",
    "Пятигорск_Бештаугорское_шоссе": "г_Пятигорск_ш_Бештаугорское_д_4",
    "Пятигорск_Калинина": "г_Пятигорск_ул_Калинина_д_299",
}


def parse_session(s):
    p = s.split("-")
    if len(p) < 3:
        return None
    return p[0], p[1], p[2]


def main():
    data = json.load(open(REPORT))
    sids = set()
    for b in data["blocks"]:
        for s in b.get("session_ids", []):
            sids.add(s)

    client = boto3.client(
        "s3",
        endpoint_url="http://localhost:9000",
        aws_access_key_id="minioadmin",
        aws_secret_access_key="minioadmin",
        region_name="us-east-1",
    )

    selected = {name: [] for name in TARGETS}
    for s in sids:
        dt, t, sto = parse_session(s)
        if dt != DATE or not (T_START <= t <= T_END):
            continue
        for name, store in TARGETS.items():
            if sto == store:
                selected[name].append(s)
    for name in selected:
        selected[name].sort()

    os.makedirs(OUT_DIR, exist_ok=True)

    summary = []
    for name, sessions in selected.items():
        zip_path = os.path.join(OUT_DIR, f"{name}_27.08_08-50_09-00.zip")
        file_count = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for sid in sessions:
                r = client.list_objects_v2(Bucket=BUCKET, Prefix=f"audio/{sid}/")
                objs = sorted(r.get("Contents", []), key=lambda x: x["Key"])
                for o in objs:
                    key = o["Key"]
                    body = client.get_object(Bucket=BUCKET, Key=key)["Body"]
                    arcname = f"{sid}/{os.path.basename(key)}"
                    data = body.read()
                    zf.writestr(arcname, data)
                    file_count += 1
        size = os.path.getsize(zip_path)
        summary.append((name, len(sessions), file_count, size, zip_path))
        print(f"{name}: sessions={len(sessions)} files={file_count} size={size}")

    print("\nSUMMARY")
    for name, n_sess, n_files, size, zip_path in summary:
        print(f"  {name}: {n_sess} sessions, {n_files} files, {size} bytes, {zip_path}")


if __name__ == "__main__":
    main()
