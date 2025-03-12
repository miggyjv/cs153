import os
import discord
import logging
import time
import sys
from datetime import datetime, timedelta
import random

from discord.ext import commands
from dotenv import load_dotenv

# Create a completely separate logger for our application
APP_LOGGER = logging.getLogger('sherlock_app')
APP_LOGGER.setLevel(logging.INFO)

# Remove any existing handlers to be safe
for handler in APP_LOGGER.handlers:
    APP_LOGGER.removeHandler(handler)

# Add a single handler
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
APP_LOGGER.addHandler(handler)

# Configure Discord's own logging
discord_logger = logging.getLogger('discord')
discord_logger.setLevel(logging.WARNING)  # Only show warnings and errors from discord.py

# Configure other loggers
for logger_name in ['urllib3', 'selenium', 'WDM']:
    logging.getLogger(logger_name).setLevel(logging.WARNING)

# Load environment variables
load_dotenv()

# Create the bot with all intents
# The message content and members intent must be enabled in the Discord Developer Portal for the bot to work.
intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

# Import the Fact Check agent
from agent import FactCheckAgent
agent = FactCheckAgent(app_logger=APP_LOGGER)

# Now that bot is set up, import any other modules that might use it
# (This helps prevent circular imports)

# Get the token from the environment variables
token = os.getenv("DISCORD_TOKEN")

# Global fact check lock and status tracking
is_fact_checking = False
current_fact_check_info = None  # Will store (channel_id, user_id, start_time)

# Command tracking to prevent duplicate processing
processed_commands = set()  # Stores command message IDs that have been processed
MAX_PROCESSED_COMMANDS = 1000  # Limit to prevent memory issues

# Cache for recent fact check results
factcheck_cache = {}  # Dictionary to store recent results: {message_id: (timestamp, embed)}
CACHE_EXPIRY = 600  # Cache expiry time in seconds (10 minutes)

@bot.event
async def on_ready():
    """
    Called when the client is done preparing the data received from Discord.
    Prints message on terminal when bot successfully connects to discord.

    https://discordpy.readthedocs.io/en/latest/api.html#discord.on_ready
    """
    APP_LOGGER.info(f"Bot {bot.user} has connected to Discord!")

@bot.event
async def on_message(message: discord.Message):
    """
    Called when a message is sent in any channel the bot can see.
    """
    # Only process commands for non-bot messages
    if not message.author.bot:
        # Check if this is a command message
        if message.content.startswith(bot.command_prefix):
            # Only process if we haven't seen this command message before
            if message.id not in processed_commands:
                # Add this message ID to our processed set before handling
                processed_commands.add(message.id)
                
                # Limit the size of processed_commands set
                if len(processed_commands) > MAX_PROCESSED_COMMANDS:
                    # Remove the oldest entries (sadly, sets don't track insertion order)
                    # So we just clear half the set when it gets too large
                    processed_commands.clear()
                
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

# Add a command to get current fact-check status
@bot.command(name="status", help="Check if the bot is currently performing a fact check")
async def status_command(ctx):
    global is_fact_checking, current_fact_check_info
    
    if not is_fact_checking:
        await ctx.send("No fact checks are currently running. The bot is ready for new requests.")
        return
    
    # If a fact check is running, show details
    channel_id, user_id, start_time = current_fact_check_info
    elapsed_time = time.time() - start_time
    
    try:
        channel = bot.get_channel(channel_id)
        user = await bot.fetch_user(user_id)
        
        await ctx.send(f"⏳ A fact check requested by {user.name} in {channel.name} has been running for {elapsed_time:.1f} seconds.")
    except:
        await ctx.send(f"⏳ A fact check has been running for {elapsed_time:.1f} seconds.")

