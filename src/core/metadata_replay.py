"""Fill catalog metadata from one retained observation, using the worker's durable intent."""

import gzip
import json
import re
from dataclasses import asdict
from datetime import datetime, timezone
from typing import NotRequired, TypedDict

from pydantic import TypeAdapter

from core import actions, archive, metadata, mirror
from core.hub import Hub
from core.model import Action, Live


class _Artist(TypedDict, total=False):
    name: str | None


class _Album(_Artist, total=False):
    release_date: str | None


class _Identity(TypedDict, total=False):
    id: str | None


class _Track(_Identity, total=False):
    uri: str | None
    name: str | None
    artists: list[_Artist] | None
    album: _Album | None
    external_ids: dict[str, str | None] | None
    linked_from: _Identity | None
    duration_ms: int | None
    is_playable: bool | None
    is_local: bool | None


class _Item(TypedDict, total=False):
    added_at: str | None
    item: _Track | None
    track: _Track | None


class _Pull(TypedDict):
    playlists: list[dict]
    items: dict[str, list[_Item]]
    liked: list[_Item]
    market: NotRequired[str | None]


class _Capture(TypedDict):
    playlist_id: str
    items: list[_Item]
    resolved_track: NotRequired[_Track]
    market: NotRequired[str | None]


def validate_request(archive_key, observed_at, dry_run=True) -> datetime:
    if (
        not isinstance(archive_key, str)
        or not re.fullmatch(r"raw/spotify-(pull|capture)/[A-Za-z0-9_./-]+\.json\.gz", archive_key)
        or any(part in ("", ".", "..") for part in archive_key.split("/"))
    ):
        raise ValueError("archive_key must be a normalized retained Spotify .json.gz object key")
    if type(dry_run) is not bool:
        raise ValueError("dry_run must be a JSON boolean")
    if not isinstance(observed_at, str):
        raise ValueError("observed_at must be an ISO timestamp with a timezone")
    observed_time = datetime.fromisoformat(observed_at)
    if observed_time.tzinfo is None:
        raise ValueError("observed_at must include a timezone")
    return observed_time.astimezone(timezone.utc)


def _read_live(settings, key):
    saved = archive.get(settings, key)
    if saved is None:
        raise FileNotFoundError(f"Retained archive not found: {key}")
    try:
        raw = json.loads(gzip.decompress(saved))
    except (OSError, EOFError, ValueError) as exc:
        raise ValueError("Invalid gzip/JSON archive") from exc
    capture = key.startswith("raw/spotify-capture/")
    body = TypeAdapter(_Capture if capture else _Pull).validate_python(raw, strict=True)
    if body.get("market") is not None and not re.fullmatch(r"[A-Z]{2}", body["market"]):
        raise ValueError("Retained market must be a country code or null")
    rows = (
        body["items"]
        if capture
        else [
            *body["liked"],
            *(row for items in body["items"].values() for row in items),
        ]
    )
    if capture and "resolved_track" in body:
        rows = [*rows, {"track": body["resolved_track"]}]
    if any(not (row.keys() & {"item", "track"}) for row in rows):
        raise ValueError("Archive items must contain item or track")
    return Live({}, {}, raw, [mirror.item_from_raw(row) for row in rows])


