FROM python:3.9-slim

# --- 1) Create /app and install system dependencies ---
WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# --- 2) Create a non-root user and log directory ---
RUN useradd -m -u 1000 appuser && \
    mkdir -p /home/appuser/relay_logs && \
    chown -R appuser:appuser /home/appuser/relay_logs

# --- 3) Copy and install Python dependencies (including gunicorn) ---
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --- 4) Copy the application source (including src/ folder) ---
COPY --chown=appuser:appuser . .

# --- 5) Switch to non-root user ---
USER appuser
ENV HOME /home/appuser

# --- 6) Change working directory to src/ before starting ---
WORKDIR /app/src

# --- 7) Launch with Gunicorn using main:app ---

CMD ["gunicorn", "--bind", "0.0.0.0:5000", "--workers", "1", "--threads", "4", "--timeout", "60", "main:app"]
