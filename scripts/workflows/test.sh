#!/usr/bin/env bash
set -euo pipefail

# Project validation: exercise PRISM's Python behavior from its service root.
cd "$(dirname "$0")/../../services/prism-service"
exec uv run pytest -q
