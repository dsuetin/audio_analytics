from __future__ import annotations

import boto3


class S3MultipartUploader:
    def __init__(self, endpoint_url: str, access_key_id: str, secret_access_key: str, bucket: str):
        self.bucket = bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=access_key_id,
            aws_secret_access_key=secret_access_key,
        )

    def create_multipart_upload(self, key: str) -> str:
        response = self.client.create_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            ContentType="application/octet-stream",
        )
        return response["UploadId"]

    def upload_part(self, key: str, upload_id: str, part_number: int, body: bytes) -> str:
        response = self.client.upload_part(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=body,
        )
        return response["ETag"]

    def complete_upload(self, key: str, upload_id: str, parts: list[dict]) -> None:
        self.client.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={"Parts": parts},
        )

    def abort_upload(self, key: str, upload_id: str) -> None:
        self.client.abort_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
        )
