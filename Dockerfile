FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source
COPY . .

# Make the entrypoint script executable
RUN chmod +x /app/docker-entrypoint.sh

# Set up entrypoint
ENTRYPOINT ["/app/docker-entrypoint.sh"]

# Default configuration directory
ENV CONFIG_DIR=/app/data

# Run the Discord bot as a continuous service
CMD ["python", "bot.py"]
