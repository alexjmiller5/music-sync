"""Pure observation patches and field evidence, independent of enrichment and gestures."""

import json
from collections import defaultdict
from dataclasses import asdict, replace
from datetime import datetime, timezone

from core.model import Action, Live, LiveItem, Mirror
from core.hub import HubError

DISPLAY_FIELDS = ("title", "artists", "album", "album_year", "duration_ms")


def require_observed_contract(hub) -> None:
    properties = {
        p["col"]: p
        for p in hub.catalog()["properties"]
        if p.get("tbl") == "songs" and not p.get("deleted_at")
    }
    for col in (*DISPLAY_FIELDS, "spotify_ids", "spotify_playable"):
        if col not in properties or properties[col].get("derived_by") is not None:
            raise HubError(
                f"songs.{col} must exist without a derivation binding; live cutover required"
            )


def present(value):
    return value is not None and value != "" and value != []


def observations(live: Live) -> dict[str, list[LiveItem]]:
    grouped = defaultdict(list)
    items = [*live.observations, *live.liked.values()]
    items.extend(it for p in live.playlists.values() for it in p.items or [])
    for it in items:
        if it.isrc and not it.is_local:
            grouped[it.isrc].append(it)
    return grouped


def evidence_by_song(mirror: Mirror) -> dict[str, list[dict]]:
    grouped = defaultdict(list)
    for row in mirror.observations:
        grouped[row["to_ref"]].append(row)
    return grouped


def availability(
    evidence: list[dict],
    items: list[LiveItem],
    market: str | None,
) -> dict[str, bool]:
    """Positive replacement evidence is per alias and market, never a legacy row flag."""
    known = {
        r["detail"]["track_id"]: r["detail"]["value"]
        for r in evidence
        if r["detail"]["field"] == "spotify_playable" and r["detail"].get("market") == market
    }
    current = {}
    for it in items:
        if it.track_id and it.playable is not None:
            # Conflicting responses for one alias in this pull are not a safe replacement.
            current[it.track_id] = current.get(it.track_id, True) and it.playable
    return known | current


def observation_actions(
    mirror: Mirror,
    live: Live,
    now: datetime,
    *,
    source_ref: str | None = None,
    market: str | None = None,
    fill_only: bool = False,
) -> list[Action]:
    stamp = now.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    acts = []
    prior = {r["id"]: r for r in mirror.observations}
    by_song = evidence_by_song(mirror)
    for isrc, items in sorted(observations(live).items()):
        song = mirror.songs.get(isrc)
        existing = asdict(song) if song else {}
        profiles = [r for r in by_song[isrc] if r["detail"]["field"] in DISPLAY_FIELDS]
        previous = max(
            profiles,
            key=lambda r: (r["detail"]["observed_at"], r["detail"]["field"] == "title", r["id"]),
            default=None,
        )
        representative = previous["detail"]["track_id"] if previous else None
        chosen = min(
            items,
            key=lambda it: (
                it.track_id != representative if representative else True,
                it.playable is not True,
                it.track_id or "",
                json.dumps(asdict(it), sort_keys=True),
            ),
        )
        observed = {
            "title": chosen.name,
            "artists": chosen.artists,
            "album": chosen.album,
            "album_year": chosen.album_year,
            "duration_ms": chosen.duration_ms,
        }
        # Keep an initial partial release, but never mix it with a later observation.
        has_album = present(existing.get("album")) or present(existing.get("album_year"))
        if has_album and (fill_only or not (present(chosen.album) and present(chosen.album_year))):
            observed.pop("album")
            observed.pop("album_year")
        patch = {
            key: value
            for key, value in observed.items()
            if present(value)
            and value != existing.get(key)
            and (not fill_only or not present(existing.get(key)))
        }
        old_ids = existing.get("spotify_ids", [])
        alias_sources = {}
        for it in sorted(items, key=lambda it: (it.track_id or "", it.linked_from_id or "")):
            for alias in (it.track_id, it.linked_from_id):
                if alias:
                    alias_sources.setdefault(alias, it.track_id)
        ids = list(dict.fromkeys([*old_ids, *sorted(alias_sources)]))
        if ids != old_ids:
            patch["spotify_ids"] = ids
        available = availability(by_song[isrc], items, market)
        playable = (
            1
            if any(available.values())
            else 0
            if ids and all(available.get(tid) is False for tid in ids)
            else None
        )
        if (
            playable is not None
            and playable != existing.get("spotify_playable")
            and (not fill_only or existing.get("spotify_playable") is None)
        ):
            patch["spotify_playable"] = playable
        if patch:
            acts.append(Action("upsert_song", isrc=isrc, row={"id": isrc, **patch}))
        if not source_ref:
            continue

        def evidence(field, value, track_id, suffix=""):
            row = {
                "id": f"spotify-observation:{isrc}:{field}{suffix}",
                "from_kind": "takeout",
                "from_ref": source_ref,
                "to_kind": "songs",
                "to_ref": isrc,
                "rel": "evidence_of",
                "asserted_by": "music-sync",
                "detail": {
                    "observed_at": stamp,
                    "kind": "spotify_observation",
                    "field": field,
                    "value": value,
                    "track_id": track_id,
                    "market": market,
                },
            }
            old = prior.get(row["id"], {})
            if (fill_only and old) or all(old.get(k) == v for k, v in row.items()):
                return
            acts.append(Action("edge", isrc=isrc, row=row))

        for field, value in observed.items():
            if present(value) and (not fill_only or field in patch):
                evidence(field, value, chosen.track_id)
        for alias, track_id in sorted(alias_sources.items()):
            if not fill_only or alias not in old_ids:
                evidence("spotify_ids", alias, track_id, f":{alias}")
        current = availability([], items, market)
        for track_id, value in sorted(current.items()):
            evidence("spotify_playable", value, track_id, f":{market or ''}:{track_id}")
    return acts


def merge_actions(actions: list[Action], observed: list[Action]) -> list[Action]:
    """Fold metadata into the first write, preserving later gesture patches and input rows."""
    result = [replace(a, row=dict(a.row) if a.row is not None else None) for a in actions]
    first = {}
    for a in result:
        if a.kind == "upsert_song":
            first.setdefault(a.isrc, a)
    for a in observed:
        if a.kind == "upsert_song" and a.isrc in first:
            first[a.isrc].row.update(a.row)
        else:
            result.append(a)
    return result
