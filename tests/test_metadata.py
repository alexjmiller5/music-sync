from copy import deepcopy
from datetime import datetime, timezone

import pytest

from core.mirror import item_from_raw, load_mirror, pull_live
from core.model import Live, LivePlaylist, Mirror, Song
from tests.test_mirror import FakeHub

NOW = datetime(2026, 9, 12, 12, tzinfo=timezone.utc)
STAMP = "2026-09-12T12:00:00.000Z"
ISRC = "USAAA2600001"
SOURCE = "raw/spotify-pull/dummy.json.gz"


def raw(tid="observed", **changes):
    return {
        "added_at": "2020-01-01T00:00:00Z",
        "track": {
            "id": tid,
            "uri": f"spotify:track:{tid}",
            "external_ids": {"isrc": ISRC},
            "name": "Observed title",
            "artists": [{"name": "Observed artist"}],
            "album": {"name": "Observed release", "release_date": "2018-02-03"},
            "duration_ms": 123456,
            "is_playable": True,
            **changes,
        },
    }


def live(*rows):
    return Live(
        {"P": LivePlaylist("P", "Playlist", None, None, [item_from_raw(r) for r in rows])},
        {},
        {"items": {"P": list(rows)}},
    )


def observe(m=None, state=None, **kwargs):
    from core.metadata import observation_actions

    return observation_actions(
        m or Mirror({}, {}, {}, [], set()),
        state or live(raw()),
        NOW,
        source_ref=SOURCE,
        market="US",
        **kwargs,
    )


def rows(acts, kind="upsert_song"):
    return [a.row for a in acts if a.kind == kind]


def persisted(acts, old=None):
    tables = deepcopy(old or {"songs": [], "provenance": []})
    for kind, table in (("upsert_song", "songs"), ("edge", "provenance")):
        indexed = {r["id"]: r for r in tables[table]}
        for row in rows(acts, kind):
            indexed.setdefault(row["id"], {}).update(row)
        tables[table] = list(indexed.values())
    return tables


def evidence(acts, field):
    return [r for r in rows(acts, "edge") if r["detail"]["field"] == field]


def test_observation_patch_preserves_base_facts_without_input_mutation():
    state = live(raw(linked_from={"id": "original"}))
    before = deepcopy(state)
    acts = observe(state=state)
    assert rows(acts) == [
        {
            "id": ISRC,
            "title": "Observed title",
            "artists": ["Observed artist"],
            "album": "Observed release",
            "album_year": 2018,
            "duration_ms": 123456,
            "spotify_ids": ["observed", "original"],
            "spotify_playable": 1,
        }
    ]
    assert state == before
    edge = evidence(acts, "title")[0]
    assert (edge["from_kind"], edge["rel"], edge["from_ref"], edge["observed_at"]) == (
        "takeout",
        "evidence_of",
        SOURCE,
        STAMP,
    )
    assert edge["detail"]["track_id"] == "observed"
    assert edge["detail"]["value"] == "Observed title"
    assert not {"liked", "first_seen", "first_year"} & rows(acts)[0].keys()


def test_liked_deduplication_keeps_all_observations_and_linked_ids():
    class Spotify:
        def get_playlists(self):
            return []

        def get_liked(self, market):
            return [raw("b"), raw("a", linked_from={"id": "original"})]

    state = pull_live(Spotify(), "US", "me", Mirror({}, {}, {}, [], set()))
    assert len(state.liked) == 1
    patch = rows(observe(state=state))[0]
    assert patch["spotify_ids"] == ["a", "b", "original"]
    assert len(state.raw["liked"]) == 2


def test_missing_facts_preserve_old_values_and_field_attribution():
    tables = persisted(observe())
    m = load_mirror(FakeHub(tables))
    before = deepcopy(m)
    acts = observe(m, live(raw("new", name="New title", artists=[], album=None, duration_ms=None)))
    patch = rows(acts)[0]
    assert patch == {"id": ISRC, "title": "New title", "spotify_ids": ["observed", "new"]}
    assert not evidence(acts, "artists")
    assert not evidence(acts, "album")
    assert not evidence(acts, "album_year")
    assert not evidence(acts, "duration_ms")
    assert m == before


