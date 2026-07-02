"""RED scaffold — Sessions 'TOKENS / FILE' KPI must divide by files READ (task 8c596af6).

/sessions shows a 'Tokens / file' KPI card. With 2 sessions (161,897 tok / 7
files read; 44,192 tok / 1 file), the card read 206,089 — EXACTLY the raw token
SUM, not a per-file figure — because the denominator was built from
`files_modified` (0 for both rows), so the `|| 1` fallback divided by one file.
Expected ~25,761 = 206,089 / 8 files read (matching mainline 8888's plausible
per-file value). The fix computes the KPI denominator from `files_read`.

Scans web/src/pages/SessionsPage.tsx. FAILS before the fix: the 'Tokens / file'
KPI denominator is derived from files_modified, not files_read.
"""

import re
from pathlib import Path

_HERE = Path(__file__).resolve()
_SERVICE_ROOT = _HERE.parent.parent.parent  # .../services/prism-service
_PAGES = _SERVICE_ROOT / "prism_service" / "web" / "src" / "pages"


def _read(page: str) -> str:
    return (_PAGES / page).read_text(encoding="utf-8")


def test_tokens_per_file_kpi_denominator_is_files_read():
    src = _read("SessionsPage.tsx")
    m = re.search(r'label="Tokens / file"\s+value=\{([^}]*)\}', src)
    assert m, "Tokens / file KPI card not found"
    expr = m.group(1)
    dm = re.search(r"totalTokens\s*/\s*([A-Za-z_$][\w$]*)", expr)
    assert dm, "KPI must divide totalTokens by a named denominator: " + expr
    denom = dm.group(1)
    # Resolve the denominator variable's definition.
    defm = re.search(r"\b" + re.escape(denom) + r"\b\s*=\s*([\s\S]*?);", src)
    assert defm, f"denominator {denom!r} has no definition"
    defn = defm.group(1)
    # The per-file denominator must sum files READ, never files_modified.
    assert "files_read" in defn, (
        f"Tokens/file denominator {denom!r} is not derived from files_read: "
        f"{defn.strip()}"
    )
    assert "files_modified" not in defn, (
        f"Tokens/file denominator {denom!r} still uses files_modified: "
        f"{defn.strip()}"
    )
