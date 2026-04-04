"""Everything related to live tracking is in this cog. The poll_quakes function is the main logic"""
import asyncio
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import pickle
import logging
import time
import os
import sqlite3
import csv
import io
import re

from colorlog.escape_codes import escape_codes as c
from discord.ext import tasks, commands
from discord.utils import get
import plotly.graph_objects as go
import pandas as pd
import discord
import aiohttp
try:
    import pycountry
except ImportError:  # pragma: no cover - optional dependency in local dev
    pycountry = None

from cogs.earthquake_sources import (
    EarthquakeEvent,
    build_earthquake_sources,
    fetch_events_from_sources,
)

logger = logging.getLogger("discord")

DEFAULT_EARTHQUAKE_SOURCES = ["usgs", "emsc"]

GUILD_PREFS_PATH = "data/guild_prefs.pkl"
EQ_NOTIFY_DB_PATH = "data/eq_notify_db.pkl"
EQ_DB_PATH = "data/eq_db1.pkl"
SQLITE_DB_PATH = "data/namazu.db"
DISCORD_FILE_LIMIT_BYTES = 10 * 1024 * 1024
DEFAULT_MAP_LONGITUDE = -74.00
DEFAULT_MAP_LATITUDE = 40.71
MAP_STYLE_OPTIONS = [
    {
        "label": "OpenStreetMap",
        "map_style": "open-street-map",
        "font_color": "black",
        "paper_bgcolor": "white",
    },
    {
        "label": "Carto Positron",
        "map_style": "carto-positron",
        "font_color": "black",
        "paper_bgcolor": "white",
    },
    {
        "label": "Carto Darkmatter",
        "map_style": "carto-darkmatter",
        "font_color": "white",
        "paper_bgcolor": "#232328",
    },
    {
        "label": "White Background",
        "map_style": "white-bg",
        "font_color": "black",
        "paper_bgcolor": "white",
    },
]
NUMBER_REACTIONS = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
ISLAND_REGION_KEYWORDS = [
    "hawaii",
    "puerto rico",
    "guam",
    "south sandwich islands",
    "solomon islands",
    "papua new guinea",
    "new zealand",
    "tonga",
    "vanuatu",
    "philippines",
    "indonesia",
    "timor leste",
    "trinidad and tobago",
    "beaufort sea",
]

US_STATE_ABBREV_TO_NAME = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
    "DC": "District of Columbia",
}
US_STATE_NAME_TO_ABBREV = {
    name.lower(): code for code, name in US_STATE_ABBREV_TO_NAME.items()
}
US_STATE_NAME_PATTERNS = {
    code: re.compile(rf"\b{re.escape(name.lower())}\b")
    for code, name in US_STATE_ABBREV_TO_NAME.items()
}
COUNTRY_ALIAS_TO_CANONICAL = {
    "us": "United States",
    "u s": "United States",
    "u s a": "United States",
    "usa": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "u k": "United Kingdom",
    "russia": "Russian Federation",
    "south korea": "Korea, Republic of",
    "north korea": "Korea, Democratic People's Republic of",
    "iran": "Iran, Islamic Republic of",
    "venezuela": "Venezuela, Bolivarian Republic of",
    "syria": "Syrian Arab Republic",
    "laos": "Lao People's Democratic Republic",
    "moldova": "Moldova, Republic of",
    "czech republic": "Czechia",
    "bolivia": "Bolivia, Plurinational State of",
    "tanzania": "Tanzania, United Republic of",
}
AMBIGUOUS_COUNTRY_NAMES = {"Georgia"}


def get_default_guild_prefs():
    return {
        "MinMagnitude": 3,
        "UpdateFrequency": 0,
        "UpdateChannelId": 0,
        "PlotStyle": 0,
        "PinMagnitude": None,
    }


def get_default_user_pref():
    return {
        "MagnitudeMentionEnabled": False,
        "MagnitudeThreshold": None,
        "States": set(),
        "Countries": set(),
    }


def get_map_style_option(plot_style_idx: int):
    if 0 <= plot_style_idx < len(MAP_STYLE_OPTIONS):
        return MAP_STYLE_OPTIONS[plot_style_idx], plot_style_idx
    return MAP_STYLE_OPTIONS[0], 0


def sanitize_filename(value: str):
    return "".join(char if char.isalnum() or char in ("-", "_") else "_" for char in value)


def get_single_quake_zoom(place: str, default_zoom=2.1):
    place_lower = str(place).lower()
    if any(keyword in place_lower for keyword in ISLAND_REGION_KEYWORDS):
        return 4.0
    return default_zoom


def ensure_data_dir():
    os.makedirs("data", exist_ok=True)


def _normalize_geo_text(value: str):
    normalized = re.sub(r"[^a-z0-9\s]", " ", str(value).lower())
    return " ".join(normalized.split())


def _build_country_lookups():
    country_name_to_canonical = {}
    canonical_names = set()

    if pycountry is not None:
        for country in pycountry.countries:
            canonical_name = country.name
            canonical_names.add(canonical_name)

            for attr_name in ("name", "official_name", "common_name"):
                attr_value = getattr(country, attr_name, None)
                if attr_value:
                    country_name_to_canonical[_normalize_geo_text(attr_value)] = canonical_name

    for alias, canonical_name in COUNTRY_ALIAS_TO_CANONICAL.items():
        country_name_to_canonical[_normalize_geo_text(alias)] = canonical_name
        canonical_names.add(canonical_name)

    country_patterns = {
        canonical_name: re.compile(rf"\b{re.escape(canonical_name.lower())}\b")
        for canonical_name in canonical_names
    }
    return country_name_to_canonical, country_patterns


COUNTRY_NAME_TO_CANONICAL, COUNTRY_PATTERNS = _build_country_lookups()


def normalize_state_code(state_input: str):
    normalized = str(state_input).strip()
    if not normalized:
        return None

    upper = normalized.upper()
    if upper in US_STATE_ABBREV_TO_NAME:
        return upper

    name_key = normalized.lower()
    name_key = re.sub(r"[^a-z\s]", "", name_key)
    name_key = " ".join(name_key.split())
    return US_STATE_NAME_TO_ABBREV.get(name_key)


def infer_us_state_codes_from_place(place: str):
    if not place:
        return set()

    state_codes = set()
    upper_place = str(place).upper()

    for segment in upper_place.split(","):
        token = segment.strip().split(" ")[0].strip(".")
        if len(token) == 2 and token in US_STATE_ABBREV_TO_NAME:
            state_codes.add(token)

    place_lower = str(place).lower()
    for state_code, state_pattern in US_STATE_NAME_PATTERNS.items():
        if state_pattern.search(place_lower):
            state_codes.add(state_code)

    return state_codes


def normalize_country_name(country_input: str):
    normalized = _normalize_geo_text(country_input)
    return COUNTRY_NAME_TO_CANONICAL.get(normalized)


def infer_country_names_from_place(place: str, us_state_codes: set[str] | None = None):
    if not place:
        return set()

    countries = set()
    place_lower = str(place).lower()

    for segment in str(place).split(","):
        normalized_segment = _normalize_geo_text(segment)
        if normalized_segment in COUNTRY_NAME_TO_CANONICAL:
            countries.add(COUNTRY_NAME_TO_CANONICAL[normalized_segment])

    for canonical_name, country_pattern in COUNTRY_PATTERNS.items():
        if country_pattern.search(place_lower):
            countries.add(canonical_name)

    has_us_signals = bool(us_state_codes) or bool(
        re.search(r"\bunited states\b|\busa\b|\bu\.s\.a\.?\b|\b us\b", place_lower)
    )
    if has_us_signals:
        countries.difference_update(AMBIGUOUS_COUNTRY_NAMES)

    return countries


