import os
import discord
import logging
import time
import sys
import asyncio
import hashlib
from functools import wraps
import threading
import random

from discord.ext import commands
from dotenv import load_dotenv

# Create a single instance marker to prevent duplicate bot instances
_instance_lock_file = "bot_instance.lock"
try:
    if os.path.exists(_instance_lock_file):
        with open(_instance_lock_file, 'r') as f:
            pid = f.read().strip()
            if pid and os.path.exists(f"/proc/{pid}"):
                print(f"Bot already running with PID {pid}. Exiting.")
                sys.exit(0)
    
    with open(_instance_lock_file, 'w') as f:
        f.write(str(os.getpid()))
except:
    pass  # Fail silently on Windows or other systems

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

# Also log to a file for debugging
file_handler = logging.FileHandler('bot_debug.log')
file_handler.setFormatter(logging.Formatter('%(asctime)s [%(levelname)s] %(message)s'))
APP_LOGGER.addHandler(file_handler)

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

# Set to track processed command messages to avoid duplicates
processed_commands = set()
MAX_PROCESSED_COMMANDS = 1000  # Maximum number of command IDs to remember

# Tracking for commands that are currently being processed
# Format: {command_hash: (start_time, message_id, channel_id)}
active_commands = {}
command_lock = asyncio.Lock()

# Cache for recent fact check results
factcheck_cache = {}  # Dictionary to store recent results: {message_id: (timestamp, embed)}
CACHE_EXPIRY = 600  # Cache expiry time in seconds (10 minutes)

# Generate a unique hash for a command to prevent duplicates
def get_command_hash(ctx):
    """Create a unique hash of command + message ID + channel + author"""
    command_str = f"{ctx.command.name}:{ctx.message.id}:{ctx.channel.id}:{ctx.author.id}"
    return hashlib.md5(command_str.encode()).hexdigest()

# Decorator to prevent duplicate command execution
def prevent_duplicate(func):
    @wraps(func)
    async def wrapper(ctx, *args, **kwargs):
        command_hash = get_command_hash(ctx)
        
        async with command_lock:
            # Check if command is already being processed
            if command_hash in active_commands:
                start_time, msg_id, _ = active_commands[command_hash]
                elapsed = time.time() - start_time
                APP_LOGGER.warning(f"Duplicate command detected: {ctx.command.name} from {ctx.author} - already running for {elapsed:.1f}s")
                await ctx.send(f"⚠️ This command is already being processed (running for {elapsed:.1f} seconds). Please wait.")
                return
            
            # Mark command as being processed
            active_commands[command_hash] = (time.time(), ctx.message.id, ctx.channel.id)
            APP_LOGGER.info(f"Starting command: {ctx.command.name} from {ctx.author} with hash {command_hash}")
        
        try:
            # Execute the command
            return await func(ctx, *args, **kwargs)
        finally:
            # Always clean up, even if command fails
            async with command_lock:
                if command_hash in active_commands:
                    del active_commands[command_hash]
                    APP_LOGGER.info(f"Completed command: {ctx.command.name} from {ctx.author} with hash {command_hash}")
    
    return wrapper

@bot.event
async def on_ready():
    """
    Called when the client is done preparing the data received from Discord.
    Prints message on terminal when bot successfully connects to discord.

    https://discordpy.readthedocs.io/en/latest/api.html#discord.on_ready
    """
    APP_LOGGER.info(f"Bot {bot.user} has connected to Discord!")
    APP_LOGGER.info(f"Using Discord.py version: {discord.__version__}")

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
@prevent_duplicate
async def ping(ctx, *, arg=None):
    if arg is None:
        await ctx.send("Pong!")
    else:
        await ctx.send(f"Pong! Your argument was {arg}")

