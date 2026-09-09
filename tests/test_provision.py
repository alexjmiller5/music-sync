import httpx
import pytest

from scripts import provision


def test_r2_access_key_field_follows_token_mint(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["provision.py", "--list"])
    provision.main()
    fields = capsys.readouterr().out.splitlines()
    assert fields.index("R2_ACCESS_KEY_ID") > fields.index("R2_API_TOKEN")


@pytest.mark.parametrize("matches", [[], ["existing-id"], ["id-one", "id-two"]])
def test_r2_access_key_lookup_is_read_only_paginated_and_unambiguous(matches, capsys, monkeypatch):
    pages = []

    def handler(req):
        assert req.method == "GET" and req.url.path == "/user/tokens"
        page = int(req.url.params["page"])
        pages.append(page)
        rows = (
            [{"id": "unrelated", "name": "other-token"}]
            if page == 1
            else [{"id": value, "name": provision.NAME} for value in matches]
        )
        return httpx.Response(200, json={"result": rows, "result_info": {"total_pages": 2}})

    client = httpx.Client(base_url="https://cf.test", transport=httpx.MockTransport(handler))
    monkeypatch.setattr(provision, "op_read", lambda ref: "dummy")
    monkeypatch.setattr(provision.httpx, "Client", lambda **kwargs: client)
    monkeypatch.setattr("sys.argv", ["provision.py", "--field", "R2_ACCESS_KEY_ID"])
    if len(matches) == 1:
        provision.main()
        assert capsys.readouterr().out == "existing-id\n"
    else:
        with pytest.raises(RuntimeError, match="exactly one"):
            provision.main()
        assert capsys.readouterr().out == ""
    assert pages == [1, 2]
