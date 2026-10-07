"""References to individual source occurrences, never reconstructed event dates."""

import hashlib
import json

from core.mirror import item_from_raw
from core.model import Action


def occurrence_evidence(raw, source_ref, previous):
    actions, fingerprints = [], {}
    for pid, items in sorted(raw.get("items", {}).items()):
        digest = hashlib.sha256(
            json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        fingerprints[pid] = digest
        if previous.get(pid) == digest:
            continue
        # JSON Pointer escapes identify the original position even for duplicates,
        # absent dates and different aliases. The ID names this observation only;
        # it never claims that separate snapshots prove separate add events.
        escaped = pid.replace("~", "~0").replace("/", "~1")
        for index, item in enumerate(items):
            isrc = item_from_raw(item).isrc
            if not isrc:
                continue  # unsupported items remain in the retained raw archive
            locator = f"/items/{escaped}/{index}"
            identity = hashlib.sha256(json.dumps([source_ref, locator, isrc]).encode()).hexdigest()
            actions.append(
                Action(
                    "insert_edge",
                    isrc=isrc,
                    row={
                        "id": f"spotify-occurrence:{identity}",
                        "from_kind": "takeout",
                        "from_ref": source_ref,
                        "to_kind": "songs",
                        "to_ref": isrc,
                        "rel": "evidence_of",
                        "field": None,
                        "asserted_by": "music-sync",
                        "detail": {"kind": "spotify_occurrence_observation", "locator": locator},
                    },
                )
            )
    return actions, fingerprints