# Add a slash command for fact-checking
@bot.command(name="factcheck", help="Fact check a claim by replying to it with this command")
async def factcheck_command(ctx):
    """Command to fact check a message that was replied to."""
    global is_fact_checking, current_fact_check_info
    
    # Check if this is a reply
    if ctx.message.reference is None:
        await ctx.send("Please reply to a message containing a claim to fact check.")
        return
    
    # Get the referenced message ID
    ref_msg_id = ctx.message.reference.message_id
    
    # Create a unique identifier for this specific factcheck request
    # This combines the command message ID and the referenced message ID
    request_id = f"{ctx.message.id}-{ref_msg_id}"
    
    # Check for an active factcheck with the same request ID
    active_requests = await ctx.channel.history(limit=10).flatten()
    for msg in active_requests:
        # Skip messages not from the bot
        if msg.author != bot.user:
            continue
            
        # Check if this is a "processing" message for the same request
        if msg.content.startswith("🔍 Analyzing claim...") and hasattr(msg, 'reference'):
            # If the message has our reference ID format at the end
            if msg.content.endswith(f"[Request: {request_id}]"):
                await ctx.send("⚠️ This claim is already being fact-checked. Please wait for results.")
                return
    
    # Check if there's already a fact check in progress (globally)
    if is_fact_checking:
        channel_id, user_id, start_time = current_fact_check_info
        elapsed_time = time.time() - start_time
        
        try:
            channel = bot.get_channel(channel_id)
            user = await bot.fetch_user(user_id)
            await ctx.send(f"⏳ Another fact check requested by {user.name} in {channel.name} is currently in progress "
                          f"({elapsed_time:.1f} seconds elapsed). Please try again later.")
        except:
            await ctx.send(f"⏳ Another fact check is currently in progress. Please try again later.")
        return
    
    # Check if we have a recent result for this message
    current_time = time.time()
    if ref_msg_id in factcheck_cache:
        timestamp, embed = factcheck_cache[ref_msg_id]
        # If the cached result is recent enough, use it
        if current_time - timestamp < CACHE_EXPIRY:
            await ctx.send("📋 Using recent fact-check result:", embed=embed)
            return
        # Otherwise, remove the expired cache entry
        else:
            del factcheck_cache[ref_msg_id]
    
    # Set the global fact-checking lock
    is_fact_checking = True
    current_fact_check_info = (ctx.channel.id, ctx.author.id, current_time)
    
    try:
        # Get the message being replied to
        referenced_msg = await ctx.channel.fetch_message(ref_msg_id)
        claim = referenced_msg.content
        
        # Let the user know we're working on it - with unique request ID
        response_msg = await ctx.send(f"🔍 Analyzing claim... This might take a moment. [Request: {request_id}]")
        
        # Process the fact check
        start = time.time()
        APP_LOGGER.info(f"Fact checking claim from {referenced_msg.author}: \"{claim}\" [Request: {request_id}]")
        
        embed = await agent.fact_check(claim)
        
        # Edit our response with the result embed - keep the request ID in a hidden field
        embed.add_field(name="\u200b", value=f"Request ID: {request_id}", inline=False)
        
        # Edit our response with the result embed
        await response_msg.edit(content=None, embed=embed)
        
        # Store the result in cache
        factcheck_cache[ref_msg_id] = (current_time, embed)
        
        end = time.time()
        time_delay = end - start
        APP_LOGGER.info(f"Completed fact check in {time_delay:.2f} seconds [Request: {request_id}]")
        
    except Exception as e:
        APP_LOGGER.error(f"Error in fact check: {e}")
        if 'response_msg' in locals():
            await response_msg.edit(content=f"Error during fact check: {str(e)} [Request: {request_id}]")
        else:
            await ctx.send(f"Error during fact check: {str(e)}")
    
    finally:
        # Always release the lock when done
        is_fact_checking = False
        current_fact_check_info = None
        
        # Clean up expired cache entries occasionally
        if random.random() < 0.1:  # ~10% chance to clean up on each fact check
            clean_expired_cache()

def clean_expired_cache():
    """Remove expired entries from the factcheck cache."""
    current_time = time.time()
    expired_keys = [k for k, (timestamp, _) in factcheck_cache.items() 
                   if current_time - timestamp > CACHE_EXPIRY]
    
    for key in expired_keys:
        del factcheck_cache[key]
    
    if expired_keys:
        APP_LOGGER.info(f"Cleaned {len(expired_keys)} expired cache entries")

# Only run if this file is executed directly
if __name__ == "__main__":
    try:
        # Start the bot, connecting it to the gateway
        APP_LOGGER.info("Starting bot...")
        bot.run(token)
    except Exception as e:
        APP_LOGGER.error(f"Failed to start bot: {e}")
