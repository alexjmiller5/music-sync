"""Smart playlist rules: validate, render to SQL, render to a description, evaluate.

The reconciler evaluates rules over an in-memory sqlite copy of the mirror; no
static copy of the rules is installed in the catalog. New rules reference
playlists by stable ID; legacy name keys stay readable for old rows only.
"""

import json
import re
import sqlite3

from core.model import Mirror

KEYS = {
    "v",
    "deezer_genres_any",
    "mb_tags_any",
    "genre_any",
    "first_year",
    "in_playlist_any",
    "not_in_playlist",
    "in_playlist_ids_any",
    "not_in_playlist_ids",
    "captured_by",
    "liked_after",
    "matches_rule_ids_any",
    "not_matches_rule_ids",
}
GENRE_KEYS = {"deezer", "mb_tags_contain"}
CAPTURE_KINDS = {"shazam", "playlist", "like"}
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


class RuleError(ValueError):
    pass


def _str_list(v, key):
    if not isinstance(v, list) or not v or not all(isinstance(x, str) and x for x in v):
        raise RuleError(f"{key}: non-empty list of strings required")


def validate(rule: dict) -> None:
    if not isinstance(rule, dict) or rule.get("v") != 1:
        raise RuleError("rule must be an object with v = 1")
    unknown = set(rule) - KEYS
    if unknown:
        raise RuleError(f"unknown keys: {sorted(unknown)}")
    for k in (
        "deezer_genres_any",
        "mb_tags_any",
        "in_playlist_any",
        "not_in_playlist",
        "in_playlist_ids_any",
        "not_in_playlist_ids",
        "matches_rule_ids_any",
        "not_matches_rule_ids",
    ):
        if k in rule:
            _str_list(rule[k], k)
    if "genre_any" in rule:
        g = rule["genre_any"]
        if not isinstance(g, dict) or not g or set(g) - GENRE_KEYS:
            raise RuleError("genre_any: object with deezer / mb_tags_contain")
        for k, v in g.items():
            _str_list(v, f"genre_any.{k}")
    if "first_year" in rule:
        fy = rule["first_year"]
        if not isinstance(fy, dict) or not fy or set(fy) - {"lt", "gte", "between"}:
            raise RuleError("first_year: object with lt / gte / between")
        for k in ("lt", "gte"):
            if k in fy and type(fy[k]) is not int:
                raise RuleError(f"first_year.{k}: int")
        if "between" in fy and not (
            isinstance(fy["between"], list)
            and len(fy["between"]) == 2
            and all(type(x) is int for x in fy["between"])
            and fy["between"][0] <= fy["between"][1]
        ):
            raise RuleError("first_year.between: [a, b]")
    if "captured_by" in rule and (
        not isinstance(rule["captured_by"], str) or rule["captured_by"] not in CAPTURE_KINDS
    ):
        raise RuleError(f"captured_by: one of {sorted(CAPTURE_KINDS)}")
    if "liked_after" in rule and not (
        isinstance(rule["liked_after"], str) and DATE_RE.match(rule["liked_after"])
    ):
        raise RuleError("liked_after: YYYY-MM-DD")


