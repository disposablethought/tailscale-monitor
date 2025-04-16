# Tailscale Device Monitor

This script monitors your Tailscale devices and sends notifications to a Discord webhook when devices haven't been seen for more than 6 minutes.

## Features

- Checks all devices in your Tailscale tailnet
- Sends notifications only once per offline event
- Sends recovery notifications when devices come back online
- Maintains state between runs to avoid duplicate notifications

## Setup

1. Make sure the script is executable:
   ```bash
   chmod +x tailscale_monitor.sh
   ```

2. Configure your crontab to run the script every 5 minutes:
   ```bash
   crontab -e
   ```

3. Add this line to your crontab (adjust the path as needed):
   ```
   */5 * * * * /Users/matholm/CascadeProjects/tailscale-monitor/tailscale_monitor.sh
   ```

## Configuration

The script has the following configurable values at the top:

- `API_KEY`: Your Tailscale API key
- `DISCORD_WEBHOOK`: Your Discord webhook URL
- `THRESHOLD_MINUTES`: Time threshold in minutes (default: 6)
- `STATE_FILE`: Path to store notification state (default: same directory as script)

## Security Note

The script contains your Tailscale API key and Discord webhook URL. Ensure the script file has appropriate permissions to prevent unauthorized access.

## Troubleshooting

- Ensure `curl` and `jq` are installed on your system
- Check that the API key has sufficient permissions to list devices
- Verify the Discord webhook URL is correct and active
