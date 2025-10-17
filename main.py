"""This is the main file, which is executed first and handles connecting to the Namazu Bot application and loads cogs"""
from datetime import datetime
from pprint import pprint
import logging
import os

import discord
import aiohttp
from discord.ext import commands
from discord import app_commands
from discord.utils import get

version_number = "v0.03"
DISCORD_SECRET = os.getenv("DISCORD_SECRET")

# Setup logging and file handler
handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
logger = logging.getLogger('discord')
logger.setLevel(logging.DEBUG)
logging.getLogger('discord.http').setLevel(logging.INFO)
logger.addHandler(handler)


# Instantiate the bot object and set options variables
intents = discord.Intents(messages=True, message_content=True, guilds=True)
client = commands.Bot(command_prefix=".", intents=intents)

# Test url for now
EARTHQUAKE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/1.0_hour.geojson"


def split_message(message):
    if len(message) > 2000:
        message = message[:2000]
    return message


def get_formatted_features(feature_obj):
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
    formatted_dt = dt.strftime("%m/%d/%Y %I:%M:%S")

    # Assemble message str
    formatted_message = (f"Magnitude: {mag}\n"
                         f"PAGER Alert Level: {pager_lvl_icon}\n"
                         f"Location: {place}\n"
                         f"Time: {formatted_dt}\n"
                         f"More Info: {url} \n")

    return formatted_message


async def get_data():
    async with aiohttp.ClientSession() as session:
        async with session.get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson") as resp:

            if resp.status == 200:
                data = await resp.json()
                return data
            else:
                logging.error("Error getting data from earthquake\n Response: %s Status: %s", resp.text(), resp.status)
                return None


@client.event
async def on_ready():
    await client.tree.sync()
    await client.load_extension('cogs.live_tracking')
    await client.change_presence(activity=discord.Game(name='github.com/odinmay'))


@client.command()
async def msg(ctx):
    logger.info(f"{ctx.message.author}: {ctx.message.content} |Timestamp: {ctx.message.created_at}" )
    await ctx.send("Test message")


@client.command()
async def eq(ctx):
    """Test command for earthquake data"""
    logger.info(f"{ctx.message.author}: {ctx.message.content} |Timestamp: {ctx.message.created_at}" )
    data = await get_data()
    pprint(data)
    if data.get("features") and len(data.get("features")) > 0:
        logging.info("Total features found: %s", len(data.get("features")))
        features = data.get("features")

        all_features = []

        for feature in features:
            formatted_message = get_formatted_features(feature)
            all_features.append(formatted_message)

        all_features_str = "\n".join(all_features)
        trimmed_msg = split_message(all_features_str)
        await ctx.send(trimmed_msg)


if __name__ == '__main__':
    print("Version #: " + version_number)
    client.run(DISCORD_SECRET, log_level=logging.DEBUG)
