import os
import json
import sys
import gc
import asyncio
import aiohttp
import socket
import traceback
import subprocess
import logging
import atexit
import signal
from datetime import datetime, timezone, timedelta
from collections import deque

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler('discord_bot.log')
    ]
)
logger = logging.getLogger('tailscale_monitor')

import discord
from discord.ext import commands, tasks

# Persistent state file for notification statuses
# Get configuration directory from environment variable, default to current directory
CONFIG_DIR = os.environ.get("CONFIG_DIR", ".") 

# Ensure the directory exists
if not os.path.exists(CONFIG_DIR):
    os.makedirs(CONFIG_DIR, exist_ok=True)

# Path to notification state file
STATE_FILE = os.path.join(CONFIG_DIR, "notification_state.json")

# Global state tracking per guild.
notification_state = {}

# Discord Rate Limit Handler
class RateLimiter:
    def __init__(self, rate_limit_per_second=1, burst_limit=5):
        self.rate_limit_per_second = rate_limit_per_second  # Standard rate
        self.burst_limit = burst_limit  # Maximum allowed in burst
        self.message_timestamps = deque(maxlen=100)  # Track recent message times
        self.retry_after = 0  # Time to wait if we hit a rate limit
        
    async def acquire(self):
        """Acquire permission to send a message, waiting if necessary"""
        now = time.time()
        
        # If we're in a retry-after period, wait it out
        if self.retry_after > now:
            wait_time = self.retry_after - now
            logger.info(f"Rate limited, waiting {wait_time:.2f} seconds")
            await asyncio.sleep(wait_time)
            return True
            
        # Clean up old timestamps
        while self.message_timestamps and self.message_timestamps[0] < now - 60:
            self.message_timestamps.popleft()
            
        # Check if we've hit the burst limit
        if len(self.message_timestamps) >= self.burst_limit:
            # Calculate the time we need to wait based on the oldest message
            wait_time = max(0, 1.0/self.rate_limit_per_second - (now - self.message_timestamps[-self.burst_limit]))
            if wait_time > 0:
                logger.info(f"Approaching rate limit, throttling for {wait_time:.2f} seconds")
                await asyncio.sleep(wait_time)
                
        # Record this message
        self.message_timestamps.append(time.time())
        return True
        
    def update_from_response(self, response):
        """Update rate limit info based on Discord API response headers"""
        # Check for rate limit headers
        remaining = response.headers.get('X-RateLimit-Remaining')
        reset_after = response.headers.get('X-RateLimit-Reset-After')
        retry_after = response.headers.get('Retry-After')
        
        if retry_after is not None:
            # We hit a rate limit
            retry_seconds = float(retry_after)
            self.retry_after = time.time() + retry_seconds
            logger.warning(f"Discord rate limit hit, retry after {retry_seconds} seconds")
            return True
            
        if remaining is not None and reset_after is not None:
            # Update our understanding of the rate limit
            remaining = int(remaining)
            reset_after = float(reset_after)
            
            if remaining == 0:
                # We're about to hit the rate limit
                self.retry_after = time.time() + reset_after
                logger.warning(f"Discord rate limit reached, cooling down for {reset_after} seconds")
                return True
                
        return False

# Create global rate limiter for Discord messages
global_rate_limiter = RateLimiter(rate_limit_per_second=0.5, burst_limit=5)
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r") as f:
            notification_state = json.load(f)
    except Exception as e:
        print(f"Could not load state file: {e}")

# Global configuration for each guild.
server_config = {}

# Configuration file
# Path to server configuration file
CONFIG_FILE = os.path.join(CONFIG_DIR, "server_config.json")

# Load configuration if it exists
if os.path.exists(CONFIG_FILE):
    try:
        with open(CONFIG_FILE, "r") as f:
            server_config = json.load(f)
    except Exception as e:
        print(f"Could not load config file: {e}")

# Function to save configuration
def save_config():
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump(server_config, f)
    except Exception as e:
        print(f"Error saving config: {e}")

# Tailscale API URL (returns all devices)
TAILSCALE_API_URL = "https://api.tailscale.com/api/v2/tailnet/-/devices"

# Create intents object with message content intent enabled
intents = discord.Intents.default()
intents.message_content = True  # Required for commands to work in Discord.py 2.0+

# DNS Cache to avoid repetitive lookups
class DNSCache:
    def __init__(self):
        self.cache = {}
        # Pre-populate with common Discord domains
        try:
            # Resolve important domains at startup
            for domain in ['discord.com', 'gateway.discord.gg', 'cdn.discordapp.com']:
                ip = socket.gethostbyname(domain)
                self.cache[domain] = ip
                logger.info(f"Pre-cached DNS for {domain}: {ip}")
        except Exception as e:
            logger.warning(f"Failed to pre-cache DNS: {e}")
    
    def get(self, domain):
        return self.cache.get(domain)
    
    def set(self, domain, ip):
        self.cache[domain] = ip

# Initialize DNS cache
dns_cache = DNSCache()

# Create custom TCP connector with DNS caching
class CachingResolver:
    def __init__(self, loop):
        self._loop = loop
    
    async def resolve(self, host, port=0, family=socket.AF_INET):
        # Check cache first
        cached_ip = dns_cache.get(host)
        if cached_ip:
            logger.info(f"Using cached DNS for {host}: {cached_ip}")
            return [{'hostname': host, 'host': cached_ip, 'port': port,
                    'family': family, 'proto': 0, 'flags': 0}]
        
        # If not in cache, resolve normally
        try:
            result = await self._loop.getaddrinfo(host, port, family=family, proto=socket.IPPROTO_TCP)
            # Cache the result for future use
            if result:
                dns_cache.set(host, result[0][4][0])
                logger.info(f"Cached new DNS for {host}: {result[0][4][0]}")
            return [{'hostname': host, 'host': item[4][0], 'port': port,
                    'family': item[0], 'proto': item[2], 'flags': item[3]} for item in result]
        except socket.gaierror as e:
            # Try a local hosts file approach on failure
            fixed_ips = {
                'discord.com': '162.159.136.232',  # Example - this is a Cloudflare IP for Discord
                'gateway.discord.gg': '162.159.135.232',
                'cdn.discordapp.com': '162.159.133.232'
            }
            if host in fixed_ips:
                ip = fixed_ips[host]
                logger.warning(f"DNS lookup failed. Using hardcoded fallback IP for {host}: {ip}")
                return [{'hostname': host, 'host': ip, 'port': port,
                        'family': family, 'proto': 0, 'flags': 0}]
            raise  # Re-raise if no fallback available

