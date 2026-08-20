#!/usr/bin/env bash
set -euo pipefail

# Compile the exact React bundle served by the PRISM Python service.
cd "$(dirname "$0")/../../services/prism-service/prism_service/web"
exec npm run build
