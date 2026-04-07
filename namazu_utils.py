"""Shared utility functions and constants for Namazu's earthquake features."""

import csv
import io
import logging
import os
import pickle
import re
import sqlite3
import textwrap
import time

from colorlog.escape_codes import escape_codes as c
import discord
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

try:
    import pycountry
except ImportError:  # pragma: no cover - optional dependency in local dev
    pycountry = None

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
        "label": "Carto Voyager",
        "map_style": "carto-voyager",
        "font_color": "black",
        "paper_bgcolor": "white",
    },
    {
        "label": "USGS Imagery",
        "map_style": "white-bg",
        "font_color": "black",
        "paper_bgcolor": "white",
        "map_layers": [
            {
                "below": "traces",
                "sourcetype": "raster",
                "sourceattribution": "United States Geological Survey",
                "source": [
                    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
                ],
            }
        ],
    },
    {
        "label": "USGS Imagery + Borders",
        "map_style": "white-bg",
        "font_color": "black",
        "paper_bgcolor": "white",
        "map_layers": [
            {
                "below": "traces",
                "sourcetype": "raster",
                "sourceattribution": "United States Geological Survey",
                "source": [
                    "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
                ],
            },
            {
                "sourcetype": "raster",
                "sourceattribution": (
                    "Esri, Garmin, HERE, OpenStreetMap contributors, "
                    "and the GIS User Community"
                ),
                "source": [
                    "https://services.arcgisonline.com/arcgis/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}"
                ],
            },
        ],
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
    depth_col = [row.get("depth") for row in eq_db.values()]
    latitude_col = [row["latitude"] for row in eq_db.values()]
    longitude_col = [row["longitude"] for row in eq_db.values()]
    significance_col = [row.get("significance") for row in eq_db.values()]

    df_ready_dict = {"earthquake_id": earthquake_id_col,
                     "place": places_col,
                     "magnitude": magnitude_col,
                     "url": url_col,
                     "time": time_col,
                     "pager_alert_level": pager_alert_level_col,
                     "tsunami_potential": tsunami_potential_col,
                     "depth": depth_col,
                     "latitude": latitude_col,
                     "longitude": longitude_col,
                     "significance": significance_col,
                     }
    df = pd.DataFrame.from_dict(df_ready_dict)
    end_time = time.perf_counter()
    logging.info("||=*=|| DataFrame Ready in %.2f seconds. ||=*=||", end_time - start_time)
    return df


def _earthquake_region_label(place: str):
    place_text = " ".join(str(place or "").split())
    if not place_text:
        return "Unknown"

    state_codes = sorted(infer_us_state_codes_from_place(place_text))
    if state_codes:
        return US_STATE_ABBREV_TO_NAME[state_codes[0]]

    country_names = sorted(infer_country_names_from_place(place_text, set(state_codes)))
    if country_names:
        return country_names[0]

    if "," in place_text:
        trailing_segment = place_text.split(",")[-1].strip()
        if trailing_segment:
            return trailing_segment.title() if trailing_segment.isupper() else trailing_segment

    shortened = textwrap.shorten(place_text, width=32, placeholder="...")
    return shortened.title() if shortened.isupper() else shortened


def _prepare_earthquake_visual_df(eq_df: pd.DataFrame):
    df = eq_df.copy()
    expected_columns = (
        "earthquake_id",
        "place",
        "magnitude",
        "time",
        "depth",
        "latitude",
        "longitude",
        "significance",
    )

    for column in expected_columns:
        if column not in df.columns:
            df[column] = pd.NA

    df["time"] = pd.to_datetime(df["time"], errors="coerce")
    df["magnitude"] = pd.to_numeric(df["magnitude"], errors="coerce")
    df["depth_km"] = pd.to_numeric(df["depth"], errors="coerce")
    df["latitude"] = pd.to_numeric(df["latitude"], errors="coerce")
    df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
    df["significance"] = pd.to_numeric(df["significance"], errors="coerce")
    df["date"] = df["time"].dt.date
    df["region_label"] = df["place"].apply(_earthquake_region_label)
    return df


def _write_placeholder_chart(filename: str, title: str, message: str):
    fig = go.Figure()
    fig.add_annotation(
        x=0.5,
        y=0.5,
        xref="paper",
        yref="paper",
        text=message,
        showarrow=False,
        font={"size": 18, "color": "#475569"},
    )
    fig.update_layout(
        title={"text": title, "x": 0.5, "xanchor": "center"},
        template="plotly_white",
        paper_bgcolor="white",
        plot_bgcolor="#f8fafc",
        margin={"l": 40, "r": 40, "t": 70, "b": 40},
        xaxis={"visible": False},
        yaxis={"visible": False},
    )
    fig.write_image(filename, width=1000, height=550)


def _apply_overview_layout(
    fig: go.Figure,
    title: str,
    *,
    xaxis_title: str | None = None,
    yaxis_title: str | None = None,
):
    fig.update_layout(
        title={"text": title, "x": 0.5, "xanchor": "center"},
        template="plotly_white",
        paper_bgcolor="white",
        plot_bgcolor="#f8fafc",
        font={"color": "#0f172a"},
        margin={"l": 60, "r": 40, "t": 70, "b": 60},
        legend={
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.02,
            "xanchor": "right",
            "x": 1,
        },
    )
    fig.update_xaxes(
        title_text=xaxis_title,
        gridcolor="#e2e8f0",
        linecolor="#cbd5e1",
        showline=True,
        zeroline=False,
    )
    fig.update_yaxes(
        title_text=yaxis_title,
        gridcolor="#e2e8f0",
        linecolor="#cbd5e1",
        showline=True,
        zeroline=False,
    )


def plot_earthquake_overview_map(
    eq_df: pd.DataFrame,
    filename="eq_overview_map.png",
    plot_style=0,
):
    """Plot a magnitude-weighted map view for an overview card."""
    style, _ = get_map_style_option(plot_style)
    df = _prepare_earthquake_visual_df(eq_df)
    map_df = df.dropna(subset=["latitude", "longitude"]).copy()

    if map_df.empty:
        _write_placeholder_chart(filename, "Earthquake Map", "No coordinate data is available yet.")
        return

    map_df["display_magnitude"] = map_df["magnitude"].fillna(0.0)
    map_df["marker_size"] = map_df["display_magnitude"].clip(lower=0).mul(4).add(8)
    map_df["magnitude_label"] = map_df["magnitude"].apply(
        lambda value: f"{value:.1f}" if pd.notna(value) else "N/A"
    )
    map_df["time_label"] = map_df["time"].dt.strftime("%b %d, %Y %I:%M %p").fillna("Unknown time")

    fig = go.Figure()
    fig.add_trace(
        go.Scattermap(
            lon=map_df["longitude"],
            lat=map_df["latitude"],
            mode="markers",
            customdata=map_df[["place", "magnitude_label", "time_label"]].to_numpy(),
            marker={
                "size": map_df["marker_size"],
                "color": map_df["display_magnitude"],
                "colorscale": "Turbo",
                "showscale": True,
                "colorbar": {"title": "Magnitude"},
                "opacity": 0.85,
            },
            hovertemplate=(
                "%{customdata[0]}<br>"
                "Magnitude %{customdata[1]}<br>"
                "%{customdata[2]}<extra></extra>"
            ),
        )
    )

    map_config = {
        "style": style["map_style"],
        "center": {
            "lon": float(map_df["longitude"].mean()),
            "lat": float(map_df["latitude"].mean()),
        },
        "zoom": 3.2 if len(map_df) == 1 else 0.7,
    }
    if style.get("map_layers"):
        map_config["layers"] = style["map_layers"]

    fig.update_layout(
        title={"text": "Earthquake Map", "x": 0.5, "xanchor": "center"},
        font={"color": style["font_color"]},
        margin={"r": 0, "t": 60, "l": 0, "b": 0},
        map=map_config,
        paper_bgcolor=style["paper_bgcolor"],
        plot_bgcolor=style["paper_bgcolor"],
        showlegend=False,
    )
    fig.write_image(filename, width=1000, height=550)


def plot_earthquake_activity_timeline(eq_df: pd.DataFrame, filename="eq_activity_timeline.png"):
    """Plot daily earthquake counts with recent magnitude trends."""
    df = _prepare_earthquake_visual_df(eq_df)
    timeline_df = df.dropna(subset=["time"]).copy()

    if timeline_df.empty:
        _write_placeholder_chart(
            filename,
            "Daily Activity",
            "No timestamped earthquakes are available for this window.",
        )
        return

    daily = (
        timeline_df.groupby("date", dropna=True)
        .agg(
            quake_count=("earthquake_id", "count"),
            avg_magnitude=("magnitude", "mean"),
            max_magnitude=("magnitude", "max"),
        )
        .reset_index()
    )

    fig = make_subplots(specs=[[{"secondary_y": True}]])
    fig.add_trace(
        go.Bar(
            x=daily["date"],
            y=daily["quake_count"],
            name="Quakes",
            marker_color="#2563eb",
            hovertemplate="%{x|%b %d, %Y}<br>%{y} quakes<extra></extra>",
        ),
        secondary_y=False,
    )
    fig.add_trace(
        go.Scatter(
            x=daily["date"],
            y=daily["max_magnitude"],
            name="Max Mag",
            mode="lines+markers",
            line={"color": "#f97316", "width": 3},
            hovertemplate="%{x|%b %d, %Y}<br>Max magnitude %{y:.1f}<extra></extra>",
        ),
        secondary_y=True,
    )

    if daily["avg_magnitude"].notna().any():
        fig.add_trace(
            go.Scatter(
                x=daily["date"],
                y=daily["avg_magnitude"],
                name="Avg Mag",
                mode="lines",
                line={"color": "#0f766e", "dash": "dot", "width": 2},
                hovertemplate="%{x|%b %d, %Y}<br>Average magnitude %{y:.2f}<extra></extra>",
            ),
            secondary_y=True,
        )

    _apply_overview_layout(fig, "Daily Activity", xaxis_title="Date", yaxis_title="Quake Count")
    fig.update_xaxes(tickformat="%b %d")
    fig.update_yaxes(rangemode="tozero", secondary_y=False, title_text="Quake Count")
    fig.update_yaxes(rangemode="tozero", secondary_y=True, title_text="Magnitude")
    fig.write_image(filename, width=1000, height=550)


def plot_earthquake_magnitude_distribution(
    eq_df: pd.DataFrame,
    filename="eq_magnitude_distribution.png",
):
    """Plot a histogram of magnitudes for the selected earthquake window."""
    df = _prepare_earthquake_visual_df(eq_df)
    magnitudes = df["magnitude"].dropna()

    if magnitudes.empty:
        _write_placeholder_chart(
            filename,
            "Magnitude Distribution",
            "No magnitude values are available for this window.",
        )
        return

    magnitude_span = float(magnitudes.max() - magnitudes.min()) if len(magnitudes) > 1 else 0.0
    if magnitude_span <= 2:
        bin_size = 0.25
    elif magnitude_span <= 5:
        bin_size = 0.5
    else:
        bin_size = 1.0

    fig = go.Figure()
    fig.add_trace(
        go.Histogram(
            x=magnitudes,
            xbins={"size": bin_size},
            marker_color="#10b981",
            marker_line={"color": "#047857", "width": 1},
            hovertemplate="Magnitude %{x}<br>%{y} quakes<extra></extra>",
        )
    )

    avg_magnitude = float(magnitudes.mean())
    median_magnitude = float(magnitudes.median())
    fig.add_vline(x=avg_magnitude, line_width=3, line_dash="dash", line_color="#1d4ed8")
    fig.add_vline(x=median_magnitude, line_width=3, line_dash="dot", line_color="#f97316")
    fig.add_annotation(
        x=avg_magnitude,
        y=1.03,
        yref="paper",
        text=f"Avg {avg_magnitude:.2f}",
        showarrow=False,
        font={"color": "#1d4ed8"},
    )
    fig.add_annotation(
        x=median_magnitude,
        y=0.95,
        yref="paper",
        text=f"Median {median_magnitude:.2f}",
        showarrow=False,
        font={"color": "#f97316"},
    )

    _apply_overview_layout(
        fig,
        "Magnitude Distribution",
        xaxis_title="Magnitude",
        yaxis_title="Earthquake Count",
    )
    fig.update_layout(bargap=0.06, showlegend=False)
    fig.update_yaxes(rangemode="tozero")
    fig.write_image(filename, width=1000, height=550)


def plot_earthquake_top_regions(eq_df: pd.DataFrame, filename="eq_top_regions.png"):
    """Plot the most active inferred regions for the selected earthquake window."""
    df = _prepare_earthquake_visual_df(eq_df)

    regions = (
        df.groupby("region_label", dropna=True)
        .agg(
            quake_count=("earthquake_id", "count"),
            avg_magnitude=("magnitude", "mean"),
            max_magnitude=("magnitude", "max"),
        )
        .reset_index()
    )

    if regions.empty:
        _write_placeholder_chart(
            filename,
            "Most Active Regions",
            "No region labels are available for this window.",
        )
        return

    regions.sort_values(
        by=["quake_count", "max_magnitude", "avg_magnitude", "region_label"],
        ascending=[False, False, False, True],
        inplace=True,
    )
    regions = regions.head(10).copy()
    regions.sort_values(by=["quake_count", "avg_magnitude"], ascending=[True, True], inplace=True)
    regions["avg_mag_label"] = regions["avg_magnitude"].apply(
        lambda value: f"{value:.2f}" if pd.notna(value) else "N/A"
    )
    regions["max_mag_label"] = regions["max_magnitude"].apply(
        lambda value: f"{value:.2f}" if pd.notna(value) else "N/A"
    )

    fig = go.Figure()
    fig.add_trace(
        go.Bar(
            x=regions["quake_count"],
            y=regions["region_label"],
            orientation="h",
            customdata=regions[["avg_mag_label", "max_mag_label"]].to_numpy(),
            marker={
                "color": regions["avg_magnitude"].fillna(0),
                "colorscale": [
                    [0.0, "#fde68a"],
                    [0.5, "#fb923c"],
                    [1.0, "#b91c1c"],
                ],
                "showscale": True,
                "colorbar": {"title": "Avg Mag"},
            },
            hovertemplate=(
                "%{y}<br>"
                "%{x} quakes<br>"
                "Avg magnitude %{customdata[0]}<br>"
                "Max magnitude %{customdata[1]}<extra></extra>"
            ),
        )
    )

    _apply_overview_layout(
        fig,
        "Most Active Regions",
        xaxis_title="Earthquake Count",
        yaxis_title=None,
    )
    fig.update_yaxes(categoryorder="array", categoryarray=regions["region_label"].tolist())
    fig.update_xaxes(rangemode="tozero")
    fig.write_image(filename, width=1000, height=550)


def _build_single_quake_title(mag, place: str):
    """Build a wrapped Plotly title that stays readable within the 400px export."""
    magnitude_line = f"Magnitude: {mag}"
    normalized_place = " ".join(str(place).split())
    if not normalized_place:
        return {
            "text": magnitude_line,
            "font": {"size": 16},
            "x": 0.5,
            "xanchor": "center",
            "automargin": True,
        }, 24

    def estimate_line_width(text: str, font_size: int):
        width_units = 0.0
        for char in text:
            if char == " ":
                width_units += 0.32
            elif char in ".,:;!|'`":
                width_units += 0.24
            elif char in "ilIjtfr()[]":
                width_units += 0.34
            elif char in "mwMW@#%&":
                width_units += 0.90
            elif char.isupper() or char.isdigit():
                width_units += 0.62
            else:
                width_units += 0.54
        return width_units * font_size

    def wrap_line_to_width(text: str, font_size: int, max_width_px: float):
        wrapped_lines = []
        current_line = ""

        for word in text.split():
            candidate = word if not current_line else f"{current_line} {word}"
            if estimate_line_width(candidate, font_size) <= max_width_px:
                current_line = candidate
                continue

            if current_line:
                wrapped_lines.append(current_line)
                current_line = word
            else:
                shortened = textwrap.shorten(text, width=max(len(word) - 1, 8), placeholder="...")
                wrapped_lines.append(shortened)
                current_line = ""
                break

        if current_line:
            wrapped_lines.append(current_line)
        return wrapped_lines

    max_title_width_px = 340
    min_font_size = 10
    max_font_size = 16
    single_line_title = f"{magnitude_line} {normalized_place}"

    for font_size in range(max_font_size, min_font_size - 1, -1):
        if estimate_line_width(single_line_title, font_size) <= max_title_width_px:
            title_config = {
                "text": single_line_title,
                "font": {"size": font_size},
                "x": 0.5,
                "xanchor": "center",
                "automargin": True,
            }
            return title_config, max(22, 6 + font_size)

    for font_size in range(max_font_size, min_font_size - 1, -1):
        wrapped_place_lines = wrap_line_to_width(normalized_place, font_size, max_title_width_px)
        if len(wrapped_place_lines) <= 2:
            title_lines = [magnitude_line, *wrapped_place_lines]
            title_config = {
                "text": "<br>".join(title_lines),
                "font": {"size": font_size},
                "x": 0.5,
                "xanchor": "center",
                "automargin": True,
            }
            title_margin = max(22, 6 + len(title_lines) * (font_size + 2))
            return title_config, title_margin

    wrapped_place_lines = wrap_line_to_width(normalized_place, min_font_size, max_title_width_px)
    max_place_lines = 3
    if len(wrapped_place_lines) > max_place_lines:
        remaining_text = " ".join(wrapped_place_lines[max_place_lines - 1:])
        wrapped_place_lines = wrapped_place_lines[:max_place_lines]
        wrapped_place_lines[-1] = textwrap.shorten(
            remaining_text,
            width=max(len(wrapped_place_lines[-1]) - 1, 12),
            placeholder="...",
        )

    title_lines = [magnitude_line, *wrapped_place_lines]
    title_config = {
        "text": "<br>".join(title_lines),
        "font": {"size": min_font_size},
        "x": 0.5,
        "xanchor": "center",
        "automargin": True,
    }
    title_margin = max(22, 6 + len(title_lines) * (min_font_size + 2))
    return title_config, title_margin


def plot_to_img_with_plotly(long, lat, place, mag, filename="eq_plot.png", plot_style=0):
    """Plot a single earthquake point and save as an image."""
    style, _ = get_map_style_option(plot_style)
    map_zoom = get_single_quake_zoom(place)
    title_config, title_margin = _build_single_quake_title(mag, place)

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

    map_config = {
        "style": style["map_style"],
        "center": {"lon": long, "lat": lat},
        "zoom": map_zoom,
    }
    if style.get("map_layers"):
        map_config["layers"] = style["map_layers"]

    fig.update_layout(
        title=title_config,
        font={"color": style["font_color"]},
        margin={"r": 0, "t": title_margin, "l": 0, "b": 0},
        map=map_config,
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

    map_config = {
        "style": style["map_style"],
        "center": {"lon": center_lon, "lat": center_lat},
        "zoom": zoom,
    }
    if style.get("map_layers"):
        map_config["layers"] = style["map_layers"]

    fig.update_layout(
        font={"color": style["font_color"]},
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        map=map_config,
        paper_bgcolor=style["paper_bgcolor"],
        plot_bgcolor=style["paper_bgcolor"],
        showlegend=False,
    )
    fig.write_image(filename, width=400, height=250)
