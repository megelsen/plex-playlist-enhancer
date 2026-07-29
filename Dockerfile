FROM python:3.11-slim

WORKDIR /app

# Install required Python extensions
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the full app package (entrypoint, modules, and tabs/ subfolder)
COPY app.py styles.py plex_helpers.py ui_components.py recommendations.py artist_mix.py clustering.py galaxy.py ./
COPY tabs/ ./tabs/

# /app/data holds the disk-cached genre/mood -> cluster mapping (see
# CLUSTER_CACHE_PATH in clustering.py) so it survives container restarts.
# Mount this as a volume in docker-compose.yml, e.g.:
#   volumes:
#     - ./data:/app/data
RUN mkdir -p /app/data

# Streamlit interface port mapping exposure
EXPOSE 8502

CMD ["streamlit", "run", "app.py", "--server.port=8502", "--server.address=0.0.0.0"]
