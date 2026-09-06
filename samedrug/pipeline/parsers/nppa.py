"""NPPA ceiling-price CSV parser with pack-variant qualifier decomposition.

Input: the manually exported IPDMS ceiling-price CSVs (see docs/sources.md).
Both files share a layout: 4 preamble rows, header on line 5 (located by
content, never by hardcoded offset), one leading empty column, UTF-8 with
BOM, double-quoted fields with embedded newlines preserved raw.

Ceiling Price cell grammar: "<rupee> <value>(<qualifier>)[(<qualifier>)]".
The qualifier inventory was measured exhaustively from both seed files
(Step-0: 60 distinct qualifier strings in nppa_ceiling_all.csv, 11 in
nppa_ceiling_special.csv) and every shape below classifies 100% of it.
Anything new falls into the reject queue as "unparseable_price_qualifier".

Multi-row (formulation, strength) keys are PACK VARIANTS (same NLEM + same
SO number, different qualifier) and are all kept. Only byte-identical
(formulation, strength, qualifier, price) repeats are duplicates.
"""

import csv
import re
from dataclasses import dataclass
from pathlib import Path

RUPEE = "\u20b9"

_PRICE_RE = re.compile(r"^\s*" + RUPEE + r"\s*([0-9]+(?:\.[0-9]+)?)\s*(.*)$", re.DOTALL)
_GROUP_RE = re.compile(r"\((?:[^()]|\([^()]*\))*\)")
_STRENGTH_BOUNDARY_RE = re.compile(r"(?<!\S)(?:\d[^\s]*|--)(?=\s|$)")

# Normalized qualifier group -> (unit_type, default unit_count or None).
_COUNTABLE_UNITS = frozenset(
    {
        "tablet",
        "capsule",
        "ml",
        "gm",
        "condom",
        "suppository",
        "dose",
        "iud",
        "gum",
        "lozenge",
        "pastille",
        "pessary",
        "unit",
        "vial",
    }
)

_PLURAL_SINGULAR = {
    "tablets": "tablet",
    "capsules": "capsule",
    "vials": "vial",
    "packs": "pack",
    "doses": "dose",
    "condoms": "condom",
    "suppositories": "suppository",
    "suppository": "suppository",
    "units": "unit",
    "iuds": "iud",
    "gums": "gum",
    "lozenges": "lozenge",
    "pastilles": "pastille",
    "pessaries": "pessary",
    "pessary": "pessary",
}

_COUNTED_UNIT_RE = re.compile(
    r"^(\d+(?:\.\d+)?)\s*(tablets?|capsules?|ml|gm|g|condoms?|suppositor(?:y|ies)|"
    r"doses?|iuds?|gums?|lozenges?|pastilles?|pessar(?:y|ies)|units?|vials?)\s*$"
)
_EACH_UNIT_RE = re.compile(
    r"^each\s+(vial|pack|dose|condom|suppository|unit|iud|gum|lozenges?|"
    r"pastilles?|pessary|tablet|capsule)\s*$"
)
_EACH_PACK_VOL_RE = re.compile(r"^each\s+pack\s*\(\s*(\d+(?:\.\d+)?)\s*ml\s*\)$")
_EACH_VOL_PACK_RE = re.compile(r"^each\s+(\d+(?:\.\d+)?)\s*ml\s+pack\b")
_VOL_PACK_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*ml\s+pack$")
_VOL_CONTAINER_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*ml\s+(glass|non-?\s*glass)\b")
_PER_ML_RE = re.compile(r"^per\s+(\d+(?:\.\d+)?)\s*ml$")
_BARE_ML_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*ml$")
_THRESHOLD_RE = re.compile(r"^\s*1\s*ml\s*\((.*)\)\s*$", re.DOTALL | re.IGNORECASE)


@dataclass(frozen=True)
class NppRecord:
    sl_no: int
    nlem_version: str | None
    formulation_raw: str
    form_raw: str | None
    strength_raw: str | None
    so_number: str
    so_date: str
    price_value: float
    unit_type: str | None
    unit_count: float | None
    pack_volume_ml: float | None
    container: str | None
    pack_condition_raw: str | None
    qualifier_raw: str
    source_file: str
    source_row: str


@dataclass
class RejectEntry:
    identifier: int | None
    reason: str


def _normalize(text: str) -> str:
    """Lowercase match form: NBSP -> space, collapse whitespace runs."""
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip().lower()


def _singular(unit: str) -> str:
    unit = unit.lower()
    if unit == "g":
        return "gm"
    return _PLURAL_SINGULAR.get(unit, unit)


