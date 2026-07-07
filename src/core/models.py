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
