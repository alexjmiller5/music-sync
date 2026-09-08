# Music Sync Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Notion prototype with a life-data catalog of recordings keyed by ISRC, hub derivations for metadata, and one Modal app that reconciles Spotify playlists hourly (smart playlists from rules, liked = pool, un-heart removes everywhere) plus a `/capture` endpoint for the Shazam shortcut.

**Architecture:** Three repos change. `derivations` (Modal) gains four `http:` endpoints the life-data hub calls by ISRC. life-data gets three cataloged tables (`songs`, `playlists`, `playlist_songs`) created through the installed `life` CLI (state, never repo code). `music-sync` (Modal) is rewritten around a pure `reconcile.plan(mirror, live)` diff whose actions are applied to Spotify and pushed to the hub; the same rule→SQL renderer drives smart playlists, descriptions, and the `life check` invariant. The Shazam shortcut becomes a thin client of `/capture`.

**Tech Stack:** Python 3.13, uv, httpx, pydantic-settings, structlog, pytest + pytest-mock, Modal 1.5 (`modal.Cron`, `modal.fastapi_endpoint(requires_proxy_auth=True)`, `max_containers=1`), sqlite3 (stdlib, in-memory rule evaluation), life-data hub HTTP protocol, Cloudflare R2 REST API, Notion API, Cherri for the shortcut.

**Spec:** `docs/superpowers/specs/2026-09-08-music-sync-redesign-design.md` (this repo). Read it first; every task cites its sections.

## Global Constraints

- Business logic lives in `src/core/` as plain Python with **no Modal imports**; only `app.py` imports `modal` (repo AGENTS.md).
- `Settings()` is instantiated inside functions, never at import time.
- Spotify: only endpoints available to Development Mode apps created after 2026-02-11 (spec §7.1). Concretely: `GET /v1/me`, `/v1/me/playlists`, `/v1/playlists/{id}`, `/v1/playlists/{id}/items`, `/v1/me/tracks`, `/v1/tracks/{id}`, `/v1/search?type=track` with `limit<=10`, `POST /v1/playlists/{id}/items` body `{"uris": [...]}`, `DELETE /v1/playlists/{id}/items` body `{"uris": [...]}`, `PUT /v1/playlists/{id}` body `{"description": ...}`, `PUT /v1/me/library?uris=<comma list>` (query param, NOT body; verified 2026-09-08), `GET /v1/me/library/contains?uris=`. **Never** `GET /v1/tracks?ids=`, `/v1/artists?ids=`, `/v1/playlists/{id}/tracks`, `PUT /v1/me/tracks`, audio-features, recommendations, top, recently-played (all 403/404 on this app).
- Spotify refresh token scopes required: `playlist-read-private playlist-read-collaborative playlist-modify-private playlist-modify-public user-library-read user-library-modify user-follow-read`. The token stored 2026-09-07 lacks `user-library-modify` (verified: `PUT /me/library` → 403) and is re-minted in Task 6.
- `songs.id` = ISRC, pattern `^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$`; recordings without one are skipped and counted (spec §3).
- life-data writes only from the Modal app (`songs`, `playlist_songs`, all `playlists` columns except `kind`, `rule`, `pinned`, `expires_at`, which an agent writes) (spec §2.1, req 12). Client pushes go through `POST /v1/rows/push` `{"table", "columns", "rows"}` → `{"upserted", "rejected", "hub_at"}`; pulls `POST /v1/rows/pull` `{"table", "columns", "since": ""}` → `{"rows"}`; `POST /v1/derive` `{"table", "ids"}` max 50 ids. Every hub request carries a real `User-Agent` (Cloudflare 403s the default one).
- Derived columns are never written by the app; the hub fills them from `http:` endpoints named in its `DERIVATIONS` secret (spec §7.2).
- Provenance edges follow the estate contract (life-map schema.md `provenance`): id `<from_kind>:<from_ref>:<to_ref>`, `rel`, `to_kind='songs'`, `to_ref=<isrc>`, `asserted_by='music-sync'`, `detail` JSON about the pair only.
- MusicBrainz: `User-Agent: music-sync-derivations/0.1 (https://github.com/alexjmiller5/derivations)`, at most 1 request per second (spec req 14).
- Modal Starter plan: 5 cron slots total; 3 in use on 2026-09-08 (notion-automations, birthday-reminders, media-center). This app uses 1.
- Secrets: `.env.tpl` op:// refs by name; `Music Sync ENV` item in the `Music Sync` vault (id `4cpe3gxzolbxtu4tnsyzzvhype`, item `65zbc6qstoi6m64hjtxb5uuhu4`). Deploy = push to main; CI runs `deploy.yml`. **Do not push to main before Task 13 is green** (the workflow deploys whatever is on main).
- No em dashes anywhere. Plain `-`.
- Commits: `git add -A` (sweep everything), no attribution trailers, but end each message with `Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns`.

## File structure (end state)

**derivations repo** (`~/Desktop/coding/active-projects/derivations`)
- `src/core/spotify.py` - ISRC → Spotify search (client credentials), `to_row`
- `src/core/deezer.py` - ISRC → Deezer track + album genres
- `src/core/musicbrainz.py` - ISRC → recording tags + first-release year, paced
- `src/core/years.py` - `first_year` = min of inputs
- `app.py` - four new endpoints alongside `/movie`, `/tv`
- `tests/test_spotify.py`, `tests/test_deezer.py`, `tests/test_musicbrainz.py`, `tests/test_years.py`, `tests/test_app.py` (extended), `tests/fixtures/*.json`
- `.env.tpl` - adds `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` (client-credentials only)

**life-data estate** (state via `life` CLI, documented in the `life-map` skill; nothing in the life-data repo)
- tables `songs`, `playlists`, `playlist_songs`; rules; `provenance.from_kind` gains `shazam`, `playlist`, `like`; hub `DERIVATIONS` gains four names; token `music-sync` (`tables:write`)

**music-sync repo** (`~/Desktop/coding/active-projects/music-sync`)
- `app.py` - Modal shim: `reconcile` (cron + endpoint), `capture` endpoint
- `src/core/config.py` - Settings (rewritten)
- `src/core/spotify_client.py` - kept, extended (items, liked, search, like, add/remove, description)
- `src/core/hub.py` - `Hub` HTTP client: `pull`, `push`, `derive`
- `src/core/archive.py` - R2 object put (CF REST)
- `src/core/model.py` - dataclasses `Song`, `Playlist`, `Membership`, `Mirror`, `LiveItem`, `LivePlaylist`, `Live`, `Action`
- `src/core/mirror.py` - `load_mirror(hub)`, `pull_live(spotify, market)`, `live_to_archive_bytes(live_raw)`
- `src/core/rules.py` - `validate_rule`, `rule_to_sql`, `render_description`, `evaluate(mirror, playlist_names)`
- `src/core/reconcile.py` - `plan(mirror, live, now) -> list[Action]` (pure)
- `src/core/actions.py` - `apply(actions, spotify, hub, dry_run) -> RunLog`
- `src/core/flags.py` - `file_flags(settings, http, flags, today)`
- `src/core/capture.py` - `capture(payload, spotify, hub, settings, now)`
- `src/core/run.py` - `reconcile(settings, dry_run) -> RunLog` (glue)
- `scripts/review.py` - §9.7 report over the mirror
- `scripts/migrate.py` - §9 steps, `--step N --dry-run`
- `scripts/backfill_derive.py` - chunked `/v1/derive`
- `scripts/provision.py` - mints `R2_API_TOKEN` (provision contract)
- `scripts/spotify_auth.py` - kept, scopes extended
- `scripts/sync_secrets.py` - kept
- `tests/` - one file per core module + `conftest.py` fixtures
- Deleted: `src/core/{export_ingest,notion_sync,playlist_builder,models,pipeline}.py`, `scripts/{ingest_export,sync_playlists_to_notion,sync_followed_to_notion,create_50s_playlist}.py` (the last one after migration step 9), `sandbox_config.json`, `data/`, `tests/{test_export_ingest,test_notion_sync,test_playlist_builder,test_pipeline}.py`, `tests/fixtures/{follow,your_library,playlists}.json`

**ios-shortcuts repo**
- `shortcuts/shazam_right_pointing_arrow_spotify.cherri` - rewritten (Task 15)
- deleted: `shortcuts/spotify_reauth.cherri`, `docs/superpowers/specs/2026-06-26-spotify-reauth-design.md`, `shortcuts/Shazam → Spotify.shortcut`

---

## Phase A - derivations service

### Task 1: Spotify ISRC derivation endpoint

**Files:**
- Create: `src/core/spotify.py`, `tests/test_spotify.py`, `tests/fixtures/spotify_search_isrc.json`
- Modify: `src/core/config.py`, `.env.tpl`, `app.py`, `tests/test_app.py`

**Interfaces:**
- Produces: `spotify.search_isrc(isrc: str, client_id: str, client_secret: str, market: str = "US", client=None) -> list[dict]` (raw track objects, may be empty), raises `spotify.Unavailable`. `spotify.to_row(isrc: str, tracks: list[dict], today: str) -> dict` with keys `title, artists, album, album_year, duration_ms, spotify_ids, spotify_playable, _source_ref`. `spotify.token(client_id, client_secret, client=None) -> str`.

- [ ] **Step 1: Write the failing tests** (`tests/test_spotify.py`)

```python
import json
from pathlib import Path

import pytest

from core import spotify

FIX = json.loads((Path(__file__).parent / "fixtures" / "spotify_search_isrc.json").read_text())


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise spotify.httpx.HTTPStatusError("x", request=None, response=None)


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def post(self, url, **kw):
        self.calls.append(("POST", url, kw))
        return self.responses.pop(0)

    def get(self, url, **kw):
        self.calls.append(("GET", url, kw))
        return self.responses.pop(0)


def test_search_isrc_uses_client_credentials_and_isrc_filter():
    c = FakeClient([FakeResp(200, {"access_token": "T"}), FakeResp(200, FIX)])
    tracks = spotify.search_isrc("GBBTV1101287", "id", "sec", client=c)
    assert [t["id"] for t in tracks] == [t["id"] for t in FIX["tracks"]["items"]]
    method, url, kw = c.calls[1]
    assert url == "https://api.spotify.com/v1/search"
    assert kw["params"] == {"q": "isrc:GBBTV1101287", "type": "track", "limit": 10, "market": "US"}
    assert kw["headers"]["Authorization"] == "Bearer T"


def test_to_row_prefers_playable_then_widest_release():
    row = spotify.to_row("GBBTV1101287", FIX["tracks"]["items"], "2026-09-08")
    assert row["title"] == "Take My Hand"
    assert row["artists"] == ["Matt Berry"]
    assert row["album"] == "Witchazel"
    assert row["album_year"] == 2011
    assert row["duration_ms"] == FIX["tracks"]["items"][0]["duration_ms"]
    assert row["spotify_ids"][0] == "6n4iuOHAOIu5LtbXBKrD0f"
    assert set(row["spotify_ids"]) == {t["id"] for t in FIX["tracks"]["items"]}
    assert row["spotify_playable"] == 1
    assert row["_source_ref"] == "spotify:isrc/GBBTV1101287@2026-09-08"


def test_to_row_empty_search_returns_only_playable_zero():
    assert spotify.to_row("XX0000000000", [], "2026-09-08") == {
        "spotify_playable": 0,
        "spotify_ids": [],
        "_source_ref": "spotify:isrc/XX0000000000@2026-09-08",
    }


def test_search_isrc_server_error_is_unavailable():
    c = FakeClient([FakeResp(200, {"access_token": "T"}), FakeResp(503, {})])
    with pytest.raises(spotify.Unavailable):
        spotify.search_isrc("GBBTV1101287", "id", "sec", client=c)
```

Fixture `tests/fixtures/spotify_search_isrc.json`: a real `GET /v1/search?q=isrc:GBBTV1101287&type=track&limit=10&market=US` response captured with the Music Sync app credentials (`op run --env-file=.env.tpl -- curl ...` from the music-sync repo), trimmed to `tracks.items[*].{id,name,uri,is_playable,duration_ms,artists[*].name,album.{name,release_date,release_date_precision,available_markets?}}` and `tracks.total`. The first item must be `6n4iuOHAOIu5LtbXBKrD0f` (album "Witchazel", `release_date` `2011-03-07`, `is_playable` true); include at least one item with `is_playable: false` so the preference test is meaningful. Keep the file under 10 KB.

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd ~/Desktop/coding/active-projects/derivations && uv run pytest tests/test_spotify.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'core.spotify'`

- [ ] **Step 3: Implement `src/core/spotify.py`**

```python
"""ISRC -> Spotify track candidates via client-credentials search.

Returns exactly the derived columns the songs table declares for
http:spotify_isrc. Development Mode apps created after 2026-02-11 may only
search with limit <= 10; no batch endpoints are used.
"""

import httpx

TOKEN_URL = "https://accounts.spotify.com/api/token"
SEARCH_URL = "https://api.spotify.com/v1/search"


class Unavailable(Exception):
    """Spotify could not be reached or answered with an error (short reason only)."""


def token(client_id: str, client_secret: str, client=None) -> str:
    try:
        r = (client or httpx).post(
            TOKEN_URL, data={"grant_type": "client_credentials"}, auth=(client_id, client_secret), timeout=10
        )
        r.raise_for_status()
        return r.json()["access_token"]
    except httpx.HTTPError as e:
        raise Unavailable(f"token: {type(e).__name__}") from e


def search_isrc(isrc: str, client_id: str, client_secret: str, market: str = "US", client=None) -> list[dict]:
    tok = token(client_id, client_secret, client)
    try:
        r = (client or httpx).get(
            SEARCH_URL,
            params={"q": f"isrc:{isrc}", "type": "track", "limit": 10, "market": market},
            headers={"Authorization": f"Bearer {tok}"},
            timeout=10,
        )
        r.raise_for_status()
    except httpx.HTTPError as e:
        raise Unavailable(f"search: {type(e).__name__}") from e
    return ((r.json().get("tracks") or {}).get("items")) or []


def _rank(t: dict) -> tuple:
    album = t.get("album") or {}
    return (
        0 if t.get("is_playable") else 1,
        0 if album.get("album_type") == "album" else 1,
        album.get("release_date") or "9999",
    )


def to_row(isrc: str, tracks: list[dict], today: str) -> dict:
    ref = f"spotify:isrc/{isrc}@{today}"
    if not tracks:
        return {"spotify_playable": 0, "spotify_ids": [], "_source_ref": ref}
    ordered = sorted(tracks, key=_rank)
    best = ordered[0]
    album = best.get("album") or {}
    rd = album.get("release_date") or ""
    return {
        "title": best.get("name"),
        "artists": [a["name"] for a in best.get("artists") or [] if a.get("name")],
        "album": album.get("name"),
        "album_year": int(rd[:4]) if rd[:4].isdigit() else None,
        "duration_ms": best.get("duration_ms"),
        "spotify_ids": [t["id"] for t in ordered],
        "spotify_playable": 1 if any(t.get("is_playable") for t in tracks) else 0,
        "_source_ref": ref,
    }
```

- [ ] **Step 4: Add settings, `.env.tpl`, and the endpoint**

`src/core/config.py`:

```python
class Settings(BaseSettings):
    tmdb_api_key: str
    spotify_client_id: str
    spotify_client_secret: str
```

`.env.tpl` (append; names by design, bootstrap parses them):

```
SPOTIFY_CLIENT_ID=op://Derivations/Derivations ENV/SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET=op://Derivations/Derivations ENV/SPOTIFY_CLIENT_SECRET
```

`app.py` (add after `tv`):

```python
def _isrc_input(body: dict):
    isrc = (body.get("inputs") or {}).get("id")
    if isrc in (None, ""):
        return None
    return str(isrc).strip().upper()


@app.function(image=image, secrets=secrets)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def spotify_isrc(body: dict):
    from datetime import date

    from fastapi.responses import JSONResponse

    from core import spotify
    from core.config import Settings

    isrc = _isrc_input(body)
    if not isrc:
        return JSONResponse({"error": "inputs.id is required"}, status_code=422)
    s = Settings()
    try:
        tracks = spotify.search_isrc(isrc, s.spotify_client_id, s.spotify_client_secret)
    except spotify.Unavailable as e:
        return JSONResponse({"error": f"spotify: {e}"}, status_code=502)
    return spotify.to_row(isrc, tracks, date.today().isoformat())
```

`tests/test_app.py` (append):

```python
def test_spotify_isrc_missing_input_is_422():
    response = app.spotify_isrc.get_raw_f()({"inputs": {}})
    assert response.status_code == 422
```

If `get_raw_f` is not available on the Modal function object in 1.5, refactor the body into a module-level `_spotify_isrc(body)` exactly like `_derive`, decorate a thin wrapper, and test `_spotify_isrc` directly.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest -v && just check`
Expected: all PASS, ruff clean.

- [ ] **Step 6: Add the two secrets to the Derivations vault and commit**

Add fields `SPOTIFY_CLIENT_ID` and `SPOTIFY_CLIENT_SECRET` to the `Derivations ENV` item with the values from `Music Sync ENV` (same developer app; the derivation only needs client credentials). Desktop auth, Alex approves:

```bash
zsh -ic 'cid=$(op-personal read "op://4cpe3gxzolbxtu4tnsyzzvhype/65zbc6qstoi6m64hjtxb5uuhu4/SPOTIFY_CLIENT_ID"); cs=$(op-personal read "op://4cpe3gxzolbxtu4tnsyzzvhype/65zbc6qstoi6m64hjtxb5uuhu4/SPOTIFY_CLIENT_SECRET"); op-personal item edit "Derivations ENV" --vault Derivations "SPOTIFY_CLIENT_ID[concealed]=$cid" "SPOTIFY_CLIENT_SECRET[concealed]=$cs"'
~/.claude/skills/1password/scripts/op-project-bootstrap --check .env.tpl
git add -A && git commit -m "Add /spotify_isrc derivation: ISRC search, preferred id ranking

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

Do not push yet (push deploys; Task 4 pushes all four endpoints together).

### Task 2: Deezer ISRC derivation endpoint

**Files:**
- Create: `src/core/deezer.py`, `tests/test_deezer.py`, `tests/fixtures/deezer_track.json`, `tests/fixtures/deezer_album.json`
- Modify: `app.py`, `tests/test_app.py`

**Interfaces:**
- Produces: `deezer.lookup(isrc: str, client=None) -> dict | None` (None when Deezer has no track for the ISRC), raises `deezer.Unavailable`. `deezer.to_row(isrc, track: dict | None, album: dict | None, today) -> dict` with keys `deezer_genres` (list[str]), `deezer_year` (int | None), `_source_ref`.

- [ ] **Step 1: Write the failing tests** (`tests/test_deezer.py`)

```python
import json
from pathlib import Path

import pytest

from core import deezer

FIX = Path(__file__).parent / "fixtures"
TRACK = json.loads((FIX / "deezer_track.json").read_text())
ALBUM = json.loads((FIX / "deezer_album.json").read_text())


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise deezer.httpx.HTTPStatusError("x", request=None, response=None)


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kw):
        self.calls.append(url)
        return self.responses.pop(0)


def test_lookup_fetches_track_then_album():
    c = FakeClient([FakeResp(200, TRACK), FakeResp(200, ALBUM)])
    track, album = deezer.lookup("USUM71703861", client=c)
    assert c.calls == [
        "https://api.deezer.com/2.0/track/isrc:USUM71703861",
        f"https://api.deezer.com/album/{TRACK['album']['id']}",
    ]
    assert track["id"] == TRACK["id"] and album["id"] == ALBUM["id"]


def test_lookup_unknown_isrc_returns_none_pair():
    c = FakeClient([FakeResp(200, {"error": {"type": "DataException", "message": "no data"}})])
    assert deezer.lookup("XX0000000000", client=c) == (None, None)


def test_to_row_genres_and_year():
    row = deezer.to_row("USUM71703861", TRACK, ALBUM, "2026-09-08")
    assert row["deezer_genres"] == [g["name"] for g in ALBUM["genres"]["data"]]
    assert row["deezer_year"] == int(TRACK["release_date"][:4])
    assert row["_source_ref"] == f"deezer:track/{TRACK['id']}@2026-09-08"


def test_to_row_no_track():
    assert deezer.to_row("XX0000000000", None, None, "2026-09-08") == {
        "deezer_genres": [],
        "deezer_year": None,
        "_source_ref": "deezer:isrc/XX0000000000@2026-09-08",
    }


def test_lookup_server_error_is_unavailable():
    c = FakeClient([FakeResp(500, {})])
    with pytest.raises(deezer.Unavailable):
        deezer.lookup("USUM71703861", client=c)
```

Fixtures: `deezer_track.json` = real `GET https://api.deezer.com/2.0/track/isrc:USUM71703861` response (no auth), trimmed to `id, title, isrc, release_date, album.{id,title}, artist.name`. `deezer_album.json` = real `GET https://api.deezer.com/album/<that album id>` trimmed to `id, title, release_date, genres.data[*].{id,name}`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_deezer.py -v`
Expected: FAIL, `No module named 'core.deezer'`

- [ ] **Step 3: Implement `src/core/deezer.py`**

```python
"""ISRC -> Deezer track + album genres. Unauthenticated public API."""

import httpx

BASE = "https://api.deezer.com"


class Unavailable(Exception):
    """Deezer could not be reached or answered with an error (short reason only)."""


def _get(url: str, client=None) -> dict:
    try:
        r = (client or httpx).get(url, timeout=10)
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise Unavailable(type(e).__name__) from e


def lookup(isrc: str, client=None) -> tuple[dict | None, dict | None]:
    track = _get(f"{BASE}/2.0/track/isrc:{isrc}", client)
    if not track.get("id"):  # Deezer answers 200 with {"error": ...} for unknown ISRCs
        return None, None
    album_id = (track.get("album") or {}).get("id")
    album = _get(f"{BASE}/album/{album_id}", client) if album_id else None
    return track, album


def to_row(isrc: str, track: dict | None, album: dict | None, today: str) -> dict:
    if not track:
        return {"deezer_genres": [], "deezer_year": None, "_source_ref": f"deezer:isrc/{isrc}@{today}"}
    genres = [g["name"] for g in ((album or {}).get("genres") or {}).get("data", []) if g.get("name")]
    rd = track.get("release_date") or ""
    return {
        "deezer_genres": genres,
        "deezer_year": int(rd[:4]) if rd[:4].isdigit() else None,
        "_source_ref": f"deezer:track/{track['id']}@{today}",
    }
```

- [ ] **Step 4: Endpoint in `app.py`** (same shape as `spotify_isrc`; reuse `_isrc_input`)

```python
@app.function(image=image, secrets=secrets)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def deezer_isrc(body: dict):
    from datetime import date

    from fastapi.responses import JSONResponse

    from core import deezer

    isrc = _isrc_input(body)
    if not isrc:
        return JSONResponse({"error": "inputs.id is required"}, status_code=422)
    try:
        track, album = deezer.lookup(isrc)
    except deezer.Unavailable as e:
        return JSONResponse({"error": f"deezer: {e}"}, status_code=502)
    return deezer.to_row(isrc, track, album, date.today().isoformat())
```

