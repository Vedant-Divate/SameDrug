"""Phase 3 normalization engine: messy human strings -> canonical match keys.

Covers (per docs/sources.md addenda + Phase 3 brief):
- clean_text: NFKC, NBSP -> space, IP/BP/USP/RS stripping, whitespace
  collapse. Greek letters preserved via NFKC.
- parse_strength / parse_strength_parts: canonical strengths ("300mg",
  "125mg+500mg" sorted combos, "25mg/ml" concentrations, "40iu/ml",
  "1%ww"/"0.3%wv" percents, mcg/g -> mg conversion). Returns
  (canonical | None, reason | None); unknown strength is NEVER faked.
- canonical_form / form_family / extract_modifiers: dosage-form
  canonicalization with liquid / inhalation / topical families.
- parse_pack: JAP unitSize -> per-unit divisor (count / volume_ml /
  dose_count / weight_g / vial).
- FormulationKey: frozen dataclass; key() is the join key.

Every function is pure (no I/O). JAP composition extraction and the
molecule alias table arrive as a follow-up commit on top of this module.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# A. Text cleanup
# ---------------------------------------------------------------------------

_QUALITY_RE = re.compile(r"(?<![A-Za-z])(IP|BP|USP|RS)(?![A-Za-z])", re.IGNORECASE)
_DOTTED_QUALITY_RE = re.compile(r"(?<![A-Za-z])(I\.P\.?|B\.P\.?|U\.S\.P\.?)(?![A-Za-z])")
_WS_RE = re.compile(r"\s+")


def clean_text(s: str) -> str:
    """NFKC-normalize, map NBSP to space, strip quality markers, collapse ws."""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace(" ", " ")
    s = _DOTTED_QUALITY_RE.sub(" ", s)  # "Tablets I.P 2mg" (JAP dots style)
    s = _QUALITY_RE.sub(" ", s)
    s = _WS_RE.sub(" ", s).strip()
    return s


def fold(s: str) -> str:
    """Case-insensitive match form of an already cleaned string."""
    return clean_text(s).casefold()


# ---------------------------------------------------------------------------
# D. Strength canonical forms
# ---------------------------------------------------------------------------

_NUM = r"(?:\d{1,3}(?:,\d{2,3})+|\d+(?:\.\d+)?)"
# NOTE: bare "g" (gram) is intentionally absent here — "18G" needle gauge
# must never tokenize as grams. Spaced grams ("1 g") are matched separately.
_UNIT = r"(?:mcg|μg|ug|mg|gm|iu|ku)\b"
_STRENGTH_TOKEN_RE = re.compile(rf"({_NUM})\s*(%|{_UNIT})|({_NUM})\s+g\b", re.IGNORECASE)
_RATIO_RE = re.compile(r"1\s*:\s*\d+")
_RANGE_RE = re.compile(rf"{_NUM}\s*(?:-|–|—|\bto\b)\s*{_NUM}", re.IGNORECASE)
_TO_RANGE_RE = re.compile(
    rf"{_NUM}\s*(?:mcg|μg|ug|mg|g|gm|iu|ku|%)\s+to\s+{_NUM}", re.IGNORECASE
)
_MGOF_RE = re.compile(
    r"(mcg|μg|ug|mg|g|gm|iu|ku)\s*of\b", re.IGNORECASE
)
_PACK_PHRASE_RE = re.compile(r"\bin\b.{0,90}pack.{0,50}$", re.IGNORECASE | re.DOTALL)
_PACK_PAREN_RE = re.compile(r"\(\s*\d[^()]*pack\s*\)", re.IGNORECASE)
_LABEL_RE = re.compile(r"\(([ABC])\)")
_OF_MOLECULE_RE = re.compile(r"\s+of\s+[A-Za-z][A-Za-z\s\-]*$", re.IGNORECASE)
_LEADING_FORM_RE = re.compile(
    r"^(inhaler|tablet|tablets|capsule|capsules|injection|syrup|drops|"
    r"suspension|solution|cream|gel|ointment|lotion|powder|sachet)\b\.?\s*",
    re.IGNORECASE,
)
_CONC_PER_RE = re.compile(
    rf"({_NUM})\s*(mcg|μg|ug|mg|g|gm|iu|ku)\s*per\s*(?:({_NUM})\s*)?ml\b", re.IGNORECASE
)
_CONC_SLASH_RE = re.compile(
    rf"({_NUM})\s*(mcg|μg|ug|mg|g|gm|iu|ku)\s*/\s*(?:({_NUM})\s*)?ml\b", re.IGNORECASE
)
_PERCENT_RE = re.compile(
    rf"({_NUM})\s*%\s*(w\s*/\s*v|w\s*/\s*w|v\s*/\s*v|wv|ww|vv)?", re.IGNORECASE
)
_PLAIN_MULTI_RE = re.compile(
    rf"({_NUM})\s*(mcg|μg|ug|mg|gm|iu|ku)\b", re.IGNORECASE
)
_PLAIN_G_RE = re.compile(rf"({_NUM})\s+g\b", re.IGNORECASE)
_MILLION_RE = re.compile(
    rf"({_NUM})\s*(millions?|lakhs?|billions?)\s*(mcg|μg|ug|mg|g|gm|iu|ku)\b",
    re.IGNORECASE,
)
_MULTIPLIER = {
    "million": 1e6,
    "millions": 1e6,
    "lakh": 1e5,
    "lakhs": 1e5,
    "billion": 1e9,
    "billions": 1e9,
}
_GAUGE_RE = re.compile(r"\b\d+\s*G\b")  # case-sensitive: "26 G" needle gauge
_BARE_ML_RE = re.compile(rf"^({_NUM})\s*ml$", re.IGNORECASE)
_BARE_NUM_RE = re.compile(rf"^({_NUM})$")
_K_AU_RE = re.compile(r"K\s*AU\b", re.IGNORECASE)
_M_UNIT_RE = re.compile(r"^\d+\s*M$", re.IGNORECASE)
_KU_RE = re.compile(rf"^({_NUM})\s*KU$", re.IGNORECASE)

# Canonical-unit sort rank so combo joins are deterministic across unit kinds.
_UNIT_RANK = {
    "mg": 0,
    "iu": 1,
    "ku": 2,
    "%wv": 3,
    "%ww": 4,
    "%vv": 5,
    "mg/ml": 6,
    "iu/ml": 7,
    "ku/ml": 8,
}


def _fmt_num(x: float) -> str:
    if x.is_integer() and abs(x) < 1e15:
        return str(int(x))
    s = f"{x:.6g}"
    if "e" in s or "E" in s:
        s = f"{x:.10f}".rstrip("0").rstrip(".")
    return s


def _to_mg(value: float, unit: str) -> float:
    unit = unit.lower()
    if unit in ("mcg", "μg", "ug"):
        return value * 0.001
    if unit in ("g", "gm"):
        return value * 1000.0
    return value


def _num(token: str) -> float:
    return float(token.replace(",", ""))


def _plain_canon(value: float, unit: str) -> str:
    unit = unit.lower()
    if unit in ("mcg", "μg", "ug", "mg", "g", "gm"):
        return f"{_fmt_num(_to_mg(value, unit))}mg"
    if unit == "iu":
        return f"{_fmt_num(value)}iu"
    return f"{_fmt_num(value)}ku"  # ku


def _strength_sort_key(canon: str) -> tuple[int, float, str]:
    m = re.fullmatch(r"(.+?)(mg/ml|iu/ml|ku/ml|mg|iu|ku|%wv|%ww)", canon)
    if not m:
        return (99, 0.0, canon)
    try:
        return (_UNIT_RANK.get(m.group(2), 50), float(m.group(1)), canon)
    except ValueError:
        return (99, 0.0, canon)


def _parse_single(part: str) -> tuple[str | None, str | None]:
    """Parse one non-combo strength fragment. Returns (canonical, reason)."""
    p = part.strip()
    if not p:
        return None, "absent_strength"
    if _RATIO_RE.search(p):
        return None, "ratio_strength"
    if "±" in p:
        return None, "range_strength"
    if _K_AU_RE.search(p) or _M_UNIT_RE.match(p):
        return None, "complex_unit"
    m = _KU_RE.match(p)
    if m:
        return f"{_fmt_num(_num(m.group(1)))}ku", None
    if _RANGE_RE.search(p) or _TO_RANGE_RE.search(p):
        return None, "range_strength"
    m = _CONC_PER_RE.fullmatch(p) or _CONC_SLASH_RE.fullmatch(p)
    if m:
        amount = _to_mg(_num(m.group(1)), m.group(2))
        vol = _num(m.group(3)) if m.group(3) else 1.0
        if vol == 0:
            return None, "unparseable_strength"
        unit = m.group(2).lower()
        base = "iu" if unit == "iu" else ("ku" if unit == "ku" else "mg")
        per = amount if base == "mg" else _num(m.group(1))
        return f"{_fmt_num(per / vol)}{base}/ml", None
    m = _PERCENT_RE.fullmatch(p)
    if m:
        kind = (m.group(2) or "").replace(" ", "").lower()
        if kind in ("w/w", "ww"):
            suffix = "%ww"
        elif kind in ("v/v", "vv"):
            suffix = "%vv"  # volume-in-volume, never mg/ml-convertible
        else:
            suffix = "%wv"  # bare % is w/v implied
        return f"{_fmt_num(_num(m.group(1)))}{suffix}", None
    m = _MILLION_RE.fullmatch(p)
    if m:
        mult = _MULTIPLIER[m.group(2).lower()]
        return _plain_canon(_num(m.group(1)) * mult, m.group(3)), None
    m = _PLAIN_MULTI_RE.fullmatch(p)
    if m:
        return _plain_canon(_num(m.group(1)), m.group(2)), None
    m = _PLAIN_G_RE.fullmatch(p)
    if m:
        return _plain_canon(_num(m.group(1)), "g"), None
    if _BARE_ML_RE.match(p):
        return None, "volume_not_strength"
    if _BARE_NUM_RE.match(p):
        return None, "missing_unit"
    # Trailing qualifier words after a leading strength token: only the
    # measured "100 mg elemental iron" shape is accepted ("100% mercury
    # free" device text must NOT become a strength).
    m = _STRENGTH_TOKEN_RE.match(p)
    if (m and _PERCENT_RE.fullmatch(p) is None
            and _PLAIN_MULTI_RE.fullmatch(p) is None
            and _PLAIN_G_RE.fullmatch(p) is None):
        rest = p[m.end():].strip().casefold()
        if rest in ("elemental iron", "iron"):
            sub, _ = _parse_single(m.group(0))
            if sub is not None:
                return sub, None
    # Leading count-form words ("1 Tablet 750 mg" inside combipack strengths).
    m = re.sub(
        r"^(\d+\s*)?(tablets?|capsules?)\b\.?\s*", "", p, flags=re.IGNORECASE
    )
    if m != p:
        return _parse_single(m)
    return None, "unparseable_strength"


def _split_combo(s: str) -> list[str]:
    if "+" in s:
        return [p.strip() for p in s.split("+") if p.strip()]
    if re.search(r"\bwith\b", s, re.IGNORECASE):
        chunks = [c.strip() for c in re.split(r"\bwith\b", s, flags=re.IGNORECASE)]
        if len(chunks) == 2 and all(_STRENGTH_TOKEN_RE.search(c) for c in chunks):
            out = []
            for c in chunks:
                c = re.sub(r"\s+[A-Za-z][A-Za-z\s\-]*$", "", c).strip()
                out.append(c)
            return [c for c in out if c]
    return [s]


def _split_slash(part: str) -> list[str] | None:
    """Split "6/400 MCG"-style slash combos; None when it is a concentration."""
    if "/" not in part:
        return None
    if _CONC_SLASH_RE.search(part):
        return None
    chunks = [c.strip() for c in part.split("/")]
    if len(chunks) < 2 or any(not c for c in chunks):
        return None
    unit_m = re.search(r"(mcg|μg|ug|mg|g|gm|iu|ku|%)\s*$", chunks[-1], re.IGNORECASE)
    if not unit_m:
        return None
    tail_unit = unit_m.group(1)
    expanded: list[str] = []
    for c in chunks[:-1]:
        if re.fullmatch(_NUM, c):
            expanded.append(f"{c} {tail_unit}" if tail_unit != "%" else f"{c}%")
        else:
            expanded.append(c)
    expanded.append(chunks[-1])
    return expanded


def _preprocess_strength(s: str) -> str:
    t = clean_text(s)
    t = t.replace("\uFFFD", " ")  # source-corrupted char (e.g. NPPA id 855)
    t = _PACK_PAREN_RE.sub(" ", t)
    t = _PACK_PHRASE_RE.sub("", t)
    t = _LABEL_RE.sub(" ", t)  # (A)/(B)/(C) pack labels go before splicing
    if re.search(r"\([^()]*\+[^()]*\)", t):
        # Combipack strengths group inner "+"-parts in parens
        # ("1 Tablet (750 mg+ 37.5 mg) (B)"); splice them into the top level.
        t = t.replace("(", " ").replace(")", " ")
    t = _MGOF_RE.sub(r"\1 OF", t)  # "52 MGOF LEVONORGESTREL" -> "52 MG ..."
    t = _LEADING_FORM_RE.sub("", t)
    t = _OF_MOLECULE_RE.sub("", t)
    return _WS_RE.sub(" ", t).strip()


def parse_strength_parts(s: str | None) -> tuple[list[str] | None, str | None]:
    """Parse a strength string to positionally ordered canonical strengths.

    Returns (parts | None, reason | None). Slash-combos ("6/400 MCG") and
    "+"-combos keep file order here so callers can bind them positionally
    to molecule order; use parse_strength for the sorted canonical join.
    """
    if s is None or not s.strip() or s.strip() == "--":
        return None, "absent_strength"
    t = _preprocess_strength(s)
    if not t or t == "--":
        return None, "absent_strength"
    low = t.casefold()
    if "contains:" in low or "toxoid" in low:
        return None, "degenerate_composition"
    chunks = _split_combo(t)
    ordered: list[str] = []
    for chunk in chunks:
        sub = _split_slash(chunk)
        pieces = sub if sub is not None else [chunk]
        for piece in pieces:
            canon, reason = _parse_single(piece)
            if canon is None and reason == "missing_unit":
                ordered.append("")  # placeholder: may inherit sibling unit
            elif canon is None:
                return None, reason
            else:
                ordered.append(canon)
    # Bare numbers inherit the single distinct unit of their siblings
    # ("60 (A) + 30 mg (B)" -> "60mg+30mg"; measured NPPA defect, ids 7/152).
    if "" in ordered:
        sib_units = {
            re.search(r"(mg|iu|ku|%wv|%ww)$", c).group(1)  # type: ignore[union-attr]
            for c in ordered
            if c and re.search(r"(mg|iu|ku|%wv|%ww)$", c)
        }
        if len(sib_units) != 1:
            return None, "missing_unit"
        (unit,) = sib_units
        chunks_raw = _split_combo(t)
        raws: list[str] = []
        for chunk in chunks_raw:
            sub = _split_slash(chunk)
            raws.extend(sub if sub is not None else [chunk])
        fixed: list[str] = []
        for raw, cur in zip(raws, ordered, strict=True):
            if cur == "":
                canon, reason = _parse_single(f"{raw} {unit}")
                if canon is None:
                    return None, reason
                fixed.append(canon)
            else:
                fixed.append(cur)
        ordered = fixed
    if any(not c for c in ordered):
        return None, "missing_unit"
    return ordered, None


def parse_strength(s: str | None) -> tuple[str | None, str | None]:
    """Parse a strength string to the sorted canonical join ("125mg+500mg").

    Returns (canonical | None, reason | None). A None canonical is never a
    fake strength: the row stays valid and goes to the unmatched queue.
    """
    parts, reason = parse_strength_parts(s)
    if parts is None:
        return None, reason
    return "+".join(sorted(parts, key=_strength_sort_key)), None


# ---------------------------------------------------------------------------
# F. Form canonicalization
# ---------------------------------------------------------------------------

_FORM_BASE: dict[str, str] = {
    # Oral solids are merged per the Phase 3 grammar contract.
    "tablet": "tablet",
    "tablets": "tablet",
    "tab": "tablet",
    "tabs": "tablet",
    "tabelts": "tablet",
    "tabets": "tablet",
    "capulses": "tablet",
    "cap": "tablet",
    "caps": "tablet",
    "capsule": "tablet",
    "capsules": "tablet",
    "chewable tablet": "tablet",
    "dispersible tablet": "tablet",
    "effervescent tablet": "tablet",
    "sublingual tablet": "tablet",
    "tablet dt": "tablet",
    "tablet er": "tablet",
    "sr tablet": "tablet",
    "er capsule": "tablet",
    "modified release tablet": "tablet",
    "modified release capsule": "tablet",
    # Injectables.
    "injection": "injection",
    "injections": "injection",
    "inj": "injection",
    "powder for injection": "injection",
    "sterile powder for injection": "injection",
    "infusion": "infusion",
    "infusions": "infusion",
    "intravenous infusion": "infusion",
    # Liquids.
    "syrup": "syrup",
    "syrups": "syrup",
    "dry syrup": "dry_syrup",
    "dry_syrup": "dry_syrup",
    "suspension": "suspension",
    "susp": "suspension",
    "suspensions": "suspension",
    "oral suspension": "suspension",
    "oral liquid": "solution",
    "oral solution": "solution",
    "solution": "solution",
    "solutions": "solution",
    "liquid": "solution",
    # Drops stay route-specific: eye/ear/nasal must never match each other.
    "drops": "drops",
    "eye drop": "eye_drops",
    "eye drops": "eye_drops",
    "eye_drops": "eye_drops",
    "ear drop": "ear_drops",
    "ear drops": "ear_drops",
    "ear_drops": "ear_drops",
    "nasal drop": "nasal_drops",
    "nasal drops": "nasal_drops",
    "nasal_drops": "nasal_drops",
    "oral drop": "oral_drops",
    "oral drops": "oral_drops",
    # Inhalation family.
    "inhaler": "inhaler",
    "inhalers": "inhaler",
    "inhalation": "inhalation",
    "inhalations": "inhalation",
    "rotacap": "rotacaps",
    "rotacaps": "rotacaps",
    "respule": "respules",
    "respules": "respules",
    "respirator suspension": "respules",
    "nebuliser": "respules",
    "nebulizer": "respules",
    "nebuliser suspension": "respules",
    "mdi": "inhaler",
    "nasal spray": "nasal_spray",
    "nasal_spray": "nasal_spray",
    "nasal sprays": "nasal_spray",
    "spray": "nasal_spray",
    "sprays": "nasal_spray",
    # Topical family.
    "cream": "cream",
    "creams": "cream",
    "gel": "gel",
    "gels": "gel",
    "ointment": "ointment",
    "ointments": "ointment",
    "lotion": "lotion",
    "lotions": "lotion",
    "paste": "paste",
    "paint": "paint",
    "shampoo": "shampoo",
    "soap": "soap",
    "liniment": "liniment",
    "powder for external use": "topical_powder",
    # Miscellaneous exact-only bases.
    "suppository": "suppository",
    "suppositories": "suppository",
    "pessary": "pessary",
    "pessaries": "pessary",
    "granules": "granules",
    "sachet": "sachet",
    "sachets": "sachet",
    "gargle": "gargle",
    "gargles": "gargle",
    "mouth paint": "mouth_paint",
    "mouth wash": "mouth_wash",
    "mouthwash": "mouth_wash",
    "enema": "enema",
    "enemas": "enema",
    "condom": "condom",
    "condoms": "condom",
    "iud": "iud",
    "gum": "gum",
    "gums": "gum",
    "chewing gum": "gum",
    "lozenge": "lozenge",
    "lozenges": "lozenge",
    "pastille": "pastille",
    "pastilles": "pastille",
    "vaccine": "vaccine",
    "kit": "kit",
    "device": "device",
    "patch": "patch",
    "patches": "patch",
    "oil": "oil",
    "oils": "oil",
    "liquids": "solution",
    "powder": "powder",
    "powders": "powder",
    "vial": "injection",
    "vials": "injection",
    "polypack": "injection",
    "ampoule": "injection",
    "ampoules": "injection",
    "pen": "pen",
    "pens": "pen",
    "cartridge": "cartridge",
    "oxygen": "oxygen",
    "foam": "foam",
    "scrub": "scrub",
}

_FORM_FAMILY: dict[str, str] = {
    "syrup": "liquid",
    "suspension": "liquid",
    "dry_syrup": "liquid",
    "solution": "liquid",
    "inhaler": "inhalation",
    "inhalation": "inhalation",
    "rotacaps": "inhalation",
    "respules": "inhalation",
    "nasal_spray": "inhalation",
    "cream": "topical",
    "gel": "topical",
    "ointment": "topical",
    "lotion": "topical",
    "paste": "topical",
    "paint": "topical",
    "shampoo": "topical",
    "soap": "topical",
    "liniment": "topical",
    "topical_powder": "topical",
}

_MODIFIER_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = tuple(
    (re.compile(pat, re.IGNORECASE), token)
    for pat, token in [
        (r"sustained[\s\-]*release", "sustained_release"),
        (r"prolonged[\s\-]*release", "prolonged_release"),
        (r"extended[\s\-]*release", "extended_release"),
        (r"modified[\s\-]*release", "modified_release"),
        (r"delayed[\s\-]*release", "delayed_release"),
        (r"controlled[\s\-]*release", "controlled_release"),
        (r"\(cr\)", "controlled_release"),
        (r"enteric[\s\-]*coat(?:ed|ing)?", "enteric_coated"),
        (r"gastro[\s\-]*resistant", "gastro_resistant"),
        (r"dispersible", "dispersible"),
        (r"effervescent", "effervescent"),
        (r"chewable", "chewable"),
        (r"sublingual", "sublingual"),
    ]
)


def canonical_form(raw: str | None) -> str:
    """Map a raw form string to its canonical base ("INHALER" -> "inhaler")."""
    if raw is None:
        return "unknown"
    key = clean_text(raw).casefold()
    if key in _FORM_BASE:
        return _FORM_BASE[key]
    # Multi-form NPPA values ("Effervescent/ Dispersible/ Enteric coated
    # Tablet") carry the base as their last token.
    for token in re.split(r"[\/\n]+", key):
        token = token.strip()
        if token in _FORM_BASE:
            return _FORM_BASE[token]
    words = key.split()
    for n in range(min(3, len(words)), 0, -1):
        for i in range(len(words) - n + 1):
            phrase = " ".join(words[i : i + n])
            if phrase in _FORM_BASE:
                return _FORM_BASE[phrase]
    return key


def form_family(base: str) -> str:
    """Map a canonical base form to its fallback family (default: itself)."""
    return _FORM_FAMILY.get(base, base)


def extract_modifiers(text: str | None) -> tuple[str, ...]:
    """Extract release/coating modifiers; they never join the base match key."""
    if not text:
        return ()
    found = [token for pat, token in _MODIFIER_PATTERNS if pat.search(text)]
    return tuple(sorted(set(found)))


# ---------------------------------------------------------------------------
# G. JAP pack parsing (unitSize -> per-unit price divisor)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PackInfo:
    kind: str  # count | volume_ml | dose_count | weight_g | vial
    value: float
    raw: str


_WORD_NUMBERS = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
    "pair": 2,
}

_COUNT_X_RE = re.compile(r"(\d+(?:\.\d+)?)\s*x\s*(\d+(?:\.\d+)?)\s*'s", re.IGNORECASE)
_COUNT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*'s", re.IGNORECASE)
_S_CONTAINER_RE = re.compile(r"(\d+(?:\.\d+)?)\s*s\s+in\s+containers?", re.IGNORECASE)
_MDI_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(mdi|md)\b", re.IGNORECASE)
_DOSES_RE = re.compile(r"(\d+(?:\.\d+)?)\s*doses?\b", re.IGNORECASE)
_ML_RE = re.compile(r"(\d+(?:\.\d+)?)\s*ml\b", re.IGNORECASE)
_KG_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg\b", re.IGNORECASE)
_G_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(gms?|grams?|g)\b", re.IGNORECASE)
_SACHET_RE = re.compile(r"(\d+(?:\.\d+)?)\s*sachets?\b", re.IGNORECASE)
_PACK_OF_RE = re.compile(r"pack\s+of\s+(\d+(?:\.\d+)?)", re.IGNORECASE)
_PCS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*pcs?\b", re.IGNORECASE)
_BUDS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(ear\s*buds?|wipes?)\b", re.IGNORECASE)
_STRIPS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*strips?\b", re.IGNORECASE)
_VIAL_WORD_RE = re.compile(r"\bvials?\b", re.IGNORECASE)
_WFI_RE = re.compile(r"\bwfi\b", re.IGNORECASE)
_WORD_NUM_RE = re.compile(
    r"\b(one|two|three|four|five|six|seven|eight|nine|ten|pair)\b"
    r"(?:\s*x\s*(\d+(?:\.\d+)?))?",
    re.IGNORECASE,
)


def parse_pack(unit_size: str | None) -> tuple[PackInfo | None, str | None]:
    """Parse JAP unitSize to (PackInfo | None, reason | None)."""
    if unit_size is None or not unit_size.strip():
        return None, "unparseable_pack"
    raw = clean_text(unit_size).replace("\u2019", "'")
    low = raw.casefold()
    m = _MDI_RE.search(raw)
    if m and "mono" not in low:
        # "200 MDI" / "70 MD" (guard: "10 ml /vial" has no md-word boundary hit).
        return PackInfo("dose_count", float(m.group(1)), unit_size), None
    m = _DOSES_RE.search(raw)
    if m:
        return PackInfo("dose_count", float(m.group(1)), unit_size), None
    m = _ML_RE.search(raw)
    if m:
        return PackInfo("volume_ml", float(m.group(1)), unit_size), None
    m = _KG_RE.search(raw)
    if m:
        return PackInfo("weight_g", float(m.group(1)) * 1000.0, unit_size), None
    m = _G_RE.search(raw)
    if m and "gel cool" not in low and "carry bag" not in low:
        # Guarded against kit minuteriae ("Gel Cool Pack ... Carry Bag").
        return PackInfo("weight_g", float(m.group(1)), unit_size), None
    if _VIAL_WORD_RE.search(raw) or _WFI_RE.search(low) or "ampoule" in low or "amp." in low:
        return PackInfo("vial", 1.0, unit_size), None
    m = _COUNT_X_RE.search(raw)
    if m:
        return PackInfo("count", float(m.group(1)) * float(m.group(2)), unit_size), None
    m = _COUNT_RE.search(raw)
    if m:
        return PackInfo("count", float(m.group(1)), unit_size), None
    m = _SACHET_RE.search(raw)
    if m:
        return PackInfo("count", float(m.group(1)), unit_size), None
    m = _S_CONTAINER_RE.search(raw)
    if m:
        return PackInfo("count", float(m.group(1)), unit_size), None
    m = _PACK_OF_RE.search(raw)
    if m:
        return PackInfo("count", float(m.group(1)), unit_size), None
    m = _PCS_RE.search(raw)
    if m:
        return PackInfo("count", float(m.group(1)), unit_size), None
    m = _BUDS_RE.search(raw)
    if m:
        return PackInfo("count", float(m.group(1)), unit_size), None
    m = _STRIPS_RE.search(raw)
    if m:
        return PackInfo("count", float(m.group(1)), unit_size), None
    if "combipack of" in low or "combikit of" in low:
        m = re.search(r"of\s+(\d+(?:\.\d+)?)\s+tablets?", low)
        if m:
            return PackInfo("count", float(m.group(1)), unit_size), None
    m = _WORD_NUM_RE.search(raw)
    if m and ("mono" in low or "pack" in low or "strip" in low or "pair" in low):
        total = float(_WORD_NUMBERS[m.group(1).casefold()])
        if m.group(2):
            total *= float(m.group(2))
        return PackInfo("count", total, unit_size), None
    return None, "unparseable_pack"


# ---------------------------------------------------------------------------
# FormulationKey
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FormulationKey:
    """Canonical join key: sorted "molecule:strength" pairs + base form."""

    molecules: tuple[str, ...]
    strengths: tuple[str, ...]
    pairs: tuple[str, ...]
    form: str

    def key(self) -> str:
        return "+".join(self.pairs) + "|" + self.form


def make_key(
    molecules: list[str], strengths: list[str | None], form: str
) -> FormulationKey:
    """Build a FormulationKey; unknown strengths bind as "" (never faked)."""
    norm_strengths = [s if s is not None else "" for s in strengths]
    pairs = tuple(
        sorted(f"{m}:{s}" for m, s in zip(molecules, norm_strengths, strict=True))
    )
    return FormulationKey(
        molecules=tuple(sorted(molecules)),
        strengths=tuple(sorted(norm_strengths, key=_strength_sort_key)),
        pairs=pairs,
        form=form,
    )


# ---------------------------------------------------------------------------
# E. Molecule aliases (seed table; grown via evidence-backed iteration)
# ---------------------------------------------------------------------------

ALIASES: dict[str, str] = {
    "acetylsalicylic acid": "aspirin",
    "frusemide": "furosemide",
    "formoteral": "formoterol",
    "amoxycillin": "amoxicillin",
    "metformin hydrochloride": "metformin",
    "metformin hcl": "metformin",
    "s(-)amlodipine": "amlodipine",
    "s-amlodipine": "amlodipine",
    "levo-thyroxine": "levothyroxine",
    "cetrizine": "cetirizine",
    "nimesulid": "nimesulide",
    "medroxyprogesteroneacetate": "medroxyprogesterone acetate",
}

# Salt suffixes stripped ONLY as whole trailing words (so "calcium phosphate"
# and "hydrochlorothiazide" are never harmed) and only after the alias-table
# lookup. Hydrate words loop so "formoterol fumarate dihydrate" resolves.
_SALT_STRIP_RE = re.compile(
    r"\s+(hydrochloride|hcl|sodium|potassium|succinate|besilate|besylate|"
    r"maleate|tartrate|fumarate|hydrobromide|mesylate|mesilate|diethylamine|"
    r"embonate|pamoate|monohydrate|dihydrate|trihydrate|tetrahydrate|"
    r"hydrate|anhydrous)\s*$",
    re.IGNORECASE,
)
_SALT_WORDS = frozenset(
    "acetate hydrochloride hcl sodium potassium succinate besilate besylate "
    "maleate tartrate fumarate hydrobromide mesylate mesilate diethylamine "
    "embonate pamoate monohydrate dihydrate trihydrate hydrate sulfate "
    "sulphate phosphate citrate carbonate".split()
)
_S_AMLODIPINE_RE = re.compile(r"s\(\s*-\s*\)\s*", re.IGNORECASE)


def salt_strip(name: str) -> str:
    """Strip generic salt/hydrate suffix words ("amlodipine besilate"->"amlodipine")."""
    prev, cur = "", name
    for _ in range(3):
        cur = _SALT_STRIP_RE.sub("", cur).strip()
        if cur == prev:
            break
        prev = cur
    return cur


def try_spaceless_split(name: str) -> str | None:
    """Split spaceless compounds ("medroxyprogesteroneacetate").

    Only fires when the name has no spaces, starts with a known molecule word,
    and the remainder is a known salt word — never a blind guess.
    """
    if " " in name:
        return None
    known: set[str] = set()
    for entry in list(ALIASES) + list(ALIASES.values()):
        known.update(w for w in entry.split() if len(w) >= 5)
    for mol in sorted(known, key=len, reverse=True):
        if name.startswith(mol) and name[len(mol):] in _SALT_WORDS:
            return f"{mol} {name[len(mol):]}"
    return None


def _base_clean_molecule(name: str) -> str:
    m = fold(name)
    m = _LABEL_RE.sub(" ", m)
    m = _S_AMLODIPINE_RE.sub("s(-)", m)
    return _WS_RE.sub(" ", m).strip()


def apply_alias(molecule: str) -> str:
    """Resolve one cleaned molecule through alias table, spaceless, salt rules."""
    if molecule in ALIASES:
        return ALIASES[molecule]
    split = try_spaceless_split(molecule)
    if split is not None:
        return ALIASES.get(split, split)
    stripped = salt_strip(molecule)
    if stripped and stripped != molecule:
        return ALIASES.get(stripped, stripped)
    return molecule


def canonical_molecules(names: list[str], *, use_alias: bool = True) -> list[str]:
    """Clean molecule names; with use_alias=False the pre-alias (stage-1) form."""
    out = [_base_clean_molecule(n) for n in names]
    if use_alias:
        out = [apply_alias(m) for m in out]
    return out


# ---------------------------------------------------------------------------
# NPPA formulation_raw -> molecule list
# ---------------------------------------------------------------------------

_NPPA_SYNONYM_PAREN_RE = re.compile(r"\s*\([^()]*\)")
_NPPA_INNER_PLUS_RE = re.compile(r"\(([^()]+\+[^()]+)\)[\]\)]?")
_NPPA_TRAILING_STRENGTH_RE = re.compile(
    rf"\s*{_NUM}\s*(%|(?:mcg|μg|ug|mg|g|gm|iu|ku)\b)\s*$", re.IGNORECASE
)


def split_nppa_molecules(
    formulation_raw: str, n_strengths: int | None = None
) -> list[str]:
    """Split NPPA formulation_raw into raw molecule strings.

    Handles "+" combos, "(A)"/"(B)" pack labels, synonym parens
    ("ASCORBIC ACID (VITAMIN C)"), inner-plus groups
    ("CO-TRIMOXAZOLE (SULPHAMETHOXAZOLE(A)+TRIMETHOPRIM(B)]" -> the inner
    pair), and hyphenated pairs ("Sulphadoxine -Pyrimethamine") only when
    the strength count demands more molecules than "+" gives (so
    "AMPHOTERICIN B - LIPOSOMAL" variant descriptors are never split).
    """
    t = clean_text(formulation_raw).replace("\n", " ")
    t = _WS_RE.sub(" ", t).strip()
    inner = _NPPA_INNER_PLUS_RE.search(t)
    if inner:
        parts = [p.strip() for p in inner.group(1).split("+")]
    else:
        t = _NPPA_SYNONYM_PAREN_RE.sub(" ", t)
        t = _LABEL_RE.sub(" ", t)
        parts = [p.strip() for p in t.split("+")]
    parts = [re.sub(r"\s+plain\s*$", "", p, flags=re.IGNORECASE) for p in parts]
    parts = [_NPPA_TRAILING_STRENGTH_RE.sub("", p).strip() for p in parts]
    parts = [p for p in (p.strip(" ]") for p in parts) if p]
    if n_strengths is not None and 0 < len(parts) < n_strengths:
        widened: list[str] = []
        for p in parts:
            widened.extend(
                [q.strip() for q in re.split(r"\s+-\s*", p) if q.strip()]
            )
        parts = widened
    return parts


# ---------------------------------------------------------------------------
# B. JAP genericName -> (molecules, strengths, form, modifiers)
# ---------------------------------------------------------------------------


@dataclass
class JapComposition:
    molecules: tuple[str, ...]  # raw cleaned molecule strings, in order
    strengths: tuple[str | None, ...]  # raw bound strength strings ("": absent)
    form_raw: str | None
    modifiers: tuple[str, ...]
    notes: str
    ok: bool
    reason: str | None  # complex_composition | unparseable_* | None
    low_confidence: bool = False


_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")
_INNERMOST_PAREN_RE = re.compile(r"\(([^()]*)\)")
_PACK_COUNT_PAREN_RE = re.compile(
    r"^\d+\s*(tablets?|capsules?|tabs?|caps?)?\s*$", re.IGNORECASE
)
_STRENGTH_WORDS_RE = re.compile(
    r"^[\d\s\.,/%xX:\-–+a-zμ]*$", re.IGNORECASE
)
_FLAVOUR_RE = re.compile(
    r"\b(flavou?r|orange|mint|chocolate|pineapple|mango|lemon|tutti|frutti|"
    r"raspberry|strawberry|banana|vanilla|cola|mixed fruit)\b",
    re.IGNORECASE,
)
_VITAMIN_LIST_RE = re.compile(r"\bB\s?\d\b")
_COMPLEX_NAME_RE = re.compile(
    r"multivitamins?|multi-vitamin|b-complex|\bb\s*complex\b", re.IGNORECASE
)
_WITH_BASE_RE = re.compile(
    r"\s+with\s+[A-Za-z][A-Za-z\s\-]*base\s*$", re.IGNORECASE
)
_SEG_SPLIT_RE = re.compile(r"\s*&\s*|\s+and\s+|,\s*(?=[A-Za-z\x00])")
_DESCRIPTOR_RE = re.compile(
    r"\b(paediatric|pediatric|adult|junior|forte|sterile|sterilized|sterilised|"
    r"buffered|flavoured|flavored|alkalyser|alkalyzer|soft\s+gelatin|gelatin|"
    r"expectorant|nfi)\b",
    re.IGNORECASE,
)
_FORM_LEFTOVER_RE = re.compile(
    r"\b(amp|ampoule|ampoules|vial|vials|polypack|poly\s*pack|with\s+wfi|wfi|"
    # "Water for Injection" keeps its "for Injection" (molecule, not pack).
    r"(?<!water\s)(for\s+injection)|for\s+intravenous(\s+use)?|"
    r"for\s+i\.?\s*v\.?(\s+use)?)\b",
    re.IGNORECASE,
)
# Route/site adjectives stranded after form removal ("Bimatoprost
# Ophthalmic Solution" -> molecule "Bimatoprost", form "solution").
_ROUTE_RE = re.compile(
    r"\b(ophthalmic|ocular|topical|nasal|oral|rectal|vaginal|otic|dental|"
    r"antiseptic|auricular)\b",
    re.IGNORECASE,
)
# Trailing pack-flavour/base phrases ("Syrup with Menthol base",
# "Syrup in Flavour base", "Enzyme syrup mixed fruit flavour").
_FLAVOUR_BASE_RE = re.compile(
    r"\s+(with\s+[A-Za-z][A-Za-z\s\-]*base|in\s+(a\s+)?(flavou?red?\s+)?base|"
    r"(?:[A-Za-z]+\s+){1,3}flavou?r)\s*$",
    re.IGNORECASE,
)
# Inline needle gauges ("26 G" spaced) are device sizes, never gram
# strengths. ("18G" never tokenizes; "26 G" is stripped here with a note.)
_GAUGE_INLINE_RE = re.compile(r"\s*\b\d+(?:\.\d+)?\s*G\b")
# Trailing "per ..." residue left after the real strength bound elsewhere
# ("Cyproheptadine Syrup per 5ml" with inline "2mg").
_PER_RESIDUE_RE = re.compile(r"\s+per\s+\d*\s*[A-Za-z]*\s*$", re.IGNORECASE)
_WITH_WFI_RE = re.compile(r"\s+with(\s+wfi)?\s*$", re.IGNORECASE)
# Leading form-of phrases ("Syrup of Iron" -> "Iron").
_LEADING_FORM_OF_RE = re.compile(
    r"^(syrup|syrups|solution|solutions|suspension|suspensions|oil|oils|"
    r"powder|powders)\s+of\s+",
    re.IGNORECASE,
)
_STRENGTH_FIND_RE = re.compile(
    rf"{_NUM}\s*(?:mcg|μg|ug|mg|gm|iu|ku)\b(?:\s*per\s*(?:{_NUM}\s*)?ml\b)?"
    rf"|{_NUM}\s+g\b"
    rf"|{_NUM}\s*(?:mcg|μg|ug|mg|gm|iu|ku)\s*/\s*(?:{_NUM}\s*)?ml\b"
    # Bare "%" needs a lookahead, not \b: "%" is a non-word char, so \b
    # never matches right after it ("5%" at end of string).
    rf"|{_NUM}\s*%(?:\s*(?:w\s*/\s*v|w\s*/\s*w|v\s*/\s*v|wv|ww|vv)|(?![A-Za-z]))"
    rf"|{_NUM}\s*(?:millions?|lakhs?|billions?)\s*(?:mcg|μg|ug|mg|gm|iu|ku)\b"
    rf"|\b100\s*K\s*AU\b|\b\d+\s*M\b|1\s*:\s*\d+"
    rf"|{_NUM}\s*ml\b",
    re.IGNORECASE,
)

# Segments that are bare glue/device words (brand punctuation splits like
# "Pre & Pro-Biotic", device attributes like "No. 22") must never become
# molecules.
_BARE_WORD_VETO = frozenset(
    "pre pro plus and with for new old super extra max mini no size round "
    "grey gray disposable sterile sterilized sterilised".split()
)
# Splitter for paren-composition content (allows "with": "Cetrimide 0.10%
# w/w with Lidocaine 1.0% w/w"); the main split stays stricter.
_PAREN_CHUNK_RE = re.compile(r"\s*&\s*|\s+and\s+|\s+with\s+|,\s*")


# Form keywords, longest phrases first for match priority.
_FORM_KEYWORDS: tuple[str, ...] = (
    "powder for inhalation",
    "powder for injection",
    "powder for oral",
    "oral suspension",
    "oral solution",
    "respirator suspension",
    "nebuliser suspension",
    "nasal spray",
    "eye drops",
    "ear drops",
    "nasal drops",
    "oral drops",
    "mouth paint",
    "mouth wash",
    "dry syrup",
    "chewing gum",
    "chewing gum",
    "tablets",
    "tablet",
    "capsules",
    "capsule",
    "injections",
    "injection",
    "infusions",
    "infusion",
    "syrup",
    "syrups",
    "suspension",
    "suspensions",
    "solution",
    "solutions",
    "sachets",
    "pens",
    "oils",
    "powders",
    "inhalers",
    "inhalations",
    "suppositories",
    "pessaries",
    "gargles",
    "enemas",
    "sprays",
    "gels",
    "creams",
    "ointments",
    "lotions",
    "eye drop",
    "ear drop",
    "nasal drop",
    "oral drop",
    "tabelts",
    "tabets",
    "capulses",
    "respules",
    "respule",
    "rotacaps",
    "rotacap",
    "inhaler",
    "inhalation",
    "nebuliser",
    "nebulizer",
    "granules",
    "sachet",
    "drops",
    "cream",
    "ointment",
    "lotion",
    "shampoo",
    "gargle",
    "enema",
    "pessary",
    "suppository",
    "paste",
    "paint",
    "spray",
    "powder",
    "gel",
    "soap",
    "oil",
    "liquid",
    "syringe",
    "ampoule",
    "polypack",
    "vial",
    "pen",
    "cartridge",
    "patch",
    "lozenge",
    "gum",
    "kit",
    "device",
    "elixir",
    "tonic",
    "liniment",
    "foam",
    "scrub",
    "condom",
)


def _classify_paren(text: str) -> str:
    """Classify a parenthetical group: pack_count | modifier | strength |
    flavour | ratio | complex | note."""
    t = text.strip()
    if not t:
        return "note"
    if _PACK_COUNT_PAREN_RE.match(t) or "'s" in t or re.match(
        r"^\d+\s*x\s*\d+", t, re.IGNORECASE
    ):
        return "pack_count"
    if "(CR)" in f"({t})" and re.fullmatch(r"CR", t, re.IGNORECASE):
        return "modifier"
    for pat, _ in _MODIFIER_PATTERNS:
        if pat.search(t):
            return "modifier"
    if "±" in t or _VITAMIN_LIST_RE.search(t):
        return "complex"
    if _GAUGE_RE.search(t):
        # Needle gauge ("18G", "26 G") is a device size, never a strength.
        return "note"
    if re.search(r":\s*[A-Za-z]", t):
        # Label-led prose ("Each pack contains: ...") is never a strength.
        return "note"
    if _RATIO_RE.search(t):
        return "strength" if _STRENGTH_TOKEN_RE.search(t) else "ratio"
    if _STRENGTH_TOKEN_RE.search(t) and _STRENGTH_WORDS_RE.match(t):
        return "strength"
    if _FLAVOUR_RE.search(t):
        return "flavour"
    return "note"


def _is_strength_tail(tail: str) -> bool:
    tail = _PLACEHOLDER_RE.sub(" ", tail)
    tail = re.sub(
        r"(\d|\s|mg|mcg|μg|ug|g|gm|iu|ku|ml|per|w/v|w/w|wv|ww|/|%|\.|,)+",
        " ",
        tail,
        flags=re.IGNORECASE,
    )
    return not re.search(r"[A-Za-z]", tail)


def _detect_form(main: str) -> tuple[str | None, str]:
    """Find the trailing form word; return (form_raw, main_without_form)."""
    low = main.casefold()
    best: tuple[int, int, str] | None = None
    for kw in _FORM_KEYWORDS:
        start = 0
        while True:
            i = low.find(kw, start)
            if i < 0:
                break
            end = i + len(kw)
            before_ok = i == 0 or not low[i - 1].isalpha()
            after_ok = end >= len(low) or not low[end].isalpha()
            if before_ok and after_ok:
                tail = main[end:]
                if not tail.strip() or _is_strength_tail(tail):
                    # Longest match wins (so "eye drops" beats the nested
                    # "drops"); ties break rightmost.
                    if best is None or len(main[i:end]) > len(best[2]) or (
                        len(main[i:end]) == len(best[2]) and i >= best[0]
                    ):
                        best = (i, end, main[i:end])
            start = end
    if best is None:
        return None, main
    i, end, raw = best
    return raw, (main[:i] + " " + main[end:])


def extract_jap_composition(generic_name: str) -> JapComposition:
    """Split a JAP genericName into molecules + bound strengths + form."""
    notes: list[str] = []
    t = clean_text(generic_name).replace("\u2019", "'")
    prefix = re.match(r"^(combipack|combikit)\s+of\s+", t, re.IGNORECASE)
    if prefix:
        notes.append(f"dropped pack prefix {prefix.group(0).strip()!r}")
        t = t[prefix.end():]
    if _COMPLEX_NAME_RE.search(t):
        return JapComposition((), (), None, (), "; ".join(notes), False,
                              "complex_composition")
    # Hoist innermost parens to placeholders (handles nesting inside-out).
    groups: list[str] = []
    while True:
        m = _INNERMOST_PAREN_RE.search(t)
        if not m:
            break
        groups.append(m.group(1))
        t = t[: m.start()] + f"\x00{len(groups) - 1}\x00" + t[m.end():]
    t = _FLAVOUR_BASE_RE.sub("", t)
    form_raw, main = _detect_form(t)
    split = [s for s in _SEG_SPLIT_RE.split(main) if s.strip()]
    # Placeholder-only segments (", (...)" splits) rejoin the global pool.
    raw_segments: list[str] = []
    global_ph: list[str] = []
    for s in split:
        phs = _PLACEHOLDER_RE.findall(s)
        if phs and not _PLACEHOLDER_RE.sub(" ", s).strip(" -,"):
            global_ph.extend(phs)
        else:
            raw_segments.append(s)
    if len(raw_segments) > 3:
        notes.append(f"{len(raw_segments)} molecule segments")
        return JapComposition((), (), form_raw, (), "; ".join(notes), False,
                              "complex_composition")
    if not raw_segments:
        return JapComposition((), (), form_raw, (), "; ".join(notes), False,
                              "unparseable_molecule")

    # Per-segment state; the paren-composition splice below may EXPAND this
    # list (a combo-name segment replaced by its parenthesised composition).
    seg_texts: list[str] = list(raw_segments)
    seg_phs: list[list[str]] = [_PLACEHOLDER_RE.findall(s) for s in seg_texts]
    seg_mods: list[list[str]] = [[] for _ in seg_texts]
    seg_strength: list[str | None] = [None for _ in seg_texts]
    seg_ratio: list[bool] = [False for _ in seg_texts]

    def _handle_paren(ph: str, idx: int | None) -> str | None:
        """Classify one group; returns complex flag or None. Strength-groups
        accumulate into paren_strengths for the caller to assign."""
        kind = _classify_paren(groups[int(ph)])
        content = groups[int(ph)].strip()
        if kind == "pack_count":
            notes.append(f"pack-count parens {content!r} are not strengths")
        elif kind == "modifier":
            found = [token for pat, token in _MODIFIER_PATTERNS
                     if pat.search(content)]
            if re.fullmatch(r"CR", content, re.IGNORECASE):
                found.append("controlled_release")
            if idx is not None:
                seg_mods[idx].extend(found)
            else:
                global_mods.extend(found)
        elif kind == "strength":
            paren_strengths.append((idx, content))
        elif kind == "ratio":
            if idx is not None:
                seg_ratio[idx] = True
            notes.append(f"ratio component kept raw: {content!r}")
        elif kind == "complex":
            return "complex_composition"
        elif kind == "flavour":
            notes.append(f"dropped flavour parens {content!r}")
        else:
            notes.append(f"dropped parens {content!r}")
        return None

    paren_strengths: list[tuple[int | None, str]] = []
    global_mods: list[str] = []
    for idx in range(len(seg_texts)):
        for ph in seg_phs[idx]:
            failed = _handle_paren(ph, idx)
            if failed:
                return JapComposition((), (), form_raw, (),
                                      "; ".join(notes), False, failed)
    for ph in global_ph:
        failed = _handle_paren(ph, None)
        if failed:
            return JapComposition((), (), form_raw, (), "; ".join(notes),
                                  False, failed)
    # Trailing placeholders outside any segment are also global (e.g.
    # Lignocaine/Adrenaline "(2%w/v and 1:80000)").
    for ph in _PLACEHOLDER_RE.findall(main):
        if any(ph in phs for phs in seg_phs) or ph in global_ph:
            continue
        failed = _handle_paren(ph, None)
        if failed:
            return JapComposition((), (), form_raw, (), "; ".join(notes),
                                  False, failed)

    # Inline strengths bind first; paren groups only fill gaps. A strength
    # group whose content splits into >=2 strength-bearing chunks splices
    # the composition in place of a strength-less combo-name segment
    # ("Co-trimoxazole (Sulphamethoxazole 100mg and Trimethoprim 20mg)").
    by_seg: dict[int, list[str]] = {}
    for i, content in paren_strengths:
        if i is not None:
            by_seg.setdefault(i, []).append(content)
    idx = 0
    while idx < len(seg_texts):
        bare = _PLACEHOLDER_RE.sub(" ", seg_texts[idx])
        found = _STRENGTH_FIND_RE.search(bare)
        mine = by_seg.pop(idx, [])
        for extra in mine[1:]:
            notes.append(f"extra strength parens {extra!r}")
        if found is not None:
            seg_strength[idx] = found.group(0)
            seg_texts[idx] = bare[: found.start()] + " " + bare[found.end():]
            for extra in mine:
                notes.append(f"extra strength parens {extra!r}")
        elif mine:
            content = mine[0]
            form2, trimmed = _detect_form(content)
            chunks = [c.strip() for c in _PAREN_CHUNK_RE.split(trimmed)
                      if c.strip()]
            chunks = [re.sub(r"\s+", " ", c) for c in chunks]
            outer_bare = _PLACEHOLDER_RE.sub(" ", seg_texts[idx]).strip()
            single_brand = (len(chunks) == 1
                            and outer_bare.casefold().startswith("janaushadhi")
                            and _STRENGTH_TOKEN_RE.search(chunks[0]))
            if (len(chunks) >= 2
                    and all(_STRENGTH_TOKEN_RE.search(c) for c in chunks)) \
                    or single_brand:
                notes.append(f"spliced paren composition {content!r}")
                if form2 is not None and form_raw is None:
                    form_raw = form2
                    notes.append(f"form from paren composition: {form2!r}")
                seg_texts[idx:idx + 1] = chunks
                seg_phs[idx:idx + 1] = [[] for _ in chunks]
                seg_mods[idx:idx + 1] = [list(seg_mods[idx])
                                         for _ in chunks]
                seg_strength[idx:idx + 1] = [None for _ in chunks]
                seg_ratio[idx:idx + 1] = [False for _ in chunks]
                # Shift keys after the splice point.
                by_seg = {(k if k < idx else k + len(chunks) - 1): v
                          for k, v in by_seg.items()}
                if len(seg_texts) > 3:
                    notes.append(f"{len(seg_texts)} molecule segments")
                    return JapComposition((), (), form_raw, (),
                                          "; ".join(notes), False,
                                          "complex_composition")
                continue  # reprocess the spliced-in first chunk
            notes.append(f"single strength parens {content!r}")
            seg_strength[idx] = content
        idx += 1
    # Global strength groups assign positionally to segments still lacking
    # strengths ("Lignocaine and Adrenaline ... (2%w/v and 1:80000)").
    pending = [c for i, c in paren_strengths if i is None]
    if pending:
        needy = [i for i, s in enumerate(seg_strength) if s is None]
        chunks = _PAREN_CHUNK_RE.split(pending[0])
        chunks = [c.strip() for c in chunks if c.strip()]
        if len(pending) == 1 and len(chunks) == len(needy) and needy:
            for i, chunk in zip(needy, chunks, strict=True):
                seg_strength[i] = chunk
        else:
            notes.append(f"unassigned strength parens {pending!r}")

    molecules: list[str] = []
    strengths: list[str | None] = []
    modifiers: set[str] = set()
    low_confidence = False
    for i, seg in enumerate(seg_texts):
        seg = _PLACEHOLDER_RE.sub(" ", seg)
        seg, n_gauge = _GAUGE_INLINE_RE.subn(" ", seg)
        if n_gauge:
            notes.append("stripped needle-gauge size (device, not strength)")
        for pat, token in _MODIFIER_PATTERNS:
            if pat.search(seg):
                seg_mods[i].append(token)
                seg = pat.sub(" ", seg)
        seg = _DESCRIPTOR_RE.sub(" ", seg)
        seg = _ROUTE_RE.sub(" ", seg)
        seg = _WS_RE.sub(" ", seg).strip(" -,")
        if seg_strength[i] is None:
            found = _STRENGTH_FIND_RE.search(seg)
            if found:
                seg_strength[i] = found.group(0)
                seg = seg[: found.start()] + " " + seg[found.end():]
        if form_raw is None:
            # "... for Injection" is form information, not pack residue —
            # rescue it before the leftover strip below eats it.
            mfi = re.search(r"\s+for\s+injection\s*$", seg, re.IGNORECASE)
            if mfi:
                form_raw = "Injection"
                seg = seg[: mfi.start()]
                notes.append("form from 'for injection' residue")
        seg = _FORM_LEFTOVER_RE.sub(" ", seg)
        seg = _PER_RESIDUE_RE.sub("", seg)
        seg = _WITH_WFI_RE.sub("", seg)
        seg = _LEADING_FORM_OF_RE.sub("", seg)
        seg = _WS_RE.sub(" ", seg).strip(" -,")
        # Late form rescue: a form word stranded inside a segment (its tail
        # was not strength-only at the whole-name level) is still the form.
        if form_raw is None:
            form2, rest = _detect_form(seg)
            if form2 is not None:
                form_raw = form2
                seg = rest
                notes.append(f"late form rescue: {form2!r}")
        raw_strength = seg_strength[i]
        canon_strength: str | None = None
        if raw_strength is not None:
            canon, reason = parse_strength(raw_strength)
            if canon is not None:
                canon_strength = canon
            elif reason == "ratio_strength":
                low_confidence = True
                notes.append(f"ratio strength kept raw: {raw_strength!r}")
            elif reason in ("absent_strength", "missing_unit",
                            "volume_not_strength"):
                notes.append(f"no strength for segment: {raw_strength!r}")
            elif reason == "complex_unit":
                return JapComposition((), (), form_raw, (),
                                      "; ".join(notes), False,
                                      "complex_composition")
            else:
                return JapComposition((), (), form_raw, (),
                                      "; ".join(notes), False,
                                      "unparseable_strength")
        if seg_ratio[i]:
            low_confidence = True
        seg = _WS_RE.sub(" ", seg).strip(" -,")
        if (not seg or not re.search(r"[A-Za-z]", seg)
                or seg.casefold() in _BARE_WORD_VETO or len(seg) < 3):
            return JapComposition((), (), form_raw, (),
                                  "; ".join(notes), False,
                                  "unparseable_molecule")
        molecules.append(seg)
        strengths.append(canon_strength)
        modifiers.update(seg_mods[i])
        modifiers.update(global_mods)
    return JapComposition(
        molecules=tuple(molecules),
        strengths=tuple(strengths),
        form_raw=form_raw,
        modifiers=tuple(sorted(modifiers)),
        notes="; ".join(notes),
        ok=True,
        reason=None,
        low_confidence=low_confidence,
    )

