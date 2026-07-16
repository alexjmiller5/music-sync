"""Read Spotify GDPR 'Account Data' export zips directly — no unzip step."""

import json
import zipfile
from pathlib import Path

import structlog

from core.models import FollowedArtist, Playlist, Song

log = structlog.get_logger()

_PREFIX = "Spotify Account Data"


def _read_json(zip_path: Path | str, member: str) -> dict:
    with zipfile.ZipFile(zip_path) as zf:
        return json.load(zf.open(f"{_PREFIX}/{member}"))


def load_playlists(zip_path: Path | str) -> list[Playlist]:
    playlists = []
    for pl in _read_json(zip_path, "Playlist1.json")["playlists"]:
        songs = [
            Song(
                name=item["track"]["trackName"],
                artist=item["track"]["artistName"],
                album=item["track"]["albumName"],
                spotify_uri=item["track"].get("trackUri"),
                added_date=item["addedDate"],
            )
            for item in pl["items"]
            # ponytail: episodes/audiobooks/localTracks skipped (15 local items
            # in the real export); parse localTrack URIs if they ever matter
            if item.get("track")
        ]
        playlists.append(Playlist(name=pl["name"], description=pl["description"], songs=songs))
    return playlists


def find_export_zip(raw_dir: Path) -> Path:
    """Newest 'Account Data' zip in raw_dir. GDPR downloads also produce
    streaming-history / technical-log zips under the same filename pattern,
    so membership of the Account Data folder is what identifies the right one.
    """
    zips = sorted(raw_dir.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in zips:
        with zipfile.ZipFile(path) as zf:
            if any(name.startswith(_PREFIX) for name in zf.namelist()):
                return path
    raise FileNotFoundError(
        f"No '{_PREFIX}' export zip found in {raw_dir} — download your Spotify GDPR "
        "export (spotify.com/account/privacy) and drop it there, or pass a path as "
        "an argument."
    )


def load_followed_artists(zip_path: Path | str) -> list[FollowedArtist]:
    # Follow.json only contains follower/following COUNTS — the actual followed
    # artists live in YourLibrary.json under "artists" as {name, uri}.
    return [
        FollowedArtist(name=a["name"], spotify_id=a["uri"].rsplit(":", 1)[-1])
        for a in _read_json(zip_path, "YourLibrary.json")["artists"]
    ]