# Create a Discord bot with the commands extension and required intents
async def create_aiohttp_session():
    connector = aiohttp.TCPConnector(
        resolver=CachingResolver(asyncio.get_event_loop()),
        ttl_dns_cache=600,  # 10 minutes
        force_close=True,  # Avoid connection pooling issues
        enable_cleanup_closed=True
    )
    return aiohttp.ClientSession(connector=connector)

# Override Discord.py's HTTP client to use our custom session
class CustomHTTPClient(discord.http.HTTPClient):
    async def _HTTPClient__session(self):
        if self.__session is None:
            self.__session = await create_aiohttp_session()
        return self.__session
        
    # Add a proper cleanup method to close the session
    async def close(self):
        if self.__session:
            await self.__session.close()
            self.__session = None

# Create the bot with our custom session handling
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# Patch Discord's HTTP client to use our custom class
discord.http.HTTPClient = CustomHTTPClient

# Custom help command
@bot.command(name="help")
async def help_command(ctx):
    """Display help information for the Tailscale Monitor Bot"""
    embed = discord.Embed(
        title="Tailscale Monitor Bot",
        description="A bot to monitor your Tailscale devices and notify you when they go offline or come back online.",
        color=discord.Color.blue()
    )
    
    # Add setup command info
    embed.add_field(
        name="⚙️ Setup & Configuration",
        value="**!setup** `<api_key> [poll_interval] [device1,device2,...]`\n"
              "Configure the bot to monitor your Tailscale devices.\n"
              "• Get your API key from: [Tailscale Admin Panel](https://login.tailscale.com/admin/settings/keys)\n"
              "• `poll_interval` (optional): Check frequency in seconds (default: 60)\n"
              "• `devices` (optional): Comma-separated list of device names",
        inline=False
    )
    
    # Add device management commands
    embed.add_field(
        name="📱 Device Management",
        value="**!devices** - List all monitored devices and their current status\n"
              "**!add** `<device1,device2,...>` - Add devices to monitoring\n"
              "**!remove** `<device1,device2,...>` - Remove devices from monitoring\n"
              "**!ping** `<device>` - Check if a specific device is online",
        inline=False
    )
    
    # Add monitoring control commands
    embed.add_field(
        name="🔎 Monitoring Controls",
        value="**!start** - Start the monitoring loop\n"
              "**!stop** - Stop the monitoring loop\n"
              "**!interval** `<seconds>` - Change the polling interval\n"
              "**!channel** - Set current channel for notifications",
        inline=False
    )
    
    # Add utility commands
    embed.add_field(
        name="🛠️ Utilities",
        value="**!status** - Check bot's network connectivity\n"
              "**!config** - Show current configuration\n"
              "**!help** - Show this help message",
        inline=False
    )
    
    # Add footer
    embed.set_footer(text="Tailscale Monitor Bot v1.0 | Made with ❤️")
    
    await ctx.send(embed=embed)

async def fetch_devices(api_key: str, session: aiohttp.ClientSession, max_retries=2):
    auth = aiohttp.BasicAuth(api_key, "")
    retry_count = 0
    
    while retry_count <= max_retries:
        try:
            # Cache the DNS for tailscale API before attempting to connect
            if "api.tailscale.com" not in dns_cache.cache:
                try:
                    # Resolve important domains at startup
                    ip = socket.gethostbyname("api.tailscale.com")
                    dns_cache.set("api.tailscale.com", ip)
                    logger.info(f"Cached new DNS for api.tailscale.com: {ip}")
                except Exception as dns_err:
                    logger.warning(f"Failed to resolve api.tailscale.com: {dns_err}")
            else:
                logger.info(f"Using cached DNS for api.tailscale.com: {dns_cache.get('api.tailscale.com')}")
                
            async with session.get(TAILSCALE_API_URL, auth=auth, timeout=30) as response:
                if response.status == 200:
                    return await response.json()
                elif response.status == 401 or response.status == 403:
                    logger.error(f"Authentication error with Tailscale API: HTTP {response.status}")
                    # Set a special flag so monitor_devices can notify the user
                    return {"_auth_error": True, "status": response.status}

                else:
                    logger.warning(f"Error fetching devices: HTTP {response.status}, attempt {retry_count+1}/{max_retries+1}")
                    response_text = await response.text()
                    logger.warning(f"Response body: {response_text[:200]}...")
                    
        except asyncio.TimeoutError:
            logger.warning(f"Timeout connecting to Tailscale API, attempt {retry_count+1}/{max_retries+1}")
        except aiohttp.ClientConnectorError as e:
            logger.warning(f"Connection error to Tailscale API: {e}, attempt {retry_count+1}/{max_retries+1}")
        except Exception as e:
            logger.error(f"Exception fetching devices: {e}", exc_info=True)
            
        # Only retry if we haven't reached max_retries
        if retry_count < max_retries:
            retry_count += 1
            await asyncio.sleep(1)  # Wait before retrying
        else:
            break
            
    return None  # Return None if all retries failed

