"""Pydantic models for the Spotify GDPR export — only fields the export actually has."""

from datetime import date

from pydantic import BaseModel


class Song(BaseModel):
    name: str
    artist: str
    album: str
    spotify_uri: str | None = None
    added_date: date


class Playlist(BaseModel):
    name: str
    description: str | None = None
    songs: list[Song] = []


class FollowedArtist(BaseModel):
    name: str
    spotify_id: str | None = None
    # empty in the GDPR export; the live API (spotify_client) fills these in
    genres: list[str] = []
    followers: int | None = None
