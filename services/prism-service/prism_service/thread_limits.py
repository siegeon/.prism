"""Native-math thread guards (GH #155).

The silent all-threads wedge is OpenMP/OpenBLAS futex oversubscription on
the embedding path: a request thread enters native math (model2vec/numpy
BLAS) while holding the GIL, blocks on a native futex, and every other
Python thread starves on the GIL — total wedge, no traceback.

`apply_thread_limits()` pins the native thread pools to a single thread,
which removes the oversubscription. It MUST run before numpy/model2vec are
imported (the BLAS backends read these vars once at load), so it is called
at the very TOP of both entrypoints (main.py + cli/prism_cli.py), before
the config/brain import block that transitively pulls numpy in.

Operator intent always wins: a var the operator has already set in the
environment is left UNTOUCHED — the guard only fills in safe defaults for
vars the operator has not chosen.
"""

from __future__ import annotations

import os

# var -> safe single-thread / passive default, applied only when unset.
_DEFAULTS = {
    "OMP_NUM_THREADS": "1",
    "OPENBLAS_NUM_THREADS": "1",
    "MKL_NUM_THREADS": "1",
    "NUMEXPR_NUM_THREADS": "1",
    "OMP_WAIT_POLICY": "PASSIVE",
    "TOKENIZERS_PARALLELISM": "false",
}


def apply_thread_limits() -> dict[str, str]:
    """Pin native-math thread pools to 1 (and OpenMP/tokenizer policy to the
    passive defaults) for every var the operator has not already set.

    Returns the map of vars this call actually filled in (operator-set vars
    are excluded), so a caller can log exactly what was pinned.
    """
    applied: dict[str, str] = {}
    for var, default in _DEFAULTS.items():
        if os.environ.get(var) is None:
            os.environ[var] = default
            applied[var] = default
    return applied