def build_mention_chunks(user_ids: list[str], prefix: str = "Personal alerts: "):
    if not user_ids:
        return []

    chunks = []
    current = prefix

    for user_id in user_ids:
        mention = f"<@{user_id}>"
        candidate = mention if current == prefix else f" {mention}"

        if len(current) + len(candidate) > 2000:
            chunks.append(current)
            current = prefix + mention
            continue

        current += candidate

    if current != prefix:
        chunks.append(current)

    return chunks


def get_sqlite_conn():
    ensure_data_dir()
    conn = sqlite3.connect(SQLITE_DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_sqlite():
    with get_sqlite_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_prefs (
                guild_id TEXT PRIMARY KEY,
                min_magnitude INTEGER NOT NULL DEFAULT 3,
                update_frequency INTEGER NOT NULL DEFAULT 0,
                update_channel_id INTEGER NOT NULL DEFAULT 0,
                plot_style INTEGER NOT NULL DEFAULT 0,
                pin_magnitude REAL
            )
            """
        )
        guild_pref_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(guild_prefs)").fetchall()
        }
        if "pin_magnitude" not in guild_pref_columns:
            conn.execute("ALTER TABLE guild_prefs ADD COLUMN pin_magnitude REAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS eq_notify (
                guild_id TEXT NOT NULL,
                earthquake_id TEXT NOT NULL,
                notified INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, earthquake_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS earthquakes (
                earthquake_id TEXT PRIMARY KEY,
                pager_lvl_icon TEXT,
                place TEXT,
                magnitude REAL,
                url TEXT,
                time TEXT,
                pager_alert_level TEXT,
                tsunami_potential INTEGER,
                depth TEXT,
                latitude REAL,
                longitude REAL,
                significance INTEGER
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_alert_prefs (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                magnitude_mention_enabled INTEGER NOT NULL DEFAULT 0,
                magnitude_threshold REAL,
                PRIMARY KEY (guild_id, user_id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_state_alerts (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                state_code TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, state_code),
                FOREIGN KEY (guild_id, user_id)
                    REFERENCES user_alert_prefs(guild_id, user_id)
                    ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_country_alerts (
                guild_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                country_name TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id, country_name),
                FOREIGN KEY (guild_id, user_id)
                    REFERENCES user_alert_prefs(guild_id, user_id)
                    ON DELETE CASCADE
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_alert_prefs_guild
            ON user_alert_prefs (guild_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_state_alerts_guild_state
            ON user_state_alerts (guild_id, state_code)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_user_country_alerts_guild_country
            ON user_country_alerts (guild_id, country_name)
            """
        )
        conn.commit()


def save_eq_db_to_sqlite(eq_db: dict):
    init_sqlite()
    with get_sqlite_conn() as conn:
        conn.executemany(
            """
            INSERT INTO earthquakes (
                earthquake_id, pager_lvl_icon, place, magnitude, url, time,
                pager_alert_level, tsunami_potential, depth, latitude, longitude, significance
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(earthquake_id) DO UPDATE SET
                pager_lvl_icon=excluded.pager_lvl_icon,
                place=excluded.place,
                magnitude=excluded.magnitude,
                url=excluded.url,
                time=excluded.time,
                pager_alert_level=excluded.pager_alert_level,
                tsunami_potential=excluded.tsunami_potential,
                depth=excluded.depth,
                latitude=excluded.latitude,
                longitude=excluded.longitude,
                significance=excluded.significance
            """,
            [
                (
                    eq_id,
                    row.get("pager_lvl_icon"),
                    row.get("place"),
                    row.get("magnitude"),
                    row.get("url"),
                    row.get("time"),
                    row.get("pager_alert_level"),
                    int(bool(row.get("tsunami_potential"))),
                    row.get("depth"),
                    row.get("latitude"),
                    row.get("longitude"),
                    row.get("significance"),
                )
                for eq_id, row in eq_db.items()
            ],
        )
        conn.commit()


def save_guild_prefs_to_sqlite(guild_prefs: dict):
    init_sqlite()
    with get_sqlite_conn() as conn:
        conn.executemany(
            """
            INSERT INTO guild_prefs (
                guild_id, min_magnitude, update_frequency, update_channel_id, plot_style, pin_magnitude
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                min_magnitude=excluded.min_magnitude,
                update_frequency=excluded.update_frequency,
                update_channel_id=excluded.update_channel_id,
                plot_style=excluded.plot_style,
                pin_magnitude=excluded.pin_magnitude
            """,
            [
                (
                    guild_id,
                    prefs.get("MinMagnitude", 3),
                    prefs.get("UpdateFrequency", 0),
                    prefs.get("UpdateChannelId", 0),
                    prefs.get("PlotStyle", 0),
                    prefs.get("PinMagnitude"),
                )
                for guild_id, prefs in guild_prefs.items()
            ],
        )
        conn.commit()


def save_eq_notify_db_to_sqlite(eq_notify_db: dict):
    init_sqlite()
    with get_sqlite_conn() as conn:
        rows = [
            (guild_id, earthquake_id, int(bool(notified)))
            for guild_id, guild_data in eq_notify_db.items()
            for earthquake_id, notified in guild_data.items()
        ]
        conn.executemany(
            """
            INSERT INTO eq_notify (guild_id, earthquake_id, notified)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, earthquake_id) DO UPDATE SET
                notified=excluded.notified
            """,
            rows,
        )
        conn.commit()


def _ensure_user_pref_row_sqlite(conn, guild_id: str, user_id: str):
    conn.execute(
        """
        INSERT OR IGNORE INTO user_alert_prefs (
            guild_id, user_id, magnitude_mention_enabled, magnitude_threshold
        ) VALUES (?, ?, 0, NULL)
        """,
        (guild_id, user_id),
    )


def save_user_prefs_to_sqlite(user_prefs: dict):
    """Save in-memory user prefs structure to sqlite (full replace)."""
    init_sqlite()
    pref_rows = []
    state_rows = []
    country_rows = []

    for guild_id, guild_users in user_prefs.items():
        for user_id, prefs in guild_users.items():
            pref_rows.append(
                (
                    guild_id,
                    user_id,
                    int(bool(prefs.get("MagnitudeMentionEnabled", False))),
                    prefs.get("MagnitudeThreshold"),
                )
            )
            for state_code in sorted(prefs.get("States", set())):
                state_rows.append((guild_id, user_id, state_code))
            for country_name in sorted(prefs.get("Countries", set())):
                country_rows.append((guild_id, user_id, country_name))

    with get_sqlite_conn() as conn:
        conn.execute("DELETE FROM user_state_alerts")
        conn.execute("DELETE FROM user_country_alerts")
        conn.execute("DELETE FROM user_alert_prefs")
        if pref_rows:
            conn.executemany(
                """
                INSERT INTO user_alert_prefs (
                    guild_id, user_id, magnitude_mention_enabled, magnitude_threshold
                ) VALUES (?, ?, ?, ?)
                """,
                pref_rows,
            )
        if state_rows:
            conn.executemany(
                """
                INSERT INTO user_state_alerts (
                    guild_id, user_id, state_code
                ) VALUES (?, ?, ?)
                """,
                state_rows,
            )
        if country_rows:
            conn.executemany(
                """
                INSERT INTO user_country_alerts (
                    guild_id, user_id, country_name
                ) VALUES (?, ?, ?)
                """,
                country_rows,
            )
        conn.commit()


