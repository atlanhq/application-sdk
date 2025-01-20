# Use the official Python 3.11 image from the Docker Hub
FROM python:3.11-slim

# Install system dependencies and build tools
RUN apt-get update && apt-get install -y \
    gcc \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory in the container
WORKDIR /application_sdk

# Copy the application_sdk directory contents into the container at /application_sdk
COPY ./application_sdk .

# Copy the pyproject.toml and poetry.lock files into the container at /application_sdk
COPY ./pyproject.toml .

# Install Poetry
RUN pip install poetry==1.8.3

# Install any needed packages specified in requirements.txt
RUN poetry install --no-interaction --no-root --extras "workflows dashboard"