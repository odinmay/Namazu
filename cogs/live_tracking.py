import time

import aiosqlite
from datetime import datetime
import logging
import os

from discord.ext import tasks, commands
from discord.utils import get
from colorlog.escape_codes import escape_codes as c

import discord
import aiohttp

DB_PATH = "namazu.db"

logger = logging.getLogger("discord")

# LIVE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_hour.geojson"
LIVE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/1.0_hour.geojson"

""" 
DEVMODE Environment var is passed to the run config for "test" in pycharm
It resets db and only post updates in dev-quake-updates channel.
"""
DEVMODE = os.getenv("DEVMODE")
if DEVMODE:
    if os.path.exists("namazu.db"):
        logging.info("DEVMODE active. Removing database...")
        os.remove("namazu.db")
        logging.info("Database removed.")


def colorize(text, color):
    return f"{c[color]}{text}{c['reset']}"


async def setup_database():
    """Initialize the database, create the required tables"""
    logging.info("Running database setup...")

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS earthquakes (
            Id INTEGER PRIMARY KEY AUTOINCREMENT,
            EarthquakeId TEXT NOT NULL,
            Longitude REAL,
            Latitude REAL,
            Place TEXT)
            """)
        logging.info("Database setup complete!")

def create_embed_quake_alert(earthquake_data: dict):
    # Check on the color and make embed the color, else make it gray

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
        description=f"A magnitude {earthquake_data["magnitude"]} earthquake has just occurred {earthquake_data["place"]}.",
        color=embed_color
    )

    if earthquake_data.get("magnitude"):
        embed.add_field(name="Magnitude", value=earthquake_data.get("magnitude"), inline=False)

    if earthquake_data["tsunami_potential"]:
        embed.add_field(name="There is potential for a Tsunami", value="🌊", inline=False)

    if valid_pager_alert:
        embed.add_field(name="PAGER Alert", value=pager_alert, inline=False)

    embed.add_field(name="Time", value=earthquake_data["time"], inline=False)
    return embed

class LiveTracking(commands.Cog):
    def __init__(self, client):
        self.client = client
        self.client.loop.create_task(self._initialize())
        # Channel that live updates get posted to
        # self.update_channel = get(self.client.get_all_channels(), name="bot-commands")

    def cog_unload(self):
        self.poll_quakes.cancel()


    async def _initialize(self):
        """This func is run when on LiveTracking __init__ is called.
        Ensures DB is set up before starting the polling loop."""
        await setup_database()
        self.poll_quakes.start()


    async def get_earthquake_data(self, feature_obj: dict):
        """Unpack the dictionary and format all values, return formatted dict"""
        pager_alert_level = feature_obj.get("properties")["alert"]

        # Give each alert level an emoji | Info - https://earthquake.usgs.gov/data/pager/onepager.php
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
        dt = datetime.fromtimestamp(time_seconds)
        formatted_dt = dt.strftime("%m/%d/%Y - %I:%M %p")
        tsunami_potential = feature_obj.get("properties")["tsunami"]
        depth = feature_obj.get("properties").get("depth")
        longitude = feature_obj.get("properties").get("longitude")
        latitude = feature_obj.get("properties").get("latitude")

        if not depth:
            depth = "unknown"
        else:
            depth = str(depth)

        earthquake_id = str(mag) + "-" + str(feature_obj.get("properties").get("code")) + "-" + str(feature_obj.get("properties").get("time"))

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
        }

    @tasks.loop(seconds=60.0)
    async def poll_quakes(self):
        """Every 10 minutes, poll the api for new earthquakes. When a new quake is detected,
        add the quake_id to a sqlite database. If the quake is not in the database, it will be added.
        If the quake is in the database, it will be ignored."""
        start_time = time.perf_counter()
        logging.info("%s function initiated.", colorize("poll_quakes", "blue"))

        await self.client.wait_until_ready()
        async with aiohttp.ClientSession() as session:
            async with session.get(LIVE_URL) as resp:
                data = await resp.json()
                logging.debug(f"Request: {LIVE_URL}")
                logging.debug(f"Response StatusCode: {resp.status}")
                if resp.status != 200:
                    logging.error("Error getting latest data. Response text: %s", resp.text())
                    return

                feature_list = data.get("features", [])
                for feature in feature_list:
                    eq_data = await self.get_earthquake_data(feature)

                    if await self.is_earthquake_already_in_db(eq_data["earthquake_id"]):
                        eq_embed = create_embed_quake_alert(eq_data)

                        for guild in self.client.guilds:
                            if os.getenv("DEVMODE"):
                                channel = get(guild.text_channels, name="dev-quake-updates")
                            else:
                                channel = get(guild.text_channels, name="quake-updates")
                            if channel:
                                await channel.send(embed=eq_embed)
                end_time = time.perf_counter()

                logging.info("%s function completed. Elapsed %.2f seconds.", colorize("poll_quakes", "blue"), end_time - start_time)


    @staticmethod
    async def add_earthquake(earthquake_id):
        """Add the earthquake to the sqlite database"""

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO earthquakes (EarthquakeId) VALUES (?)", (earthquake_id,))
            logging.info("EarthquakeID | %-38s | Added to database", colorize(earthquake_id, "purple"))
            await db.commit()

    async def is_earthquake_already_in_db(self, earthquake_id):
        """Check if the earthquake id is already in the sqlite database"""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM earthquakes WHERE EarthquakeID = ?", (earthquake_id,)) as cursor:
                result = await cursor.fetchone()

                if result:
                    logging.info(f"EarthquakeID | %-38s | Found in database", colorize(earthquake_id, "purple"))
                    return False
                else:
                    logging.info("EarthquakeID | %-38s | Not found in database..", colorize(earthquake_id, "purple"))
                    await self.add_earthquake(earthquake_id)
                    return True


    async def clear_database(self):
        """Truncate tables in the database to reset it"""
        pass

async def setup(bot):
    await bot.add_cog(LiveTracking(bot))
