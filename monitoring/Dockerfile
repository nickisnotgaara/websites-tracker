# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Europe/Moscow

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    && rm -rf /var/lib/apt/lists/*

# Copy pyproject.toml first to leverage Docker cache
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Install the application and dependencies
# We install in editable mode or just install the package
RUN pip install --no-cache-dir .

# Copy the rest of the application
COPY main.py .
COPY credentials.json* ./

# Create directories for data and logs
RUN mkdir -p data logs

# Run the application
CMD ["python", "main.py"]
