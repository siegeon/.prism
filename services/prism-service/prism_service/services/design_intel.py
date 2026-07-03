"""Per-industry design intelligence — a keyless, model-free design brain.

Ported from the MIT-licensed ui-ux-pro-max-skill
(github.com/nextlevelbuilder/ui-ux-pro-max-skill) per PRISM's port-and-reskin
convention: we copied the *structure* — its BM25 retrieval engine (k1=1.5,
b=0.75) and its industry-keyed color + typography data — and reskinned the
payload to PRISM's --app-* design tokens. The upstream skill ships ~1.5 MB of
CSVs and leans on a model to choose a look; we vendor a compact curated table
(the palette + font values below are adapted from the source repo's
data/colors.csv and data/typography.csv) and let BM25 alone resolve freeform
business text ("a boutique fitness studio") to an industry palette.

Same keyless/deterministic philosophy as the rest of Magic: no network, no
model, always renders. A serious industry never gets the generic AI
purple-gradient look — trust blues/navies/teals are chosen deliberately, per
the source's anti-pattern guidance.

MIT: colors adapted from source data/colors.csv; font pairings from
data/typography.csv. Copyright (c) nextlevelbuilder, MIT License.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict

# --- BM25 retrieval engine (ported: k1=1.5, b=0.75) -------------------------


def _tokenize(text: str) -> list[str]:
    """Lowercase, strip punctuation, drop 1-char tokens (source tokenizer)."""
    text = re.sub(r"[^\w\s]", " ", str(text).lower())
    return [w for w in text.split() if len(w) >= 2]


class BM25:
    """Compact BM25 index over a small corpus of industry descriptions.

    Uses the Lucene-style non-negative IDF (log(1 + (N-df+0.5)/(df+0.5))) so a
    term shared by many industries never earns a negative weight and flips the
    ranking — important with a corpus this small."""

    def __init__(self, documents: list[str], k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self.corpus = [_tokenize(d) for d in documents]
        self.N = len(self.corpus)
        self.doc_lengths = [len(d) for d in self.corpus]
        self.avgdl = (sum(self.doc_lengths) / self.N) if self.N else 0.0
        df: dict[str, int] = defaultdict(int)
        for doc in self.corpus:
            for term in set(doc):
                df[term] += 1
        self.idf = {t: math.log(1 + (self.N - n + 0.5) / (n + 0.5))
                    for t, n in df.items()}

    def scores(self, query: str) -> list[float]:
        q = _tokenize(query)
        out: list[float] = []
        for idx, doc in enumerate(self.corpus):
            freqs: dict[str, int] = defaultdict(int)
            for term in doc:
                freqs[term] += 1
            dl = self.doc_lengths[idx] or 1
            s = 0.0
            for term in q:
                if term not in freqs:
                    continue
                tf = freqs[term]
                idf = self.idf.get(term, 0.0)
                num = tf * (self.k1 + 1)
                den = tf + self.k1 * (1 - self.b + self.b * dl / (self.avgdl or 1))
                s += idf * num / den
            out.append(s)
        return out

    def best(self, query: str) -> tuple[int, float]:
        scores = self.scores(query)
        if not scores:
            return -1, 0.0
        idx = max(range(len(scores)), key=lambda i: scores[i])
        return idx, scores[idx]


# --- curated industry design table (adapted from source colors/typography) --
# Each entry: the retrieval `text` (industry + signal keywords BM25 matches on)
# and the full 9-key --app-* token set. Palettes favour accessible contrast and
# an industry-appropriate mood; NO generic purple gradient for serious
# verticals (clinic/law/finance/logistics get trust blues, navies, teals).

_GENERIC = {
    "--app-brand": "#2563EB", "--app-accent": "#EA580C",
    "--app-bg": "#F8FAFC", "--app-surface": "#FFFFFF",
    "--app-fg": "#1E293B", "--app-muted": "#64748B",
    "--app-border": "#E2E8F0", "--app-radius": "12px",
    "--app-font": "Poppins, ui-sans-serif, system-ui, sans-serif",
}

INDUSTRIES: list[dict] = [
    {
        "key": "clinic",
        "text": "clinic healthcare medical patient patients doctor "
                "appointment appointments hospital pharmacy health dental "
                "therapy care nurse practice",
        "tokens": {
            "--app-brand": "#0891B2", "--app-accent": "#16A34A",
            "--app-bg": "#F0FDFA", "--app-surface": "#FFFFFF",
            "--app-fg": "#134E4A", "--app-muted": "#64748B",
            "--app-border": "#99F6E4", "--app-radius": "8px",
            "--app-font": "Figtree, ui-sans-serif, system-ui, sans-serif",
        },
    },
    {
        "key": "shop",
        "text": "shop store retail ecommerce e-commerce commerce product "
                "products order orders customer cart checkout inventory "
                "supplier merchandise",
        "tokens": {
            "--app-brand": "#059669", "--app-accent": "#EA580C",
            "--app-bg": "#ECFDF5", "--app-surface": "#FFFFFF",
            "--app-fg": "#064E3B", "--app-muted": "#64748B",
            "--app-border": "#A7F3D0", "--app-radius": "12px",
            "--app-font": "Rubik, ui-sans-serif, system-ui, sans-serif",
        },
    },
    {
        "key": "crm",
        "text": "crm sales lead leads deal deals contact contacts pipeline "
                "client clients relationship account opportunity prospect",
        "tokens": {
            "--app-brand": "#2563EB", "--app-accent": "#059669",
            "--app-bg": "#F8FAFC", "--app-surface": "#FFFFFF",
            "--app-fg": "#0F172A", "--app-muted": "#64748B",
            "--app-border": "#E4ECFC", "--app-radius": "10px",
            "--app-font": "Poppins, ui-sans-serif, system-ui, sans-serif",
        },
    },
    {
        "key": "gym",
        "text": "gym fitness studio athletic workout training exercise "
                "sports bootcamp crossfit trainer membership class classes "
                "boutique",
        "tokens": {
            "--app-brand": "#F97316", "--app-accent": "#22C55E",
            "--app-bg": "#111827", "--app-surface": "#1F2937",
            "--app-fg": "#F8FAFC", "--app-muted": "#9CA3AF",
            "--app-border": "#374151", "--app-radius": "14px",
            "--app-font": "'Barlow Condensed', Barlow, ui-sans-serif, "
                          "system-ui, sans-serif",
        },
    },
    {
        "key": "restaurant",
        "text": "restaurant food cafe menu dining kitchen chef culinary "
                "hospitality bar bistro catering meal reservation table",
        "tokens": {
            "--app-brand": "#DC2626", "--app-accent": "#A16207",
            "--app-bg": "#FEF2F2", "--app-surface": "#FFFFFF",
            "--app-fg": "#450A0A", "--app-muted": "#78716C",
            "--app-border": "#FECACA", "--app-radius": "10px",
            "--app-font": "'Playfair Display', Georgia, 'Times New Roman', "
                          "serif",
        },
    },
    {
        "key": "salon",
        "text": "salon spa beauty wellness massage hair nails barber grooming "
                "aesthetics cosmetic relax pampering skincare",
        "tokens": {
            "--app-brand": "#EC4899", "--app-accent": "#8B5CF6",
            "--app-bg": "#FDF2F8", "--app-surface": "#FFFFFF",
            "--app-fg": "#831843", "--app-muted": "#8B7A85",
            "--app-border": "#FBCFE8", "--app-radius": "16px",
            "--app-font": "Lora, Georgia, serif",
        },
    },
    {
        "key": "law",
        "text": "law legal lawyer attorney firm court case cases counsel "
                "litigation compliance contract paralegal justice",
        "tokens": {
            "--app-brand": "#1E3A8A", "--app-accent": "#B45309",
            "--app-bg": "#F8FAFC", "--app-surface": "#FFFFFF",
            "--app-fg": "#0F172A", "--app-muted": "#64748B",
            "--app-border": "#E2E8F0", "--app-radius": "6px",
            "--app-font": "'EB Garamond', Georgia, serif",
        },
    },
    {
        "key": "education",
        "text": "education school course courses learning student students "
                "teacher classroom academy university e-learning tutor lesson "
                "training curriculum",
        "tokens": {
            "--app-brand": "#0D9488", "--app-accent": "#EA580C",
            "--app-bg": "#F0FDFA", "--app-surface": "#FFFFFF",
            "--app-fg": "#134E4A", "--app-muted": "#64748B",
            "--app-border": "#5EEAD4", "--app-radius": "12px",
            "--app-font": "'Crimson Pro', Georgia, serif",
        },
    },
    {
        "key": "logistics",
        "text": "logistics shipping delivery freight warehouse transport "
                "fleet courier supply tracking distribution dispatch cargo "
                "route",
        "tokens": {
            "--app-brand": "#2563EB", "--app-accent": "#EA580C",
            "--app-bg": "#EFF6FF", "--app-surface": "#FFFFFF",
            "--app-fg": "#1E40AF", "--app-muted": "#64748B",
            "--app-border": "#BFDBFE", "--app-radius": "8px",
            "--app-font": "'IBM Plex Sans', ui-sans-serif, system-ui, "
                          "sans-serif",
        },
    },
    {
        "key": "finance",
        "text": "finance banking bank fintech investment loan payment "
                "accounting insurance money financial trading wealth",
        "tokens": {
            "--app-brand": "#0F172A", "--app-accent": "#A16207",
            "--app-bg": "#F8FAFC", "--app-surface": "#FFFFFF",
            "--app-fg": "#020617", "--app-muted": "#64748B",
            "--app-border": "#E2E8F0", "--app-radius": "6px",
            "--app-font": "'IBM Plex Sans', ui-sans-serif, system-ui, "
                          "sans-serif",
        },
    },
    {
        "key": "generic",
        # Deliberately narrow: only unambiguous "generic tool" words. Common
        # filler ("business", "app", "service") is omitted so a business-fact
        # sentence like "...business is a clinic" routes on its real noun, not
        # here. A no-signal query still lands on generic via the score<=0
        # fallback in design_tokens.
        "text": "dashboard admin console platform toolkit portal software "
                "management workspace",
        "tokens": dict(_GENERIC),
    },
]

_BY_KEY = {e["key"]: e for e in INDUSTRIES}
_INDEX = BM25([e["text"] for e in INDUSTRIES])


def industries() -> list[str]:
    """The curated industry keys (for API/inspection)."""
    return [e["key"] for e in INDUSTRIES]


def design_tokens(industry_or_text: str) -> dict:
    """Resolve an industry name OR freeform business text to the full --app-*
    token set. Deterministic: an exact key wins; otherwise BM25 ranks the
    curated industries and the best positive match wins; a no-signal query
    falls back to the neutral generic palette. Always returns all 9 keys.

    Examples: 'clinic' -> clinic; 'a boutique fitness studio' -> gym;
    'This customer's business is a shop.' -> shop; '' -> generic."""
    text = str(industry_or_text or "").strip().lower()
    if not text:
        return dict(_GENERIC)
    if text in _BY_KEY:
        return dict(_BY_KEY[text]["tokens"])
    # Token-exact beats BM25: db/module names like "gym_management" carry the
    # industry as one token plus noise words ("management") that drag the
    # ranked match toward generic. Any single token that IS an industry key
    # resolves deterministically.
    for tok in re.split(r"[^a-z0-9]+", text):
        if tok in _BY_KEY:
            return dict(_BY_KEY[tok]["tokens"])
    idx, score = _INDEX.best(text)
    if idx < 0 or score <= 0:
        return dict(_GENERIC)
    return dict(INDUSTRIES[idx]["tokens"])
