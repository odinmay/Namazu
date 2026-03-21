"""Everything related to live tracking is in this cog. The poll_quakes function is the main logic"""
import asyncio
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import pickle
import logging
import time
import os
import sqlite3

from colorlog.escape_codes import escape_codes as c
from discord.ext import tasks, commands
from discord.utils import get
import plotly.graph_objects as go
import pandas as pd
import discord
import aiohttp

logger = logging.getLogger("discord")

LIVE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"

GUILD_PREFS_PATH = "data/guild_prefs.pkl"
EQ_NOTIFY_DB_PATH = "data/eq_notify_db.pkl"
EQ_DB_PATH = "data/eq_db1.pkl"
SQLITE_DB_PATH = "data/namazu.db"


def ensure_data_dir():
    os.makedirs("data", exist_ok=True)


def get_sqlite_conn():
    ensure_data_dir()
    return sqlite3.connect(SQLITE_DB_PATH)


def init_sqlite():
    with get_sqlite_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_prefs (
                guild_id TEXT PRIMARY KEY,
                min_magnitude INTEGER NOT NULL DEFAULT 3,
                update_frequency INTEGER NOT NULL DEFAULT 0,
                update_channel_id INTEGER NOT NULL DEFAULT 0,
                plot_style INTEGER NOT NULL DEFAULT 0
            )
            """
        )
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
                guild_id, min_magnitude, update_frequency, update_channel_id, plot_style
            ) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id) DO UPDATE SET
                min_magnitude=excluded.min_magnitude,
                update_frequency=excluded.update_frequency,
                update_channel_id=excluded.update_channel_id,
                plot_style=excluded.plot_style
            """,
            [
                (
                    guild_id,
                    prefs.get("MinMagnitude", 3),
                    prefs.get("UpdateFrequency", 0),
                    prefs.get("UpdateChannelId", 0),
                    prefs.get("PlotStyle", 0),
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
            SELECT guild_id, min_magnitude, update_frequency, update_channel_id, plot_style
            FROM guild_prefs
            """
        ).fetchall()
    return {
        row[0]: {
            "MinMagnitude": row[1],
            "UpdateFrequency": row[2],
            "UpdateChannelId": row[3],
            "PlotStyle": row[4],
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

def plot_to_img_with_plotly(long, lat, place, mag):
    """Plot the single earthquake to a map and save the image."""
    label = mag

    # Create the figure
    fig = go.Figure()

    # Add the point
    fig.add_trace(go.Scattergeo(
        lon = [long],
        lat = [lat],
        text = label,
        mode = 'markers',
        marker=dict(size=6, color='red'),
        textposition="bottom left",
        textfont={"weight":700, "size":16, "color":'black'},
        hoverinfo='text',
    ))

    # Configure the globe layout
    fig.update_geos(
        projection_type="natural earth",  # Globe-style
        projection_scale=0.90,
        showcountries=True,
        showcoastlines=True,
        showland=True,
        landcolor="lightgray",
        oceancolor="lightblue",
        showocean=True,
        bgcolor="#232328"
    )
    title_str = "Magnitude: " + str(mag) + " " +  place
    if len(title_str) > 35:
        title_str = "Magnitude: " + str(mag) + "\n" + place

    # Set layout
    fig.update_layout(
        title=title_str,
        font={"color": 'white'},
        margin={"r":0,"t":30,"l":0,"b":0},
        geo=dict(
            projection_rotation=dict(lon=long, lat=lat) # Center globe on the point
        ),
        paper_bgcolor="#232328",
        plot_bgcolor="#232328",
    )
    # Save to PNG
    fig.write_image("eq_plot.png", width=400, height=250)


def colorize(text, color):
    """Colorize text in the terminal with colorlog helper func"""
    return f"{c[color]}{text}{c['reset']}"


def create_embed_quake_alert(earthquake_data: dict):
    # Check on the color and make embed the color, else make it gray
    plot_to_img_with_plotly(earthquake_data["longitude"],
                            earthquake_data["latitude"],
                            earthquake_data["place"],
                            earthquake_data["magnitude"])

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

    img_file = discord.File("eq_plot.png", filename="earthquake.png")
    embed.set_image(url="attachment://earthquake.png")

    embed.add_field(name="Time",
                    value=earthquake_data["time"],
                    inline=False)
    return embed, img_file


def plot_daily_earthquakes(eq_df: pd.DataFrame):
    # Create the figure
    fig = go.Figure()

    # Add the point
    fig.add_trace(go.Scattergeo(
        lon=eq_df["longitude"],
        lat=eq_df["latitude"],
        mode='markers',
        marker={"size":5, "color":'red'},
        textposition="bottom left",
        textfont={"weight":700, "size":16, "color":"black"},
        hoverinfo='text',
    ))

    # Configure the globe layout
    fig.update_geos(
        projection_type="natural earth",  # Globe-style
        projection_scale=1.0,
        showcountries=True,
        showcoastlines=True,
        showland=True,
        landcolor="lightgray",
        oceancolor="lightblue",
        showocean=True,
        bgcolor="#232328"
    )

    # Set layout
    fig.update_layout(
        font=dict(
            color='white'
        ),
        margin={"r": 0, "t": 0, "l": 0, "b": 0},
        paper_bgcolor="#232328",
        plot_bgcolor="#232328",
    )
    # Save to PNG
    fig.write_image("eq_plot_all_today.png", width=400, height=250)


class LiveTracking(commands.Cog):
    def __init__(self, client):
        self.client = client
        init_sqlite()
        migrate_pickle_data_if_needed()
        self.guild_prefs = get_guild_prefs()
        self.eq_notify_db = get_eq_notify_db()
        self.eq_db = get_eq_db()
        self.client.loop.create_task(self._initialize())


    def cog_unload(self):
        self.poll_quakes.cancel()


    async def _initialize(self):
        """This func is run when on LiveTracking __init__ is called.
        Ensures eq_notify_db and guild_prefs are loaded before starting the polling loop."""

        # Create or set guild preferences
        for guild in self.client.guilds:
            if self.guild_prefs.get(str(guild.id)):
                continue

            # Set default prefs if not in the prefs dict
            self.guild_prefs[str(guild.id)] = {"MinMagnitude": 3,
                                               "UpdateFrequency": 0,
                                               "UpdateChannelId": 0,
                                               "PlotStyle": 0}

        # Set eq_notify_db guild.id default key object
        for guild in self.client.guilds:
            if self.eq_notify_db.get(str(guild.id)):
                continue
            self.eq_notify_db[str(guild.id)] = {}

        save_guild_prefs_to_sqlite(self.guild_prefs)
        save_eq_notify_db_to_sqlite(self.eq_notify_db)
        self.poll_quakes.start()


    async def save_earthquakes(self, list_of_features: list):
        """Save all earthquake data from a list of features to the eq_db object and save the obj."""
        for feature in list_of_features:
            eq_data = await self.get_earthquake_data(feature)
            eq_id = eq_data.get("earthquake_id")

            if not self.eq_db.get(eq_id):
                logging.info("||=*=|| Saving Earthquake ||=*=|| %s", eq_data["earthquake_id"])
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


    async def notify_guild(self, features: list, guild: discord.Guild):
        for feature in features:
            eq_data = await self.get_earthquake_data(feature)

            if self.eq_notify_db[str(guild.id)].get(eq_data["earthquake_id"]):
                continue

            channel = get(guild.text_channels, name="quake-updates")
            if not channel:
                return

            # Filter the earthquake by guild preferred reporting magnitude
            match self.guild_prefs[str(guild.id)]["MinMagnitude"]:
                case 0:
                    # Create and send the message
                    eq_embed, img_file = create_embed_quake_alert(eq_data)
                    await channel.send(embed=eq_embed, file=img_file)
                    self.eq_notify_db[str(guild.id)][eq_data["earthquake_id"]] = True
                case 1:
                    if eq_data["magnitude"] >= 1.0:
                        # Create and send the message
                        eq_embed, img_file = create_embed_quake_alert(eq_data)
                        await channel.send(embed=eq_embed, file=img_file)
                        self.eq_notify_db[str(guild.id)][eq_data["earthquake_id"]] = True
                    else:
                        logging.info(
                            f"Magnitude {eq_data['magnitude']} is too low (<1.0)"
                            f" skipping message for guild: %s",
                            guild,)
                case 2:
                    if eq_data["magnitude"] >= 2.5:
                        # Create and send the message
                        eq_embed, img_file = create_embed_quake_alert(eq_data)
                        await channel.send(embed=eq_embed, file=img_file)
                        self.eq_notify_db[str(guild.id)][eq_data["earthquake_id"]] = True
                    else:
                        logging.info(
                            f"Magnitude {eq_data['magnitude']} is too low (<2.5)"
                            f" skipping message for guild: %s",
                            guild)
                case 3:
                    # User selected option 3: (4.5 or larger)
                    if eq_data["magnitude"] >= 4.5:
                        # Create and send the message
                        eq_embed, img_file = create_embed_quake_alert(eq_data)
                        await channel.send(embed=eq_embed, file=img_file)
                        self.eq_notify_db[str(guild.id)][eq_data["earthquake_id"]] = True
                    else:
                        logging.info(
                            f"Magnitude {eq_data['magnitude']} is too low (<4.5)"
                            f" skipping message for guild: %s",
                            guild)
                case 4:
                    #TODO Configure this option, find what classifies as significant.
                    logging.info(
                        "Significant only quakes selected,"
                        " but not configured(how to parse these from the hourly all eq feed?)")


    async def get_earthquake_data(self, feature_obj: dict):
        """Unpack the dictionary and format all values, return a formatted dict"""
        pager_alert_level = feature_obj.get("properties")["alert"]

        # Colored Square | Info - https://earthquake.usgs.gov/data/pager/onepager.php
        match pager_alert_level:
            case "green":
                pager_lvl_icon = "🟩"
            case "yellow":
                pager_lvl_icon = "🟨"
            case "orange":
                pager_lvl_icon = "🟧"
            case "red":
                pager_lvl_icon = "🟥"
            case _:
                pager_lvl_icon = "-"

        place = feature_obj.get("properties")["place"]
        mag = feature_obj.get("properties")["mag"]
        url = feature_obj.get("properties")["url"]
        time_ms = feature_obj.get("properties")["time"]
        time_seconds = time_ms / 1000
        utc_dt = datetime.fromtimestamp(time_seconds,tz=timezone.utc)
        dt = utc_dt.astimezone(ZoneInfo("America/New_York"))
        formatted_dt = dt.strftime("%m/%d/%Y - %I:%M %p")
        tsunami_potential = feature_obj.get("properties")["tsunami"]
        depth = feature_obj.get("properties").get("depth")
        longitude = feature_obj.get("geometry").get("coordinates")[0]
        latitude = feature_obj.get("geometry").get("coordinates")[1]
        significance = feature_obj.get("properties").get("sig")

        if not depth:
            depth = "unknown"
        else:
            depth = str(depth)

        earthquake_id = (str(mag) + "-"
                         + str(feature_obj.get("properties").get("code")) + "-"
                         + str(feature_obj.get("properties").get("time")))

        # Initialize eq_data.guild earthquake records
        for guild in self.client.guilds:
            if self.eq_notify_db.get(str(guild.id)):
                if self.eq_notify_db.get(str(guild.id)).get(earthquake_id):
                    continue
                else:
                    # Register that we see the earthquake and set value to false
                    # This false value is acting as the answer to a question, has this guild.id
                    # reported on this earthquake
                    self.eq_notify_db[str(guild.id)][earthquake_id] = False

        return {
            "pager_lvl_icon": pager_lvl_icon,
            "place": place,
            "magnitude": mag,
            "url": url,
            "time": formatted_dt,
            "earthquake_id": earthquake_id,
            "pager_alert_level": pager_alert_level,
            "tsunami_potential": tsunami_potential,
            "depth": depth,
            "latitude": latitude,
            "longitude": longitude,
            "significance": significance,
        }


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
            async with session.get(LIVE_URL) as resp:
                data = await resp.json()
                logging.debug(f"Request: %s", LIVE_URL)
                logging.debug(f"Response StatusCode: %s", resp.status)
                if resp.status != 200:
                    logging.error("Error getting latest data. Response text: %s", resp.text())
                    return

                feature_list = data.get("features", [])
                if len(feature_list) == 0:
                    logging.info("No earthquakes detected in the last hour.")
                    return

                # Save the earthquakes once after the HTTP pull
                await self.save_earthquakes(feature_list)

                for guild in self.client.guilds:
                    logging.info("Attempting to notify guild: %s", guild.name)
                    await self.notify_guild(feature_list, guild)
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
        df =load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])
        df["date"] = df["time"].dt.date
        today = datetime.today().date()
        df_today = df[df["date"] == today]  # Filter for today's data only

        plot_daily_earthquakes(df_today)
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
        df = load_eq_db_to_df()
        df["time"] = pd.to_datetime(df["time"])
        df["date"] = df["time"].dt.date
        today = datetime.today().date()
        df_today = df[df["date"] == today].copy()

        plot_daily_earthquakes(df_today)

        embed = discord.Embed(
            title=f"Earthquakes Today ({today.strftime('%B %d, %Y')})",
            color=discord.Color.gold(),
        )

        img_file = discord.File("eq_plot_all_today.png", filename="earthquake.png")
        embed.set_image(url="attachment://earthquake.png")

        await ctx.send(embed=embed, file=img_file)


    @commands.hybrid_command(name="config")
    async def config(self, ctx: commands.Context):
        """Configure the bots settings for a specific guild."""

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
            reaction, usr = await self.client.wait_for(
                "reaction_add",
                timeout=60.0
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


async def setup(bot):
    """Called when the cog is loaded."""
    await bot.add_cog(LiveTracking(bot))
