"""Playlist covers: soma holds the cover; Music Sync applies it only when it changed."""

import base64
import hashlib

import pytest

from core import covers
from core.model import Mirror, Playlist

JPEG = b"\xff\xd8\xff\xe0" + b"cover" * 10
SHA = hashlib.sha256(JPEG).hexdigest()
CUSTOM = "https://image-cdn-ak.spotifycdn.com/image/ab67706c0000da84" + "1" * 24
SAME_ELSEWHERE = "https://i.scdn.co/image/ab67706c0000bebb" + "1" * 24  # other host and size
OTHER = "https://image-cdn-ak.spotifycdn.com/image/ab67706c0000da84" + "2" * 24
MOSAIC = "https://mosaic.scdn.co/640/" + "ab67616d0000b273" + "3" * 24 * 4
ALBUM = "https://i.scdn.co/image/ab67616d0000b273" + "4" * 24


def playlist(pid="P", cover_file=None, cover_sha256=None, cover_applied=None):
    return Playlist(
        pid, "mood", "curated", None, None, None, 1, None, cover_file, cover_sha256, cover_applied
    )


def covered(applied):
    return playlist(cover_file=covers.key_for("P", SHA), cover_sha256=SHA, cover_applied=applied)


def test_a_cover_is_uploaded_only_when_it_changed_in_soma_or_in_spotify():
    assert covers.decide(playlist(), CUSTOM) is None  # no cover in soma: left alone
    assert covers.decide(covered(None), CUSTOM) == "upload"  # never applied
    assert covers.decide(covered({"sha256": "0" * 64, "spotify_url": CUSTOM}), CUSTOM) == "upload"
    applied = {"sha256": SHA, "spotify_url": CUSTOM}
    assert covers.decide(covered(applied), CUSTOM) is None
    assert covers.decide(covered(applied), SAME_ELSEWHERE) is None  # same image, other CDN URL
    assert covers.decide(covered(applied), OTHER) == "upload"  # changed in Spotify: restore
    assert covers.decide(covered(applied), None) == "upload"
    # Uploaded, but Spotify still showed the old image right after (it processes uploads
    # asynchronously): a later run adopts the new image, or uploads again if it never came.
    pending = {"sha256": SHA, "spotify_url": None, "replaced_url": OTHER}
    assert covers.decide(covered(pending), CUSTOM) == "adopt"
    assert covers.decide(covered(pending), OTHER) == "upload"


def test_applied_records_the_image_observed_right_after_the_upload():
    assert covers.applied(SHA, OTHER, CUSTOM) == {"sha256": SHA, "spotify_url": CUSTOM}
    assert covers.applied(SHA, OTHER, OTHER) == {
        "sha256": SHA,
        "spotify_url": None,
        "replaced_url": OTHER,
    }


def test_only_a_jpeg_spotify_accepts_is_a_cover():
    assert covers.validate(JPEG) is None
    assert "JPEG" in covers.validate(b"\x89PNG\r\n\x1a\n")
    largest = 256 * 1024 * 3 // 4  # base64 grows 4/3; Spotify takes at most 256 KB of it
    assert covers.validate(JPEG[:4] + b"x" * (largest - 4)) is None
    assert "256 KB" in covers.validate(JPEG[:4] + b"x" * (largest - 3))
    assert len(base64.b64encode(JPEG[:4] + b"x" * (largest - 4))) == 256 * 1024


def test_only_custom_images_are_kept_as_covers():
    assert covers.is_custom(CUSTOM)
    assert not covers.is_custom(MOSAIC) and not covers.is_custom(ALBUM)
    assert not covers.is_custom(None)


class Hub:
    def __init__(self):
        self.files, self.pushed = {}, []

    def put_file(self, key, data, content_type):
        self.files[key] = (data, content_type)

    def push(self, table, rows):
        self.pushed.append((table, rows))


def mirror(**playlists):
    return Mirror({}, {"P": playlist(), **playlists}, {}, [], set())


def test_setting_a_cover_retains_the_file_and_records_it_on_the_playlist():
    hub = Hub()
    row = covers.set_cover(hub, mirror(), "P", JPEG)
    key = f"raw/spotify-covers/P/{SHA}.jpg"
    assert hub.files == {key: (JPEG, "image/jpeg")}
    assert row == {"id": "P", "cover_file": key, "cover_sha256": SHA}
    assert hub.pushed == [("playlists", [row])]
    for pid, data in (("UNKNOWN", JPEG), ("P", b"not a jpeg")):
        with pytest.raises(covers.CoverError):
            covers.set_cover(hub, mirror(), pid, data)
    assert len(hub.pushed) == 1


def listed(pid, url, owner="me"):
    return {"id": pid, "owner": {"id": owner}, "images": [{"url": url}] if url else None}


def test_seeding_keeps_each_owned_playlists_current_custom_image():
    m = mirror(
        MOS=playlist("MOS"),
        SET=covered({"sha256": SHA, "spotify_url": CUSTOM}),
        NONE=playlist("NONE"),
        THEIRS=playlist("THEIRS"),  # followed, not owned
    )
    listing = [
        listed("P", CUSTOM),
        listed("MOS", MOSAIC),  # a generated mosaic is not a cover
        listed("SET", OTHER),  # already has a cover in soma
        listed("NONE", None),
        listed("THEIRS", CUSTOM, owner="else"),
        listed("NEW", CUSTOM),  # not classified yet
    ]
    fetched = []

    def fetch(url):
        fetched.append(url)
        return JPEG

    hub = Hub()
    preview = covers.seed(hub, m, listing, "me", fetch)
    assert preview["dry_run"] and preview["seed"] == [{"id": "P", "name": "mood", "url": CUSTOM}]
    assert hub.files == {} and hub.pushed == [] and fetched == []
    out = covers.seed(hub, m, listing, "me", fetch, apply=True)
    key = f"raw/spotify-covers/P/{SHA}.jpg"
    assert fetched == [CUSTOM] and hub.files == {key: (JPEG, "image/jpeg")}
    # Seeded from what Spotify already shows, so the next reconcile uploads nothing.
    row = {
        "id": "P",
        "cover_file": key,
        "cover_sha256": SHA,
        "cover_applied": {"sha256": SHA, "spotify_url": CUSTOM},
    }
    assert hub.pushed == [("playlists", [row])] and out["seeded"] == ["P"]
    assert covers.decide(playlist(**{k: v for k, v in row.items() if k != "id"}), CUSTOM) is None