@tasks.loop(seconds=60)
async def monitor_devices():
    """Polling task: For each server with a config, fetch Tailscale device data and send status updates."""
    logger.info("Running device monitoring cycle")
    try:
        # Create a new session for this monitoring cycle using the same helper as elsewhere
        session = await create_aiohttp_session()
        async with session:

            # Process each server (guild) where the bot is configured.
            for guild in bot.guilds:
                guild_conf = server_config.get(str(guild.id))
                if not guild_conf:
                    continue  # Skip if not yet configured
                    
                api_key = guild_conf["api_key"]
                # Use poll_interval defined in config if needed. In this example, the task always polls every 60s.
                monitored_devices = guild_conf.get("devices")  # None means all devices

                # Use the configured notification channel if available
                notification_channel_id = guild_conf.get("notification_channel_id")
                
                if notification_channel_id:
                    # Try to get the configured channel
                    channel = guild.get_channel(notification_channel_id)
                    if not channel:
                        # If channel no longer exists, fall back to the first available channel
                        logger.warning(f"Configured notification channel {notification_channel_id} not found for guild {guild.id}")
                        if not guild.text_channels:
                            continue
                        channel = guild.text_channels[0]
                        # Update the configuration with the new channel
                        guild_conf["notification_channel_id"] = channel.id
                        save_config()
                else:
                    # No channel configured, use the first available one
                    if not guild.text_channels:
                        continue
                    channel = guild.text_channels[0]
                    # Update the configuration with this channel
                    guild_conf["notification_channel_id"] = channel.id
                    save_config()

                # Skip guilds where monitoring is explicitly stopped
                if guild_conf.get("monitoring_stopped", False):
                    logger.info(f"Skipping guild {guild.id} as monitoring is stopped")
                    continue
                    
                now = datetime.now(timezone.utc)
                data = await fetch_devices(api_key, session)
                # Handle authentication errors distinctly
                if isinstance(data, dict) and data.get("_auth_error"):
                    guild_state = notification_state.setdefault(str(guild.id), {})
                    last_auth_error = guild_state.get("last_auth_error", 0)
                    current_time = int(datetime.now().timestamp())
                    if current_time - last_auth_error > 3600:
                        # Use the rate limited message helper
                        await send_message_with_rate_limit(
                            channel,
                            content=f"❌ Authentication error with Tailscale API (HTTP {data['status']}). Please re-run `!setup` to update your API key."
                        )
                        
                        guild_state["last_auth_error"] = current_time
                        try:
                            with open(STATE_FILE, "w") as f:
                                json.dump(notification_state, f)
                        except Exception as e:
                            logger.error(f"Error saving error state: {e}")
                    continue
                if data is None:
                    # Only notify the guild about API failures every hour to avoid spam
                    guild_state = notification_state.setdefault(str(guild.id), {})
                    last_api_error = guild_state.get("last_api_error", 0)
                    current_time = int(datetime.now().timestamp())
                    
                    if current_time - last_api_error > 3600:  # 1 hour
                        # Use the rate limited message helper
                        await send_message_with_rate_limit(
                            channel,
                            content="⚠️ Error fetching Tailscale devices data. Will continue monitoring."
                        )
                        
                        guild_state["last_api_error"] = current_time
                        # Save the updated error state
                        try:
                            with open(STATE_FILE, "w") as f:
                                json.dump(notification_state, f)
                        except Exception as e:
                            logger.error(f"Error saving error state: {e}")
                    continue

                # Count of notifications to be sent this cycle
                notifications_count = 0
                notifications_to_send = []
                
                # First, gather all devices that need notifications
                for device in data.get("devices", []):
                    name = device.get("name")
                    # If a device list is specified, skip devices not in that list.
                    if monitored_devices and name not in monitored_devices:
                        continue

                    last_seen_str = device.get("lastSeen")
                    try:
                        last_seen = datetime.strptime(last_seen_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    except Exception as e:
                        logger.error(f"Error parsing time for {name}: {e}")
                        continue

                    # Fixed threshold of 6 minutes (adjust if needed)
                    threshold = timedelta(minutes=6)
                    offline = (now - last_seen) > threshold
                    # Guild-specific state tracking
                    guild_state = notification_state.setdefault(str(guild.id), {})
                    notified = guild_state.get(name, False)

                    # Only queue notifications if status has changed
                    if (offline and not notified) or (not offline and notified):
                        minutes_offline = int((now - last_seen).total_seconds() // 60)
                        
                        if offline and not notified:
                            message = (f"🔴 Device '{name}' has not been seen for {minutes_offline} minute(s). "
                                       f"Last seen: {last_seen.strftime('%Y-%m-%d %H:%M:%S')} UTC.")
                            notifications_to_send.append((name, message, True))
                        elif not offline and notified:
                            message = (f"🟢 Device '{name}' is back online! "
                                       f"Last seen: {last_seen.strftime('%Y-%m-%d %H:%M:%S')} UTC.")
                            notifications_to_send.append((name, message, False))
                
                # Prioritize notifications if we're over rate limits
                if len(notifications_to_send) > 10:
                    # Prioritize offline notifications over online ones
                    offline_notifications = [n for n in notifications_to_send if n[2]]
                    online_notifications = [n for n in notifications_to_send if not n[2]]
                    
                    # Take all offline notifications and enough online ones to fit within rate limits
                    notifications_to_send = offline_notifications + online_notifications[:max(0, 10 - len(offline_notifications))]
                    logger.warning(f"Rate limiting notifications for guild {guild.id}: sending {len(notifications_to_send)} of {len(notifications_to_send)} possible notifications")
                
                # Now send the notifications with rate limiting
                for name, message, is_offline in notifications_to_send:
                    # Apply rate limiter before sending each message
                    await global_rate_limiter.acquire()
                    
                    try:
                        # Use the rate limited message helper
                        await send_message_with_rate_limit(channel, content=message)
                        
                        # Update state after successful send
                        guild_state = notification_state.setdefault(str(guild.id), {})
                        guild_state[name] = is_offline
                        
                        # Add small delay between messages to prevent bursts
                        if len(notifications_to_send) > 3:
                            await asyncio.sleep(0.5)
                            
                    except Exception as e:
                        logger.error(f"Error sending notification: {e}")

                # Persist the updated notification state after processing each guild
                try:
                    with open(STATE_FILE, "w") as f:
                        json.dump(notification_state, f)
                except Exception as e:
                    logger.error(f"Error saving state: {e}")
    except Exception as e:
        # Suppress aiohttp bug: 'NoneType' object has no attribute '_abort' on session close
        if isinstance(e, AttributeError) and "_abort" in str(e):
            logger.warning(f"Suppressed aiohttp session close bug: {e}")
        else:
            logger.error(f"Error in monitor_devices: {e}", exc_info=True)

@bot.command(name="setup")
async def setup(ctx, api_key: str, poll_interval: int = 60, *, devices: str = None):
    """
    Configure the bot to monitor Tailscale devices.
    
    Get your API key from: https://login.tailscale.com/admin/settings/keys
    under 'API access tokens'
    
    Usage:
       !setup <api_key> [poll_interval_in_seconds] [device1,device2,...]
    
    If no device list is provided, the bot monitors all devices.
    """
    logger.info(f"Setup command invoked by {ctx.author} in {ctx.guild}")
    
    # First send an acknowledgement so the user knows the command was received
    try:
        await ctx.send("Processing setup command... This may take a moment.")
    except Exception as e:
        logger.error(f"Failed to send acknowledgement: {e}")
        # Continue anyway - we'll try to set up even if we can't send messages yet
    
    guild_id = str(ctx.guild.id)
    device_list = [d.strip() for d in devices.split(",")] if devices else None

    # Validate API key by making a test request with our custom session
    try:
        session = await create_aiohttp_session()
        async with session:
            test_result = await fetch_devices(api_key, session)
            if test_result is None:
                await ctx.send("❌ Invalid API key or connection error. Please check your Tailscale API key and try again.")
                return
    except Exception as e:
        logger.error(f"Error validating API key: {e}")
        await ctx.send(f"❌ Error validating API key: {str(e)}")
        return

    # Store the channel ID where the setup command was used
    notification_channel_id = ctx.channel.id
    
    server_config[guild_id] = {
        "api_key": api_key,
        "poll_interval": poll_interval,
        "devices": device_list,
        "notification_channel_id": notification_channel_id,  # Save the channel ID
        "monitoring_stopped": False  # Reset stopped state on setup
    }
    
    # Let the user know which channel will be used for notifications
    channel_mention = f"<#{notification_channel_id}>"
    await ctx.send(f"ℹ️ Notifications will be sent to {channel_mention}")
    
    await ctx.send(
        f"✅ Configuration updated for this server:\n"
        f"- Polling interval: {poll_interval} seconds\n"
        f"- Devices: {', '.join(device_list) if device_list else 'All devices'}"
    )

    # Start the monitoring loop if it isn't already running.
    save_config()
    # If monitoring was previously stopped, clear the stopped state
    server_config[guild_id]["monitoring_stopped"] = False
    save_config()
    if not monitor_devices.is_running():
        monitor_devices.start()
        await ctx.send("🔄 Device monitoring has started!")
    else:
        await ctx.send("ℹ️ Device monitoring was already running and will continue with the updated configuration.")

@bot.event
async def on_ready():
    print(f"Logged in as: {bot.user}")
    print(f"Connected to {len(bot.guilds)} servers")
    
    # Auto-start monitoring if configurations exist
    # Only auto-start monitoring if at least one guild is not marked as stopped
    should_start = False
    if server_config and not monitor_devices.is_running():
        for guild_id, conf in server_config.items():
            if not conf.get("monitoring_stopped", False):
                should_start = True
                break
    if should_start:
        print("Found existing configuration - auto-starting device monitoring...")
        try:
            # Set the interval based on the first guild's configuration
            if server_config:
                first_guild = next(iter(server_config.values()))
                interval = first_guild.get("poll_interval", 60)
                monitor_devices.change_interval(seconds=interval)
                logger.info(f"Using poll interval of {interval} seconds from saved configuration")
            # Start the monitoring
            monitor_devices.start()
            print("Device monitoring started automatically!")
        except Exception as e:
            logger.error(f"Failed to auto-start monitoring: {e}", exc_info=True)
    
    print("Bot is ready to receive commands!")
    
    # Set a custom status
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="Tailscale devices | !help"
        )
    )
    
    # Send a message to the configured notification channel of each guild
    for guild in bot.guilds:
        guild_id = str(guild.id)
        if guild_id in server_config:
            try:
                # Use the configured notification channel if available
                notification_channel_id = server_config[guild_id].get("notification_channel_id")
                
                if notification_channel_id:
                    channel = guild.get_channel(notification_channel_id)
                    if channel:
                        await channel.send("🟢 **Tailscale Monitor Bot has (re)started**\n" +
                                          "Device monitoring is active. Type `!help` to see available commands.")
                        continue
                
                # Fall back to first channel if configured channel not found
                if guild.text_channels:
                    channel = guild.text_channels[0]
                    await channel.send("🟢 **Tailscale Monitor Bot has (re)started**\n" +
                                      "Device monitoring is active. Type `!help` to see available commands.")
            except Exception as e:
                logger.warning(f"Could not send startup message to guild {guild.id}: {e}")

