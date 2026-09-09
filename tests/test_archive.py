import gzip
from datetime import datetime, timezone
from io import BytesIO

import boto3
import httpx
import pytest
from botocore.exceptions import ClientError, IncompleteReadError
from botocore.response import StreamingBody
from botocore.stub import Stubber

from core import archive


@pytest.fixture
def s3(monkeypatch):
    # Reproduce the reported REST rejection without calling Cloudflare.
    legacy = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(403)))
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: legacy)
    client = boto3.client(
        "s3",
        endpoint_url="https://acct.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id="dummy",
        aws_secret_access_key="dummy",
    )
    # Exercise the production call signatures; only the SDK transport is replaced.
    monkeypatch.setattr(boto3, "client", lambda *args, **kwargs: client)
    yield client
    client.close()


def test_put_preserves_gzip_bytes_key_and_content_type(settings, s3):
    key = archive.PENDING_KEY
    data = gzip.compress(b'{"operations":[]}')
    with Stubber(s3) as stub:
        stub.add_response(
            "put_object",
            {"ETag": '"dummy"'},
            {"Bucket": "bucket", "Key": key, "Body": data, "ContentType": "application/gzip"},
        )
        assert archive.put(settings, key, data) is None
        stub.assert_no_pending_responses()


def test_get_returns_exact_bytes_and_closes_body(settings, s3):
    data = gzip.compress(b'{"operations":[{"kind":"hub"}]}')
    stream = BytesIO(data)
    with Stubber(s3) as stub:
        stub.add_response(
            "get_object",
            {"Body": StreamingBody(stream, len(data)), "ContentType": "application/gzip"},
            {"Bucket": "bucket", "Key": "music-sync/pending-reconcile.json.gz"},
        )
        assert archive.get(settings, archive.PENDING_KEY) == data
        assert stream.closed
        stub.assert_no_pending_responses()


@pytest.mark.parametrize(
    "code,status",
    [
        ("NoSuchKey", 404),
        ("NoSuchBucket", 404),
        ("404", 404),
        ("AccessDenied", 403),
        ("InternalError", 500),
    ],
)
@pytest.mark.parametrize("operation", ["get", "put"])
def test_only_get_no_such_key_is_absence(settings, s3, code, status, operation):
    with Stubber(s3) as stub:
        stub.add_client_error(f"{operation}_object", code, http_status_code=status)
        if operation == "get" and code == "NoSuchKey":
            assert archive.get(settings, archive.PENDING_KEY) is None
        else:
            with pytest.raises(ClientError) as exc:
                if operation == "get":
                    archive.get(settings, archive.PENDING_KEY)
                else:
                    archive.put(settings, archive.PENDING_KEY, b"dummy")
            assert exc.value.response["Error"]["Code"] == code
        stub.assert_no_pending_responses()


def test_truncated_get_fails_closed_and_closes_body(settings, s3):
    stream = BytesIO(b"short")
    with Stubber(s3) as stub:
        stub.add_response("get_object", {"Body": StreamingBody(stream, 100)})
        with pytest.raises(IncompleteReadError):
            archive.get(settings, archive.PENDING_KEY)
        assert stream.closed
        stub.assert_no_pending_responses()


def test_client_uses_r2_s3_credentials_derived_in_memory(settings, monkeypatch):
    from hashlib import sha256

    # A presigned URL exercises endpoint, region and signing without any request.
    monkeypatch.setattr(boto3, "DEFAULT_SESSION", None)
    client = archive._client(settings)
    try:
        assert client.meta.endpoint_url == "https://acct.r2.cloudflarestorage.com"
        assert client.meta.region_name == "auto"
        credentials = client._request_signer._credentials
        assert credentials.access_key == "r2-key-id"
        assert credentials.secret_key == sha256(b"r2tok").hexdigest()
        url = client.generate_presigned_url(
            "get_object", Params={"Bucket": "bucket", "Key": archive.PENDING_KEY}
        )
        assert "X-Amz-Algorithm=AWS4-HMAC-SHA256" in url
        assert "X-Amz-Credential=r2-key-id%2F" in url
    finally:
        client.close()


def test_key_for():
    now = datetime(2026, 9, 8, 13, 5, 9, tzinfo=timezone.utc)
    key = archive.key_for(now)
    assert key.startswith("raw/spotify-pull/2026-09-08T130509Z-")
    assert key.endswith(".json.gz")
    assert key != archive.key_for(now)


def test_raw_archives_use_life_api_without_r2_credentials(settings, monkeypatch):
    calls = []
    data = gzip.compress(b'{"raw": true}')

    def handler(request):
        calls.append(request)
        assert request.url == "https://hub/v1/files/raw/spotify-pull/a.json.gz"
        assert request.headers["Authorization"] == "Bearer hub-token"
        if request.method == "PUT":
            assert request.content == data
            assert request.headers["Content-Type"] == "application/gzip"
            return httpx.Response(201, json={"key": "raw/spotify-pull/a.json.gz"})
        return httpx.Response(200, content=data)

    settings.life_hub_url = "https://hub"
    settings.life_hub_token = "hub-token"
    client = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setattr(archive, "_client", lambda *_: pytest.fail("raw archive used R2"))
    monkeypatch.setattr(httpx, "request", client.request)
    archive.put(settings, "raw/spotify-pull/a.json.gz", data)
    assert archive.get(settings, "raw/spotify-pull/a.json.gz") == data
    assert [r.method for r in calls] == ["PUT", "GET"]


@pytest.mark.parametrize("status", [403, 500])
def test_raw_upload_failure_stops_flow(settings, monkeypatch, status):
    client = httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(status)))
    monkeypatch.setattr(httpx, "request", client.request)
    with pytest.raises(httpx.HTTPStatusError):
        archive.put(settings, "raw/spotify-pull/a.json.gz", b"x")
