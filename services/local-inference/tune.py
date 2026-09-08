"""Apply repeatable llama.cpp profiles and measure the Aspire-managed server."""
import argparse
import json
from pathlib import Path
import subprocess
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
STATE = ROOT / "temp/local-inference"
APPHOST = Path.home() / "projects/aos/apphost.cs"
PROFILES = {
    "cpu-8": ["-ngl", "0", "--device", "none", "--no-op-offload", "-t", "8"],
    "cpu-12": ["-ngl", "0", "--device", "none", "--no-op-offload", "-t", "12"],
    "hybrid-8": ["-ngl", "99", "--cpu-moe", "-t", "8"],
    "hybrid-12": ["-ngl", "99", "--cpu-moe", "-t", "12"],
    "hybrid-8-resident": ["-ngl", "99", "--cpu-moe", "-t", "8", "--load-mode", "none"],
    "hybrid-36": ["-ngl", "99", "--n-cpu-moe", "36", "-t", "8"],
    "partial-8": ["-ngl", "16", "-t", "8"],
    "partial-12": ["-ngl", "16", "-t", "12"],
}

def aspire(*args):
    return subprocess.run(["aspire", *args, "--apphost", str(APPHOST),
                           "--non-interactive"], check=True, text=True,
                          capture_output=True, timeout=180).stdout

def endpoint():
    data = json.loads(aspire("describe", "inference", "--format", "Json"))
    resources = data if isinstance(data, list) else data.get("resources", [data])
    return next(u["url"] for r in resources for u in r.get("urls", [])
                if u["name"] == "http")

def apply(name):
    STATE.mkdir(parents=True, exist_ok=True)
    args = PROFILES[name] + ["-tb", "12", "-c", "8192", "-b", "512", "-ub", "128"]
    tmp = STATE / "active.args.tmp"
    tmp.write_text("\n".join(args) + "\n")
    tmp.replace(STATE / "active.args")
    (STATE / "profile.json").write_text(json.dumps({"profile": name, "args": args}, indent=2))

def request(url, path, body):
    req = urllib.request.Request(url + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as response:
        return json.load(response)

def measure(name, url):
    # Fixed prompt and generation length; cache disabled for comparable prefills.
    prompt = "Explain how a durable job queue handles retries and duplicate deliveries. " * 32
    rows = []
    profile = json.loads((STATE / "profile.json").read_text())
    for repetition in range(2):
        start = time.monotonic()
        result = request(url, "/completion", {
            "prompt": prompt, "n_predict": 128, "temperature": 0,
            "seed": 42, "ignore_eos": True, "cache_prompt": False,
        })
        row = {"profile": profile["profile"], "args": profile["args"], "repetition": repetition,
               "wall_seconds": round(time.monotonic() - start, 3),
               "timings": result.get("timings"), "timestamp": time.time()}
        gpu = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.free,power.draw",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True)
        row["gpu_snapshot_mib_watts"] = gpu.stdout.strip() if gpu.returncode == 0 else None
        print(json.dumps(row), flush=True)
        with (STATE / "benchmarks.jsonl").open("a") as out:
            out.write(json.dumps(row) + "\n")
        rows.append(row)
    return rows

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=[*PROFILES, "current"])
    parser.add_argument("--prepare-only", action="store_true")
    parser.add_argument("--benchmark", action="store_true")
    options = parser.parse_args()
    if options.profile != "current":
        apply(options.profile)
        if options.prepare_only:
            return
        print(aspire("resource", "inference", "restart"), flush=True)
    print(aspire("wait", "inference", "--timeout", "120"), flush=True)
    url = endpoint()
    print("Inference endpoint: " + url, flush=True)
    if options.benchmark:
        measure(options.profile, url)

if __name__ == "__main__":
    main()
