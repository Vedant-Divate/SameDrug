"""Golden NPPA parser tests from real seed excerpts (plus labeled synthetic rows).

Fixtures are verbatim copies (preamble + header + data rows) from
data/raw/nppa_ceiling_all.csv (25 rows) and
data/raw/nppa_ceiling_special.csv (5 rows). The real seeds contain no
missing-formulation or unparseable-price rows (measured 2026-09-06), so
those paths are covered by labeled synthetic rows below. The real seeds
also contain zero NBSP cells, so NBSP passthrough is covered synthetically.
"""

from pathlib import Path

import pytest

from samedrug.pipeline.parsers.nppa import (
    _parse_qualifier,
    parse_nppa_csv,
    split_form_strength,
)

ALL_FIXTURE = Path(__file__).parent.parent / "fixtures" / "nppa_all_sample.csv"
SPECIAL_FIXTURE = Path(__file__).parent.parent / "fixtures" / "nppa_special_sample.csv"


def test_header_detection_past_preamble_and_leading_column_offset():
    kept, rejected = parse_nppa_csv(ALL_FIXTURE, "nppa_all")
    # 25 real rows in, 24 kept + exactly the 1 genuine duplicate rejected.
    assert len(kept) == 24
    assert len(rejected) == 1
    first = kept[0]
    assert first.sl_no == 1
    assert first.nlem_version == "2011"
    assert first.formulation_raw == "Acetylsalicylic acid"
    assert first.source_file == "nppa_all"
    assert first.source_row == "nppa_all sl_no=1 csv_record=6"


def test_duplicate_row_rejected_exactly_once():
    kept, rejected = parse_nppa_csv(ALL_FIXTURE, "nppa_all")
    assert [(r.identifier, r.reason) for r in rejected] == [(532, "duplicate_row")]
    sls = [r.sl_no for r in kept]
    assert sls.count(531) == 1 and 532 not in sls


def test_form_strength_split_normal_and_degenerate():
    kept, _ = parse_nppa_csv(ALL_FIXTURE, "nppa_all")
    by_sl = {r.sl_no: r for r in kept}
    assert (by_sl[1].form_raw, by_sl[1].strength_raw) == ("Tablet", "300 mg")
    # Condom: no strength-looking token -> whole value is the form.
    assert by_sl[3].form_raw == "CONDOM"
    assert by_sl[3].strength_raw is None
    # Water for Injection: "--" means explicitly absent strength.
    assert by_sl[142].form_raw == "Injection"
    assert by_sl[142].strength_raw is None


def test_glucose_variant_group_kept_as_pack_variants():
    kept, _ = parse_nppa_csv(ALL_FIXTURE, "nppa_all")
    group = [r for r in kept if r.formulation_raw == "Glucose" and r.strength_raw == "5%"]
    assert len(group) == 9
    assert len({r.qualifier_raw for r in group}) == 9
    assert {r.so_number for r in group} == {"1581(E)"}


def test_qualifier_decomposition_spot_asserts():
    kept, _ = parse_nppa_csv(ALL_FIXTURE, "nppa_all")
    by_sl = {r.sl_no: r for r in kept}
    glass = by_sl[60]
    assert glass.price_value == 22.99
    assert glass.pack_volume_ml == 100.0
    assert glass.container == "glass"
    non_glass = by_sl[61]
    assert non_glass.price_value == 20.80
    assert non_glass.pack_volume_ml == 100.0
    assert non_glass.container == "non_glass"


def test_per_metered_dose_decomposition():
    kept, _ = parse_nppa_csv(ALL_FIXTURE, "nppa_all")
    # Fixture holds only one Budesonide pair; the metered-dose shape is
    # asserted against the real seed row measured in Step-0 (FORMOTERAL).
    parsed = _parse_qualifier("(Per Metered Dose)( Pack)")
    assert parsed is not None
    assert parsed["unit_type"] == "metered_dose"
    budesonide = [r for r in kept if r.formulation_raw == "Budesonide"]
    assert len(budesonide) == 2
    by_form = {r.form_raw: r for r in budesonide}
    assert (by_form["Nasal Spray"].unit_type, by_form["Nasal Spray"].unit_count) == (
        "dose",
        1.0,
    )
    assert by_form["Inhalation"].unit_type == "dose"
    assert by_form["Inhalation"].unit_count is None


def test_dicyclomine_threshold_conditions_verbatim():
    kept, _ = parse_nppa_csv(ALL_FIXTURE, "nppa_all")
    by_sl = {r.sl_no: r for r in kept}
    assert by_sl[383].pack_condition_raw == "10 ML & more pack"
    assert by_sl[384].pack_condition_raw == "Less than 10 ML Pack"
    assert (by_sl[383].unit_type, by_sl[383].unit_count) == ("ml", 1.0)