@bot.event
async def on_command_error(ctx, error):
    """Global error handler for command errors"""
    if isinstance(error, commands.MissingRequiredArgument):
        if ctx.command.name == "setup":
            # Detailed help for setup command
            await ctx.send(f"❌ Missing required argument: {error.param.name}")
            await ctx.send("**Tailscale Monitor Bot - Setup Instructions**\n\n"
                          "To set up the bot, you need a Tailscale API key.\n\n"
                          "**How to get an API key:**\n"
                          "1. Go to https://login.tailscale.com/admin/settings/keys\n"
                          "2. Look under 'API access tokens'\n"
                          "3. Create a new API key with appropriate permissions\n\n"
                          "**Command usage:**\n"
                          "`!setup <api_key> [poll_interval_in_seconds] [device1,device2,...]`\n\n"
                          "- `poll_interval` defaults to 60 seconds if not specified\n"
                          "- If no devices are specified, all devices will be monitored")
        else:
            await ctx.send(f"❌ Missing required argument: {error.param.name}")
    elif isinstance(error, commands.CommandInvokeError):
        await ctx.send(f"❌ Error executing command: {error.original}")
        logger.error(f"Command error: {error}", exc_info=True)
        traceback.print_exception(type(error), error, error.__traceback__)
    else:
        await ctx.send(f"❌ Error: {error}")
        logger.error(f"General error: {error}", exc_info=True)
        traceback.print_exception(type(error), error, error.__traceback__)

