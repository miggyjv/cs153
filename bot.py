import os
import discord
import logging
import time
import sys

from discord.ext import commands
from dotenv import load_dotenv

# Prevent double execution
if getattr(sys, 'bot_initialized', False):
    print("Bot already running, exiting.")
    sys.exit(0)
sys.bot_initialized = True

# Completely reset Python's logging to prevent duplicates
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

# Setup logging - prevent duplicate logs
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(name)s %(message)s',
    handlers=[logging.StreamHandler()]
)

# Disable loggers we don't need to see
logging.getLogger('urllib3').setLevel(logging.WARNING)
logging.getLogger('selenium').setLevel(logging.WARNING)
logging.getLogger('WDM').setLevel(logging.WARNING)

# Get our logger
logger = logging.getLogger('discord')

PREFIX = "!"

# Load the environment variables
load_dotenv()

# Create the bot with all intents
# The message content and members intent must be enabled in the Discord Developer Portal for the bot to work.
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=PREFIX, intents=intents)

# Import the Fact Check agent
from agent import FactCheckAgent
agent = FactCheckAgent()

# Now that bot is set up, import any other modules that might use it
# (This helps prevent circular imports)

# Get the token from the environment variables
token = os.getenv("DISCORD_TOKEN")

@bot.event
async def on_ready():
    """
    Called when the client is done preparing the data received from Discord.
    Prints message on terminal when bot successfully connects to discord.

    https://discordpy.readthedocs.io/en/latest/api.html#discord.on_ready
    """
    logger.info(f"{bot.user} has connected to Discord!")

@bot.event
async def on_message(message: discord.Message):
    """
    Called when a message is sent in any channel the bot can see.
    """
    # Only process commands, nothing else needed here
    if not message.author.bot:  # Only process messages from non-bots
        await bot.process_commands(message)

# Commands

# This example command is here to show you how to add commands to the bot.
# Run !ping with any number of arguments to see the command in action.
# Feel free to delete this if your project will not need commands.
@bot.command(name="ping", help="Pings the bot.")
async def ping(ctx, *, arg=None):
    if arg is None:
        await ctx.send("Pong!")
    else:
        await ctx.send(f"Pong! Your argument was {arg}")

# Add a slash command for fact-checking
@bot.command(name="factcheck", help="Fact check a claim by replying to it with this command")
async def factcheck_command(ctx):
    """Command to fact check a message that was replied to."""
    # Check if this is a reply
    if ctx.message.reference is None:
        await ctx.send("Please reply to a message containing a claim to fact check.")
        return
    
    try:
        # Get the message being replied to
        referenced_msg = await ctx.channel.fetch_message(ctx.message.reference.message_id)
        claim = referenced_msg.content
        
        # Let the user know we're working on it
        response_msg = await ctx.send("🔍 Analyzing claim... This might take a moment.")
        
        # Process the fact check
        start = time.time()
        logger.info(f"Fact checking claim from {referenced_msg.author}: \"{claim}\"")
        
        embed = await agent.fact_check(claim)
        
        # Edit our response with the result embed
        await response_msg.edit(content=None, embed=embed)
        
        end = time.time()
        time_delay = end - start
        logger.info(f"Completed fact check in {time_delay:.2f} seconds")
        
    except Exception as e:
        logger.error(f"Error in fact check: {e}")
        if 'response_msg' in locals():
            await response_msg.edit(content=f"Error during fact check: {str(e)}")
        else:
            await ctx.send(f"Error during fact check: {str(e)}")

# Start the bot, connecting it to the gateway
bot.run(token)
