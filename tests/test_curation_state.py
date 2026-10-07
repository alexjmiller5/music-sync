import copy

import pytest

from core import curation


def observe(
    previous=None, *, liked=(), members=(), source="raw/spotify-pull/current.json.gz", own=()
):
    return curation.advance(previous, set(liked), set(members), source, own_likes=set(own))


def test_first_observation_does_not_silently_bootstrap_existing_curated():
    likes, state = observe(members={("P", "A")})
    assert likes == set()
    assert state["baseline"]["curated"] == [["P", "A"]]
    assert state["exceptions"] == {}


def test_new_curated_add_likes_but_existing_unliked_does_not():
    _, prior = observe(members={("P", "A")})
    before = copy.deepcopy(prior)
    likes, state = observe(prior, members={("P", "A"), ("P", "B")})
    assert likes == {"B"}
    assert prior == before
    assert state["baseline"]["liked"] == []  # proposal is not an observed like


def test_unlike_keeps_one_durable_exception_and_blocks_other_new_playlist():
    _, prior = observe(liked={"A"}, members={("P", "A")}, source="raw/spotify-pull/before.json.gz")
    likes, state = observe(prior, members={("P", "A")})
    assert not likes
    assert set(state["exceptions"]) == {"A"}
    exception = copy.deepcopy(state["exceptions"]["A"])
    assert exception["before_ref"] == "raw/spotify-pull/before.json.gz"
    assert exception["after_ref"] == "raw/spotify-pull/current.json.gz"
    likes, state = observe(
        state, members={("P", "A"), ("Q", "A")}, source="raw/spotify-pull/next.json.gz"
    )
    assert not likes
    assert state["exceptions"]["A"] == exception


def test_liked_but_not_previously_curated_does_not_claim_explicit_unlike():
    _, prior = observe(liked={"A"})
    likes, state = observe(prior, members={("P", "A")})
    assert not likes  # conflicting gestures are held conservatively
    assert not state["exceptions"]


def test_confirmed_own_like_is_distinguished_from_proposal():
    _, prior = observe(members={("P", "A")}, own={"A"})
    assert prior["baseline"]["own_likes"] == ["A"]
    likes, state = observe(prior, members={("P", "A")})
    assert not likes
    assert state["exceptions"]["A"]["prior_like_origin"] == "music-sync"


def test_curated_removal_alone_is_not_unlike_exception():
    _, prior = observe(liked={"A"}, members={("P", "A")})
    likes, state = observe(prior, liked={"A"})
    assert not likes and not state["exceptions"]


@pytest.mark.parametrize("prior", [{}, {"version": 9}, {"version": 1, "baseline": None}])
def test_invalid_retained_state_fails_closed(prior):
    with pytest.raises(ValueError, match="curation"):
        observe(prior)