@bot.listen('on_message')
async def on_message(message):
    # Log message details for debugging
    if message.content.startswith('!'):
        print(f"Command received: {message.content} from {message.author} in {message.guild}")
    
    # Don't process commands here - bot.process_commands is called automatically

# ---
# Config reload command for admins
import functools

def reload_config_from_disk():
    global server_config
    try:
        with open(CONFIG_FILE, "r") as f:
            server_config = json.load(f)
        logger.info("Reloaded server_config from disk.")
        return True
    except Exception as e:
        logger.error(f"Failed to reload config: {e}")
        return False

@bot.command(name="reload_config")
@commands.has_permissions(administrator=True)
async def reload_config(ctx):
    """Reload the server configuration from disk (admin only)"""
    if reload_config_from_disk():
        await ctx.send("✅ Configuration reloaded from disk.")
    else:
        await ctx.send("❌ Failed to reload configuration from disk. Check logs.")

# Channel configuration command
@bot.command(name="channel")
async def set_channel(ctx):
    """Set the current channel as the notification channel"""
    guild_id = str(ctx.guild.id)
    if guild_id not in server_config:
        await ctx.send("❌ This server is not set up yet. Use `!setup` first.")
        return
    
    # Update the notification channel to the current channel
    channel_id = ctx.channel.id
    server_config[guild_id]["notification_channel_id"] = channel_id
    save_config()
    
    # Confirm the change
    await ctx.send(f"✅ Notification channel updated! All Tailscale device notifications will now be sent to this channel.")

