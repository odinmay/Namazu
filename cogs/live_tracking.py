import asyncio
from datetime import datetime, timezone
from unittest import case
from zoneinfo import ZoneInfo
import logging
import time
import os

from colorlog.escape_codes import escape_codes as c
from discord.ext import tasks, commands
import plotly.graph_objects as go
from discord.utils import get
import aiosqlite
import discord
import aiohttp

DB_PATH = "namazu.db"

logger = logging.getLogger("discord")

# LIVE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_hour.geojson"
LIVE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"

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


def plot_with_guild_preference(guid_id: int):
    """load guilds plot style preference and use that one to plot the map image"""

    pass


def plot_to_img_with_plotly(long, lat, place, mag):
    # Coordinates for New York City
    lon, lat = long, lat
    label = mag

    # Create the figure
    fig = go.Figure()

    # Add the point
    fig.add_trace(go.Scattergeo(
        lon = [lon],
        lat = [lat],
        text = label,
        mode = 'markers',
        marker=dict(size=6, color='red'),
        textposition="bottom left",
        textfont=dict(weight=700, size=16, color='black'),
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

    # Set layout
    fig.update_layout(
        title="Magnitude: " + str(mag) + " " +  place,
        font=dict(
            color='white'
        ),
        margin={"r":0,"t":30,"l":0,"b":0},
        geo=dict(
            projection_rotation=dict(lon=lon, lat=lat) # Center globe on the point
        ),
        paper_bgcolor="#232328",
        plot_bgcolor="#232328",
    )
    # Save to PNG
    fig.write_image("eq_plot.png", width=400, height=250)


def colorize(text, color):
    """Colorize text in the terminal. colorlog helper func"""
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
        logging.info("%s table created.", colorize("earthquake", "yellow"))

        await db.execute("""
        CREATE TABLE IF NOT EXISTS guild_prefs (
            GuildId INTEGER PRIMARY KEY,
            UpdateFrequency INTEGER,
            MinMagnitude INTEGER,
            UpdateChannelId INTEGER,
            PlotStyle INTEGER)
        """)
        logging.info("%s table created.", colorize("guild_prefs", "yellow"))
        logging.info("%s sqlite3 database setup complete!", colorize("namazu.db", "yellow"))


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
        description=f"A magnitude {earthquake_data["magnitude"]} earthquake has just occurred {earthquake_data["place"]}.",
        color=embed_color
    )

    if earthquake_data.get("magnitude"):
        embed.add_field(name="Magnitude", value=earthquake_data.get("magnitude"), inline=False)

    if earthquake_data["tsunami_potential"]:
        embed.add_field(name="There is potential for a Tsunami", value="🌊", inline=False)

    if valid_pager_alert:
        embed.add_field(name="PAGER Alert", value=pager_alert, inline=False)

    img_file = discord.File("eq_plot.png", filename="earthquake.png")
    embed.set_image(url="attachment://earthquake.png")

    embed.add_field(name="Time", value=earthquake_data["time"], inline=False)
    return embed, img_file

class LiveTracking(commands.Cog):
    def __init__(self, client):
        self.client = client
        self.client.loop.create_task(self._initialize())
        self.guild_prefs = {}
        # Channel that live updates get posted to
        # self.update_channel = get(self.client.get_all_channels(), name="bot-commands")

    def cog_unload(self):
        self.poll_quakes.cancel()


    async def _initialize(self):
        """This func is run when on LiveTracking __init__ is called.
        Ensures DB is set up before starting the polling loop."""
        await setup_database()
        # Build a local dictionary of all guild preferences
        for guild in self.client.guilds:
            self.guild_prefs[guild.id] = await self.get_or_set_guilds_default_config(guild.id)

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
        utc_dt = datetime.fromtimestamp(time_seconds,tz=timezone.utc)
        dt = utc_dt.astimezone(ZoneInfo("America/New_York"))
        formatted_dt = dt.strftime("%m/%d/%Y - %I:%M %p")
        tsunami_potential = feature_obj.get("properties")["tsunami"]
        depth = feature_obj.get("properties").get("depth")
        longitude = feature_obj.get("geometry").get("coordinates")[0]
        latitude = feature_obj.get("geometry").get("coordinates")[1]


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
                if len(feature_list) == 0:
                    return

                # loop over the geojson features (earthquakes)
                for feature in feature_list:
                    eq_data = await self.get_earthquake_data(feature)

                    # If it is not in the database, add it to db and then create embed
                    if await self.earthquake_not_in_db(eq_data["earthquake_id"]):
                        eq_embed, img_file = create_embed_quake_alert(eq_data)

                        # Message handling for each Guild the bot is in
                        for guild in self.client.guilds:
                            if os.getenv("DEVMODE"):
                                channel = get(guild.text_channels, name="dev-quake-updates")
                            else:
                                channel = get(guild.text_channels, name="quake-updates")

                            if channel:
                                match self.guild_prefs[guild.id]["MinMagnitude"]:
                                    case 0:
                                        await channel.send(embed=eq_embed, file=img_file)
                                    case 1:
                                        if eq_data["magnitude"] >= 1.0:
                                            await channel.send(embed=eq_embed, file=img_file)
                                        else:
                                            logging.info(f"Magnitude {eq_data['magnitude']} is too low (<1.0), skipping message for guild: {guild}")
                                    case 2:
                                        if eq_data["magnitude"] >= 2.5:
                                            await channel.send(embed=eq_embed, file=img_file)
                                        else:
                                            logging.info(
                                                f"Magnitude {eq_data['magnitude']} is too low (<2.5), skipping message for guild: {guild}")
                                    case 3:
                                        if eq_data["magnitude"] >= 4.5:
                                            await channel.send(embed=eq_embed, file=img_file)
                                        else:
                                            logging.info(
                                                f"Magnitude {eq_data['magnitude']} is too low (<4.5), skipping message for guild: {guild}")
                                    case 4:
                                        logging.info("Significant only quakes selected, but not configured(how to parse these from the hourly all eq feed?)")
                                        pass

                end_time = time.perf_counter()

                logging.info("%s function completed. Elapsed %.2f seconds.", colorize("poll_quakes", "blue"), end_time - start_time)


    @staticmethod
    async def add_earthquake(earthquake_id):
        """Add the earthquake to the sqlite database"""

        async with aiosqlite.connect(DB_PATH) as db:
            await db.execute("INSERT INTO earthquakes (EarthquakeId) VALUES (?)", (earthquake_id,))
            logging.info("EarthquakeID | %-38s | Added to database", colorize(earthquake_id, "purple"))
            await db.commit()

    async def earthquake_not_in_db(self, earthquake_id):
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

    @commands.command()
    async def config(self, ctx: commands.Context):
        """Configure the bot"""
        user = ctx.author

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

            logging.info(f"Reaction : {reaction}, user: {usr.name}")
            emoji = str(reaction.emoji)

        except asyncio.TimeoutError:
            await ctx.send("Timed out. You must re-run the .config command, and react within 1 minute.")
            return

        match emoji:
            case "0️⃣":
                logging.info("Case: 0 Guild prefs = ", self.guild_prefs)
                await self.set_guild_preference(guild_id=ctx.guild.id, preference="MinMagnitude", choice=0)
                await ctx.send("All earthquakes are being reported on by the minute.")
            case "1️⃣":
                logging.info("Case: 1 Guild prefs = ", self.guild_prefs)
                await self.set_guild_preference(guild_id=ctx.guild.id, preference="MinMagnitude", choice=1)
                await ctx.send("1.0+ earthquakes are being reported on by the minute.")
            case "2️⃣":
                logging.info("Case: 2 Guild prefs = ", self.guild_prefs)
                await self.set_guild_preference(guild_id=ctx.guild.id, preference="MinMagnitude", choice=2)
                await ctx.send("2.5+ earthquakes are being reported on by the minute.")
            case "3️⃣":
                logging.info("Case: 3 Guild prefs = ", self.guild_prefs)
                await self.set_guild_preference(guild_id=ctx.guild.id, preference="MinMagnitude", choice=3)
                await ctx.send("4.5+ earthquakes are being reported on by the minute.")
            case "4️⃣":
                logging.info("Case: 4 Guild prefs = ", self.guild_prefs)
                await self.set_guild_preference(guild_id=ctx.guild.id, preference="MinMagnitude", choice=4)
                await ctx.send("Only significant earthquakes are being reported on by the minute.")
            case _:
                await ctx.send("That is not a valid emoji.")

        await msg.delete()

    async def set_guild_preference(self, guild_id: int, preference: str, choice: int):
        """Set the preference for a guild in the database"""
        async with aiosqlite.connect(DB_PATH) as db:
            logging.info(f"Setting {preference} for guild {guild_id} to {choice}")
            async with db.execute(f"UPDATE guild_prefs set {preference} = {choice} WHERE GuildId = {guild_id}") as cursor:
                result = await cursor.fetchone()
                await db.commit()
                logging.info(f"Database update successful!.")

                self.guild_prefs[str(guild_id)] = await self.get_or_set_guilds_default_config(guild_id)


    async def get_or_set_guilds_default_config(self, guild_id: int):
        """Get guild config from database, or create defaults and return them if not present"""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                                  SELECT GuildId, UpdateFrequency, MinMagnitude, UpdateChannelId, PlotStyle
                                  FROM guild_prefs
                                  WHERE GuildId = ?
                                  """, (guild_id,)) as cursor:
                result = await cursor.fetchone()

            if not result:
                logging.info(f"Guild {guild_id} not in preferences database, adding defaults")
                await db.execute("""
                                 INSERT INTO guild_prefs (GuildId, UpdateFrequency, MinMagnitude, UpdateChannelId, PlotStyle)
                                 VALUES (?, ?, ?, ?, ?)
                                 """, (guild_id, 60, 2, 0, 0))
                await db.commit()

                # Fetch the newly inserted row to return it
                async with db.execute("""
                                      SELECT GuildId, UpdateFrequency, MinMagnitude, UpdateChannelId, PlotStyle
                                      FROM guild_prefs
                                      WHERE GuildId = ?
                                      """, (guild_id,)) as cursor:
                    result = await cursor.fetchone()

                    # Save results to a dict for easy accessing
                    guild_pref_dict = {
                        "UpdateFrequency": result[1],
                        "MinMagnitude": result[2],
                        "UpdateChannelId": result[3],
                        "PlotStyle": result[4]
                    }
            else:
                # Save results to a dict for easy accessing
                guild_pref_dict = {
                    "UpdateFrequency": result[1],
                    "MinMagnitude": result[2],
                    "UpdateChannelId": result[3],
                    "PlotStyle": result[4]
                }
            return guild_pref_dict


    async def get_guild_preference_choice(self, guild_id: int, preference: str) -> int:
        """Get the preference for a guild in the database"""
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(f"""
            SELECT {preference} FROM guild_prefs WHERE GuildId = {guild_id}
            """) as cursor:
                result = await cursor.fetchone()
                result = result[0]
                return result

    async def clear_database(self):
        """Truncate tables in the database to reset it"""
        pass

async def setup(bot):
    await bot.add_cog(LiveTracking(bot))
