#!/bin/bash

echo "Starting Kindle MCP Server..."

# Check if mcp-shared network exists, create if not
if ! docker network ls | grep -q "mcp-shared"; then
    echo "Creating mcp-shared network..."
    docker network create mcp-shared
fi

# Stop and remove existing containers
echo "Stopping existing containers..."
docker compose down

# Remove existing images to force rebuild
echo "Removing existing images..."
docker rmi -f mcp-kindle-mcp-kindle 2>/dev/null || true

# Build and start services
echo "Building and starting services..."
docker compose up -d --build --remove-orphans

echo "Kindle MCP Server is running!"
echo "MCP Endpoint: http://localhost:8018/kindle/"
echo "Health Check: http://localhost:8018/health"
