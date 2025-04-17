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
3. Run the bot: `python bot.py`
4. In Discord, use the `!setup` command with your Tailscale API key to configure monitoring

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
- Discord commands:
  - `!setup <api_key>`: Configure Tailscale monitoring with your API key

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

## Environment Variables

### Where to Put Variables

There are two ways to manage environment variables in this project:

1. **Environment Files**:
   - `.env` (for local development) - Git-ignored file for your personal API keys
   - `.env.server` (for server deployment) - Template file with placeholders

2. **System Environment Variables**:
   - Set directly in your shell or deployment platform

### Required Variables

For the Discord Bot:
```
# Required
DISCORD_BOT_TOKEN=your_discord_bot_token_here

# Optional
LOG_LEVEL=INFO  # Options: DEBUG, INFO, WARNING, ERROR, CRITICAL
```

> **Important**: For multi-server deployments where the bot can be invited to others' servers, do NOT specify the Tailscale API key in environment variables. Instead, each server admin should use the `!setup` command to configure their own Tailscale API key.

For the Shell Script:
```
API_KEY=your_tailscale_api_key
DISCORD_WEBHOOK=your_discord_webhook_url
```

### Environment Variables vs. Discord Bot Commands

It's important to understand how environment variables and Discord bot commands work together:

1. **Initial Configuration**: Environment variables provide only the Discord bot token when the bot starts up.

2. **Per-Server Configuration**: The `!setup` command in Discord configures the Tailscale monitoring for each server individually:

   ```
   !setup <api_key> [poll_interval] [device1,device2,...]
   ```

   This command:
   - Validates the provided Tailscale API key
   - Sets the Discord channel for notifications
   - Configures which devices to monitor
   - Persists settings between restarts

3. **Security Model**:
   - Each server admin provides their own Tailscale API key
   - No Tailscale API keys are shared between servers
   - The bot's developer never needs access to any Tailscale API keys

### Using Environment Files

1. For local development, copy `.env.server` to `.env`:
   ```bash
   cp .env.server .env
   ```

2. Edit the `.env` file with your actual API keys and tokens:
   ```bash
   nano .env
   ```

3. The Docker Compose configuration will automatically use variables from `.env`

## Security Note

Protect your API keys and tokens. For local development, use an `.env` file (which is git-ignored). For production, use secure environment variables or Docker secrets.
