FROM python:3.12-slim

WORKDIR /app

# Install uv for fast dependency resolution
RUN pip install --no-cache-dir uv

# Copy project files
COPY pyproject.toml .
COPY src/ src/

# Install dependencies
RUN uv pip install --system -e .

# Expose the default port
EXPOSE 8410

# Run the daemon. It listens on all interfaces, so it refuses to start without
# authentication: set AMPLIFIERD_API_KEY (or AMPLIFIERD_AUTH_ENABLED=true).
CMD ["amplifierd", "serve", "--host", "0.0.0.0", "--port", "8410"]
