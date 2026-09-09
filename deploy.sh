#!/bin/bash
set -e

cd /opt/apps/moneda/calculator

echo "=== Starting Moneda Calculator deployment ==="

echo "=== Pulling latest code ==="
git fetch origin main
git reset --hard origin/main

echo "=== Rebuilding Docker images ==="
docker compose build

echo "=== Restarting application ==="
docker compose up -d

echo "=== Checking containers ==="
docker compose ps

echo "=== Deployment completed ==="