def upsert_user_magnitude_pref_to_sqlite(guild_id: str, user_id: str, enabled: bool, threshold: float | None):
    init_sqlite()
    with get_sqlite_conn() as conn:
        _ensure_user_pref_row_sqlite(conn, guild_id, user_id)
        conn.execute(
            """
            UPDATE user_alert_prefs
            SET magnitude_mention_enabled = ?, magnitude_threshold = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (int(bool(enabled)), threshold, guild_id, user_id),
        )
        conn.commit()


def add_user_state_alert_to_sqlite(guild_id: str, user_id: str, state_code: str):
    init_sqlite()
    with get_sqlite_conn() as conn:
        _ensure_user_pref_row_sqlite(conn, guild_id, user_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO user_state_alerts (
                guild_id, user_id, state_code
            ) VALUES (?, ?, ?)
            """,
            (guild_id, user_id, state_code),
        )
        conn.commit()


def remove_user_state_alert_from_sqlite(guild_id: str, user_id: str, state_code: str):
    init_sqlite()
    with get_sqlite_conn() as conn:
        conn.execute(
            """
            DELETE FROM user_state_alerts
            WHERE guild_id = ? AND user_id = ? AND state_code = ?
            """,
            (guild_id, user_id, state_code),
        )
        conn.commit()


def add_user_country_alert_to_sqlite(guild_id: str, user_id: str, country_name: str):
    init_sqlite()
    with get_sqlite_conn() as conn:
        _ensure_user_pref_row_sqlite(conn, guild_id, user_id)
        conn.execute(
            """
            INSERT OR IGNORE INTO user_country_alerts (
                guild_id, user_id, country_name
            ) VALUES (?, ?, ?)
            """,
            (guild_id, user_id, country_name),
        )
        conn.commit()


def remove_user_country_alert_from_sqlite(guild_id: str, user_id: str, country_name: str):
    init_sqlite()
    with get_sqlite_conn() as conn:
        conn.execute(
            """
            DELETE FROM user_country_alerts
            WHERE guild_id = ? AND user_id = ? AND country_name = ?
            """,
            (guild_id, user_id, country_name),
        )
        conn.commit()


def get_eq_db():
    """Load earthquakes from sqlite and return in legacy dict format."""
    init_sqlite()
    with get_sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT earthquake_id, pager_lvl_icon, place, magnitude, url, time,
                   pager_alert_level, tsunami_potential, depth, latitude, longitude, significance
            FROM earthquakes
            """
        ).fetchall()
    return {
        row[0]: {
            "earthquake_id": row[0],
            "pager_lvl_icon": row[1],
            "place": row[2],
            "magnitude": row[3],
            "url": row[4],
            "time": row[5],
            "pager_alert_level": row[6],
            "tsunami_potential": bool(row[7]),
            "depth": row[8],
            "latitude": row[9],
            "longitude": row[10],
            "significance": row[11],
        }
        for row in rows
    }


def get_guild_prefs():
    """Load guild preferences from sqlite and return in legacy dict format."""
    init_sqlite()
    with get_sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT guild_id, min_magnitude, update_frequency, update_channel_id, plot_style, pin_magnitude
            FROM guild_prefs
            """
        ).fetchall()
    return {
        row[0]: {
            "MinMagnitude": row[1],
            "UpdateFrequency": row[2],
            "UpdateChannelId": row[3],
            "PlotStyle": row[4],
            "PinMagnitude": row[5],
        }
        for row in rows
    }


def get_eq_notify_db():
    """Load guild earthquake notify state from sqlite and return in legacy dict format."""
    init_sqlite()
    with get_sqlite_conn() as conn:
        rows = conn.execute(
            """
            SELECT guild_id, earthquake_id, notified
            FROM eq_notify
            """
        ).fetchall()

    eq_notify_db = {}
    for guild_id, earthquake_id, notified in rows:
        eq_notify_db.setdefault(guild_id, {})[earthquake_id] = bool(notified)
    return eq_notify_db


def get_user_prefs():
    """Load user alert preferences from sqlite to nested dict keyed by guild_id then user_id."""
    init_sqlite()
    with get_sqlite_conn() as conn:
        pref_rows = conn.execute(
            """
            SELECT guild_id, user_id, magnitude_mention_enabled, magnitude_threshold
            FROM user_alert_prefs
            """
        ).fetchall()
        state_rows = conn.execute(
            """
            SELECT guild_id, user_id, state_code
            FROM user_state_alerts
            """
        ).fetchall()
        country_rows = conn.execute(
            """
            SELECT guild_id, user_id, country_name
            FROM user_country_alerts
            """
        ).fetchall()

    user_prefs = {}
    for guild_id, user_id, magnitude_mention_enabled, magnitude_threshold in pref_rows:
        guild_prefs = user_prefs.setdefault(guild_id, {})
        guild_prefs[user_id] = {
            "MagnitudeMentionEnabled": bool(magnitude_mention_enabled),
            "MagnitudeThreshold": magnitude_threshold,
            "States": set(),
            "Countries": set(),
        }

    for guild_id, user_id, state_code in state_rows:
        guild_prefs = user_prefs.setdefault(guild_id, {})
        user_pref = guild_prefs.setdefault(user_id, get_default_user_pref())
        user_pref["States"].add(state_code)

    for guild_id, user_id, country_name in country_rows:
        guild_prefs = user_prefs.setdefault(guild_id, {})
        user_pref = guild_prefs.setdefault(user_id, get_default_user_pref())
        user_pref["Countries"].add(country_name)

    return user_prefs


def migrate_pickle_data_if_needed():
    """One-time migration: if sqlite tables are empty, import legacy pickle data."""
    init_sqlite()
    with get_sqlite_conn() as conn:
        earthquakes_count = conn.execute("SELECT COUNT(*) FROM earthquakes").fetchone()[0]
        prefs_count = conn.execute("SELECT COUNT(*) FROM guild_prefs").fetchone()[0]
        notify_count = conn.execute("SELECT COUNT(*) FROM eq_notify").fetchone()[0]

    if earthquakes_count == 0 and os.path.exists(EQ_DB_PATH):
        with open(EQ_DB_PATH, "rb") as f:
            save_eq_db_to_sqlite(pickle.load(f))
        logger.info("Migrated earthquake records from %s to sqlite.", EQ_DB_PATH)

    if prefs_count == 0 and os.path.exists(GUILD_PREFS_PATH):
        with open(GUILD_PREFS_PATH, "rb") as f:
            save_guild_prefs_to_sqlite(pickle.load(f))
        logger.info("Migrated guild prefs from %s to sqlite.", GUILD_PREFS_PATH)

    if notify_count == 0 and os.path.exists(EQ_NOTIFY_DB_PATH):
        with open(EQ_NOTIFY_DB_PATH, "rb") as f:
            save_eq_notify_db_to_sqlite(pickle.load(f))
        logger.info("Migrated notify db from %s to sqlite.", EQ_NOTIFY_DB_PATH)


def format_bytes(size_bytes: int):
    """Format byte count into a human-readable size string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.2f} KB"
    return f"{size_bytes / (1024 * 1024):.2f} MB"


def _csv_row_to_bytes(row: tuple):
    """Serialize a single CSV row to UTF-8 bytes."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(row)
    return buf.getvalue().encode("utf-8")


