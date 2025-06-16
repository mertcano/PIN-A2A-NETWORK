#!/bin/bash

cd "$(dirname "$0")/../samples/python" || exit

source .venv/bin/activate

echo "Running Food Ordering Service Agent..."
uv run agents/food_ordering_services --host 0.0.0.0 --port 10003

# uv run agents/food_ordering_services --port 10002 