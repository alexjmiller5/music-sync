"""Workspace-owned unlike exceptions; persisted before observation baselines advance."""

import gzip
import json

from core import archive


def key(settings):
    return archive.pending_key(settings).replace("pending-reconcile", "review-state")


def load(settings) -> dict:
    raw = archive.get(settings, key(settings))
    value = json.loads(gzip.decompress(raw)) if raw is not None else {}
    if not isinstance(value, dict) or any(not isinstance(v, dict) for v in value.values()):
        raise ValueError("invalid persisted review state")
    return value


def record(settings, row: dict) -> None:
    state = load(settings)
    if row["id"] in state:
        return
    state[row["id"]] = row
    archive.put(settings, key(settings), gzip.compress(json.dumps(state, sort_keys=True).encode()))
