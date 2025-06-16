#!/bin/bash

cd "$(dirname "$0")/../demo/ui" || exit

source .venv/bin/activate

echo "Running Host Agent UI..."
uv run main.py

# uv run main.py --port 12000 