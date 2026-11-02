"""In-memory S3 transport for the one Spotify cooldown object."""

from hashlib import sha256
from io import BytesIO
from threading import Lock

from botocore.exceptions import ClientError


class GateStore:
    def __init__(self, objects=None):
        self.objects = objects if objects is not None else {}
        self.lock = Lock()
        self.after_read = lambda: None
        self.before_write = lambda body: None
        self.after_write = lambda body: None
        self.conflicts = 0

    def get_object(self, *, Bucket, Key):
        with self.lock:
            data = self.objects.get(Key)
        self.after_read()
        if data is None:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        return {"Body": BytesIO(data), "ETag": '"' + sha256(data).hexdigest() + '"'}

    def put_object(self, *, Bucket, Key, Body, ContentType, **conditions):
        self.before_write(Body)
        with self.lock:
            previous = self.objects.get(Key)
            etag = '"' + sha256(previous).hexdigest() + '"' if previous else None
            if (conditions.get("IfNoneMatch") == "*" and previous is not None) or (
                "IfMatch" in conditions and conditions["IfMatch"] != etag
            ):
                self.conflicts += 1
                raise ClientError(
                    {
                        "Error": {"Code": "PreconditionFailed"},
                        "ResponseMetadata": {"HTTPStatusCode": 412},
                    },
                    "PutObject",
                )
            self.objects[Key] = Body
        self.after_write(Body)
        return {"ETag": '"' + sha256(Body).hexdigest() + '"'}
