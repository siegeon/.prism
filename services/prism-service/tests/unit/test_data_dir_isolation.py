"""Guards the suite-wide PRISM_DATA_DIR isolation (tests/conftest.py).

If these fail, tests are writing into a REAL data dir again — the exact
regression that leaked 15+ junk projects into the live dogfood store.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def test_env_is_pinned_to_a_throwaway_temp_dir():
    pinned = os.environ.get("PRISM_DATA_DIR")
    assert pinned, "tests/conftest.py must pin PRISM_DATA_DIR for the suite"
    p = Path(pinned)
    assert p.name.startswith("prism-test-data-"), pinned
    assert str(p).startswith(str(Path(tempfile.gettempdir()))), pinned


def test_config_constants_froze_against_the_pinned_dir():
    from prism_service import config

    pinned = Path(os.environ["PRISM_DATA_DIR"]).resolve()
    assert config.DATA_DIR == pinned, (
        "config.DATA_DIR froze before the conftest pin — import-order "
        f"regression: {config.DATA_DIR} != {pinned}"
    )
    assert config.PROJECTS_DIR == pinned / "projects"


def test_project_dirs_land_inside_the_pinned_dir():
    from prism_service import config

    d = config.project_data_dir("isolation-canary")
    assert Path(os.environ["PRISM_DATA_DIR"]).resolve() in d.resolve().parents
