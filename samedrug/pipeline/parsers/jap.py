"""Jan Aushadhi product parser with a reject queue.

Input: the full JAP products response JSON (as saved by the fetcher), or —
for convenience — a bare list of product dicts. Extra/unknown fields are
ignored. Only active (status=1) rows become JapRecords; everything else is
recorded in the reject queue for audit.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class JapRecord:
    product_id: int
    drug_code: int
    generic_name: str
    group_name: str | None
    unit_size: str | None
    mrp: float
    status: int
    source_row: str


@dataclass
class RejectEntry:
    product_id: int | None
    reason: str


def _extract_rows(data: dict | list) -> list:
    if isinstance(data, list):
        return data
    body = data.get("responseBody", {}) if isinstance(data, dict) else {}
    rows = body.get("newProductResponsesList", {}) if isinstance(body, dict) else {}
    return rows if isinstance(rows, list) else []


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def parse_jap_products(data: dict | list) -> tuple[list[JapRecord], list[RejectEntry]]:
    """Split raw JAP product dicts into kept records and reject entries."""
    kept: list[JapRecord] = []
    rejected: list[RejectEntry] = []
    for row in _extract_rows(data):
        if not isinstance(row, dict):
            rejected.append(RejectEntry(product_id=None, reason="missing_field:row"))
            continue
        raw_id = row.get("productId")
        raw_code = row.get("drugCode")
        raw_name = row.get("genericName")
        raw_mrp = row.get("mrp")
        raw_status = row.get("status")
        source_row = f"jap productId={raw_id} drugCode={raw_code}"

        if not _is_int(raw_id):
            rejected.append(RejectEntry(product_id=None, reason="missing_field:product_id"))
            continue
        if not _is_int(raw_code):
            rejected.append(RejectEntry(product_id=raw_id, reason="missing_field:drug_code"))
            continue
        if not isinstance(raw_name, str) or not raw_name.strip():
            rejected.append(
                RejectEntry(product_id=raw_id, reason="missing_field:generic_name")
            )
            continue
        if raw_mrp is None or not _is_number(raw_mrp):
            rejected.append(RejectEntry(product_id=raw_id, reason="invalid_mrp"))
            continue
        if raw_status not in (0, 1) or isinstance(raw_status, bool):
            rejected.append(RejectEntry(product_id=raw_id, reason="unknown_status"))
            continue
        if raw_status == 0:
            rejected.append(RejectEntry(product_id=raw_id, reason="inactive_product"))
            continue
        kept.append(
            JapRecord(
                product_id=raw_id,
                drug_code=raw_code,
                generic_name=raw_name,
                group_name=row.get("groupName"),
                unit_size=row.get("unitSize"),
                mrp=float(raw_mrp),
                status=raw_status,
                source_row=source_row,
            )
        )
    return kept, rejected
