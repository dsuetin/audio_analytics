import boto3
from botocore.exceptions import ClientError


class S3Client:
    def __init__(self, endpoint, key, secret, bucket):
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint,
            aws_access_key_id=key,
            aws_secret_access_key=secret,
        )
        self.bucket = bucket

    def get_object(self, key: str) -> bytes:
        try:
            obj = self.s3.get_object(Bucket=self.bucket, Key=key)
            return obj["Body"].read()
        except ClientError as e:
            error_code = e.response.get("Error", {}).get("Code", "Unknown")
            if error_code == "NoSuchKey":
                raise FileNotFoundError(f"Object not found: {key}") from e
            elif error_code == "NoSuchBucket":
                raise FileNotFoundError(f"Bucket not found: {self.bucket}") from e
            else:
                raise