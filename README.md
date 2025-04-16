# Tailscale Monitor

This project provides two implementations to monitor your Tailscale devices and receive notifications when devices go offline or come back online:

1. A Discord bot that actively monitors devices in real-time
2. A shell script that can be run via cron to periodically check device status

## Features

- Monitors all devices in your Tailscale tailnet
- Sends notifications only once per offline event
- Sends recovery notifications when devices come back online
- Maintains state between restarts to avoid duplicate notifications
- Supports per-guild configurations (Discord bot)
- Can monitor specific devices or all devices (Discord bot)

## Implementation Options

### Option 1: Discord Bot (Recommended)

The Discord bot (`bot.py`) provides a real-time, interactive way to monitor your Tailscale devices.

**Setup:**
1. Install dependencies: `pip install -r requirements.txt`
2. Set the environment variable `DISCORD_BOT_TOKEN` with your Discord bot token
3. Set the environment variable `TAILSCALE_API_KEY` with your Tailscale API key
4. Run the bot: `python bot.py`

**Docker Setup:**
```bash
docker-compose up -d
```

### Option 2: Shell Script with Cron

The shell script (`tailscale_monitor.sh`) can be run periodically via cron to check device status.

**Setup:**
1. Make sure the script is executable: `chmod +x tailscale_monitor.sh`
2. Set your Tailscale API key and Discord webhook URL in the script
3. Configure your crontab to run the script every 5 minutes:
   ```bash
   crontab -e
   */5 * * * * /path/to/tailscale_monitor.sh
   ```

## Docker Deployment

The project includes Docker support for easy deployment:

```bash
# Build and start the container
docker-compose up -d

# View logs
docker-compose logs -f

# Stop the container
docker-compose down
```

## Configuration

### Discord Bot
- Environment variables:
  - `DISCORD_BOT_TOKEN`: Your Discord bot token
  - `TAILSCALE_API_KEY`: Your Tailscale API key

### Shell Script
- Configurable values at the top of the script:
  - `API_KEY`: Your Tailscale API key
  - `DISCORD_WEBHOOK`: Your Discord webhook URL
  - `THRESHOLD_MINUTES`: Time threshold in minutes (default: 6)
  - `STATE_FILE`: Path to store notification state

## Getting Started

1. Clone this repository
2. Choose your preferred implementation (Discord bot or shell script)
3. Configure environment variables or update the script as needed
4. Deploy using Docker or run directly

## Security Note

Protect your API keys and tokens. For local development, use an `.env` file (which is git-ignored). For production, use secure environment variables.
