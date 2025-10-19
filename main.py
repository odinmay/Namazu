"""This is the main file, which is executed first and handles connecting to the Namazu Bot application and loads cogs"""
from datetime import datetime
from pprint import pprint
import logging
import sys
import os
# External imports
import discord
import aiohttp
from discord.ext import commands
import colorlog
from colorlog.escape_codes import escape_codes as c
from discord import app_commands
from discord.utils import get

version_number = "v0.05"

DISCORD_SECRET = os.getenv("DISCORD_SECRET")

# Console logging initialization
logger = logging.getLogger("discord")
console_handler = logging.StreamHandler(sys.stdout)
console_formatter = colorlog.ColoredFormatter(
    "%(log_color)s[%(asctime)s.%(msecs)03d] [%(levelname)s]%(reset)s %(message_log_color)s%(message)s",
    datefmt="%H:%M:%S",
    log_colors={
        "DEBUG": "cyan",
        "INFO": "green",
        "WARNING": "yellow",
        "ERROR": "red",
        "CRITICAL": "bold_red",
    },
    secondary_log_colors={
        "message": {
            "ERROR": "red",
            "CRITICAL": "bold_red",
        }
    },
)


# Instantiate the bot object and set options variables
intents = discord.Intents(messages=True, message_content=True, guilds=True)
client = commands.Bot(command_prefix=".", intents=intents)

# Test url for now
EARTHQUAKE_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/1.0_hour.geojson"

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
async def test(ctx):
    logger.info(f"{ctx.message.author}: {ctx.message.content} |Timestamp: {ctx.message.created_at}" )
    await ctx.send("Test message")


if __name__ == '__main__':
    logger.info("Namazu " + version_number)
    client.run(DISCORD_SECRET, log_level=logging.INFO, root_logger=True, log_handler=console_handler, log_formatter=console_formatter)
