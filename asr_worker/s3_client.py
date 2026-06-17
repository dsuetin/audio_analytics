import boto3


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
        obj = self.s3.get_object(Bucket=self.bucket, Key=key)
        return obj["Body"].read()