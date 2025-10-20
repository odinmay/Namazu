"""This is the main file, which is executed first and handles connecting to the Namazu Bot application and loads cogs"""
import logging
import sys
import os
# External imports
import discord
from discord.ext import commands
import colorlog

version_number = "v0.06"

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
intents = discord.Intents(messages=True, message_content=True, guilds=True, reactions=True)
client = commands.Bot(command_prefix=".", intents=intents)

@client.event
async def on_ready():
    await client.tree.sync()
    await client.load_extension('cogs.live_tracking')
    await client.change_presence(activity=discord.Game(name='github.com/odinmay'))


if __name__ == '__main__':
    logger.info("Namazu " + version_number)
    client.run(DISCORD_SECRET, log_level=logging.INFO, root_logger=True, log_handler=console_handler, log_formatter=console_formatter)
