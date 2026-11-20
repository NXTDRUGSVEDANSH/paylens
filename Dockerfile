FROM python:3.12-slim

WORKDIR /app

# Install build essentials for native extensions (duckdb, chromadb)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all project files
COPY . .

# Generate deterministic dataset at build time
RUN python data/generate.py

ENV PORT=8000
EXPOSE 8000

# Start server with dynamic port support for cloud hosts
CMD ["sh", "-c", "uvicorn backend.api.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
