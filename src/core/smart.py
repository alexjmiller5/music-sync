"""Supported playlist configuration: kind, rule, pinned and expiry, keyed by stable ID.

This is the only writer of `playlists.kind` / `rule` / `pinned` / `expires_at`; it runs
in the serialized worker (`just rules ...`). Rules reference playlists by ID, and the
hub's `playlists-rule-iff-smart` invariant rejects a smart row without a valid rule.
"""

from core import rules
from core.model import Mirror


class ConfigError(ValueError):
    pass


def row(pid, name, rule, today, names, pinned=1, expires_at=None) -> dict:
    return {
        "id": pid,
        "name": name,
        "kind": "smart",
        "rule": rule,
        "description": rules.describe(rule, today, names),
        "pinned": pinned,
        "expires_at": expires_at,
        "deleted_at": None,
    }


def _check(rule, mirror: Mirror):
    names = {}
    for p in mirror.playlists.values():
        names[p.name] = None if p.name in names else p.id
    try:
        rules.to_sql(rule, names, set(mirror.playlists))
    except rules.RuleError as exc:
        raise ConfigError(str(exc)) from exc


def listing(mirror: Mirror) -> list[dict]:
    return [
        {"id": p.id, "name": p.name, "kind": p.kind, "rule": p.rule, "pinned": p.pinned}
        for p in sorted(mirror.playlists.values(), key=lambda p: (p.kind, p.name))
    ]


def configure(body: dict, *, hub, mirror: Mirror, live_names: dict, today: str, spotify=None):
    """One configuration change. Returns the pushed playlists row."""
    action = body.get("action")
    names = {p.id: p.name for p in mirror.playlists.values()}
    if action == "create":
        rule = body.get("rule")
        _check(rule, mirror)
        if body["name"] in live_names.values():
            raise ConfigError("an owned playlist already has this name")
        created = spotify.create_playlist(
            body["name"], rules.describe(rule, today, names), public=False
        )
        out = row(
            created["id"],
            body["name"],
            rule,
            today,
            names,
            body.get("pinned", 1),
            body.get("expires_at"),
        )
    else:
        pid = body.get("playlist_id")
        if pid not in live_names:
            raise ConfigError("unknown or unowned playlist id")
        if action == "smart":
            _check(body.get("rule"), mirror)
            out = row(
                pid,
                live_names[pid],
                body["rule"],
                today,
                names,
                body.get("pinned", 1),
                body.get("expires_at"),
            )
        elif action in ("curated", "clear"):
            existing = mirror.playlists.get(pid)
            if action == "clear" and (existing is None or existing.kind != "smart"):
                raise ConfigError("only a smart playlist can be cleared")
            if existing and existing.kind == "inbox":
                raise ConfigError("the inbox keeps its kind")
            out = {
                "id": pid,
                "name": live_names[pid],
                "kind": "curated",
                "rule": None,
                "pinned": 1,
                "expires_at": None,
                "deleted_at": None,
            }
        else:
            raise ConfigError("action must be create, smart, curated or clear")
    hub.push("playlists", [out])
    return out
