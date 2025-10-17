from pprint import pprint
import aiosqlite
from datetime import datetime
import logging

from discord.ext import tasks, commands
from discord.utils import get
import discord
import aiohttp

DB_PATH = "namazu.db"

handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
logger = logging.getLogger('discord')
logger.setLevel(logging.DEBUG)
logging.getLogger('discord.http').setLevel(logging.INFO)
logger.addHandler(handler)

# LIVE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_hour.geojson"
LIVE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/1.0_hour.geojson"


async def setup_database():
    """Initialize the database, create the required tables"""
    logging.info("Running Database Setup")
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS earthquakes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            earthquake_id TEXT NOT NULL,
            longitude REAL,
            latitude REAL,
            place TEXT)
            """)

        logging.info("Database setup complete")

def create_embed_quake_alert(earthquake_data: dict):
    # Check on the color and make embed the color, else make it gray
    match earthquake_data["pager_alert_level"]:
        case "green":
            embed_color = discord.Color.green()
        case "yellow":
            embed_color = discord.Color.yellow()
        case "orange":
            embed_color = discord.Color.orange()
        case "red":
            embed_color = discord.Color.red()
        case _:
            embed_color = discord.Color.dark_gray()

    embed = discord.Embed(
        title="🚨 Earthquake Alert 🚨",
        description=earthquake_data["description"],
        color=embed_color
    )
    if earthquake_data["tsunami_potential"]:
        embed.add_field(name="Potential for a Tsunami", value="🌊", inline=False)

    embed.add_field(name="Time", value=earthquake_data["time"], inline=False)
    return embed

class LiveTracking(commands.Cog):
    def __init__(self, client):
        self.client = client
        self.poll_quakes.start()
        self.client.loop.create_task(setup_database())
        # Channel that live updates are posted to
        self.update_channel = get(self.client.get_all_channels(), name="bot-commands")

    def cog_unload(self):
        self.poll_quakes.cancel()


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
        formatted_dt = dt.strftime("%m/%d/%Y %I:%M:%S %p")
        tsunami_potential = feature_obj.get("properties")["tsunami"]
        depth = feature_obj.get("properties")["depth"]

        description = f"An earthquake has just occurred {place} with a magnitude of {mag} at a depth of {depth}km."

        earthquake_id = str(feature_obj.get("properties").get("code")) + "-" + str(feature_obj.get("properties").get("time"))

        return {
            "pager_lvl_icon": pager_lvl_icon,
            "place": place,
            "magnitude": mag,
            "url": url,
            "time": formatted_dt,
            "earthquake_id": earthquake_id,
            "pager_alert_level": pager_alert_level,
            "description": description,
            "tsunami_potential": tsunami_potential,
            "depth": depth,
        }

    @tasks.loop(seconds=60.0)
    async def poll_quakes(self):
        """Every 10 minutes, poll the api for new earthquakes. When a new quake is detected,
        add the quake_id to a sqlite database. If the quake is not in the database, it will be added.
        If the quake is in the database, it will be ignored."""
        await self.client.wait_until_ready()
        async with aiohttp.ClientSession() as session:
            async with session.get(LIVE_URL) as resp:
                data = await resp.json()
                if resp.status != 200:
                    logging.error("Error getting latest data. Response text: %s", resp.text())
                    print("Error getting latest data. Response text: %s", resp.text())
                    return

                feature_list = data.get("features", [])
                for feature in feature_list:
                    eq_data = await self.get_earthquake_data(feature)

                    if await self.is_earthquake_already_in_db(eq_data["earthquake_id"]):
                        eq_embed = create_embed_quake_alert(eq_data)
                        # channel = self.client.get_channel(1428197710485524511)
                        for guild in self.client.guilds:
                            channel = get(guild.text_channels, name="quake-updates")
                            if channel:
                                await channel.send(embed=eq_embed)

                print("Poll quakes complete!")


    @staticmethod
    async def add_earthquake(earthquake_id):
        """Add the earthquake to the sqlite database"""

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO earthquakes (earthquake_id) VALUES (?)", (earthquake_id,))
            print("Added earthquake id: %s", earthquake_id)
            logging.info("EarthquakeID: %s added to the sqlite database", earthquake_id)
            await db.commit()

    async def is_earthquake_already_in_db(self, earthquake_id):
        """Check if the earthquake id is already in the sqlite database"""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM earthquakes WHERE earthquake_id = ?", (earthquake_id,)) as cursor:
                result = await cursor.fetchone()
                print("DB Check result: ", result)

                if result:
                    logging.info("The earthquake id is already in the sqlite database")
                    print("Already in sqlite database")
                    return False
                else:
                    logging.info("Earthquake Id: %s is not in database..", earthquake_id)
                    print(f"Earthquake Id: {earthquake_id} is not in database..")
                    await self.add_earthquake(earthquake_id)
                    return True

    async def clear_database(self):
        """Truncate tables in the database to reset it"""
        pass


async def setup(bot):
    await bot.add_cog(LiveTracking(bot))