def _plan(m, live, observed_time, key, market):
    observed = metadata.observations(live)
    plan = metadata.observation_actions(
        m,
        live,
        observed_time,
        source_ref=key,
        market=market,
        fill_only=True,
    )
    patches = {a.isrc: a.row for a in plan if a.kind == "upsert_song" and a.isrc in m.songs}
    # Evidence only accompanies actual fills. Existing facts keep their attribution.
    plan = [
        a
        for a in plan
        if a.isrc in patches
        and (a.kind == "upsert_song" or a.row["detail"]["field"] in patches[a.isrc])
    ]
    outcomes = []
    for isrc in sorted(m.songs.keys() | observed.keys()):
        song, items = m.songs.get(isrc), observed.get(isrc, [])
        conflicts = []
        if song and items:
            existing = asdict(song)
            for item in items:
                values = {
                    "title": item.name,
                    "artists": item.artists,
                    "album": item.album,
                    "album_year": item.album_year,
                    "duration_ms": item.duration_ms,
                }
                for field, value in values.items():
                    retained = existing[field]
                    paired = field in ("album", "album_year") and (
                        metadata.present(song.album) or metadata.present(song.album_year)
                    )
                    if (
                        metadata.present(value)
                        and value != retained
                        and (metadata.present(retained) or paired)
                    ):
                        conflict = {"field": field, "existing": retained, "observed": value}
                        if conflict not in conflicts:
                            conflicts.append(conflict)
            available = metadata.availability([], items, market)
            aliases = set(song.spotify_ids) | available.keys()
            playable = (
                1
                if any(available.values())
                else 0
                if aliases and all(available.get(tid) is False for tid in aliases)
                else None
            )
            if (
                playable is not None
                and song.spotify_playable is not None
                and playable != song.spotify_playable
            ):
                conflicts.append(
                    {
                        "field": "spotify_playable",
                        "existing": song.spotify_playable,
                        "observed": playable,
                        "market": market,
                    }
                )
        status = (
            "missing_source"
            if not song or not items
            else "conflicting"
            if conflicts
            else "recovered"
            if isrc in patches
            else "missing_source"
            if any(not metadata.present(existing[field]) for field in metadata.DISPLAY_FIELDS)
            else "already_present"
        )
        outcomes.append({"isrc": isrc, "status": status, "conflicts": conflicts})
    return plan, outcomes


def run(settings, archive_key: str, observed_at: str, *, dry_run: bool = True, hub=None) -> dict:
    observed_time = validate_request(archive_key, observed_at, dry_run)
    stamp = observed_time.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    saved = archive.get(settings, archive.PENDING_KEY)
    pending = json.loads(gzip.decompress(saved)) if saved else None
    identity = {"intent": "metadata_replay", "archive_key": archive_key, "observed_at": stamp}
    if pending and any(pending.get(k) != v for k, v in identity.items()):
        raise RuntimeError("Pending recovery must finish with its original operation and source")
    hub = hub or Hub(settings.life_hub_url, settings.life_hub_token)
    if not dry_run:
        metadata.require_observed_contract(hub)
    if pending:
        plan = [Action(**a) for a in pending["planned"]]
        outcomes = pending["outcomes"]
    else:
        live = _read_live(settings, archive_key)
        hub = hub or Hub(settings.life_hub_url, settings.life_hub_token)
        plan, outcomes = _plan(
            mirror.load_mirror(hub),
            live,
            observed_time,
            archive_key,
            live.raw.get("market"),
        )

    def checkpoint(remaining):
        data = (
            {
                **identity,
                "planned": [asdict(a) for a in plan],
                "operations": remaining,
                "writes": False,
                "outcomes": outcomes,
            }
            if remaining
            else None
        )
        archive.put(settings, archive.PENDING_KEY, gzip.compress(json.dumps(data).encode()))

    if plan:
        hub = hub or Hub(settings.life_hub_url, settings.life_hub_token)
        log = actions.apply(
            plan,
            None,
            hub,
            dry_run,
            writes=False,
            checkpoint=checkpoint,
            pending=pending["operations"] if pending else None,
        )
    else:
        log = actions.RunLog(dry_run=dry_run)
    rows = [dict(row) for row in outcomes]
    if log.errors:
        affected = {a.isrc for a in plan}
        rows = [{**row, "status": "failed"} if row["isrc"] in affected else row for row in rows]
    return {
        **{
            name: sum(row["status"] == name for row in rows)
            for name in (
                "recovered",
                "already_present",
                "conflicting",
                "missing_source",
                "failed",
            )
        },
        "dry_run": dry_run,
        "rows": rows,
        "planned": log.planned,
        "applied": dict(log.applied),
        "errors": [{"code": "apply_failed", "message": error} for error in log.errors],
    }