def _classify_group(inner_raw: str) -> dict | None:
    """Classify one qualifier group; None if the grammar cannot cover it."""
    g = _normalize(inner_raw)
    if g in ("pack",):
        return {"marker": True}
    m = _THRESHOLD_RE.match(inner_raw)
    if m:
        return {"unit_type": "ml", "unit_count": 1.0, "condition": m.group(1).strip()}
    if g == "each vial":
        return {"unit_type": "vial", "unit_count": 1.0}
    m = _EACH_PACK_VOL_RE.match(g)
    if m:
        return {"unit_type": "pack", "unit_count": 1.0, "volume": float(m.group(1))}
    m = _EACH_VOL_PACK_RE.match(g)
    if m:
        return {"unit_type": "pack", "unit_count": 1.0, "volume": float(m.group(1))}
    if g == "each pack":
        return {"unit_type": "pack", "unit_count": 1.0}
    if g == "each dose":
        return {"unit_type": "dose", "unit_count": 1.0}
    if g == "per dose":
        return {"unit_type": "dose"}
    if g == "per metered dose":
        return {"unit_type": "metered_dose"}
    if g.startswith("per dual chamber bag"):
        return {"unit_type": "dual_chamber_bag"}
    if g == "per mg of phospholipids in the pack":
        return {"unit_type": "mg_phospholipid"}
    if g in ("cubic meter", "cubic meters"):
        return {"unit_type": "cubic_meter"}
    if g in ("combi pack", "combi packs"):
        return {"unit_type": "combi_pack"}
    if g == "1 gm or 1 ml":
        return {"unit_type": "gm_or_ml", "unit_count": 1.0}
    m = _VOL_CONTAINER_RE.match(g)
    if m:
        container = "non_glass" if "non" in m.group(2) else "glass"
        return {"unit_type": "pack", "volume": float(m.group(1)), "container": container}
    m = _VOL_PACK_RE.match(g)
    if m:
        return {"unit_type": "pack", "volume": float(m.group(1))}
    m = _PER_ML_RE.match(g)
    if m:
        return {"unit_type": "ml", "unit_count": float(m.group(1))}
    m = _BARE_ML_RE.match(g)
    if m:
        return {"unit_type": "ml", "unit_count": float(m.group(1))}
    m = _COUNTED_UNIT_RE.match(g)
    if m:
        base = _singular(m.group(2))
        if base in _COUNTABLE_UNITS:
            return {"unit_type": base, "unit_count": float(m.group(1))}
        return None
    m = _EACH_UNIT_RE.match(g)
    if m:
        return {"unit_type": _singular(m.group(1)), "unit_count": 1.0}
    return None


def _parse_qualifier(qualifier_raw: str) -> dict | None:
    """Decompose qualifier text; None when the grammar cannot classify it."""
    groups = _GROUP_RE.findall(qualifier_raw)
    if not groups:
        return None
    rest = _GROUP_RE.sub("", qualifier_raw)
    if rest.strip():
        return None
    inners = [g[1:-1] for g in groups]
    classified = [_classify_group(g) for g in inners]
    if any(c is None for c in classified):
        return None
    assert all(c is not None for c in classified)
    # Two-group special case: "<V> ML Pack" + "<C> ML Pack" (Tramadol) means
    # a V-ml pack priced per C ml: unit basis from group 2, volume from group 1.
    if len(classified) == 2:
        m1 = _VOL_PACK_RE.match(_normalize(inners[0]))
        m2 = _VOL_PACK_RE.match(_normalize(inners[1]))
        if m1 and m2 and not classified[0].get("marker") and not classified[1].get("marker"):
            return {
                "unit_type": "ml",
                "unit_count": float(m2.group(1)),
                "pack_volume_ml": float(m1.group(1)),
                "container": None,
                "pack_condition_raw": None,
            }
    out: dict = {
        "unit_type": None,
        "unit_count": None,
        "pack_volume_ml": None,
        "container": None,
        "pack_condition_raw": None,
    }
    for c in classified:
        assert c is not None
        if c.get("marker"):
            continue
        if out["unit_type"] is None and c.get("unit_type"):
            out["unit_type"] = c["unit_type"]
            out["unit_count"] = c.get("unit_count")
        if out["pack_volume_ml"] is None and c.get("volume") is not None:
            out["pack_volume_ml"] = c["volume"]
        if out["container"] is None and c.get("container"):
            out["container"] = c["container"]
        if out["pack_condition_raw"] is None and c.get("condition"):
            out["pack_condition_raw"] = c["condition"]
    if out["unit_type"] is None:
        return None
    return out


def split_form_strength(dosage: str) -> tuple[str | None, str | None]:
    """Split "Dosage & Strength" at the first strength-looking token.

    The boundary is the first whitespace-delimited token that is digit-led
    or "--". No strength token -> the whole value is the form. A "--"
    token means explicitly absent strength (None). Interior whitespace
    (including embedded newlines) is preserved raw; only the ends and the
    single boundary are trimmed.
    """
    text = dosage.strip()
    if not text:
        return None, None
    m = _STRENGTH_BOUNDARY_RE.search(text)
    if m is None:
        return text, None
    form = text[: m.start()].strip() or None
    if m.group(0) == "--":
        return form, None
    return form, text[m.start():].strip() or None


