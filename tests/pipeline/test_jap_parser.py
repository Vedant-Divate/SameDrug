"""Golden parser tests from a real seed excerpt (plus labeled synthetic rows)."""

import json
from pathlib import Path

from samedrug.pipeline.parsers.jap import parse_jap_products

FIXTURE = Path(__file__).parent.parent / "fixtures" / "jap_products_sample.json"

# NOTE: the seed file contains zero status=0 rows and zero null/absent-mrp
# rows (measured 2026-09-06), so this faithful 12-record excerpt has none
# either. Those paths are covered by labeled synthetic rows below.


def test_fixture_record_count_and_field_mappings():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    kept, rejected = parse_jap_products(data)
    assert len(kept) == 12
    assert rejected == []
    first = kept[0]
    assert first.product_id == 1
    assert first.drug_code == 1
    assert first.generic_name == "Aceclofenac 100mg and Paracetamol 325mg Tablets"
    assert first.group_name == "Analgesic/Antipyretic/Anti-Inflammatory"
    assert first.unit_size == "10's"
    assert first.mrp == 10.32
    assert first.status == 1
    assert first.source_row == "jap productId=1 drugCode=1"


def test_zero_mrp_is_valid_numeric_and_kept():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    kept, _ = parse_jap_products(data)
    zero = [r for r in kept if r.mrp == 0.0]
    assert len(zero) == 2
    assert {r.product_id for r in zero} == {1493, 2385}


def test_synthetic_inactive_row_rejected():
    synthetic = {  # synthetic: status=0 does not occur in the seed
        "productId": 999001,
        "genericName": "Synthetic Inactive",
        "groupName": "Test",
        "drugCode": 999001,
        "unitSize": "10's",
        "mrp": 5.0,
        "status": 0,
        "serialNo": 1,
    }
    kept, rejected = parse_jap_products([synthetic])
    assert kept == []
    assert [(r.product_id, r.reason) for r in rejected] == [
        (999001, "inactive_product")
    ]


def test_synthetic_missing_field_rows_rejected():
    synthetic = [  # synthetic: seed has no missing-field rows
        {"genericName": "No Id", "drugCode": 1, "mrp": 1.0, "status": 1},
        {"productId": 999002, "drugCode": 2, "mrp": 1.0, "status": 1},
        {"productId": 999003, "drugCode": 3, "genericName": "  ", "mrp": 1.0,
         "status": 1},
    ]
    kept, rejected = parse_jap_products(synthetic)
    assert kept == []
    assert [r.reason for r in rejected] == [
        "missing_field:product_id",
        "missing_field:generic_name",
        "missing_field:generic_name",
    ]


def test_synthetic_invalid_mrp_rows_rejected():
    synthetic = [  # synthetic: seed has no null/non-numeric mrp rows
        {"productId": 999004, "drugCode": 4, "genericName": "Null Mrp",
         "mrp": None, "status": 1},
        {"productId": 999005, "drugCode": 5, "genericName": "String Mrp",
         "mrp": "12.5", "status": 1},
    ]
    kept, rejected = parse_jap_products(synthetic)
    assert kept == []
    assert [r.reason for r in rejected] == ["invalid_mrp", "invalid_mrp"]


def test_synthetic_unknown_status_rejected_and_extra_fields_ignored():
    synthetic = [  # synthetic: seed has only status=1, no extra keys
        {"productId": 999006, "drugCode": 6, "genericName": "Bad Status",
         "mrp": 1.0, "status": 7},
        {"productId": 999007, "drugCode": 7, "genericName": "Extra Fields",
         "mrp": 2, "status": 1, "futureField": "ignored", "serialNo": 9},
    ]
    kept, rejected = parse_jap_products(synthetic)
    assert [(r.product_id, r.reason) for r in rejected] == [
        (999006, "unknown_status")
    ]
    assert len(kept) == 1
    assert kept[0].product_id == 999007
    assert kept[0].mrp == 2.0