- [ ] **Step 5: Run tests and lint, commit**

Run: `uv run pytest -v && just check`
Expected: PASS.

```bash
git add -A && git commit -m "Add /deezer_isrc derivation: album genres and release year

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

### Task 3: MusicBrainz ISRC derivation endpoint (paced)

**Files:**
- Create: `src/core/musicbrainz.py`, `tests/test_musicbrainz.py`, `tests/fixtures/musicbrainz_isrc.json`
- Modify: `app.py`, `tests/test_app.py`

**Interfaces:**
- Produces: `musicbrainz.lookup(isrc, client=None, sleep=time.sleep) -> dict | None` (the `/ws/2/isrc/<isrc>?inc=releases+tags&fmt=json` body, None on 404), raises `Unavailable`. `musicbrainz.to_row(isrc, body, today) -> dict` with `mb_tags` (list[str], lowercased, count > 0, sorted), `mb_first_year` (int | None), `_source_ref`. Module constant `MIN_INTERVAL = 1.1` seconds; `lookup` sleeps so consecutive calls in one process are at least that far apart.

- [ ] **Step 1: Write the failing tests** (`tests/test_musicbrainz.py`)

```python
import json
from pathlib import Path

import pytest

from core import musicbrainz as mb

FIX = json.loads((Path(__file__).parent / "fixtures" / "musicbrainz_isrc.json").read_text())


class FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._p = status, payload

    def json(self):
        return self._p

    def raise_for_status(self):
        if self.status_code >= 400:
            raise mb.httpx.HTTPStatusError("x", request=None, response=None)


class FakeClient:
    def __init__(self, responses):
        self.responses, self.calls = list(responses), []

    def get(self, url, **kw):
        self.calls.append((url, kw))
        return self.responses.pop(0)


def test_lookup_sends_user_agent_and_inc():
    c = FakeClient([FakeResp(200, FIX)])
    body = mb.lookup("GBUM71304955", client=c, sleep=lambda s: None)
    url, kw = c.calls[0]
    assert url == "https://musicbrainz.org/ws/2/isrc/GBUM71304955"
    assert kw["params"] == {"inc": "releases+tags", "fmt": "json"}
    assert kw["headers"]["User-Agent"].startswith("music-sync-derivations/")
    assert body["isrc"] == "GBUM71304955"


def test_lookup_404_is_none():
    c = FakeClient([FakeResp(404, {"error": "Not Found"})])
    assert mb.lookup("XX0000000000", client=c, sleep=lambda s: None) is None


def test_lookup_paces_consecutive_calls(monkeypatch):
    slept = []
    clock = iter([100.0, 100.0, 100.2, 100.2])
    monkeypatch.setattr(mb.time, "monotonic", lambda: next(clock))
    c = FakeClient([FakeResp(200, FIX), FakeResp(200, FIX)])
    mb._last_call = 0.0
    mb.lookup("A", client=c, sleep=slept.append)
    mb.lookup("B", client=c, sleep=slept.append)
    assert slept and abs(slept[-1] - (mb.MIN_INTERVAL - 0.2)) < 0.01


def test_to_row_tags_and_first_year():
    row = mb.to_row("GBUM71304955", FIX, "2026-09-08")
    assert row["mb_tags"] == sorted(
        {t["name"].lower() for r in FIX["recordings"] for t in r.get("tags", []) if t["count"] > 0}
    )
    assert row["mb_first_year"] == int(FIX["recordings"][0]["first-release-date"][:4])
    assert row["_source_ref"] == f"musicbrainz:recording/{FIX['recordings'][0]['id']}@2026-09-08"


def test_to_row_none():
    assert mb.to_row("XX0000000000", None, "2026-09-08") == {
        "mb_tags": [],
        "mb_first_year": None,
        "_source_ref": "musicbrainz:isrc/XX0000000000@2026-09-08",
    }
```

Fixture `musicbrainz_isrc.json`: real `GET https://musicbrainz.org/ws/2/isrc/GBUM71304955?inc=releases+tags&fmt=json` (Elton John, "Bennie And The Jets", 2014 remaster), with `User-Agent: music-sync-derivations/0.1 (https://github.com/alexjmiller5/derivations)`. Keep `isrc`, `recordings[*].{id,title,first-release-date,tags[*].{name,count},releases[*].{id,date}}`.

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_musicbrainz.py -v`
Expected: FAIL, `No module named 'core.musicbrainz'`

- [ ] **Step 3: Implement `src/core/musicbrainz.py`**

```python
"""ISRC -> MusicBrainz recording tags and first-release year.

MusicBrainz allows one request per second per User-Agent. `lookup` paces
itself inside a process; app.py also pins the Modal function to one
container so parallel invocations cannot exceed the limit.
"""

import time

import httpx

BASE = "https://musicbrainz.org/ws/2"
USER_AGENT = "music-sync-derivations/0.1 (https://github.com/alexjmiller5/derivations)"
MIN_INTERVAL = 1.1
_last_call = 0.0


class Unavailable(Exception):
    """MusicBrainz could not be reached or answered with an error (short reason only)."""