def fetch_earthquake_export_data():
    """Fetch all earthquake records and summary stats for CSV export."""
    init_sqlite()
    with get_sqlite_conn() as conn:
        summary_row = conn.execute(
            """
            SELECT
                COUNT(*) AS quake_count,
                AVG(magnitude) AS avg_magnitude,
                MIN(magnitude) AS min_magnitude,
                MAX(magnitude) AS max_magnitude
            FROM earthquakes
            """
        ).fetchone()
        rows = conn.execute(
            """
            SELECT earthquake_id, pager_lvl_icon, place, magnitude, url, time,
                   pager_alert_level, tsunami_potential, depth, latitude, longitude, significance
            FROM earthquakes
            ORDER BY time ASC
            """
        ).fetchall()

    return {
        "count": summary_row[0] or 0,
        "avg_magnitude": summary_row[1],
        "min_magnitude": summary_row[2],
        "max_magnitude": summary_row[3],
        "rows": rows,
    }


def build_earthquake_csv_parts(rows: list[tuple], max_bytes: int = DISCORD_FILE_LIMIT_BYTES):
    """Split earthquake rows into CSV byte chunks that stay under max_bytes."""
    header = (
        "earthquake_id",
        "pager_lvl_icon",
        "place",
        "magnitude",
        "url",
        "time",
        "pager_alert_level",
        "tsunami_potential",
        "depth",
        "latitude",
        "longitude",
        "significance",
    )
    header_bytes = _csv_row_to_bytes(header)
    parts = []
    current_bytes = bytearray(header_bytes)
    current_rows = 0

    for row in rows:
        row_bytes = _csv_row_to_bytes(row)
        next_size = len(current_bytes) + len(row_bytes)

        if next_size > max_bytes and current_rows > 0:
            parts.append({"bytes": bytes(current_bytes), "row_count": current_rows})
            current_bytes = bytearray(header_bytes)
            current_rows = 0

        if len(header_bytes) + len(row_bytes) > max_bytes:
            raise ValueError("A single row exceeds the 10MB Discord file size limit.")

        current_bytes.extend(row_bytes)
        current_rows += 1

    if current_rows > 0:
        parts.append({"bytes": bytes(current_bytes), "row_count": current_rows})

    return parts


def earthquake_passes_guild_magnitude_filter(eq_data: dict, min_magnitude_setting: int):
    magnitude = eq_data.get("magnitude")
    if magnitude is None:
        return False

    match min_magnitude_setting:
        case 0:
            return True
        case 1:
            return magnitude >= 1.0
        case 2:
            return magnitude >= 2.5
        case 3:
            return magnitude >= 4.5
        case 4:
            # Significant-only mode is still not wired to USGS significance rules yet.
            return False
        case _:
            return False


def load_eq_db_to_df():
    """Load the eq_db object from the binary file, and convert the dict to a dataframe"""
    start_time = time.perf_counter()
    logging.info("||=*=|| Loading EQ_DB to DataFrame ||=*=||")
    eq_db: dict = get_eq_db()

    places_col = [row["place"] for row in eq_db.values()]
    magnitude_col = [row["magnitude"] for row in eq_db.values()]
    url_col = [row["url"] for row in eq_db.values()]
    time_col = [row["time"] for row in eq_db.values()]
    earthquake_id_col = [row["earthquake_id"] for row in eq_db.values()]
    pager_alert_level_col = [row["pager_alert_level"] for row in eq_db.values()]
    tsunami_potential_col = [row["tsunami_potential"] for row in eq_db.values()]
    latitude_col = [row["latitude"] for row in eq_db.values()]
    longitude_col = [row["longitude"] for row in eq_db.values()]

    df_ready_dict = {"earthquake_id": earthquake_id_col,
                     "place": places_col,
                     "magnitude": magnitude_col,
                     "url": url_col,
                     "time": time_col,
                     "pager_alert_level": pager_alert_level_col,
                     "tsunami_potential": tsunami_potential_col,
                     "latitude": latitude_col,
                     "longitude": longitude_col,
                     }
    df = pd.DataFrame.from_dict(df_ready_dict)
    end_time = time.perf_counter()
    logging.info("||=*=|| DataFrame Ready in %.2f seconds. ||=*=||", end_time - start_time)
    return df

def plot_to_img_with_plotly(long, lat, place, mag, filename="eq_plot.png", plot_style=0):
    """Plot a single earthquake point and save as an image."""
    style, _ = get_map_style_option(plot_style)
    map_zoom = get_single_quake_zoom(place)
    title_str = "Magnitude: " + str(mag) + " " + place
    if len(title_str) > 35:
        title_str = "Magnitude: " + str(mag) + "\n" + place

    fig = go.Figure()
    fig.add_trace(
        go.Scattermap(
            lon=[long],
            lat=[lat],
            mode="markers",
            marker={"size": 11, "color": "red"},
            hovertemplate=f"Magnitude {mag}<br>{place}<extra></extra>",
        )
    )

    fig.update_layout(
        title=title_str,
        font={"color": style["font_color"]},
        margin={"r": 0, "t": 30, "l": 0, "b": 0},
        map={
            "style": style["map_style"],
            "center": {"lon": long, "lat": lat},
            "zoom": map_zoom,
        },
        paper_bgcolor=style["paper_bgcolor"],
        plot_bgcolor=style["paper_bgcolor"],
        showlegend=False,
    )
    fig.write_image(filename, width=400, height=250)


def colorize(text, color):
    """Colorize text in the terminal with colorlog helper func"""
    return f"{c[color]}{text}{c['reset']}"


def create_embed_quake_alert(earthquake_data: dict, plot_style=0, image_path="eq_plot.png"):
    # Check on the color and make embed the color, else make it gray
    plot_to_img_with_plotly(
        earthquake_data["longitude"],
        earthquake_data["latitude"],
        earthquake_data["place"],
        earthquake_data["magnitude"],
        filename=image_path,
        plot_style=plot_style,
    )

    match earthquake_data["pager_alert_level"]:
        case "green":
            embed_color = discord.Color.green()
            pager_alert = "No expected casualties or damage"
            valid_pager_alert = True
        case "yellow":
            embed_color = discord.Color.yellow()
            pager_alert = "Some casualties and localized damage possible"
            valid_pager_alert = True
        case "orange":
            embed_color = discord.Color.orange()
            pager_alert = "Significant casualties and regional damage likely"
            valid_pager_alert = True
        case "red":
            embed_color = discord.Color.red()
            pager_alert = "high casualties and widespread catastrophic damage expected"
            valid_pager_alert = True
        case _:
            embed_color = discord.Color.dark_gray()
            valid_pager_alert = False

    embed = discord.Embed(
        title=f"🚨 {earthquake_data["magnitude"]} Earthquake 🚨",
        description=f"A magnitude {earthquake_data["magnitude"]} earthquake"
                    f" has just occurred {earthquake_data["place"]}.",
        color=embed_color
    )

    if earthquake_data.get("magnitude"):
        embed.add_field(name="Magnitude",
                        value=earthquake_data.get("magnitude"),
                        inline=False)

    if earthquake_data.get("significance"):
        embed.add_field(name="Significance[1-1000]",
                        value=earthquake_data["significance"],
                        inline=True)

    if earthquake_data.get("tsunami_potential"):
        embed.add_field(name="There is potential for a Tsunami",
                        value="🌊",
                        inline=False)

    if valid_pager_alert:
        embed.add_field(name=f"{earthquake_data["pager_alert_level"].upper()} PAGER Alert",
                        value=pager_alert,
                        inline=False)

    img_file = discord.File(image_path, filename="earthquake.png")
    embed.set_image(url="attachment://earthquake.png")

    embed.add_field(name="Time",
                    value=earthquake_data["time"],
                    inline=False)
    return embed, img_file


