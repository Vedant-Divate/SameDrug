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
_WS_RE = re.compile(r"\s+")


def clean_text(s: str) -> str:
    """NFKC-normalize, map NBSP to space, strip quality markers, collapse ws."""
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("\u00a0", " ")
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
_UNIT = r"(?:mcg|μg|ug|mg|g|gm|iu|ku)\b"
_STRENGTH_TOKEN_RE = re.compile(rf"({_NUM})\s*(%|{_UNIT})", re.IGNORECASE)
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
_PERCENT_RE = re.compile(rf"({_NUM})\s*%\s*(w\s*/\s*v|w\s*/\s*w|wv|ww)?", re.IGNORECASE)
_PLAIN_RE = re.compile(rf"({_NUM})\s*(mcg|μg|ug|mg|g|gm|iu|ku)\b", re.IGNORECASE)
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
    "mg/ml": 5,
    "iu/ml": 6,
    "ku/ml": 7,
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
        suffix = "%ww" if kind in ("w/w", "ww") else "%wv"  # bare % is w/v implied
        return f"{_fmt_num(_num(m.group(1)))}{suffix}", None
    m = _PLAIN_RE.fullmatch(p)
    if m:
        return _plain_canon(_num(m.group(1)), m.group(2)), None
    if _BARE_ML_RE.match(p):
        return None, "volume_not_strength"
    if _BARE_NUM_RE.match(p):
        return None, "missing_unit"
    # Trailing qualifier words after a leading strength token
    # ("100 mg elemental iron", "52 MGOF LEVONORGESTREL" style): accept the
    # leading token. (fullmatch guards: a bare prefix match is not enough.)
    m = _STRENGTH_TOKEN_RE.match(p)
    if m and _PERCENT_RE.fullmatch(p) is None and _PLAIN_RE.fullmatch(p) is None:
        rest = p[m.end():].strip()
        if rest and re.fullmatch(r"[A-Za-z][A-Za-z\s\-]*", rest):
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
    if re.search(r"\([^()]*\+[^()]*\)", t):
        # Combipack strengths group inner "+"-parts in parens
        # ("1 Tablet (750 mg+ 37.5 mg) (B)"); splice them into the top level.
        t = t.replace("(", " ").replace(")", " ")
    t = _LABEL_RE.sub(" ", t)
    t = _MGOF_RE.sub(r"\1 ", t)  # "52 MGOF LEVONORGESTREL" -> "52 MG ..."
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
    "intravenous infusion": "infusion",
    # Liquids.
    "syrup": "syrup",
    "dry syrup": "dry_syrup",
    "dry_syrup": "dry_syrup",
    "suspension": "suspension",
    "susp": "suspension",
    "oral suspension": "suspension",
    "oral liquid": "solution",
    "oral solution": "solution",
    "solution": "solution",
    "liquid": "solution",
    # Drops stay route-specific: eye/ear/nasal must never match each other.
    "drops": "drops",
    "eye drops": "eye_drops",
    "eye_drops": "eye_drops",
    "ear drops": "ear_drops",
    "ear_drops": "ear_drops",
    "nasal drops": "nasal_drops",
    "nasal_drops": "nasal_drops",
    "oral drops": "oral_drops",
    # Inhalation family.
    "inhaler": "inhaler",
    "inhalation": "inhalation",
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
    "spray": "nasal_spray",
    # Topical family.
    "cream": "cream",
    "gel": "gel",
    "ointment": "ointment",
    "lotion": "lotion",
    "paste": "paste",
    "paint": "paint",
    "shampoo": "shampoo",
    "soap": "soap",
    "liniment": "liniment",
    "powder for external use": "topical_powder",
    # Miscellaneous exact-only bases.
    "suppository": "suppository",
    "pessary": "pessary",
    "granules": "granules",
    "sachet": "sachet",
    "gargle": "gargle",
    "mouth paint": "mouth_paint",
    "mouth wash": "mouth_wash",
    "mouthwash": "mouth_wash",
    "enema": "enema",
    "condom": "condom",
    "iud": "iud",
    "gum": "gum",
    "lozenge": "lozenge",
    "lozenges": "lozenge",
    "pastille": "pastille",
    "pastilles": "pastille",
    "vaccine": "vaccine",
    "kit": "kit",
    "device": "device",
    "patch": "patch",
    "oil": "oil",
    "powder": "powder",
    "vial": "injection",
    "ampoule": "injection",
    "pen": "pen",
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
