"""RED contract tests — task 401811b8 "Radix color tokens — legible in both themes".

Pins the acceptance criteria of the Radix token migration against the REAL web
source (same *_ui.py pattern as test_animate_conductor_tasks_ui.py):
  1. @radix-ui/colors is a declared dependency; index.css imports light AND
     dark scales and switches themes (data-theme / prefers-color-scheme).
  2. --accent-{tone}-* declarations alias semantic vars (var(--...)), never
     raw dark-tuned rgba/hex literals; no orphaned consumers post-sweep.
  3. A categorical --et-* ramp exists for canvas/entity-type surfaces,
     mirrored byte-identically into lib/palette.ts (ET_HEX) and
     routes/graph_static.py (the task's likely_misfire is this list drifting).
  4. Zero inline text-[<11px] sizes in web/src (238 today); the Tailwind v4
     @theme block carries a >=5-step type scale.
  5. The contrast-check script (web/scripts/check-contrast.mjs) — the task
     oracle vs live :8888 — exists and is wired as an npm script.
  6. PRISM_VERSION is bumped past the pre-migration 6.9.7 dev build.

ALL of these FAIL today (dark-only rgba tokens at index.css:66-92, no radix
dep, no --et- ramp, no contrast script, version 6.9.6). They go green only
when the migration actually lands.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SVC = _HERE.parent.parent.parent / "prism_service"
_WEB = _SVC / "web"
_SRC = _WEB / "src"

_CSS_PATH = _SRC / "index.css"
_PALETTE = _SRC / "lib" / "palette.ts"
_GRAPH_STATIC = _SVC / "routes" / "graph_static.py"
_PKG = _WEB / "package.json"

_TONES = ("teal", "sage", "amber", "rose", "violet", "emerald", "slate")
_RADIX_SCALES = ("slate", "blue", "green", "amber", "red", "violet")


def _css() -> str:
    return _CSS_PATH.read_text(encoding="utf-8")


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


# --- 1. Radix dependency + dual-scale imports + theme switching -------------

def test_radix_colors_is_a_declared_dependency():
    pkg = json.loads(_read(_PKG))
    deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
    assert "@radix-ui/colors" in deps, \
        "@radix-ui/colors must be a web/package.json dependency"


def test_index_css_imports_light_and_dark_radix_scales():
    css = _css()
    for scale in _RADIX_SCALES:
        assert f"@radix-ui/colors/{scale}" in css, \
            f"index.css must import the radix '{scale}' scale"
        assert re.search(rf"@radix-ui/colors/{scale}[a-z-]*-dark", css), \
            f"index.css must import the radix '{scale}' DARK scale (dual-theme)"


def test_css_switches_between_light_and_dark_theme_blocks():
    css = _css()
    assert re.search(r"data-theme|prefers-color-scheme", css), (
        "index.css has NO theme switching (data-theme / prefers-color-scheme "
        "grep for both = zero hits) — tokens are dark-only, the light theme "
        "block the task exists to add is missing"
    )


# --- 2. Semantic aliasing — no raw dark-tuned literals, no orphans ----------

def test_accent_declarations_alias_semantic_vars_not_raw_literals():
    css = _css()
    bad: list[str] = []
    for tone in _TONES:
        decls = re.findall(
            rf"(--accent-{tone}-(?:bg|ring|fg))\s*:\s*([^;]+);", css)
        for name, value in decls:
            if "var(--" not in value:
                bad.append(f"{name}: {value.strip()}")
    assert not bad, (
        f"{len(bad)} accent tokens still carry raw color literals instead of "
        "aliasing the radix semantic vars (dual half-applied palette): "
        + "; ".join(bad[:6])
    )


def test_no_orphaned_accent_consumers():
    defined = set(re.findall(r"(--accent-[a-z]+-(?:bg|ring|fg))\s*:", _css()))
    consumed: set[str] = set()
    for f in _SRC.rglob("*.ts*"):
        consumed |= set(re.findall(
            r"var\((--accent-[a-z]+-(?:bg|ring|fg))\)", _read(f)))
    orphans = sorted(consumed - defined)
    assert not orphans, \
        f"consumers reference accent tokens no theme defines: {orphans}"


# --- 3. --et-* categorical ramp, mirrored byte-identically 3 ways -----------

def test_et_categorical_ramp_defined_in_css():
    assert re.search(r"--et-[a-z0-9-]+\s*:", _css()), (
        "no --et-* categorical entity-type ramp in index.css — it must exist "
        "separately from the semantic accent tokens (canvas/entity surfaces)"
    )


def _et_hexes_from_palette() -> list[str]:
    m = re.search(r"ET_HEX[^=]*=\s*\[([^\]]+)\]", _read(_PALETTE))
    assert m, "lib/palette.ts must export an ET_HEX categorical hex list"
    return re.findall(r'"(#[0-9a-fA-F]{6})"', m.group(1))


def test_et_ramp_mirrored_into_palette_ts():
    hexes = _et_hexes_from_palette()
    assert len(hexes) >= 6, \
        f"ET_HEX must carry the categorical ramp, found {len(hexes)} hexes"


def test_et_ramp_byte_identical_in_graph_static():
    et = _et_hexes_from_palette()
    gs_hexes = re.findall(r'["\'](#[0-9a-fA-F]{6})["\']', _read(_GRAPH_STATIC))
    n = len(et)
    assert any(gs_hexes[i:i + n] == et for i in range(len(gs_hexes) - n + 1)), (
        "routes/graph_static.py does not carry the ET ramp byte-identical to "
        "palette.ts ET_HEX (same hexes, same order) — the two lists drifted"
    )


# --- 4. Tiny-type sweep + 5-step type scale ----------------------------------

def test_no_inline_text_below_11px_anywhere_in_src():
    offenders: dict[str, int] = {}
    for f in _SRC.rglob("*.ts*"):
        hits = [v for v in re.findall(r"text-\[(\d+(?:\.\d+)?)px\]", _read(f))
                if float(v) < 11]
        if hits:
            offenders[f.name] = len(hits)
    total = sum(offenders.values())
    top = sorted(offenders.items(), key=lambda kv: -kv[1])[:5]
    assert total == 0, (
        f"{total} inline text-[<11px] sizes remain in web/src (top: {top}) — "
        "route them through the 5-step type scale"
    )


def test_theme_block_defines_a_five_step_type_scale():
    theme_bodies = re.findall(r"@theme[^{]*\{([^}]*)\}", _css())
    steps = set(re.findall(r"--text-([a-z0-9]+)\s*:", " ".join(theme_bodies)))
    assert len(steps) >= 5, (
        "the Tailwind v4 @theme block must define the 5-step type scale as "
        f"--text-* font-size tokens; found {len(steps)}: {sorted(steps)}"
    )


# --- 5. Contrast oracle wiring + 6. version bump -----------------------------

def test_contrast_check_script_exists_and_is_wired():
    script = _WEB / "scripts" / "check-contrast.mjs"
    assert script.exists(), (
        "web/scripts/check-contrast.mjs missing — the task oracle is a "
        "contrast script proving every lozenge text/bg pair >=4.5:1 in BOTH "
        "themes against the live :8888 daemon"
    )
    body = _read(script)
    assert "4.5" in body, "contrast script must enforce the 4.5:1 AA threshold"
    assert "amber" in body, \
        "contrast script must cover amber/warn (solid amber takes DARK text)"
    pkg = json.loads(_read(_PKG))
    assert any("check-contrast" in v for v in pkg.get("scripts", {}).values()), \
        "package.json must expose the contrast check as an npm script"


def test_version_patch_bumped_past_pre_migration_build():
    m = re.search(r'PRISM_VERSION\s*=\s*"(\d+)\.(\d+)\.(\d+)"',
                  _read(_SVC / "__version__.py"))
    assert m, "__version__.py must define PRISM_VERSION"
    assert tuple(map(int, m.groups())) > (6, 9, 7), (
        "PRISM_VERSION must be patch-bumped past 6.9.7 (the dev build serving "
        ":8888 before this migration) so the bounced daemon proves new code"
    )