# Device management commands
@bot.command(name="devices")
async def list_devices(ctx):
    """List all devices being monitored and their status"""
    guild_id = str(ctx.guild.id)
    if guild_id not in server_config:
        await ctx.send("❌ This server is not set up yet. Use `!setup` first.")
        return
    
    guild_conf = server_config[guild_id]
    api_key = guild_conf["api_key"]
    monitored_devices = guild_conf.get("devices")
    
    embed = discord.Embed(
        title="Tailscale Devices",
        description="Current status of your Tailscale devices",
        color=discord.Color.blue()
    )
    
    try:
        session = await create_aiohttp_session()
        async with session:
            data = await fetch_devices(api_key, session)
            if data is None:
                await ctx.send("❌ Error fetching device data. Please check your API key.")
                return
            
            now = datetime.now(timezone.utc)
            threshold = timedelta(minutes=6)
            
            for device in data.get("devices", []):
                name = device.get("name")
                # Skip devices not in monitored list if a list is specified
                if monitored_devices and name not in monitored_devices:
                    continue
                
                # Get device status
                last_seen_str = device.get("lastSeen")
                try:
                    last_seen = datetime.strptime(last_seen_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    minutes_ago = int((now - last_seen).total_seconds() // 60)
                    offline = (now - last_seen) > threshold
                    
                    status = "🔴 Offline" if offline else "🔵 Online"
                    value = f"Last seen: {last_seen.strftime('%Y-%m-%d %H:%M:%S')} UTC ({minutes_ago} mins ago)"
                    
                    embed.add_field(name=f"{name} - {status}", value=value, inline=False)
                except Exception as e:
                    embed.add_field(name=f"{name} - ❓ Unknown", value=f"Error: {str(e)}", inline=False)
    
    except Exception as e:
        logger.error(f"Error listing devices: {e}", exc_info=True)
        await ctx.send(f"❌ Error listing devices: {str(e)}")
        return
    
    if len(embed.fields) == 0:
        if monitored_devices:
            embed.description = "No devices found matching your monitoring list."
        else:
            embed.description = "No devices found in your Tailscale account."
    
    await ctx.send(embed=embed)

@bot.command(name="add")
async def add_devices(ctx, *, devices: str):
    """Add devices to the monitoring list"""
    guild_id = str(ctx.guild.id)
    if guild_id not in server_config:
        await ctx.send("❌ This server is not set up yet. Use `!setup` first.")
        return
    
    device_list = [d.strip() for d in devices.split(",")]
    if not device_list:
        await ctx.send("❌ Please specify at least one device to add.")
        return
    
    # Get current device list or create new one
    current_devices = server_config[guild_id].get("devices", [])
    if current_devices is None:  # If monitoring all devices
        await ctx.send("❌ Currently monitoring all devices. Use `!remove` first to switch to selective monitoring.")
        return
    
    # Add new devices
    for device in device_list:
        if device not in current_devices:
            current_devices.append(device)
    
    server_config[guild_id]["devices"] = current_devices
    save_config()
    
    await ctx.send(f"✅ Added {len(device_list)} device(s) to monitoring list. Now monitoring: {', '.join(current_devices)}")

@bot.command(name="remove")
async def remove_devices(ctx, *, devices: str):
    """Remove devices from the monitoring list"""
    guild_id = str(ctx.guild.id)
    if guild_id not in server_config:
        await ctx.send("❌ This server is not set up yet. Use `!setup` first.")
        return
    
    device_list = [d.strip() for d in devices.split(",")]
    if not device_list:
        await ctx.send("❌ Please specify at least one device to remove.")
        return
    
    # Get current device list
    current_devices = server_config[guild_id].get("devices", [])
    if current_devices is None:  # If monitoring all devices
        # Create a new list with all devices except the ones to remove
        try:
            session = await create_aiohttp_session()
            async with session:
                data = await fetch_devices(server_config[guild_id]["api_key"], session)
                if data is None:
                    await ctx.send("❌ Error fetching device data. Please check your API key.")
                    return
                
                all_devices = [device.get("name") for device in data.get("devices", [])]
                current_devices = [d for d in all_devices if d not in device_list]
                server_config[guild_id]["devices"] = current_devices
                save_config()
                
                await ctx.send(f"✅ Switched from monitoring all devices to selective monitoring.")
                await ctx.send(f"Now monitoring {len(current_devices)} device(s): {', '.join(current_devices)}")
        except Exception as e:
            logger.error(f"Error removing devices: {e}", exc_info=True)
            await ctx.send(f"❌ Error: {str(e)}")
        return
    
    # Remove devices from the list
    removed = []
    for device in device_list:
        if device in current_devices:
            current_devices.remove(device)
            removed.append(device)
    
    if not removed:
        await ctx.send("❌ None of the specified devices were in your monitoring list.")
        return
    
    server_config[guild_id]["devices"] = current_devices
    save_config()
    
    if current_devices:
        await ctx.send(f"✅ Removed {len(removed)} device(s) from monitoring. Still monitoring: {', '.join(current_devices)}")
    else:
        server_config[guild_id]["devices"] = None  # Switch back to monitoring all
        save_config()
        await ctx.send("✅ All devices removed from selective monitoring. Now monitoring all devices.")

@bot.command(name="ping")
async def ping_device(ctx, device_name: str):
    """Check if a specific device is online"""
    guild_id = str(ctx.guild.id)
    if guild_id not in server_config:
        await ctx.send("❌ This server is not set up yet. Use `!setup` first.")
        return
    
    api_key = server_config[guild_id]["api_key"]
    
    try:
        await ctx.send(f"Checking status of device: `{device_name}`...")
        
        session = await create_aiohttp_session()
        async with session:
            data = await fetch_devices(api_key, session)
            if data is None:
                await ctx.send("❌ Error fetching device data. Please check your API key.")
                return
            
            found = False
            now = datetime.now(timezone.utc)
            threshold = timedelta(minutes=6)
            
            for device in data.get("devices", []):
                name = device.get("name")
                if name == device_name:
                    found = True
                    last_seen_str = device.get("lastSeen")
                    try:
                        last_seen = datetime.strptime(last_seen_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        minutes_ago = int((now - last_seen).total_seconds() // 60)
                        offline = (now - last_seen) > threshold
                        
                        embed = discord.Embed(
                            title=f"Device Status: {name}",
                            description=f"{'🔴 Device is offline' if offline else '🟢 Device is online'}",
                            color=discord.Color.red() if offline else discord.Color.green()
                        )
                        
                        embed.add_field(
                            name="Last Seen", 
                            value=f"{last_seen.strftime('%Y-%m-%d %H:%M:%S')} UTC", 
                            inline=True
                        )
                        embed.add_field(
                            name="Time Since Last Seen", 
                            value=f"{minutes_ago} minute(s) ago", 
                            inline=True
                        )
                        
                        # Add OS and other device info if available
                        if "os" in device:
                            embed.add_field(name="OS", value=device["os"], inline=True)
                        if "machineHostname" in device:
                            embed.add_field(name="Hostname", value=device["machineHostname"], inline=True)
                        
                        await ctx.send(embed=embed)
                    except Exception as e:
                        await ctx.send(f"❌ Error processing device data: {str(e)}")
                    break
            
            if not found:
                await ctx.send(f"❌ Device '{device_name}' not found in your Tailscale network.")
    
    except Exception as e:
        logger.error(f"Error pinging device: {e}", exc_info=True)
        await ctx.send(f"❌ Error checking device: {str(e)}")

async def send_message_with_rate_limit(channel, content=None, embed=None):
    """Send a message to a channel with rate limiting applied"""
    try:
        # Apply rate limiting
        await global_rate_limiter.acquire()
        
        try:    
            response = await channel.send(content=content, embed=embed)
            # Update rate limiter based on response
            if hasattr(response, "_http") and hasattr(response._http, "headers"):
                global_rate_limiter.update_from_response(response._http)
            return response
        except discord.errors.HTTPException as e:
            if e.status == 429:  # Rate limit error
                retry_after = e.retry_after
                logger.warning(f"Discord rate limit hit, waiting {retry_after} seconds")
                # Update our rate limiter
                global_rate_limiter.retry_after = time.time() + retry_after
                await asyncio.sleep(retry_after)
                # Try again after waiting
                return await channel.send(content=content, embed=embed)
            else:
                raise
    except Exception as e:
        logger.error(f"Error sending message: {e}", exc_info=True)
        raise

# Monitoring control commands
@bot.command(name="start")
async def start_monitoring(ctx):
    """Start the device monitoring loop"""
    if not server_config.get(str(ctx.guild.id)):
        await ctx.send("❌ This server is not set up yet. Use `!setup` first.")
        return
    
    guild_id = str(ctx.guild.id)
    if guild_id in server_config:
        server_config[guild_id]["monitoring_stopped"] = False
        save_config()
    if monitor_devices.is_running():
        await ctx.send("ℹ️ Monitoring is already running.")
    else:
        monitor_devices.start()
        await ctx.send("🔄 Device monitoring has started!")

@bot.command(name="stop")
async def stop_monitoring(ctx):
    """Stop the device monitoring loop"""
    guild_id = str(ctx.guild.id)
    if guild_id in server_config:
        server_config[guild_id]["monitoring_stopped"] = True
        save_config()
    if monitor_devices.is_running():
        monitor_devices.cancel()
        await ctx.send("⏹️ Device monitoring has been stopped.")
    else:
        await ctx.send("ℹ️ Monitoring is not currently running.")

@bot.command(name="interval")
async def set_interval(ctx, seconds: int):
    """Change the polling interval"""
    guild_id = str(ctx.guild.id)
    if guild_id not in server_config:
        await ctx.send("❌ This server is not set up yet. Use `!setup` first.")
        return
    
    if seconds < 60:
        await ctx.send("❌ Polling interval must be at least 60 seconds to avoid rate limiting.")
        return
    
    server_config[guild_id]["poll_interval"] = seconds
    save_config()
    
    # Restart the loop if it's running
    was_running = monitor_devices.is_running()
    if was_running:
        monitor_devices.cancel()
        monitor_devices.change_interval(seconds=seconds)
        monitor_devices.start()
        await ctx.send(f"✅ Polling interval updated to {seconds} seconds. Monitoring restarted.")
    else:
        await ctx.send(f"✅ Polling interval updated to {seconds} seconds. Monitoring is not currently running.")

@bot.command(name="config")
async def show_config(ctx):
    """Show the current configuration"""
    guild_id = str(ctx.guild.id)
    if guild_id not in server_config:
        await ctx.send("❌ This server is not set up yet. Use `!setup` first.")
        return
    
    guild_conf = server_config[guild_id]
    
    embed = discord.Embed(
        title="Tailscale Monitor Configuration",
        color=discord.Color.blue()
    )
    
    # Mask the API key for security
    api_key = guild_conf["api_key"]
    masked_key = api_key[:5] + "*" * (len(api_key) - 9) + api_key[-4:]
    embed.add_field(name="API Key", value=f"`{masked_key}`", inline=False)
    
    # Poll interval
    poll_interval = guild_conf.get("poll_interval", 60)
    embed.add_field(name="Poll Interval", value=f"{poll_interval} seconds", inline=True)
    
    # Monitoring status
    status = "Running" if monitor_devices.is_running() else "Stopped"
    embed.add_field(name="Status", value=status, inline=True)
    
    # Devices being monitored
    devices = guild_conf.get("devices")
    if devices is None:
        device_text = "All devices"
    elif not devices:
        device_text = "No devices (monitoring disabled)"
    else:
        device_text = ", ".join(devices)
    embed.add_field(name="Monitored Devices", value=device_text, inline=False)
    
    await ctx.send(embed=embed)

# Network status command
@bot.command(name="status")
async def status(ctx):
    """Check the bot's network connectivity and monitored device status"""
    try:
        await ctx.send("Checking system and device status... This may take a moment.")
        
        # Part 1: Bot connectivity status
        diagnostics_output = []
        
        # Check DNS cache
        diagnostics_output.append("📋 **DNS Cache Status:**")
        for domain, ip in dns_cache.cache.items():
            diagnostics_output.append(f"- {domain}: {ip}")
        
        # Check current connectivity
        diagnostics_output.append("\n📡 **Current Connectivity:**")
        
        # Test Discord connectivity
        try:
            session = await create_aiohttp_session()
            async with session.get("https://discord.com/api/v10/gateway") as resp:
                if resp.status == 200:
                    diagnostics_output.append("- ✅ Discord API: Connected")
                else:
                    diagnostics_output.append(f"- ❌ Discord API: Error (Status {resp.status})")
        except Exception as e:
            diagnostics_output.append(f"- ❌ Discord API: {str(e)}")
        
        # Send bot status results
        output = "\n".join(diagnostics_output)
        chunks = [output[i:i+1900] for i in range(0, len(output), 1900)]
        for chunk in chunks:
            await ctx.send(f"```{chunk}```")
            
        # Part 2: Tailscale device status
        guild_id = str(ctx.guild.id)
        if guild_id not in server_config:
            await ctx.send("❌ This server is not set up for Tailscale monitoring. Use `!setup` first.")
            return
        
        guild_conf = server_config[guild_id]
        api_key = guild_conf["api_key"]
        monitored_devices = guild_conf.get("devices")
        
        embed = discord.Embed(
            title="Tailscale Device Status",
            description="Current status of monitored Tailscale devices",
            color=discord.Color.blue()
        )
        
        try:
            session = await create_aiohttp_session()
            async with session:
                data = await fetch_devices(api_key, session)
                if data is None:
                    await ctx.send("❌ Error fetching device data. Please check your API key.")
                    return
                
                now = datetime.now(timezone.utc)
                threshold = timedelta(minutes=6)
                
                online_count = 0
                offline_count = 0
                unknown_count = 0
                
                # Track devices by status for a more organized display
                online_devices = []
                offline_devices = []
                unknown_devices = []
                
                for device in data.get("devices", []):
                    name = device.get("name")
                    # Skip devices not in monitored list if a list is specified
                    if monitored_devices and name not in monitored_devices:
                        continue
                    
                    # Get device status
                    last_seen_str = device.get("lastSeen")
                    try:
                        last_seen = datetime.strptime(last_seen_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                        minutes_ago = int((now - last_seen).total_seconds() // 60)
                        offline = (now - last_seen) > threshold
                        
                        status_info = {
                            "name": name,
                            "last_seen": last_seen.strftime('%Y-%m-%d %H:%M:%S'),
                            "minutes_ago": minutes_ago
                        }
                        
                        if offline:
                            offline_count += 1
                            offline_devices.append(status_info)
                        else:
                            online_count += 1
                            online_devices.append(status_info)
                    except Exception as e:
                        unknown_count += 1
                        unknown_devices.append({"name": name, "error": str(e)})
                
                # Add summary field
                embed.add_field(
                    name="📊 Summary",
                    value=f"🔵 Online: {online_count} | 🔴 Offline: {offline_count} | ❓ Unknown: {unknown_count}",
                    inline=False
                )
                
                # Add online devices
                if online_devices:
                    online_text = "\n".join([f"**{d['name']}** - {d['minutes_ago']} mins ago" for d in online_devices])
                    embed.add_field(name="🔵 Online Devices", value=online_text or "None", inline=False)
                
                # Add offline devices
                if offline_devices:
                    offline_text = "\n".join([f"**{d['name']}** - Last seen: {d['last_seen']} UTC ({d['minutes_ago']} mins ago)" for d in offline_devices])
                    embed.add_field(name="🔴 Offline Devices", value=offline_text or "None", inline=False)
                
                # Add unknown devices
                if unknown_devices:
                    unknown_text = "\n".join([f"**{d['name']}** - Error: {d['error']}" for d in unknown_devices])
                    embed.add_field(name="❓ Unknown Status", value=unknown_text or "None", inline=False)
        
        except Exception as e:
            logger.error(f"Error listing devices in status command: {e}", exc_info=True)
            await ctx.send(f"❌ Error listing devices: {str(e)}")
            return
        
        if len(embed.fields) == 1:  # Only summary field
            if monitored_devices:
                embed.description = "No devices found matching your monitoring list."
            else:
                embed.description = "No devices found in your Tailscale account."
        
        await ctx.send(embed=embed)
    except Exception as e:
        logger.error(f"Error in status command: {e}")
        await ctx.send(f"❌ Error checking status: {str(e)}")

# Network diagnostics function
def run_network_diagnostics():
    print("\n==== RUNNING NETWORK DIAGNOSTICS ====")
    
    # Check if we can resolve DNS
    print("\n-- DNS Resolution Test --")
    domains_to_check = ["discord.com", "google.com", "api.tailscale.com"]
    for domain in domains_to_check:
        try:
            ip = socket.gethostbyname(domain)
            print(f"✓ Successfully resolved {domain} to {ip}")
        except socket.gaierror as e:
            print(f"✗ Failed to resolve {domain}: {e}")
    
    # Check connectivity with ping
    print("\n-- Ping Test --")
    for domain in domains_to_check:
        try:
            result = subprocess.run(["ping", "-c", "1", "-W", "2", domain], 
                                   capture_output=True, text=True, timeout=3)
            if result.returncode == 0:
                print(f"✓ Successfully pinged {domain}")
            else:
                print(f"✗ Failed to ping {domain}: {result.stderr or result.stdout}")
        except subprocess.TimeoutExpired:
            print(f"✗ Ping to {domain} timed out")
        except Exception as e:
            print(f"✗ Error pinging {domain}: {e}")
    
    # Check internet connectivity
    print("\n-- HTTP Connectivity Test --")
    for url in ["https://www.google.com", "https://discord.com", "https://api.tailscale.com"]:
        try:
            result = subprocess.run(["curl", "--max-time", "5", "-I", url], 
                                  capture_output=True, text=True, timeout=6)
            if result.returncode == 0:
                print(f"✓ Successfully connected to {url}")
            else:
                print(f"✗ Failed to connect to {url}: {result.stderr}")
        except Exception as e:
            print(f"✗ Error connecting to {url}: {e}")
    
    print("\n==== NETWORK DIAGNOSTICS COMPLETE ====\n")

# Define cleanup handler for proper resource management
def cleanup_resources():
    # This will be called when the program exits
    print("Performing cleanup before shutdown...")
    
    # Close any pending aiohttp sessions
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    async def close_sessions():
        current = asyncio.current_task(loop=loop)
        tasks = [t for t in asyncio.all_tasks(loop=loop) if t is not current and not t.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    
    try:
        # Run the cleanup coroutine
        loop.run_until_complete(close_sessions())
    except asyncio.CancelledError:
        # Ignore cancellation errors during cleanup
        pass
    except Exception as e:
        print(f"Error during session cleanup: {e}")
    finally:
        loop.close()
    
    # Force garbage collection
    gc.collect()
    print("Cleanup complete. Bot shutting down.")

# Register the cleanup function to run on exit
atexit.register(cleanup_resources)

# Handle termination signals gracefully
def signal_handler(signum, frame):
    print(f"\nReceived signal {signum}. Shutting down gracefully...")
    sys.exit(0)  # This will trigger the atexit handlers

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# Run diagnostics and then start the bot
token = os.environ.get("DISCORD_BOT_TOKEN")
if not token:
    print("ERROR: DISCORD_BOT_TOKEN environment variable not set!")
    exit(1)

# Check if network diagnostics is needed
diag_arg = "--network-diagnostics"
if diag_arg in sys.argv or (len(sys.argv) > 1 and sys.argv[1] == "--diagnose"):
    run_network_diagnostics()
    # Free up memory after running diagnostics (optional but good practice)
    gc.collect()

try:
    print("Attempting to connect to Discord...")
    # Run the bot with proper cleanup
    bot.run(token)
except discord.errors.LoginFailure:
    print("ERROR: Invalid Discord token!")
except aiohttp.client_exceptions.ClientConnectorError as e:
    print(f"ERROR: Network connection issue: {e}")
    print("\nRunning network diagnostics...")
    run_network_diagnostics()
    print("\nTroubleshooting suggestions:")
    print("1. Check if this machine has internet access")
    print("2. Verify DNS settings (e.g., check /etc/resolv.conf)")
    print("3. Try adding public DNS servers like 8.8.8.8 to your network config")
    print("4. Check if a firewall is blocking outbound connections")
    print("5. If using a proxy, ensure it's properly configured")
except Exception as e:
    print(f"ERROR: Failed to start bot: {e}")
    traceback.print_exc()