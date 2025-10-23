import asyncio
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import pickle
import logging
import time
import os

from colorlog.escape_codes import escape_codes as c
from discord.ext import tasks, commands
import plotly.graph_objects as go
from discord.utils import get
import discord
import aiohttp

logger = logging.getLogger("discord")

LIVE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/all_hour.geojson"

GUILD_PREFS_PATH = "/data/guild_prefs.pkl"
EQ_DB_PATH = "/data/eq_db.pkl"


def get_guild_prefs():
    """Load guild prefs from the serial file and return the object"""
    if os.path.exists(GUILD_PREFS_PATH):
        with open(GUILD_PREFS_PATH, "rb") as f:
            guild_prefs = pickle.load(f)
            return guild_prefs
    guild_prefs = {}
    return guild_prefs


def get_eq_db():
    if os.path.exists(EQ_DB_PATH):
        with open(EQ_DB_PATH, "rb") as f:
            eq_db = pickle.load(f)
            return eq_db
    else:
        eq_db = {}
        return eq_db


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
        embed.add_field(name=f"{earthquake_data["pager_alert_level"].upper()} PAGER Alert", value=pager_alert, inline=False)

    img_file = discord.File("eq_plot.png", filename="earthquake.png")
    embed.set_image(url="attachment://earthquake.png")

    embed.add_field(name="Time", value=earthquake_data["time"], inline=False)
    return embed, img_file


class LiveTracking(commands.Cog):
    def __init__(self, client):
        self.client = client
        self.guild_prefs = get_guild_prefs()
        self.eq_db = get_eq_db()
        self.client.loop.create_task(self._initialize())


    def cog_unload(self):
        self.poll_quakes.cancel()


    async def _initialize(self):
        """This func is run when on LiveTracking __init__ is called.
        Ensures eq_db and guild_prefs are loaded before starting the polling loop."""
        # Create or set guild preferences
        for guild in self.client.guilds:
            if self.guild_prefs.get(str(guild.id)):
                continue

            # Set default prefs if not in the prefs dict
            self.guild_prefs[str(guild.id)] = {"MinMagnitude": 3,
                                               "UpdateFrequency": 0,
                                               "UpdateChannelId": 0,
                                               "PlotStyle": 0}

        # Set eq_db guild.id default key object
        for guild in self.client.guilds:
            if self.eq_db.get(str(guild.id)):
                continue
            self.eq_db[str(guild.id)] = {}

            logging.info("Guild prefs at the end of  _initialize method. Guild prefs: ", self.guild_prefs)



        self.poll_quakes.start()


    async def notify_guild(self, features: list, guild: discord.Guild):
        for feature in features:
            eq_data = await self.get_earthquake_data(feature)

            if self.eq_db[str(guild.id)].get(eq_data["earthquake_id"]):
                continue

            #TODO set notify channel through config command
            channel = get(guild.text_channels, name="quake-updates")
            if not channel:
                return

            # Filter the earthquake by guild preferred reporting magnitude
            match self.guild_prefs[str(guild.id)]["MinMagnitude"]:
                case 0:
                    # Create and send the message
                    eq_embed, img_file = create_embed_quake_alert(eq_data)
                    await channel.send(embed=eq_embed, file=img_file)
                case 1:
                    if eq_data["magnitude"] >= 1.0:
                        # Create and send the message
                        eq_embed, img_file = create_embed_quake_alert(eq_data)
                        await channel.send(embed=eq_embed, file=img_file)
                    else:
                        logging.info(
                            f"Magnitude {eq_data['magnitude']} is too low (<1.0), skipping message for guild: {guild}")
                case 2:
                    if eq_data["magnitude"] >= 2.5:
                        # Create and send the message
                        eq_embed, img_file = create_embed_quake_alert(eq_data)
                        await channel.send(embed=eq_embed, file=img_file)
                    else:
                        logging.info(
                            f"Magnitude {eq_data['magnitude']} is too low (<2.5), skipping message for guild: {guild}")
                case 3:
                    if eq_data["magnitude"] >= 4.5:
                        # Create and send the message
                        eq_embed, img_file = create_embed_quake_alert(eq_data)
                        await channel.send(embed=eq_embed, file=img_file)
                    else:
                        logging.info(
                            f"Magnitude {eq_data['magnitude']} is too low (<4.5), skipping message for guild: {guild}")
                case 4:
                    # # See if we should send message and send it if the criteria is met for the guild
                    # eq_embed, img_file = create_embed_quake_alert(eq_data)
                    logging.info(
                        "Significant only quakes selected, but not configured(how to parse these from the hourly all eq feed?)")


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

        # Initialize eq_data.guild earthquake records
        for guild in self.client.guilds:
            if self.eq_db.get(str(guild.id)):
                if self.eq_db.get(str(guild.id)).get(earthquake_id):
                    continue
                else:
                    # Register that we see the earthquake and set value to false
                    # This false value is acting as the answer to a question, has this guild.id
                    # reported on this earthquake
                    self.eq_db[str(guild.id)][earthquake_id] = False

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
        """Every 10 minutes, poll the api for new earthquakes. When a new quake is detected, add the quake_id
        to the eq_db dictionary. If the guildid.quakeid = false, the eq has not been reported on by that guild
        If the quake is in the dict, it will be ignored."""
        print("Guild prefs from start of poll quakes cmd: ", self.guild_prefs)
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
                    logging.info("No earthquakes detected in the last hour.")
                    return

                for guild in self.client.guilds:
                    logging.info("Attempting to notify guild: %s", guild.name)
                    await self.notify_guild(feature_list, guild)
                    logging.info("Completed notifying guild: %s", guild.name)

                end_time = time.perf_counter()

                # Save the eq_eb object to a binary file, Pickle it!
                with open(EQ_DB_PATH, "wb") as f:
                    pickle.dump(self.eq_db, f)
                logging.info("%s function completed. Elapsed %.2f seconds.", colorize("poll_quakes", "blue"), end_time - start_time)


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
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 0
                await ctx.send("All earthquakes are being reported on by the minute.")
            case "1️⃣":
                logging.info("Case: 1 Guild prefs = ", self.guild_prefs)
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 1
                await ctx.send("1.0+ earthquakes are being reported on by the minute.")
            case "2️⃣":
                logging.info("Case: 2 Guild prefs = ", self.guild_prefs)
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 2
                await ctx.send("2.5+ earthquakes are being reported on by the minute.")
            case "3️⃣":
                logging.info("Case: 3 Guild prefs = ", self.guild_prefs)
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 3
                await ctx.send("4.5+ earthquakes are being reported on by the minute.")
            case "4️⃣":
                logging.info("Case: 4 Guild prefs = ", self.guild_prefs)
                self.guild_prefs[str(ctx.guild.id)]["MinMagnitude"] = 4
                await ctx.send("Only significant earthquakes are being reported on by the minute.")
            case _:
                await ctx.send("That is not a valid emoji.")

        await msg.delete()


async def setup(bot):
    await bot.add_cog(LiveTracking(bot))
