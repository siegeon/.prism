#!/bin/sh
set -eu
# One literal argument per line; no shell evaluation of tuning profiles.
set --
while IFS= read -r arg || [ -n "$arg" ]; do
    [ -z "$arg" ] || set -- "$@" "$arg"
done < /profiles/active.args
exec /app/llama-server -m /models/Qwen3-30B-A3B-Q4_K_M.gguf \
    --host 0.0.0.0 --port 8080 --alias prism-local \
    --parallel 1 --metrics --jinja --cors-origins localhost "$@"
