"""Pin test: the SPA's package.json must not admit react 19.3.x.

react 19.3.0 (published 2026-09-11T16:28Z) narrows what @react-three/fiber
accepts: its peer dependency on react is ">=19 <19.3". A caret range on react
(e.g. "^19.2.7") admits 19.3.x, so a lockfile-less `npm install` (no
package-lock.json is committed) resolves react to 19.3.0 and npm exits
ERESOLVE — every PR check's "Install SPA deps" step then fails in CI.

This test parses the exact range strings declared for react and react-dom in
package.json and fails if either range can resolve to a 19.3.x version.
"""
import json
import re
from pathlib import Path

import pytest

PACKAGE_JSON = (
    Path(__file__).resolve().parents[2]
    / "prism_service"
    / "web"
    / "package.json"
)

_RANGE_RE = re.compile(r"^(\^|~)?(\d+)\.(\d+)\.(\d+)$")


def admits_19_3(range_spec: str) -> bool:
    """Return True if the npm range can resolve to any react 19.3.x version.

    Covers the range shapes used in this file: caret (^X.Y.Z), tilde
    (~X.Y.Z), and an exact pin (X.Y.Z).
    """
    range_spec = range_spec.strip()
    match = _RANGE_RE.match(range_spec)
    if not match:
        raise ValueError(f"Unrecognized range spec: {range_spec!r}")
    modifier, major, minor, _patch = (
        match.group(1),
        int(match.group(2)),
        int(match.group(3)),
        int(match.group(4)),
    )

    if major != 19:
        return False

    if modifier == "^":
        # ^19.Y.Z admits [19.Y.Z, 20.0.0) — includes 19.3.x whenever Y <= 3.
        return minor <= 3
    if modifier == "~":
        # ~19.Y.Z admits [19.Y.Z, 19.(Y+1).0) — includes 19.3.x only if Y == 3.
        return minor == 3
    # Exact pin: only matches if it names a 19.3.x version itself.
    return minor == 3


@pytest.mark.parametrize(
    "spec,expected",
    [
        ("^19.2.7", True),
        ("^19.3.0", True),
        ("~19.2.7", False),
        ("~19.3.0", True),
        ("19.2.7", False),
        ("^18.3.1", False),
    ],
)
def test_admits_19_3_reference_cases(spec: str, expected: bool) -> None:
    assert admits_19_3(spec) is expected


@pytest.mark.parametrize("dep_name", ["react", "react-dom"])
def test_dependency_range_excludes_react_19_3(dep_name: str) -> None:
    data = json.loads(PACKAGE_JSON.read_text())
    range_spec = data["dependencies"][dep_name]
    assert not admits_19_3(range_spec), (
        f"{dep_name} range {range_spec!r} in {PACKAGE_JSON} can resolve to a "
        "19.3.x release, which @react-three/fiber's peer dependency "
        '(">=19 <19.3") rejects, breaking `npm install` in CI'
    )