def test_multiline_fields_preserved_raw():
    kept, _ = parse_nppa_csv(ALL_FIXTURE, "nppa_all")
    by_sl = {r.sl_no: r for r in kept}
    assert by_sl[65].formulation_raw == "Glucose (A) +\nSodium Chloride (B)"
    assert by_sl[12].strength_raw == "100 mg"
    assert by_sl[12].form_raw.startswith("Effervescent/ Dispersible/\n")
    assert "\n" in by_sl[44].strength_raw


def test_special_fixture_schema_and_shapes():
    kept, rejected = parse_nppa_csv(SPECIAL_FIXTURE, "nppa_special")
    assert len(kept) == 5 and rejected == []
    assert all(r.nlem_version is None for r in kept)
    by_sl = {r.sl_no: r for r in kept}
    assert by_sl[1].unit_type == "pack" and by_sl[1].pack_volume_ml == 1000.0
    assert by_sl[5].container == "non_glass" and by_sl[5].pack_volume_ml == 1000.0
    assert (by_sl[13].unit_type, by_sl[13].unit_count) == ("ml", 1.0)
    assert by_sl[19].unit_type == "dual_chamber_bag"


def _write_csv(path: Path, rows: list[list[str]]) -> Path:
    import csv

    with open(path, "w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)
        writer.writerows(rows)
    return path


def _synthetic_frame(extra_header: list[str], data_rows: list[list[str]]) -> list[list[str]]:
    header = ["", " SL No", " NLEM Version", " Formulation", " Dosage & Strength",
              " SO Number", " SO Date",
              " Ceiling Price ( Excluding Taxes) (Rs.)(Per Unit)"]
    return [
        ["", ""],
        ["", "All Drugs Ceiling Prices"],
        ["", "", "DATE", "5/9/2026"],
        ["", "", "TIME", "17:16:6"],
        header + extra_header,
        *data_rows,
    ]


def test_synthetic_unparseable_qualifier_rejected(tmp_path):
    synthetic = _synthetic_frame([], [  # synthetic: unknown qualifier shape
        ["", "1", "2022", "Synthetic Drug", "Tablet 10 mg", "9999(E)", "25-Mar-2026",
         "₹ 5.00(1 Blister Pack)"],
    ])
    kept, rejected = parse_nppa_csv(_write_csv(tmp_path / "s.csv", synthetic), "nppa_all")
    assert kept == []
    assert [(r.identifier, r.reason) for r in rejected] == [
        (1, "unparseable_price_qualifier")
    ]


def test_synthetic_missing_formulation_and_price_rejected(tmp_path):
    synthetic = _synthetic_frame([], [  # synthetic: blank formulation / price
        ["", "1", "2022", "   ", "Tablet 10 mg", "9999(E)", "25-Mar-2026", "₹ 5.00(1 Tablet)"],
        ["", "2", "2022", "Synthetic Drug", "Tablet 10 mg", "9999(E)", "25-Mar-2026", ""],
    ])
    kept, rejected = parse_nppa_csv(_write_csv(tmp_path / "s.csv", synthetic), "nppa_all")
    assert kept == []
    assert [(r.identifier, r.reason) for r in rejected] == [
        (1, "missing_formulation"),
        (2, "unparseable_price"),
    ]


def test_synthetic_nbsp_and_extra_columns_preserved(tmp_path):
    synthetic = _synthetic_frame([" Mystery Column"], [  # synthetic: NBSP + unknown col
        ["", "1", "2022", "Synthetic\u00a0Drug", "Tablet 10 mg", "9999(E)", "25-Mar-2026",
         "₹ 5.00(1 Tablet)", "ignored"],
    ])
    kept, rejected = parse_nppa_csv(_write_csv(tmp_path / "s.csv", synthetic), "nppa_all")
    assert rejected == []
    assert kept[0].formulation_raw == "Synthetic\u00a0Drug"
    assert (kept[0].unit_type, kept[0].unit_count) == ("tablet", 1.0)


def test_missing_header_raises(tmp_path):
    import csv

    path = tmp_path / "noheader.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows([["", ""], ["", "1", "x"]])
    with pytest.raises(ValueError, match="header row"):
        parse_nppa_csv(path, "nppa_all")


def test_split_form_strength_edge_cases():
    assert split_form_strength("Tablet 300 mg") == ("Tablet", "300 mg")
    assert split_form_strength("CONDOM") == ("CONDOM", None)
    assert split_form_strength("Injection --") == ("Injection", None)
    assert split_form_strength("Powder For Injection 1000000") == (
        "Powder For Injection",
        "1000000",
    )
    assert split_form_strength("Topical forms 2-5%") == ("Topical forms", "2-5%")
    assert split_form_strength("   ") == (None, None)