def _q(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _pid(name: str, playlist_ids: dict[str, str]) -> str:
    if name not in playlist_ids:
        raise RuleError(f"unknown playlist: {name}")
    if playlist_ids[name] is None:
        raise RuleError(f"ambiguous playlist: {name}; use its stable ID")
    return _q(playlist_ids[name])


def _genre_sql(g: dict) -> str:
    """Deezer genre equals one listed, or a MusicBrainz tag contains one listed (any case)."""
    any_ = []
    if "deezer" in g:
        any_.append(
            "EXISTS (SELECT 1 FROM json_each(s.deezer_genres) WHERE value IN (%s))"
            % ",".join(_q(x) for x in g["deezer"])
        )
    if "mb_tags_contain" in g:
        any_.append(
            "EXISTS (SELECT 1 FROM json_each(s.mb_tags) WHERE %s)"
            % " OR ".join(f"instr(lower(value), {_q(t.lower())}) > 0" for t in g["mb_tags_contain"])
        )
    return "(" + " OR ".join(any_) + ")"


def to_sql(
    rule: dict,
    playlist_ids: dict[str, str],
    known_ids: set[str] | None = None,
    rules_by_id: dict[str, dict] | None = None,
    pid: str | None = None,
    _seen: tuple = (),
) -> str:
    """`matches_rule_ids_any` / `not_matches_rule_ids` inline another smart playlist's
    rule, so one predicate (say a genre) is defined once and reused by several rules."""
    validate(rule)
    parts = []
    known_ids = known_ids if known_ids is not None else set(playlist_ids.values())
    seen = _seen + ((pid,) if pid else ())
    for key, prefix in (("matches_rule_ids_any", ""), ("not_matches_rule_ids", "NOT ")):
        if key in rule:
            missing = set(rule[key]) - known_ids
            if missing:
                raise RuleError(f"unknown playlist ID: {', '.join(sorted(missing))}")
            subs = []
            for ref in rule[key]:
                if ref in seen:
                    raise RuleError(f"rule reference cycle through {ref}")
                if not (rules_by_id or {}).get(ref):
                    raise RuleError(f"{ref} is not a smart playlist")
                subs.append(
                    to_sql(rules_by_id[ref], playlist_ids, known_ids, rules_by_id, ref, seen)
                )
            parts.append(f"{prefix}(" + " OR ".join(subs) + ")")
    for key, prefix in (("in_playlist_ids_any", ""), ("not_in_playlist_ids", "NOT ")):
        if key in rule:
            missing = set(rule[key]) - known_ids
            if missing:
                raise RuleError(f"unknown playlist ID: {', '.join(sorted(missing))}")
            parts.append(
                f"{prefix}EXISTS (SELECT 1 FROM playlist_songs ps WHERE ps.isrc = s.id "
                "AND ps.deleted_at IS NULL AND ps.playlist_id IN (%s))"
                % ",".join(_q(pid) for pid in rule[key])
            )
    if "deezer_genres_any" in rule:
        parts.append(
            "EXISTS (SELECT 1 FROM json_each(s.deezer_genres) WHERE value IN (%s))"
            % ",".join(_q(g) for g in rule["deezer_genres_any"])
        )
    if "genre_any" in rule:
        parts.append(_genre_sql(rule["genre_any"]))
    if "mb_tags_any" in rule:
        parts.append(
            "EXISTS (SELECT 1 FROM json_each(s.mb_tags) WHERE value IN (%s))"
            % ",".join(_q(t.lower()) for t in rule["mb_tags_any"])
        )
    if "first_year" in rule:
        fy = rule["first_year"]
        if "lt" in fy:
            parts.append(f"s.first_year < {int(fy['lt'])}")
        if "gte" in fy:
            parts.append(f"s.first_year >= {int(fy['gte'])}")
        if "between" in fy:
            a, b = fy["between"]
            parts.append(f"s.first_year BETWEEN {int(a)} AND {int(b)}")
    if "in_playlist_any" in rule:
        parts.append(
            "EXISTS (SELECT 1 FROM playlist_songs ps WHERE ps.isrc = s.id AND ps.deleted_at IS NULL AND ps.playlist_id IN (%s))"
            % ",".join(_pid(n, playlist_ids) for n in rule["in_playlist_any"])
        )
    if "not_in_playlist" in rule:
        parts.append(
            "NOT EXISTS (SELECT 1 FROM playlist_songs ps WHERE ps.isrc = s.id AND ps.deleted_at IS NULL AND ps.playlist_id IN (%s))"
            % ",".join(_pid(n, playlist_ids) for n in rule["not_in_playlist"])
        )
    if "captured_by" in rule:
        parts.append(
            f"EXISTS (SELECT 1 FROM captures c WHERE c.isrc = s.id AND c.from_kind = {_q(rule['captured_by'])})"
        )
    if "liked_after" in rule:
        parts.append(f"s.liked_at >= {_q(rule['liked_after'])}")
    return "(" + " AND ".join(parts) + ")" if parts else "(1=1)"


def describe(rule: dict, synced: str, playlist_names: dict[str, str] | None = None) -> str:
    seg = ["smart"]
    for key, label in (
        ("in_playlist_ids_any", "in: "),
        ("not_in_playlist_ids", "not in: "),
        ("matches_rule_ids_any", "matches: "),
        ("not_matches_rule_ids", "not: "),
    ):
        if key in rule:
            seg.append(label + ", ".join((playlist_names or {}).get(pid, pid) for pid in rule[key]))
    if "deezer_genres_any" in rule:
        seg.append("genre: " + ", ".join(rule["deezer_genres_any"]))
    if "genre_any" in rule:
        g = rule["genre_any"]
        text = [", ".join(g["deezer"])] if "deezer" in g else []
        if "mb_tags_contain" in g:
            text.append("tags containing " + ", ".join(g["mb_tags_contain"]))
        seg.append("genre: " + " or ".join(text))
    if "mb_tags_any" in rule:
        seg.append("tags: " + ", ".join(rule["mb_tags_any"]))
    if "first_year" in rule:
        fy = rule["first_year"]
        if "between" in fy:
            seg.append(f"year {fy['between'][0]}-{fy['between'][1]}")
        else:
            if "gte" in fy:
                seg.append(f"year >= {fy['gte']}")
            if "lt" in fy:
                seg.append(f"year < {fy['lt']}")
    if "in_playlist_any" in rule:
        seg.append("in: " + ", ".join(rule["in_playlist_any"]))
    if "not_in_playlist" in rule:
        seg.append("not in: " + ", ".join(rule["not_in_playlist"]))
    if "captured_by" in rule:
        seg.append(
            {"shazam": "shazamed", "playlist": "from playlists", "like": "from likes"}[
                rule["captured_by"]
            ]
        )
    if "liked_after" in rule:
        seg.append(f"liked after {rule['liked_after']}")
    seg.append(f"synced {synced}")
    return " · ".join(seg)[:300]


def load_sqlite(mirror: Mirror) -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    db.executescript("""
        CREATE TABLE songs (id TEXT PRIMARY KEY, liked INT, liked_at TEXT, first_year INT, deezer_genres TEXT, mb_tags TEXT);
        CREATE TABLE playlist_songs (id TEXT PRIMARY KEY, playlist_id TEXT, isrc TEXT, deleted_at TEXT);
        CREATE TABLE captures (isrc TEXT, from_kind TEXT);
    """)
    db.executemany(
        "INSERT INTO songs VALUES (?,?,?,?,?,?)",
        [
            (
                s.id,
                s.liked,
                s.liked_at,
                s.first_year,
                json.dumps(s.deezer_genres),
                json.dumps(s.mb_tags),
            )
            for s in mirror.songs.values()
        ],
    )
    db.executemany(
        "INSERT INTO playlist_songs VALUES (?,?,?,NULL)",
        [(m.id, m.playlist_id, m.isrc) for m in mirror.memberships.values()],
    )
    db.executemany("INSERT INTO captures VALUES (?,?)", list(mirror.captures))
    return db


def evaluate(
    mirror: Mirror, playlist_ids: dict[str, str], errors: dict[str, str] | None = None
) -> dict[str, set[str]]:
    db = load_sqlite(mirror)
    playlist_ids = dict(playlist_ids)
    current_names = {}
    for p in mirror.playlists.values():
        if p.name in current_names and current_names[p.name] != p.id:
            playlist_ids[p.name] = None
        current_names[p.name] = p.id
    out = {}
    smart_rules = {p.id: p.rule for p in mirror.playlists.values() if p.kind == "smart"}
    for p in mirror.playlists.values():
        if p.kind != "smart":
            continue
        try:
            where = to_sql(p.rule, playlist_ids, set(mirror.playlists), smart_rules, p.id)
            sql = f"SELECT id FROM songs s WHERE s.liked = 1 AND {where}"
        except RuleError as e:
            if errors is None:
                raise
            errors[p.id] = str(e)
            continue
        out[p.id] = {r[0] for r in db.execute(sql)}
    return out
