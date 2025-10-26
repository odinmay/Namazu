"""This is the main entry point, which is executed first and handles connecting to the
Namazu Bot discord application, loads cogs, and establishes log formatting."""
import logging
import sys
import os
# External imports
import discord
from discord.ext import commands
import colorlog

VERSION_NUMBER = "v0.13"

DISCORD_SECRET = os.getenv("DISCORD_SECRET")

# Console logging initialization
logger = logging.getLogger("discord")
console_handler = logging.StreamHandler(sys.stdout)
console_formatter = colorlog.ColoredFormatter(
    "%(log_color)s[%(asctime)s.%(msecs)03d] "
    "[%(levelname)s]%(reset)s %(message_log_color)s%(message)s",
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
    """Runs when the bot is ready, loads cogs, set presence message, and sync command tree."""
    await client.load_extension('cogs.live_tracking')
    await client.change_presence(activity=discord.Game(name='github.com/odinmay'))
    await client.tree.sync()


if __name__ == '__main__':
    logger.info("Namazu %s", VERSION_NUMBER)
    client.run(DISCORD_SECRET,
               log_level=logging.INFO,
               root_logger=True,
               log_handler=console_handler,
               log_formatter=console_formatter)