# Add a command to get current fact-check status
@bot.command(name="status", help="Check if the bot is currently performing a fact check")
@prevent_duplicate
async def status_command(ctx):
    """Check the status of active commands."""
    if not active_commands:
        await ctx.send("No commands are currently being processed.")
        return
    
    status_text = []
    current_time = time.time()
    
    for cmd_hash, (start_time, msg_id, chan_id) in active_commands.items():
        elapsed = current_time - start_time
        try:
            channel = bot.get_channel(chan_id)
            channel_name = channel.name if channel else "Unknown channel"
            status_text.append(f"• Command hash {cmd_hash[:8]} running for {elapsed:.1f}s in {channel_name}")
        except:
            status_text.append(f"• Command hash {cmd_hash[:8]} running for {elapsed:.1f}s")
    
    await ctx.send("**Active Commands:**\n" + "\n".join(status_text))

# Add a slash command for fact-checking
@bot.command(name="factcheck", help="Fact check a claim by replying to it with this command")
@prevent_duplicate
async def factcheck_command(ctx):
    """Fact check a claim by replying to a message."""
    # Check if this is a reply
    if ctx.message.reference is None:
        await ctx.send("Please reply to a message containing a claim to fact check.")
        return
    
    # Get the referenced message ID
    ref_msg_id = ctx.message.reference.message_id
    
    # Generate a unique request ID for tracking
    request_id = f"{ctx.message.id}-{ref_msg_id}"
    command_hash = get_command_hash(ctx)
    
    APP_LOGGER.info(f"Starting factcheck for request {request_id} (hash: {command_hash})")
    
    # Check if we have a recent result for this message
    current_time = time.time()
    if ref_msg_id in factcheck_cache:
        timestamp, embed = factcheck_cache[ref_msg_id]
        # If the cached result is recent enough, use it
        if current_time - timestamp < CACHE_EXPIRY:
            await ctx.send("📋 Using recent fact-check result:", embed=embed)
            APP_LOGGER.info(f"Used cached result for request {request_id}")
            return
        # Otherwise, remove the expired cache entry
        else:
            del factcheck_cache[ref_msg_id]
    
    try:
        # Get the message being replied to
        referenced_msg = await ctx.channel.fetch_message(ref_msg_id)
        claim = referenced_msg.content
        
        # Send a message indicating work is in progress
        response_msg = await ctx.send(f"🔍 Analyzing claim... This might take a moment. [Request: {request_id}]")
        
        # Process the fact check
        start = time.time()
        APP_LOGGER.info(f"Processing fact check for request {request_id}: {claim}")
        
        embed = await agent.fact_check(claim, request_id)
        
        # Additional request ID for tracking
        embed.add_field(name="\u200b", value=f"Request ID: {request_id}", inline=False)
        
        # Edit our response with the result embed
        await response_msg.edit(content=None, embed=embed)
        
        # Store the result in cache
        factcheck_cache[ref_msg_id] = (current_time, embed)
        
        end = time.time()
        time_delay = end - start
        APP_LOGGER.info(f"Completed fact check in {time_delay:.2f} seconds for request {request_id}")
        
    except Exception as e:
        APP_LOGGER.error(f"Error in fact check for request {request_id}: {e}", exc_info=True)
        if 'response_msg' in locals():
            await response_msg.edit(content=f"Error during fact check: {str(e)} [Request: {request_id}]")
        else:
            await ctx.send(f"Error during fact check: {str(e)}")

def clean_expired_cache():
    """Remove expired entries from the factcheck cache."""
    current_time = time.time()
    expired_keys = [k for k, (timestamp, _) in factcheck_cache.items() 
                   if current_time - timestamp > CACHE_EXPIRY]
    
    for key in expired_keys:
        del factcheck_cache[key]
    
    if expired_keys:
        APP_LOGGER.info(f"Cleaned {len(expired_keys)} expired cache entries")

# Handle bot shutdown
def cleanup():
    """Clean up when the bot shuts down."""
    try:
        if os.path.exists(_instance_lock_file):
            os.remove(_instance_lock_file)
    except:
        pass

# Only run if this file is executed directly
if __name__ == "__main__":
    try:
        # Start the bot, connecting it to the gateway
        APP_LOGGER.info("Starting bot...")
        
        # Register cleanup on exit
        import atexit
        atexit.register(cleanup)
        
        # Run the bot
        bot.run(token)
    except Exception as e:
        APP_LOGGER.error(f"Failed to start bot: {e}", exc_info=True)
        cleanup()