def plot_daily_earthquakes(eq_df: pd.DataFrame, filename="eq_plot_all_today.png", plot_style=0):
    style, _ = get_map_style_option(plot_style)
    has_rows = not eq_df.empty

    center_lon = float(eq_df["longitude"].mean()) if has_rows else DEFAULT_MAP_LONGITUDE
    center_lat = float(eq_df["latitude"].mean()) if has_rows else DEFAULT_MAP_LATITUDE
    zoom = 0.65 if has_rows else 2.1

    fig = go.Figure()
    fig.add_trace(
        go.Scattermap(
            lon=eq_df["longitude"] if has_rows else [DEFAULT_MAP_LONGITUDE],
            lat=eq_df["latitude"] if has_rows else [DEFAULT_MAP_LATITUDE],
            mode="markers",
            marker={"size": 7 if has_rows else 10, "color": "red"},
            hoverinfo="skip",
        )
    )

    fig.update_layout(
        font={"color": style["font_color"]},
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        map={
            "style": style["map_style"],
            "center": {"lon": center_lon, "lat": center_lat},
            "zoom": zoom,
        },
        paper_bgcolor=style["paper_bgcolor"],
        plot_bgcolor=style["paper_bgcolor"],
        showlegend=False,
    )
    fig.write_image(filename, width=400, height=250)