def test_representative_is_stable_and_album_pair_does_not_mix_responses():
    m = load_mirror(FakeHub(persisted(observe(state=live(raw("z"))))))
    state = live(raw("a", name="Other profile"), raw("z", name="Same profile", is_playable=False))
    acts = observe(m, state)
    assert rows(acts)[0]["title"] == "Same profile"
    assert evidence(acts, "title")[0]["detail"]["track_id"] == "z"
    assert "spotify_playable" not in rows(acts)[0]  # a is still positively playable
    assert observe(m, live(*reversed(state.raw["items"]["P"]))) == acts
    partial = observe(m, live(raw("b", album={"name": "Incomplete release"})))
    assert not {"album", "album_year"} & rows(partial)[0].keys()
    assert not evidence(partial, "album") and not evidence(partial, "album_year")
    complete = observe(m, live(raw("b", album={"name": "Another release", "release_date": "2022"})))
    assert rows(complete)[0]["album"] == "Another release"
    assert rows(complete)[0]["album_year"] == 2022


def test_new_representative_prefers_playable_then_track_id():
    acts = observe(
        state=live(
            raw("a", name="Unavailable", is_playable=False), raw("z"), raw("b", name="Winner")
        )
    )
    assert rows(acts)[0]["title"] == "Winner"
    assert evidence(acts, "title")[0]["detail"]["track_id"] == "b"


@pytest.mark.parametrize("playable, expected", [(None, None), (False, 0), (True, 1)])
def test_availability_keeps_unknown_distinct_from_false(playable, expected):
    r = raw(is_playable=playable)
    if playable is None:
        del r["track"]["is_playable"]
    patch = rows(observe(state=live(r)))[0]
    assert patch.get("spotify_playable") == expected
    assert ("spotify_playable" in patch) is (expected is not None)


def test_unavailable_alias_does_not_downgrade_other_observed_playable_alias():
    m = load_mirror(FakeHub(persisted(observe(state=live(raw("good"))))))
    acts = observe(m, live(raw("bad", is_playable=False)))
    assert "spotify_playable" not in rows(acts)[0]
    negative = evidence(acts, "spotify_playable")[0]
    assert negative["detail"]["track_id"] == "bad"
    assert negative["detail"]["value"] is False
    assert negative["detail"]["market"] == "US"


def test_fill_only_unions_aliases_without_relabeling_existing_facts_or_evidence():
    tables = persisted(observe())
    tables["provenance"].append(
        {
            "id": "legacy",
            "from_kind": "derivation",
            "rel": "derived_from",
            "to_kind": "songs",
            "to_ref": ISRC,
        }
    )
    m = load_mirror(FakeHub(tables))
    state = live(raw("a", name="Conflicting", album={"name": "Conflict", "release_date": "2025"}))
    acts = observe(m, state, fill_only=True)
    assert rows(acts) == [{"id": ISRC, "spotify_ids": ["observed", "a"]}]
    assert not evidence(acts, "title") and not evidence(acts, "album")
    result = persisted(acts, tables)
    assert next(r for r in result["provenance"] if r["id"] == "legacy") == tables["provenance"][-1]
    assert observe(load_mirror(FakeHub(result)), state, fill_only=True) == []


def test_fill_only_album_pair_does_not_overwrite_half_existing_pair():
    m = Mirror({ISRC: Song(ISRC, 0, None, STAMP, album="Existing")}, {}, {}, [], set())
    acts = observe(m, fill_only=True)
    assert not {"album", "album_year"} & rows(acts)[0].keys()


def test_same_value_refresh_updates_only_directly_observed_sources():
    tables = persisted(observe())
    m = load_mirror(FakeHub(tables))
    from core.metadata import observation_actions

    acts = observation_actions(
        m, live(raw(album=None)), NOW, source_ref="raw/spotify-pull/new.json.gz", market="US"
    )
    assert not rows(acts)
    assert evidence(acts, "title")[0]["from_ref"] == "raw/spotify-pull/new.json.gz"
    assert not evidence(acts, "album")
    assert not evidence(acts, "album_year")


def test_without_retained_source_does_not_invent_provenance():
    from core.metadata import observation_actions

    acts = observation_actions(Mirror({}, {}, {}, [], set()), live(raw()), NOW)
    assert rows(acts)
    assert not rows(acts, "edge")


def test_negative_observation_only_marks_recording_unavailable_when_all_aliases_known_false():
    m = load_mirror(FakeHub(persisted(observe(state=live(raw("one"), raw("two"))))))
    acts = observe(m, live(raw("one", is_playable=False)))
    assert not rows(acts)  # the second alias retains its direct positive evidence
    both = observe(m, live(raw("one", is_playable=False), raw("two", is_playable=False)))
    assert rows(both) == [{"id": ISRC, "spotify_playable": 0}]


def test_all_alias_evidence_keeps_its_actual_response_identity():
    acts = observe(state=live(raw("b", linked_from={"id": "original"}), raw("a")))
    assert [
        (r["detail"]["value"], r["detail"]["track_id"]) for r in evidence(acts, "spotify_ids")
    ] == [
        ("a", "a"),
        ("b", "b"),
        ("original", "b"),
    ]
