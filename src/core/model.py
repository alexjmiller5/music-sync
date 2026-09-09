"""Plain dataclasses shared by mirror, reconcile, actions."""

from dataclasses import dataclass, field

ISRC_RE = r"^[A-Z]{2}[A-Z0-9]{3}[0-9]{7}$"


@dataclass
class Song:
    id: str
    liked: int
    liked_at: str | None
    first_seen: str
    spotify_ids: list[str] = field(default_factory=list)
    spotify_playable: int | None = None
    first_year: int | None = None
    deezer_genres: list[str] = field(default_factory=list)
    mb_tags: list[str] = field(default_factory=list)
    title: str | None = None
    artists: list[str] = field(default_factory=list)


@dataclass
class Playlist:
    id: str
    name: str
    kind: str
    rule: dict | None
    description: str | None
    snapshot_id: str | None
    pinned: int
    expires_at: str | None


@dataclass
class Membership:
    playlist_id: str
    isrc: str
    spotify_track_id: str
    added_at: str
    deleted_at: str | None = None

    @property
    def id(self) -> str:
        return f"{self.playlist_id}:{self.isrc}"


@dataclass
class Capture:
    isrc: str
    from_kind: str


@dataclass
class Mirror:
    songs: dict[str, Song]
    playlists: dict[str, Playlist]
    memberships: dict[tuple[str, str], Membership]
    deleted_memberships: list[Membership]
    captures: set[tuple[str, str]]  # (isrc, from_kind)


@dataclass
class LiveItem:
    isrc: str | None
    track_id: str | None
    uri: str | None
    added_at: str
    playable: bool
    is_local: bool
    name: str | None
    artists: list[str]


@dataclass
class LivePlaylist:
    id: str
    name: str
    description: str | None
    snapshot_id: str | None
    items: list[LiveItem] | None


@dataclass
class Live:
    playlists: dict[str, LivePlaylist]
    liked: dict[str, LiveItem]
    raw: dict


@dataclass
class Action:
    kind: str
    playlist_id: str | None = None
    isrc: str | None = None
    uri: str | None = None
    text: str | None = None
    row: dict | None = None
    reason: str = ""
    playlist_name: str | None = None
    title: str | None = None