class LiveTracking(commands.Cog):
    def __init__(self, client):
        self.client = client
        init_sqlite()
        migrate_pickle_data_if_needed()
        self.event_sources = build_earthquake_sources(DEFAULT_EARTHQUAKE_SOURCES)
        self.guild_prefs = get_guild_prefs()
        self.eq_notify_db = get_eq_notify_db()
        self.user_prefs = get_user_prefs()
        self.eq_db = get_eq_db()
        logger.info(
            "Configured earthquake sources: %s",
            ", ".join(source.source for source in self.event_sources),
        )
        self.client.loop.create_task(self._initialize())

    def _ensure_guild_pref(self, guild_id: str):
        guild_pref = self.guild_prefs.setdefault(guild_id, get_default_guild_prefs())
        default_pref = get_default_guild_prefs()
        for pref_key, pref_value in default_pref.items():
            guild_pref.setdefault(pref_key, pref_value)

    def _ensure_user_pref(self, guild_id: str, user_id: str):
        guild_user_prefs = self.user_prefs.setdefault(guild_id, {})
        if not guild_user_prefs.get(user_id):
            guild_user_prefs[user_id] = get_default_user_pref()
        return guild_user_prefs[user_id]

    def _set_user_magnitude_alert(self, guild_id: str, user_id: str, threshold: float):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["MagnitudeMentionEnabled"] = True
        user_pref["MagnitudeThreshold"] = threshold
        upsert_user_magnitude_pref_to_sqlite(guild_id, user_id, True, threshold)

    def _disable_user_magnitude_alert(self, guild_id: str, user_id: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["MagnitudeMentionEnabled"] = False
        user_pref["MagnitudeThreshold"] = None
        upsert_user_magnitude_pref_to_sqlite(guild_id, user_id, False, None)

    def _add_user_state_alert(self, guild_id: str, user_id: str, state_code: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["States"].add(state_code)
        add_user_state_alert_to_sqlite(guild_id, user_id, state_code)

    def _remove_user_state_alert(self, guild_id: str, user_id: str, state_code: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["States"].discard(state_code)
        remove_user_state_alert_from_sqlite(guild_id, user_id, state_code)

    def _add_user_country_alert(self, guild_id: str, user_id: str, country_name: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["Countries"].add(country_name)
        add_user_country_alert_to_sqlite(guild_id, user_id, country_name)

    def _remove_user_country_alert(self, guild_id: str, user_id: str, country_name: str):
        user_pref = self._ensure_user_pref(guild_id, user_id)
        user_pref["Countries"].discard(country_name)
        remove_user_country_alert_from_sqlite(guild_id, user_id, country_name)

    def _get_matching_user_ids(self, guild: discord.Guild, eq_data: dict):
        guild_user_prefs = self.user_prefs.get(str(guild.id), {})
        if not guild_user_prefs:
            return []

        eq_magnitude = eq_data.get("magnitude")
        eq_states = infer_us_state_codes_from_place(eq_data.get("place", ""))
        eq_countries = infer_country_names_from_place(eq_data.get("place", ""), eq_states)
        matching_user_ids = set()

        for user_id, user_pref in guild_user_prefs.items():
            matches = False

            if user_pref.get("MagnitudeMentionEnabled"):
                threshold = user_pref.get("MagnitudeThreshold")
                if threshold is not None and eq_magnitude is not None and eq_magnitude >= threshold:
                    matches = True

            user_states = user_pref.get("States", set())
            if user_states and eq_states and user_states.intersection(eq_states):
                matches = True

            user_countries = user_pref.get("Countries", set())
            if user_countries and eq_countries and user_countries.intersection(eq_countries):
                matches = True

            if not matches:
                continue

            matching_user_ids.add(user_id)

        return sorted(matching_user_ids, key=int)


    def cog_unload(self):
        self.poll_quakes.cancel()


    async def _initialize(self):
        """This func is run when on LiveTracking __init__ is called.
        Ensures eq_notify_db and guild_prefs are loaded before starting the polling loop."""

        # Create or set guild preferences
        for guild in self.client.guilds:
            self._ensure_guild_pref(str(guild.id))

        # Set eq_notify_db guild.id default key object
        for guild in self.client.guilds:
            if self.eq_notify_db.get(str(guild.id)):
                continue
            self.eq_notify_db[str(guild.id)] = {}

        save_guild_prefs_to_sqlite(self.guild_prefs)
        save_eq_notify_db_to_sqlite(self.eq_notify_db)
        self.poll_quakes.start()


    def _register_event_for_all_guilds(self, earthquake_id: str):
        for guild in self.client.guilds:
            guild_id = str(guild.id)
            self.eq_notify_db.setdefault(guild_id, {})
            self.eq_notify_db[guild_id].setdefault(earthquake_id, False)

    def _pager_icon_for_level(self, pager_alert_level: str | None):
        match pager_alert_level:
            case "green":
                return "🟩"
            case "yellow":
                return "🟨"
            case "orange":
                return "🟧"
            case "red":
                return "🟥"
            case _:
                return "-"

    def _format_event_time(self, event_time_utc: datetime | None):
        if event_time_utc is None:
            return "unknown"
        dt = event_time_utc.astimezone(ZoneInfo("America/New_York"))
        return dt.strftime("%m/%d/%Y - %I:%M %p")

    def _event_to_eq_data(self, event: EarthquakeEvent):
        depth = "unknown" if event.depth_km is None else str(event.depth_km)
        return {
            "pager_lvl_icon": self._pager_icon_for_level(event.pager_alert_level),
            "place": event.place,
            "magnitude": event.magnitude,
            "url": event.url,
            "time": self._format_event_time(event.event_time_utc),
            "earthquake_id": event.earthquake_id,
            "pager_alert_level": event.pager_alert_level,
            "tsunami_potential": bool(event.tsunami_potential) if event.tsunami_potential is not None else False,
            "depth": depth,
            "latitude": event.latitude,
            "longitude": event.longitude,
            "significance": event.significance,
        }

    async def save_earthquakes(self, earthquakes: list[dict]):
        """Save all earthquake payloads to the eq_db object."""
        for eq_data in earthquakes:
            eq_id = eq_data.get("earthquake_id")
            if not eq_id:
                continue

            self._register_event_for_all_guilds(eq_id)
            if self.eq_db.get(eq_id):
                continue

            logging.info("||=*=|| Saving Earthquake ||=*=|| %s", eq_id)
            self.eq_db[eq_id] = {
                "pager_lvl_icon": eq_data["pager_lvl_icon"],
                "place": eq_data["place"],
                "magnitude": eq_data["magnitude"],
                "url": eq_data["url"],
                "time": eq_data["time"],
                "earthquake_id": eq_data["earthquake_id"],
                "pager_alert_level": eq_data["pager_alert_level"],
                "tsunami_potential": eq_data["tsunami_potential"],
                "depth": eq_data["depth"],
                "latitude": eq_data["latitude"],
                "longitude": eq_data["longitude"],
                "significance": eq_data["significance"],
            }
            logging.info("||=*=|| Earthquake Saved! ||=*=|| ")

    async def notify_guild(self, earthquakes: list[dict], guild: discord.Guild):
        guild_id = str(guild.id)
        self._ensure_guild_pref(guild_id)
        if not self.eq_notify_db.get(guild_id):
            self.eq_notify_db[guild_id] = {}
        plot_style = self.guild_prefs[guild_id].get("PlotStyle", 0)
        channel = get(guild.text_channels, name="quake-updates")
        if not channel:
            return

        async def send_alert(eq_data: dict):
            image_path = f"eq_plot_{guild_id}_{sanitize_filename(eq_data['earthquake_id'])}.png"
            eq_embed, img_file = create_embed_quake_alert(
                eq_data,
                plot_style=plot_style,
                image_path=image_path,
            )
            mention_chunks = build_mention_chunks(self._get_matching_user_ids(guild, eq_data))
            first_message_mentions = mention_chunks[0] if mention_chunks else None
            try:
                sent_message = await channel.send(
                    content=first_message_mentions,
                    embed=eq_embed,
                    file=img_file,
                )
                for extra_chunk in mention_chunks[1:]:
                    await channel.send(extra_chunk)
                pin_threshold = self.guild_prefs[guild_id].get("PinMagnitude")
                eq_magnitude = eq_data.get("magnitude")
                if (
                    pin_threshold is not None
                    and eq_magnitude is not None
                    and float(eq_magnitude) >= float(pin_threshold)
                ):
                    try:
                        await sent_message.pin(
                            reason=f"Earthquake magnitude {eq_magnitude} meets pin threshold {pin_threshold}"
                        )
                    except discord.Forbidden:
                        logging.warning(
                            "Missing permission to pin messages in #%s for guild %s.",
                            channel.name,
                            guild.name,
                        )
                    except discord.HTTPException as err:
                        logging.warning(
                            "Failed to pin earthquake alert %s in guild %s: %s",
                            eq_data.get("earthquake_id"),
                            guild.name,
                            err,
                        )
            finally:
                if os.path.exists(image_path):
                    os.remove(image_path)
            self.eq_notify_db[guild_id][eq_data["earthquake_id"]] = True

        for eq_data in earthquakes:
            if self.eq_notify_db[guild_id].get(eq_data["earthquake_id"]):
                continue

            if earthquake_passes_guild_magnitude_filter(
                eq_data,
                self.guild_prefs[guild_id]["MinMagnitude"],
            ):
                await send_alert(eq_data)
                continue

            if self.guild_prefs[guild_id]["MinMagnitude"] == 4:
                logging.info(
                    "Significant only quakes selected,"
                    " but not configured(how to parse these from the hourly all eq feed?)"
                )
                continue

            threshold_label = {
                1: "1.0",
                2: "2.5",
                3: "4.5",
            }.get(self.guild_prefs[guild_id]["MinMagnitude"], "unknown")
            logging.info(
                "Magnitude %s is too low (<%s), skipping message for guild: %s",
                eq_data.get("magnitude"),
                threshold_label,
                guild,
            )


    @tasks.loop(seconds=60.0)
    async def poll_quakes(self):
        """Every 10 minutes, poll the api for new earthquakes. When a new quake is detected,
         add the quake_id to the eq_notify_db dictionary. If eq_notify_db.guildid.quakeid = false,
         that guild has not reported on the eq If the quake is in the dict, it will be ignored."""
        print("Guild prefs from start of poll quakes cmd: ", self.guild_prefs)
        start_time = time.perf_counter()
        logging.info("%s function initiated.", colorize("poll_quakes", "blue"))

        # First fetch latest eq_db
        self.eq_db = get_eq_db()
        logging.info("%s loaded! %s total earthquakes on record.",
                     colorize("eq_db", "yellow"),
                     len(self.eq_db))

        await self.client.wait_until_ready()
        async with aiohttp.ClientSession() as session:
            source_events = await fetch_events_from_sources(session, self.event_sources)

        if len(source_events) == 0:
            logging.info("No earthquakes detected from configured sources.")
            return

        earthquake_payloads = [self._event_to_eq_data(event) for event in source_events]

        # Save earthquakes once after fetching all configured sources.
        await self.save_earthquakes(earthquake_payloads)

        for guild in self.client.guilds:
            logging.info("Attempting to notify guild: %s", guild.name)
            await self.notify_guild(earthquake_payloads, guild)
            logging.info("Completed notifying guild: %s", guild.name)

        end_time = time.perf_counter()

        save_eq_db_to_sqlite(self.eq_db)
        save_eq_notify_db_to_sqlite(self.eq_notify_db)
        logging.info("%s function completed. Elapsed %.2f seconds.",
                     colorize("poll_quakes", "blue"),
                     end_time - start_time)

    @commands.hybrid_command(name="top-10-largest-today",aliases=["top10"])
    async def top10(self, ctx: commands.Context):
        """Send an embed message to the channel containing
         the top 10 earthquakes that occurred on today's date."""
        df = load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])
        df["date"] = df["time"].dt.date  # Parse dates out of the time col
        today = datetime.today().date()  # Get the date
        today_str = today.strftime("%B %d, %Y")  # Get str of today's date
        df_today = df[df["date"] == today].copy()  # Filter for today's data only

        embed = discord.Embed(
            title=f"Top 10 Largest Earthquakes {today_str}",
            color=discord.Color.gold(),
            )

        df_today.sort_values(by=["magnitude"], ascending=False, inplace=True)
        top10: pd.DataFrame = df_today[['place', 'magnitude']].head(10)

        line_count = 1
        for _, row in top10.iterrows():
            formatted_line = f"{row['place']:<50} | MAG:{row['magnitude']:>4}"
            embed.add_field(name=f"#{line_count}", value=formatted_line, inline=False)
            line_count += 1

        await ctx.send(embed=embed)


    @commands.hybrid_command(name="top-10-largest-30days", aliases=["top10month"])
    async def top10month(self, ctx: commands.Context):
        """Send an embed message to the channel containing
         the top 10 earthquakes from the last 30 days."""
        df = load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])

        now = datetime.now()
        cutoff = now - timedelta(days=30)
        df_last_30 = df[df["time"] >= cutoff].copy()

        embed = discord.Embed(
            title="Top 10 Largest Earthquakes (Last 30 Days)",
            color=discord.Color.gold(),
        )

        df_last_30.sort_values(by=["magnitude"], ascending=False, inplace=True)
        top10: pd.DataFrame = df_last_30[['place', 'magnitude']].head(10)

        line_count = 1
        for _, row in top10.iterrows():
            formatted_line = f"{row['place']:<50} | MAG:{row['magnitude']:>4}"
            embed.add_field(name=f"#{line_count}", value=formatted_line, inline=False)
            line_count += 1

        await ctx.send(embed=embed)


    @commands.hybrid_command(name="today")
    async def today(self, ctx: commands.Context):
        """Output a summary of today's earthquakes.
        Plot all earthquakes to a map and send as message."""
        if ctx.interaction is not None:
            await ctx.defer()

        df = load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])
        df["date"] = df["time"].dt.date
        today = datetime.today().date()
        df_today = df[df["date"] == today]  # Filter for today's data only

        plot_style = 0
        if ctx.guild is not None:
            self._ensure_guild_pref(str(ctx.guild.id))
            plot_style = self.guild_prefs[str(ctx.guild.id)]["PlotStyle"]
        plot_daily_earthquakes(df_today, plot_style=plot_style)
        title_str = (f"Total Eathquakes Today: {len(df_today)}\n "
                     f">= Magnitude 3: {len(df_today[df_today['magnitude'] >= 3.0])}")
        embed = discord.Embed(
            title=title_str,
            color=discord.Color.gold(),
        )

        embed.add_field(name="Average Magnitude",
                        value=f"{df_today['magnitude'].mean():.2f}",
                        inline=True)
        embed.add_field(name="Highest Magnitude",
                        value=f"{df_today['magnitude'].max():.2f}",
                        inline=True)
        embed.add_field(name="Lowest Magnitude",
                        value=f"{df_today['magnitude'].min():.2f}",
                        inline=True)

        img_file = discord.File("eq_plot_all_today.png", filename="earthquake.png")
        embed.set_image(url="attachment://earthquake.png")

        await ctx.send(embed=embed, file=img_file)


    @commands.hybrid_command(name="maptoday")
    async def maptoday(self, ctx: commands.Context):
        """Plot all earthquakes from today on a flat map and send as message."""
        if ctx.interaction is not None:
            await ctx.defer()

        df = load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])
        df["date"] = df["time"].dt.date
        today = datetime.today().date()
        df_today = df[df["date"] == today].copy()

        plot_style = 0
        if ctx.guild is not None:
            self._ensure_guild_pref(str(ctx.guild.id))
            plot_style = self.guild_prefs[str(ctx.guild.id)]["PlotStyle"]
        plot_daily_earthquakes(df_today, plot_style=plot_style)

        embed = discord.Embed(
            title=f"Earthquakes Today ({today.strftime('%B %d, %Y')})",
            color=discord.Color.gold(),
        )

        img_file = discord.File("eq_plot_all_today.png", filename="earthquake.png")
        embed.set_image(url="attachment://earthquake.png")

        await ctx.send(embed=embed, file=img_file)


    @commands.hybrid_command(name="export-earthquakes", aliases=["exporteqcsv", "exportcsv"])
    async def export_earthquakes(self, ctx: commands.Context):
        """Export all earthquake records to CSV and upload files to the current channel."""
        if ctx.interaction is not None:
            await ctx.defer()

        export_data = fetch_earthquake_export_data()
        total_quakes = export_data["count"]

        if total_quakes == 0:
            await ctx.send("No earthquake data is available to export.")
            return

        try:
            csv_parts = build_earthquake_csv_parts(export_data["rows"])
        except ValueError as err:
            await ctx.send(f"Export failed: {err}")
            return

        split_count = len(csv_parts)
        total_csv_bytes = sum(len(part["bytes"]) for part in csv_parts)
        avg_mag = export_data["avg_magnitude"]
        min_mag = export_data["min_magnitude"]
        max_mag = export_data["max_magnitude"]

        avg_mag_str = f"{avg_mag:.2f}" if avg_mag is not None else "N/A"
        min_mag_str = f"{min_mag:.2f}" if min_mag is not None else "N/A"
        max_mag_str = f"{max_mag:.2f}" if max_mag is not None else "N/A"

        summary = (
            f"Earthquake export ready.\n"
            f"Rows: {total_quakes:,}\n"
            f"Average magnitude: {avg_mag_str}\n"
            f"Min magnitude: {min_mag_str}\n"
            f"Max magnitude: {max_mag_str}\n"
            f"Total CSV size: {format_bytes(total_csv_bytes)} ({total_csv_bytes:,} bytes)\n"
            f"Split count: {split_count} file(s)\n"
            f"Per-file limit: {format_bytes(DISCORD_FILE_LIMIT_BYTES)} hard max"
        )
        await ctx.send(summary)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        for idx, part in enumerate(csv_parts, start=1):
            filename = f"earthquakes_export_{timestamp}_part{idx:03d}.csv"
            payload = io.BytesIO(part["bytes"])
            csv_file = discord.File(payload, filename=filename)
            await ctx.send(
                content=(
                    f"Export file {idx}/{split_count} | "
                    f"Rows: {part['row_count']:,} | "
                    f"Size: {format_bytes(len(part['bytes']))} ({len(part['bytes']):,} bytes)"
                ),
                file=csv_file,
            )

    @commands.hybrid_group(name="alert-me", aliases=["alertme"], invoke_without_command=True)
    async def alert_me(self, ctx: commands.Context):
        """Manage personal earthquake alerts for this guild."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        if ctx.invoked_subcommand is not None:
            return

        await ctx.send(
            "Use one of these subcommands:\n"
            "/alert-me magnitude <threshold>\n"
            "/alert-me disable-magnitude\n"
            "/alert-me add-state <state>\n"
            "/alert-me remove-state <state>\n"
            "/alert-me add-country <country>\n"
            "/alert-me remove-country <country>\n"
            "/my-alerts"
        )

    @alert_me.command(name="magnitude")
    async def alert_me_magnitude(self, ctx: commands.Context, threshold: float):
        """Enable personal mention alerts for earthquakes above a magnitude threshold."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return
        if threshold < 0 or threshold > 10:
            await ctx.send("Magnitude threshold must be between 0.0 and 10.0.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        self._set_user_magnitude_alert(guild_id, user_id, threshold)
        await ctx.send(
            f"Personal magnitude mentions enabled for <@{user_id}> at M{threshold:.1f}+ in this server."
        )

    @alert_me.command(name="disable-magnitude")
    async def alert_me_disable_magnitude(self, ctx: commands.Context):
        """Disable personal magnitude mention alerts."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        self._disable_user_magnitude_alert(guild_id, user_id)
        await ctx.send("Personal magnitude mention alerts are now disabled.")

    @alert_me.command(name="add-state")
    async def alert_me_add_state(self, ctx: commands.Context, *, state: str):
        """Subscribe to personal alerts for a US state (abbr or full name)."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        state_code = normalize_state_code(state)
        if state_code is None:
            await ctx.send(
                "Invalid US state. Use a 2-letter code like `OH` or full name like `Ohio`."
            )
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)
        if state_code in user_pref["States"]:
            await ctx.send(
                f"You are already subscribed to {state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})."
            )
            return

        self._add_user_state_alert(guild_id, user_id, state_code)
        await ctx.send(
            f"Added personal state alert for {state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})."
        )

    @alert_me.command(name="remove-state")
    async def alert_me_remove_state(self, ctx: commands.Context, *, state: str):
        """Unsubscribe from personal alerts for a US state (abbr or full name)."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        state_code = normalize_state_code(state)
        if state_code is None:
            await ctx.send(
                "Invalid US state. Use a 2-letter code like `OH` or full name like `Ohio`."
            )
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)
        if state_code not in user_pref["States"]:
            await ctx.send(
                f"You do not have a state alert configured for {state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})."
            )
            return

        self._remove_user_state_alert(guild_id, user_id, state_code)
        await ctx.send(
            f"Removed personal state alert for {state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})."
        )

    @alert_me.command(name="add-country")
    async def alert_me_add_country(self, ctx: commands.Context, *, country: str):
        """Subscribe to personal alerts for a country."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        country_name = normalize_country_name(country)
        if country_name is None:
            await ctx.send("Invalid country name. Example: `Iran`, `Chile`, `United States`.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)
        if country_name in user_pref["Countries"]:
            await ctx.send(f"You are already subscribed to country alerts for {country_name}.")
            return

        self._add_user_country_alert(guild_id, user_id, country_name)
        await ctx.send(f"Added personal country alert for {country_name}.")

    @alert_me.command(name="remove-country")
    async def alert_me_remove_country(self, ctx: commands.Context, *, country: str):
        """Unsubscribe from personal alerts for a country."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        country_name = normalize_country_name(country)
        if country_name is None:
            await ctx.send("Invalid country name. Example: `Iran`, `Chile`, `United States`.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)
        if country_name not in user_pref["Countries"]:
            await ctx.send(f"You do not have a country alert configured for {country_name}.")
            return

        self._remove_user_country_alert(guild_id, user_id, country_name)
        await ctx.send(f"Removed personal country alert for {country_name}.")

    @commands.hybrid_command(name="my-alerts")
    async def my_alerts(self, ctx: commands.Context):
        """List your personal alert settings for this guild."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        guild_id = str(ctx.guild.id)
        user_id = str(ctx.author.id)
        user_pref = self._ensure_user_pref(guild_id, user_id)

        if user_pref["MagnitudeMentionEnabled"] and user_pref["MagnitudeThreshold"] is not None:
            magnitude_text = f"enabled at M{user_pref['MagnitudeThreshold']:.1f}+"
        else:
            magnitude_text = "disabled"

        state_codes = sorted(user_pref["States"])
        if state_codes:
            state_text = ", ".join(
                f"{state_code} ({US_STATE_ABBREV_TO_NAME[state_code]})"
                for state_code in state_codes
            )
        else:
            state_text = "none"

        country_names = sorted(user_pref["Countries"])
        country_text = ", ".join(country_names) if country_names else "none"

        await ctx.send(
            f"Personal earthquake alerts for <@{user_id}> in **{ctx.guild.name}**\n"
            f"- Magnitude mentions: {magnitude_text}\n"
            f"- State subscriptions: {state_text}\n"
            f"- Country subscriptions: {country_text}\n"
            "- State/country matching is inferred from USGS `place` text."
        )

    @commands.hybrid_command(name="config_map")
    async def config_map(self, ctx: commands.Context):
        """Configure the map style for this guild with reaction-based style previews."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        if ctx.interaction is not None:
            await ctx.defer()

        if len(MAP_STYLE_OPTIONS) > len(NUMBER_REACTIONS):
            await ctx.send("Too many map styles are configured for the available number reactions.")
            return

        guild_id = str(ctx.guild.id)
        self._ensure_guild_pref(guild_id)

        files = []
        embeds = []
        preview_paths = []
        style_lines = []
        reaction_to_style = {}

        try:
            for idx, style in enumerate(MAP_STYLE_OPTIONS):
                emoji = NUMBER_REACTIONS[idx]
                reaction_to_style[emoji] = idx
                style_lines.append(f"{emoji} | {style['label']} (`{style['map_style']}`)")

                preview_path = f"map_style_preview_{guild_id}_{idx}.png"
                preview_paths.append(preview_path)

                plot_to_img_with_plotly(
                    DEFAULT_MAP_LONGITUDE,
                    DEFAULT_MAP_LATITUDE,
                    "Example Location",
                    "Preview",
                    filename=preview_path,
                    plot_style=idx,
                )

                attachment_name = f"map_style_{idx}.png"
                files.append(discord.File(preview_path, filename=attachment_name))

                embed = discord.Embed(
                    title=f"{emoji} {style['label']}",
                    description=f"`{style['map_style']}`",
                    color=discord.Color.blurple(),
                )
                embed.set_image(url=f"attachment://{attachment_name}")
                embeds.append(embed)
        except Exception as err:
            for path in preview_paths:
                if os.path.exists(path):
                    os.remove(path)
            await ctx.send(f"Unable to generate map style previews: {err}")
            return

        prompt = (
            "Choose a map style for this server by reacting with a number.\n"
            "Preview coordinate: (-74.00, 40.71)\n\n"
            + "\n".join(style_lines)
        )

        try:
            msg = await ctx.send(content=prompt, embeds=embeds, files=files)
        finally:
            for path in preview_paths:
                if os.path.exists(path):
                    os.remove(path)

        for emoji in reaction_to_style:
            await msg.add_reaction(emoji)

        def check(reaction, usr):
            return (
                usr.id == ctx.author.id
                and reaction.message.id == msg.id
                and str(reaction.emoji) in reaction_to_style
            )

        try:
            reaction, usr = await self.client.wait_for(
                "reaction_add",
                timeout=120.0,
                check=check,
            )
            logging.info("Reaction : %s, user: %s", reaction, usr.name)
        except asyncio.TimeoutError:
            await ctx.send("Timed out. You must re-run /config_map and react within 2 minutes.")
            return

        selected_style_idx = reaction_to_style[str(reaction.emoji)]
        self.guild_prefs[guild_id]["PlotStyle"] = selected_style_idx
        save_guild_prefs_to_sqlite(self.guild_prefs)
        selected_style, _ = get_map_style_option(selected_style_idx)
        await ctx.send(
            f"Map style set to {selected_style['label']} (`{selected_style['map_style']}`) "
            "for this server."
        )


    @commands.hybrid_command(name="config")
    async def config(self, ctx: commands.Context):
        """Configure the bots settings for a specific guild."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        self._ensure_guild_pref(str(ctx.guild.id))

        msg = await ctx.send(
            "Please choose a minimum magnitude to report on.\n\n"
            "0️⃣ | All earthquakes (Caution.. lots)\n"
            "1️⃣ | 1.0 and above\n"
            "2️⃣ | 2.5 and above\n"
            "3️⃣ | 4.5 and above\n"
            "4️⃣ | Significant only"
        )
        reactions = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣"]

        for reaction in reactions:
            await msg.add_reaction(reaction)

        try:
            def check(reaction, usr):
                return (
                    usr.id == ctx.author.id
                    and reaction.message.id == msg.id
                    and str(reaction.emoji) in reactions
                )

            reaction, usr = await self.client.wait_for(
                "reaction_add",
                timeout=60.0,
                check=check,
            )

            logging.info(f"Reaction : %s, user: %s", reaction, usr.name)
            emoji = str(reaction.emoji)

        except asyncio.TimeoutError:
            await ctx.send("Timed out. You must re-run .config , and react within 1 minute.")
            return

        match emoji:
            case "0️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 0
                await ctx.send("All earthquakes are being reported on by the minute.")
            case "1️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 1
                await ctx.send("1.0+ earthquakes are being reported on by the minute.")
            case "2️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 2
                await ctx.send("2.5+ earthquakes are being reported on by the minute.")
            case "3️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 3
                await ctx.send("4.5+ earthquakes are being reported on by the minute.")
            case "4️⃣":
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 4
                await ctx.send("Only significant earthquakes are being reported on by the minute.")
            case _:
                await ctx.send("That is not a valid emoji.")

        save_guild_prefs_to_sqlite(self.guild_prefs)
        await msg.delete()

    @commands.hybrid_command(name="pin-magnitude")
    async def pin_magnitude(self, ctx: commands.Context, magnitude: float):
        """Set the minimum earthquake magnitude that triggers pinning alert messages."""
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        if magnitude < 0:
            await ctx.send("Please provide a non-negative magnitude.")
            return

        guild_id = str(ctx.guild.id)
        self._ensure_guild_pref(guild_id)
        self.guild_prefs[guild_id]["PinMagnitude"] = float(magnitude)
        save_guild_prefs_to_sqlite(self.guild_prefs)
        await ctx.send(
            f"Earthquake alerts with magnitude **{magnitude:g}+** will now be pinned in their alert channel."
        )


async def setup(bot):
    """Called when the cog is loaded."""
    await bot.add_cog(LiveTracking(bot))
