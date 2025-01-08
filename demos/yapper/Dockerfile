# Use Python 3.10+ (or whichever version you prefer)
FROM python:3.10-slim

WORKDIR /app

# Copy requirements
COPY requirements.txt .

# Install dependencies
RUN pip install --no-cache-dir -r requirements.txt

# If you plan to run with SSL, also copy your cert/key here (or mount them):
COPY cert.pem /app/
COPY key.pem /app/

# Copy the server script
COPY server.py .

# Expose the default port
EXPOSE 8765

# By default, let's not use SSL. To enable SSL, set environment variables in your run command or Docker Compose.
ENV USE_SSL=0
ENV PORT=8765

CMD ["python", "server.py"]
