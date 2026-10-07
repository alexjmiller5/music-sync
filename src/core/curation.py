"""Curation transitions from complete observations, independent of task delivery.

State belongs to the owning app's recovery store. Raw archive references retain
before/after evidence. A proposed like is never evidence that it happened.
"""

import copy
import hashlib


def state_key(workspace):
    return "music-sync/curation/" + hashlib.sha256(workspace.encode()).hexdigest() + ".json.gz"


def advance(previous, liked, members, source_ref, *, own_likes=frozenset()):
    if previous is not None:
        try:
            if previous["version"] != 1:
                raise ValueError
            baseline = previous["baseline"]
            before_liked = set(baseline["liked"]) | set(baseline["own_likes"])
            before_members = {tuple(pair) for pair in baseline["curated"]}
            if any(len(pair) != 2 for pair in before_members):
                raise ValueError
            exceptions = copy.deepcopy(previous["exceptions"])
            if not isinstance(exceptions, dict) or not isinstance(baseline["source_ref"], str):
                raise ValueError
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("invalid retained curation state") from exc
    else:
        baseline, before_liked, before_members, exceptions = None, set(), set(), {}
    unliked = before_liked - liked
    for isrc in sorted(unliked & {i for _, i in before_members} & {i for _, i in members}):
        exceptions.setdefault(
            isrc,
            {
                "reason": "unliked_while_curated",
                "before_ref": baseline["source_ref"],
                "after_ref": source_ref,
                "prior_like_origin": "music-sync" if isrc in baseline["own_likes"] else "observed",
            },
        )
    # The initial observation is a baseline, not an approved migration. Unknown
    # playlist classification must be excluded by the caller, never guessed here.
    pending = set(previous.get("pending_likes", [])) if previous else set()
    pending &= {i for _, i in members}
    to_like = (
        ({i for _, i in members - before_members} | pending) - liked - unliked - set(exceptions)
        if baseline is not None
        else set()
    )
    return to_like, {
        "version": 1,
        "pending_likes": sorted(to_like),
        "baseline": {
            "source_ref": source_ref,
            "liked": sorted(liked),
            "curated": [list(pair) for pair in sorted(members)],
            "own_likes": sorted(own_likes),
        },
        "exceptions": exceptions,
    }
