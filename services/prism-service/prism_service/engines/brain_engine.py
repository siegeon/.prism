#!/usr/bin/env python3
"""Brain Engine: 3-index hybrid search for PRISM.

Provides:
- FTS5 BM25 keyword search (always available, stdlib only)
- sqlite-vec vector search via model2vec embeddings (optional)
- GraphRAG entity/relationship search (always available)
- RRF fusion across all three indexes

Gracefully degrades to BM25+GraphRAG or BM25-only when optional
deps are unavailable.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import threading

# Embedder downloads pull HuggingFace files behind a tqdm progress bar.
# Under concurrent Brain init (drift timer + MCP tools + lifespan) tqdm's
# class-level state races and one thread fails the load with
# `'tqdm' object has no attribute '_lock'`. Server processes never show
# the bars to anyone — silence them before model2vec/sentence-transformers
# pulls tqdm in.
os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
os.environ.setdefault("TQDM_DISABLE", "1")
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# task f9e0745e: ONE skip-dir list for every indexer. brain_engine used to
# keep its own private copy (_EXCLUDED_PATH_SEGMENTS) that fell out of sync
# with source_service._INGEST_SKIP_DIRS and let web_dist/web_dist_next
# bundle files pass _should_index() straight into the graph.
from prism_service.services.source_service import _INGEST_SKIP_DIRS

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class BrainCorruptError(Exception):
    """Raised when a Brain database file fails SQLite integrity check."""


# Split PascalCase/camelCase boundaries. Registered as a SQLite function
# ("expand_identifiers") on every brain.db connection so FTS5 triggers can
# call it to index identifier-split tokens while docs.content stays raw.
_CAMEL_RE = re.compile(r'(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])')


def _expand_identifiers(text: str) -> str:
    """Expand PascalCase/camelCase identifiers for better FTS matching.

    'FreshnessStatus' -> 'FreshnessStatus Freshness Status'
    'getMatchesHandler' -> 'getMatchesHandler get Matches Handler'

    Keeps original term + adds split parts so both exact and partial
    matches work. Used by (a) the FTS5 insert/delete/update triggers to
    expand docs.content before writing to docs_fts, and (b) query-side
    expansion in BrainService.search so query tokens match the expanded
    index.
    """
    if not text:
        return text
    out = []
    for word in text.split():
        out.append(word)
        if len(word) > 2 and _CAMEL_RE.search(word):
            parts = _CAMEL_RE.sub(' ', word).split()
            if len(parts) > 1:
                out.extend(parts)
    return ' '.join(out)


def encode_task_text(text: str) -> Optional[bytes]:
    """Encode arbitrary text via the loaded MiniLM embedder and return
    packed float32 bytes suitable for storing in a SQLite BLOB column.

    Reused by TaskService (LL-03) so the learning loop's task-similarity
    retrieval lives on the same vectors Brain uses for document search.
    Returns ``None`` when no embedder is loaded — callers must handle
    the offline case gracefully. First 2048 chars only (model ctx cap).
    """
    global _MODEL
    if _MODEL is None:
        return None
    try:
        import numpy as _np
        # Single-flight: serialize the native encode (GH #157). Fast-path
        # `_MODEL is None` check above stays OUTSIDE the lock.
        with _ENCODE_LOCK, _threadpool_limit_1():
            vec = _MODEL.encode([text[:2048]])[0]
            arr = _np.asarray(vec, dtype=_np.float32)
        return arr.tobytes()
    except Exception:
        return None


def decode_task_embedding(blob: Optional[bytes]) -> Optional[list[float]]:
    """Reverse of :func:`encode_task_text` — packed bytes → list[float]."""
    if not blob:
        return None
    try:
        import numpy as _np
        return _np.frombuffer(blob, dtype=_np.float32).tolist()
    except Exception:
        return None


def _similar_task_ids(
    tasks_conn: sqlite3.Connection,
    query_task_id: str,
    k: int = 20,
) -> list[tuple[str, float]]:
    """Return the top-k task_ids by cosine similarity to ``query_task_id``.

    Excludes the query task itself. Returns ``[(task_id, cosine), ...]``
    sorted by cosine descending. Tasks without an embedding (or with a
    mismatched-dim embedding) are skipped.
    """
    import math
    row = tasks_conn.execute(
        "SELECT embedding FROM tasks WHERE id=?", (query_task_id,)
    ).fetchone()
    if row is None:
        return []
    q_blob = row[0] if not hasattr(row, "keys") else row["embedding"]
    q_vec = decode_task_embedding(q_blob)
    if not q_vec:
        return []
    q_norm = math.sqrt(sum(x * x for x in q_vec)) or 1.0

    out: list[tuple[str, float]] = []
    for r in tasks_conn.execute(
        "SELECT id, embedding FROM tasks "
        "WHERE id != ? AND embedding IS NOT NULL",
        (query_task_id,),
    ).fetchall():
        tid = r[0] if not hasattr(r, "keys") else r["id"]
        blob = r[1] if not hasattr(r, "keys") else r["embedding"]
        v = decode_task_embedding(blob)
        if not v or len(v) != len(q_vec):
            continue
        dot = sum(a * b for a, b in zip(q_vec, v))
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        sim = dot / (q_norm * n)
        out.append((tid, sim))
    out.sort(key=lambda t: t[1], reverse=True)
    return out[:k]


# ---------------------------------------------------------------------------
# Optional dependency detection
# ---------------------------------------------------------------------------
_MODEL = None
# HF id of the loaded embedder (e.g. "minishlab/potion-retrieval-32M").
# Persisted into brain.db index_meta('embedder_model') so a model swap at
# the SAME dim is detected at startup (see _init_brain_schema self-heal).
_MODEL_ID: Optional[str] = None
_MODEL_LOCK = threading.Lock()
# Single-flight serialization of _MODEL.encode() native inference (GH #157).
# DISTINCT from _MODEL_LOCK (which guards only the model LOAD at :459): two
# request threads entering model2vec->numpy/BLAS native math at once invert
# the GIL<->native-futex and silently wedge every thread. Every encode site
# acquires this so only ONE encode runs at a time. No load-inside-encode or
# encode-inside-load nesting (the loader never encodes; encode never loads).
_ENCODE_LOCK = threading.Lock()
_SQLITE_VEC_LOADED = False


def _threadpool_limit_1():
    """Authoritative single-thread native-math context (GH #162).

    Returns threadpoolctl.threadpool_limits(limits=1), which pins BLAS AND
    OpenMP pools to 1 thread for the duration of the block. UNLIKE the
    process-wide thread_limits.apply_thread_limits() env pins (which only
    apply "if unset" and only before BLAS loads), this runtime pin OVERRIDES
    a pre-set OPENBLAS_NUM_THREADS=8 and is import-order-proof — it is the
    structural prevention for the GIL<->native-futex wedge. threadpoolctl is
    now a DECLARED runtime dependency (pyproject.toml), so the import is hard;
    a nullcontext fallback survives only a genuinely broken install."""
    try:
        import threadpoolctl  # declared runtime dep (pyproject.toml)
        return threadpoolctl.threadpool_limits(limits=1)
    except Exception:
        import contextlib
        return contextlib.nullcontext()


# Holds the persistent (never-restored) threadpool limiter so a pre-set
# OPENBLAS_NUM_THREADS=8 stays clamped to 1 for the WHOLE process lifetime,
# not just inside a `with _threadpool_limit_1()` block (GH #162 FR-1c). A
# scoped limiter restores the pool to 8 on __exit__; this one is kept alive
# at module scope so the override is permanent.
_PERSISTENT_PIN = None


def pin_native_threads_permanently() -> bool:
    """Clamp every BLAS/OpenMP pool to 1 thread for the rest of the process
    (GH #162 FR-1c). threadpoolctl.threadpool_limits used as a LIVE object
    (not a `with` block) applies immediately and only restores on __exit__/
    __del__ — so we stash it in a module global so it is NEVER restored. This
    OVERRIDES a hostile pre-set OPENBLAS_NUM_THREADS and is import-order-proof.
    Idempotent. Returns True when a pin was applied."""
    global _PERSISTENT_PIN
    if _PERSISTENT_PIN is not None:
        return True
    try:
        import threadpoolctl  # declared runtime dep (pyproject.toml)
        _PERSISTENT_PIN = threadpoolctl.threadpool_limits(limits=1)
        return True
    except Exception:
        return False


def threadpool_info() -> list[dict]:
    """Return threadpoolctl.threadpool_info() (BLAS/OpenMP pools + thread
    counts), or [] when threadpoolctl is unavailable. Used to PROVE pools
    are pinned to 1 in startup/post-load logs (GH #162 FR-3)."""
    try:
        import threadpoolctl
        return threadpoolctl.threadpool_info()
    except Exception:
        return []


def log_threadpool_info(where: str) -> None:
    """Log threadpool_info() so the log PROVES native pools are pinned (FR-3).

    Emitted at startup and after model load. Each pool's num_threads is
    surfaced so a uncapped pool (num_threads>1) is visible in prism.log."""
    # Distinguish "threadpoolctl genuinely missing" (pin would no-op — a real
    # problem) from "no native pools loaded yet" (normal at startup, before
    # numpy/OpenBLAS import). Conflating them logged a misleading
    # "unavailable" at startup that made the FR-3 proof look broken (GH #162).
    try:
        import threadpoolctl  # noqa: F401
    except Exception:
        print(f"Brain: threadpool_info [{where}]: threadpoolctl NOT INSTALLED "
              f"— native-thread pin is INACTIVE", file=sys.stderr)
        return
    info = threadpool_info()
    if not info:
        print(f"Brain: threadpool_info [{where}]: no native pools loaded yet",
              file=sys.stderr)
        return
    pools = ", ".join(
        f"{p.get('internal_api', p.get('user_api', '?'))}="
        f"{p.get('num_threads', '?')}"
        for p in info
    )
    print(f"Brain: threadpool_info [{where}]: {pools}", file=sys.stderr)

# Cross-encoder reranker (lazy-loaded on first use when PRISM_RERANK != off).
# Separate from the embedder so both can coexist in memory.
_RERANKER = None
_RERANKER_KEY = ""

_RERANKER_PRESETS = {
    # key -> sentence-transformers CrossEncoder model_id
    "bge-v2": "BAAI/bge-reranker-v2-m3",
    "jina-v2": "jinaai/jina-reranker-v2-base-multilingual",
    "ms-marco-minilm": "cross-encoder/ms-marco-MiniLM-L-6-v2",
}


_AUTO_RERANK_PRESET: Optional[str] = None

# What PRISM_RERANK=auto resolves to when the machine can actually run it.
# ms-marco-MiniLM-L-6-v2 and not bge-v2 because this is the one that was
# MEASURED (task 19e4e7f7) and it is ~6M params against bge-v2's 570M; the
# default has to be the configuration whose cost is known.
_AUTO_RERANK_MODEL = "ms-marco-minilm"


def _auto_rerank_preset() -> str:
    """Resolve PRISM_RERANK=auto once per process.

    WHY auto IS THE DEFAULT (task 19e4e7f7, owner doctrine mx-71dc57): a flag
    may exist so a user can turn something OFF, never so they have to turn
    something ON. PRISM_RERANK defaulted to "off", so even a user who had
    deliberately installed the [neural] extra got no reranking unless they
    also discovered an environment variable -- while the measurement says
    reranking is the single largest retrieval win available (r@5 +0.106 on
    PocketBase p=0.0075, +0.153 on FullStackHero p=0.0009).

    It resolves to "off" when sentence-transformers is absent, which is the
    DEFAULT install: torch brings a second OpenMP runtime, threadpoolctl's
    documented unrecoverable deadlock case (GH #162), so it stays an opt-in
    extra. Deciding here rather than inside _load_reranker keeps that case
    silent -- the alternative logs "not installed" on every single search.
    """
    global _AUTO_RERANK_PRESET
    if _AUTO_RERANK_PRESET is None:
        import importlib.util as _ilu
        _AUTO_RERANK_PRESET = (
            _AUTO_RERANK_MODEL
            if _ilu.find_spec("sentence_transformers") is not None else "off")
    return _AUTO_RERANK_PRESET


def _load_reranker(preset: str):
    """Return a cached CrossEncoder for ``preset``, or None on failure.

    Loading is lazy and cached process-wide. Unknown preset -> None.
    """
    global _RERANKER, _RERANKER_KEY
    preset = (preset or "").strip().lower()
    if preset in ("", "off", "none"):
        return None
    if preset not in _RERANKER_PRESETS:
        print(f"Brain: unknown PRISM_RERANK={preset!r}; disabling reranker",
              file=sys.stderr)
        return None
    if _RERANKER is not None and _RERANKER_KEY == preset:
        return _RERANKER
    # GH #162 — explicit availability gate with graceful degradation. The
    # CrossEncoder pulls torch (a 2nd OpenMP/libgomp runtime — threadpoolctl's
    # documented unrecoverable deadlock case). torch + sentence-transformers
    # are now an OPTIONAL extra ([neural]); when not installed we SKIP neural
    # rerank with a clear log so default search never loads the 2nd runtime.
    import importlib.util as _ilu
    if _ilu.find_spec("sentence_transformers") is None:
        print(f"Brain: PRISM_RERANK={preset} requested but sentence-transformers "
              "not installed (pip install 'prism-service[neural]'); skipping "
              "neural rerank", file=sys.stderr)
        return None
    try:
        from sentence_transformers import CrossEncoder  # type: ignore
        model_id = _RERANKER_PRESETS[preset]
        _RERANKER = CrossEncoder(model_id, trust_remote_code=True)
        _RERANKER_KEY = preset
        print(f"Brain: reranker = {preset} ({model_id})", file=sys.stderr)
        return _RERANKER
    except Exception as e:
        print(f"Brain: reranker load failed ({preset}: {e!r}); disabled",
              file=sys.stderr)
        return None

# ---------------------------------------------------------------------------
# Tree-sitter language loader
# ---------------------------------------------------------------------------
_TS_PARSER_CACHE: dict[str, object] = {}

# tree-sitter-language-pack (maintained successor of the unmaintained
# tree-sitter-languages) uses "csharp" where our _TS_LANG_MAP historically
# used the grammar's symbol name "c_sharp". Keep the internal name stable
# (chunk configs / extractors key off it) and alias at the pack boundary.
_TS_PACK_ALIASES: dict[str, str] = {"c_sharp": "csharp"}


def _ts_find_name(node, name_types):
    """Return the text of the first identifier-like child, or None."""
    try:
        for c in node.children:  # type: ignore[attr-defined]
            if c.type in name_types:
                return c.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
    except Exception:
        pass
    return None


def _get_treesitter_parser(lang_name: str) -> Optional[object]:
    """Return a cached tree_sitter.Parser for the given language, or None.

    Built from ``tree_sitter_language_pack.get_language`` (a genuine
    ``tree_sitter.Language``) + py-tree-sitter's own Parser. The pack's
    ``get_parser`` is deliberately NOT used: as of pack 1.12 it returns a
    Rust-binding parser whose Tree/Node surface (``root_node`` as a method,
    str-only ``parse``) is incompatible with the ``node.children`` /
    ``node.text`` / ``start_point`` API our extractors walk.
    """
    if lang_name in _TS_PARSER_CACHE:
        return _TS_PARSER_CACHE[lang_name]
    try:
        import tree_sitter
        from tree_sitter_language_pack import get_language
        lang = get_language(_TS_PACK_ALIASES.get(lang_name, lang_name))
        parser = tree_sitter.Parser(lang)
        _TS_PARSER_CACHE[lang_name] = parser
        return parser
    except Exception:
        return None


# Map file suffix -> tree-sitter language name (as in languages.so symbol)
# Per-language chunker config for _chunk_treesitter_lang. Keys are
# language names from _TS_LANG_MAP. Entries describe which AST node
# types are top-level declarations, which of those carry member
# bodies, what the body node type is, which member node types to
# emit as methods, and which wrapper/container nodes to transparently
# descend through (decorators, namespaces, export statements).
_LANG_CHUNK_CONFIG: dict[str, dict | str] = {
    "python": {
        "top": {"function_definition": "function",
                "class_definition": "class"},
        "decorated_wrapper": "decorated_definition",
        "class_types": {"class_definition"},
        "body_type": "block",
        "method": {"function_definition": "method"},
        "name_types": ("identifier",),
        "descend": set(),
    },
    "c_sharp": {
        "top": {"class_declaration": "class",
                "interface_declaration": "interface",
                "struct_declaration": "struct",
                "record_declaration": "record"},
        "decorated_wrapper": None,
        "class_types": {"class_declaration", "interface_declaration",
                        "struct_declaration", "record_declaration"},
        "body_type": "declaration_list",
        "method": {"method_declaration": "method",
                   "constructor_declaration": "constructor"},
        "name_types": ("identifier",),
        # namespace bodies also use declaration_list in C#; descend
        # into it so class_declaration inside a namespace is reached.
        # The walker only RECURSES into ``descend`` types — class
        # members are not reached this way because class_declaration
        # itself is not in ``descend``; its methods are emitted via
        # _chunk_ts_methods, which walks the class body explicitly.
        "descend": {"namespace_declaration", "declaration_list"},
    },
    "typescript": {
        "top": {"class_declaration": "class",
                "function_declaration": "function",
                "interface_declaration": "interface"},
        "decorated_wrapper": None,
        "class_types": {"class_declaration", "interface_declaration"},
        "body_type": "class_body",
        "method": {"method_definition": "method"},
        "name_types": ("identifier", "property_identifier",
                       "type_identifier"),
        "descend": {"export_statement"},
    },
    "javascript": {
        "top": {"class_declaration": "class",
                "function_declaration": "function"},
        "decorated_wrapper": None,
        "class_types": {"class_declaration"},
        "body_type": "class_body",
        "method": {"method_definition": "method"},
        "name_types": ("identifier", "property_identifier"),
        "descend": {"export_statement"},
    },
    "tsx": "typescript",      # alias resolved at lookup time
    "jsx": "javascript",
}


_TS_LANG_MAP: dict[str, str] = {
    ".py": "python",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".js": "javascript",
    ".jsx": "javascript",
    ".cs": "c_sharp",
}


# C# framework/DSL methods filtered from the call graph. Tree-sitter can't
# resolve symbol origin (that needs Roslyn), so we drop call edges whose
# target matches a name from ASP.NET Core DI/middleware, Minimal APIs, LINQ,
# EF Core, async plumbing, or common BCL overrides. Without this, fluent
# chains like `services.AddSingleton<Foo>().Configure<Opts>(...)` dominate
# every method's out-edges and the real first-party graph is unreadable.
# Trade-off: a first-party `Build()` or `Configure()` gets filtered too.
# Roslyn-backed extraction would use `ContainingAssembly` instead.
_CS_FRAMEWORK_CALLS: frozenset[str] = frozenset({
    # Hosting / DI builder
    "Build", "CreateBuilder", "CreateHostBuilder", "CreateDefaultBuilder",
    "Configure", "ConfigureServices", "ConfigureAppConfiguration",
    "ConfigureLogging", "ConfigureWebHostDefaults",
    # IServiceCollection registration
    "AddSingleton", "AddScoped", "AddTransient",
    "AddDbContext", "AddDbContextPool", "AddDbContextFactory",
    "AddIdentity", "AddIdentityCore",
    "AddAuthentication", "AddAuthorization",
    "AddCors", "AddHttpClient", "AddHttpContextAccessor",
    "AddControllers", "AddControllersWithViews",
    "AddRazorPages", "AddMvc", "AddMvcCore",
    "AddLogging", "AddMemoryCache", "AddDistributedMemoryCache",
    "AddSwaggerGen", "AddEndpointsApiExplorer", "AddApiVersioning",
    "AddHostedService", "AddOptions", "AddSignalR", "AddGrpc",
    "AddJwtBearer", "AddCookie", "AddOpenIdConnect", "AddGoogle",
    "AddSerilog", "AddNLog", "AddOpenTelemetry", "AddHealthChecks",
    # IApplicationBuilder / WebApplication middleware
    "UseRouting", "UseEndpoints", "UseStaticFiles", "UseHttpsRedirection",
    "UseAuthentication", "UseAuthorization", "UseCors", "UseMiddleware",
    "UseExceptionHandler", "UseDeveloperExceptionPage", "UseHsts",
    "UseSerilog", "UseNLog", "UseKestrel", "UseIIS", "UseIISIntegration",
    "UseSwagger", "UseSwaggerUI", "UseSpa", "UseSpaStaticFiles",
    "UseResponseCompression", "UseResponseCaching", "UseSession",
    # Minimal API / endpoint mapping
    "MapGet", "MapPost", "MapPut", "MapDelete", "MapPatch",
    "MapControllers", "MapControllerRoute", "MapRazorPages",
    "MapHub", "MapFallback", "MapFallbackToFile", "MapFallbackToPage",
    "MapHealthChecks", "MapGrpcService", "MapWhen", "MapGroup",
    "RequireAuthorization", "RequireCors", "RequireHost", "RequireRateLimiting",
    "WithName", "WithTags", "WithOpenApi", "WithMetadata", "WithSummary",
    "Produces", "ProducesProblem", "Accepts",
    # LINQ (IEnumerable / IQueryable)
    "Where", "Select", "SelectMany",
    "OrderBy", "OrderByDescending", "ThenBy", "ThenByDescending",
    "GroupBy", "GroupJoin", "Join", "Zip",
    "ToList", "ToArray", "ToDictionary", "ToHashSet", "ToLookup",
    "First", "FirstOrDefault", "Single", "SingleOrDefault",
    "Last", "LastOrDefault", "ElementAt", "ElementAtOrDefault",
    "Any", "All", "Count", "LongCount",
    "Sum", "Min", "Max", "Average", "Aggregate",
    "Distinct", "DistinctBy", "Skip", "SkipWhile", "Take", "TakeWhile",
    "Reverse", "Contains", "SequenceEqual",
    "Union", "Intersect", "Except", "Concat",
    "Cast", "OfType", "AsEnumerable", "AsQueryable",
    # EF Core
    "Include", "ThenInclude", "AsNoTracking", "AsTracking", "AsSplitQuery",
    "FindAsync", "SaveChanges", "SaveChangesAsync",
    "AddAsync", "AddRangeAsync", "UpdateRange", "RemoveRange",
    "FromSqlRaw", "FromSqlInterpolated", "ExecuteSqlRaw",
    "ExecuteUpdateAsync", "ExecuteDeleteAsync",
    # BCL overrides / delegate invocation / async plumbing
    "ToString", "GetHashCode", "Equals", "GetType",
    "Append", "AppendLine", "AppendFormat",
    "Invoke", "InvokeAsync", "DynamicInvoke",
    "ConfigureAwait", "GetAwaiter", "GetResult", "Wait",
    "WaitAsync", "AsTask", "AsValueTask",
})


_EMBEDDER_PRESETS = {
    # key -> (backend, model_id)
    # backend in {"model2vec", "sentence-transformers"}
    # Default swapped to potion-retrieval-32M (same 512-dim family, tuned for
    # retrieval). Existing stores need a reindex to re-embed with it; the
    # docs_vec dim self-heal keeps mixed-dim startup from erroring either way.
    # "potion-base" stays selectable via PRISM_EMBEDDER for rollback.
    "potion": ("model2vec", "minishlab/potion-retrieval-32M"),
    "potion-base": ("model2vec", "minishlab/potion-base-32M"),
    "minilm": ("sentence-transformers", "sentence-transformers/all-MiniLM-L6-v2"),
    "nomic-code": ("sentence-transformers", "nomic-ai/nomic-embed-code"),
    "bge-small": ("sentence-transformers", "BAAI/bge-small-en-v1.5"),
    "jina-code": ("sentence-transformers", "jinaai/jina-embeddings-v2-base-code"),
}


def _load_sentence_transformer(model_id: str):
    """Load a sentence-transformers model. Returns object with .encode([str]) API."""
    from sentence_transformers import SentenceTransformer  # type: ignore
    return SentenceTransformer(model_id)


def _load_model2vec(model_id: str):
    from model2vec import StaticModel  # type: ignore
    return StaticModel.from_pretrained(model_id)


def _local_snapshot_or_id(model_id: str) -> str:
    """Prefer the LOCAL HuggingFace cache: resolve ``model_id`` to its cached
    snapshot directory with ``local_files_only=True`` — zero network by
    contract. Loading from that path means the hub is never consulted, so a
    cold daemon start makes no external call and cannot stall on an
    unreachable huggingface.co (task b0138f17, "a cold PRISM answers without
    calling the internet" — the first request after a restart paid a ~5-11s
    hub round trip, or a full connect timeout offline). Only a genuinely
    uncached model falls back to the online repo id, once, after which the
    cache serves every later boot."""
    try:
        from huggingface_hub import snapshot_download  # type: ignore
        return snapshot_download(model_id, local_files_only=True)
    except Exception:
        return model_id


def warm_embedder() -> bool:
    """Load the embedding model into the process-wide cache OFF the request
    path — called from a boot thread (main.py lifespan, task b0138f17) so
    the FIRST brain/memory/task-create call after a restart never pays the
    model load. Same lock, same thread-pinning, same offline-first snapshot
    resolution as _try_enable_vector's load; a request that races the warm
    simply blocks on _MODEL_LOCK and finds _MODEL ready. Failure is benign:
    the lazy request-path load remains the fallback."""
    import os
    global _MODEL, _MODEL_ID
    preset = os.environ.get("PRISM_EMBEDDER", "potion").strip().lower()
    if preset not in _EMBEDDER_PRESETS:
        preset = "potion"
    backend, model_id = _EMBEDDER_PRESETS[preset]
    with _MODEL_LOCK:
        if _MODEL is not None:
            return True
        try:
            pin_native_threads_permanently()
            with _threadpool_limit_1():
                _src = _local_snapshot_or_id(model_id)
                if backend == "model2vec":
                    _MODEL = _load_model2vec(_src)
                elif backend == "sentence-transformers":
                    _MODEL = _load_sentence_transformer(_src)
            _MODEL_ID = model_id
            print(f"Brain: embedder warmed at boot = {preset} "
                  f"({backend}: {model_id})", file=sys.stderr)
            return True
        except Exception as e:
            print(f"Brain: boot embedder warm failed ({preset}: {e!r}); "
                  f"lazy load remains", file=sys.stderr)
            return False


def _try_enable_vector(db: sqlite3.Connection) -> bool:
    """Attempt to load sqlite-vec extension and an embedding model.

    The embedding model is chosen via env var PRISM_EMBEDDER (one of the keys
    in _EMBEDDER_PRESETS); defaults to 'potion'. Returns True on success.
    """
    import os
    global _MODEL, _MODEL_ID, _SQLITE_VEC_LOADED
    try:
        import sqlite_vec  # type: ignore
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        _SQLITE_VEC_LOADED = True
    except (ImportError, AttributeError, Exception):
        print("Brain: running in BM25+GraphRAG mode (sqlite-vec unavailable)",
              file=sys.stderr)
        return False

    preset = os.environ.get("PRISM_EMBEDDER", "potion").strip().lower()
    if preset not in _EMBEDDER_PRESETS:
        print(f"Brain: unknown PRISM_EMBEDDER={preset!r}; falling back to 'potion'",
              file=sys.stderr)
        preset = "potion"
    backend, model_id = _EMBEDDER_PRESETS[preset]

    # Serialize the load so two threads can't both decide _MODEL is None
    # and both fetch from HuggingFace at once — that's how tqdm's class
    # state races and one of them logs "embedder load failed".
    with _MODEL_LOCK:
        if _MODEL is not None:
            return True  # already loaded (same process reuse)
        try:
            # GH #162 — pin BLAS/OpenMP to 1 thread for the LOAD too (not
            # only the encode sites). Model init touches native math; an
            # unpinned load under _MODEL_LOCK is the same futex-wedge risk.
            # The PERSISTENT pin (never restored) OVERRIDES a pre-set
            # OPENBLAS_NUM_THREADS=8 for the rest of the process, so the
            # override survives after this block exits (FR-1c), while the
            # scoped `with` below double-pins the load itself.
            pin_native_threads_permanently()
            with _threadpool_limit_1():
                # Offline-first: a cached model loads from its local
                # snapshot path and never touches the hub (task b0138f17).
                _src = _local_snapshot_or_id(model_id)
                if backend == "model2vec":
                    _MODEL = _load_model2vec(_src)
                elif backend == "sentence-transformers":
                    _MODEL = _load_sentence_transformer(_src)
                # Log INSIDE the pin so the proof reflects the load context.
                log_threadpool_info("after-model-load")
            _MODEL_ID = model_id
            print(f"Brain: embedder = {preset} ({backend}: {model_id})",
                  file=sys.stderr)
            return True
        except Exception as e:
            print(f"Brain: embedder load failed ({preset}: {e!r}); BM25+GraphRAG only",
                  file=sys.stderr)
            return False


# ---------------------------------------------------------------------------
# Reciprocal Rank Fusion
# ---------------------------------------------------------------------------

def reciprocal_rank_fusion(
    result_lists: list[list[dict]], k: int = 60
) -> list[dict]:
    """Fuse multiple ranked result lists via Reciprocal Rank Fusion."""
    scores: dict[str, float] = {}
    doc_data: dict[str, dict] = {}

    for results in result_lists:
        for rank, item in enumerate(results, start=1):
            doc_id = item["doc_id"]
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
            if doc_id not in doc_data:
                doc_data[doc_id] = item

    # Deterministic tie-breaking: sort by score DESC then doc_id ASC
    # so equal-score results always appear in the same order.
    fused = sorted(scores.items(), key=lambda x: (-x[1], x[0]))
    return [
        {"doc_id": doc_id, "rrf_score": score, **doc_data[doc_id]}
        for doc_id, score in fused
    ]


# ---------------------------------------------------------------------------
# Structural query detection
# ---------------------------------------------------------------------------

_STRUCTURAL_PATTERNS = [
    (r"what\s+(?:calls|invokes|uses)\s+(\w+)", "called_by"),
    (r"(?:dependencies|deps)\s+of\s+(\w+)", "depends_on"),
    (r"what\s+(?:imports|requires)\s+(\w+)", "imported_by"),
    (r"(?:callers|consumers)\s+of\s+(\w+)", "called_by"),
    (r"(?:extends|subclasses|inherits)\s+(\w+)", "extends"),
]


def _detect_structural_query(query: str) -> tuple[Optional[str], Optional[str]]:
    """Return (entity_name, relation) if structural query, else (None, None)."""
    for pattern, relation in _STRUCTURAL_PATTERNS:
        m = re.search(pattern, query, re.IGNORECASE)
        if m:
            return m.group(1), relation
    return None, None


# ---------------------------------------------------------------------------
# Brain class
# ---------------------------------------------------------------------------

class Brain:
    """3-index hybrid knowledge store: FTS5 BM25 + sqlite-vec + GraphRAG."""

    # PSP weights per (persona, step_id)
    PSP_WEIGHTS: dict[tuple[str, str], dict[str, float]] = {
        ("qa", "write_failing_tests"): {
            "gate_passed": 0.3,
            "traceability_pct": 0.3,
            "first_attempt": 0.2,
            "token_efficiency": 0.2,
        },
        ("dev", "implement_tasks"): {
            "gate_passed": 0.4,
            "coverage_pct": 0.3,
            "retry_rate": 0.15,
            "token_efficiency": 0.15,
        },
        ("sm", "draft_story"): {
            "probe_accuracy": 0.4,
            "story_completeness": 0.3,
            "token_efficiency": 0.3,
        },
    }
    DEFAULT_WEIGHTS: dict[str, float] = {
        "gate_passed": 0.4,
        "token_efficiency": 0.3,
        "retry_rate": 0.3,
    }

    _INDEXABLE_SUFFIXES = {
        ".py", ".ts", ".tsx", ".js", ".jsx",
        ".md", ".yaml", ".yml", ".json", ".txt", ".sh",
    }

    # task f9e0745e: this used to be a PRIVATE copy of the skip-dir list,
    # missing web_dist/web_dist_next/worktrees/.venvs -- so a file the
    # walker in source_service.py correctly skipped could still pass
    # _should_index() here and get indexed anyway. One list, one owner:
    # source_service._INGEST_SKIP_DIRS. Every indexer imports it; nobody
    # keeps a second copy that can drift out of sync again.
    _EXCLUDED_PATH_SEGMENTS = _INGEST_SKIP_DIRS

    # Role → preferred Brain domain list for system_context() filtering.
    # SM/PO/Architect: architecture decisions and docs live in expertise+md.
    # QA: test conventions live in expertise records.
    # DEV/Engineer: code patterns live in source code domains.
    ROLE_DOMAIN_MAP: dict[str, list[str]] = {
        "sm": ["expertise", "md"],
        "po": ["expertise", "md"],
        "architect": ["expertise", "md"],
        "qa": ["expertise"],
        "dev": ["py", "ts", "js", "expertise"],
        "engineer": ["py", "ts", "js", "expertise"],
    }

    def __init__(
        self,
        brain_db: str = "/data/brain.db",
        graph_db: str = "/data/graph.db",
        scores_db: str = "/data/scores.db",
        tasks_db: Optional[str] = None,
    ) -> None:
        self._brain_db_path = brain_db
        self._graph_db_path = graph_db
        self._scores_db_path = scores_db
        # Optional read-only handle to the project's tasks.db. Used by
        # LL-06's best_prompt(similar_to_task_id=...) to pull the
        # embedding BLOB and compute cosine similarity across tasks.
        # Falls back to global score_aggregates when not configured.
        self._tasks_db_path: Optional[str] = tasks_db
        self._current_step_id: Optional[str] = None
        self.last_result_count: int = 0

        # One sqlite connection PER THREAD PER database via the lazy
        # `_brain`/`_graph`/`_scores`/`_tasks` properties below
        # (sqlite-hardening workstream): the old shared handles
        # (check_same_thread=False) let concurrent callers interleave
        # statements/commits on a single connection.
        self._tlocal = threading.local()
        self.vector_enabled = False

        for path in (brain_db, graph_db, scores_db):
            Path(path).parent.mkdir(parents=True, exist_ok=True)

        # Materialize the constructing thread's connections up front so
        # corruption surfaces here (as it always has), then run schema
        # init ONCE on this thread — other threads' lazy connections
        # open the same, already-migrated db files.
        for label, db in (
            (brain_db, self._brain),
            (graph_db, self._graph),
            (scores_db, self._scores),
        ):
            try:
                db.execute("PRAGMA journal_mode=WAL")
            except sqlite3.DatabaseError as exc:
                raise BrainCorruptError(f"{label} is corrupt: {exc}") from exc
        if tasks_db is not None:
            self._tasks  # noqa: B018 — probe once; disables itself on error

        self._check_db_integrity()
        self.vector_enabled = _try_enable_vector(self._brain)

        self._init_brain_schema()
        self._heal_fts_orphans()
        self._init_graph_schema()
        self._init_scores_schema()

    # ------------------------------------------------------------------
    # Per-thread connection factory (sqlite-hardening workstream)
    # ------------------------------------------------------------------

    def _thread_conn(self, key: str, path: str) -> sqlite3.Connection:
        """Return the CALLING thread's connection to ``path``, opening
        and caching it in a thread-local slot on first use. Existing
        call sites (and tests) keep reading ``self._brain`` etc.
        unchanged, but no two threads ever share a handle."""
        cache = getattr(self._tlocal, "conns", None)
        if cache is None:
            cache = self._tlocal.conns = {}
        conn = cache.get(key)
        if conn is None:
            conn = self._connect(path)
            if key == "brain" and self.vector_enabled:
                # sqlite-vec is loaded per-connection: the constructing
                # thread enabled it via _try_enable_vector; every other
                # thread's brain connection must load the extension too
                # or its vec_* queries would fail.
                try:
                    import sqlite_vec  # type: ignore
                    conn.enable_load_extension(True)
                    sqlite_vec.load(conn)
                    conn.enable_load_extension(False)
                except Exception:  # pragma: no cover — degrade to BM25
                    pass
            cache[key] = conn
        return conn

    @property
    def _brain(self) -> sqlite3.Connection:
        return self._thread_conn("brain", self._brain_db_path)

    @property
    def _graph(self) -> sqlite3.Connection:
        return self._thread_conn("graph", self._graph_db_path)

    @property
    def _scores(self) -> sqlite3.Connection:
        return self._thread_conn("scores", self._scores_db_path)

    @property
    def _tasks(self) -> Optional[sqlite3.Connection]:
        """Optional read-only handle to the project's tasks.db (LL-06).
        Returns None when unconfigured or the file is unreadable —
        preserving the old constructor's fall-back-to-None semantics."""
        if self._tasks_db_path is None:
            return None
        try:
            return self._thread_conn("tasks", self._tasks_db_path)
        except sqlite3.DatabaseError:
            self._tasks_db_path = None
            return None

    @staticmethod
    def _connect(path: str) -> sqlite3.Connection:
        # Delegate the canonical hardening (timeout=5.0, Row, WAL,
        # busy_timeout=5000) to the ONE chokepoint so there is a single
        # source of truth; this method only layers the Brain-specific FTS
        # function on top. Local import keeps the engines->services edge
        # lazy and cycle-proof (sqlite_db is a dependency-free leaf).
        from prism_service.services import sqlite_db

        conn = sqlite_db.connect(path)
        # Register identifier-expander so FTS5 triggers can call it.
        # Deterministic: same input always yields same output (pure fn).
        try:
            conn.create_function(
                "expand_identifiers", 1, _expand_identifiers,
                deterministic=True,
            )
        except TypeError:
            # Older Python sqlite3 without deterministic kwarg.
            conn.create_function("expand_identifiers", 1, _expand_identifiers)
        return conn

    _FTS_HEAL_KEY = "fts_orphan_heal_v1"

    def _heal_fts_orphans(self) -> None:
        """Rebuild docs_fts once on a store indexed before the pragma fix.

        Until recursive_triggers was turned ON (services/sqlite_db.py) an
        INSERT OR REPLACE conflict deleted the docs row without firing
        docs_fts_ad, so every changed document left its superseded index
        entry behind at a rowid no docs row occupies. Nothing else can
        reach such an entry, so the store only ever grows.

        Deliberately NOT docs_fts(docs_fts) VALUES('rebuild'): rebuild
        reads the RAW docs.content column, which throws away every token
        expand_identifiers() split out (measured: hits for 'Gamma' fall
        from 2 to 0). 'delete-all' plus a re-insert through the same
        expression the triggers use is the only form that preserves it.

        Runs at most once per store: an empty store cannot hold an
        orphan, and the index_meta marker stops a second pass. Failures
        are swallowed -- a store that will not heal must still open.
        """
        try:
            done = self._brain.execute(
                "SELECT value FROM index_meta WHERE key = ?",
                (self._FTS_HEAL_KEY,),
            ).fetchone()
            if done is not None:
                return
            n_docs = self._brain.execute(
                "SELECT count(*) FROM docs").fetchone()[0]
            if not n_docs:
                return
            # ONE transaction. delete-all empties the index; only the
            # re-insert puts it back. A failure between the two must not
            # be left open on this shared connection for some later
            # commit to make permanent -- that would persist an EMPTIED
            # search index. isolation_level is "" on this connection, so
            # the transaction is opened explicitly here.
            self._brain.execute("BEGIN IMMEDIATE")
            try:
                self._brain.execute(
                    "INSERT INTO docs_fts(docs_fts) VALUES('delete-all')")
                self._brain.execute(
                    "INSERT INTO docs_fts(rowid, id, content, domain) "
                    "SELECT rowid, id, expand_identifiers(content), domain "
                    "FROM docs")
                self._brain.execute(
                    "INSERT OR REPLACE INTO index_meta (key, value) "
                    "VALUES (?, datetime('now'))", (self._FTS_HEAL_KEY,))
            except Exception:
                self._brain.rollback()
                raise
            self._brain.commit()
        except Exception as exc:  # noqa: BLE001 — never block the open
            try:
                self._brain.rollback()
            except Exception:
                pass
            print(f"Brain: FTS orphan heal skipped: {exc!r}",
                  file=sys.stderr, flush=True)

    def _check_db_integrity(self) -> None:
        """Run PRAGMA integrity_check on each DB. Raise BrainCorruptError if any fails."""
        for label, conn in (
            (self._brain_db_path, self._brain),
            (self._graph_db_path, self._graph),
            (self._scores_db_path, self._scores),
        ):
            try:
                row = conn.execute("PRAGMA integrity_check").fetchone()
                result = row[0] if row else "no result"
            except Exception as exc:
                raise BrainCorruptError(f"{label} integrity check error: {exc}") from exc
            if result != "ok":
                raise BrainCorruptError(f"{label} is corrupt: {result}")

    # ------------------------------------------------------------------
    # Schema initialisation
    # ------------------------------------------------------------------

    def _init_brain_schema(self) -> None:
        self._brain.executescript("""
            CREATE TABLE IF NOT EXISTS docs (
                id TEXT PRIMARY KEY,
                source_file TEXT,
                content TEXT NOT NULL,
                domain TEXT,
                content_hash TEXT,
                indexed_at TEXT DEFAULT (datetime('now')),
                entity_name TEXT,
                entity_kind TEXT,
                line_start INTEGER,
                line_end INTEGER
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
                id UNINDEXED,
                content,
                domain UNINDEXED,
                content='docs',
                content_rowid='rowid'
            );
            -- Drop legacy triggers (pre-#34) that indexed raw docs.content
            -- without identifier expansion. Replaced below with triggers
            -- that call expand_identifiers() so docs.content stays raw
            -- while docs_fts indexes the expanded form.
            DROP TRIGGER IF EXISTS docs_fts_ai;
            DROP TRIGGER IF EXISTS docs_fts_ad;
            DROP TRIGGER IF EXISTS docs_fts_au;
            -- v5.3.12 — IF NOT EXISTS on CREATE too. The drift timer's
            -- Brain caches its own connection (issue #38) and races with
            -- the request-path init; without this guard, the second
            -- caller hits "trigger docs_fts_ai already exists" and
            -- BrainService flips to _available=False even though the
            -- schema is fine.
            CREATE TRIGGER IF NOT EXISTS docs_fts_ai AFTER INSERT ON docs BEGIN
                INSERT INTO docs_fts(rowid, id, content, domain)
                    VALUES(new.rowid, new.id,
                           expand_identifiers(new.content), new.domain);
            END;
            CREATE TRIGGER IF NOT EXISTS docs_fts_ad AFTER DELETE ON docs BEGIN
                INSERT INTO docs_fts(docs_fts, rowid, id, content, domain)
                    VALUES('delete', old.rowid, old.id,
                           expand_identifiers(old.content), old.domain);
            END;
            CREATE TRIGGER IF NOT EXISTS docs_fts_au AFTER UPDATE ON docs BEGIN
                INSERT INTO docs_fts(docs_fts, rowid, id, content, domain)
                    VALUES('delete', old.rowid, old.id,
                           expand_identifiers(old.content), old.domain);
                INSERT INTO docs_fts(rowid, id, content, domain)
                    VALUES(new.rowid, new.id,
                           expand_identifiers(new.content), new.domain);
            END;
            CREATE INDEX IF NOT EXISTS idx_docs_source_file
                ON docs(source_file);
            CREATE INDEX IF NOT EXISTS idx_docs_entity_name
                ON docs(entity_name);
            CREATE TABLE IF NOT EXISTS index_meta (
                key TEXT PRIMARY KEY,
                value TEXT
            );
            CREATE TABLE IF NOT EXISTS searches (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts TEXT NOT NULL DEFAULT (datetime('now')),
                query TEXT NOT NULL,
                domain TEXT,
                domains TEXT,
                mode TEXT,
                rerank TEXT,
                context_prefix INTEGER,
                chunk_agg INTEGER,
                limit_requested INTEGER,
                n_results INTEGER,
                latency_ms INTEGER,
                final_top TEXT,
                -- Attribution: WHO asked (the session) and for WHAT task, so
                -- /api/retrievals can link each retrieval back to its origin.
                session_id TEXT,
                task_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_searches_ts
                ON searches(ts DESC);
            CREATE TABLE IF NOT EXISTS search_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                search_id INTEGER,
                doc_id TEXT NOT NULL,
                signal TEXT NOT NULL,
                note TEXT,
                ts TEXT NOT NULL DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_sf_search
                ON search_feedback(search_id);
            CREATE INDEX IF NOT EXISTS idx_sf_doc
                ON search_feedback(doc_id);
        """)
        # Migrate existing DBs: add chunk metadata columns if missing
        _meta_cols = [
            ("entity_name", "TEXT"),
            ("entity_kind", "TEXT"),
            ("line_start", "INTEGER"),
            ("line_end", "INTEGER"),
        ]
        existing_cols = {
            row[1]
            for row in self._brain.execute("PRAGMA table_info(docs)").fetchall()
        }
        for _col, _col_type in _meta_cols:
            if _col not in existing_cols:
                try:
                    self._brain.execute(
                        f"ALTER TABLE docs ADD COLUMN {_col} {_col_type}"
                    )
                    self._brain.commit()
                except sqlite3.OperationalError:
                    pass

        # Migrate existing DBs: add search attribution columns if missing so a
        # searches table created before this slice can record the asking
        # session/task. Existing rows keep NULL.
        _search_cols = {
            row[1]
            for row in self._brain.execute(
                "PRAGMA table_info(searches)"
            ).fetchall()
        }
        for _col in ("session_id", "task_id"):
            if _col not in _search_cols:
                try:
                    self._brain.execute(
                        f"ALTER TABLE searches ADD COLUMN {_col} TEXT"
                    )
                    self._brain.commit()
                except sqlite3.OperationalError:
                    pass

        if self.vector_enabled:
            # Discover the model's native embedding dimension at startup so
            # the vec0 table matches whatever local model is loaded
            # (potion-retrieval-32M is 512-dim; MiniLM-L6 is 384-dim).
            try:
                # Single-flight: serialize the native encode (GH #157).
                with _ENCODE_LOCK, _threadpool_limit_1():
                    probe = _MODEL.encode(["probe"])[0]
                dim = len(probe)
            except Exception:
                dim = 384
            # Self-heal a dimension change: if docs_vec already exists at a
            # different float[N] than the live model, the IF NOT EXISTS below
            # keeps the stale table and every insert raises "Dimension
            # mismatch" (e.g. 384-dim MiniLM table vs 512-dim potion model).
            # Drop it so it is recreated at the current dim; a reindex then
            # repopulates the vectors. Lets PRISM_EMBEDDER / model upgrades
            # work without a manual brain wipe.
            try:
                row = self._brain.execute(
                    "SELECT sql FROM sqlite_master WHERE name='docs_vec'"
                ).fetchone()
                if row and row[0]:
                    m = re.search(r"float\[(\d+)\]", row[0])
                    if m and int(m.group(1)) != dim:
                        print(
                            f"Brain: docs_vec dim {m.group(1)} != model dim "
                            f"{dim}; rebuilding vec table",
                            file=sys.stderr,
                        )
                        self._brain.execute("DROP TABLE IF EXISTS docs_vec")
                        self._brain.commit()
            except Exception:
                pass
            # Self-heal an embedder MODEL swap at the SAME dim (e.g.
            # potion-base-32M -> potion-retrieval-32M, both 512): two models
            # share no vector space, so mixing their vectors in one vec0
            # table silently degrades cosine ranking. The live model id is
            # persisted in index_meta('embedder_model'); on drift do exactly
            # what the dim heal does — drop docs_vec (recreated below) and
            # reset last_indexed so the next index pass re-embeds. A fresh
            # (or pre-tracking) store just records the current model id.
            try:
                live_model = _MODEL_ID
                if live_model:
                    row = self._brain.execute(
                        "SELECT value FROM index_meta "
                        "WHERE key = 'embedder_model'"
                    ).fetchone()
                    recorded = row["value"] if row else None
                    if recorded and recorded != live_model:
                        print(
                            f"Brain: embedder model changed {recorded!r} -> "
                            f"{live_model!r}; rebuilding vec table "
                            f"(reindex will re-embed)",
                            file=sys.stderr,
                        )
                        self._brain.execute("DROP TABLE IF EXISTS docs_vec")
                        self._brain.execute(
                            "DELETE FROM index_meta WHERE key = 'last_indexed'"
                        )
                    if recorded != live_model:
                        self._brain.execute(
                            "INSERT OR REPLACE INTO index_meta (key, value) "
                            "VALUES ('embedder_model', ?)",
                            (live_model,),
                        )
                    self._brain.commit()
            except Exception:
                pass
            try:
                self._brain.execute(
                    f"CREATE VIRTUAL TABLE IF NOT EXISTS docs_vec "
                    f"USING vec0(doc_id TEXT, embedding float[{dim}])"
                )
                self._brain.commit()
            except Exception:
                self.vector_enabled = False

    def _init_graph_schema(self) -> None:
        self._graph.executescript("""
            CREATE TABLE IF NOT EXISTS entities (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                kind TEXT DEFAULT 'unknown',
                file TEXT,
                line INTEGER,
                UNIQUE(name, file)
            );
            CREATE TABLE IF NOT EXISTS relationships (
                source_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
                target_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
                relation TEXT,
                PRIMARY KEY (source_id, target_id, relation)
            );
            CREATE INDEX IF NOT EXISTS idx_ent_name ON entities(name);
            CREATE INDEX IF NOT EXISTS idx_rel_src ON relationships(source_id);
            CREATE INDEX IF NOT EXISTS idx_rel_tgt ON relationships(target_id);
        """)
        # Apply the graphify-side schema extensions (confidence,
        # confidence_score, weight, source_location, communities,
        # graphify_id, etc.) so call_chain / graph_query can rely on
        # those columns existing without depending on _import_graph_json
        # having run first. Idempotent — each ALTER guards on
        # PRAGMA table_info.
        try:
            from prism_service.services.graph_service import _graph_schema_migrations
            _graph_schema_migrations(self._graph)
        except Exception:
            # Tests with stripped imports / circular-import edge cases
            # fall through silently — the SELECT will then raise and
            # the call_chain except-clause returns []. Production has
            # the import path available.
            pass

    def _init_scores_schema(self) -> None:
        self._scores.executescript("""
            CREATE TABLE IF NOT EXISTS prompt_scores (
                prompt_id TEXT,
                persona TEXT,
                step_id TEXT,
                score REAL,
                tokens_used INTEGER,
                context_tokens INTEGER,
                duration_s REAL,
                retries INTEGER,
                difficulty TEXT,
                tests_passed INTEGER,
                coverage_pct REAL,
                traceability_pct REAL,
                gate_passed INTEGER,
                probe_accuracy REAL,
                timestamp TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (prompt_id, persona, step_id, timestamp)
            );
            CREATE TABLE IF NOT EXISTS score_aggregates (
                prompt_id TEXT,
                persona TEXT,
                step_id TEXT,
                avg_score REAL DEFAULT 0.0,
                total_runs INTEGER DEFAULT 0,
                last_updated TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (prompt_id, persona, step_id)
            );
            CREATE TABLE IF NOT EXISTS prompt_variants (
                prompt_id TEXT PRIMARY KEY,
                persona TEXT,
                content TEXT NOT NULL,
                source TEXT DEFAULT 'learned',
                created_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS retired_variants (
                prompt_id TEXT PRIMARY KEY,
                persona TEXT,
                retired_at TEXT DEFAULT (datetime('now')),
                reason TEXT
            );
            CREATE TABLE IF NOT EXISTS meta_prompt_candidates (
                candidate_id TEXT PRIMARY KEY,
                prompt_id TEXT UNIQUE NOT NULL,
                persona TEXT NOT NULL,
                step_id TEXT NOT NULL,
                parent_prompt_id TEXT,
                content TEXT NOT NULL,
                rationale TEXT,
                generator TEXT,
                status TEXT DEFAULT 'proposed',
                created_at TEXT DEFAULT (datetime('now')),
                evaluated_at TEXT,
                promoted_at TEXT,
                decision_json TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_meta_prompt_candidates_status
                ON meta_prompt_candidates(status);
            CREATE INDEX IF NOT EXISTS idx_meta_prompt_candidates_persona_step
                ON meta_prompt_candidates(persona, step_id);
            CREATE TABLE IF NOT EXISTS meta_prompt_evaluations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                candidate_id TEXT NOT NULL,
                baseline_score REAL,
                holdout_score REAL,
                train_score REAL,
                contextpack_score REAL,
                tests_passed INTEGER,
                retry_delta REAL,
                token_ratio REAL,
                followup_delta REAL,
                revert_delta REAL,
                sample_n INTEGER,
                score_delta REAL,
                passed INTEGER,
                reason TEXT,
                metrics_json TEXT,
                evaluated_at TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS session_outcomes (
                session_id TEXT PRIMARY KEY,
                duration_s INTEGER DEFAULT 0,
                tokens_used INTEGER DEFAULT 0,
                files_read INTEGER DEFAULT 0,
                files_modified INTEGER DEFAULT 0,
                skills_invoked INTEGER DEFAULT 0,
                timestamp TEXT DEFAULT (datetime('now')),
                -- JSON arrays of the actual paths a session read/modified;
                -- the *_read/*_modified ints above stay the legacy counts.
                files_read_paths TEXT,
                files_modified_paths TEXT
            );
            CREATE TABLE IF NOT EXISTS skill_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                skill_name TEXT NOT NULL,
                timestamp TEXT DEFAULT (datetime('now'))
            );
            CREATE TABLE IF NOT EXISTS subagent_outcomes (
                prompt_id TEXT PRIMARY KEY,
                validator TEXT,
                recommendation TEXT,
                evidence_count INTEGER DEFAULT 0,
                certificate_complete INTEGER DEFAULT 0,
                certificate_blocked INTEGER DEFAULT 0,
                timed_out INTEGER DEFAULT 0,
                gate_agreed INTEGER,
                tokens_used INTEGER DEFAULT 0,
                duration_s REAL DEFAULT 0.0,
                timestamp TEXT DEFAULT (datetime('now'))
            );

            -- ---- Learning-loop v5 tables (LL-01) ----------------------------
            -- Ties Claude sessions to PRISM tasks so per-task rollup joins
            -- through the full session history. Schema-only; populated by
            -- later LL-04 / LL-07 subtasks.
            CREATE TABLE IF NOT EXISTS task_sessions (
                task_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                started_at TEXT,
                ended_at TEXT,
                PRIMARY KEY (task_id, session_id)
            );
            CREATE INDEX IF NOT EXISTS idx_task_sessions_session_id
                ON task_sessions(session_id);
            CREATE INDEX IF NOT EXISTS idx_task_sessions_task_id
                ON task_sessions(task_id);

            -- Which prompt variant was used for which (task, step). Feeds
            -- Brain.best_prompt(similar_to_task_id=...) in LL-06.
            CREATE TABLE IF NOT EXISTS task_variants (
                task_id TEXT NOT NULL,
                step_id TEXT NOT NULL,
                prompt_id TEXT NOT NULL,
                persona TEXT,
                recorded_at TEXT DEFAULT (datetime('now')),
                PRIMARY KEY (task_id, step_id, prompt_id)
            );
            CREATE INDEX IF NOT EXISTS idx_task_variants_task_id
                ON task_variants(task_id);
            CREATE INDEX IF NOT EXISTS idx_task_variants_prompt_id
                ON task_variants(prompt_id);

            -- Quantitative + qualitative rollup per task (one row per merged
            -- task). `quality_score` is the Layer-A composite, `cuped_score`
            -- is the operator-baseline-adjusted value, `qualitative_score`
            -- is the Layer-B reflection overlay, `components_json` stores
            -- the raw signals for auditability.
            CREATE TABLE IF NOT EXISTS task_quality_rollup (
                task_id TEXT PRIMARY KEY,
                quality_score REAL,
                qualitative_score REAL,
                cuped_score REAL,
                components_json TEXT,
                scored_at TEXT DEFAULT (datetime('now'))
            );

            -- Per-operator rolling merge-rate baseline for CUPED
            -- residualization (LL-05). Keeps operator skill from being
            -- credited to the variant.
            CREATE TABLE IF NOT EXISTS operator_baselines (
                operator_id TEXT PRIMARY KEY,
                window_start TEXT,
                merge_rate REAL,
                sample_n INTEGER DEFAULT 0,
                updated_at TEXT DEFAULT (datetime('now'))
            );

            -- Layer-B queue. Stop hook fills this via janitor_mark_stale;
            -- janitor_check dispenses; caller's prism-reflect subagent
            -- submits back.
            CREATE TABLE IF NOT EXISTS consolidation_candidates (
                id TEXT PRIMARY KEY,
                task_id TEXT,
                session_id TEXT,
                trigger TEXT,
                scope_json TEXT,
                status TEXT DEFAULT 'pending',
                queued_at TEXT DEFAULT (datetime('now')),
                staled_at TEXT,
                dispensed_at TEXT,
                completed_at TEXT,
                retry_count INTEGER DEFAULT 0,
                last_nudged_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_consolidation_candidates_session_id
                ON consolidation_candidates(session_id);
            CREATE INDEX IF NOT EXISTS idx_consolidation_candidates_task_id
                ON consolidation_candidates(task_id);
            CREATE INDEX IF NOT EXISTS idx_consolidation_candidates_status
                ON consolidation_candidates(status);

            -- Audit trail of every completed (or errored) reflection run.
            -- Output JSON is preserved verbatim — invalidated memories can
            -- still be traced back to the run that retired them.
            CREATE TABLE IF NOT EXISTS consolidation_runs (
                id TEXT PRIMARY KEY,
                candidate_id TEXT,
                run_at TEXT DEFAULT (datetime('now')),
                output_json TEXT,
                subagent_type TEXT,
                confidence REAL,
                schema_valid INTEGER DEFAULT 1,
                op_type TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_consolidation_runs_candidate_id
                ON consolidation_runs(candidate_id);

            -- Memory metadata sidecar. The JSONL-under-mulch store remains
            -- the source of truth for memory *content*; this SQL table
            -- tracks the queryable metadata the janitor needs: session
            -- attribution, recency, soft-invalidation status.
            CREATE TABLE IF NOT EXISTS memory_meta (
                memory_id TEXT PRIMARY KEY,
                session_id TEXT,
                last_recalled_at TEXT,
                recall_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'active'
            );
            CREATE INDEX IF NOT EXISTS idx_memory_meta_session_id
                ON memory_meta(session_id);
            CREATE INDEX IF NOT EXISTS idx_memory_meta_status
                ON memory_meta(status);

            -- Tier-3 adaptive policy: each row is one tuning of the memory
            -- knobs (forget_cutoff / decay_weight / merge_similarity_threshold)
            -- computed from the recall->outcome signal. Append-only — the
            -- newest row is the active policy, older rows are the history the
            -- /learning panel charts and a future bandit would replay.
            CREATE TABLE IF NOT EXISTS policy_knobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                forget_cutoff REAL,
                decay_weight REAL,
                merge_similarity_threshold REAL,
                rationale TEXT,
                tuned_at TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_policy_knobs_tuned_at
                ON policy_knobs(tuned_at);
        """)
        # agent_runs lives in agent_runs_data.py, not duplicated here: that
        # module is the sole reader/writer of the table, and its own
        # _connect() applies the identical schema on the ingest path's
        # first touch (fresh-install ordering hole, task <gamify defect 1>)
        # -- importing the constant keeps ONE definition instead of two
        # that could silently drift apart.
        from prism_service.services.agent_runs_data import AGENT_RUNS_SCHEMA
        self._scores.executescript(AGENT_RUNS_SCHEMA)
        # Migrate existing scores.db: add op_type to consolidation_runs so
        # every memory-operation verdict (reflection / forget / prune /
        # distill / ...) is replayable + diffable by op family. Existing
        # rows keep op_type=NULL; the reflection path writes 'reflection'.
        _runs_cols = {
            row[1]
            for row in self._scores.execute(
                "PRAGMA table_info(consolidation_runs)"
            ).fetchall()
        }
        if "op_type" not in _runs_cols:
            try:
                self._scores.execute(
                    "ALTER TABLE consolidation_runs ADD COLUMN op_type TEXT"
                )
                self._scores.commit()
            except sqlite3.OperationalError:
                pass
        # Migrate existing scores.db: add the per-session file-path columns so a
        # store created before this slice can hold WHICH files a session
        # touched (the transcript importer writes JSON arrays here). Existing
        # rows keep NULL, which the readers treat as [].
        _so_cols = {
            row[1]
            for row in self._scores.execute(
                "PRAGMA table_info(session_outcomes)"
            ).fetchall()
        }
        for _col in ("files_read_paths", "files_modified_paths"):
            if _col not in _so_cols:
                try:
                    self._scores.execute(
                        f"ALTER TABLE session_outcomes ADD COLUMN {_col} TEXT"
                    )
                    self._scores.commit()
                except sqlite3.OperationalError:
                    pass

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _content_hash(content: str) -> str:
        import hashlib
        return hashlib.sha256(content.encode()).hexdigest()[:16]

    def _embed(self, text: str) -> Optional[list[float]]:
        if not self.vector_enabled or _MODEL is None:
            return None
        try:
            # Single-flight: serialize the native encode (GH #157). Guard
            # above stays OUTSIDE the lock; only the encode is wrapped so the
            # .tolist() conversion doesn't hold the lock (throughput).
            with _ENCODE_LOCK, _threadpool_limit_1():
                vecs = _MODEL.encode([text[:2048]])
            return vecs[0].tolist()
        except Exception:
            return None

    def _should_index(self, filepath: str) -> bool:
        p = Path(filepath)
        if any(part in self._EXCLUDED_PATH_SEGMENTS for part in p.parts):
            return False
        return p.suffix in self._INDEXABLE_SUFFIXES

    def _get_last_index_timestamp(self) -> str:
        row = self._brain.execute(
            "SELECT value FROM index_meta WHERE key = 'last_indexed'"
        ).fetchone()
        return row["value"] if row else "1970-01-01T00:00:00"

    def _update_last_index_timestamp(self) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        self._brain.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('last_indexed', ?)",
            (ts,),
        )
        self._brain.commit()

    def _purge_deleted(self) -> int:
        """Remove DB entries for files that no longer exist or are excluded.

        Safety: if ZERO indexed source files are reachable from this
        process, the project directory is likely not mounted (common in
        service-mode containers). A 100%-purge decision in that case
        wipes the index. We detect and skip, logging so operators know.
        Override with PRISM_PURGE_FORCE=1 for environments where the
        empty result is the actual truth.
        """
        import os as _os
        import sys as _sys
        rows = self._brain.execute(
            "SELECT id, source_file FROM docs WHERE source_file IS NOT NULL"
        ).fetchall()
        if not rows:
            return 0
        to_purge: list[str] = []
        reachable = 0
        for row in rows:
            sf = row["source_file"]
            exists = Path(sf).exists()
            if exists:
                reachable += 1
                if not self._should_index(sf):
                    to_purge.append(sf)
            else:
                to_purge.append(sf)
        if to_purge and reachable == 0:
            if _os.environ.get("PRISM_PURGE_FORCE", "").strip() != "1":
                print(
                    f"[purge-skip] {len(to_purge)}/{len(rows)} rows look "
                    f"missing but no indexed file is reachable — "
                    f"skipping purge (project likely unmounted). Set "
                    f"PRISM_PURGE_FORCE=1 to override.",
                    file=_sys.stderr,
                )
                return 0
        if to_purge:
            self._remove_entries_by_source(to_purge)
        return len(to_purge)

    def _remove_entries_by_source(self, files: list[str]) -> None:
        for filepath in files:
            rows = self._brain.execute(
                "SELECT id FROM docs WHERE source_file = ?", (filepath,)
            ).fetchall()
            for row in rows:
                doc_id = row["id"]
                if self.vector_enabled:
                    try:
                        self._brain.execute(
                            "DELETE FROM docs_vec WHERE doc_id = ?", (doc_id,)
                        )
                    except Exception:
                        pass
                self._brain.execute("DELETE FROM docs WHERE id = ?", (doc_id,))
        self._brain.commit()

    # ------------------------------------------------------------------
    # Chunking helpers
    # ------------------------------------------------------------------

    def _chunk_source_file(self, filepath: str, content: str) -> list[dict]:
        """Split a source file into multi-granular chunks.

        Returns a list of chunk dicts with keys:
          doc_id, content, entity_name, entity_kind, line_start, line_end

        Three granularity tiers (all emitted when PRISM_MULTIGRAN=on, default):
          - coarse: one whole-file chunk (``path::__file__`` for code, or the
            existing single-chunk ``filepath`` id for prose)
          - mid: semantic chunks at function/class boundaries (code only,
            ``path::EntityName``) and a ``path::__module__`` for loose
            top-level statements
          - fine: sliding 2048-char windows with 256-char overlap over the
            whole content (``path::win_N``). Emitted when content is large
            enough that windows carry new signal (>= min_chars).

        Chars/4 approximation: 2048 chars ~ 512 tokens, 256 chars ~ 64 tokens,
        matching the [512, 128]-token mid/fine target plus a file-level
        coarse pass. Per brain_engine.search() matches `_embed()`'s own
        2048-char truncation, so windows are sized to fit one embedding.

        Set PRISM_MULTIGRAN=off to fall back to the original single-tier
        semantic-only chunking (useful for A/B comparisons).

        task f9e0745e: every ``entity_kind == "module"`` chunk this method
        returns is bounded to a 4 KB head slice (see ``_cap_module_chunks``)
        before it goes back to the caller. A changelog-style module (one
        huge appended string, e.g. ``__version__.py``) can otherwise grow
        past 100 KB and out-score every real symbol on every search; the
        sliding-window tier already covers the rest of the file.
        """
        import os as _os

        multigran = _os.environ.get("PRISM_MULTIGRAN", "on").strip().lower() != "off"

        suffix = Path(filepath).suffix.lower()
        lines = content.splitlines()
        n = len(lines) or 1

        if suffix not in _TS_LANG_MAP:
            # Prose/config/unknown: keep the legacy single whole-file chunk
            # as the coarse tier, then add sliding windows for large files.
            chunks: list[dict] = [{
                "doc_id": filepath,
                "content": content,
                "entity_name": "__module__",
                "entity_kind": "module",
                "line_start": 1,
                "line_end": n,
            }]
            if multigran:
                chunks.extend(
                    self._sliding_window_chunks(filepath, content, min_chars=2048)
                )
            return self._cap_module_chunks(chunks)

        lang_name = _TS_LANG_MAP[suffix]
        parser = _get_treesitter_parser(lang_name)

        if parser is not None and lang_name in _LANG_CHUNK_CONFIG:
            chunks = self._chunk_treesitter_lang(
                filepath, content, parser, lines, lang_name,
            )
        else:
            chunks = self._chunk_regex_fallback(filepath, content, lines, suffix)

        if not multigran:
            return self._cap_module_chunks(chunks)

        # Coarse tier: whole-file view, distinct from __module__ (which only
        # covers lines NOT covered by any def/class). Only worth emitting when
        # the file has multiple semantic chunks AND is substantial.
        if len(chunks) > 1 and len(content) >= 2048:
            chunks.append({
                "doc_id": f"{filepath}::__file__",
                "content": content,
                "entity_name": "__file__",
                "entity_kind": "file",
                "line_start": 1,
                "line_end": n,
            })

        # Fine tier: sliding windows over full content. Skips small files
        # where the semantic chunks already cover everything.
        chunks.extend(
            self._sliding_window_chunks(filepath, content, min_chars=2048)
        )

        return self._cap_module_chunks(chunks)

    _MODULE_CHUNK_CAP_BYTES = 4096  # task f9e0745e

    @classmethod
    def _cap_module_chunks(cls, chunks: list[dict]) -> list[dict]:
        """Bound every whole-module chunk (``entity_kind == "module"``) to
        a 4 KB head slice, in place.

        A module-level doc holds every top-level line the language
        chunker did not already carve into its own definition. For most
        files this is small, but a changelog-style module (one string
        appended to on every release) can grow past 100 KB and then
        answer every search query, crowding out real symbols. The
        sliding-window tier already covers the full file byte for byte,
        so capping the head loses no content -- it only stops one doc
        from dominating search.
        """
        cap = cls._MODULE_CHUNK_CAP_BYTES
        for chunk in chunks:
            if chunk.get("entity_kind") != "module":
                continue
            content = chunk.get("content") or ""
            encoded = content.encode("utf-8")
            if len(encoded) > cap:
                chunk["content"] = encoded[:cap].decode("utf-8", errors="ignore")
        return chunks

    def _sliding_window_chunks(
        self,
        filepath: str,
        content: str,
        *,
        min_chars: int = 2048,
        window_chars: int = 2048,
        overlap_chars: int = 256,
    ) -> list[dict]:
        """Emit overlapping content windows for the fine-granularity tier.

        Returns an empty list when content is shorter than ``min_chars``
        (no new signal vs. the whole-file chunk). Windows are ``window_chars``
        wide with ``overlap_chars`` overlap between consecutive windows.
        Line ranges are computed from newline counts so UI linking stays
        accurate on arbitrary offsets.
        """
        total = len(content)
        if total < min_chars:
            return []
        step = max(1, window_chars - overlap_chars)
        windows: list[dict] = []
        pos = 0
        idx = 0
        while pos < total:
            end_pos = min(pos + window_chars, total)
            windows.append({
                "doc_id": f"{filepath}::win_{idx}",
                "content": content[pos:end_pos],
                "entity_name": f"win_{idx}",
                "entity_kind": "window",
                "line_start": content.count("\n", 0, pos) + 1,
                "line_end": content.count("\n", 0, end_pos) + 1,
            })
            idx += 1
            if end_pos >= total:
                break
            pos += step
        return windows

    def _chunk_treesitter_lang(
        self,
        filepath: str,
        content: str,
        parser: object,
        lines: list[str],
        lang_name: str,
    ) -> list[dict]:
        """Language-generic tree-sitter chunker.

        Produces the same output shape as ``_chunk_python_treesitter`` for
        any language that has an entry in ``_LANG_CHUNK_CONFIG`` (Python,
        C#, TypeScript, JavaScript, TSX, JSX today). Methods nested in
        classes/interfaces/structs are emitted as their own chunks with
        doc_id = ``{path}::{ContainerName}.{method_name}`` so
        find_symbol can return a function-level slice.
        """
        cfg = _LANG_CHUNK_CONFIG.get(lang_name)
        if cfg is None:
            return []
        if isinstance(cfg, str):
            cfg = _LANG_CHUNK_CONFIG[cfg]  # alias
        raw = content.encode("utf-8", errors="replace")
        tree = parser.parse(raw)  # type: ignore[attr-defined]
        chunks: list[dict] = []
        covered: set[int] = set()
        self._chunk_ts_walk(
            tree.root_node, cfg, filepath, lines, chunks, covered,
            emit_docstring=(lang_name == "python"),
        )
        module_lines = [
            lines[i] for i in range(len(lines))
            if i not in covered and lines[i].strip()
        ]
        if module_lines:
            chunks.append({
                "doc_id": f"{filepath}::__module__",
                "content": "\n".join(module_lines),
                "entity_name": "__module__",
                "entity_kind": "module",
                "line_start": 1,
                "line_end": len(lines) or 1,
            })
        if not chunks:
            return [{
                "doc_id": filepath, "content": content,
                "entity_name": "__module__", "entity_kind": "module",
                "line_start": 1, "line_end": len(lines) or 1,
            }]
        return chunks

    def _chunk_ts_walk(
        self, node, cfg, filepath, lines, chunks, covered, emit_docstring,
    ):
        """Recursive AST visitor for _chunk_treesitter_lang."""
        for child in node.children:
            t = child.type
            if t in cfg["descend"]:
                self._chunk_ts_walk(
                    child, cfg, filepath, lines, chunks, covered,
                    emit_docstring,
                )
                continue
            self._chunk_ts_emit(
                child, cfg, filepath, lines, chunks, covered, emit_docstring,
            )

    def _chunk_ts_emit(
        self, outer, cfg, filepath, lines, chunks, covered, emit_docstring,
    ):
        """Emit a chunk for ``outer`` if it is a top-level declaration."""
        t = outer.type
        def_node = outer
        if cfg["decorated_wrapper"] and t == cfg["decorated_wrapper"]:
            inner = next(
                (c for c in outer.children if c.type in cfg["top"]),
                None,
            )
            if inner is None:
                return
            def_node = inner
            t = inner.type
        if t not in cfg["top"]:
            return
        kind = cfg["top"][t]
        name = _ts_find_name(def_node, cfg["name_types"])
        if name is None:
            return
        start = outer.start_point[0]
        end = outer.end_point[0]
        body = "\n".join(lines[start:end + 1])
        if emit_docstring:
            summary = self._extract_python_docstring(def_node)
            if summary:
                body = f"{summary}\n\n{body}"
        for i in range(start, end + 1):
            covered.add(i)
        chunks.append({
            "doc_id": f"{filepath}::{name}",
            "content": body,
            "entity_name": name,
            "entity_kind": kind,
            "line_start": start + 1,
            "line_end": end + 1,
        })
        if t in cfg["class_types"]:
            self._chunk_ts_methods(
                def_node, cfg, filepath, lines, chunks, name,
                emit_docstring,
            )

    def _chunk_ts_methods(
        self, class_node, cfg, filepath, lines, chunks, class_name,
        emit_docstring,
    ):
        """Emit method chunks for members of a class-like node."""
        body = next(
            (c for c in class_node.children if c.type == cfg["body_type"]),
            None,
        )
        if body is None:
            return
        seen: set[str] = set()
        for member in body.children:
            outer_m = member
            mtype = member.type
            mnode = member
            if cfg["decorated_wrapper"] and mtype == cfg["decorated_wrapper"]:
                inner = next(
                    (c for c in member.children if c.type in cfg["method"]),
                    None,
                )
                if inner is None:
                    continue
                mnode = inner
                mtype = inner.type
            if mtype not in cfg["method"]:
                continue
            mkind = cfg["method"][mtype]
            mname = _ts_find_name(mnode, cfg["name_types"])
            if mname is None:
                continue
            doc_id = f"{filepath}::{class_name}.{mname}"
            if doc_id in seen:
                continue
            seen.add(doc_id)
            ms = outer_m.start_point[0]
            me = outer_m.end_point[0]
            mbody = "\n".join(lines[ms:me + 1])
            if emit_docstring:
                msummary = self._extract_python_docstring(mnode)
                if msummary:
                    mbody = f"{msummary}\n\n{mbody}"
            chunks.append({
                "doc_id": doc_id,
                "content": mbody,
                "entity_name": mname,
                "entity_kind": mkind,
                "line_start": ms + 1,
                "line_end": me + 1,
            })

    def _chunk_python_treesitter(
        self, filepath: str, content: str, parser: object, lines: list[str]
    ) -> list[dict]:
        """Chunk a Python file using tree-sitter AST."""
        raw = content.encode("utf-8", errors="replace")
        tree = parser.parse(raw)  # type: ignore[attr-defined]
        root = tree.root_node

        chunks: list[dict] = []
        covered: set[int] = set()  # 0-indexed line numbers

        for child in root.children:  # type: ignore[attr-defined]
            # Handle decorated definitions (decorators + def/class)
            if child.type == "decorated_definition":
                inner = next(
                    (c for c in child.children  # type: ignore[attr-defined]
                     if c.type in ("function_definition", "class_definition")),
                    None,
                )
                if inner is None:
                    continue
                def_node = inner
                outer_node = child
            elif child.type in ("function_definition", "class_definition"):
                def_node = child
                outer_node = child
            else:
                continue

            kind = "function" if def_node.type == "function_definition" else "class"
            name_node = next(
                (c for c in def_node.children if c.type == "identifier"),  # type: ignore[attr-defined]
                None,
            )
            if name_node is None:
                continue
            name = name_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]

            start = outer_node.start_point[0]  # type: ignore[attr-defined]
            end = outer_node.end_point[0]      # type: ignore[attr-defined]

            chunk_lines = lines[start:end + 1]
            chunk_content = "\n".join(chunk_lines)

            # Prepend docstring summary for richer embeddings
            summary = self._extract_python_docstring(def_node)
            if summary:
                chunk_content = f"{summary}\n\n{chunk_content}"

            for i in range(start, end + 1):
                covered.add(i)

            chunks.append({
                "doc_id": f"{filepath}::{name}",
                "content": chunk_content,
                "entity_name": name,
                "entity_kind": kind,
                "line_start": start + 1,
                "line_end": end + 1,
            })

            if kind == "class":
                chunks.extend(
                    self._python_class_methods(
                        def_node, lines, name, filepath,
                    )
                )

        # Module-level chunk: non-empty lines not covered by any definition
        module_lines = [
            lines[i] for i in range(len(lines))
            if i not in covered and lines[i].strip()
        ]
        if module_lines:
            # Check for a module-level docstring
            module_summary = self._summarize_chunk(
                "\n".join(lines[:10]), "module"
            )
            module_content = "\n".join(module_lines)
            if module_summary and not module_content.startswith(module_summary):
                module_content = f"{module_summary}\n\n{module_content}"
            chunks.append({
                "doc_id": f"{filepath}::__module__",
                "content": module_content,
                "entity_name": "__module__",
                "entity_kind": "module",
                "line_start": 1,
                "line_end": len(lines) or 1,
            })

        if not chunks:
            return [{
                "doc_id": filepath,
                "content": content,
                "entity_name": "__module__",
                "entity_kind": "module",
                "line_start": 1,
                "line_end": len(lines) or 1,
            }]

        return chunks

    def _python_class_methods(
        self, class_node, lines, class_name, filepath,
    ) -> list[dict]:
        """Emit one method chunk per function_definition inside a class.

        The class chunk itself still carries the full class body so
        whole-class queries work; these extra chunks let find_symbol
        return a ~40-line method slice instead.
        """
        block = next(
            (c for c in class_node.children if c.type == "block"), None,
        )
        if block is None:
            return []
        out: list[dict] = []
        for child in block.children:
            if child.type == "decorated_definition":
                mdef = next(
                    (c for c in child.children
                     if c.type == "function_definition"), None,
                )
                outer = child
            elif child.type == "function_definition":
                mdef = child
                outer = child
            else:
                continue
            if mdef is None:
                continue
            nname = next(
                (c for c in mdef.children if c.type == "identifier"),
                None,
            )
            if nname is None:
                continue
            mname = nname.text.decode("utf-8", errors="replace")
            start = outer.start_point[0]
            end = outer.end_point[0]
            body = "\n".join(lines[start:end + 1])
            summary = self._extract_python_docstring(mdef)
            if summary:
                body = f"{summary}\n\n{body}"
            out.append({
                "doc_id": f"{filepath}::{class_name}.{mname}",
                "content": body,
                "entity_name": mname,
                "entity_kind": "method",
                "line_start": start + 1,
                "line_end": end + 1,
            })
        return out

    @staticmethod
    def _extract_python_docstring(func_or_class_node: object) -> str:
        """Extract docstring text from a Python function/class AST node."""
        body = next(
            (c for c in func_or_class_node.children if c.type == "block"),  # type: ignore[attr-defined]
            None,
        )
        if body is None:
            return ""
        for child in body.children:  # type: ignore[attr-defined]
            if child.type == "expression_statement":
                str_node = next(
                    (c for c in child.children if c.type == "string"),  # type: ignore[attr-defined]
                    None,
                )
                if str_node is not None:
                    raw = str_node.text.decode("utf-8", errors="replace").strip()  # type: ignore[attr-defined]
                    for q in ('"""', "'''"):
                        if raw.startswith(q) and len(raw) > len(q) * 2:
                            return raw[len(q):raw.rfind(q)].strip()
                    if len(raw) > 2 and raw[0] in ('"', "'") and raw[-1] == raw[0]:
                        return raw[1:-1]
                break
            elif child.type not in ("comment", "newline", "indent", "\n"):
                break
        return ""

    @staticmethod
    def _summarize_chunk(chunk_content: str, entity_kind: str) -> str:
        """Extract a summary from chunk content for improved embedding quality.

        Looks for the first triple-quoted docstring or leading comment block
        after the entity definition line.  Returns summary text or empty string.
        """
        lines = chunk_content.splitlines()
        i = 0
        # Skip leading decorator / def / class / shebang lines
        while i < len(lines):
            stripped = lines[i].strip()
            if stripped.startswith(("@", "def ", "async def ", "class ", "#!")):
                i += 1
                continue
            break

        while i < len(lines):
            stripped = lines[i].strip()
            if not stripped:
                i += 1
                continue
            # Triple-quoted docstring
            for q in ('"""', "'''"):
                if stripped.startswith(q):
                    rest = stripped[len(q):]
                    end_idx = rest.find(q)
                    if end_idx >= 0:
                        # Single-line docstring
                        return rest[:end_idx].strip()
                    # Multi-line: keep scanning
                    doc_parts = [rest]
                    i += 1
                    while i < len(lines):
                        l = lines[i]
                        s = l.strip()
                        end_idx = s.find(q)
                        if end_idx >= 0:
                            doc_parts.append(s[:end_idx])
                            return "\n".join(doc_parts).strip()
                        doc_parts.append(l)
                        i += 1
                    return "\n".join(doc_parts).strip()
            # Leading comment block
            if stripped.startswith("#"):
                comment_lines = []
                while i < len(lines) and lines[i].strip().startswith("#"):
                    comment_lines.append(lines[i].strip().lstrip("#").strip())
                    i += 1
                return "\n".join(comment_lines)
            break  # non-docstring content
        return ""

    def _chunk_regex_fallback(
        self, filepath: str, content: str, lines: list[str], suffix: str
    ) -> list[dict]:
        """Chunk a source file using regex patterns (fallback without tree-sitter)."""
        if suffix == ".py":
            patterns = [
                (re.compile(r"^(?:async\s+)?def\s+(\w+)"), "function"),
                (re.compile(r"^class\s+(\w+)"), "class"),
            ]
        elif suffix in (".ts", ".tsx", ".js", ".jsx"):
            patterns = [
                (re.compile(r"^(?:export\s+)?(?:async\s+)?function\s+(\w+)"), "function"),
                (re.compile(r"^(?:export\s+)?class\s+(\w+)"), "class"),
            ]
        elif suffix == ".cs":
            patterns = [
                (re.compile(r"^\s*(?:public|private|protected|internal|static).*\s+(\w+)\s*\("), "function"),
                (re.compile(r"^\s*(?:public|private|protected|internal)?\s*class\s+(\w+)"), "class"),
            ]
        else:
            patterns = []

        if not patterns:
            return [{
                "doc_id": filepath,
                "content": content,
                "entity_name": "__module__",
                "entity_kind": "module",
                "line_start": 1,
                "line_end": len(lines) or 1,
            }]

        # Find all top-level definition start lines
        definitions: list[tuple[int, str, str]] = []  # (0-indexed line, name, kind)
        for i, line in enumerate(lines):
            for pattern, kind in patterns:
                m = pattern.match(line)
                if m:
                    definitions.append((i, m.group(1), kind))
                    break

        if not definitions:
            return [{
                "doc_id": filepath,
                "content": content,
                "entity_name": "__module__",
                "entity_kind": "module",
                "line_start": 1,
                "line_end": len(lines) or 1,
            }]

        chunks: list[dict] = []
        covered: set[int] = set()

        for idx, (start, name, kind) in enumerate(definitions):
            end = (
                definitions[idx + 1][0] - 1
                if idx + 1 < len(definitions)
                else len(lines) - 1
            )
            chunk_lines = lines[start:end + 1]
            chunk_content = "\n".join(chunk_lines)
            summary = self._summarize_chunk(chunk_content, kind)
            if summary and not chunk_content.startswith(summary):
                chunk_content = f"{summary}\n\n{chunk_content}"
            for i in range(start, end + 1):
                covered.add(i)
            chunks.append({
                "doc_id": f"{filepath}::{name}",
                "content": chunk_content,
                "entity_name": name,
                "entity_kind": kind,
                "line_start": start + 1,
                "line_end": end + 1,
            })

        module_lines = [
            lines[i] for i in range(len(lines))
            if i not in covered and lines[i].strip()
        ]
        if module_lines:
            module_content = "\n".join(module_lines)
            chunks.append({
                "doc_id": f"{filepath}::__module__",
                "content": module_content,
                "entity_name": "__module__",
                "entity_kind": "module",
                "line_start": 1,
                "line_end": len(lines) or 1,
            })

        # Second pass: for Python classes, emit indented-def as method chunks
        # so find_symbol('method_name') returns a ~40-line slice instead of
        # the whole class. Python-only for now — other languages can follow
        # with appropriate indent-aware regex.
        if suffix == ".py":
            chunks.extend(self._regex_python_methods(filepath, lines, chunks))

        return chunks

    def _regex_python_methods(
        self, filepath: str, lines: list[str], chunks: list[dict],
    ) -> list[dict]:
        """Emit method chunks for directly-nested defs inside each class chunk.

        Locks onto the indent level of the first def seen in the class body
        and only emits defs at that exact level, so nested helper functions
        inside a method don't collide with their outer siblings.
        """
        mre = re.compile(r"^(\s+)(?:async\s+)?def\s+(\w+)")
        out: list[dict] = []
        seen: set[str] = set()
        for c in chunks:
            if c.get("entity_kind") != "class":
                continue
            cname = c["entity_name"]
            cs, ce = c["line_start"] - 1, c["line_end"] - 1
            method_indent: int | None = None
            hits: list[tuple[int, str]] = []
            for i in range(cs, ce + 1):
                m = mre.match(lines[i])
                if not m:
                    continue
                indent = len(m.group(1))
                if method_indent is None:
                    method_indent = indent
                if indent != method_indent:
                    continue  # nested helper inside a method
                hits.append((i, m.group(2)))
            if not hits:
                continue
            for j, (ms, mname) in enumerate(hits):
                me = hits[j + 1][0] - 1 if j + 1 < len(hits) else ce
                doc_id = f"{filepath}::{cname}.{mname}"
                if doc_id in seen:
                    continue  # defensive: suffix-level collision
                seen.add(doc_id)
                body = "\n".join(lines[ms:me + 1])
                out.append({
                    "doc_id": doc_id,
                    "content": body,
                    "entity_name": mname,
                    "entity_kind": "method",
                    "line_start": ms + 1,
                    "line_end": me + 1,
                })
        return out

    def _index_files(self, files: list[str]) -> None:
        for filepath in files:
            try:
                content = Path(filepath).read_text(encoding="utf-8", errors="replace")
                domain = Path(filepath).suffix.lstrip(".")
                chunks = self._chunk_source_file(filepath, content)
                for chunk in chunks:
                    self._ingest_single(
                        chunk["doc_id"], chunk["content"],
                        source_file=filepath, domain=domain,
                        entity_name=chunk["entity_name"],
                        entity_kind=chunk["entity_kind"],
                        line_start=chunk["line_start"],
                        line_end=chunk["line_end"],
                    )
            except (IOError, OSError):
                pass

    def _ingest_single(
        self,
        doc_id: str,
        content: str,
        source_file: Optional[str] = None,
        domain: Optional[str] = None,
        entity_name: Optional[str] = None,
        entity_kind: Optional[str] = None,
        line_start: Optional[int] = None,
        line_end: Optional[int] = None,
    ) -> bool:
        """Ingest one document. Returns True if actually indexed (not skipped)."""
        chash = self._content_hash(content)
        existing = self._brain.execute(
            "SELECT content_hash FROM docs WHERE id = ?", (doc_id,)
        ).fetchone()
        if existing and existing["content_hash"] == chash:
            return False

        self._brain.execute(
            "INSERT OR REPLACE INTO docs "
            "(id, source_file, content, domain, content_hash, "
            " entity_name, entity_kind, line_start, line_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (doc_id, source_file, content, domain, chash,
             entity_name, entity_kind, line_start, line_end),
        )
        if self.vector_enabled:
            vec = self._embed(content)
            if vec is not None:
                import struct
                blob = struct.pack(f"{len(vec)}f", *vec)
                try:
                    self._brain.execute(
                        "DELETE FROM docs_vec WHERE doc_id = ?", (doc_id,)
                    )
                    self._brain.execute(
                        "INSERT INTO docs_vec (doc_id, embedding) VALUES (?, ?)",
                        (doc_id, blob),
                    )
                except Exception:
                    pass
        self._brain.commit()

        if source_file:
            self._index_graph(source_file, content)

        return True

    def _index_graph(self, filepath: str, content: str) -> None:
        """Extract entities and relationships from source and store in graph.db."""
        suffix = Path(filepath).suffix.lower()
        lang_name = _TS_LANG_MAP.get(suffix)
        parser = _get_treesitter_parser(lang_name) if lang_name else None

        if parser is not None:
            entities, relationships = self._extract_entities_treesitter(
                filepath, content, parser, suffix
            )
        else:
            entities = self._extract_entities(filepath, content)
            relationships = []

        for name, kind, line in entities:
            self._graph.execute(
                "INSERT OR IGNORE INTO entities (name, kind, file, line) "
                "VALUES (?, ?, ?, ?)",
                (name, kind, filepath, line),
            )

        # Ensure all relationship endpoint entities exist before inserting
        for src_name, tgt_name, relation in relationships:
            for ename in (src_name, tgt_name):
                self._graph.execute(
                    "INSERT OR IGNORE INTO entities (name, kind, file, line) "
                    "VALUES (?, ?, ?, ?)",
                    (ename, "unknown", filepath, 0),
                )
            row_src = self._graph.execute(
                "SELECT id FROM entities WHERE name = ? AND file = ? LIMIT 1",
                (src_name, filepath),
            ).fetchone()
            row_tgt = self._graph.execute(
                "SELECT id FROM entities WHERE name = ? LIMIT 1", (tgt_name,)
            ).fetchone()
            if row_src and row_tgt:
                self._graph.execute(
                    "INSERT OR IGNORE INTO relationships (source_id, target_id, relation) "
                    "VALUES (?, ?, ?)",
                    (row_src["id"], row_tgt["id"], relation),
                )

        self._graph.commit()

    @staticmethod
    def _extract_entities_treesitter(
        filepath: str, content: str, parser: object, suffix: str
    ) -> tuple[list[tuple[str, str, int]], list[tuple[str, str, str]]]:
        """Extract entities and relationships via tree-sitter AST.

        Returns:
            (entities, relationships) where:
              entities: list of (name, kind, line_number)
              relationships: list of (source_name, target_name, relation)
                relation in {'calls', 'imports', 'extends'}
        """
        entities: list[tuple[str, str, int]] = []
        relationships: list[tuple[str, str, str]] = []

        raw = content.encode("utf-8", errors="replace")
        tree = parser.parse(raw)  # type: ignore[attr-defined]
        root = tree.root_node
        file_stem = Path(filepath).stem

        if suffix == ".py":
            Brain._ts_extract_python(root, file_stem, entities, relationships)
        elif suffix in (".ts", ".tsx", ".js", ".jsx"):
            Brain._ts_extract_js_ts(root, file_stem, entities, relationships)
        elif suffix == ".cs":
            Brain._ts_extract_csharp(root, file_stem, entities, relationships)

        # File as module entity (always)
        if file_stem:
            entities.append((file_stem, "file", 0))

        return entities, relationships

    @staticmethod
    def _ts_collect_calls(
        node: object,
        container_name: str,
        relationships: list[tuple[str, str, str]],
        call_node_types: tuple[str, ...],
        call_name_extractor,  # callable(call_node) -> str | None
    ) -> None:
        """Walk node subtree collecting call relationships."""
        stack = [node]
        while stack:
            n = stack.pop()
            if n.type in call_node_types:  # type: ignore[attr-defined]
                callee = call_name_extractor(n)
                if callee:
                    relationships.append((container_name, callee, "calls"))
            stack.extend(reversed(n.children))  # type: ignore[attr-defined]

    @staticmethod
    def _ts_extract_python(
        root: object,
        file_stem: str,
        entities: list[tuple[str, str, int]],
        relationships: list[tuple[str, str, str]],
    ) -> None:
        """Extract Python entities and relationships from AST root."""

        def _py_call_name(node: object) -> Optional[str]:
            func = next(
                (c for c in node.children if c.type in ("identifier", "attribute")),  # type: ignore[attr-defined]
                None,
            )
            if func is None:
                return None
            if func.type == "identifier":
                return func.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
            # attribute: foo.bar -> take last identifier
            parts = [c for c in func.children if c.type == "identifier"]  # type: ignore[attr-defined]
            return parts[-1].text.decode("utf-8", errors="replace") if parts else None  # type: ignore[attr-defined]

        def _walk_top(node: object) -> None:
            for child in node.children:  # type: ignore[attr-defined]
                t = child.type
                if t == "import_statement":
                    # import os, sys
                    for name_node in child.children:  # type: ignore[attr-defined]
                        if name_node.type in ("dotted_name", "identifier"):
                            mod = name_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                            relationships.append((file_stem, mod, "imports"))
                elif t == "import_from_statement":
                    # from pathlib import Path
                    mod_node = next(
                        (c for c in child.children if c.type in ("dotted_name", "relative_import", "identifier")),  # type: ignore[attr-defined]
                        None,
                    )
                    if mod_node:
                        mod = mod_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                        relationships.append((file_stem, mod, "imports"))
                elif t == "class_definition":
                    name_node = next(
                        (c for c in child.children if c.type == "identifier"), None  # type: ignore[attr-defined]
                    )
                    if name_node:
                        cls_name = name_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                        line = name_node.start_point[0] + 1  # type: ignore[attr-defined]
                        entities.append((cls_name, "class", line))
                        # Extends: argument_list children that are identifiers
                        arg_list = next(
                            (c for c in child.children if c.type == "argument_list"), None  # type: ignore[attr-defined]
                        )
                        if arg_list:
                            for base in arg_list.children:  # type: ignore[attr-defined]
                                if base.type == "identifier":
                                    base_name = base.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                                    relationships.append((cls_name, base_name, "extends"))
                        # Methods inside class body
                        body = next(
                            (c for c in child.children if c.type == "block"), None  # type: ignore[attr-defined]
                        )
                        if body:
                            _walk_class_body(body, cls_name)
                elif t == "function_definition" or t == "decorated_definition":
                    _handle_func(child, file_stem)
                elif t == "block":
                    _walk_top(child)

        def _handle_func(node: object, container: str) -> None:
            fn_node = node if node.type == "function_definition" else next(  # type: ignore[attr-defined]
                (c for c in node.children if c.type == "function_definition"), None  # type: ignore[attr-defined]
            )
            if fn_node is None:
                return
            name_node = next(
                (c for c in fn_node.children if c.type == "identifier"), None  # type: ignore[attr-defined]
            )
            if name_node:
                fn_name = name_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                line = name_node.start_point[0] + 1  # type: ignore[attr-defined]
                entities.append((fn_name, "function", line))
                body = next((c for c in fn_node.children if c.type == "block"), None)  # type: ignore[attr-defined]
                if body:
                    Brain._ts_collect_calls(body, fn_name, relationships, ("call",), _py_call_name)

        def _walk_class_body(body: object, cls_name: str) -> None:
            for member in body.children:  # type: ignore[attr-defined]
                if member.type in ("function_definition", "decorated_definition"):
                    _handle_func(member, cls_name)

        _walk_top(root)

    @staticmethod
    def _ts_extract_js_ts(
        root: object,
        file_stem: str,
        entities: list[tuple[str, str, int]],
        relationships: list[tuple[str, str, str]],
    ) -> None:
        """Extract JS/TS entities and relationships from AST root."""

        def _js_call_name(node: object) -> Optional[str]:
            func = next(
                (c for c in node.children if c.type in ("identifier", "member_expression")),  # type: ignore[attr-defined]
                None,
            )
            if func is None:
                return None
            if func.type == "identifier":
                return func.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
            # member_expression: obj.method -> last identifier
            parts = [c for c in func.children if c.type in ("identifier", "property_identifier")]  # type: ignore[attr-defined]
            return parts[-1].text.decode("utf-8", errors="replace") if parts else None  # type: ignore[attr-defined]

        def _walk(node: object) -> None:
            for child in node.children:  # type: ignore[attr-defined]
                t = child.type
                if t == "import_statement":
                    src_node = next(
                        (c for c in child.children if c.type == "string"), None  # type: ignore[attr-defined]
                    )
                    if src_node:
                        raw_mod = src_node.text.decode("utf-8", errors="replace").strip("'\"")  # type: ignore[attr-defined]
                        mod = Path(raw_mod).stem if raw_mod.startswith(".") else raw_mod
                        relationships.append((file_stem, mod, "imports"))
                elif t in ("class_declaration", "class"):
                    _handle_class(child)
                elif t == "export_statement":
                    # export class / export function
                    inner = next(
                        (c for c in child.children if c.type in ("class_declaration", "function_declaration")),  # type: ignore[attr-defined]
                        None,
                    )
                    if inner:
                        if inner.type == "class_declaration":
                            _handle_class(inner)
                        else:
                            _handle_func(inner, file_stem)
                elif t == "function_declaration":
                    _handle_func(child, file_stem)
                elif t in ("lexical_declaration", "variable_declaration"):
                    _walk(child)
                elif t == "variable_declarator":
                    # const Foo = class { ... } or const fn = () => { ... }
                    inner = next(
                        (c for c in child.children if c.type in ("class", "arrow_function", "function")),  # type: ignore[attr-defined]
                        None,
                    )
                    if inner:
                        name_node = next((c for c in child.children if c.type == "identifier"), None)  # type: ignore[attr-defined]
                        if name_node:
                            nm = name_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                            if inner.type == "class":
                                entities.append((nm, "class", name_node.start_point[0] + 1))  # type: ignore[attr-defined]
                            else:
                                entities.append((nm, "function", name_node.start_point[0] + 1))  # type: ignore[attr-defined]

        def _handle_class(node: object) -> None:
            name_node = next(
                (c for c in node.children if c.type in ("identifier", "type_identifier")), None  # type: ignore[attr-defined]
            )
            if not name_node:
                return
            cls_name = name_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
            entities.append((cls_name, "class", name_node.start_point[0] + 1))  # type: ignore[attr-defined]
            heritage = next((c for c in node.children if c.type == "class_heritage"), None)  # type: ignore[attr-defined]
            if heritage:
                extends = next((c for c in heritage.children if c.type == "extends_clause"), None)  # type: ignore[attr-defined]
                if extends:
                    base = next(
                        (c for c in extends.children if c.type in ("identifier", "type_identifier")), None  # type: ignore[attr-defined]
                    )
                    if base:
                        relationships.append((cls_name, base.text.decode("utf-8", errors="replace"), "extends"))  # type: ignore[attr-defined]
            body = next((c for c in node.children if c.type == "class_body"), None)  # type: ignore[attr-defined]
            if body:
                for member in body.children:  # type: ignore[attr-defined]
                    if member.type == "method_definition":
                        mname_node = next(
                            (c for c in member.children if c.type in ("identifier", "property_identifier")), None  # type: ignore[attr-defined]
                        )
                        if mname_node:
                            mname = mname_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                            entities.append((mname, "method", mname_node.start_point[0] + 1))  # type: ignore[attr-defined]
                            mbody = next((c for c in member.children if c.type == "statement_block"), None)  # type: ignore[attr-defined]
                            if mbody:
                                Brain._ts_collect_calls(mbody, mname, relationships, ("call_expression",), _js_call_name)

        def _handle_func(node: object, container: str) -> None:
            name_node = next((c for c in node.children if c.type == "identifier"), None)  # type: ignore[attr-defined]
            if name_node:
                fn_name = name_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                entities.append((fn_name, "function", name_node.start_point[0] + 1))  # type: ignore[attr-defined]
                body = next((c for c in node.children if c.type == "statement_block"), None)  # type: ignore[attr-defined]
                if body:
                    Brain._ts_collect_calls(body, fn_name, relationships, ("call_expression",), _js_call_name)

        _walk(root)

    @staticmethod
    def _ts_extract_csharp(
        root: object,
        file_stem: str,
        entities: list[tuple[str, str, int]],
        relationships: list[tuple[str, str, str]],
    ) -> None:
        """Extract C# entities and relationships from AST root."""

        def _cs_call_name(node: object) -> Optional[str]:
            func = next(
                (c for c in node.children if c.type in ("identifier", "member_access_expression")),  # type: ignore[attr-defined]
                None,
            )
            if func is None:
                return None
            if func.type == "identifier":
                name = func.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                return None if name in _CS_FRAMEWORK_CALLS else name
            # Fluent-chain filter: `a.X().Y()` parses as an outer invocation
            # whose member_access receiver has an invocation_expression as
            # its first child. Those tail calls are almost always DSL
            # plumbing (builder-pattern config, LINQ pipelines).
            fc = func.children  # type: ignore[attr-defined]
            if fc and fc[0].type == "invocation_expression":
                return None
            parts = [c for c in fc if c.type == "identifier"]
            if not parts:
                return None
            name = parts[-1].text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
            return None if name in _CS_FRAMEWORK_CALLS else name

        def _walk(node: object) -> None:
            for child in node.children:  # type: ignore[attr-defined]
                t = child.type
                if t == "using_directive":
                    name_node = next(
                        (c for c in child.children if c.type in ("identifier", "qualified_name")), None  # type: ignore[attr-defined]
                    )
                    if name_node:
                        mod = name_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                        relationships.append((file_stem, mod, "imports"))
                elif t in ("namespace_declaration", "declaration_list", "compilation_unit"):
                    _walk(child)
                elif t == "class_declaration":
                    _handle_class(child)

        def _handle_class(node: object) -> None:
            name_node = next((c for c in node.children if c.type == "identifier"), None)  # type: ignore[attr-defined]
            if not name_node:
                return
            cls_name = name_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
            entities.append((cls_name, "class", name_node.start_point[0] + 1))  # type: ignore[attr-defined]
            base_list = next((c for c in node.children if c.type == "base_list"), None)  # type: ignore[attr-defined]
            if base_list:
                for base in base_list.children:  # type: ignore[attr-defined]
                    if base.type == "identifier":
                        relationships.append((cls_name, base.text.decode("utf-8", errors="replace"), "extends"))  # type: ignore[attr-defined]
            body = next((c for c in node.children if c.type == "declaration_list"), None)  # type: ignore[attr-defined]
            if body:
                for member in body.children:  # type: ignore[attr-defined]
                    if member.type == "method_declaration":
                        mname_node = next((c for c in member.children if c.type == "identifier"), None)  # type: ignore[attr-defined]
                        if mname_node:
                            mname = mname_node.text.decode("utf-8", errors="replace")  # type: ignore[attr-defined]
                            entities.append((mname, "method", mname_node.start_point[0] + 1))  # type: ignore[attr-defined]
                            mbody = next((c for c in member.children if c.type == "block"), None)  # type: ignore[attr-defined]
                            if mbody:
                                Brain._ts_collect_calls(mbody, mname, relationships, ("invocation_expression",), _cs_call_name)

        _walk(root)

    @staticmethod
    def _extract_entities(
        filepath: str, content: str
    ) -> list[tuple[str, str, int]]:
        """Extract (name, kind, line) from source content via regex (fallback)."""
        results: list[tuple[str, str, int]] = []
        for i, line in enumerate(content.splitlines(), start=1):
            # Python class
            m = re.match(r"^class\s+(\w+)", line)
            if m:
                results.append((m.group(1), "class", i))
                continue
            # Python def
            m = re.match(r"^(?:async\s+)?def\s+(\w+)", line)
            if m:
                results.append((m.group(1), "function", i))
                continue
            # JS/TS exported class
            m = re.match(r"^export\s+(?:default\s+)?class\s+(\w+)", line)
            if m:
                results.append((m.group(1), "class", i))
                continue
            # JS/TS exported function
            m = re.match(r"^export\s+(?:async\s+)?function\s+(\w+)", line)
            if m:
                results.append((m.group(1), "function", i))
                continue
        # File as module entity
        stem = Path(filepath).stem
        if stem:
            results.append((stem, "file", 0))
        return results

    # ------------------------------------------------------------------
    # FTS5 / vector / graph search
    # ------------------------------------------------------------------

    def _fts5_search(
        self,
        query: str,
        domain: Optional[str],
        limit: int,
        domains: Optional[list[str]] = None,
    ) -> list[dict]:
        safe = re.sub(r"[^\w\s]", " ", query).strip()
        if not safe:
            return []
        try:
            # Multi-domain list takes precedence over single domain.
            if domains:
                placeholders = ",".join("?" * len(domains))
                rows = self._brain.execute(
                    f"SELECT id, bm25(docs_fts) AS score FROM docs_fts "
                    f"WHERE docs_fts MATCH ? AND domain IN ({placeholders}) "
                    f"ORDER BY score, id LIMIT ?",
                    (safe, *domains, limit),
                ).fetchall()
            elif domain:
                rows = self._brain.execute(
                    "SELECT id, bm25(docs_fts) AS score FROM docs_fts "
                    "WHERE docs_fts MATCH ? AND domain = ? ORDER BY score, id LIMIT ?",
                    (safe, domain, limit),
                ).fetchall()
            else:
                rows = self._brain.execute(
                    "SELECT id, bm25(docs_fts) AS score FROM docs_fts "
                    "WHERE docs_fts MATCH ? ORDER BY score, id LIMIT ?",
                    (safe, limit),
                ).fetchall()
            return [{"doc_id": r["id"], "score": -r["score"]} for r in rows]
        except Exception:
            return []

    def _vector_search(
        self,
        query: str,
        domain: Optional[str],
        limit: int,
        domains: Optional[list[str]] = None,
    ) -> list[dict]:
        if not self.vector_enabled:
            return []
        vec = self._embed(query)
        if vec is None:
            return []
        try:
            import struct
            blob = struct.pack(f"{len(vec)}f", *vec)
            # sqlite-vec vec0 doesn't support WHERE on non-vec columns,
            # so over-fetch by 3x when domain filtering is needed, then
            # post-filter by joining doc_id back to the docs table.
            need_filter = bool(domains or domain)
            fetch_limit = limit * 3 if need_filter else limit
            rows = self._brain.execute(
                "SELECT doc_id, distance FROM docs_vec "
                "WHERE embedding MATCH ? AND k = ?",
                (blob, fetch_limit),
            ).fetchall()
            results = [
                {"doc_id": r["doc_id"], "score": 1.0 / (1.0 + r["distance"])}
                for r in rows
            ]
            # Multi-domain list takes precedence over single domain.
            if domains and results:
                doc_ids = [r["doc_id"] for r in results]
                placeholders_ids = ",".join("?" * len(doc_ids))
                placeholders_dom = ",".join("?" * len(domains))
                domain_rows = self._brain.execute(
                    f"SELECT id FROM docs WHERE id IN ({placeholders_ids}) "
                    f"AND domain IN ({placeholders_dom})",
                    (*doc_ids, *domains),
                ).fetchall()
                allowed = {r["id"] for r in domain_rows}
                results = [r for r in results if r["doc_id"] in allowed][:limit]
            elif domain and results:
                doc_ids = [r["doc_id"] for r in results]
                placeholders = ",".join("?" * len(doc_ids))
                domain_rows = self._brain.execute(
                    f"SELECT id FROM docs WHERE id IN ({placeholders}) AND domain = ?",
                    (*doc_ids, domain),
                ).fetchall()
                allowed = {r["id"] for r in domain_rows}
                results = [r for r in results if r["doc_id"] in allowed][:limit]
            return results
        except Exception:
            return []

    def _graph_search(self, query: str, limit: int) -> list[dict]:
        entity_name, relation = _detect_structural_query(query)
        if entity_name:
            return self._traverse_graph(entity_name, relation, limit)

        tokens = [t for t in re.split(r"\W+", query) if len(t) > 3]
        if not tokens:
            return []
        seen: set[str] = set()
        results: list[dict] = []
        for token in tokens[:8]:
            try:
                rows = self._graph.execute(
                    "SELECT DISTINCT file, name FROM entities "
                    "WHERE name LIKE ? LIMIT ?",
                    (f"%{token}%", limit),
                ).fetchall()
                for r in self._resolve_graph_doc_ids(rows):
                    if r["doc_id"] not in seen:
                        seen.add(r["doc_id"])
                        results.append(r)
            except Exception:
                pass
        return results[:limit]

    def _resolve_graph_doc_ids(self, rows) -> list[dict]:
        """Map graph.db entity rows (file, name) onto docs.id rows.

        entities.file is graph.db's own id space (a bare path); the space
        BM25/vector/RRF fuse on is docs.id = "<file>::<name>". Only ids
        with a real docs row are returned, so the graph leg never surfaces
        a pseudo-id that nothing downstream can score (task e58543b8).
        """
        candidates: list[str] = []
        for r in rows:
            if r["file"] and r["name"]:
                cid = f"{r['file']}::{r['name']}"
                if cid not in candidates:
                    candidates.append(cid)
        if not candidates:
            return []
        placeholders = ",".join("?" * len(candidates))
        present = {
            row["id"] for row in self._brain.execute(
                f"SELECT id FROM docs WHERE id IN ({placeholders})", candidates
            ).fetchall()
        }
        return [{"doc_id": c, "score": 1.0} for c in candidates if c in present]

    def _traverse_graph(
        self, entity_name: str, relation: Optional[str], limit: int
    ) -> list[dict]:
        try:
            ent = self._graph.execute(
                "SELECT id FROM entities WHERE name = ? LIMIT 1", (entity_name,)
            ).fetchone()
            if not ent:
                return []
            eid = ent["id"]
            if relation:
                rows = self._graph.execute(
                    "SELECT e.file, e.name FROM relationships r "
                    "JOIN entities e ON e.id = r.target_id "
                    "WHERE r.source_id = ? AND r.relation = ? LIMIT ?",
                    (eid, relation, limit),
                ).fetchall()
            else:
                rows = self._graph.execute(
                    "SELECT e.file, e.name FROM relationships r "
                    "JOIN entities e ON e.id = r.target_id "
                    "WHERE r.source_id = ? LIMIT ?",
                    (eid, limit),
                ).fetchall()
            return self._resolve_graph_doc_ids(rows)
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Public search API
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        domain: Optional[str] = None,
        limit: int = 5,
        domains: Optional[list[str]] = None,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> list[dict]:
        """3-index hybrid search with RRF fusion.

        Auto-bootstraps on first call when the index is empty, and runs
        incremental_reindex() on subsequent calls to stay current.

        Args:
            query: Search query string.
            domain: Single domain filter (e.g. 'py', 'expertise').
            limit: Maximum results to return.
            domains: Multi-domain filter list; takes precedence over ``domain``.
                     When provided, results are restricted to docs whose domain
                     is in this list (e.g. ['expertise', 'md'] for SM persona).
            session_id: Asking session, logged to searches for attribution.
            task_id: Task this retrieval served, logged for attribution.
        """
        # NOTE: Auto-bootstrap disabled for service mode.
        # In CLI mode the Brain auto-ingests CWD on first search.
        # In service mode, documents are indexed via brain_index_doc MCP tool.
        # The old CLI auto-ingest would index /app (the container code) which is wrong.
        import os as _os
        import time as _time

        _search_t0 = _time.perf_counter()

        # Experimental: PRISM_SEARCH_MODE controls which indices contribute.
        #   hybrid (default) = BM25 + vector + graph, fused via RRF
        #   vector           = vector search only (when vector_enabled)
        #   bm25             = BM25 only
        mode = _os.environ.get("PRISM_SEARCH_MODE", "hybrid").strip().lower()

        # PRISM_CHUNK_AGG (default on): collapse same-source_file hits to the
        # single best-ranked chunk per file so multi-granular chunking doesn't
        # crowd top-K with __file__/__module__/func_X variants of one file.
        aggregate = (
            _os.environ.get("PRISM_CHUNK_AGG", "on").strip().lower() != "off"
        )

        # When aggregating we over-fetch from each sub-index and from the
        # fused list so there are enough candidates left after dedupe.
        inner = limit * 6 if aggregate else limit * 2

        # QUERY DECOMPOSITION WAS REMOVED HERE (task 19e4e7f7, PLAT-0042
        # retired). PRISM_QUERY_DECOMP shipped defaulting to "off" and was
        # measured on three independent corpora before being deleted rather
        # than left off forever, per the owner's rule that a flag may exist so
        # a user can turn something OFF, never so they can turn something ON:
        #
        #   PocketBase code search   n=115  r@5 -0.0014  McNemar p=1.0
        #   FullStackHero code search n=119 r@5 +0.0042  McNemar p=1.0
        #   LongMemEval questions    n=120  r@5 -0.0167  McNemar p=0.7266
        #
        # LongMemEval is the test it deserved -- 66% of those questions
        # decomposed, against 21% of the commit subjects -- and it still lost.
        # The mechanism is why: with no connective, "decomposition" was a blind
        # MIDPOINT SPLIT of any query over 12 tokens, so "How many amateur
        # comedians did I watch perform at the open mic night?" became "How
        # many amateur comedians did I" + "watch perform at the open mic
        # night?". Those fragments do widen the candidate pool (pool_recall@50
        # +0.0333) and then dilute the ranking, which is exactly the shape of
        # the loss: more gold in the pool, less gold in the top 5.
        #
        # Worth keeping if anyone revisits this: the POOL gain was real. A
        # decomposer that splits on meaning rather than on token count could
        # convert it. The rules-based v1 could not.
        if mode == "vector" and self.vector_enabled:
            fused = self._vector_search(query, domain, inner, domains=domains)
        elif mode == "bm25":
            fused = self._fts5_search(query, domain, inner, domains=domains)
        else:
            bm25 = self._fts5_search(query, domain, inner, domains=domains)
            vec = (self._vector_search(query, domain, inner, domains=domains)
                   if self.vector_enabled else [])
            graph = self._graph_search(query, inner)
            fused = reciprocal_rank_fusion(
                [bm25, vec, graph] if self.vector_enabled else [bm25, graph]
            )

        # Cross-encoder reranker (PRISM_RERANK=auto|bge-v2|jina-v2|
        # ms-marco-minilm|off). Rescores the top PRISM_RERANK_TOPN candidates
        # by feeding (query, chunk_content) pairs through a cross-encoder,
        # then replaces that slice of ``fused`` with the reranked order.
        # DEFAULT auto (task 19e4e7f7): on wherever it can run, off where it
        # cannot, never a capability the user has to know an env var to reach.
        rerank_preset = (
            _os.environ.get("PRISM_RERANK", "auto").strip().lower()
        )
        if rerank_preset == "auto":
            rerank_preset = _auto_rerank_preset()
        if rerank_preset not in ("", "off", "none") and fused:
            try:
                pool_n = int(_os.environ.get("PRISM_RERANK_TOPN", "50"))
            except ValueError:
                pool_n = 50
            # PRISM_RERANK_TOPN is a CAP, not a floor (task 19e4e7f7). It used
            # to be raised to ``inner`` (= limit*6 when aggregating), so asking
            # for 50 silently reranked 120 at limit=20 and the only knob for
            # the single most expensive step in search did nothing. Candidates
            # past the cap keep their RRF order, which is the ordinary
            # rerank-a-pool design; the cap is what makes the cost bounded and
            # therefore defaultable.
            pool_n = max(1, min(pool_n, len(fused)))
            pool = fused[:pool_n]
            reranked = self._rerank_candidates(query, pool, rerank_preset)
            if reranked is not None:
                fused = reranked + fused[pool_n:]

        # PRISM_FEEDBACK_WEIGHT (default 0.002; "off" disables): close the
        # feedback loop by nudging rrf_score by accumulated past thumbs on
        # each doc_id. Small weight so a single vote doesn't flip ordering —
        # ~3 consistent thumbs overcome a typical RRF gap.
        fb_weight_env = _os.environ.get("PRISM_FEEDBACK_WEIGHT", "0.002")
        try:
            fb_weight = 0.0 if fb_weight_env.strip().lower() in (
                "off", "none", ""
            ) else float(fb_weight_env)
        except ValueError:
            fb_weight = 0.0
        if fb_weight and fused:
            fb_scores = self.get_feedback_scores(
                [c["doc_id"] for c in fused[:200]]
            )
            if fb_scores:
                for c in fused:
                    adj = fb_scores.get(c["doc_id"], 0.0)
                    if adj:
                        c["rrf_score"] = (
                            c.get("rrf_score", 0.0) + fb_weight * adj
                        )
                        c["feedback_adj"] = adj
                fused = sorted(fused, key=lambda x: (
                    -x.get("rrf_score", 0.0), x.get("doc_id", ""),
                ))

        # Take a larger candidate pool when aggregating so collapsing doesn't
        # leave us short of ``limit`` results.
        top = fused[: inner if aggregate else limit]
        if not top:
            return []

        ids = [item["doc_id"] for item in top]
        placeholders = ",".join("?" * len(ids))
        rows = self._brain.execute(
            f"SELECT id, source_file, content, domain, entity_name, entity_kind, "
            f"line_start, line_end "
            f"FROM docs WHERE id IN ({placeholders})",
            ids,
        ).fetchall()
        content_map = {r["id"]: r for r in rows}

        results: list[dict] = []
        seen_files: set[str] = set()
        for item in top:
            row = content_map.get(item["doc_id"])
            if not row:
                continue
            if aggregate:
                # Use source_file as the dedupe key; fall back to doc_id for
                # rows without one (legacy expertise/memory domain docs).
                group_key = row["source_file"] or item["doc_id"]
                if group_key in seen_files:
                    continue
                seen_files.add(group_key)
            results.append({
                "doc_id": item["doc_id"],
                "source_file": row["source_file"],
                "content": row["content"],
                "domain": row["domain"],
                "entity_name": row["entity_name"],
                "entity_kind": row["entity_kind"],
                "line_start": row["line_start"],
                "line_end": row["line_end"],
                "rrf_score": item.get("rrf_score", 0.0),
                "rerank_score": item.get("rerank_score"),
                "feedback_adj": item.get("feedback_adj"),
            })
            if len(results) >= limit:
                break
        search_id = self._log_search(
            query=query,
            domain=domain,
            domains=domains,
            mode=mode,
            rerank=rerank_preset,
            context_prefix=_os.environ.get(
                "PRISM_CONTEXT_PREFIX", "on"
            ).strip().lower() != "off",
            chunk_agg=aggregate,
            limit_requested=limit,
            results=results,
            latency_ms=int((_time.perf_counter() - _search_t0) * 1000),
            session_id=session_id,
            task_id=task_id,
        )
        if search_id is not None:
            for r in results:
                r["search_id"] = search_id
        return results

    def _log_search(
        self,
        *,
        query: str,
        domain: Optional[str],
        domains: Optional[list[str]],
        mode: str,
        rerank: str,
        context_prefix: bool,
        chunk_agg: bool,
        limit_requested: int,
        results: list[dict],
        latency_ms: int,
        session_id: Optional[str] = None,
        task_id: Optional[str] = None,
    ) -> Optional[int]:
        """Persist one search event to the ``searches`` table.

        Returns the new row id (used by search() to stamp each result with a
        ``search_id`` so feedback can be tied back later). ``session_id`` /
        ``task_id`` attribute the retrieval to the session that asked and the
        task it served (both optional — legacy callers pass neither). Silent on
        failure — observability must never break retrieval.
        """
        try:
            import json as _json
            final_top = _json.dumps([
                {
                    "doc_id": r.get("doc_id"),
                    "rrf_score": r.get("rrf_score"),
                    "rerank_score": r.get("rerank_score"),
                    "domain": r.get("domain"),
                    "entity_name": r.get("entity_name"),
                }
                for r in results
            ])
            cur = self._brain.execute(
                "INSERT INTO searches (query, domain, domains, mode, rerank, "
                "context_prefix, chunk_agg, limit_requested, n_results, "
                "latency_ms, final_top, session_id, task_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    query, domain,
                    _json.dumps(domains) if domains else None,
                    mode, rerank or "off",
                    1 if context_prefix else 0,
                    1 if chunk_agg else 0,
                    limit_requested, len(results), latency_ms, final_top,
                    session_id, task_id,
                ),
            )
            self._brain.commit()
            return cur.lastrowid
        except Exception:
            return None

    def get_recent_searches(self, limit: int = 50) -> list[dict]:
        """Return the last ``limit`` search events, newest first.

        Each row is augmented with ``up_count`` and ``down_count`` aggregated
        from the ``search_feedback`` table so the UI can surface sentiment
        without a second round-trip.
        """
        try:
            rows = self._brain.execute(
                "SELECT s.id, s.ts, s.query, s.domain, s.domains, s.mode, "
                "s.rerank, s.context_prefix, s.chunk_agg, s.limit_requested, "
                "s.n_results, s.latency_ms, s.final_top, "
                "s.session_id, s.task_id, "
                "COALESCE(SUM(CASE WHEN f.signal='up' THEN 1 ELSE 0 END), 0) "
                "    AS up_count, "
                "COALESCE(SUM(CASE WHEN f.signal='down' THEN 1 ELSE 0 END), 0) "
                "    AS down_count "
                "FROM searches s "
                "LEFT JOIN search_feedback f ON f.search_id = s.id "
                "GROUP BY s.id "
                "ORDER BY s.id DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def record_search_feedback(
        self,
        search_id: int,
        doc_id: str,
        signal: str,
        note: Optional[str] = None,
    ) -> Optional[int]:
        """Record a thumbs-up / thumbs-down on one doc from a prior search.

        Returns the new feedback row id, or None if the insert failed (e.g.
        unknown search_id, malformed signal). Only 'up' and 'down' signals
        are accepted.
        """
        if signal not in ("up", "down"):
            return None
        try:
            cur = self._brain.execute(
                "INSERT INTO search_feedback (search_id, doc_id, signal, note) "
                "VALUES (?, ?, ?, ?)",
                (int(search_id), doc_id, signal, note),
            )
            self._brain.commit()
            return cur.lastrowid
        except Exception:
            return None

    def get_search_feedback(self, search_id: int) -> list[dict]:
        """Return all feedback rows tied to ``search_id``."""
        try:
            rows = self._brain.execute(
                "SELECT id, search_id, doc_id, signal, note, ts "
                "FROM search_feedback WHERE search_id = ? ORDER BY id",
                (int(search_id),),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def get_feedback_scores(
        self,
        doc_ids: list[str],
        cap: float = 5.0,
        decay_days: int = 30,
    ) -> dict:
        """Return a net signal per doc_id for the consumption layer.

        net = SUM(up) - SUM(down), clamped to [-cap, +cap]. Rows older
        than ``decay_days`` get weight 0.3 so ancient feedback decays
        rather than dominating. Silent on error — retrieval must keep
        working even if feedback data is weird.
        """
        if not doc_ids:
            return {}
        try:
            placeholders = ",".join("?" * len(doc_ids))
            rows = self._brain.execute(
                f"SELECT doc_id, signal, ts FROM search_feedback "
                f"WHERE doc_id IN ({placeholders})",
                list(doc_ids),
            ).fetchall()
        except Exception:
            return {}
        if not rows:
            return {}
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        out: dict[str, float] = {}
        for r in rows:
            try:
                ts = datetime.fromisoformat(
                    (r["ts"] or "").replace(" ", "T")
                )
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                age_days = (now - ts).days
            except Exception:
                age_days = 0
            w = 0.3 if age_days > decay_days else 1.0
            delta = w if r["signal"] == "up" else (-w if r["signal"] == "down" else 0)
            out[r["doc_id"]] = out.get(r["doc_id"], 0.0) + delta
        # Clamp to [-cap, +cap]
        for k in list(out):
            out[k] = max(-cap, min(cap, out[k]))
        return out

    def feedback_stats(self) -> dict:
        """Aggregate thumbs up/down counts and per-doc win rates."""
        try:
            total = self._brain.execute(
                "SELECT signal, COUNT(*) AS n FROM search_feedback "
                "GROUP BY signal"
            ).fetchall()
            counts = {r["signal"]: r["n"] for r in total}
            worst = self._brain.execute(
                "SELECT doc_id, "
                "  SUM(CASE WHEN signal='down' THEN 1 ELSE 0 END) AS downs, "
                "  SUM(CASE WHEN signal='up' THEN 1 ELSE 0 END) AS ups, "
                "  COUNT(*) AS total "
                "FROM search_feedback GROUP BY doc_id "
                "HAVING downs > ups ORDER BY downs DESC LIMIT 10"
            ).fetchall()
        except Exception:
            return {"up": 0, "down": 0, "worst": []}
        return {
            "up": int(counts.get("up", 0)),
            "down": int(counts.get("down", 0)),
            "worst": [dict(r) for r in worst],
        }

    def _rerank_candidates(
        self, query: str, candidates: list[dict], preset: str,
    ) -> Optional[list[dict]]:
        """Rescore ``candidates`` with a cross-encoder and return new order.

        Returns None when the reranker is unavailable so the caller falls
        back to RRF order. Attaches a ``rerank_score`` field to each
        returned item. Caps each document at 2048 chars to keep the
        cross-encoder under its input limit.
        """
        if not candidates:
            return None
        reranker = _load_reranker(preset)
        if reranker is None:
            return None
        ids = [c["doc_id"] for c in candidates]
        placeholders = ",".join("?" * len(ids))
        rows = self._brain.execute(
            f"SELECT id, content FROM docs WHERE id IN ({placeholders})", ids,
        ).fetchall()
        content_by_id = {r["id"]: r["content"] for r in rows}
        pairs: list[tuple[str, str]] = []
        ordered: list[dict] = []
        for c in candidates:
            text = content_by_id.get(c["doc_id"])
            if not text:
                continue
            pairs.append((query, text[:2048]))
            ordered.append(c)
        if not pairs:
            return None
        try:
            scores = reranker.predict(pairs)
        except Exception as e:
            print(f"Brain: reranker predict failed: {e!r}", file=sys.stderr)
            return None
        scored: list[dict] = []
        for c, s in zip(ordered, scores):
            c2 = dict(c)
            c2["rerank_score"] = float(s)
            scored.append(c2)
        scored.sort(key=lambda x: -x["rerank_score"])
        return scored

    def system_context(
        self,
        story_file: Optional[str] = None,
        persona: Optional[str] = None,
        limit: int = 8,
    ) -> str:
        """Run hybrid search from story/persona context and return formatted block.

        When ``persona`` matches a known PRISM role (sm/qa/dev/po/architect/engineer),
        search results are filtered to role-relevant domains via ROLE_DOMAIN_MAP.
        If the filtered search yields no results, falls back to unfiltered search.
        """
        query = ""
        if story_file:
            try:
                query = Path(story_file).read_text(
                    encoding="utf-8", errors="replace"
                )[:1000]
            except (IOError, OSError):
                pass
        if not query and persona:
            query = persona
        if not query:
            return ""

        # Resolve role-specific domain filter from persona.
        role_domains: Optional[list[str]] = None
        if persona:
            role_key = persona.lower().strip()
            role_domains = self.ROLE_DOMAIN_MAP.get(role_key)

        results = self.search(query, limit=limit, domains=role_domains)
        # If role-filtered search returned nothing, fall back to unfiltered.
        if not results and role_domains:
            results = self.search(query, limit=limit)
        if not results:
            self.last_result_count = 0
            return ""

        # A single-index exact hit has RRF ~= 1 / (60 + 1) == 0.01639.
        # The previous 0.02 cutoff dropped valid BM25-only context, which
        # made context_bundle lose role-specific Brain material in small or
        # fresh projects. Search ranking already happens before this point;
        # for system context, keep any positively scored top-K result.
        results = [r for r in results if r.get("rrf_score", 0.0) > 0.0]
        if not results:
            self.last_result_count = 0
            return ""

        self.last_result_count = len(results)
        parts = ["<brain_context>"]
        for i, r in enumerate(results, 1):
            parts.append(f"[{i}] {r['doc_id']}")
            parts.append(r["content"][:600])
            parts.append("")
        parts.append("</brain_context>")
        return "\n".join(parts)

    # ------------------------------------------------------------------
    # Graph API
    # ------------------------------------------------------------------

    def graph_query(
        self,
        entity: str,
        relation: Optional[str] = None,
        limit: int = 10,
        include_rationale: bool = False,
    ) -> list[dict]:
        """Traverse entity relationships and return related entities.

        ``include_rationale`` defaults to False so rationale nodes
        (graphify-extracted ``# WHY:`` / ``# HACK:`` / ``# NOTE:``
        comments stored as ``kind='rationale'``) don't pollute graph
        traversal results — they account for ~43% of nodes in a typical
        graph and answer different questions than code-flow traversal.
        Pass ``True`` to surface them when intent metadata is the goal.
        """
        ent_row = self._graph.execute(
            "SELECT id FROM entities WHERE name = ? LIMIT 1", (entity,)
        ).fetchone()
        if not ent_row:
            return []
        eid = ent_row["id"]
        rat_clause = "" if include_rationale else (
            " AND COALESCE(e.kind,'') != 'rationale'"
        )
        try:
            if relation:
                rows = self._graph.execute(
                    "SELECT e.name, e.kind, e.file, r.relation "
                    "FROM relationships r "
                    "JOIN entities e ON e.id = r.target_id "
                    f"WHERE r.source_id = ? AND r.relation = ?{rat_clause} "
                    "LIMIT ?",
                    (eid, relation, limit),
                ).fetchall()
            else:
                rows = self._graph.execute(
                    "SELECT e.name, e.kind, e.file, r.relation "
                    "FROM relationships r "
                    "JOIN entities e ON e.id = r.target_id "
                    f"WHERE r.source_id = ?{rat_clause} LIMIT ?",
                    (eid, limit),
                ).fetchall()
            return [
                {"name": r["name"], "kind": r["kind"],
                 "file": r["file"], "relation": r["relation"]}
                for r in rows
            ]
        except Exception:
            return []

    # ------------------------------------------------------------------
    # Semantic chunk accessors (token-efficient alternatives to file Read)
    # ------------------------------------------------------------------

    def find_symbol(
        self,
        name: str,
        kind: Optional[str] = None,
        limit: int = 10,
    ) -> list[dict]:
        """Return chunks whose entity_name matches ``name``.

        Optional ``kind`` filter (function/class/method/etc). Returns the
        full chunk content so Claude can read a bounded semantic unit
        instead of loading the whole parent file.
        """
        try:
            if kind:
                rows = self._brain.execute(
                    "SELECT id, source_file, content, entity_name, "
                    "entity_kind, line_start, line_end FROM docs "
                    "WHERE entity_name = ? AND entity_kind = ? "
                    "ORDER BY source_file, line_start LIMIT ?",
                    (name, kind, int(limit)),
                ).fetchall()
            else:
                rows = self._brain.execute(
                    "SELECT id, source_file, content, entity_name, "
                    "entity_kind, line_start, line_end FROM docs "
                    "WHERE entity_name = ? "
                    "ORDER BY source_file, line_start LIMIT ?",
                    (name, int(limit)),
                ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def outline(self, source_file: str) -> list[dict]:
        """Return the symbol outline of a file — metadata only, no bodies.

        For a ~2500-line file this drops the read cost from ~15K tokens
        (whole-file Read) to ~200 tokens (one line per entity).
        """
        try:
            rows = self._brain.execute(
                "SELECT entity_name, entity_kind, line_start, line_end "
                "FROM docs WHERE source_file = ? "
                "AND entity_kind NOT IN ('window', 'file') "
                "AND entity_name NOT IN ('__file__', '__module__') "
                "ORDER BY line_start",
                (source_file,),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def find_references(
        self, name: str, limit: int = 20,
        include_rationale: bool = False,
    ) -> list[dict]:
        """Return call sites referencing ``name`` via the graph.

        Looks up ``name`` in graph.db entities, then finds inbound
        relationships. For each caller, returns its name/kind/file and
        the relation type. No chunk body — use find_symbol() on the
        returned caller names for content.

        ``include_rationale`` defaults to False so ``rationale_for``
        edges (rationale-comment → entity-it-explains) don't show up
        as fake "callers". Pass True when looking for intent metadata
        attached to ``name``.
        """
        try:
            tgt = self._graph.execute(
                "SELECT id FROM entities WHERE name = ? LIMIT 1", (name,),
            ).fetchone()
            if not tgt:
                return []
            rat_clause = "" if include_rationale else (
                " AND COALESCE(e.kind,'') != 'rationale'"
            )
            rows = self._graph.execute(
                "SELECT e.name AS caller_name, e.kind AS caller_kind, "
                "e.file AS caller_file, r.relation AS relation "
                "FROM relationships r "
                "JOIN entities e ON e.id = r.source_id "
                f"WHERE r.target_id = ?{rat_clause} LIMIT ?",
                (tgt["id"], int(limit)),
            ).fetchall()
        except Exception:
            return []
        return [dict(r) for r in rows]

    def call_chain(
        self,
        entity: str,
        depth: int = 2,
        limit: int = 50,
        relation: str | list[str] | tuple[str, ...] | None = "calls",
        direction: str = "callees",
    ) -> list[dict]:
        """Bounded BFS on the relationships graph starting at ``entity``.

        Returns a flat list of edges [{from, to, kind, relation, hop,
        direction}] so the caller can reconstruct either tree or flat
        views. Hop 1 is direct neighbours; hop 2 is neighbours-of-
        neighbours; etc.

        ``relation`` filters edges by their relation kind. Default is
        ``"calls"`` so structural edges (``contains``/``method``/``uses``
        /``imports_from``) don't eat the depth+limit budget. Pass
        ``None`` or ``"*"`` for every kind; list/tuple for several kinds.

        ``direction`` controls traversal:
          * ``"callees"`` (default) — walk forward on source_id IN frontier;
            answers "what does ``entity`` transitively call?"
          * ``"callers"`` — walk backward on target_id IN frontier; the
            blast-radius primitive — answers "who would break if I change
            ``entity``?"
          * ``"both"`` — union of the two; useful for impact analysis
            that needs both upstream and downstream edges in one query.

        Each edge carries its ``direction`` so callers of "both" can
        partition the result.
        """
        # Normalize the relation filter into a list of allowed kinds
        # (or None meaning "no filter").
        allowed: list[str] | None
        if relation is None or relation == "*" or relation == "":
            allowed = None
        elif isinstance(relation, str):
            allowed = [relation]
        else:
            allowed = [str(r) for r in relation if r]
            if not allowed:
                allowed = None

        # Normalize direction; tolerate plurals and casing.
        dir_norm = (direction or "callees").lower().strip()
        if dir_norm in ("callee", "down", "forward", "out"):
            dir_norm = "callees"
        elif dir_norm in ("caller", "up", "reverse", "back", "in",
                          "blast", "blast_radius"):
            dir_norm = "callers"
        elif dir_norm in ("bidirectional", "all", "either"):
            dir_norm = "both"
        if dir_norm not in ("callees", "callers", "both"):
            dir_norm = "callees"

        try:
            start = self._graph.execute(
                "SELECT id, name FROM entities WHERE name = ? LIMIT 1",
                (entity,),
            ).fetchone()
            # AC4: fuzzy fallback via norm_label so 'Brain.search()',
            # 'Brain.search', and 'brain_search' all resolve to the same
            # entity. norm_label is graphify-emitted (or derived during
            # _import_graph_json), and the column may be absent on
            # pre-AC4 graphs — wrap in try/except.
            if not start:
                try:
                    from prism_service.services.graph_service import (
                        _derive_norm_label as _norm,
                    )
                    needle = _norm(entity)
                    if needle:
                        start = self._graph.execute(
                            "SELECT id, name FROM entities "
                            "WHERE norm_label = ? LIMIT 1",
                            (needle,),
                        ).fetchone()
                except Exception:
                    start = None
            if not start:
                return []
            edges: list[dict] = []
            seen_edges: set[tuple[int, int, str]] = set()
            directions = (
                ["callees", "callers"] if dir_norm == "both" else [dir_norm]
            )
            for one_dir in directions:
                self._walk_chain(
                    start_id=start["id"], depth=depth, limit=limit,
                    allowed=allowed, direction=one_dir,
                    edges=edges, seen_edges=seen_edges,
                )
            return edges
        except Exception:
            return []

    def _walk_chain(
        self,
        *,
        start_id: int,
        depth: int,
        limit: int,
        allowed: list[str] | None,
        direction: str,
        edges: list[dict],
        seen_edges: set[tuple[int, int, str]],
    ) -> None:
        """One-direction BFS used by call_chain.

        For ``direction='callees'`` the frontier is on source_id and we
        advance via target_id (forward call flow). For ``'callers'`` it
        flips: frontier on target_id, advance via source_id (reverse).
        Edges are appended to the shared ``edges`` list; ``seen_edges``
        de-dupes when called twice (direction='both' case).
        """
        # Pivot column the frontier matches against, and the column to
        # advance to next hop.
        if direction == "callers":
            frontier_col = "r.target_id"
            advance_col = "src_id"
        else:
            frontier_col = "r.source_id"
            advance_col = "tgt_id"
        visited = {start_id}
        frontier = [start_id]
        for hop in range(1, max(1, int(depth)) + 1):
            if not frontier or len(edges) >= limit:
                break
            placeholders = ",".join("?" * len(frontier))
            sql = (
                "SELECT r.source_id AS src_id, "
                "s.name AS src_name, t.name AS tgt_name, "
                "t.kind AS tgt_kind, t.id AS tgt_id, "
                "s.kind AS src_kind, "
                "r.relation AS relation, r.target_id AS tgt_id_raw, "
                "r.confidence AS confidence, "
                "r.confidence_score AS confidence_score, "
                "r.call_site_file AS call_site_file, "
                "r.source_location AS call_site_location "
                "FROM relationships r "
                "JOIN entities s ON s.id = r.source_id "
                "JOIN entities t ON t.id = r.target_id "
                f"WHERE {frontier_col} IN ({placeholders})"
            )
            params: list = [*frontier]
            if allowed is not None:
                rel_placeholders = ",".join("?" * len(allowed))
                sql += f" AND r.relation IN ({rel_placeholders})"
                params.extend(allowed)
            sql += " LIMIT ?"
            params.append(int(limit) - len(edges))
            rows = self._graph.execute(sql, params).fetchall()
            next_frontier: list[int] = []
            for r in rows:
                key = (r["src_id"], r["tgt_id_raw"], r["relation"])
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                # 'from' / 'to' always reflect the underlying call edge
                # direction (caller → callee), regardless of traversal
                # direction. The 'direction' field marks how this edge
                # was discovered — useful when the caller passed
                # direction='both' and wants to partition results.
                # confidence_score may be NULL for edges from the
                # legacy tree-sitter pass (graphify started populating
                # it in v0.4.x). Coerce to a float so the API stays
                # uniform; treat missing as 1.0 (extracted-with-no-doubt
                # is the conservative interpretation for legacy edges).
                conf_raw = r["confidence_score"]
                conf_score = (
                    float(conf_raw) if conf_raw is not None else 1.0
                )
                edges.append({
                    "from": r["src_name"], "to": r["tgt_name"],
                    "kind": r["tgt_kind"] if direction == "callees"
                    else r["src_kind"],
                    "relation": r["relation"],
                    "confidence": r["confidence"] or "EXTRACTED",
                    "confidence_score": conf_score,
                    # AC5: per-edge call-site location. Empty string
                    # for legacy edges that predate the column.
                    "call_site_file": r["call_site_file"] or "",
                    "call_site_location": r["call_site_location"] or "",
                    "hop": hop,
                    "direction": direction,
                })
                advance_id = r[advance_col]
                if advance_id not in visited:
                    visited.add(advance_id)
                    next_frontier.append(advance_id)
            frontier = next_frontier

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    def _ingest_mulch_expertise(self) -> int:
        """Ingest .mulch/expertise/*.jsonl files into Brain with domain='expertise'.

        Each JSONL record is indexed as a separate document. Content is built from
        description, content, and resolution fields (whichever are present).
        The domain name from the filename stem is embedded in the content.

        Returns count of newly indexed records.
        """
        expertise_dir = Path(".mulch") / "expertise"
        if not expertise_dir.exists():
            return 0

        count = 0
        for jsonl_file in sorted(expertise_dir.glob("*.jsonl")):
            domain_name = jsonl_file.stem  # e.g. "brain", "cli", "hooks"
            try:
                lines = jsonl_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except (IOError, OSError):
                continue
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    record = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue

                rec_id = record.get("id") or ""
                if not rec_id:
                    continue

                doc_id = f"expertise:{domain_name}:{rec_id}"
                parts: list[str] = [f"[expertise:{domain_name}]"]
                if record.get("name"):
                    parts.append(f"name: {record['name']}")
                if record.get("type"):
                    parts.append(f"type: {record['type']}")
                if record.get("description"):
                    parts.append(record["description"])
                if record.get("content"):
                    parts.append(record["content"])
                if record.get("resolution"):
                    parts.append(f"resolution: {record['resolution']}")

                content = "\n".join(parts)
                if self._ingest_single(
                    doc_id,
                    content,
                    source_file=str(jsonl_file),
                    domain="expertise",
                ):
                    count += 1
        return count

    def _ingest_overstory_logs(self) -> int:
        """Ingest .overstory/logs/**/*.ndjson into Brain with domain='sessions'.

        Each NDJSON file is indexed as one document. Content is built from
        event fields (timestamp, event, agentName, data). Uses source_file=None
        to avoid _purge_deleted() conflicts (.overstory is an excluded path segment).

        Returns count of newly indexed records.
        """
        logs_dir = Path(".overstory") / "logs"
        if not logs_dir.exists():
            return 0

        count = 0
        for ndjson_file in sorted(logs_dir.rglob("*.ndjson")):
            try:
                lines = ndjson_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except (IOError, OSError):
                continue

            events = []
            for raw in lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    event = json.loads(raw)
                    if isinstance(event, dict):
                        events.append(event)
                except json.JSONDecodeError:
                    continue

            if not events:
                continue

            # Build searchable content from event fields
            rel = str(ndjson_file)
            parts: list[str] = [f"[sessions] {ndjson_file.parent.name} {ndjson_file.stem}"]
            for event in events:
                ts = event.get("timestamp", "")[:19]
                ev = event.get("event", "")
                agent = event.get("agentName", "")
                data = event.get("data") or {}
                msg = ""
                if isinstance(data, dict):
                    msg = (
                        data.get("message")
                        or data.get("toolName")
                        or data.get("text")
                        or ""
                    )
                line_parts = [x for x in [ts, ev, agent, str(msg)[:80]] if x]
                if line_parts:
                    parts.append(" ".join(line_parts))

            doc_id = f"sessions:{rel}"
            content = "\n".join(parts)
            # source_file=None: .overstory is excluded from _should_index(), so
            # passing the real path would cause _purge_deleted() to remove this entry.
            if self._ingest_single(doc_id, content, source_file=None, domain="sessions"):
                count += 1

        return count

    def ingest(self, sources: list[str]) -> int:
        """Full index of all provided file paths or directories. Returns doc count."""
        count = 0
        for source in sources:
            p = Path(source)
            if not p.exists():
                continue
            if p.is_file() and self._should_index(source):
                content = p.read_text(encoding="utf-8", errors="replace")
                domain = p.suffix.lstrip(".")
                chunks = self._chunk_source_file(source, content)
                for chunk in chunks:
                    if self._ingest_single(
                        chunk["doc_id"], chunk["content"],
                        source_file=source, domain=domain,
                        entity_name=chunk["entity_name"],
                        entity_kind=chunk["entity_kind"],
                        line_start=chunk["line_start"],
                        line_end=chunk["line_end"],
                    ):
                        count += 1
            elif p.is_dir():
                for child in p.rglob("*"):
                    if child.is_file() and self._should_index(str(child)):
                        try:
                            content = child.read_text(encoding="utf-8", errors="replace")
                            rel = str(child)
                            domain = child.suffix.lstrip(".")
                            chunks = self._chunk_source_file(rel, content)
                            for chunk in chunks:
                                if self._ingest_single(
                                    chunk["doc_id"], chunk["content"],
                                    source_file=rel, domain=domain,
                                    entity_name=chunk["entity_name"],
                                    entity_kind=chunk["entity_kind"],
                                    line_start=chunk["line_start"],
                                    line_end=chunk["line_end"],
                                ):
                                    count += 1
                        except (IOError, OSError):
                            pass
        count += self._ingest_mulch_expertise()
        count += self._ingest_overstory_logs()
        self._purge_deleted()
        self._update_last_index_timestamp()
        return count

    def incremental_reindex(self) -> int:
        """Re-index only files changed since last index. Returns count reindexed."""
        try:
            changed_out = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=ACMRD", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
            deleted_out = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=D", "HEAD"],
                capture_output=True, text=True,
            ).stdout.strip()
            untracked_out = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                capture_output=True, text=True,
            ).stdout.strip()
        except (FileNotFoundError, subprocess.SubprocessError):
            changed_out, deleted_out, untracked_out = "", "", ""

        changed = changed_out.split("\n") if changed_out else []
        deleted = deleted_out.split("\n") if deleted_out else []
        untracked = untracked_out.split("\n") if untracked_out else []

        # Remove entries for explicitly deleted files
        deleted_indexed = [f for f in deleted if f]
        if deleted_indexed:
            self._remove_entries_by_source(deleted_indexed)

        to_index = [
            f for f in changed + untracked
            if f and self._should_index(f) and Path(f).exists()
        ]

        if to_index:
            self._remove_entries_by_source(to_index)
            self._index_files(to_index)

        self._purge_deleted()
        self._update_last_index_timestamp()
        return len(to_index)

    # ------------------------------------------------------------------
    # Prompt management
    # ------------------------------------------------------------------

    # LL-06: sample-threshold for per-variant observation count on the
    # similar-task path. Below 5 observations, a variant's average is
    # too noisy to influence ranking.
    _SIMILAR_TASK_K = 20
    _VARIANT_SAMPLE_THRESHOLD = 5
    # Cosine-similarity floor for a neighbor to count at all. Below
    # this, the task is "not actually similar" and including it would
    # let cross-cluster noise dominate when both clusters are in the
    # top-k (the weighted mean normalizes the weight factor out).
    _MIN_SIMILARITY = 0.3

    def best_prompt(
        self,
        persona: str,
        step_id: str,
        difficulty: Optional[str] = None,
        similar_to_task_id: Optional[str] = None,
    ) -> str:
        """Return highest-scoring prompt variant ID for persona/step.

        When ``similar_to_task_id`` is provided and the LL-06 similarity
        path has enough data, rank variants by their CUPED-adjusted
        quality on the top-20 most similar past tasks (by cosine on
        task embeddings). Otherwise falls through to the historical
        difficulty / score_aggregates path.
        """
        # LL-06 similar-task path
        if similar_to_task_id and self._tasks is not None:
            pick = self._best_prompt_by_similar_task(
                persona, step_id, similar_to_task_id,
            )
            if pick is not None:
                return pick

        if difficulty:
            row = self._scores.execute(
                "SELECT prompt_id, AVG(score) AS avg_score, COUNT(*) AS cnt "
                "FROM prompt_scores "
                "WHERE persona = ? AND step_id = ? AND difficulty = ? "
                "GROUP BY prompt_id HAVING cnt >= 3 "
                "ORDER BY avg_score DESC LIMIT 1",
                (persona, step_id, difficulty),
            ).fetchone()
            if row:
                return row["prompt_id"]

        row = self._scores.execute(
            "SELECT prompt_id FROM score_aggregates "
            "WHERE persona = ? AND step_id = ? AND total_runs >= 3 "
            "ORDER BY avg_score DESC LIMIT 1",
            (persona, step_id),
        ).fetchone()
        return row["prompt_id"] if row else f"{persona}/default"

    def _best_prompt_by_similar_task(
        self,
        persona: str,
        step_id: str,
        task_id: str,
    ) -> Optional[str]:
        """LL-06 core — rank variants by CUPED-weighted quality on the
        top-k cosine-similar past tasks. Returns None when no variant
        has crossed the sample threshold (caller falls back)."""
        neighbors = _similar_task_ids(
            self._tasks, task_id, k=self._SIMILAR_TASK_K,
        )
        # Drop neighbors below the similarity floor so cross-cluster
        # tasks with high quality don't dominate via the weighted mean.
        neighbors = [
            (tid, sim) for tid, sim in neighbors
            if sim >= self._MIN_SIMILARITY
        ]
        if not neighbors:
            return None
        sim_map = {tid: sim for tid, sim in neighbors}
        placeholders = ",".join("?" * len(sim_map))
        # Join task_variants × task_quality_rollup on the similar-task
        # set. Prefer cuped_score; fall back to quality_score when
        # CUPED hasn't been computed yet.
        rows = self._scores.execute(
            f"SELECT tv.task_id, tv.prompt_id, "
            f"       COALESCE(qr.cuped_score, qr.quality_score) AS score "
            f"FROM task_variants tv "
            f"JOIN task_quality_rollup qr ON qr.task_id = tv.task_id "
            f"WHERE tv.persona=? AND tv.step_id=? "
            f"  AND tv.task_id IN ({placeholders})",
            (persona, step_id, *sim_map.keys()),
        ).fetchall()
        if not rows:
            return None

        # Weighted average per prompt_id.
        agg: dict[str, dict[str, float]] = {}
        for r in rows:
            w = max(0.0, float(sim_map.get(r["task_id"], 0.0)))
            entry = agg.setdefault(
                r["prompt_id"], {"weighted": 0.0, "weight": 0.0, "n": 0}
            )
            score = float(r["score"] or 0.0)
            entry["weighted"] += score * w
            entry["weight"] += w
            entry["n"] += 1

        # Sample-threshold gate: a variant with fewer than
        # VARIANT_SAMPLE_THRESHOLD observations across similar tasks
        # is correlational noise, not signal. Filter them out.
        eligible = {
            pid: (e["weighted"] / e["weight"]) if e["weight"] > 0 else 0.0
            for pid, e in agg.items()
            if e["n"] >= self._VARIANT_SAMPLE_THRESHOLD
        }
        if not eligible:
            return None
        return max(eligible.items(), key=lambda kv: kv[1])[0]

    def get_prompt(self, persona: str, variant: str = "default") -> str:
        """Return prompt variant text from scores.db or shipped prompts."""
        row = self._scores.execute(
            "SELECT content FROM prompt_variants WHERE prompt_id = ?",
            (f"{persona}/{variant}",),
        ).fetchone()
        if row:
            return row["content"]
        prompt_file = (
            Path(__file__).parent.parent / "prompts" / persona / f"{variant}.md"
        )
        if prompt_file.exists():
            return prompt_file.read_text(encoding="utf-8")
        return ""

    # ------------------------------------------------------------------
    # Outcome tracking
    # ------------------------------------------------------------------

    def record_outcome(
        self,
        prompt_id: str,
        persona: str,
        step_id: str,
        metrics: dict,
    ) -> None:
        """Store execution result in scores.db and append to outcomes.jsonl."""
        score = self._compute_psp_score(persona, step_id, metrics)
        ts = metrics.get("timestamp") or datetime.now(timezone.utc).isoformat()

        self._scores.execute(
            "INSERT OR REPLACE INTO prompt_scores "
            "(prompt_id, persona, step_id, score, tokens_used, context_tokens, "
            " duration_s, retries, difficulty, tests_passed, coverage_pct, "
            " traceability_pct, gate_passed, probe_accuracy, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                prompt_id, persona, step_id, score,
                metrics.get("tokens_used"), metrics.get("context_tokens"),
                metrics.get("duration_s"), metrics.get("retries"),
                metrics.get("difficulty"), metrics.get("tests_passed"),
                metrics.get("coverage_pct"), metrics.get("traceability_pct"),
                metrics.get("gate_passed"), metrics.get("probe_accuracy"), ts,
            ),
        )
        agg = self._scores.execute(
            "SELECT avg_score, total_runs FROM score_aggregates "
            "WHERE prompt_id = ? AND persona = ? AND step_id = ?",
            (prompt_id, persona, step_id),
        ).fetchone()
        if agg is None:
            self._scores.execute(
                "INSERT INTO score_aggregates "
                "(prompt_id, persona, step_id, avg_score, total_runs) "
                "VALUES (?, ?, ?, ?, 1)",
                (prompt_id, persona, step_id, score),
            )
        else:
            n = agg["total_runs"] + 1
            new_avg = (agg["avg_score"] * agg["total_runs"] + score) / n
            self._scores.execute(
                "UPDATE score_aggregates "
                "SET avg_score = ?, total_runs = ?, last_updated = datetime('now') "
                "WHERE prompt_id = ? AND persona = ? AND step_id = ?",
                (new_avg, n, prompt_id, persona, step_id),
            )
        self._scores.commit()

        outcomes_file = Path(self._scores_db_path).parent / "outcomes.jsonl"
        record = {"prompt_id": prompt_id, "persona": persona, "step_id": step_id,
                  "score": score, "timestamp": ts, **metrics}
        with outcomes_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    def avg_tokens(self, step_id: str) -> int:
        """Historical average token usage for a step (for difficulty estimation)."""
        row = self._scores.execute(
            "SELECT AVG(tokens_used) AS avg FROM prompt_scores "
            "WHERE step_id = ? AND tokens_used IS NOT NULL",
            (step_id,),
        ).fetchone()
        if row and row["avg"] is not None:
            return int(row["avg"])
        return 4000

    def outcome_count(self, persona: str, step_id: str) -> int:
        """Total outcomes recorded for a persona/step combination."""
        row = self._scores.execute(
            "SELECT SUM(total_runs) AS total FROM score_aggregates "
            "WHERE persona = ? AND step_id = ?",
            (persona, step_id),
        ).fetchone()
        return int(row["total"]) if row and row["total"] else 0

    def top_outcomes(
        self, persona: str, step_id: str, limit: int = 5
    ) -> list[dict]:
        """Return highest-scoring outcomes for variant generation analysis."""
        rows = self._scores.execute(
            "SELECT prompt_id, score, tokens_used, duration_s, timestamp "
            "FROM prompt_scores WHERE persona = ? AND step_id = ? "
            "ORDER BY score DESC LIMIT ?",
            (persona, step_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # PSP scoring
    # ------------------------------------------------------------------

    def _compute_psp_score(
        self, persona: str, step_id: str, metrics: dict
    ) -> float:
        """Compute PSP composite score from execution metrics."""
        weights = self.PSP_WEIGHTS.get((persona, step_id), self.DEFAULT_WEIGHTS)
        score = 0.0
        for metric_name, weight in weights.items():
            value = metrics.get(metric_name)
            if value is not None:
                score += weight * self._normalize(metric_name, float(value), step_id)
        return round(score, 4)

    def _normalize(
        self, metric_name: str, value: float, step_id: str = ""
    ) -> float:
        """Normalize a metric value to 0-1 range."""
        if metric_name in ("gate_passed", "first_attempt"):
            return float(bool(value))
        if metric_name in (
            "coverage_pct", "traceability_pct", "probe_accuracy", "story_completeness"
        ):
            return min(1.0, value / 100.0)
        if metric_name == "token_efficiency":
            baseline = self.avg_tokens(step_id) if step_id else 4000
            return min(1.0, baseline / max(value, 1.0))
        if metric_name == "retry_rate":
            return max(0.0, 1.0 - (value / 3.0))
        return value

    def record_session_outcome(
        self,
        session_id: str,
        duration_s: int,
        tokens_used: int,
        files_read: int,
        files_modified: int,
        skills_invoked: int,
    ) -> None:
        """Upsert session-level outcome metrics into session_outcomes table."""
        self._scores.execute(
            "INSERT OR REPLACE INTO session_outcomes "
            "(session_id, duration_s, tokens_used, files_read, files_modified, skills_invoked, timestamp) "
            "VALUES (?, ?, ?, ?, ?, ?, datetime('now'))",
            (session_id, duration_s, tokens_used, files_read, files_modified, skills_invoked),
        )
        self._scores.commit()
        # Bridge to /consolidation: every recorded session becomes a
        # pending candidate (idempotent on session_id). Without this,
        # the page only populates when the Stop hook on the host fires
        # janitor_enqueue against an in_progress task — a path that
        # misses isolated MCP instances and task-less sessions. Wrapped
        # so a bridge failure can't break the metrics insert above.
        try:
            from prism_service.services.consolidation_data import enqueue_for_session
            db_path = getattr(self._scores, "path", None) or \
                      getattr(self, "_scores_db_path", None)
            if db_path:
                enqueue_for_session(
                    str(db_path), session_id,
                    scope={
                        "files_read": files_read,
                        "files_modified": files_modified,
                        "skills_invoked": skills_invoked,
                    },
                )
        except Exception:
            pass

    def record_skill_usage(
        self,
        session_id: str,
        skill_name: str,
        timestamp: str = "",
    ) -> None:
        """Record a skill invocation into skill_usage table."""
        ts = timestamp or datetime.now(timezone.utc).isoformat()
        self._scores.execute(
            "INSERT INTO skill_usage (session_id, skill_name, timestamp) VALUES (?, ?, ?)",
            (session_id, skill_name, ts),
        )
        self._scores.commit()

    def record_subagent_outcome(
        self,
        prompt_id: str,
        validator: str,
        recommendation: str,
        evidence_count: int = 0,
        certificate_complete: int = 0,
        certificate_blocked: int = 0,
        timed_out: int = 0,
        tokens_used: int = 0,
        duration_s: float = 0.0,
    ) -> None:
        """Upsert one SFR outcome row for a validator sub-agent."""
        self._scores.execute(
            "INSERT OR IGNORE INTO subagent_outcomes "
            "(prompt_id, validator, recommendation, evidence_count, "
            " certificate_complete, certificate_blocked, timed_out, "
            " gate_agreed, tokens_used, duration_s) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)",
            (
                prompt_id, validator, recommendation,
                int(evidence_count),
                int(certificate_complete), int(certificate_blocked),
                int(timed_out), int(tokens_used), float(duration_s),
            ),
        )
        self._scores.commit()


# ---------------------------------------------------------------------------
# CLI entrypoint
# ---------------------------------------------------------------------------

def _cli_source_dirs() -> list[str]:
    """Return source directories to index, mirroring brain_bootstrap logic.

    NOTE: Legacy CLI-only function. Not used in MCP service mode — documents
    are indexed via the brain_index_doc MCP tool instead.
    """
    sources: list[str] = []
    cwd = Path.cwd()
    engine_root = Path(__file__).resolve().parent.parent  # legacy: was plugin_root
    docs_dir = cwd / "docs"
    if docs_dir.exists():
        sources.append(str(docs_dir))
    core_steps = engine_root / "hooks" / "core-steps"
    if core_steps.exists():
        sources.append(str(core_steps))
    for src_dir in ("src", "lib", "scripts", "plugins", "hooks"):
        candidate = cwd / src_dir
        if candidate.exists() and candidate.is_dir():
            sources.append(str(candidate))
    if not sources:
        sources.append(str(cwd))
    return sources


def _cmd_init(brain: "Brain") -> int:
    sources = _cli_source_dirs()
    count = brain.ingest(sources)
    if brain.vector_enabled:
        mode = "Full \u2014 BM25+Vector+GraphRAG"
    else:
        mode = "BM25+GraphRAG"
    print(f"Brain: indexed {count} documents from {len(sources)} source(s) (mode: {mode})")
    return 0


def _cmd_search(brain: "Brain", query: str) -> int:
    results = brain.search(query)
    if not results:
        print("No results found.")
        return 0
    for i, r in enumerate(results, 1):
        score = round(r.get("rrf_score", 0.0), 4)
        print(f"[{i}] {r['doc_id']}  (score={score})")
        snippet = r.get("content", "")[:200].replace("\n", " ")
        print(f"    {snippet}")
    return 0


def _cmd_status(brain: "Brain") -> int:
    doc_count = brain._brain.execute("SELECT COUNT(*) FROM docs").fetchone()[0]
    entity_count = brain._graph.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    last_indexed = brain._get_last_index_timestamp()
    if brain.vector_enabled:
        mode = "Full \u2014 BM25+Vector+GraphRAG"
    else:
        mode = "BM25+GraphRAG (install sqlite-vec model2vec for Full mode)"
    print(f"Mode              : {mode}")
    print(f"Documents indexed : {doc_count}")
    print(f"Graph entities    : {entity_count}")
    print(f"Last indexed      : {last_indexed}")
    return 0


def _cmd_graph(brain: "Brain", entity: str) -> int:
    results = brain.graph_query(entity)
    if not results:
        print(f"No relationships found for entity '{entity}'")
        return 0
    for r in results:
        name = r.get("name", "?")
        kind = r.get("kind", "?")
        file_ = r.get("file", "?")
        relation = r.get("relation", "?")
        print(f"  --[{relation}]--> {name} ({kind})  {file_}")
    return 0


def _cmd_explain(brain: "Brain", filepath: str) -> int:
    rows = brain._brain.execute(
        "SELECT id, domain, content FROM docs WHERE source_file = ? OR id LIKE ? ORDER BY id",
        (filepath, f"%{filepath}%"),
    ).fetchall()
    if not rows:
        print(f"No indexed chunks found for '{filepath}'")
        return 0
    print(f"Brain knowledge for: {filepath}")
    print(f"  {len(rows)} chunk(s) indexed")
    for row in rows:
        snippet = row["content"][:200].replace("\n", " ")
        domain = row["domain"] or "—"
        print(f"\n  [chunk] {row['id']}  domain={domain}")
        print(f"    {snippet}")
    entities = brain._graph.execute(
        "SELECT name, kind FROM entities WHERE file = ? LIMIT 20", (filepath,)
    ).fetchall()
    if entities:
        print(f"\n  Graph entities ({len(entities)}):")
        for e in entities:
            print(f"    {e['name']} ({e['kind']})")
    return 0


def _cmd_rebuild(brain: "Brain") -> int:
    brain._purge_deleted()
    sources = _cli_source_dirs()
    count = brain.ingest(sources)
    if brain.vector_enabled:
        mode = "Full \u2014 BM25+Vector+GraphRAG"
    else:
        mode = "BM25+GraphRAG"
    print(f"Brain: rebuilt index — {count} documents from {len(sources)} source(s) (mode: {mode})")
    return 0


def _cmd_analytics(brain: "Brain") -> int:
    outcomes_file = Path(brain._scores_db_path).parent / "outcomes.jsonl"
    if not outcomes_file.exists():
        print("No outcomes recorded yet.")
        return 0

    records: list[dict] = []
    try:
        for raw in outcomes_file.read_text(encoding="utf-8", errors="replace").splitlines():
            raw = raw.strip()
            if not raw:
                continue
            try:
                records.append(json.loads(raw))
            except json.JSONDecodeError:
                continue
    except (IOError, OSError):
        print("Error reading outcomes.jsonl", file=sys.stderr)
        return 1

    if not records:
        print("No outcomes recorded yet.")
        return 0

    print(f"Brain Analytics — {len(records)} total outcome(s)\n")

    # Group by persona/step
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        key = (r.get("persona") or "?", r.get("step_id") or "?")
        groups.setdefault(key, []).append(r)

    print(f"{'Persona/Step':<42} {'Runs':>5} {'Avg':>7} {'Best':>7} {'Worst':>7}")
    print("-" * 72)
    for (persona, step), outcomes in sorted(groups.items()):
        scores = [float(o["score"]) for o in outcomes if "score" in o]
        if scores:
            avg = sum(scores) / len(scores)
            print(
                f"{persona + '/' + step:<42} {len(outcomes):>5}"
                f" {avg:>7.3f} {max(scores):>7.3f} {min(scores):>7.3f}"
            )

    # Recent trend (last 10)
    recent = sorted(records, key=lambda r: r.get("timestamp") or "", reverse=True)[:10]
    print(f"\nRecent outcomes (last {len(recent)}):")
    for r in recent:
        ts = (r.get("timestamp") or "?")[:19]
        pid = r.get("prompt_id") or "?"
        score = float(r.get("score") or 0.0)
        print(f"  {ts}  {pid:<40}  score={score:.3f}")

    return 0


def _print_usage() -> None:
    print("Usage: python3 brain_engine.py <command> [args]")
    print("")
    print("Commands:")
    print("  init              Index project source files")
    print("  ingest            Re-index all sources (same as init)")
    print("  search <query>    Search indexed knowledge")
    print("  status            Show index health and statistics")
    print("  graph <entity>    Show entity relationships in the graph")
    print("  explain <file>    Show what Brain knows about a file")
    print("  rebuild           Full purge + reindex")
    print("  analytics         Show outcome trends from outcomes.jsonl")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        _print_usage()
        sys.exit(1)

    cmd = args[0]
    if cmd in ("init", "ingest"):
        try:
            import sqlite_vec  # type: ignore  # noqa: F401
            from model2vec import StaticModel  # type: ignore  # noqa: F401
        except ImportError:
            print("Brain: attempting to install optional deps (sqlite-vec model2vec)...",
                  file=sys.stderr)
            result = subprocess.run(
                [sys.executable, "-m", "pip", "install", "-q", "sqlite-vec", "model2vec"],
                capture_output=True,
            )
            if result.returncode != 0:
                print(
                    "Brain: optional deps unavailable, continuing in BM25+GraphRAG mode",
                    file=sys.stderr,
                )

    try:
        b = Brain()
    except BrainCorruptError as exc:
        print(f"Brain: corrupt database ({exc}), deleting and re-initialising...",
              file=sys.stderr)
        brain_dir = Path(".prism/brain")
        for db_file in brain_dir.glob("*.db"):
            db_file.unlink(missing_ok=True)
        b = Brain()

    if cmd in ("init", "ingest"):
        rc = _cmd_init(b)
    elif cmd == "search":
        if len(args) < 2:
            print("Error: search requires a query argument", file=sys.stderr)
            _print_usage()
            sys.exit(1)
        rc = _cmd_search(b, " ".join(args[1:]))
    elif cmd == "status":
        rc = _cmd_status(b)
    elif cmd == "graph":
        if len(args) < 2:
            print("Error: graph requires an entity argument", file=sys.stderr)
            _print_usage()
            sys.exit(1)
        rc = _cmd_graph(b, " ".join(args[1:]))
    elif cmd == "explain":
        if len(args) < 2:
            print("Error: explain requires a file argument", file=sys.stderr)
            _print_usage()
            sys.exit(1)
        rc = _cmd_explain(b, args[1])
    elif cmd == "rebuild":
        rc = _cmd_rebuild(b)
    elif cmd == "analytics":
        rc = _cmd_analytics(b)
    else:
        print(f"Error: unknown command '{cmd}'", file=sys.stderr)
        _print_usage()
        sys.exit(1)

    sys.exit(rc)
