#!/usr/bin/env python3
import os
import json
import sys

# Get configuration directory from environment or use default
CONFIG_DIR = os.environ.get("CONFIG_DIR", "data")
print(f"Using config directory: {CONFIG_DIR}")

# Check if directory exists
if not os.path.exists(CONFIG_DIR):
    print(f"Creating config directory: {CONFIG_DIR}")
    os.makedirs(CONFIG_DIR, exist_ok=True)

# Construct paths to config files
SERVER_CONFIG = os.path.join(CONFIG_DIR, "server_config.json")
STATE_FILE = os.path.join(CONFIG_DIR, "notification_state.json")

# Default empty configs
default_server_config = {}
default_notification_state = {}

# Check if server_config.json exists
if os.path.exists(SERVER_CONFIG):
    try:
        with open(SERVER_CONFIG, "r") as f:
            server_config = json.load(f)
        print(f"Found existing server_config.json with {len(server_config)} servers")
        for guild_id, config in server_config.items():
            print(f"  Server {guild_id}: {', '.join(config.keys())}")
    except Exception as e:
        print(f"Error reading server_config.json: {e}")
        server_config = default_server_config
else:
    print(f"server_config.json not found at {SERVER_CONFIG}")
    server_config = default_server_config

# Check if notification_state.json exists
if os.path.exists(STATE_FILE):
    try:
        with open(STATE_FILE, "r") as f:
            notification_state = json.load(f)
        print(f"Found existing notification_state.json with {len(notification_state)} items")
    except Exception as e:
        print(f"Error reading notification_state.json: {e}")
        notification_state = default_notification_state
else:
    print(f"notification_state.json not found at {STATE_FILE}")
    notification_state = default_notification_state

# If asked to initialize
if len(sys.argv) > 1 and sys.argv[1] == "--init" and not os.path.exists(SERVER_CONFIG):
    print("Creating initial server_config.json")
    with open(SERVER_CONFIG, "w") as f:
        json.dump(default_server_config, f)
    print("Creating initial notification_state.json")
    with open(STATE_FILE, "w") as f:
        json.dump(default_notification_state, f)
