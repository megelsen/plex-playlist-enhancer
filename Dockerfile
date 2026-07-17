FROM python:3.11-slim

WORKDIR /app

# Install required Python extensions
RUN pip install --no-cache-dir streamlit plexapi

# Copy our app script into the container image
COPY app.py .

# Streamlit interface port mapping exposure
EXPOSE 8502

CMD ["streamlit", "run", "app.py", "--server.port=8502", "--server.address=0.0.0.0"]
