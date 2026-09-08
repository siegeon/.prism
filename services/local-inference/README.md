# Local inference tuning

The AOS AppHost (`~/projects/aos/apphost.cs`) owns the `inference` resource.
It serves Qwen3-30B-A3B Q4_K_M using llama.cpp at port 8086, with the
OpenAI-compatible API under `/v1` and model alias `prism-local`.
Existing PRISM Claude jobs are not automatically redirected to this endpoint.

The model lives in `temp/local-inference/models` (ignored by git).
The container image is pinned in the AppHost. It maps `/dev/dxg` and
`/usr/lib/wsl` for this WSL2 machine; other Linux hosts should use their
configured NVIDIA container runtime instead.

Model provenance:

- Repository: `Qwen/Qwen3-30B-A3B-GGUF`
- Revision: `e4d4bafdfb96a411a163846265362aceb0b9c63a`
- File: `Qwen3-30B-A3B-Q4_K_M.gguf`
- SHA-256: `0d003f6662faee786ed5da3e31b29c978de5ae5d275c8794c606a7f3c01aa8f5`
- Runtime image: `ghcr.io/ggml-org/llama.cpp@sha256:a292d888ae5051503309aa080ecdc08239c7a77ef8a2112e6dcbaf18c249c1d7`

## Tune

From the PRISM repository:

```bash
python services/local-inference/tune.py hybrid-8 --benchmark
python services/local-inference/tune.py cpu-8 --benchmark
python services/local-inference/tune.py partial-8 --benchmark
```

Each command writes a profile, restarts only inference through Aspire, waits
for health, discovers its endpoint, and optionally records two measurements.
Profiles also exist with 12 generation threads. CPU profiles disable GPU
offload; hybrid profiles keep experts in RAM and offload attention; partial
profiles offload 16 layers. All use one slot, 12 batch threads, a 512-token
batch, and a 128-token microbatch.

The context length defaults to 40960, the length the model trains to. Pass
`--ctx <tokens>` to change it. RESULTS.md measures 8192, 32768 and 40960. The
longest context is the fastest of the three. It costs about 1.9 GiB of GPU
memory more than the shortest. A conductor step carries a plan, a diff and
file contents, so it needs the long context.

`tune.py` writes its profile to `~/projects/prism/temp/local-inference`,
because the AppHost bind-mounts that exact directory as `/profiles`. Set
`PRISM_INFERENCE_STATE` to point somewhere else. Do not make this path
relative to the script. A copy of the script that runs from a linked checkout
would then write the profile beside itself, and the container would restart on
the old arguments and report nothing.

`hybrid-8-resident` disables mmap to load experts directly into RAM.
`hybrid-36` keeps the first 36 layers' experts on CPU and offloads the rest.
The exact deployed resource is preserved in `apphost-snippet.cs.txt`.

Run `python services/local-inference/check.py` to check JSON incident extraction
and a complete streamed chat answer, including time to first content. Its
report is saved as `temp/local-inference/api-check.json`.

`temp/local-inference/active.args` contains one literal server argument per
line. The launcher reads it on every restart without shell evaluation. The
container is limited to 40 GiB RAM, no additional swap, and 16 CPU equivalents.
Do not edit profiles during someone else's inference request.

Measurements append to `temp/local-inference/benchmarks.jsonl`. The prompt,
128-token output length, seed, and cache policy are fixed. These microbenchmarks
compare throughput, not model quality or full agent performance. Validate real
tasks and longer contexts before increasing concurrency or replacing a provider.

The selected default is `hybrid-8-resident`; see `RESULTS.md` for the measured
comparison. Restore it after an experiment with:

```bash
python services/local-inference/tune.py hybrid-8-resident
```

AppHost edits require the existing `aos.service` supervisor to reload. Do not
launch a second AppHost with `aspire start` while the supervisor owns it.

## Restore the model

```bash
hf download Qwen/Qwen3-30B-A3B-GGUF Qwen3-30B-A3B-Q4_K_M.gguf \
  --revision e4d4bafdfb96a411a163846265362aceb0b9c63a \
  --local-dir temp/local-inference/models
python services/local-inference/tune.py hybrid-8-resident --prepare-only
```

The prepare-only command creates the initial profile before the resource starts.
