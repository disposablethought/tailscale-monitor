FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the application source
COPY . .

# Run the Discord bot as a continuous service
CMD ["python", "bot.py"]
