import pytest

from core.config import Settings


@pytest.fixture
def settings():
    return Settings(
        spotify_client_id="cid",
        spotify_client_secret="csec",
        spotify_refresh_token="rtok",
        life_hub_url="https://hub.test",
        life_hub_token="hubtok",
        notion_token="ntok",
        notion_tasks_data_source_id="ds",
        notion_project_page_id="proj",
        r2_account_id="acct",
        r2_bucket="bucket",
        r2_api_token="r2tok",
        r2_access_key_id="r2-key-id",
    )


@pytest.fixture(autouse=True)
def no_external_sockets(monkeypatch):
    """Real services are never test dependencies; OAuth callback loopback is allowed."""
    import ipaddress
    import socket

    original = socket.socket.connect

    def connect(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            host = address[0]
            assert host == "localhost" or ipaddress.ip_address(host).is_loopback, (
                "external socket blocked"
            )
        return original(sock, address)

    monkeypatch.setattr(socket.socket, "connect", connect)
