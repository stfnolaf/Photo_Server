import hashlib
import json
from contextlib import closing

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

from photo_server.config import LibraryError, Settings

CHUNK = 1024 * 1024


def canonical_json(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


class Storage:
    def __init__(self, settings: Settings):
        self.bucket = settings.s3_bucket
        credentials = {}
        if not settings.s3_anonymous:
            for name in ("aws_access_key_id", "aws_secret_access_key", "aws_session_token"):
                value = getattr(settings, name)
                if value and value.get_secret_value():
                    credentials[name] = value.get_secret_value()
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name="us-east-1",
            **credentials,
            config=Config(
                signature_version=UNSIGNED if settings.s3_anonymous else "s3v4",
                s3={"addressing_style": "path"},
                connect_timeout=5,
                read_timeout=60,
                retries={"max_attempts": 3, "mode": "standard"},
                request_checksum_calculation="when_required",
                response_checksum_validation="when_required",
            ),
        )

    def ensure_bucket(self):
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
                raise
            self.client.create_bucket(Bucket=self.bucket)

    def head(self, key: str) -> dict | None:
        try:
            return self.client.head_object(Bucket=self.bucket, Key=key)
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] == 404:
                return None
            raise

    def put(self, key: str, body, mime: str, metadata: dict | None = None) -> bool:
        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=body,
                ContentType=mime,
                Metadata=metadata or {},
                IfNoneMatch="*",
            )
            return True
        except ClientError as error:
            if error.response["ResponseMetadata"]["HTTPStatusCode"] == 412:
                return False
            raise

    def put_json(self, key: str, value: dict):
        data = canonical_json(value)
        if not self.put(key, data, "application/json") and self.get_json(key) != value:
            raise LibraryError(f"Immutable object conflicts with this operation: {key}")

    def get_json(self, key: str) -> dict:
        with closing(self.client.get_object(Bucket=self.bucket, Key=key)["Body"]) as body:
            data = body.read(8 * CHUNK + 1)
        if len(data) > 8 * CHUNK:
            raise LibraryError(f"Metadata object exceeds 8 MB: {key}")
        return json.loads(data)

    def keys(self, prefix: str):
        for page in self.client.get_paginator("list_objects_v2").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            for entry in page.get("Contents", []):
                yield entry["Key"]

    def delete(self, key: str):
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def abort_multipart_uploads(self, prefix: str) -> int:
        """Abort incomplete multipart uploads below a staging prefix."""
        aborted = 0
        request = {"Bucket": self.bucket, "Prefix": prefix}
        while True:
            response = self.client.list_multipart_uploads(**request)
            for upload in response.get("Uploads", []):
                self.client.abort_multipart_upload(
                    Bucket=self.bucket,
                    Key=upload["Key"],
                    UploadId=upload["UploadId"],
                )
                aborted += 1
            if not response.get("IsTruncated"):
                return aborted
            request["KeyMarker"] = response["NextKeyMarker"]
            request["UploadIdMarker"] = response["NextUploadIdMarker"]

    def chunks(self, key: str):
        with closing(self.client.get_object(Bucket=self.bucket, Key=key)["Body"]) as body:
            while chunk := body.read(CHUNK):
                yield chunk

    def verify(self, key: str, size: int, sha256: str, full: bool = True):
        head = self.head(key)
        if head is None or head["ContentLength"] != size:
            raise LibraryError(f"Missing object or incorrect size: {key}")
        if full:
            digest, count = hashlib.sha256(), 0
            for chunk in self.chunks(key):
                count += len(chunk)
                digest.update(chunk)
            if count != size or digest.hexdigest() != sha256:
                raise LibraryError(f"Checksum verification failed: {key}")
