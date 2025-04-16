#!/bin/bash

# Tailscale Device Monitor
# This script checks Tailscale devices and sends Discord notifications
# if any device hasn't been seen for over 6 minutes

# Configuration
API_KEY=
DISCORD_WEBHOOK=
THRESHOLD_MINUTES=6
STATE_FILE="$(dirname "$0")/last_notifications.json"

# Create state file if it doesn't exist
if [ ! -f "$STATE_FILE" ]; then
  echo "{}" > "$STATE_FILE"
fi

# Get current timestamp in UTC
current_time=$(date -u +"%Y-%m-%dT%H:%M:%SZ")

# Convert to epoch seconds for easier comparison
current_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$current_time" +%s)

# Fetch devices from Tailscale API
echo "Fetching Tailscale devices..."
response=$(curl -s -u "$API_KEY:" "https://api.tailscale.com/api/v2/tailnet/-/devices")

# Check if curl command succeeded
if [ $? -ne 0 ]; then
  message="Error: Failed to fetch Tailscale devices"
  echo "$message"
  curl -s -H "Content-Type: application/json" -d "{\"content\":\"🚨 $message\"}" "$DISCORD_WEBHOOK"
  exit 1
fi

# Process devices and check last seen times
echo "Processing device status..."

# Load previous notification state
previous_notifications=$(cat "$STATE_FILE")

# Initialize new notifications state
new_notifications="$previous_notifications"

# Use process substitution to avoid subshell issues with variable persistence
while IFS='|' read -r name last_seen; do
  # Convert lastSeen to epoch seconds
  last_seen_epoch=$(date -j -f "%Y-%m-%dT%H:%M:%SZ" "$last_seen" +%s)
  
  # Calculate minutes since last seen
  minutes_since=$((($current_epoch - $last_seen_epoch) / 60))
  
  # Check if device hasn't been seen for more than threshold
  if [ "$minutes_since" -gt "$THRESHOLD_MINUTES" ]; then
    # Format a readable time
    readable_time=$(echo "$last_seen" | sed 's/T/ /g' | sed 's/Z//g')
    
    # Check if we already notified about this device
    already_notified=$(echo "$previous_notifications" | jq -r --arg device "$name" '.[$device] // "false"')
    
    if [ "$already_notified" = "false" ]; then
      message="🔴 Device '$name' has not been seen for $minutes_since minutes. Last seen: $readable_time"
      echo "$message"
      
      # Send Discord notification
      curl -s -H "Content-Type: application/json" \
           -d "{\"content\":\"$message\"}" \
           "$DISCORD_WEBHOOK"
      
      # Update notifications state (mark as notified)
      new_notifications=$(echo "$new_notifications" | jq --arg device "$name" --arg value "true" '. + {($device): $value}')
    else
      echo "Device '$name' still offline ($minutes_since minutes). Already notified."
      # Keep the notification status
      new_notifications=$(echo "$new_notifications" | jq --arg device "$name" --arg value "true" '. + {($device): $value}')
    fi
  else
    # Check if this device was previously offline but now back online
    previously_notified=$(echo "$previous_notifications" | jq -r --arg device "$name" '.[$device] // "false"')
    
    if [ "$previously_notified" = "true" ]; then
      # Device is back online, send recovery notification
      message="🟢 Device '$name' is back online! Last seen: $(echo "$last_seen" | sed 's/T/ /g' | sed 's/Z//g')"
      echo "$message"
      
      # Send Discord notification
      curl -s -H "Content-Type: application/json" \
           -d "{\"content\":\"$message\"}" \
           "$DISCORD_WEBHOOK"
    else
      echo "Device '$name' is online ($minutes_since minutes since last seen)"
    fi
    
    # Reset notification state for this device (mark as not notified)
    new_notifications=$(echo "$new_notifications" | jq --arg device "$name" --arg value "false" '. + {($device): $value}')
  fi
done < <(echo "$response" | jq -r '.devices[] | "\(.name)|\(.lastSeen)"')

# Save the new notification state
echo "$new_notifications" > "$STATE_FILE"

echo "Monitoring complete."