def _locate_columns(header: list[str]) -> dict[str, int]:
    cells = [c.strip() for c in header]
    cols: dict[str, int] = {}
    for i, c in enumerate(cells):
        if c in ("SL No", "S.No."):
            cols.setdefault("sl_no", i)
        elif c == "NLEM Version":
            cols.setdefault("nlem_version", i)
        elif c == "Formulation":
            cols.setdefault("formulation", i)
        elif c == "Dosage & Strength":
            cols.setdefault("dosage", i)
        elif c == "SO Number":
            cols.setdefault("so_number", i)
        elif c == "SO Date":
            cols.setdefault("so_date", i)
        elif c.startswith("Ceiling Price"):
            cols.setdefault("price", i)
    required = ("sl_no", "formulation", "dosage", "so_number", "so_date", "price")
    missing = [k for k in required if k not in cols]
    if missing:
        raise ValueError(f"NPPA header missing columns: {missing} (header={cells!r})")
    return cols


def _cell(row: list[str], cols: dict[str, int], name: str) -> str:
    idx = cols[name]
    return row[idx].strip() if idx < len(row) else ""


def parse_nppa_csv(
    path: str | Path, source_file_label: str
) -> tuple[list[NppRecord], list[RejectEntry]]:
    """Parse one NPPA ceiling-price CSV into kept records and reject entries."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header_idx = next(
        (
            i
            for i, row in enumerate(rows)
            if any(c.strip() in ("SL No", "S.No.") for c in row)
        ),
        None,
    )
    if header_idx is None:
        raise ValueError(f"NPPA header row (SL No / S.No.) not found in {path}")
    cols = _locate_columns(rows[header_idx])
    has_nlem = "nlem_version" in cols

    kept: list[NppRecord] = []
    rejected: list[RejectEntry] = []
    seen: set[tuple[str, str | None, str, float]] = set()
    for lineno, row in enumerate(rows[header_idx + 1 :], start=header_idx + 2):
        if not row or not any(c.strip() for c in row):
            continue

        sl_raw = _cell(row, cols, "sl_no")
        source_row = f"{source_file_label} sl_no={sl_raw or '?'} csv_record={lineno}"
        try:
            sl_no: int | None = int(sl_raw)
        except ValueError:
            rejected.append(RejectEntry(identifier=None, reason="invalid_sl_no"))
            continue
        formulation = _cell(row, cols, "formulation")
        if not formulation:
            rejected.append(RejectEntry(identifier=sl_no, reason="missing_formulation"))
            continue
        so_number = _cell(row, cols, "so_number")
        if not so_number:
            rejected.append(
                RejectEntry(identifier=sl_no, reason="missing_field:so_number")
            )
            continue
        so_date = _cell(row, cols, "so_date")
        if not so_date:
            rejected.append(RejectEntry(identifier=sl_no, reason="missing_field:so_date"))
            continue
        price_cell = _cell(row, cols, "price")
        m = _PRICE_RE.match(price_cell) if price_cell else None
        if not m:
            rejected.append(RejectEntry(identifier=sl_no, reason="unparseable_price"))
            continue
        price_value = float(m.group(1))
        qualifier_raw = m.group(2).strip()
        parsed = _parse_qualifier(qualifier_raw) if qualifier_raw else None
        if parsed is None:
            rejected.append(
                RejectEntry(identifier=sl_no, reason="unparseable_price_qualifier")
            )
            continue
        form_raw, strength_raw = split_form_strength(_cell(row, cols, "dosage"))
        dup_key = (formulation, strength_raw, qualifier_raw, price_value)
        if dup_key in seen:
            rejected.append(RejectEntry(identifier=sl_no, reason="duplicate_row"))
            continue
        seen.add(dup_key)
        kept.append(
            NppRecord(
                sl_no=sl_no,
                nlem_version=_cell(row, cols, "nlem_version") if has_nlem else None,
                formulation_raw=formulation,
                form_raw=form_raw,
                strength_raw=strength_raw,
                so_number=so_number,
                so_date=so_date,
                price_value=price_value,
                unit_type=parsed["unit_type"],
                unit_count=parsed["unit_count"],
                pack_volume_ml=parsed["pack_volume_ml"],
                container=parsed["container"],
                pack_condition_raw=parsed["pack_condition_raw"],
                qualifier_raw=qualifier_raw,
                source_file=source_file_label,
                source_row=source_row,
            )
        )
    return kept, rejected