def lookup(isrc: str, client=None, sleep=time.sleep) -> dict | None:
    global _last_call
    wait = MIN_INTERVAL - (time.monotonic() - _last_call)
    if wait > 0:
        sleep(wait)
    _last_call = time.monotonic()
    try:
        r = (client or httpx).get(
            f"{BASE}/isrc/{isrc}",
            params={"inc": "releases+tags", "fmt": "json"},
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
            timeout=20,
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.json()
    except httpx.HTTPError as e:
        raise Unavailable(type(e).__name__) from e


def to_row(isrc: str, body: dict | None, today: str) -> dict:
    recs = (body or {}).get("recordings") or []
    if not recs:
        return {"mb_tags": [], "mb_first_year": None, "_source_ref": f"musicbrainz:isrc/{isrc}@{today}"}
    tags = sorted({t["name"].lower() for r in recs for t in r.get("tags") or [] if t.get("count", 0) > 0 and t.get("name")})
    years = [int(r["first-release-date"][:4]) for r in recs if (r.get("first-release-date") or "")[:4].isdigit()]
    years += [int(rel["date"][:4]) for r in recs for rel in r.get("releases") or [] if (rel.get("date") or "")[:4].isdigit()]
    return {
        "mb_tags": tags,
        "mb_first_year": min(years) if years else None,
        "_source_ref": f"musicbrainz:recording/{recs[0]['id']}@{today}",
    }
```

- [ ] **Step 4: Endpoint in `app.py`**, one container only:

```python
@app.function(image=image, secrets=secrets, max_containers=1, timeout=60)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def musicbrainz_isrc(body: dict):
    from datetime import date

    from fastapi.responses import JSONResponse

    from core import musicbrainz

    isrc = _isrc_input(body)
    if not isrc:
        return JSONResponse({"error": "inputs.id is required"}, status_code=422)
    try:
        found = musicbrainz.lookup(isrc)
    except musicbrainz.Unavailable as e:
        return JSONResponse({"error": f"musicbrainz: {e}"}, status_code=502)
    return musicbrainz.to_row(isrc, found, date.today().isoformat())
```

`max_containers=1` is the Modal 1.5 name (verified in the pinned version); with the default of one input per container this serializes calls.

- [ ] **Step 5: Run tests and lint, commit**

Run: `uv run pytest -v && just check`

```bash
git add -A && git commit -m "Add /musicbrainz_isrc derivation: tags and first-release year, 1 req/s

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

### Task 4: `first_year` endpoint, register the four derivations on the hub, deploy

**Files:**
- Create: `src/core/years.py`, `tests/test_years.py`
- Modify: `app.py`, `README.md` (endpoint table), `tests/test_app.py`

**Interfaces:**
- Produces: `years.first_year(inputs: dict) -> dict` returning `{"first_year": int | None, "_source_ref": "min:<a>,<b>,<c>"}`.
- The hub's `DERIVATIONS` secret gains `spotify_isrc`, `deezer_isrc`, `musicbrainz_isrc`, `first_year` (used by Task 5).

- [ ] **Step 1: Write the failing test** (`tests/test_years.py`)

```python
from core import years


def test_first_year_is_min_of_present_inputs():
    out = years.first_year({"album_year": 2018, "deezer_year": 2018, "mb_first_year": 1974})
    assert out == {"first_year": 1974, "_source_ref": "min:2018,2018,1974"}


def test_first_year_ignores_nulls_and_garbage():
    assert years.first_year({"album_year": None, "deezer_year": "2011", "mb_first_year": 0})["first_year"] == 2011


def test_first_year_all_null():
    assert years.first_year({"album_year": None, "deezer_year": None, "mb_first_year": None}) == {
        "first_year": None,
        "_source_ref": "min:",
    }
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement `src/core/years.py`**

```python
"""first_year = the earliest plausible year among the source years."""

INPUTS = ("album_year", "deezer_year", "mb_first_year")


def _year(v) -> int | None:
    try:
        y = int(v)
    except (TypeError, ValueError):
        return None
    return y if 1900 <= y <= 2100 else None


def first_year(inputs: dict) -> dict:
    ys = [_year(inputs.get(k)) for k in INPUTS]
    present = [y for y in ys if y is not None]
    return {
        "first_year": min(present) if present else None,
        "_source_ref": "min:" + ",".join(str(y) for y in present),
    }
```

- [ ] **Step 4: Endpoint** (no network, no secrets needed but keep the same decorator shape):

```python
@app.function(image=image, secrets=secrets)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def first_year(body: dict):
    from core import years

    return years.first_year(body.get("inputs") or {})
```

- [ ] **Step 5: README endpoint table** - add four rows under "Endpoints": `POST /spotify_isrc`, `/deezer_isrc`, `/musicbrainz_isrc` (input `inputs.id` = ISRC), `/first_year` (inputs `album_year, deezer_year, mb_first_year`), with their output keys as defined in Tasks 1-4. Note the MusicBrainz 1 req/s pacing and single container.

- [ ] **Step 6: Tests, lint, commit, push, watch the deploy**

```bash
uv run pytest -v && just check
git add -A && git commit -m "Add /first_year derivation and document the ISRC endpoints

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
git push
gh run list --workflow=deploy.yml --limit 1
gh run watch <run-id> --exit-status
```

Expected: deploy green; `modal app list` still shows `derivations` deployed. Note the four endpoint URLs printed by the deploy log (`https://<workspace>--derivations-spotify-isrc.modal.run` etc.).

- [ ] **Step 7: Smoke each endpoint live** with the hub's existing proxy-auth token (the `Modal-Key`/`Modal-Secret` pair already inside the `DERIVATIONS` field of the `Life Data ENV` item; read it with desktop auth):

```bash
zsh -ic 'd=$(op-personal read "op://Life Data/Life Data ENV/DERIVATIONS"); key=$(jq -r ".tmdb_movie.headers[\"Modal-Key\"]" <<<"$d"); sec=$(jq -r ".tmdb_movie.headers[\"Modal-Secret\"]" <<<"$d"); for ep in spotify-isrc deezer-isrc musicbrainz-isrc; do curl -s -X POST "https://<workspace>--derivations-$ep.modal.run" -H "Modal-Key: $key" -H "Modal-Secret: $sec" -H "Content-Type: application/json" -d "{\"tbl\":\"songs\",\"id\":\"GBUM71304955\",\"inputs\":{\"id\":\"GBUM71304955\"}}"; echo; done; curl -s -X POST "https://<workspace>--derivations-first-year.modal.run" -H "Modal-Key: $key" -H "Modal-Secret: $sec" -H "Content-Type: application/json" -d "{\"inputs\":{\"album_year\":2014,\"deezer_year\":2014,\"mb_first_year\":1973}}"'
```

Expected: JSON rows; `first_year` returns 1973.

- [ ] **Step 8: Register the names on the hub.** Read the current `DERIVATIONS` JSON from the `Life Data ENV` item, add four keys with the same `headers` object as `tmdb_movie`, write it back to the item, then set the Worker secret (both desktop auth, Alex approves each):

```bash
zsh -ic 'd=$(op-personal read "op://Life Data/Life Data ENV/DERIVATIONS"); new=$(jq -c --arg w "<workspace>" ". + {spotify_isrc: {url: \"https://\($w)--derivations-spotify-isrc.modal.run\", headers: .tmdb_movie.headers}, deezer_isrc: {url: \"https://\($w)--derivations-deezer-isrc.modal.run\", headers: .tmdb_movie.headers}, musicbrainz_isrc: {url: \"https://\($w)--derivations-musicbrainz-isrc.modal.run\", headers: .tmdb_movie.headers}, first_year: {url: \"https://\($w)--derivations-first-year.modal.run\", headers: .tmdb_movie.headers}}" <<<"$d"); op-personal item edit "Life Data ENV" --vault "Life Data" "DERIVATIONS[concealed]=$new"'
cd ~/Desktop/coding/active-projects/life-data/worker && zsh -ic 'export CLOUDFLARE_API_TOKEN=$(op-personal read "op://Life Data/Life Data CI Cloudflare Token/credential"); op-personal read "op://Life Data/Life Data ENV/DERIVATIONS" | bunx wrangler@4 secret put DERIVATIONS'
```

(If the CI token item name differs, list the vault first: `zsh -ic 'op-personal item list --vault "Life Data"'`. This mirrors how the secret was first set in the movies migration plan, step "DERIVATIONS JSON".)

Verify: `curl -s https://life-data.nqipomyrjb.workers.dev/health` is `{"ok":true}`; the names take effect on the next derivation call (Task 5 step 6 proves it).

## Phase B - life-data catalog (state, via the installed CLI)

### Task 5: Create `songs`, `playlists`, `playlist_songs`, rules, token; document in life-map

**Files (state, not repo):** the local life-data replica + hub. **Docs:** `~/.config/agent-config/skills/life-map/SKILL.md` and `references/schema.md` (regenerated).

**Interfaces:**
- Produces the exact column set every later task reads and writes. Column names below are the contract; `Song`, `Playlist`, `Membership` dataclasses in Task 8 mirror them one to one.

- [ ] **Step 1: Confirm the client is current and the replica is synced**

```bash
life sync && life check | tail -3
```

Expected: sync prints `{"pushed": 0, "pulled": N, ...}`; `check` exits 0 (or lists only pre-existing findings; note them).

- [ ] **Step 2: Create the tables** (quote every spec; `!` = required; `(a|b)` = options)

```bash
life table create songs 'liked:int!' 'liked_at:datetime' 'first_seen:datetime!' \
  'title:text' 'artists:json' 'album:text' 'album_year:int' 'duration_ms:int' \
  'spotify_ids:json' 'spotify_playable:int' 'deezer_genres:json' 'deezer_year:int' \
  'mb_tags:json' 'mb_first_year:int' 'first_year:int'
life table create playlists 'name:text!' 'kind:select!(inbox|curated|smart)' 'rule:json' \
  'description:text' 'snapshot_id:text' 'track_count:int' 'pinned:int!' 'expires_at:datetime' \
  'last_reconciled:datetime'
life table create playlist_songs 'playlist_id:ref!' 'isrc:ref!' 'spotify_track_id:text!' 'added_at:datetime!'
life property set playlist_songs.playlist_id --ref-table playlists
life property set playlist_songs.isrc --ref-table songs
```

- [ ] **Step 3: Constrain and describe the columns**

```bash
life property set songs.id --immutable 1 --pattern '^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$' --description "ISRC (International Standard Recording Code). Row identity; re-imports dedupe on it. Never a Spotify id: Spotify relinks ids freely and one recording carries several."
life property set songs.liked --default 0 --options-sql "SELECT 0 UNION SELECT 1" --description "1 = in Liked Songs at the last reconcile. liked=1 IS the pool: eligible for every smart playlist. liked=0 = the reconciler removes the song from every non-inbox playlist."
life property set songs.liked_at --description "Spotify added_at of the like (ISO UTC ms). Null when liked=0."
life property set songs.first_seen --description "First reconcile or capture that saw this recording anywhere."
for c in title artists album album_year duration_ms spotify_ids spotify_playable; do life property set songs.$c --derived-by 'http:spotify_isrc' --inputs id; done
life property set songs.spotify_ids --description "Every Spotify track id Spotify's ISRC search returns for this recording, preferred (playable, album release, earliest) first. JSON array."
life property set songs.spotify_playable --description "1 if any of spotify_ids is playable in the user's market at last derivation."
for c in deezer_genres deezer_year; do life property set songs.$c --derived-by 'http:deezer_isrc' --inputs id; done
life property set songs.deezer_genres --description "Deezer album genre names verbatim (Pop, Rock, Rap/Hip Hop, Dance, ...). JSON array. Coarse buckets for rules."
for c in mb_tags mb_first_year; do life property set songs.$c --derived-by 'http:musicbrainz_isrc' --inputs id; done
life property set songs.mb_tags --description "MusicBrainz recording tags with count > 0, lowercased. JSON array. Fine-grained genre words for rules (deep house, reggaeton, west coast hip hop)."
life property set songs.first_year --derived-by 'http:first_year' --inputs album_year,deezer_year,mb_first_year --description "min of album_year, deezer_year, mb_first_year. Ceiling: a remaster under its own ISRC reports the remaster year when no source knows the original."
life property set playlists.id --immutable 1 --description "Spotify playlist id. Smart playlists are created in Spotify first (agent, spotify skill) so rows always carry the real id; the cron never creates playlists."
life property set playlists.kind --default curated --options '[{"v":"inbox","d":"Exactly one: new songs. Capped at 100, FIFO. Membership implies nothing; exempt from every liked rule."},{"v":"curated","d":"Hand-edited in the Spotify app. A curated playlist is a tag. Songs here are liked by the reconciler."},{"v":"smart","d":"Membership = rule over liked songs, fully materialized each run. Description rendered from rule."}]'
life property set playlists.rule --description "Smart playlist definition, rule JSON v1 (music-sync spec section 6.1): keys v, deezer_genres_any, mb_tags_any, first_year{lt,gte,between}, in_playlist_any, not_in_playlist, captured_by, liked_after. Written by an agent on the user's instruction; the only human-originated column besides kind/pinned/expires_at."
life property set playlists.pinned --default 1 --options-sql "SELECT 0 UNION SELECT 1" --description "0 = ephemeral smart playlist, deleted by the reconciler at expires_at."
life property set playlists.snapshot_id --description "Spotify snapshot id at last reconcile; an unchanged snapshot skips the playlist's item pull."
life property set playlist_songs.id --immutable 1 --description "<playlist_id>:<isrc>, deterministic."
life property set playlist_songs.added_at --description "Spotify added_at of the item (ISO UTC ms)."
```

- [ ] **Step 4: Describe the tables**

```bash
life table set songs --purpose "One row per recording the user has ever liked or placed in an owned Spotify playlist. Rows are history: never soft-deleted by the reconciler." --id-semantics "ISRC" --provenance "Spotify (playlists + Liked Songs) via the music-sync reconciler and /capture; metadata derived from Spotify, Deezer, MusicBrainz" --owner "music-sync (Modal)" --consumers music-sync,life-ui --description "liked=1 is the pool. Capture origin is a provenance edge rel=imported_from from_kind in (shazam, playlist, like), never a column. Derived columns are filled by the hub; a client write to them is rejected."
life table set playlists --purpose "One row per Spotify playlist the user owns." --id-semantics "Spotify playlist id" --provenance "Spotify via music-sync; kind/rule/pinned/expires_at written by agents" --owner "music-sync (Modal)" --consumers music-sync,life-ui --description "kind drives the reconciler (music-sync spec section 5). rule is validated JSON; the reconciler renders it into the Spotify description every run."
life table set playlist_songs --purpose "Membership of each owned playlist as of the last reconcile." --id-semantics "<playlist_id>:<isrc>" --provenance "Spotify playlist items via music-sync" --owner "music-sync (Modal)" --consumers music-sync,life-ui --description "Soft-deleted on removal; a re-like within 7 days restores curated memberships from the soft-deleted rows (undo)."
```

- [ ] **Step 5: Rules**

```bash
life rule set playlists-one-inbox --scope table --tbl playlists --kind invariant --enforce 1 \
  --text "Exactly one playlist has kind inbox." \
  --sql "SELECT id FROM changed WHERE kind='inbox' AND (SELECT count(*) FROM playlists WHERE kind='inbox' AND deleted_at IS NULL) > 1"
life rule set playlists-rule-iff-smart --scope table --tbl playlists --kind invariant --enforce 1 \
  --text "rule is present exactly when kind is smart, and is valid rule JSON v1." \
  --sql "SELECT id FROM changed WHERE (kind='smart') != (rule IS NOT NULL) OR (rule IS NOT NULL AND (json_valid(rule)=0 OR json_extract(rule,'$.v') != 1 OR EXISTS (SELECT 1 FROM json_each(rule) WHERE key NOT IN ('v','deezer_genres_any','mb_tags_any','first_year','in_playlist_any','not_in_playlist','captured_by','liked_after'))))"
life rule set songs-liked-is-pool --scope table --tbl songs --kind doctrine \
  --text "liked=1 means eligible for every smart playlist; liked=0 means the reconciler removes the song from every playlist except the inbox."
life rule set curated-songs-are-liked --scope table --tbl playlist_songs --kind invariant \
  --text "Every song in a curated playlist is liked (reconciler bug if violated)." \
  --sql "SELECT ps.id FROM playlist_songs ps JOIN playlists p ON p.id=ps.playlist_id JOIN songs s ON s.id=ps.isrc WHERE ps.deleted_at IS NULL AND p.deleted_at IS NULL AND p.kind='curated' AND s.liked=0"
```

The generic `smart-songs-match-rule` invariant is added in Task 9 step 7 once `rule_to_sql` exists, because its SQL is generated by that function.

- [ ] **Step 6: Provenance kinds, token, sync, verify**

```bash
life property set provenance.from_kind --options "$(life sql "SELECT options FROM catalog_properties WHERE tbl='provenance' AND col='from_kind'" | jq -c '.[0].options | fromjson + [{"v":"shazam","d":"Captured by the Shazam shortcut through music-sync /capture; from_ref = the Apple Music id when known, else the ISRC."},{"v":"playlist","d":"Entered the catalog by being in an owned Spotify playlist; from_ref = the Spotify playlist id."},{"v":"like","d":"Entered the catalog by being liked; from_ref = liked."}]')"
life token create music-sync --scopes tables:write
```

Store the printed token as field `LIFE_HUB_TOKEN` on the `Music Sync ENV` item (desktop auth, Alex approves): `zsh -ic 'op-personal item edit 65zbc6qstoi6m64hjtxb5uuhu4 --vault 4cpe3gxzolbxtu4tnsyzzvhype "LIFE_HUB_TOKEN[concealed]=<value>"'`. Never paste the value into a file.

```bash
life sync
life sql "INSERT INTO songs (id, liked, first_seen) VALUES ('GBUM71304955', 1, '2026-09-08T00:00:00.000Z')"
life sync
sleep 90   # hub derives on push in the background; the 15-min sweep is the fallback
life sync && life sql "SELECT id, title, artists, album_year, deezer_genres, mb_first_year, first_year FROM songs WHERE id='GBUM71304955'"
```

Expected: the row comes back with `title` "Bennie And The Jets", `deezer_genres` non-empty, `first_year` 1973 or 1974 (the `first_year` derivation runs after the three source derivations land; if it is still null after one sweep, `life derive songs.first_year` forces it). Then remove the probe row: `life sql "UPDATE songs SET deleted_at=updated_at WHERE id='GBUM71304955'"` and `life sync`.

- [ ] **Step 7: Document in life-map** (agent-config repo). Run the life-map Self-update to regenerate `references/schema.md`, then add a short section to `SKILL.md` under Tables: what a `songs` row IS, `liked` = pool, capture edges, that the reconciler is the only writer, and the three-kind playlist model with a pointer to the music-sync spec. Bump Last verified. Commit and push agent-config (no deploy workflow there):

```bash
cd ~/.config/agent-config && git add -A && git commit -m "life-map: songs, playlists, playlist_songs (music-sync)

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns" && git push
```

## Phase C - music-sync app

### Task 6: Clear the prototype, new Settings, re-mint the token, hub client

**Files:**
- Delete: `src/core/export_ingest.py`, `src/core/notion_sync.py`, `src/core/playlist_builder.py`, `src/core/models.py`, `src/core/pipeline.py`, `scripts/ingest_export.py`, `scripts/sync_playlists_to_notion.py`, `scripts/sync_followed_to_notion.py`, `sandbox_config.json`, `data/snapshot.json`, `tests/test_export_ingest.py`, `tests/test_notion_sync.py`, `tests/test_playlist_builder.py`, `tests/test_pipeline.py`, `tests/test_config.py`, `tests/fixtures/{follow,your_library,playlists}.json`, tracked `.DS_Store` files
- Keep for now: `scripts/create_50s_playlist.py` (deleted in the migration, step 9)
- Modify: `src/core/config.py`, `scripts/spotify_auth.py:31-35`, `.env.tpl`, `.gitignore`, `README.md`
- Create: `src/core/hub.py`, `tests/test_hub.py`, `tests/conftest.py`

**Interfaces:**
- Produces: `Settings` fields (all `str` unless noted): `spotify_client_id`, `spotify_client_secret`, `spotify_refresh_token`, `spotify_market = "US"`, `life_hub_url`, `life_hub_token`, `notion_token`, `notion_tasks_data_source_id`, `notion_project_page_id`, `r2_account_id`, `r2_bucket`, `r2_api_token`, `inbox_cap: int = 100`, `undo_days: int = 7`.
- `Hub(base_url, token, http=None)` with `pull(table: str, columns: list[str], since: str = "") -> list[dict]`, `push(table: str, rows: list[dict]) -> dict` (columns = sorted union of row keys; chunks of 500; raises `HubError` if any row rejected, message includes the first rejection), `derive(table: str, ids: list[str]) -> dict` (chunks of 50, aggregated `{"derived", "failed"}`).
- `tests/conftest.py` fixture `settings` returning a fully populated `Settings(...)` with dummy values.

- [ ] **Step 1: Delete the prototype and archive the export zips**

```bash
cd ~/Desktop/coding/active-projects/music-sync
git rm -q src/core/export_ingest.py src/core/notion_sync.py src/core/playlist_builder.py src/core/models.py src/core/pipeline.py scripts/ingest_export.py scripts/sync_playlists_to_notion.py scripts/sync_followed_to_notion.py tests/test_export_ingest.py tests/test_notion_sync.py tests/test_playlist_builder.py tests/test_pipeline.py tests/test_config.py tests/fixtures/follow.json tests/fixtures/your_library.json tests/fixtures/playlists.json
git rm -q --cached .DS_Store 2>/dev/null; find . -name .DS_Store -not -path './.git/*' -delete
rm -f sandbox_config.json data/snapshot.json
printf '.DS_Store\n' >> .gitignore
```

The three GDPR zips in `data/raw/` are uploaded to R2 in the migration (Task 16 step 4) and the directory removed then; leave them untouched now.

- [ ] **Step 2: Rewrite `src/core/config.py`**

```python
"""Settings from env vars - Modal Secret in the cloud, `op run` locally.

One field per line in .env.tpl. Instantiate Settings() inside functions,
never at import time, so tests run without secrets.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    spotify_client_id: str
    spotify_client_secret: str
    spotify_refresh_token: str
    spotify_market: str = "US"
    life_hub_url: str
    life_hub_token: str
    notion_token: str
    notion_tasks_data_source_id: str
    notion_project_page_id: str
    r2_account_id: str
    r2_bucket: str
    r2_api_token: str
    inbox_cap: int = 100
    undo_days: int = 7
```

`.env.tpl` (replace the Notion lines; keep the header comments):

```
SPOTIFY_CLIENT_ID=op://Music Sync/Music Sync ENV/SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET=op://Music Sync/Music Sync ENV/SPOTIFY_CLIENT_SECRET
SPOTIFY_REFRESH_TOKEN=op://Music Sync/Music Sync ENV/SPOTIFY_REFRESH_TOKEN
LIFE_HUB_URL=op://Music Sync/Music Sync ENV/LIFE_HUB_URL
LIFE_HUB_TOKEN=op://Music Sync/Music Sync ENV/LIFE_HUB_TOKEN
NOTION_TOKEN=op://Music Sync/Music Sync ENV/NOTION_TOKEN
NOTION_TASKS_DATA_SOURCE_ID=op://Music Sync/Music Sync ENV/NOTION_TASKS_DATA_SOURCE_ID
NOTION_PROJECT_PAGE_ID=op://Music Sync/Music Sync ENV/NOTION_PROJECT_PAGE_ID
R2_ACCOUNT_ID=op://Music Sync/Music Sync ENV/R2_ACCOUNT_ID
R2_BUCKET=op://Music Sync/Music Sync ENV/R2_BUCKET
R2_API_TOKEN=op://Music Sync/Music Sync ENV/R2_API_TOKEN
```

`tests/conftest.py`:

```python
import pytest

from core.config import Settings


@pytest.fixture
def settings():
    return Settings(
        spotify_client_id="cid", spotify_client_secret="csec", spotify_refresh_token="rtok",
        life_hub_url="https://hub.test", life_hub_token="hubtok",
        notion_token="ntok", notion_tasks_data_source_id="ds", notion_project_page_id="proj",
        r2_account_id="acct", r2_bucket="bucket", r2_api_token="r2tok",
    )
```

- [ ] **Step 3: Scopes and re-mint.** In `scripts/spotify_auth.py` set

```python
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-private playlist-modify-public "
    "user-library-read user-library-modify user-follow-read"
)
```

and change the default `--port` to `8080` (the redirect URI registered on the "AI Agent" app is `http://127.0.0.1:8080/callback`). Update the docstring line that says 8888. Then mint (the script opens the consent page; drive the Agree click with `chrome-cli` as done on 2026-09-07, or let Alex click) and save the token to the vault item field `SPOTIFY_REFRESH_TOKEN` via `zsh -ic 'op-personal item edit 65zbc6qstoi6m64hjtxb5uuhu4 --vault 4cpe3gxzolbxtu4tnsyzzvhype "SPOTIFY_REFRESH_TOKEN=<value>"'` (never write it to disk). Verify the new scope set:

```bash
op run --env-file=.env.tpl -- bash -c 'curl -s -X POST https://accounts.spotify.com/api/token -u "$SPOTIFY_CLIENT_ID:$SPOTIFY_CLIENT_SECRET" -d grant_type=refresh_token -d refresh_token="$SPOTIFY_REFRESH_TOKEN" | jq -r .scope'
```

Expected: includes `user-library-modify`. (Until Task 13 fills the other ENV fields, `op run` reports CHANGEME for them; that is fine for this check.)

- [ ] **Step 4: Write the failing hub tests** (`tests/test_hub.py`)

```python
import json

import httpx
import pytest

from core.hub import Hub, HubError


def make(handler):
    return Hub("https://hub.test/", "tok", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_pull_posts_table_columns_since_with_bearer_and_user_agent():
    seen = {}

    def handler(req):
        seen["url"], seen["body"], seen["h"] = str(req.url), json.loads(req.content), req.headers
        return httpx.Response(200, json={"rows": [{"id": "A"}]})

    assert make(handler).pull("songs", ["id"]) == [{"id": "A"}]
    assert seen["url"] == "https://hub.test/v1/rows/pull"
    assert seen["body"] == {"table": "songs", "columns": ["id"], "since": ""}
    assert seen["h"]["Authorization"] == "Bearer tok"
    assert "music-sync" in seen["h"]["User-Agent"]


def test_push_chunks_and_unions_columns():
    bodies = []

    def handler(req):
        bodies.append(json.loads(req.content))
        return httpx.Response(200, json={"upserted": len(bodies[-1]["rows"]), "rejected": [], "hub_at": "t"})

    rows = [{"id": str(i), "liked": 1} for i in range(501)] + [{"id": "x", "first_seen": "s"}]
    out = make(handler).push("songs", rows)
    assert out["upserted"] == 502 and len(bodies) == 2
    assert bodies[0]["columns"] == ["first_seen", "id", "liked"]
    assert bodies[0]["rows"][0] == {"id": "0", "liked": 1, "first_seen": None}


def test_push_raises_on_rejection():
    def handler(req):
        return httpx.Response(200, json={"upserted": 0, "rejected": [{"id": "A", "rule": "pattern"}], "hub_at": "t"})

    with pytest.raises(HubError, match="pattern"):
        make(handler).push("songs", [{"id": "A"}])


def test_derive_chunks_of_50():
    sizes = []

    def handler(req):
        sizes.append(len(json.loads(req.content)["ids"]))
        return httpx.Response(200, json={"derived": sizes[-1], "failed": []})

    out = make(handler).derive("songs", [str(i) for i in range(120)])
    assert sizes == [50, 50, 20] and out == {"derived": 120, "failed": []}


def test_http_error_is_huberror():
    def handler(req):
        return httpx.Response(500, text="boom")

    with pytest.raises(HubError, match="500"):
        make(handler).pull("songs", ["id"])
```

- [ ] **Step 5: Run to verify failure**, then **Step 6: implement `src/core/hub.py`**

```python
"""life-data hub client over the HTTP protocol. Knows a URL and a bearer token, nothing else."""

import httpx

PUSH_CHUNK = 500
DERIVE_CHUNK = 50
USER_AGENT = "music-sync/0.1 (+https://github.com/alexjmiller5/music-sync)"


class HubError(RuntimeError):
    pass


class Hub:
    def __init__(self, base_url: str, token: str, http: httpx.Client | None = None):
        self.base = base_url.rstrip("/")
        self._http = http or httpx.Client(timeout=120)
        self._headers = {"Authorization": f"Bearer {token}", "User-Agent": USER_AGENT}

    def _post(self, route: str, body: dict) -> dict:
        try:
            r = self._http.post(f"{self.base}{route}", json=body, headers=self._headers)
        except httpx.HTTPError as e:
            raise HubError(f"hub unreachable: {type(e).__name__}") from e
        if r.status_code >= 400:
            raise HubError(f"hub HTTP {r.status_code}: {r.text[:300]}")
        return r.json()

    def pull(self, table: str, columns: list[str], since: str = "") -> list[dict]:
        return self._post("/v1/rows/pull", {"table": table, "columns": columns, "since": since})["rows"]

    def push(self, table: str, rows: list[dict]) -> dict:
        if not rows:
            return {"upserted": 0, "rejected": []}
        columns = sorted({k for r in rows for k in r})
        total, rejected = 0, []
        for i in range(0, len(rows), PUSH_CHUNK):
            chunk = [{c: r.get(c) for c in columns} for r in rows[i : i + PUSH_CHUNK]]
            out = self._post("/v1/rows/push", {"table": table, "columns": columns, "rows": chunk})
            total += out["upserted"]
            rejected += out.get("rejected", [])
        if rejected:
            raise HubError(f"{table}: {len(rejected)} rejected, first: {rejected[0]}")
        return {"upserted": total, "rejected": []}

    def derive(self, table: str, ids: list[str]) -> dict:
        derived, failed = 0, []
        for i in range(0, len(ids), DERIVE_CHUNK):
            out = self._post("/v1/derive", {"table": table, "ids": ids[i : i + DERIVE_CHUNK]})
            derived += out.get("derived", 0)
            failed += out.get("failed", [])
        return {"derived": derived, "failed": failed}
```

Note on `push`: the dict comprehension keeps insertion order `id, liked, first_seen` for the first row, so the test's expected dict compares equal (dict equality ignores order).

- [ ] **Step 7: Tests, lint, commit** (`uv run pytest -v && just check`). The old `test_spotify_client.py` imports `core.models` and will fail now; Task 7 rewrites it, so temporarily run `uv run pytest tests/test_hub.py` and commit with the known red test noted in the message:

```bash
git add -A && git commit -m "Remove the Notion prototype; new Settings, hub client, library-modify scope

test_spotify_client.py is red until the client is rewritten (next commit).

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

### Task 7: Spotify client for the reduced endpoint set

**Files:**
- Modify: `src/core/spotify_client.py` (rewrite; keep `_request` retry logic), `tests/test_spotify_client.py` (rewrite)

**Interfaces:**
- Produces `SpotifyClient(settings, http=None)` with:
  - `me() -> dict`
  - `get_playlists() -> list[dict]` (all pages of `/v1/me/playlists`, raw objects)
  - `get_playlist(playlist_id) -> dict`
  - `get_playlist_items(playlist_id, market) -> list[dict]` (all pages of `/v1/playlists/{id}/items?limit=50&market=<market>&fields=next,items(added_at,item(id,uri,name,is_local,is_playable,external_ids,artists(name),album(name,release_date)))`)
  - `get_liked(market) -> list[dict]` (all pages of `/v1/me/tracks?limit=50&market=<market>`)
  - `get_track(track_id, market) -> dict`
  - `search_isrc(isrc, market) -> list[dict]`, `search_track(title, artist, market) -> list[dict]` (both `limit=10`)
  - `add_items(playlist_id, uris: list[str]) -> None` (POST in chunks of 100), `remove_items(playlist_id, uris) -> None` (DELETE with JSON body `{"uris": [...]}`, chunks of 100)
  - `set_description(playlist_id, text) -> None` (PUT `/v1/playlists/{id}` `{"description": text[:300]}`)
  - `like(uris: list[str]) -> None` (PUT `/v1/me/library?uris=<comma-joined>` in chunks of 50, empty body)
  - raises `SpotifyAuthError` when the token refresh answers `invalid_grant` (spec §7.1: the run flags and stops)

- [ ] **Step 1: Write the failing tests** (`tests/test_spotify_client.py`, replacing the file)

```python
"""httpx.MockTransport, no network."""

import json

import httpx
import pytest

from core.spotify_client import SpotifyAuthError, SpotifyClient


def token_resp(tok="tok1"):
    return httpx.Response(200, json={"access_token": tok, "expires_in": 3600})


def make(handler, settings, mocker):
    mocker.patch("core.spotify_client.time.sleep")
    return SpotifyClient(settings, http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_invalid_grant_raises_auth_error(settings, mocker):
    def handler(req):
        return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Refresh token revoked"})

    with pytest.raises(SpotifyAuthError, match="invalid_grant"):
        make(handler, settings, mocker).me()


def test_playlist_items_uses_items_path_market_and_fields(settings, mocker):
    seen = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        seen.append(req.url)
        if req.url.params.get("offset") == "50":
            return httpx.Response(200, json={"items": [{"added_at": "b"}], "next": None})
        return httpx.Response(200, json={"items": [{"added_at": "a"}], "next": "https://api.spotify.com/v1/playlists/P/items?offset=50&limit=50"})

    items = make(handler, settings, mocker).get_playlist_items("P", "US")
    assert [i["added_at"] for i in items] == ["a", "b"]
    assert seen[0].path == "/v1/playlists/P/items"
    assert seen[0].params["market"] == "US" and seen[0].params["limit"] == "50"
    assert "external_ids" in seen[0].params["fields"]


def test_like_uses_query_param_chunks_of_50(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append((req.method, req.url.path, req.url.params.get("uris")))
        return httpx.Response(200)

    make(handler, settings, mocker).like([f"spotify:track:{i}" for i in range(51)])
    assert calls[0][0] == "PUT" and calls[0][1] == "/v1/me/library"
    assert calls[0][2].count("spotify:track:") == 50 and calls[1][2] == "spotify:track:50"


def test_add_and_remove_items_json_bodies(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200, json={"snapshot_id": "s"})

    c = make(handler, settings, mocker)
    c.add_items("P", ["spotify:track:1"])
    c.remove_items("P", ["spotify:track:2"])
    assert calls == [
        ("POST", "/v1/playlists/P/items", {"uris": ["spotify:track:1"]}),
        ("DELETE", "/v1/playlists/P/items", {"uris": ["spotify:track:2"]}),
    ]


def test_set_description_truncates_to_300(settings, mocker):
    calls = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        calls.append((req.method, req.url.path, json.loads(req.content)))
        return httpx.Response(200)

    make(handler, settings, mocker).set_description("P", "x" * 400)
    assert calls == [("PUT", "/v1/playlists/P", {"description": "x" * 300})]


def test_search_isrc_and_track_limit_10(settings, mocker):
    seen = []

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            return token_resp()
        seen.append(dict(req.url.params))
        return httpx.Response(200, json={"tracks": {"items": [{"id": "t"}]}})

    c = make(handler, settings, mocker)
    assert c.search_isrc("GBBTV1101287", "US") == [{"id": "t"}]
    assert c.search_track("Take My Hand", "Matt Berry", "US") == [{"id": "t"}]
    assert seen[0] == {"q": "isrc:GBBTV1101287", "type": "track", "limit": "10", "market": "US"}
    assert seen[1]["q"] == "track:Take My Hand artist:Matt Berry"


def test_429_then_retry_and_401_refresh(settings, mocker):
    state = {"n": 0, "tokens": 0}

    def handler(req):
        if req.url.host == "accounts.spotify.com":
            state["tokens"] += 1
            return token_resp(f"tok{state['tokens']}")
        state["n"] += 1
        if state["n"] == 1:
            return httpx.Response(429, headers={"Retry-After": "2"})
        if state["n"] == 2:
            return httpx.Response(401)
        return httpx.Response(200, json={"id": "me"})

    assert make(handler, settings, mocker).me() == {"id": "me"}
    assert state["tokens"] == 2
```

- [ ] **Step 2: Run to verify failure** (`uv run pytest tests/test_spotify_client.py -v`; expected: ImportError on `SpotifyAuthError`, then method errors)

- [ ] **Step 3: Rewrite `src/core/spotify_client.py`**

```python
"""Thin Spotify Web API client for the post-2026-02 Development Mode endpoint
set (plain Python, no Modal imports). No batch endpoints; search limit 10;
Save to Library takes uris as a QUERY parameter.

Token refresh, 401 refresh-retry, 429 Retry-After, and next-url pagination
are unit-tested with mocks.
"""

import time

import httpx
import structlog

from core.config import Settings

log = structlog.get_logger()

TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com"
ITEM_FIELDS = (
    "next,items(added_at,item(id,uri,name,is_local,is_playable,external_ids,"
    "artists(name),album(name,release_date)))"
)


class SpotifyAuthError(RuntimeError):
    """The refresh token is dead (invalid_grant). Re-mint with scripts/spotify_auth.py."""


class SpotifyClient:
    def __init__(self, settings: Settings, http: httpx.Client | None = None):
        self._s = settings
        self._http = http or httpx.Client(timeout=30)
        self._token: str | None = None

    def _refresh_token(self) -> None:
        resp = self._http.post(
            TOKEN_URL,
            data={"grant_type": "refresh_token", "refresh_token": self._s.spotify_refresh_token},
            auth=(self._s.spotify_client_id, self._s.spotify_client_secret),
        )
        if resp.status_code == 400 and "invalid_grant" in resp.text:
            raise SpotifyAuthError(f"invalid_grant: {resp.json().get('error_description', '')}")
        resp.raise_for_status()
        self._token = resp.json()["access_token"]

    def _request(self, method: str, url: str, **kwargs):
        if self._token is None:
            self._refresh_token()
        refreshed = False
        while True:
            resp = self._http.request(method, url, headers={"Authorization": f"Bearer {self._token}"}, **kwargs)
            if resp.status_code == 401 and not refreshed:
                self._refresh_token()
                refreshed = True
                continue
            if resp.status_code == 429:
                wait = float(resp.headers.get("Retry-After", "1"))
                log.warning("spotify_rate_limited", retry_after=wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json() if resp.content else {}

    def _get(self, url, params=None):
        return self._request("GET", url, params=params)

    def _paginate(self, url: str, params: dict) -> list[dict]:
        items, body = [], self._get(url, params)
        while True:
            items.extend(body.get("items") or [])
            if not body.get("next"):
                return items
            body = self._get(body["next"])

    # reads
    def me(self) -> dict:
        return self._get(f"{API}/v1/me")

    def get_playlists(self) -> list[dict]:
        return self._paginate(f"{API}/v1/me/playlists", {"limit": 50})

    def get_playlist(self, playlist_id: str) -> dict:
        return self._get(f"{API}/v1/playlists/{playlist_id}")

    def get_playlist_items(self, playlist_id: str, market: str) -> list[dict]:
        return self._paginate(
            f"{API}/v1/playlists/{playlist_id}/items", {"limit": 50, "market": market, "fields": ITEM_FIELDS}
        )

    def get_liked(self, market: str) -> list[dict]:
        return self._paginate(f"{API}/v1/me/tracks", {"limit": 50, "market": market})

    def get_track(self, track_id: str, market: str) -> dict:
        return self._get(f"{API}/v1/tracks/{track_id}", {"market": market})

    def _search(self, q: str, market: str) -> list[dict]:
        body = self._get(f"{API}/v1/search", {"q": q, "type": "track", "limit": 10, "market": market})
        return ((body.get("tracks") or {}).get("items")) or []

    def search_isrc(self, isrc: str, market: str) -> list[dict]:
        return self._search(f"isrc:{isrc}", market)

    def search_track(self, title: str, artist: str, market: str) -> list[dict]:
        return self._search(f"track:{title} artist:{artist}", market)

    # writes
    def add_items(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            self._request("POST", f"{API}/v1/playlists/{playlist_id}/items", json={"uris": uris[i : i + 100]})

    def remove_items(self, playlist_id: str, uris: list[str]) -> None:
        for i in range(0, len(uris), 100):
            self._request("DELETE", f"{API}/v1/playlists/{playlist_id}/items", json={"uris": uris[i : i + 100]})

    def set_description(self, playlist_id: str, text: str) -> None:
        self._request("PUT", f"{API}/v1/playlists/{playlist_id}", json={"description": text[:300]})

    def like(self, uris: list[str]) -> None:
        for i in range(0, len(uris), 50):
            self._request("PUT", f"{API}/v1/me/library", params={"uris": ",".join(uris[i : i + 50])})
```

- [ ] **Step 4: Tests, lint, commit**

```bash
uv run pytest -v && just check
git add -A && git commit -m "Spotify client for the Development Mode endpoint set: items, library, search, writes

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

### Task 8: Model, mirror loading, live pull, R2 archive

**Files:**
- Create: `src/core/model.py`, `src/core/mirror.py`, `src/core/archive.py`, `tests/test_mirror.py`, `tests/test_archive.py`, `tests/fixtures/live_playlists.json`, `tests/fixtures/live_items.json`, `tests/fixtures/live_liked.json`

**Interfaces:**
- `model.py` dataclasses (all fields typed, `frozen=False`):
  - `Song(id, liked: int, liked_at: str | None, first_seen: str, spotify_ids: list[str], spotify_playable: int | None, first_year: int | None, deezer_genres: list[str], mb_tags: list[str], title: str | None, artists: list[str])`
  - `Playlist(id, name, kind: str, rule: dict | None, description: str | None, snapshot_id: str | None, pinned: int, expires_at: str | None)`
  - `Membership(playlist_id, isrc, spotify_track_id, added_at, deleted_at: str | None)` with property `id -> f"{playlist_id}:{isrc}"`
  - `Capture(isrc, from_kind)` (captured_by edges, read from `provenance`)
  - `Mirror(songs: dict[str, Song], playlists: dict[str, Playlist], memberships: dict[tuple[str, str], Membership], deleted_memberships: list[Membership], captures: set[tuple[str, str]])`
  - `LiveItem(isrc: str | None, track_id, uri, added_at, playable: bool, is_local: bool, name, artists: list[str])`
  - `LivePlaylist(id, name, description, snapshot_id, items: list[LiveItem] | None)` (`None` = skipped, snapshot unchanged)
  - `Live(playlists: dict[str, LivePlaylist], liked: dict[str, LiveItem], raw: dict)` (`raw` = every response body, for the archive)
  - `Action(kind: str, playlist_id: str | None = None, isrc: str | None = None, uri: str | None = None, text: str | None = None, row: dict | None = None, reason: str = "")` with `kind` in `like, add_item, remove_item, set_description, upsert_song, upsert_membership, delete_membership, upsert_playlist, delete_playlist, edge, flag`
- `mirror.load_mirror(hub) -> Mirror` pulls `songs`, `playlists`, `playlist_songs` (all columns above, `since=""`), and `provenance` filtered client-side to `to_kind='songs' AND rel='imported_from'`; soft-deleted rows go to `deleted_memberships` (memberships) or are dropped (others).
- `mirror.pull_live(spotify, market, me_id, mirror) -> Live`: owned playlists only (`owner.id == me_id`); a playlist whose `snapshot_id` equals the mirror's gets `items=None`; liked always pulled.
- `mirror.item_from_raw(raw: dict) -> LiveItem` maps a playlist item or liked item (`item` or `track` key) to `LiveItem`; ISRC upper-cased and validated against the pattern, else `None`.
- `archive.put(settings, key: str, data: bytes, http=None) -> None` PUTs `https://api.cloudflare.com/client/v4/accounts/{acct}/r2/buckets/{bucket}/objects/{key}` with `Authorization: Bearer <r2 token>`, `Content-Type: application/gzip`. `archive.key_for(now: datetime) -> str` = `raw/spotify-pull/<YYYY-MM-DD>T<HHMMSS>Z.json.gz`.

- [ ] **Step 1: Write the failing tests** (`tests/test_mirror.py`)

```python
import json
from pathlib import Path

from core import mirror
from core.model import Membership, Playlist, Song

FIX = Path(__file__).parent / "fixtures"
PLAYLISTS = json.loads((FIX / "live_playlists.json").read_text())
ITEMS = json.loads((FIX / "live_items.json").read_text())
LIKED = json.loads((FIX / "live_liked.json").read_text())


class FakeHub:
    def __init__(self, tables):
        self.tables = tables

    def pull(self, table, columns, since=""):
        return [{c: r.get(c) for c in columns} for r in self.tables.get(table, [])]


class FakeSpotify:
    def __init__(self):
        self.item_calls = []

    def get_playlists(self):
        return PLAYLISTS

    def get_playlist_items(self, pid, market):
        self.item_calls.append(pid)
        return ITEMS[pid]

    def get_liked(self, market):
        return LIKED


def test_load_mirror_splits_deleted_memberships_and_parses_json():
    hub = FakeHub({
        "songs": [{"id": "USUM71703861", "liked": 1, "liked_at": "t", "first_seen": "t", "spotify_ids": '["a","b"]',
                    "spotify_playable": 1, "first_year": 2017, "deezer_genres": '["Pop"]', "mb_tags": "[]",
                    "title": "x", "artists": '["y"]', "deleted_at": None}],
        "playlists": [{"id": "P", "name": "rap", "kind": "smart", "rule": '{"v":1}', "description": None,
                        "snapshot_id": "s1", "pinned": 1, "expires_at": None, "deleted_at": None}],
        "playlist_songs": [
            {"id": "P:USUM71703861", "playlist_id": "P", "isrc": "USUM71703861", "spotify_track_id": "a", "added_at": "t", "deleted_at": None},
            {"id": "P:GBBTV1101287", "playlist_id": "P", "isrc": "GBBTV1101287", "spotify_track_id": "c", "added_at": "t", "deleted_at": "t2"},
        ],
        "provenance": [
            {"id": "shazam:1:USUM71703861", "from_kind": "shazam", "to_kind": "songs", "to_ref": "USUM71703861", "rel": "imported_from", "deleted_at": None},
            {"id": "x", "from_kind": "photo", "to_kind": "places", "to_ref": "z", "rel": "evidence_of", "deleted_at": None},
        ],
    })
    m = mirror.load_mirror(hub)
    assert m.songs["USUM71703861"].spotify_ids == ["a", "b"]
    assert m.playlists["P"].rule == {"v": 1}
    assert list(m.memberships) == [("P", "USUM71703861")]
    assert m.deleted_memberships[0].isrc == "GBBTV1101287"
    assert m.captures == {("USUM71703861", "shazam")}


def test_item_from_raw_validates_isrc_and_local():
    raw = ITEMS["P1"][0]
    it = mirror.item_from_raw(raw)
    assert it.isrc == raw["item"]["external_ids"]["isrc"].upper()
    assert it.track_id == raw["item"]["id"] and it.added_at == raw["added_at"]
    local = {"added_at": "t", "item": {"id": None, "uri": "spotify:local:x", "is_local": True, "name": "n", "artists": [], "external_ids": {}}}
    assert mirror.item_from_raw(local).isrc is None and mirror.item_from_raw(local).is_local


def test_pull_live_skips_unchanged_snapshots_and_foreign_playlists():
    m = mirror.Mirror(songs={}, playlists={"P1": Playlist("P1", "a", "curated", None, None, PLAYLISTS[0]["snapshot_id"], 1, None)},
                      memberships={}, deleted_memberships=[], captures=set())
    sp = FakeSpotify()
    live = mirror.pull_live(sp, "US", "alexmiller", m)
    assert set(live.playlists) == {p["id"] for p in PLAYLISTS if p["owner"]["id"] == "alexmiller"}
    assert live.playlists["P1"].items is None and sp.item_calls == ["P2"]
    assert all(k for k in live.liked) and live.raw["liked"] == LIKED
```

Fixtures: `live_playlists.json` = two owned playlists (`P1` snapshot `s1`, `P2` snapshot `s2`, `owner.id` `alexmiller`) and one foreign (`owner.id` `someone`), shaped like `/v1/me/playlists` items (`id, name, description, snapshot_id, owner.id`). `live_items.json` = `{"P1": [...], "P2": [...]}` of `/v1/playlists/{id}/items` items with the `ITEM_FIELDS` shape from Task 7 (include one unplayable item and one without ISRC in `P2`). `live_liked.json` = three `/v1/me/tracks` items (`added_at`, `track.{id,uri,name,is_local,is_playable,external_ids.isrc,artists[].name,album.{name,release_date}}`). Capture them from the live account with `op run --env-file=.env.tpl -- curl ...` and trim; ISRCs must be valid.

`tests/test_archive.py`:

```python
import gzip
from datetime import datetime, timezone

import httpx

from core import archive


def test_put_uses_cf_r2_objects_api(settings):
    seen = {}

    def handler(req):
        seen["m"], seen["url"], seen["h"], seen["body"] = req.method, str(req.url), req.headers, req.content
        return httpx.Response(200, json={"success": True})

    archive.put(settings, "raw/spotify-pull/x.json.gz", gzip.compress(b"{}"), http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["m"] == "PUT"
    assert seen["url"] == "https://api.cloudflare.com/client/v4/accounts/acct/r2/buckets/bucket/objects/raw%2Fspotify-pull%2Fx.json.gz"
    assert seen["h"]["Authorization"] == "Bearer r2tok" and seen["h"]["Content-Type"] == "application/gzip"
    assert gzip.decompress(seen["body"]) == b"{}"


def test_key_for():
    assert archive.key_for(datetime(2026, 9, 8, 13, 5, 9, tzinfo=timezone.utc)) == "raw/spotify-pull/2026-09-08T130509Z.json.gz"
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement**

`src/core/model.py`:

```python
"""Plain dataclasses shared by mirror, reconcile, actions."""

from dataclasses import dataclass, field

ISRC_RE = r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$"


@dataclass
class Song:
    id: str
    liked: int
    liked_at: str | None
    first_seen: str
    spotify_ids: list[str] = field(default_factory=list)
    spotify_playable: int | None = None
    first_year: int | None = None
    deezer_genres: list[str] = field(default_factory=list)
    mb_tags: list[str] = field(default_factory=list)
    title: str | None = None
    artists: list[str] = field(default_factory=list)


@dataclass
class Playlist:
    id: str
    name: str
    kind: str
    rule: dict | None
    description: str | None
    snapshot_id: str | None
    pinned: int
    expires_at: str | None


@dataclass
class Membership:
    playlist_id: str
    isrc: str
    spotify_track_id: str
    added_at: str
    deleted_at: str | None = None

    @property
    def id(self) -> str:
        return f"{self.playlist_id}:{self.isrc}"


@dataclass
class Mirror:
    songs: dict[str, Song]
    playlists: dict[str, Playlist]
    memberships: dict[tuple[str, str], Membership]
    deleted_memberships: list[Membership]
    captures: set[tuple[str, str]]  # (isrc, from_kind)


@dataclass
class LiveItem:
    isrc: str | None
    track_id: str | None
    uri: str | None
    added_at: str
    playable: bool
    is_local: bool
    name: str | None
    artists: list[str]


@dataclass
class LivePlaylist:
    id: str
    name: str
    description: str | None
    snapshot_id: str | None
    items: list[LiveItem] | None


@dataclass
class Live:
    playlists: dict[str, LivePlaylist]
    liked: dict[str, LiveItem]
    raw: dict


@dataclass
class Action:
    kind: str
    playlist_id: str | None = None
    isrc: str | None = None
    uri: str | None = None
    text: str | None = None
    row: dict | None = None
    reason: str = ""
```

`src/core/mirror.py`:

```python
"""Load the life-data mirror and pull live Spotify state into plain dataclasses."""

import json
import re

from core.model import ISRC_RE, Live, LiveItem, LivePlaylist, Membership, Mirror, Playlist, Song

SONG_COLS = ["id", "liked", "liked_at", "first_seen", "spotify_ids", "spotify_playable", "first_year",
             "deezer_genres", "mb_tags", "title", "artists", "deleted_at"]
PLAYLIST_COLS = ["id", "name", "kind", "rule", "description", "snapshot_id", "pinned", "expires_at", "deleted_at"]
MEMBER_COLS = ["id", "playlist_id", "isrc", "spotify_track_id", "added_at", "deleted_at"]
PROV_COLS = ["id", "from_kind", "to_kind", "to_ref", "rel", "deleted_at"]


def _j(v, default):
    if v in (None, ""):
        return default
    return json.loads(v) if isinstance(v, str) else v


def load_mirror(hub) -> Mirror:
    songs = {
        r["id"]: Song(r["id"], int(r["liked"] or 0), r["liked_at"], r["first_seen"], _j(r["spotify_ids"], []),
                      r["spotify_playable"], r["first_year"], _j(r["deezer_genres"], []), _j(r["mb_tags"], []),
                      r["title"], _j(r["artists"], []))
        for r in hub.pull("songs", SONG_COLS) if not r.get("deleted_at")
    }
    playlists = {
        r["id"]: Playlist(r["id"], r["name"], r["kind"], _j(r["rule"], None), r["description"], r["snapshot_id"],
                          int(r["pinned"] or 0), r["expires_at"])
        for r in hub.pull("playlists", PLAYLIST_COLS) if not r.get("deleted_at")
    }
    memberships, deleted = {}, []
    for r in hub.pull("playlist_songs", MEMBER_COLS):
        m = Membership(r["playlist_id"], r["isrc"], r["spotify_track_id"], r["added_at"], r.get("deleted_at"))
        (deleted.append(m) if m.deleted_at else memberships.__setitem__((m.playlist_id, m.isrc), m))
    captures = {
        (r["to_ref"], r["from_kind"])
        for r in hub.pull("provenance", PROV_COLS)
        if r["to_kind"] == "songs" and r["rel"] == "imported_from" and not r.get("deleted_at")
    }
    return Mirror(songs, playlists, memberships, deleted, captures)


def item_from_raw(raw: dict) -> LiveItem:
    t = raw.get("item") or raw.get("track") or {}
    isrc = ((t.get("external_ids") or {}).get("isrc") or "").strip().upper()
    return LiveItem(
        isrc=isrc if re.match(ISRC_RE, isrc) else None,
        track_id=t.get("id"),
        uri=t.get("uri"),
        added_at=raw.get("added_at") or "",
        playable=bool(t.get("is_playable")),
        is_local=bool(t.get("is_local")),
        name=t.get("name"),
        artists=[a.get("name") for a in t.get("artists") or [] if a.get("name")],
    )


def pull_live(spotify, market: str, me_id: str, mirror: Mirror) -> Live:
    raw = {"playlists": [], "items": {}, "liked": []}
    playlists = {}
    for p in spotify.get_playlists():
        if (p.get("owner") or {}).get("id") != me_id:
            continue
        raw["playlists"].append(p)
        known = mirror.playlists.get(p["id"])
        if known and known.snapshot_id and known.snapshot_id == p.get("snapshot_id"):
            items = None
        else:
            body = spotify.get_playlist_items(p["id"], market)
            raw["items"][p["id"]] = body
            items = [item_from_raw(i) for i in body]
        playlists[p["id"]] = LivePlaylist(p["id"], p["name"], p.get("description"), p.get("snapshot_id"), items)
    liked_raw = spotify.get_liked(market)
    raw["liked"] = liked_raw
    liked = {}
    for i in liked_raw:
        it = item_from_raw(i)
        if it.isrc and it.isrc not in liked:
            liked[it.isrc] = it
    return Live(playlists, liked, raw)
```

`src/core/archive.py`:

```python
"""Archive the raw Spotify pull to R2 through Cloudflare's REST API (httpx only)."""

from datetime import datetime
from urllib.parse import quote

import httpx

from core.config import Settings


def key_for(now: datetime) -> str:
    return f"raw/spotify-pull/{now.strftime('%Y-%m-%dT%H%M%SZ')}.json.gz"


def put(settings: Settings, key: str, data: bytes, http: httpx.Client | None = None) -> None:
    url = (f"https://api.cloudflare.com/client/v4/accounts/{settings.r2_account_id}"
           f"/r2/buckets/{settings.r2_bucket}/objects/{quote(key, safe='')}")
    r = (http or httpx.Client(timeout=120)).put(
        url, content=data,
        headers={"Authorization": f"Bearer {settings.r2_api_token}", "Content-Type": "application/gzip"},
    )
    r.raise_for_status()
```

- [ ] **Step 4: Tests, lint, commit**

```bash
uv run pytest -v && just check
git add -A && git commit -m "Model, mirror loader, live pull with snapshot skipping, R2 archive

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

### Task 9: Rules - validation, SQL rendering, description rendering, evaluation

**Files:**
- Create: `src/core/rules.py`, `tests/test_rules.py`

**Interfaces:**
- `rules.RuleError(ValueError)`
- `rules.validate(rule: dict) -> None` - raises `RuleError` on: missing/wrong `v`, unknown keys, wrong value types (`*_any` lists of non-empty strings, `first_year` dict with only `lt`/`gte`/`between`, `captured_by` one of `shazam`/`playlist`/`like`, `liked_after` `YYYY-MM-DD`).
- `rules.to_sql(rule: dict, playlist_ids: dict[str, str]) -> str` - a boolean SQL expression over alias `s` (songs) using `json_each`, `EXISTS (SELECT 1 FROM playlist_songs ps WHERE ...)`, and `EXISTS (SELECT 1 FROM captures c WHERE ...)`; playlist names resolved through `playlist_ids` (name → id); unknown name raises `RuleError`.
- `rules.describe(rule: dict, synced: str) -> str` - `smart · genre: A, B · tags: x, y · year < 2000 · in: feel good · not in: 😴 · shazamed · liked after 2025-01-01 · synced 2026-09-08`, cut to 300 chars.
- `rules.evaluate(mirror, playlist_ids) -> dict[str, set[str]]` - for every `kind='smart'` playlist, the set of ISRCs matching its rule among `liked=1` songs, computed by loading the mirror into an in-memory sqlite3 db (`songs`, `playlist_songs` (non-deleted), `captures`) and running `SELECT id FROM songs s WHERE s.liked=1 AND (<to_sql>)`. This is the one implementation the reconciler and the `life check` invariant share.
- `rules.check_sql(playlists: list[Playlist], playlist_ids) -> str` - the generic invariant: `UNION ALL` over smart playlists of `SELECT ps.id FROM playlist_songs ps JOIN songs s ON s.id=ps.isrc WHERE ps.deleted_at IS NULL AND ps.playlist_id='<id>' AND NOT (s.liked=1 AND (<to_sql>))`, for `life rule set`.

- [ ] **Step 1: Write the failing tests** (`tests/test_rules.py`)

```python
import pytest

from core import rules
from core.model import Membership, Mirror, Playlist, Song

IDS = {"feel good": "PF", "😴": "PS"}


def song(isrc, liked=1, year=None, genres=(), tags=(), liked_at=None):
    return Song(isrc, liked, liked_at, "t", ["x"], 1, year, list(genres), list(tags), "t", ["a"])


def test_validate_accepts_full_rule():
    rules.validate({"v": 1, "deezer_genres_any": ["Rap/Hip Hop"], "mb_tags_any": ["house"],
                    "first_year": {"gte": 1990, "lt": 2000}, "in_playlist_any": ["feel good"],
                    "not_in_playlist": ["😴"], "captured_by": "shazam", "liked_after": "2025-01-01"})


@pytest.mark.parametrize("bad", [
    {}, {"v": 2}, {"v": 1, "genre": ["x"]}, {"v": 1, "deezer_genres_any": "Pop"},
    {"v": 1, "first_year": {"eq": 1999}}, {"v": 1, "first_year": {"between": [1999]}},
    {"v": 1, "captured_by": "radio"}, {"v": 1, "liked_after": "yesterday"}, {"v": 1, "mb_tags_any": []},
])
def test_validate_rejects(bad):
    with pytest.raises(rules.RuleError):
        rules.validate(bad)


def test_to_sql_unknown_playlist_name():
    with pytest.raises(rules.RuleError, match="unknown playlist"):
        rules.to_sql({"v": 1, "in_playlist_any": ["nope"]}, IDS)


def test_describe():
    r = {"v": 1, "deezer_genres_any": ["Rap/Hip Hop"], "first_year": {"lt": 2000}, "in_playlist_any": ["feel good"],
         "not_in_playlist": ["😴"], "captured_by": "shazam", "liked_after": "2025-01-01"}
    assert rules.describe(r, "2026-09-08") == (
        "smart · genre: Rap/Hip Hop · year < 2000 · in: feel good · not in: 😴 · shazamed · liked after 2025-01-01 · synced 2026-09-08"
    )
    assert rules.describe({"v": 1, "first_year": {"between": [1990, 1999]}}, "d") == "smart · year 1990-1999 · synced d"
    assert len(rules.describe({"v": 1, "mb_tags_any": ["x" * 400]}, "d")) == 300


def make_mirror():
    songs = {
        "A": song("A", year=1995, genres=["Rap/Hip Hop"]),
        "B": song("B", year=2005, genres=["Rap/Hip Hop"]),
        "C": song("C", liked=0, year=1990, genres=["Rap/Hip Hop"]),
        "D": song("D", year=1980, tags=["deep house", "house"], liked_at="2025-06-01T00:00:00.000Z"),
        "E": song("E", year=1980, tags=["house"], liked_at="2024-06-01T00:00:00.000Z"),
    }
    playlists = {
        "PF": Playlist("PF", "feel good", "curated", None, None, None, 1, None),
        "PS": Playlist("PS", "😴", "curated", None, None, None, 1, None),
        "R": Playlist("R", "rap 90s", "smart", {"v": 1, "deezer_genres_any": ["Rap/Hip Hop"], "first_year": {"lt": 2000}}, None, None, 1, None),
        "H": Playlist("H", "house", "smart", {"v": 1, "mb_tags_any": ["house"], "not_in_playlist": ["😴"], "liked_after": "2025-01-01"}, None, None, 1, None),
        "F": Playlist("F", "feel shaz", "smart", {"v": 1, "in_playlist_any": ["feel good"], "captured_by": "shazam"}, None, None, 1, None),
    }
    memberships = {("PF", "A"): Membership("PF", "A", "x", "t"), ("PS", "E"): Membership("PS", "E", "x", "t"),
                   ("PF", "D"): Membership("PF", "D", "x", "t")}
    return Mirror(songs, playlists, memberships, [], {("A", "shazam"), ("D", "like")})


def test_evaluate_all_predicates():
    out = rules.evaluate(make_mirror(), {"feel good": "PF", "😴": "PS"})
    assert out == {"R": {"A"}, "H": {"D"}, "F": {"A"}}


def test_check_sql_lists_mismatches_in_sqlite():
    import sqlite3

    m = make_mirror()
    m.memberships[("R", "B")] = Membership("R", "B", "x", "t")   # B is 2005: violates rap 90s
    m.memberships[("R", "A")] = Membership("R", "A", "x", "t")
    db = rules.load_sqlite(m)
    sql = rules.check_sql([p for p in m.playlists.values() if p.kind == "smart"], {"feel good": "PF", "😴": "PS"})
    assert {r[0] for r in db.execute(sql)} == {"R:B"}
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement `src/core/rules.py`**

```python
"""Smart playlist rules: validate, render to SQL, render to a description, evaluate.

One SQL renderer serves the reconciler (in-memory sqlite over the mirror) and
the estate's `life check` invariant (generated by `check_sql`).
"""

import re
import sqlite3

from core.model import Mirror, Playlist

KEYS = {"v", "deezer_genres_any", "mb_tags_any", "first_year", "in_playlist_any", "not_in_playlist",
        "captured_by", "liked_after"}
CAPTURE_KINDS = {"shazam", "playlist", "like"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RuleError(ValueError):
    pass


def _str_list(v, key):
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
        raise RuleError(f"{key}: non-empty list of strings required")


def validate(rule: dict) -> None:
    if not isinstance(rule, dict) or rule.get("v") != 1:
        raise RuleError("rule must be an object with v = 1")
    unknown = set(rule) - KEYS
    if unknown:
        raise RuleError(f"unknown keys: {sorted(unknown)}")
    for k in ("deezer_genres_any", "mb_tags_any", "in_playlist_any", "not_in_playlist"):
        if k in rule:
            _str_list(rule[k], k)
    if "first_year" in rule:
        fy = rule["first_year"]
        if not isinstance(fy, dict) or not fy or set(fy) - {"lt", "gte", "between"}:
            raise RuleError("first_year: object with lt / gte / between")
        for k in ("lt", "gte"):
            if k in fy and not isinstance(fy[k], int):
                raise RuleError(f"first_year.{k}: int")
        if "between" in fy and not (isinstance(fy["between"], list) and len(fy["between"]) == 2
                                    and all(isinstance(x, int) for x in fy["between"])):
            raise RuleError("first_year.between: [a, b]")
    if "captured_by" in rule and rule["captured_by"] not in CAPTURE_KINDS:
        raise RuleError(f"captured_by: one of {sorted(CAPTURE_KINDS)}")
    if "liked_after" in rule and not (isinstance(rule["liked_after"], str) and DATE_RE.match(rule["liked_after"])):
        raise RuleError("liked_after: YYYY-MM-DD")


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _pid(name: str, playlist_ids: dict[str, str]) -> str:
    if name not in playlist_ids:
        raise RuleError(f"unknown playlist: {name}")
    return _q(playlist_ids[name])


def to_sql(rule: dict, playlist_ids: dict[str, str]) -> str:
    validate(rule)
    parts = []
    if "deezer_genres_any" in rule:
        parts.append("EXISTS (SELECT 1 FROM json_each(s.deezer_genres) WHERE value IN (%s))"
                     % ",".join(_q(g) for g in rule["deezer_genres_any"]))
    if "mb_tags_any" in rule:
        parts.append("EXISTS (SELECT 1 FROM json_each(s.mb_tags) WHERE value IN (%s))"
                     % ",".join(_q(t.lower()) for t in rule["mb_tags_any"]))
    if "first_year" in rule:
        fy = rule["first_year"]
        if "lt" in fy:
            parts.append(f"s.first_year < {int(fy['lt'])}")
        if "gte" in fy:
            parts.append(f"s.first_year >= {int(fy['gte'])}")
        if "between" in fy:
            a, b = fy["between"]
            parts.append(f"s.first_year BETWEEN {int(a)} AND {int(b)}")
    if "in_playlist_any" in rule:
        parts.append("EXISTS (SELECT 1 FROM playlist_songs ps WHERE ps.isrc = s.id AND ps.deleted_at IS NULL AND ps.playlist_id IN (%s))"
                     % ",".join(_pid(n, playlist_ids) for n in rule["in_playlist_any"]))
    if "not_in_playlist" in rule:
        parts.append("NOT EXISTS (SELECT 1 FROM playlist_songs ps WHERE ps.isrc = s.id AND ps.deleted_at IS NULL AND ps.playlist_id IN (%s))"
                     % ",".join(_pid(n, playlist_ids) for n in rule["not_in_playlist"]))
    if "captured_by" in rule:
        parts.append(f"EXISTS (SELECT 1 FROM captures c WHERE c.isrc = s.id AND c.from_kind = {_q(rule['captured_by'])})")
    if "liked_after" in rule:
        parts.append(f"s.liked_at >= {_q(rule['liked_after'])}")
    return "(" + " AND ".join(parts) + ")" if parts else "(1=1)"


def describe(rule: dict, synced: str) -> str:
    seg = ["smart"]
    if "deezer_genres_any" in rule:
        seg.append("genre: " + ", ".join(rule["deezer_genres_any"]))
    if "mb_tags_any" in rule:
        seg.append("tags: " + ", ".join(rule["mb_tags_any"]))
    if "first_year" in rule:
        fy = rule["first_year"]
        if "between" in fy:
            seg.append(f"year {fy['between'][0]}-{fy['between'][1]}")
        else:
            if "gte" in fy:
                seg.append(f"year >= {fy['gte']}")
            if "lt" in fy:
                seg.append(f"year < {fy['lt']}")
    if "in_playlist_any" in rule:
        seg.append("in: " + ", ".join(rule["in_playlist_any"]))
    if "not_in_playlist" in rule:
        seg.append("not in: " + ", ".join(rule["not_in_playlist"]))
    if "captured_by" in rule:
        seg.append({"shazam": "shazamed", "playlist": "from playlists", "like": "from likes"}[rule["captured_by"]])
    if "liked_after" in rule:
        seg.append(f"liked after {rule['liked_after']}")
    seg.append(f"synced {synced}")
    return " · ".join(seg)[:300]


def load_sqlite(mirror: Mirror) -> sqlite3.Connection:
    import json

    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE songs (id TEXT PRIMARY KEY, liked INT, liked_at TEXT, first_year INT, deezer_genres TEXT, mb_tags TEXT);
        CREATE TABLE playlist_songs (id TEXT PRIMARY KEY, playlist_id TEXT, isrc TEXT, deleted_at TEXT);
        CREATE TABLE captures (isrc TEXT, from_kind TEXT);
    """)
    db.executemany("INSERT INTO songs VALUES (?,?,?,?,?,?)",
                   [(s.id, s.liked, s.liked_at, s.first_year, json.dumps(s.deezer_genres), json.dumps(s.mb_tags))
                    for s in mirror.songs.values()])
    db.executemany("INSERT INTO playlist_songs VALUES (?,?,?,NULL)",
                   [(m.id, m.playlist_id, m.isrc) for m in mirror.memberships.values()])
    db.executemany("INSERT INTO captures VALUES (?,?)", list(mirror.captures))
    return db


def evaluate(mirror: Mirror, playlist_ids: dict[str, str]) -> dict[str, set[str]]:
    db = load_sqlite(mirror)
    out = {}
    for p in mirror.playlists.values():
        if p.kind != "smart" or not p.rule:
            continue
        sql = f"SELECT id FROM songs s WHERE s.liked = 1 AND {to_sql(p.rule, playlist_ids)}"
        out[p.id] = {r[0] for r in db.execute(sql)}
    return out


def check_sql(smart: list[Playlist], playlist_ids: dict[str, str]) -> str:
    parts = [
        f"SELECT ps.id FROM playlist_songs ps JOIN songs s ON s.id = ps.isrc WHERE ps.deleted_at IS NULL "
        f"AND ps.playlist_id = {_q(p.id)} AND NOT (s.liked = 1 AND {to_sql(p.rule, playlist_ids)})"
        for p in smart if p.rule
    ]
    return " UNION ALL ".join(parts) if parts else "SELECT id FROM playlist_songs WHERE 0"
```

- [ ] **Step 4: Tests, lint, commit**

```bash
uv run pytest -v && just check
git add -A && git commit -m "Rules: validation, one SQL renderer for reconcile and life check, description rendering

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

- [ ] **Step 5: Register the generic invariant in the estate.** `check_sql` references a `captures` table that only exists in the in-memory db; the estate version uses `provenance` instead. Generate it with the playlist names from the live estate and register it (rerun whenever a smart playlist is added; `scripts/migrate.py --step rules` in Task 14 wraps this):

```bash
uv run python -c "
from core import rules
from core.model import Playlist
import json, subprocess
rows = json.loads(subprocess.check_output(['life','sql',\"SELECT id,name,kind,rule FROM playlists WHERE deleted_at IS NULL\"]))
ids = {r['name']: r['id'] for r in rows}
smart = [Playlist(r['id'], r['name'], r['kind'], json.loads(r['rule']), None, None, 1, None) for r in rows if r['kind']=='smart']
sql = rules.check_sql(smart, ids).replace('FROM captures c WHERE c.isrc = s.id AND c.from_kind', \"FROM provenance c WHERE c.to_kind='songs' AND c.rel='imported_from' AND c.deleted_at IS NULL AND c.to_ref = s.id AND c.from_kind\")
subprocess.run(['life','rule','set','smart-songs-match-rule','--scope','table','--tbl','playlist_songs','--kind','invariant','--text','Every song in a smart playlist matches its rule (reconciler bug if violated).','--sql',sql], check=True)
"
```

### Task 10: The reconciler - a pure diff of mirror vs live into actions

**Files:**
- Create: `src/core/reconcile.py`, `tests/test_reconcile.py`

**Interfaces:**
- `reconcile.plan(mirror: Mirror, live: Live, now: datetime, inbox_cap: int = 100, undo_days: int = 7, today: str | None = None) -> list[Action]` - pure; never touches the network. Every row of spec §5.3 is one branch. Ordering of the returned list is the apply order: `like` → `add_item` → `remove_item` → `set_description` → `delete_playlist` → hub rows (`upsert_song`, `upsert_playlist`, `upsert_membership`, `delete_membership`, `edge`) → `flag`.
- Helper `reconcile.preferred_uri(song: Song) -> str | None` = `spotify:track:<spotify_ids[0]>`.
- Semantics that the tests pin down (each is a §5.3 row):
  1. **New song anywhere** (isrc not in mirror.songs): `upsert_song` with `liked`, `liked_at`, `first_seen=now`; `edge` `imported_from` with `from_kind` `like` (if only liked) or `playlist` (`from_ref` = the playlist id, `detail.created_row=1`).
  2. **Liked changed** live vs mirror: `upsert_song` with new `liked`/`liked_at`.
  3. **Added to curated while unliked** (live: in curated playlist, not liked; mirror: not in that playlist): `like` + `upsert_song liked=1` + `edge` `imported_from` `playlist` (if new).
  4. **Un-heart** (mirror liked=1, live not liked, in ≥1 non-inbox playlist): `remove_item` from every curated and smart playlist it is in + `delete_membership` for each; smart recompute covers the rest. Tie with rule 3 in the same run: un-heart wins (assert no `like` action for that isrc).
  5. **Undo**: live liked=1, mirror liked=0, `deleted_memberships` for curated playlists with `deleted_at >= now - undo_days`: `add_item` back + `upsert_membership`.
  6. **Inbox overflow**: inbox items sorted by `added_at` ascending, everything beyond `inbox_cap` from the oldest → `remove_item` + `delete_membership`. Nothing else ever touches the inbox.
  7. **Smart materialization**: for each smart playlist with `items is not None` (or newly smart), desired = `rules.evaluate`; `add_item` for desired − actual (uri = `preferred_uri`, skip and flag when a song has no spotify id), `remove_item` for actual − desired with reason `"removed by rule"`, `set_description` when `rules.describe(rule, today)` differs from live description **ignoring the trailing `· synced <date>` segment** (so an unchanged rule does not rewrite the description daily).
  8. **Unplayable**: a live item with `playable=False` whose song has a playable alternative id (`spotify_playable=1` and `spotify_ids[0] != item.track_id`) → `remove_item` old uri + `add_item` preferred uri + `upsert_membership`; no alternative → `flag`.
  9. **Duplicate ISRC in a playlist**: keep the earliest `added_at`, `remove_item` the others.
  10. **Ephemeral expiry**: smart, `pinned=0`, `expires_at < now` → `delete_playlist` + `upsert_playlist` with `deleted_at=now` (soft delete, via the row).
  11. **Mirror upkeep**: for every pulled playlist, `upsert_playlist` (name, description, snapshot_id, track_count, last_reconciled); memberships upserted for every live item with an ISRC; a mirror membership absent from the live items (and not removed by the rules above) → `delete_membership`. Skipped playlists (`items is None`) produce no membership changes.
  12. **No ISRC / local**: counted into one `flag` action per run listing them (`reason="no_isrc"`), never a song row.

- [ ] **Step 1: Write the failing tests** (`tests/test_reconcile.py`). A builder keeps the cases short:

```python
from datetime import datetime, timedelta, timezone

from core import reconcile
from core.model import Live, LiveItem, LivePlaylist, Membership, Mirror, Playlist, Song

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)
T = "2026-09-01T00:00:00.000Z"


def song(isrc, liked=1, ids=("t" + isrc,), playable=1, year=2010, genres=("Pop",)):
    return Song(isrc, liked, T if liked else None, T, list(ids), playable, year, list(genres), [], "n", ["a"])


def item(isrc, track_id=None, added=T, playable=True):
    tid = track_id or ("t" + isrc if isrc else None)
    return LiveItem(isrc, tid, f"spotify:track:{tid}" if tid else "spotify:local:x", added, playable, isrc is None, "n", ["a"])


def pl(pid, name, kind, rule=None, pinned=1, expires=None):
    return Playlist(pid, name, kind, rule, None, "snap", pinned, expires)


def live_pl(pid, name, items, desc=None):
    return LivePlaylist(pid, name, desc, "snap2", items)


def kinds(actions, kind):
    return [a for a in actions if a.kind == kind]


def base():
    songs = {"A": song("A"), "B": song("B"), "C": song("C", liked=0)}
    playlists = {"IN": pl("IN", "new songs", "inbox"), "CU": pl("CU", "feel good", "curated"),
                 "SM": pl("SM", "pop", "smart", {"v": 1, "deezer_genres_any": ["Pop"]})}
    memberships = {("CU", "A"): Membership("CU", "A", "tA", T), ("SM", "A"): Membership("SM", "A", "tA", T),
                   ("SM", "B"): Membership("SM", "B", "tB", T)}
    return Mirror(songs, playlists, memberships, [], set())


def test_added_to_curated_while_unliked_gets_liked_and_edge():
    m = base()
    live = Live({"CU": live_pl("CU", "feel good", [item("A"), item("C")]), "SM": live_pl("SM", "pop", [item("A"), item("B")]),
                 "IN": live_pl("IN", "new songs", [])}, {"A": item("A"), "B": item("B")}, {})
    acts = reconcile.plan(m, live, NOW)
    assert [a.uri for a in kinds(acts, "like")] == ["spotify:track:tC"]
    assert any(a.kind == "upsert_song" and a.row["id"] == "C" and a.row["liked"] == 1 for a in acts)
    assert any(a.kind == "edge" and a.row["to_ref"] == "C" and a.row["from_kind"] == "playlist" and a.row["from_ref"] == "CU" for a in acts)


def test_unheart_removes_from_curated_and_smart_and_wins_tie():
    m = base()
    live = Live({"CU": live_pl("CU", "feel good", [item("A")]), "SM": live_pl("SM", "pop", [item("A"), item("B")]),
                 "IN": live_pl("IN", "new songs", [item("A")])}, {"B": item("B")}, {})
    acts = reconcile.plan(m, live, NOW)
    removed = {(a.playlist_id, a.uri) for a in kinds(acts, "remove_item")}
    assert removed == {("CU", "spotify:track:tA"), ("SM", "spotify:track:tA")}
    assert not kinds(acts, "like")
    assert {a.row["id"] for a in kinds(acts, "delete_membership")} == {"CU:A", "SM:A"}


def test_undo_restores_curated_within_window():
    m = base()
    m.songs["A"].liked = 0
    m.memberships.pop(("CU", "A"))
    m.deleted_memberships.append(Membership("CU", "A", "tA", T, deleted_at=(NOW - timedelta(days=2)).isoformat()))
    live = Live({"CU": live_pl("CU", "feel good", []), "SM": live_pl("SM", "pop", [item("B")]), "IN": live_pl("IN", "new songs", [])},
                {"A": item("A"), "B": item("B")}, {})
    acts = reconcile.plan(m, live, NOW)
    assert ("CU", "spotify:track:tA") in {(a.playlist_id, a.uri) for a in kinds(acts, "add_item")}
    m.deleted_memberships[0].deleted_at = (NOW - timedelta(days=9)).isoformat()
    assert ("CU", "spotify:track:tA") not in {(a.playlist_id, a.uri) for a in kinds(reconcile.plan(m, live, NOW), "add_item")}


def test_inbox_fifo_and_exemption():
    m = base()
    items = [item(f"Z{i:02d}", added=f"2026-08-{i + 1:02d}T00:00:00.000Z") for i in range(3)]
    live = Live({"IN": live_pl("IN", "new songs", items), "CU": live_pl("CU", "feel good", [item("A")]),
                 "SM": live_pl("SM", "pop", [item("A"), item("B")])}, {"A": item("A"), "B": item("B")}, {})
    acts = reconcile.plan(m, live, NOW, inbox_cap=2)
    assert [a.uri for a in kinds(acts, "remove_item") if a.playlist_id == "IN"] == ["spotify:track:tZ00"]
    assert not [a for a in kinds(acts, "like")]  # unliked inbox songs are never liked


def test_smart_materialization_and_description():
    m = base()
    m.songs["D"] = song("D", genres=["Pop"])
    m.songs["B"].deezer_genres = ["Rock"]
    live = Live({"SM": live_pl("SM", "pop", [item("A"), item("B")], desc="smart · genre: Pop · synced 2026-09-01"),
                 "CU": live_pl("CU", "feel good", [item("A")]), "IN": live_pl("IN", "new songs", [])},
                {"A": item("A"), "B": item("B"), "D": item("D")}, {})
    acts = reconcile.plan(m, live, NOW, today="2026-09-08")
    assert [a.uri for a in kinds(acts, "add_item") if a.playlist_id == "SM"] == ["spotify:track:tD"]
    assert [a.uri for a in kinds(acts, "remove_item") if a.playlist_id == "SM"] == ["spotify:track:tB"]
    assert not kinds(acts, "set_description")  # rule unchanged: no daily rewrite
    m.playlists["SM"].rule = {"v": 1, "deezer_genres_any": ["Pop"], "first_year": {"lt": 2020}}
    acts = reconcile.plan(m, live, NOW, today="2026-09-08")
    assert kinds(acts, "set_description")[0].text == "smart · genre: Pop · year < 2020 · synced 2026-09-08"


def test_unplayable_relink_or_flag():
    m = base()
    m.songs["A"].spotify_ids = ["tA2", "tA"]
    m.songs["B"].spotify_playable = 0
    live = Live({"CU": live_pl("CU", "feel good", [item("A", playable=False), item("B", playable=False)]),
                 "SM": live_pl("SM", "pop", []), "IN": live_pl("IN", "new songs", [])}, {"A": item("A"), "B": item("B")}, {})
    acts = reconcile.plan(m, live, NOW)
    assert ("CU", "spotify:track:tA") in {(a.playlist_id, a.uri) for a in kinds(acts, "remove_item")}
    assert ("CU", "spotify:track:tA2") in {(a.playlist_id, a.uri) for a in kinds(acts, "add_item")}
    assert any(a.kind == "flag" and "B" in a.text for a in acts)


def test_duplicate_isrc_keeps_earliest():
    m = base()
    live = Live({"CU": live_pl("CU", "feel good", [item("A", "tA", added="2026-08-02T00:00:00.000Z"), item("A", "tA9", added="2026-08-01T00:00:00.000Z")]),
                 "SM": live_pl("SM", "pop", []), "IN": live_pl("IN", "new songs", [])}, {"A": item("A")}, {})
    acts = reconcile.plan(m, live, NOW)
    assert [a.uri for a in kinds(acts, "remove_item") if a.playlist_id == "CU"] == ["spotify:track:tA"]


def test_ephemeral_expiry_and_skipped_playlists():
    m = base()
    m.playlists["SM"].pinned, m.playlists["SM"].expires_at = 0, (NOW - timedelta(hours=1)).isoformat()
    live = Live({"SM": live_pl("SM", "pop", None), "CU": live_pl("CU", "feel good", None), "IN": live_pl("IN", "new songs", None)},
                {"A": item("A"), "B": item("B")}, {})
    acts = reconcile.plan(m, live, NOW)
    assert [a.playlist_id for a in kinds(acts, "delete_playlist")] == ["SM"]
    assert not kinds(acts, "remove_item") and not kinds(acts, "delete_membership")


def test_no_isrc_items_are_one_flag_not_rows():
    m = base()
    live = Live({"CU": live_pl("CU", "feel good", [item(None)]), "SM": live_pl("SM", "pop", []), "IN": live_pl("IN", "new songs", [])},
                {"A": item("A"), "B": item("B")}, {})
    acts = reconcile.plan(m, live, NOW)
    flags = [a for a in kinds(acts, "flag") if a.reason == "no_isrc"]
    assert len(flags) == 1 and not any(a.kind == "upsert_song" and a.row["id"] is None for a in acts)


def test_apply_order():
    m = base()
    m.songs["B"].deezer_genres = ["Rock"]
    live = Live({"CU": live_pl("CU", "feel good", [item("A"), item("C")]), "SM": live_pl("SM", "pop", [item("A"), item("B")]),
                 "IN": live_pl("IN", "new songs", [])}, {"A": item("A"), "B": item("B")}, {})
    order = [a.kind for a in reconcile.plan(m, live, NOW)]
    rank = {k: i for i, k in enumerate(reconcile.ORDER)}
    assert order == sorted(order, key=lambda k: rank[k])
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement `src/core/reconcile.py`**

```python
"""Pure reconcile: (mirror, live) -> ordered actions. No I/O. Spec section 5.3."""

from datetime import datetime, timedelta

from core import rules
from core.model import Action, Live, Mirror, Song

ORDER = ["like", "add_item", "remove_item", "set_description", "delete_playlist",
         "upsert_song", "upsert_playlist", "upsert_membership", "delete_membership", "edge", "flag"]


def preferred_uri(song: Song) -> str | None:
    return f"spotify:track:{song.spotify_ids[0]}" if song.spotify_ids else None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _strip_synced(desc: str | None) -> str:
    return (desc or "").split(" · synced ")[0]


def plan(mirror: Mirror, live: Live, now: datetime, inbox_cap: int = 100, undo_days: int = 7, today: str | None = None) -> list[Action]:
    today = today or now.date().isoformat()
    now_s = _iso(now)
    acts: list[Action] = []
    names = {p.name: p.id for p in mirror.playlists.values()}
    names.update({p.name: p.id for p in live.playlists.values()})
    kind_of = {pid: p.kind for pid, p in mirror.playlists.items()}
    inbox_ids = {pid for pid, k in kind_of.items() if k == "inbox"}
    liked_now = set(live.liked)
    liked_before = {s.id for s in mirror.songs.values() if s.liked}
    no_isrc: list[str] = []
    flags: list[str] = []

    # live membership index: (pid, isrc) -> best item (earliest added), plus duplicates
    actual: dict[tuple[str, str], object] = {}
    for lp in live.playlists.values():
        if lp.items is None:
            continue
        for it in lp.items:
            if not it.isrc:
                no_isrc.append(f"{lp.name}: {it.name} ({'local file' if it.is_local else 'no ISRC'})")
                continue
            key = (lp.id, it.isrc)
            if key not in actual:
                actual[key] = it
            else:  # duplicate ISRC: keep the earliest added_at, drop the other (rule 9)
                keep, drop = sorted([actual[key], it], key=lambda x: x.added_at)
                actual[key] = keep
                acts.append(Action("remove_item", playlist_id=lp.id, isrc=it.isrc, uri=drop.uri, reason="duplicate isrc"))

    # songs: new rows + liked transitions (rules 1, 2)
    seen_isrcs = liked_now | {isrc for (_, isrc) in actual}
    for isrc in sorted(seen_isrcs):
        li = live.liked.get(isrc)
        liked = 1 if li else 0
        liked_at = li.added_at if li else None
        s = mirror.songs.get(isrc)
        if s is None:
            acts.append(Action("upsert_song", isrc=isrc, row={"id": isrc, "liked": liked, "liked_at": liked_at, "first_seen": now_s}))
            src = next(((pid, it) for (pid, i), it in actual.items() if i == isrc and pid not in inbox_ids), None)
            from_kind, from_ref = ("playlist", src[0]) if src else ("like", "liked") if li else ("playlist", next(pid for (pid, i) in actual if i == isrc))
            acts.append(Action("edge", isrc=isrc, row={
                "id": f"{from_kind}:{from_ref}:{isrc}", "from_kind": from_kind, "from_ref": from_ref, "to_kind": "songs",
                "to_ref": isrc, "rel": "imported_from", "asserted_by": "music-sync", "detail": {"created_row": 1}}))
        elif (s.liked, s.liked_at) != (liked, liked_at) and isrc not in (liked_before - liked_now):
            acts.append(Action("upsert_song", isrc=isrc, row={"id": isrc, "liked": liked, "liked_at": liked_at}))

    # un-heart (rule 4): remove from every non-inbox playlist it is in
    unhearted = liked_before - liked_now
    for isrc in sorted(unhearted):
        acts.append(Action("upsert_song", isrc=isrc, row={"id": isrc, "liked": 0, "liked_at": None}))
        for (pid, i), it in list(actual.items()):
            if i == isrc and pid not in inbox_ids:
                acts.append(Action("remove_item", playlist_id=pid, isrc=isrc, uri=it.uri, reason="un-hearted"))
                acts.append(Action("delete_membership", playlist_id=pid, isrc=isrc, row={"id": f"{pid}:{isrc}", "deleted_at": now_s}))
                actual.pop((pid, i))

    # added to curated while unliked (rule 3): like it. Tie with un-heart: un-heart won above.
    to_like: dict[str, str] = {}
    for (pid, isrc), it in actual.items():
        if kind_of.get(pid) == "curated" and isrc not in liked_now and isrc not in unhearted:
            if (pid, isrc) not in mirror.memberships:
                to_like[isrc] = it.uri
    for isrc, uri in sorted(to_like.items()):
        acts.append(Action("like", isrc=isrc, uri=uri, reason="in curated playlist"))
        acts.append(Action("upsert_song", isrc=isrc, row={"id": isrc, "liked": 1, "liked_at": now_s}))
    liked_effective = (liked_now | set(to_like)) - unhearted

    # undo (rule 5)
    cutoff = _iso(now - timedelta(days=undo_days))
    for m in mirror.deleted_memberships:
        s = mirror.songs.get(m.isrc)
        if (m.isrc in liked_now and s and not s.liked and kind_of.get(m.playlist_id) == "curated"
                and (m.deleted_at or "") >= cutoff and (m.playlist_id, m.isrc) not in actual and s.spotify_ids):
            uri = preferred_uri(s)
            acts.append(Action("add_item", playlist_id=m.playlist_id, isrc=m.isrc, uri=uri, reason="undo un-heart"))
            acts.append(Action("upsert_membership", playlist_id=m.playlist_id, isrc=m.isrc, row={
                "id": m.id, "playlist_id": m.playlist_id, "isrc": m.isrc, "spotify_track_id": uri.split(":")[-1],
                "added_at": now_s, "deleted_at": None}))

    # inbox FIFO (rule 6)
    for pid in inbox_ids:
        lp = live.playlists.get(pid)
        if not lp or lp.items is None:
            continue
        with_isrc = sorted([it for it in lp.items if it.isrc], key=lambda x: x.added_at)
        for it in with_isrc[: max(0, len(with_isrc) - inbox_cap)]:
            acts.append(Action("remove_item", playlist_id=pid, isrc=it.isrc, uri=it.uri, reason="inbox overflow"))
            acts.append(Action("delete_membership", playlist_id=pid, isrc=it.isrc, row={"id": f"{pid}:{it.isrc}", "deleted_at": now_s}))
            actual.pop((pid, it.isrc), None)

    # smart materialization (rule 7), on a mirror view that reflects this run's liked state
    view = Mirror(dict(mirror.songs), mirror.playlists, dict(mirror.memberships), [], mirror.captures)
    for isrc in liked_effective:
        if isrc in view.songs:
            view.songs[isrc].liked = 1
    for isrc in unhearted:
        if isrc in view.songs:
            view.songs[isrc].liked = 0
    desired = rules.evaluate(view, names)
    for pid, want in desired.items():
        lp = live.playlists.get(pid)
        p = mirror.playlists[pid]
        if lp is None or lp.items is None:
            continue
        if p.pinned == 0 and p.expires_at and p.expires_at < now_s:
            continue
        have = {isrc for (q, isrc) in actual if q == pid}
        for isrc in sorted(want - have):
            uri = preferred_uri(mirror.songs[isrc]) if isrc in mirror.songs else None
            if not uri:
                flags.append(f"{p.name}: {isrc} has no Spotify id yet")
                continue
            acts.append(Action("add_item", playlist_id=pid, isrc=isrc, uri=uri, reason="matches rule"))
            acts.append(Action("upsert_membership", playlist_id=pid, isrc=isrc, row={
                "id": f"{pid}:{isrc}", "playlist_id": pid, "isrc": isrc, "spotify_track_id": uri.split(":")[-1],
                "added_at": now_s, "deleted_at": None}))
        for isrc in sorted(have - want):
            acts.append(Action("remove_item", playlist_id=pid, isrc=isrc, uri=actual[(pid, isrc)].uri, reason="removed by rule"))
            acts.append(Action("delete_membership", playlist_id=pid, isrc=isrc, row={"id": f"{pid}:{isrc}", "deleted_at": now_s}))
            actual.pop((pid, isrc))
        text = rules.describe(p.rule, today)
        if _strip_synced(text) != _strip_synced(lp.description):
            acts.append(Action("set_description", playlist_id=pid, text=text))

    # unplayable relink (rule 8)
    for (pid, isrc), it in list(actual.items()):
        if it.playable:
            continue
        s = mirror.songs.get(isrc)
        alt = s.spotify_ids[0] if s and s.spotify_playable and s.spotify_ids and s.spotify_ids[0] != it.track_id else None
        if alt:
            acts.append(Action("remove_item", playlist_id=pid, isrc=isrc, uri=it.uri, reason="unplayable, relinking"))
            acts.append(Action("add_item", playlist_id=pid, isrc=isrc, uri=f"spotify:track:{alt}", reason="relinked"))
            acts.append(Action("upsert_membership", playlist_id=pid, isrc=isrc, row={
                "id": f"{pid}:{isrc}", "playlist_id": pid, "isrc": isrc, "spotify_track_id": alt, "added_at": it.added_at, "deleted_at": None}))
        else:
            flags.append(f"{mirror.playlists[pid].name if pid in mirror.playlists else pid}: {it.name} [{isrc}] unplayable, no alternative")

    # ephemeral expiry (rule 10)
    for p in mirror.playlists.values():
        if p.kind == "smart" and p.pinned == 0 and p.expires_at and p.expires_at < now_s and p.id in live.playlists:
            acts.append(Action("delete_playlist", playlist_id=p.id, reason="ephemeral expired"))
            acts.append(Action("upsert_playlist", playlist_id=p.id, row={"id": p.id, "deleted_at": now_s}))

    # mirror upkeep (rule 11)
    for lp in live.playlists.values():
        row = {"id": lp.id, "name": lp.name, "description": lp.description, "snapshot_id": lp.snapshot_id, "last_reconciled": now_s}
        if lp.id not in mirror.playlists:
            row.update({"kind": "curated", "pinned": 1})
        if lp.items is not None:
            row["track_count"] = len(lp.items)
        acts.append(Action("upsert_playlist", playlist_id=lp.id, row=row))
        if lp.items is None:
            continue
        for (pid, isrc), it in actual.items():
            if pid != lp.id:
                continue
            m = mirror.memberships.get((pid, isrc))
            if m is None or m.spotify_track_id != it.track_id:
                acts.append(Action("upsert_membership", playlist_id=pid, isrc=isrc, row={
                    "id": f"{pid}:{isrc}", "playlist_id": pid, "isrc": isrc, "spotify_track_id": it.track_id,
                    "added_at": it.added_at, "deleted_at": None}))
        gone = {isrc for (pid, isrc) in mirror.memberships if pid == lp.id} - {isrc for (pid, isrc) in actual if pid == lp.id}
        already = {a.row["id"] for a in acts if a.kind == "delete_membership"}
        for isrc in sorted(gone):
            if f"{lp.id}:{isrc}" not in already:
                acts.append(Action("delete_membership", playlist_id=lp.id, isrc=isrc, row={"id": f"{lp.id}:{isrc}", "deleted_at": now_s}))

    if no_isrc:
        acts.append(Action("flag", text="Items without ISRC (skipped): " + "; ".join(sorted(set(no_isrc))), reason="no_isrc"))
    for f in flags:
        acts.append(Action("flag", text=f, reason="attention"))
    rank = {k: i for i, k in enumerate(ORDER)}
    return sorted(acts, key=lambda a: rank[a.kind])
```

Ponytail note: the smart-materialization view copies `songs` dicts but mutates `Song` objects in place for `liked`; that is intentional (this run's truth) but means `plan` mutates its `mirror` argument's songs. Add `# ponytail: plan mutates mirror.songs[*].liked; deep-copy if plan is ever called twice on one mirror` above that block.

- [ ] **Step 4: Run tests, iterate until green** (`uv run pytest tests/test_reconcile.py -v`). Then the mutation check: flip `>= cutoff` to `<= cutoff` in the undo branch and confirm `test_undo_restores_curated_within_window` fails; revert.

- [ ] **Step 5: Lint, commit**

```bash
uv run pytest -v && just check
git add -A && git commit -m "Reconciler: pure diff of mirror vs live into ordered actions

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

### Task 11: Apply actions, file flags, the run glue

**Files:**
- Create: `src/core/actions.py`, `src/core/flags.py`, `src/core/run.py`, `tests/test_actions.py`, `tests/test_flags.py`, `tests/test_run.py`

**Interfaces:**
- `actions.RunLog` dataclass: `applied: dict[str, int]` (count per kind), `skipped: list[str]`, `flags: list[str]`, `dry_run: bool`, `errors: list[str]`; method `summary() -> str` (one line per non-zero kind plus flags).
- `actions.apply(actions: list[Action], spotify, hub, dry_run: bool) -> RunLog` - groups Spotify writes per playlist (`add_item`/`remove_item` batched per playlist into one client call each; `like` batched into one), then pushes hub rows grouped per table (`upsert_song`+`upsert_playlist`+`upsert_membership`+`delete_membership`+`edge` → `songs`, `playlists`, `playlist_songs`, `provenance`), then collects `flag` texts. In dry-run nothing is called; counts are still filled. Spotify errors on one playlist are caught, logged into `errors`, and do not stop the other playlists; a hub `HubError` is appended to `errors` and re-raised after the Spotify writes finished (spec: partial runs get flagged).
- `flags.file(settings, http, flags: list[str], errors: list[str], today: str) -> str | None` - no flags and no errors → `None`; otherwise finds an open task titled `Music Sync flags` (Notion query on the Tasks data source: `Name` `equals` that title AND `Status` `does_not_equal` Completed, first result) and appends the lines to its `Notes` rich_text (PATCH), else creates one with `Status` `To Do`, `Priority` `High`, `Due Date` `today`, `Tags` `[Chore]`, `Project` relation `settings.notion_project_page_id`, `Notes` = the lines. Returns the page id.
- `run.reconcile(settings, dry_run: bool = False, now: datetime | None = None, spotify=None, hub=None, http=None) -> RunLog` - the glue: `SpotifyClient` → `me()` → `load_mirror` → `pull_live` → archive the raw pull (gzip of `json.dumps(live.raw)`, skipped in dry-run) → `plan` → `apply` → `flags.file`. `SpotifyAuthError` becomes one flag `"Spotify refresh token invalid_grant: re-mint with scripts/spotify_auth.py"` and the run stops there (RunLog with `errors`).

- [ ] **Step 1: Write the failing tests**

`tests/test_actions.py`:

```python
from core import actions
from core.model import Action


class FakeSpotify:
    def __init__(self, fail_playlist=None):
        self.calls, self.fail = [], fail_playlist

    def like(self, uris):
        self.calls.append(("like", tuple(uris)))

    def add_items(self, pid, uris):
        if pid == self.fail:
            raise RuntimeError("boom")
        self.calls.append(("add", pid, tuple(uris)))

    def remove_items(self, pid, uris):
        self.calls.append(("remove", pid, tuple(uris)))

    def set_description(self, pid, text):
        self.calls.append(("desc", pid, text))

    def unfollow_playlist(self, pid):
        self.calls.append(("unfollow", pid))


class FakeHub:
    def __init__(self):
        self.pushed = []

    def push(self, table, rows):
        self.pushed.append((table, rows))
        return {"upserted": len(rows), "rejected": []}


ACTS = [
    Action("like", uri="spotify:track:1"), Action("like", uri="spotify:track:2"),
    Action("add_item", playlist_id="P", uri="spotify:track:1"), Action("add_item", playlist_id="P", uri="spotify:track:3"),
    Action("add_item", playlist_id="Q", uri="spotify:track:1"),
    Action("remove_item", playlist_id="P", uri="spotify:track:9"),
    Action("set_description", playlist_id="P", text="smart · synced d"),
    Action("upsert_song", row={"id": "A", "liked": 1}), Action("upsert_membership", row={"id": "P:A", "playlist_id": "P"}),
    Action("delete_membership", row={"id": "P:B", "deleted_at": "t"}), Action("edge", row={"id": "like:liked:A"}),
    Action("upsert_playlist", row={"id": "P", "name": "p"}),
    Action("flag", text="something odd", reason="attention"),
]


def test_apply_batches_spotify_and_groups_hub_tables():
    sp, hub = FakeSpotify(), FakeHub()
    log = actions.apply(ACTS, sp, hub, dry_run=False)
    assert ("like", ("spotify:track:1", "spotify:track:2")) in sp.calls
    assert ("add", "P", ("spotify:track:1", "spotify:track:3")) in sp.calls and ("add", "Q", ("spotify:track:1",)) in sp.calls
    assert ("remove", "P", ("spotify:track:9",)) in sp.calls and ("desc", "P", "smart · synced d") in sp.calls
    tables = {t: rows for t, rows in hub.pushed}
    assert [r["id"] for r in tables["songs"]] == ["A"]
    assert {r["id"] for r in tables["playlist_songs"]} == {"P:A", "P:B"}
    assert tables["provenance"][0]["id"] == "like:liked:A" and tables["playlists"][0]["id"] == "P"
    assert log.flags == ["something odd"] and log.applied["add_item"] == 3 and not log.errors


def test_dry_run_touches_nothing_but_counts():
    sp, hub = FakeSpotify(), FakeHub()
    log = actions.apply(ACTS, sp, hub, dry_run=True)
    assert sp.calls == [] and hub.pushed == [] and log.dry_run
    assert log.applied["like"] == 2 and "add_item: 3" in log.summary()


def test_spotify_error_isolated_per_playlist():
    sp, hub = FakeSpotify(fail_playlist="P"), FakeHub()
    log = actions.apply(ACTS, sp, hub, dry_run=False)
    assert ("add", "Q", ("spotify:track:1",)) in sp.calls
    assert log.errors and "P" in log.errors[0]
```

`tests/test_flags.py`:

```python
import json

import httpx

from core import flags


def test_no_flags_no_task(settings):
    assert flags.file(settings, httpx.Client(), [], [], "2026-09-08") is None


def test_creates_task_when_none_open(settings):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": []})
        return httpx.Response(200, json={"id": "new-page"})

    pid = flags.file(settings, httpx.Client(transport=httpx.MockTransport(handler)), ["a"], ["b"], "2026-09-08")
    assert pid == "new-page"
    method, path, body = seen[-1]
    assert (method, path) == ("POST", "/v1/pages")
    props = body["properties"]
    assert props["Name"]["title"][0]["text"]["content"] == "Music Sync flags"
    assert props["Priority"]["select"]["name"] == "High" and props["Tags"]["multi_select"] == [{"name": "Chore"}]
    assert props["Due Date"]["date"]["start"] == "2026-09-08"
    assert props["Project"]["relation"] == [{"id": "proj"}]
    assert "a" in props["Notes"]["rich_text"][0]["text"]["content"] and "error: b" in props["Notes"]["rich_text"][0]["text"]["content"]


def test_appends_to_open_task(settings):
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content) if req.content else None))
        if req.url.path.endswith("/query"):
            return httpx.Response(200, json={"results": [{"id": "open", "properties": {"Notes": {"rich_text": [{"plain_text": "old"}]}}}]})
        return httpx.Response(200, json={"id": "open"})

    assert flags.file(settings, httpx.Client(transport=httpx.MockTransport(handler)), ["new"], [], "d") == "open"
    method, path, body = seen[-1]
    assert (method, path) == ("PATCH", "/v1/pages/open")
    assert body["properties"]["Notes"]["rich_text"][0]["text"]["content"].startswith("old\n")
```

`tests/test_run.py`:

```python
from datetime import datetime, timezone

from core import run
from core.spotify_client import SpotifyAuthError


class DeadSpotify:
    def me(self):
        raise SpotifyAuthError("invalid_grant: revoked")


def test_invalid_grant_becomes_flag_and_stops(settings, mocker):
    filed = mocker.patch("core.run.flags.file", return_value="page")
    log = run.reconcile(settings, dry_run=True, now=datetime(2026, 9, 8, tzinfo=timezone.utc), spotify=DeadSpotify(), hub=object())
    assert log.errors and "invalid_grant" in log.errors[0]
    assert filed.call_args.args[2] == [] and "invalid_grant" in filed.call_args.args[3][0]
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement**

`src/core/actions.py`:

```python
"""Apply planned actions: Spotify writes batched per playlist, hub rows grouped per table."""

from collections import defaultdict
from dataclasses import dataclass, field

import structlog

from core.hub import HubError
from core.model import Action

log = structlog.get_logger()
HUB_TABLE = {"upsert_song": "songs", "upsert_playlist": "playlists", "upsert_membership": "playlist_songs",
             "delete_membership": "playlist_songs", "edge": "provenance"}


@dataclass
class RunLog:
    applied: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    skipped: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    dry_run: bool = False
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        lines = [f"{k}: {v}" for k, v in sorted(self.applied.items()) if v]
        lines += [f"flag: {f}" for f in self.flags] + [f"error: {e}" for e in self.errors]
        return ("DRY RUN\n" if self.dry_run else "") + ("\n".join(lines) or "nothing to do")


def apply(actions: list[Action], spotify, hub, dry_run: bool) -> RunLog:
    out = RunLog(dry_run=dry_run)
    likes: list[str] = []
    adds: dict[str, list[str]] = defaultdict(list)
    removes: dict[str, list[str]] = defaultdict(list)
    descs: dict[str, str] = {}
    deletes: list[str] = []
    rows: dict[str, dict[str, dict]] = defaultdict(dict)  # table -> id -> merged row
    for a in actions:
        out.applied[a.kind] += 1
        if a.kind == "like":
            likes.append(a.uri)
        elif a.kind == "add_item":
            adds[a.playlist_id].append(a.uri)
        elif a.kind == "remove_item":
            removes[a.playlist_id].append(a.uri)
        elif a.kind == "set_description":
            descs[a.playlist_id] = a.text
        elif a.kind == "delete_playlist":
            deletes.append(a.playlist_id)
        elif a.kind in HUB_TABLE:
            rows[HUB_TABLE[a.kind]].setdefault(a.row["id"], {}).update(a.row)
        elif a.kind == "flag":
            out.flags.append(a.text)
    if dry_run:
        return out
    if likes:
        _safe(out, "like", lambda: spotify.like(sorted(set(likes))))
    for pid, uris in adds.items():
        _safe(out, f"add {pid}", lambda: spotify.add_items(pid, uris))
    for pid, uris in removes.items():
        _safe(out, f"remove {pid}", lambda: spotify.remove_items(pid, uris))
    for pid, text in descs.items():
        _safe(out, f"describe {pid}", lambda: spotify.set_description(pid, text))
    for pid in deletes:
        _safe(out, f"unfollow {pid}", lambda: spotify.unfollow_playlist(pid))
    hub_error = None
    for table in ("songs", "playlists", "playlist_songs", "provenance"):
        if rows.get(table):
            try:
                hub.push(table, list(rows[table].values()))
            except HubError as e:
                out.errors.append(f"hub {table}: {e}")
                hub_error = hub_error or e
    if hub_error:
        raise hub_error
    return out


def _safe(out: RunLog, what: str, fn) -> None:
    try:
        fn()
    except Exception as e:  # one playlist failing must not stop the others
        log.warning("spotify_write_failed", what=what, error=str(e))
        out.errors.append(f"spotify {what}: {e}")
```

(`spotify.unfollow_playlist(pid)` = `DELETE /v1/playlists/{id}/followers`; add it to `SpotifyClient` in this task with a one-line test in `test_spotify_client.py`: method DELETE, path `/v1/playlists/P/followers`. Deleting an owned playlist in Spotify IS unfollowing it.)

`src/core/flags.py`:

```python
"""Batch a run's flags into ONE open Notion Chore task, appending while it stays open."""

import httpx

from core.config import Settings

TITLE = "Music Sync flags"
NOTION = "https://api.notion.com"
VERSION = "2026-03-11"


def _h(settings: Settings) -> dict:
    return {"Authorization": f"Bearer {settings.notion_token}", "Notion-Version": VERSION, "Content-Type": "application/json"}


def file(settings: Settings, http: httpx.Client, flags: list[str], errors: list[str], today: str) -> str | None:
    lines = [f"- {f}" for f in flags] + [f"- error: {e}" for e in errors]
    if not lines:
        return None
    text = f"{today}\n" + "\n".join(lines)
    q = http.post(f"{NOTION}/v1/data_sources/{settings.notion_tasks_data_source_id}/query", headers=_h(settings), json={
        "filter": {"and": [{"property": "Name", "title": {"equals": TITLE}},
                           {"property": "Status", "status": {"does_not_equal": "Completed"}}]},
        "page_size": 1})
    q.raise_for_status()
    results = q.json().get("results") or []
    if results:
        page = results[0]
        old = "".join(t.get("plain_text", "") for t in page["properties"].get("Notes", {}).get("rich_text", []))
        body = {"properties": {"Notes": {"rich_text": [{"text": {"content": (old + "\n" + text)[-1900:]}}]}}}
        r = http.patch(f"{NOTION}/v1/pages/{page['id']}", headers=_h(settings), json=body)
        r.raise_for_status()
        return page["id"]
    body = {"parent": {"type": "data_source_id", "data_source_id": settings.notion_tasks_data_source_id}, "properties": {
        "Name": {"title": [{"text": {"content": TITLE}}]},
        "Status": {"status": {"name": "To Do"}},
        "Priority": {"select": {"name": "High"}},
        "Due Date": {"date": {"start": today}},
        "Tags": {"multi_select": [{"name": "Chore"}]},
        "Project": {"relation": [{"id": settings.notion_project_page_id}]},
        "Notes": {"rich_text": [{"text": {"content": text[:1900]}}]},
    }}
    r = http.post(f"{NOTION}/v1/pages", headers=_h(settings), json=body)
    r.raise_for_status()
    return r.json()["id"]
```

`src/core/run.py`:

```python
"""One reconcile run, end to end. Plain Python; app.py calls this."""

import gzip
import json
from datetime import datetime, timezone

import httpx
import structlog

from core import actions, archive, flags, mirror, reconcile
from core.config import Settings
from core.hub import Hub
from core.spotify_client import SpotifyAuthError, SpotifyClient

log = structlog.get_logger()


def reconcile_run(settings: Settings, dry_run: bool = False, now: datetime | None = None,
                  spotify=None, hub=None, http: httpx.Client | None = None) -> actions.RunLog:
    now = now or datetime.now(timezone.utc)
    today = now.date().isoformat()
    http = http or httpx.Client(timeout=60)
    spotify = spotify or SpotifyClient(settings)
    hub = hub or Hub(settings.life_hub_url, settings.life_hub_token)
    try:
        me = spotify.me()["id"]
    except SpotifyAuthError as e:
        out = actions.RunLog(dry_run=dry_run, errors=[f"Spotify refresh token {e}: re-mint with scripts/spotify_auth.py"])
        flags.file(settings, http, [], out.errors, today)
        return out
    m = mirror.load_mirror(hub)
    live = mirror.pull_live(spotify, settings.spotify_market, me, m)
    if not dry_run:
        archive.put(settings, archive.key_for(now), gzip.compress(json.dumps(live.raw).encode()))
    plan = reconcile.plan(m, live, now, settings.inbox_cap, settings.undo_days, today)
    try:
        out = actions.apply(plan, spotify, hub, dry_run)
    except Exception as e:
        out = actions.RunLog(dry_run=dry_run, errors=[f"run aborted: {e}"])
    log.info("reconcile_done", **{k: v for k, v in out.applied.items() if v}, flags=len(out.flags), errors=len(out.errors))
    if not dry_run:
        flags.file(settings, http, out.flags, out.errors, today)
    return out


reconcile = reconcile_run  # name used by app.py and tests: run.reconcile(...)
```

Careful: `run.py` imports the module `core.reconcile` AND exposes a function named `reconcile`; keep the module reference as `from core import reconcile as reconcile_mod` inside the function body if the alias confuses ruff (F811). The test above calls `run.reconcile(...)`, so the alias must exist.

- [ ] **Step 4: Tests, lint, commit**

```bash
uv run pytest -v && just check
git add -A && git commit -m "Apply actions to Spotify and the hub, batch flags into one Notion task, run glue

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

### Task 12: `/capture` for the Shazam shortcut

**Files:**
- Create: `src/core/capture.py`, `tests/test_capture.py`

**Interfaces:**
- `capture.best_match(title: str, artist: str, tracks: list[dict]) -> dict | None` - normalized (casefold, strip punctuation and bracketed suffixes) title equality first, then artist containment; `None` if nothing matches.
- `capture.capture(payload: dict, spotify, hub, settings, now: datetime) -> dict` with `payload = {"title", "artist", "apple_music_id"?, "shazam_url"?}` → `{"ok": bool, "message": str, "isrc": str | None}`:
  - missing title or artist → `{"ok": False, "message": "title and artist required"}`
  - search via `spotify.search_track`, no match → `{"ok": False, "message": "Could not find <title> by <artist> on Spotify"}` and the caller files the task (the endpoint does, see Task 13)
  - match without ISRC → ok False, message says so
  - inbox playlist = the `playlists` row with `kind='inbox'` (pulled from the hub; missing → ok False "no inbox playlist")
  - already in inbox (mirror membership) → `{"ok": True, "message": "<title> by <artist> is already in new songs"}`
  - else `spotify.add_items(inbox, [uri])`, push `songs` row if new (`liked=0, first_seen=now`), push `playlist_songs` row, push `provenance` edge `{id: "shazam:<apple_music_id or isrc>:<isrc>", from_kind: "shazam", from_ref: apple_music_id or isrc, to_kind: "songs", to_ref: isrc, rel: "imported_from", asserted_by: "music-sync", detail: {created_row: 0|1, shazam_url}}`, then trim the inbox to `settings.inbox_cap` by removing the oldest live items → `{"ok": True, "message": "<title> by <artist> added to new songs"}`.
  - `SpotifyAuthError` propagates (the endpoint turns it into a 503 and a flag).

- [ ] **Step 1: Write the failing tests** (`tests/test_capture.py`)

```python
from datetime import datetime, timezone

from core import capture

NOW = datetime(2026, 9, 8, 12, 0, tzinfo=timezone.utc)


def track(tid, name, artist, isrc="USUM71703861", playable=True):
    return {"id": tid, "uri": f"spotify:track:{tid}", "name": name, "is_playable": playable,
            "artists": [{"name": artist}], "external_ids": {"isrc": isrc}, "album": {"name": "a", "release_date": "2017-01-01"}}


def test_best_match_normalizes_title_and_artist():
    tracks = [track("1", "Money Trees (feat. Jay Rock)", "Kendrick Lamar"), track("2", "Money Trees - Live", "Kendrick Lamar")]
    assert capture.best_match("money trees", "Kendrick Lamar", tracks)["id"] == "1"
    assert capture.best_match("Nope", "Kendrick Lamar", tracks) is None


class FakeSpotify:
    def __init__(self, tracks, inbox_items=()):
        self.tracks, self.inbox_items, self.calls = tracks, list(inbox_items), []

    def search_track(self, title, artist, market):
        return self.tracks

    def add_items(self, pid, uris):
        self.calls.append(("add", pid, uris))

    def remove_items(self, pid, uris):
        self.calls.append(("remove", pid, uris))

    def get_playlist_items(self, pid, market):
        return self.inbox_items


class FakeHub:
    def __init__(self, playlists, songs=(), memberships=()):
        self.tables = {"playlists": playlists, "songs": list(songs), "playlist_songs": list(memberships), "provenance": []}
        self.pushed = []

    def pull(self, table, columns, since=""):
        return [{c: r.get(c) for c in columns} for r in self.tables[table]]

    def push(self, table, rows):
        self.pushed.append((table, rows))
        return {"upserted": len(rows), "rejected": []}


INBOX = [{"id": "IN", "name": "new songs", "kind": "inbox", "rule": None, "description": None, "snapshot_id": None, "pinned": 1, "expires_at": None, "deleted_at": None}]


def test_capture_adds_new_song_with_shazam_edge(settings):
    sp, hub = FakeSpotify([track("1", "Money Trees", "Kendrick Lamar")]), FakeHub(INBOX)
    out = capture.capture({"title": "Money Trees", "artist": "Kendrick Lamar", "apple_music_id": "12", "shazam_url": "https://s"}, sp, hub, settings, NOW)
    assert out == {"ok": True, "message": "Money Trees by Kendrick Lamar added to new songs", "isrc": "USUM71703861"}
    assert ("add", "IN", ["spotify:track:1"]) in sp.calls
    tables = dict(hub.pushed)
    assert tables["songs"][0]["id"] == "USUM71703861" and tables["songs"][0]["liked"] == 0
    assert tables["playlist_songs"][0]["id"] == "IN:USUM71703861"
    edge = tables["provenance"][0]
    assert edge["id"] == "shazam:12:USUM71703861" and edge["from_kind"] == "shazam" and edge["detail"]["created_row"] == 1


def test_capture_already_in_inbox(settings):
    hub = FakeHub(INBOX, songs=[{"id": "USUM71703861", "liked": 0, "deleted_at": None}],
                  memberships=[{"id": "IN:USUM71703861", "playlist_id": "IN", "isrc": "USUM71703861", "spotify_track_id": "1", "added_at": "t", "deleted_at": None}])
    sp = FakeSpotify([track("1", "Money Trees", "Kendrick Lamar")])
    out = capture.capture({"title": "Money Trees", "artist": "Kendrick Lamar"}, sp, hub, settings, NOW)
    assert out["ok"] and "already" in out["message"] and sp.calls == []


def test_capture_no_match_and_missing_fields(settings):
    sp, hub = FakeSpotify([]), FakeHub(INBOX)
    assert capture.capture({"title": "X", "artist": "Y"}, sp, hub, settings, NOW) == {"ok": False, "message": "Could not find X by Y on Spotify", "isrc": None}
    assert capture.capture({"title": "X"}, sp, hub, settings, NOW)["message"] == "title and artist required"


def test_capture_trims_inbox(settings):
    items = [{"added_at": f"2026-08-{i + 1:02d}T00:00:00.000Z", "item": track(str(i), "n", "a", isrc=f"US{i:010d}"[:12])} for i in range(3)]
    sp, hub = FakeSpotify([track("9", "New", "A", isrc="GBBTV1101287")], inbox_items=items), FakeHub(INBOX)
    settings.inbox_cap = 3
    capture.capture({"title": "New", "artist": "A"}, sp, hub, settings, NOW)
    assert ("remove", "IN", ["spotify:track:0"]) in sp.calls
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement `src/core/capture.py`**

```python
"""POST /capture: resolve a Shazam result on Spotify, add it to the inbox, record the edge."""

import re
from datetime import datetime

from core import mirror as mirror_mod
from core.config import Settings
from core.model import ISRC_RE

_STRIP = re.compile(r"\s*[\(\[\-].*$")
_PUNCT = re.compile(r"[^\w\s]")


def _norm(s: str) -> str:
    return _PUNCT.sub("", _STRIP.sub("", s or "")).casefold().strip()


def best_match(title: str, artist: str, tracks: list[dict]) -> dict | None:
    t, a = _norm(title), _norm(artist)
    for tr in tracks:
        names = [_norm(x.get("name", "")) for x in tr.get("artists") or []]
        if _norm(tr.get("name", "")) == t and any(a in n or n in a for n in names):
            return tr
    for tr in tracks:
        if _norm(tr.get("name", "")) == t:
            return tr
    return None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def capture(payload: dict, spotify, hub, settings: Settings, now: datetime) -> dict:
    title, artist = (payload.get("title") or "").strip(), (payload.get("artist") or "").strip()
    if not title or not artist:
        return {"ok": False, "message": "title and artist required", "isrc": None}
    tr = best_match(title, artist, spotify.search_track(title, artist, settings.spotify_market))
    if not tr:
        return {"ok": False, "message": f"Could not find {title} by {artist} on Spotify", "isrc": None}
    isrc = ((tr.get("external_ids") or {}).get("isrc") or "").upper()
    if not re.match(ISRC_RE, isrc):
        return {"ok": False, "message": f"{title} by {artist} has no ISRC on Spotify", "isrc": None}
    m = mirror_mod.load_mirror(hub)
    inbox = next((p for p in m.playlists.values() if p.kind == "inbox"), None)
    if not inbox:
        return {"ok": False, "message": "no inbox playlist in life-data", "isrc": isrc}
    if (inbox.id, isrc) in m.memberships:
        return {"ok": True, "message": f"{title} by {artist} is already in new songs", "isrc": isrc}
    now_s = _iso(now)
    spotify.add_items(inbox.id, [tr["uri"]])
    created = isrc not in m.songs
    if created:
        hub.push("songs", [{"id": isrc, "liked": 0, "liked_at": None, "first_seen": now_s}])
    hub.push("playlist_songs", [{"id": f"{inbox.id}:{isrc}", "playlist_id": inbox.id, "isrc": isrc,
                                 "spotify_track_id": tr["id"], "added_at": now_s, "deleted_at": None}])
    ref = str(payload.get("apple_music_id") or isrc)
    hub.push("provenance", [{"id": f"shazam:{ref}:{isrc}", "from_kind": "shazam", "from_ref": ref, "to_kind": "songs",
                             "to_ref": isrc, "rel": "imported_from", "asserted_by": "music-sync",
                             "detail": {"created_row": 1 if created else 0, "shazam_url": payload.get("shazam_url")}}])
    items = sorted((mirror_mod.item_from_raw(i) for i in spotify.get_playlist_items(inbox.id, settings.spotify_market)),
                   key=lambda x: x.added_at)
    extra = [i for i in items if i.isrc][: max(0, len([i for i in items if i.isrc]) - settings.inbox_cap)]
    if extra:
        spotify.remove_items(inbox.id, [i.uri for i in extra])
        hub.push("playlist_songs", [{"id": f"{inbox.id}:{i.isrc}", "deleted_at": now_s} for i in extra])
    return {"ok": True, "message": f"{title} by {artist} added to new songs", "isrc": isrc}
```

- [ ] **Step 4: Tests, lint, commit**

```bash
uv run pytest -v && just check
git add -A && git commit -m "/capture: resolve a Shazam result, add to the inbox, shazam provenance edge, FIFO trim

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
```

### Task 13: Modal app, R2 provisioning, ENV completion, first deploy (no cron activation yet)

**Files:**
- Modify: `app.py` (rewrite), `README.md` (rewrite for the new shape), `AGENTS.md` (update layout + the "one writer" rule), `justfile` (`sync-secrets` uses the modal wrapper pattern from derivations; `deploy: test sync-secrets` unchanged)
- Create: `scripts/provision.py`

**Interfaces:**
- Modal functions: `reconcile_cron` (`schedule=modal.Cron("0 * * * *")`, `max_containers=1`, `timeout=1500`) → `run.reconcile(Settings())`; `reconcile` endpoint (`POST`, proxy auth, body `{"dry_run": bool}`) → `{"summary": str, "applied": dict, "flags": list, "errors": list}`; `capture` endpoint (`POST`, proxy auth) → the `capture.capture` dict, plus: when `ok` is False and the message starts with "Could not find", it files the Chore task through `flags.file` with `[f"Add {title} by {artist} to new songs manually"]`; `SpotifyAuthError` → 503 with the flag filed.
- **Cron activation gate (spec req 18):** the cron decorator is present but guarded: `reconcile_cron` returns immediately unless the Modal secret has `RECONCILE_ENABLED=1`. The migration (Task 16) flips that field last.

- [ ] **Step 1: `app.py`**

```python
"""Modal deployment shim - ALL infrastructure lives here, as code.

Business logic stays in src/core/ (plain Python, no Modal imports). This file
maps it onto Modal: image, secrets, the hourly reconcile, and two proxy-auth
endpoints (/reconcile on demand, /capture for the Shazam shortcut).
"""

import os

import modal

APP_NAME = "music-sync"  # also the Modal secret name (see justfile sync-secrets)

app = modal.App(APP_NAME)

image = (
    modal.Image.debian_slim(python_version="3.13")
    .uv_sync(extra_options="--no-dev")
    .add_local_dir("src/core", remote_path="/root/core", ignore=["**/__pycache__"])
)

secrets = [modal.Secret.from_name(APP_NAME)]


def _run(dry_run: bool) -> dict:
    from core import run
    from core.config import Settings

    log = run.reconcile(Settings(), dry_run=dry_run)
    return {"summary": log.summary(), "applied": dict(log.applied), "flags": log.flags, "errors": log.errors}


@app.function(image=image, secrets=secrets, schedule=modal.Cron("0 * * * *"), max_containers=1, timeout=1500)
def reconcile_cron():
    # Activation gate (spec req 18): the migration flips RECONCILE_ENABLED=1 in the
    # Modal secret after the manual review and a clean dry-run.
    if os.environ.get("RECONCILE_ENABLED") != "1":
        print("reconcile_cron: RECONCILE_ENABLED != 1, skipping")
        return {"skipped": True}
    return _run(dry_run=False)


@app.function(image=image, secrets=secrets, max_containers=1, timeout=1500)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def reconcile(body: dict | None = None):
    return _run(dry_run=bool((body or {}).get("dry_run", False)))


@app.function(image=image, secrets=secrets, timeout=120)
@modal.fastapi_endpoint(method="POST", requires_proxy_auth=True)
def capture(body: dict):
    from datetime import date, datetime, timezone

    import httpx
    from fastapi.responses import JSONResponse

    from core import capture as cap
    from core import flags
    from core.config import Settings
    from core.hub import Hub
    from core.spotify_client import SpotifyAuthError, SpotifyClient

    s = Settings()
    try:
        out = cap.capture(body or {}, SpotifyClient(s), Hub(s.life_hub_url, s.life_hub_token), s, datetime.now(timezone.utc))
    except SpotifyAuthError as e:
        flags.file(s, httpx.Client(), [], [f"Spotify refresh token {e}: re-mint with scripts/spotify_auth.py"], date.today().isoformat())
        return JSONResponse({"ok": False, "message": "Spotify token expired; flagged"}, status_code=503)
    if not out["ok"] and out["message"].startswith("Could not find"):
        flags.file(s, httpx.Client(), [f"Add {body.get('title')} by {body.get('artist')} to new songs manually"], [], date.today().isoformat())
    return out
```

- [ ] **Step 2: `scripts/provision.py`** (provision contract; mints the R2 token as a bucket-scoped CF API token; the two non-secret ids are printed too so bootstrap fills them):

```python
# /// script
# requires-python = ">=3.12"
# dependencies = ["httpx"]
# ///
"""Mint this project's machine-creatable credentials (op-project-bootstrap
provision contract: --list prints mintable field names; --field NAME prints
ONLY the value to stdout, progress on stderr).

R2_API_TOKEN: a Cloudflare API token scoped to object writes on ONE bucket
(the life-data archive), recreated on every mint because CF never re-reveals
a token. Needs the AI Agent CF token (User API Tokens: Edit).
"""

import subprocess
import sys

import httpx

CF_ACCOUNT = "1e69de15e5dc3dddea6db7b3ae8087bc"
BUCKET = "life-data-archive"
NAME = "music-sync-r2"
OP_CF_TOKEN = "op://4eeyrkqibibn7k4j6rz2fbzvxm/mxxpo6neiz3grdyrjj7rv7nume/credential"
FIELDS = ["R2_API_TOKEN", "R2_ACCOUNT_ID", "R2_BUCKET"]


def log(msg: str) -> None:
    print(msg, file=sys.stderr)


def op_read(ref: str) -> str:
    return subprocess.run(["op", "read", ref], capture_output=True, text=True, check=True).stdout.strip()


def mint_r2_token() -> str:
    admin = op_read(OP_CF_TOKEN)
    c = httpx.Client(base_url="https://api.cloudflare.com/client/v4", headers={"Authorization": f"Bearer {admin}"})
    for t in c.get("/user/tokens", params={"per_page": 100}).raise_for_status().json()["result"] or []:
        if t["name"] == NAME:
            log(f"deleting existing token {NAME} (value not re-readable)")
            c.delete(f"/user/tokens/{t['id']}").raise_for_status()
    groups = c.get("/user/tokens/permission_groups").raise_for_status().json()["result"]
    write = next(g for g in groups if g["name"] == "Workers R2 Storage Bucket Item Write")
    r = c.post("/user/tokens", json={
        "name": NAME,
        "policies": [{
            "effect": "allow",
            "resources": {f"com.cloudflare.edge.r2.bucket.{CF_ACCOUNT}_default_{BUCKET}": "*"},
            "permission_groups": [{"id": write["id"]}],
        }],
    }).raise_for_status()
    log("✓ R2 bucket-scoped write token minted")
    return r.json()["result"]["value"]


def main() -> None:
    match sys.argv[1:]:
        case ["--list"]:
            print("\n".join(FIELDS))
        case ["--field", "R2_API_TOKEN"]:
            print(mint_r2_token())
        case ["--field", "R2_ACCOUNT_ID"]:
            print(CF_ACCOUNT)
        case ["--field", "R2_BUCKET"]:
            print(BUCKET)
        case _:
            sys.exit("usage: provision.py --list | --field <name>")


if __name__ == "__main__":
    main()
```

The permission group name `Workers R2 Storage Bucket Item Write` (id `2efd5506f9c8494dacb1fa10a3e7d5b6`, scope `com.cloudflare.edge.r2.bucket`) was verified against the account on 2026-09-08. The bucket resource key format `com.cloudflare.edge.r2.bucket.<account>_default_<bucket>` is Cloudflare's documented form for jurisdiction-less buckets; if the API rejects it, `GET /accounts/<id>/r2/buckets` lists the bucket and the error names the expected key.

- [ ] **Step 3: Fill the ENV item.** Run bootstrap in Alex's desktop-authenticated terminal (it provisions the three R2 fields via `provision.py`, prompts for the rest, and mints nothing else since the vault exists). Values to give it: `LIFE_HUB_URL` = `https://life-data.nqipomyrjb.workers.dev`, `LIFE_HUB_TOKEN` = from Task 5, `NOTION_TASKS_DATA_SOURCE_ID` = `77ef5074-aa23-468a-b5fb-2692e78184db`, `NOTION_PROJECT_PAGE_ID` = `31103953-a8af-81fd-a4c2-e666c06308ee`, `NOTION_TOKEN` already set. Then delete the stale `NOTION_PARENT_PAGE_ID` field and verify:

```bash
op-project-bootstrap ~/Desktop/coding/active-projects/music-sync/.env.tpl --repo alexjmiller5/music-sync
zsh -ic 'op-personal item edit 65zbc6qstoi6m64hjtxb5uuhu4 --vault 4cpe3gxzolbxtu4tnsyzzvhype "NOTION_PARENT_PAGE_ID[delete]"'
~/.claude/skills/1password/scripts/op-project-bootstrap --check .env.tpl
```

Expected: every ref `ok`. The `Music Sync CI Modal Token` item still shows CHANGEME for `token-id`/`token-secret`: bootstrap mints them through a `provision.py` only if the repo carries one for Modal; copy `mint()`/`read()` from `derivations/scripts/provision.py` into this repo's `provision.py` (same `PROJECT` pattern, `FIELDS` gains `token-id`, `token-secret`) before running bootstrap so both items fill in one run.

- [ ] **Step 4: README and AGENTS.md.** README: replace everything after "Layout" with: what the app does (three kinds, liked = pool, un-heart semantics, inbox), the two endpoints and their bodies, `just` verbs, the manual steps (Spotify developer app + `spotify_auth.py`, Modal proxy-auth token for the shortcut, `op-project-bootstrap`), the migration pointer (`scripts/migrate.py`, spec §9), and the 180-day token note. AGENTS.md: update the layout block to the file list in this plan's "File structure", add the rule "life-data is written ONLY by this app; agents and the user write Spotify", and the activation gate.

- [ ] **Step 5: Tests, lint, commit, push, watch the deploy**

```bash
uv run pytest -v && just check
git add -A && git commit -m "Modal app: hourly reconcile behind an activation gate, /reconcile and /capture endpoints, R2 provisioning

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
git push
gh run list --workflow=deploy.yml --limit 1 && gh run watch <run-id> --exit-status
```

Expected: green. `modal app list` shows `music-sync` deployed. The cron exists but returns `skipped` every hour until `RECONCILE_ENABLED=1`.

- [ ] **Step 6: Smoke the deployed `/reconcile` in dry-run.** Mint a Proxy Auth Token in the Modal dashboard (Settings → Proxy Auth Tokens; the one manual step), store it as fields `MODAL_KEY` / `MODAL_SECRET` on `iOS Shortcuts ENV` (the shortcut needs the same pair), then:

```bash
zsh -ic 'k=$(op-personal read "op://iOS Shortcuts/iOS Shortcuts ENV/MODAL_KEY"); s=$(op-personal read "op://iOS Shortcuts/iOS Shortcuts ENV/MODAL_SECRET"); curl -s -X POST https://<workspace>--music-sync-reconcile.modal.run -H "Modal-Key: $k" -H "Modal-Secret: $s" -H "Content-Type: application/json" -d "{\"dry_run\": true}" | jq .'
```

Expected: a summary with `upsert_song` ≈ 7,000+ and `upsert_playlist` = number of owned playlists, zero errors, and the tables in life-data still empty (dry run). Report the counts in chat.

### Task 14: Operator scripts - review report, migration steps, derivation backfill

**Files:**
- Create: `scripts/review.py`, `scripts/migrate.py`, `scripts/backfill_derive.py`, `tests/test_review.py`
- Delete after migration step 9: `scripts/create_50s_playlist.py`

**Interfaces:**
- `scripts/review.py` (PEP 723 inline metadata, `dependencies = ["httpx"]`, imports `core` via `sys.path.insert(0, "src")`): `report(mirror: Mirror, live: Live | None) -> dict[str, list[str]]` returning the eight §9.7 sections keyed `curated_not_liked`, `liked_no_playlist`, `bucket_mismatch`, `unplayable_alt`, `unplayable_none`, `dup_isrc_in_playlist`, `same_title_diff_isrc`, `shazam_never_liked`, `no_isrc`; `main()` runs `op run`-provided settings, pulls live and mirror, prints each section as `## <name> (<count>)` followed by `- <title> - <artists> [<isrc>] (<year>) in: <playlists>` lines, `--json` prints the dict. Read-only: no Spotify writes, no hub writes.
- `scripts/migrate.py --step <name> [--dry-run]` with steps `inbox` (creates the `playlists` row for the `new songs` playlist id given by `--playlist-id`), `first-pull` (runs `run.reconcile(settings, dry_run=False)` with the reconciler's Spotify writes disabled via a `writes=False` flag added to `actions.apply` in this task, so only hub rows and the archive happen; the `like`/`add_item`/`remove_item`/`set_description`/`delete_playlist` actions are counted into `skipped`), `shazam-edges` (for every membership of the playlist named `My Shazam Tracks`, push a `provenance` edge `shazam:<isrc>:<isrc>`, `from_ref=<isrc>`, `detail.created_row=0`), `like-pool` (like every song in the playlists named `the good stuff`, `galaxy`, `rap` that is not liked; prints the count and refuses without `--yes`), `smart-buckets` (sets `kind=smart` + the §5.1 rules on those three rows by pushing `playlists` rows), `rules` (registers `smart-songs-match-rule` as in Task 9 step 5), `enable` (sets `RECONCILE_ENABLED=1` by `op item edit` + `just sync-secrets`; prints the two commands instead of running them, since the item edit is Alex's approval).
- `scripts/backfill_derive.py [--table songs] [--col first_year]`: pulls ids with `hub.pull("songs", ["id","title","deezer_genres","mb_tags","first_year","deleted_at"])`, selects rows where any derived column is null, calls `hub.derive` in chunks of 50 with a 60 s sleep between chunks (MusicBrainz pacing is inside the derivation service; the sleep keeps the hub request under its time budget), prints running totals, exits 1 if `failed` is non-empty at the end.

- [ ] **Step 1: Write the failing test** (`tests/test_review.py`)

```python
import importlib.util
import sys
from pathlib import Path

from core.model import Live, LiveItem, LivePlaylist, Membership, Mirror, Playlist, Song

spec = importlib.util.spec_from_file_location("review", Path(__file__).parents[1] / "scripts" / "review.py")
review = importlib.util.module_from_spec(spec)
sys.modules["review"] = review
spec.loader.exec_module(review)


def song(isrc, liked=1, year=2010, genres=("Pop",), title="t", artists=("a",), ids=("x",), playable=1):
    return Song(isrc, liked, None, "f", list(ids), playable, year, list(genres), [], title, list(artists))


def test_report_sections():
    songs = {"A": song("A", liked=0), "B": song("B"), "C": song("C", year=1995), "D": song("D", title="Same", artists=["X"]),
             "E": song("E", title="Same", artists=["X"]), "F": song("F", playable=0, ids=[])}
    playlists = {"G": Playlist("G", "the good stuff", "curated", None, None, None, 1, None),
                 "S": Playlist("S", "My Shazam Tracks", "curated", None, None, None, 1, None)}
    memberships = {("G", "A"): Membership("G", "A", "x", "t"), ("G", "C"): Membership("G", "C", "x", "t"),
                   ("S", "A"): Membership("S", "A", "x", "t"), ("G", "F"): Membership("G", "F", "x", "t")}
    m = Mirror(songs, playlists, memberships, [], set())
    live = Live({"G": LivePlaylist("G", "the good stuff", None, "s", [
        LiveItem("F", "x", "spotify:track:x", "t", False, False, "t", ["a"]),
        LiveItem(None, None, "spotify:local:y", "t", False, True, "local", []),
        LiveItem("C", "x", "spotify:track:x", "t", True, False, "t", ["a"]),
        LiveItem("C", "x2", "spotify:track:x2", "t2", True, False, "t", ["a"])])}, {}, {})
    r = review.report(m, live)
    assert [l for l in r["curated_not_liked"] if "[A]" in l]
    assert [l for l in r["liked_no_playlist"] if "[B]" in l] and [l for l in r["liked_no_playlist"] if "[D]" in l]
    assert [l for l in r["bucket_mismatch"] if "[C]" in l and "the good stuff" in l]
    assert [l for l in r["unplayable_none"] if "[F]" in l]
    assert [l for l in r["dup_isrc_in_playlist"] if "[C]" in l]
    assert [l for l in r["same_title_diff_isrc"] if "Same" in l]
    assert [l for l in r["shazam_never_liked"] if "[A]" in l]
    assert r["no_isrc"] == ["the good stuff: local (local file)"]
```

- [ ] **Step 2: Run to verify failure**, then **Step 3: implement `scripts/review.py`**

```python
# /// script
# requires-python = ">=3.13"
# dependencies = ["httpx", "pydantic-settings", "structlog"]
# ///
"""Non-compliance report over the mirror (spec section 9.7). Read-only.

    op run --env-file=.env.tpl -- uv run scripts/review.py [--json]
"""

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from core.model import Live, Mirror  # noqa: E402

BUCKETS = {"the good stuff": lambda s: (s.first_year or 0) >= 2000, "galaxy": lambda s: (s.first_year or 9999) < 2000,
           "rap": lambda s: "Rap/Hip Hop" in (s.deezer_genres or [])}
_NORM = re.compile(r"\s*[\(\[\-].*$")


def _line(s, m: Mirror) -> str:
    where = sorted(m.playlists[pid].name for (pid, isrc) in m.memberships if isrc == s.id and pid in m.playlists)
    return f"- {s.title} - {', '.join(s.artists)} [{s.id}] ({s.first_year}) in: {', '.join(where) or '-'}"


def report(m: Mirror, live: Live | None) -> dict[str, list[str]]:
    r = defaultdict(list)
    by_name = {p.name: p.id for p in m.playlists.values()}
    kind = {p.id: p.kind for p in m.playlists.values()}
    in_playlist = {isrc for (_, isrc) in m.memberships}
    for (pid, isrc), _ in m.memberships.items():
        s = m.songs.get(isrc)
        if s and kind.get(pid) == "curated" and not s.liked:
            r["curated_not_liked"].append(_line(s, m))
    for s in m.songs.values():
        if s.liked and s.id not in in_playlist:
            r["liked_no_playlist"].append(_line(s, m))
    for name, ok in BUCKETS.items():
        pid = by_name.get(name)
        for (q, isrc) in m.memberships:
            if q == pid and isrc in m.songs and not ok(m.songs[isrc]):
                r["bucket_mismatch"].append(_line(m.songs[isrc], m) + f" <- {name}")
    if live:
        for lp in live.playlists.values():
            seen = Counter()
            for it in lp.items or []:
                if not it.isrc:
                    r["no_isrc"].append(f"{lp.name}: {it.name} ({'local file' if it.is_local else 'no ISRC'})")
                    continue
                seen[it.isrc] += 1
                s = m.songs.get(it.isrc)
                if not it.playable and s:
                    alt = s.spotify_playable and s.spotify_ids and s.spotify_ids[0] != it.track_id
                    r["unplayable_alt" if alt else "unplayable_none"].append(_line(s, m) + f" <- {lp.name}")
            for isrc, n in seen.items():
                if n > 1 and isrc in m.songs:
                    r["dup_isrc_in_playlist"].append(_line(m.songs[isrc], m) + f" x{n} in {lp.name}")
    groups = defaultdict(list)
    for s in m.songs.values():
        groups[(_NORM.sub("", s.title or "").casefold(), (s.artists or [""])[0].casefold())].append(s)
    for (title, artist), ss in groups.items():
        if len(ss) > 1 and title:
            r["same_title_diff_isrc"].append(f"- {ss[0].title} - {artist}: " + "; ".join(f"{s.title} [{s.id}] ({s.first_year})" for s in ss))
    shz = by_name.get("My Shazam Tracks")
    for (pid, isrc) in m.memberships:
        if pid == shz and isrc in m.songs and not m.songs[isrc].liked:
            r["shazam_never_liked"].append(_line(m.songs[isrc], m))
    return {k: sorted(v) for k, v in r.items()} | {k: [] for k in
        ["curated_not_liked", "liked_no_playlist", "bucket_mismatch", "unplayable_alt", "unplayable_none",
         "dup_isrc_in_playlist", "same_title_diff_isrc", "shazam_never_liked", "no_isrc"] if k not in r}


def main() -> None:
    from core import mirror as mm
    from core.config import Settings
    from core.hub import Hub
    from core.spotify_client import SpotifyClient

    s = Settings()
    sp = SpotifyClient(s)
    m = mm.load_mirror(Hub(s.life_hub_url, s.life_hub_token))
    live = mm.pull_live(sp, s.spotify_market, sp.me()["id"], Mirror({}, {}, {}, [], set()))  # full pull, no snapshot skipping
    out = report(m, live)
    if "--json" in sys.argv:
        print(json.dumps(out, indent=1, ensure_ascii=False))
        return
    for k, lines in out.items():
        print(f"\n## {k} ({len(lines)})")
        print("\n".join(lines))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: `scripts/migrate.py` and `scripts/backfill_derive.py`** (both PEP 723, same `sys.path` prelude). `migrate.py` is a `match` over `--step` implementing the interfaces above with plain calls into `core`; every step prints what it will do and stops unless `--dry-run` is absent; `like-pool` additionally needs `--yes`. `backfill_derive.py` is a 30-line loop over `hub.pull` + `hub.derive` as specified. Add the `writes: bool = True` parameter to `actions.apply` (when False, Spotify write kinds are appended to `skipped` as `"<kind> <playlist_id> <uri>"` and not called) with one test in `test_actions.py` asserting `sp.calls == []` and `len(log.skipped) == 7` for `ACTS`.

- [ ] **Step 5: Tests, lint, commit, push** (deploy runs; the cron stays gated)

```bash
uv run pytest -v && just check
git add -A && git commit -m "Operator scripts: review report, migration steps, derivation backfill; apply(writes=False)

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
git push && gh run watch $(gh run list --workflow=deploy.yml --limit 1 --json databaseId -q '.[0].databaseId') --exit-status
```

## Phase D - Shazam shortcut

### Task 15: Rewrite `Shazam → Spotify` as a `/capture` client

**Files (ios-shortcuts repo):**
- Modify: `shortcuts/shazam_right_pointing_arrow_spotify.cherri` (rewrite), `README.md` (replace the "Spotify token reauthorization" section)
- Delete: `shortcuts/spotify_reauth.cherri`, `shortcuts/Shazam → Spotify.shortcut`, `docs/superpowers/specs/2026-06-26-spotify-reauth-design.md`
- Add to `constants.txt`: `MUSIC_SYNC_CAPTURE_URL=https://example.modal.run/capture` (placeholder; the real URL goes in the untracked `constants.local.txt` per the README convention)

**Interfaces:**
- Consumes `POST /capture` (Task 13) with headers `Modal-Key`/`Modal-Secret` from `iOS Shortcuts ENV` fields `MODAL_KEY`/`MODAL_SECRET` (stored in Task 13 step 6).

- [ ] **Step 1: Rewrite the shortcut** (cherri; conditions need `@` on variables per the `cherri` skill)

```cherri
#define name Shazam → Spotify
#define color blue
#define glyph shazam
#include 'actions/media'
#include 'actions/web'

@shazamResult = startShazam(false)

if !@shazamResult {
	showNotification("Unable to recognize the song.", "Shazam → Spotify")
	stop()
}

const Song = getShazamDetail(@shazamResult, "Title")
const Artist = getShazamDetail(@shazamResult, "Artist")
const AppleMusicID = getShazamDetail(@shazamResult, "Apple Music ID")
const ShazamURL = getShazamDetail(@shazamResult, "Shazam URL")

// music-sync resolves the song on Spotify, adds it to "new songs", records the
// Shazam capture in life-data, and files a task itself when it can't match.
@response = jsonRequest("<<constant:MUSIC_SYNC_CAPTURE_URL>>", "POST", {
	"title": "{Song}",
	"artist": "{Artist}",
	"apple_music_id": "{AppleMusicID}",
	"shazam_url": "{ShazamURL}"
}, {
	"Modal-Key": "<<secret:MODAL_KEY>>",
	"Modal-Secret": "<<secret:MODAL_SECRET>>",
	"Content-Type": "application/json"
})

@responseDict = getDictionary(@response)
@message = getValue(@responseDict, "message")

if !@message {
	showNotification("music-sync did not answer - {Song} by {Artist} was not saved.", "Shazam → Spotify")
	stop()
}

showNotification("{message}", "Shazam → Spotify")
```

- [ ] **Step 2: Compile and install**

```bash
cd ~/Desktop/coding/active-projects/ios-shortcuts
printf 'MUSIC_SYNC_CAPTURE_URL=https://<workspace>--music-sync-capture.modal.run\n' >> constants.local.txt
just compile shortcuts/shazam_right_pointing_arrow_spotify.cherri
```

Import the produced `.shortcut` on the Mac (double-click), replacing the existing one, and AirDrop it to the phone (reimport re-prompts permissions; expected per README). Delete the old `Spotify Reauth` shortcut on both devices.

- [ ] **Step 3: E2E from the phone (Alex).** Shazam a song playing on the Mac. Expected within seconds: the notification "<title> by <artist> added to new songs"; the song appears in `new songs` in the Spotify app; `life sync && life sql "SELECT id FROM provenance WHERE from_kind='shazam' ORDER BY created_at DESC LIMIT 1"` shows the edge. Then run it again on the same song: "already in new songs". Report both in chat before deleting anything.

- [ ] **Step 4: Clean up credentials and commit**

```bash
git rm -q shortcuts/spotify_reauth.cherri "shortcuts/Shazam → Spotify.shortcut" docs/superpowers/specs/2026-06-26-spotify-reauth-design.md
zsh -ic 'op-personal item edit "iOS Shortcuts ENV" --vault "iOS Shortcuts" "SPOTIFY_CLIENT_ID[delete]" "SPOTIFY_CLIENT_SECRET[delete]" "SPOTIFY_REFRESH_TOKEN[delete]" "SPOTIFY_SHAZAM_PLAYLIST_ID[delete]"'
sed -i '' '/^SPOTIFY_REDIRECT_URI=/d' constants.txt
git add -A && git commit -m "Shazam → Spotify: post to music-sync /capture; drop Spotify credentials and the Reauth shortcut

Claude-Session: https://claude.ai/code/session_01Xmcui2xxjA2r4mPASiGrns"
git push
```

Also delete the "Shazam → Spotify Shortcut" developer app in the Spotify dashboard (nothing uses it now) and note in the music-sync README that the "AI Agent" app is the only one left.

## Phase E - Migration and activation (spec §9, human in the loop)

### Task 16: Run the migration

Each step is one command from Task 14 unless noted; run in order, dry-run first where the flag exists, and paste the printed counts into chat after each step. Do not skip the gate.

- [ ] **Step 1: Catalog + rules exist** (Task 5 done), derivations registered (Task 4 step 8), ENV complete (Task 13 step 3), app deployed with the cron gated (Task 13 step 5). `life check` clean.

- [ ] **Step 2: Create `new songs` in Spotify** with the spotify skill: `spotify_player playlist new "new songs"` (or in the app), read its id from `spotify_player playlist list`, then `op run --env-file=.env.tpl -- uv run scripts/migrate.py --step inbox --playlist-id <id>`; `life sync && life sql "SELECT id,name,kind FROM playlists"` shows one inbox row.

- [ ] **Step 3: Archive the GDPR zips** to R2 with the same token the app uses, then remove the local copies (they are Alex's personal data, never committed):

```bash
for f in data/raw/*.zip; do op run --env-file=.env.tpl -- bash -c 'curl -s -X PUT "https://api.cloudflare.com/client/v4/accounts/$R2_ACCOUNT_ID/r2/buckets/$R2_BUCKET/objects/raw%2Fspotify-export%2F'"$(basename "$f" | tr ' ' '_')"'" -H "Authorization: Bearer $R2_API_TOKEN" -H "Content-Type: application/zip" --data-binary @"'"$f"'" | jq -r .success'; done
rm -rf data && git status --short
```

- [ ] **Step 4: First pull** (hub rows + archive only; no Spotify writes): `op run --env-file=.env.tpl -- uv run scripts/migrate.py --step first-pull`. Expected: `upsert_song` ≈ 7,300 owned-playlist recordings + liked-only ones (≈ 400), `upsert_playlist` = 47 owned playlists, `upsert_membership` ≈ 7,800; `skipped` lists the Spotify writes that were NOT made. Then `--step shazam-edges` (≈ 1,021 edges). `life sync && life check`.

- [ ] **Step 5: Backfill derivations**: `op run --env-file=.env.tpl -- uv run scripts/backfill_derive.py`. Runs 3-4 hours (MusicBrainz at 1 req/s). Leave it in the background (`run_in_background`), check progress from its log. When done: `life sync && life sql "SELECT count(*) AS n, sum(first_year IS NULL) AS no_year, sum(json_array_length(deezer_genres)=0) AS no_genre FROM songs WHERE deleted_at IS NULL"` and report.

- [ ] **Step 6: Manual compliance review (gate).** `op run --env-file=.env.tpl -- uv run scripts/review.py` and go section by section with Alex, finance-review style: present a section (counts first, then batches of 25 lines), take decisions, apply each through the `spotify` skill (`spotify_player playlist edit --track-id <id> add|delete <playlist>`; hydrate one `-C` cache for the batch), never through life-data. Expected big sections: `curated_not_liked` ≈ 1,600 (bulk decision: `--step like-pool --yes` after Alex confirms), `shazam_never_liked` ≈ 920 (his triage of the inbox backlog; anything not hearted stays in `My Shazam Tracks`, which is now just a curated playlist), `same_title_diff_isrc` ≈ 140 (versions vs duplicates), `dup_isrc_in_playlist` ≈ 200 (the reconciler will dedupe; confirm once), `bucket_mismatch` (songs whose year disagrees with good stuff/galaxy: fix the year source or accept the move). Write the accepted exceptions into the Notion task from this session.

- [ ] **Step 7: Convert the buckets**: `--step smart-buckets` then `--step rules`. `life sync && life check` (the new invariant will list the mismatches the reconciler is about to fix; that is expected before the first real run).

- [ ] **Step 8: 50s Gold**: run `op run --env-file=.env.tpl -- uv run scripts/create_50s_playlist.py` once (it creates one private playlist), confirm it in the app, then `git rm scripts/create_50s_playlist.py` and commit ("Delete the 50s playlist script: data never lives in code"). Set its row's kind to `curated` on the next pull (default).

- [ ] **Step 9: Activation gate.** `curl` the deployed `/reconcile` with `{"dry_run": true}` (Task 13 step 6 command). Expected: `add_item`/`remove_item` only for the accepted exceptions and the bucket conversions from step 7 (report the counts and a sample of 20 lines to Alex). If anything else appears, go back to step 6. When Alex says go: `--step enable` prints the two commands; Alex runs the `op item edit`, then `just sync-secrets` pushes `RECONCILE_ENABLED=1`; trigger one real run via `/reconcile` with `{"dry_run": false}` and read the summary; then wait for two hourly cron runs (`just logs`) and confirm each reports a small or empty diff and no errors.

- [ ] **Step 10: Close out.** In Notion: complete the tasks absorbed by this project (sync playlists to databases, track following, download backup, dedup, Apple Music tag, 50s playlist) with a one-line note pointing at the spec; retitle "Extend Now Playing → Spotify shortcut..." to "Route captures through the categorizer: v2" and leave it open. Update `projects-map` (`~/.config/agent-config/skills/projects-map/SKILL.md`): Music Sync row → Active, one-liner "Catalog of recordings in life-data (ISRC) + hourly Modal reconciler materializing smart playlists in Spotify; /capture for the Shazam shortcut". Write the memory file `project_music_sync.md` (state, decisions, gotchas: 403 on batch endpoints, `PUT /me/library?uris=`, token 180 days) and its MEMORY.md line. Commit and push agent-config.

## Self-review

- **Spec coverage:** §3 identity → Tasks 8, 10, 12; §4 tables → Task 5; §5.1-5.4 semantics → Task 10 (every §5.3 row has a test), inbox exemption in tests 4 and 10; §5.5 flags → Task 11; §6 rules, description, SQL, invariant → Task 9; §7.1 Modal runtime, activation gate, secrets → Task 13; §7.2 derivations → Tasks 1-4 (+ Task 5 registration probe); §7.3 shortcut → Task 15 (+ Task 12 endpoint); §7.4 agents → Task 16 step 6 and README; §8 layout → file structure + Task 6 deletions; §9 migration → Task 16 with backup (step 3 + `archive.put` in every run) and the review gate (step 6, 9); §10 testing → each task's tests plus the mutation check in Task 10 step 4 and the E2E in Task 15 step 3; requirements 1-18 → reqs 16 (`dry_run`), 17 (`archive.put` before `apply`), 18 (`RECONCILE_ENABLED` gate) are explicit.
- **Placeholders:** `<workspace>` and `<run-id>` are values read from deploy output at execution time, not unknowns; every code step has code. The `migrate.py` body is described by contract rather than written out; its steps are thin wrappers over functions defined in Tasks 7-11, so the implementer has every callee.
- **Type consistency:** `Action.row` dicts use the catalog column names from Task 5; `Membership.id` = `f"{playlist_id}:{isrc}"` everywhere; `rules.evaluate` takes `playlist_ids: dict[name, id]` built in `reconcile.plan` from both mirror and live names; `run.reconcile` is the alias `app.py` calls; `actions.apply(actions, spotify, hub, dry_run, writes=True)` after Task 14; `SpotifyClient.unfollow_playlist` is added in Task 11 and used by `actions.apply`.
