#!/bin/sh
set -eu
# Sibling to launch.sh, for a SECOND llama.cpp server dedicated to
# embeddings. llama.cpp cannot serve chat and embeddings from the same
# process (a request against a chat-only server returns 501 "This server
# does not support embeddings. Start it with --embeddings" - confirmed
# live against the existing 'inference' resource, task 23b344d2).
#
# Model: bge-small-en-v1.5, quantized, 384-dimension output, ~37 MB.
#   hf download ggml-org/bge-small-en-v1.5-Q8_0-GGUF \
#     bge-small-en-v1.5-q8_0.gguf \
#     --local-dir temp/local-inference/models
#
# This server takes no --parallel/--metrics/--jinja flags: it never runs
# chat templates or concurrent generation, only single-shot embedding
# requests, so the chat engine's launch.sh flags do not apply here.
exec /app/llama-server -m /models/bge-small-en-v1.5-q8_0.gguf \
    --host 0.0.0.0 --port 8080 --alias prism-embeddings \
    --embeddings --pooling mean --ubatch-size 512 \
    --cors-origins localhost
