FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Install system dependencies and curl for health check
RUN apt-get update && apt-get install -y \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for faster Python package management
RUN pip install uv

# Copy dependency files
COPY pyproject.toml uv.lock* ./

# Install Python dependencies using uv (without building the project yet)
# --no-frozen so the build works before uv.lock is committed on first build
RUN uv sync --no-dev --no-install-project || uv pip install -e .

# Copy application source code
COPY src/ ./src/

# Copy seed data (Best Western hotels with coordinates, geocoded from
# scraped addresses — bypasses OSM gaps in Austria/Switzerland)
COPY helm/seed/ ./seed/

# Now install the project in editable mode
RUN uv pip install --no-deps -e .

# Create non-root user for security
RUN useradd --create-home --shell /bin/bash app && \
    mkdir -p /app/data/poi && \
    chown -R app:app /app
USER app

# Expose the application port
EXPOSE 8000

# Health check disabled - MCP protocol requires specific headers
# HEALTHCHECK --interval=30s --timeout=30s --start-period=5s --retries=3 \
#     CMD curl -f http://localhost:8000/mcp || exit 1

# Set environment variables for production
ENV PYTHONPATH=/app
ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1

# Command to run the application (MCP server with HTTP transport)
CMD ["uv", "run", "python", "-m", "src.server"]
