from __future__ import annotations

import boto3


class S3Uploader:
    def __init__(self, endpoint_url: str, access_key_id: str, secret_access_key: str, bucket: str):
        self.bucket = bucket

        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def put_object(self, key: str, body: bytes) -> None:
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=body,
            ContentType="application/octet-stream",
        )