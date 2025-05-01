# Use an official Python runtime as a parent image
FROM python:3.11-slim as builder

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE 1
ENV PYTHONUNBUFFERED 1

# Set work directory
WORKDIR /app

# Install Poetry
RUN pip install poetry==1.8.0

# Copy only requirements to cache them
COPY pyproject.toml poetry.lock* ./

# Install dependencies
# --no-root: Don't install the project itself, only dependencies
# --no-dev: Exclude development dependencies
RUN poetry install --no-root --no-dev --no-interaction --no-ansi

# --- Final Stage ---
FROM python:3.11-slim

WORKDIR /app

# Copy installed dependencies from builder stage
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /app/.venv/bin /usr/local/bin

# Copy application code
COPY src/ /app/src/

# Create a non-root user
RUN useradd --create-home appuser
USER appuser

# Expose port 8000
EXPOSE 8000

# Command to run the application
CMD ["uvicorn", "llm_gateway.main:app", "--host", "0.0.0.0", "--port", "8000"] 