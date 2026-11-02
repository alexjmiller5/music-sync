import gzip
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Event, Lock

import pytest

from core import archive, capture_clients
from tests.gate_store import GateStore
from tests.test_capture_clients import NOW, spotify_429


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("first_wait", [600, 60])
def test_two_writers_cannot_shorten_the_spotify_not_before(
    settings, monkeypatch, existing, first_wait
):
    gate = capture_clients.SPOTIFY_GATE_KEY
    store = GateStore()
    if existing:
        store.objects[gate] = gzip.compress(
            json.dumps({"retry_at": capture_clients._iso(NOW)}).encode()
        )
    monkeypatch.setattr(archive, "_shared", lambda s: store)
    both_read = Barrier(2)
    first_written = Event()
    read_lock = Lock()
    reads = 0
    long_until = capture_clients._iso(NOW + timedelta(seconds=600))
    first_until = capture_clients._iso(NOW + timedelta(seconds=first_wait))

    def after_read():
        nonlocal reads
        with read_lock:
            reads += 1
            initial = reads <= 2
        if initial:
            both_read.wait(timeout=5)

    def before_write(body):
        if json.loads(gzip.decompress(body))["retry_at"] != first_until:
            assert first_written.wait(timeout=5)

    def after_write(body):
        if json.loads(gzip.decompress(body))["retry_at"] == first_until:
            first_written.set()

    store.after_read, store.before_write, store.after_write = after_read, before_write, after_write
    with ThreadPoolExecutor(max_workers=2) as pool:
        longer = pool.submit(capture_clients.defer_spotify, settings, spotify_429("600"), NOW)
        shorter = pool.submit(capture_clients.defer_spotify, settings, spotify_429("60"), NOW)
        assert longer.result(timeout=10) == long_until
        shorter_result = shorter.result(timeout=10)
    assert json.loads(gzip.decompress(store.objects[gate]))["retry_at"] == long_until
    assert shorter_result == (long_until if first_wait == 600 else first_until)
    assert store.conflicts == 1


@pytest.mark.parametrize(
    "host,spotify",
    [
        ("api.spotify.com", True),
        ("accounts.spotify.com", True),
        ("spotify.com", True),
        ("notspotify.com", False),
        ("api.spotify.com.other.test", False),
        ("hub.test", False),
    ],
)
def test_cooldown_is_scoped_to_the_provider_host(settings, monkeypatch, host, spotify):
    import httpx

    store = GateStore()
    monkeypatch.setattr(archive, "_shared", lambda s: store)
    response = httpx.Response(
        429, headers={"Retry-After": "60"}, request=httpx.Request("GET", f"https://{host}/v1/test")
    )
    exc = httpx.HTTPStatusError("limited", request=response.request, response=response)
    assert bool(capture_clients.defer_spotify(settings, exc, NOW)) is spotify
    assert bool(store.objects) is spotify
