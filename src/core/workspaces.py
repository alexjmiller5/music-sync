"""Workspaces: whose Spotify, catalog hub and Notion a capture or reconcile
acts for.

`default` is the operator's own and is configured entirely by env
(`Settings()`), exactly as before workspaces existed. Any other workspace is a
record in the app's own R2 state (one registry object) that overrides the
per-user Settings fields: its Spotify refresh token (from Connect Spotify,
core/spotify_connect.py), its life-data hub, its Notion flags target, its
limits. The app's Spotify developer app and R2 bucket are app infrastructure
every workspace shares. Adding a person = one record + a Connect Spotify link
+ capture clients bound to it; no user database, no code change.
"""

import gzip
import json
import os
import re

from core import archive
from core.config import Settings

REGISTRY_KEY = "music-sync/workspaces.json.gz"
DEFAULT = "default"
USER_FIELDS = {
    "spotify_refresh_token": str,
    "spotify_market": str,
    "life_hub_url": str,
    "life_hub_token": str,
    "notion_token": str,
    "notion_tasks_data_source_id": str,
    "notion_project_page_id": str,
    "inbox_cap": int,
    "undo_days": int,
}
SECRET_FIELDS = {"spotify_refresh_token", "life_hub_token", "notion_token"}
# Not a Settings field: the default workspace keeps the RECONCILE_ENABLED env gate.
FLAGS = {"reconcile_enabled": lambda v: str(v).lower() in {"1", "true", "yes"}}
ID = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")


class InvalidWorkspace(ValueError):
    pass


class UnknownWorkspace(KeyError):
    pass


def _registry(settings: Settings) -> dict:
    data = archive.get(settings, REGISTRY_KEY)
    return json.loads(gzip.decompress(data)) if data else {}


def save(settings: Settings, workspace: str, **fields) -> None:
    """Create or update a non-default workspace; given fields replace, others are kept."""
    if workspace == DEFAULT or not ID.fullmatch(workspace or ""):
        raise InvalidWorkspace(
            f"workspace id {workspace!r}: lowercase letters, digits, dashes; not 'default'"
        )
    allowed = {**USER_FIELDS, **FLAGS}
    if bad := sorted(set(fields) - set(allowed)):
        raise InvalidWorkspace(f"not workspace fields: {bad}; allowed {sorted(allowed)}")
    registry = _registry(settings)
    record = registry.get(workspace, {})
    record.update({k: allowed[k](v) for k, v in fields.items()})
    registry[workspace] = record
    archive.put(
        settings, REGISTRY_KEY, gzip.compress(json.dumps(registry, sort_keys=True).encode())
    )


def settings_for(base: Settings, workspace: str) -> Settings:
    record = _registry(base).get(workspace)
    if record is None and workspace != DEFAULT:
        raise UnknownWorkspace(workspace)
    update = {k: v for k, v in (record or {}).items() if k in USER_FIELDS}
    return base.model_copy(update={**update, "workspace": workspace})


def reconcile_enabled(settings: Settings) -> bool:
    """RECONCILE_ENABLED=1 is the app-wide switch; a non-default workspace
    also needs its own reconcile_enabled flag."""
    if os.environ.get("RECONCILE_ENABLED") != "1":
        return False
    if settings.workspace == DEFAULT:
        return True
    return _registry(settings).get(settings.workspace, {}).get("reconcile_enabled") is True


def ids(settings: Settings) -> list[str]:
    return [DEFAULT, *sorted(_registry(settings))]


def summary(settings: Settings, workspace: str) -> dict:
    registry = _registry(settings)
    if workspace not in registry:
        raise UnknownWorkspace(workspace)
    record = registry[workspace]
    return {
        "workspace": workspace,
        "set": sorted(record),
        **{k: v for k, v in record.items() if k not in SECRET_FIELDS},
    }
