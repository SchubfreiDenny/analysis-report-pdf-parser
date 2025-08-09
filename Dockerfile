# Use official Python runtime
FROM python:3.11-slim

# Set working directory
WORKDIR /app

# Copy requirements and install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY main.py .
COPY parser.py .

# Run the application
CMD exec gunicorn --bind :$PORT --workers 1 --threads 8 --timeout 0 main:app