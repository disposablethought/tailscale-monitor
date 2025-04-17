#!/bin/bash
set -e

# Ensure data directory exists
mkdir -p ${CONFIG_DIR:-/app/data}

# Initialize config files if they don't exist
if [ ! -f "${CONFIG_DIR:-/app/data}/server_config.json" ]; then
    echo "{}" > "${CONFIG_DIR:-/app/data}/server_config.json"
    echo "Created initial server_config.json"
fi

if [ ! -f "${CONFIG_DIR:-/app/data}/notification_state.json" ]; then
    echo "{}" > "${CONFIG_DIR:-/app/data}/notification_state.json"
    echo "Created initial notification_state.json"
fi

# Execute the main command
exec "$@"
