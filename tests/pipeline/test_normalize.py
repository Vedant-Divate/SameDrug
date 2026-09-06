"""Golden suites for the Phase 3 normalization engine.

Every strength/form/pack/composition case below is a REAL row measured
from the seed database (source id cited in comments); synthetic edge rows
are labeled.
"""

import pytest

from samedrug.pipeline.normalize import (
    canonical_form,
    canonical_molecules,
    clean_text,
    extract_jap_composition,
    extract_modifiers,
    fold,
    form_family,
    make_key,
    parse_pack,
    parse_strength,
    parse_strength_parts,
    split_nppa_molecules,
)

# ---------------------------------------------------------------------------
# Golden strength suite: (raw, expected_canonical, expected_reason)
# ---------------------------------------------------------------------------

STRENGTH_GOLDENS: list[tuple[str | None, str | None, str | None]] = [
    # --- singles (NPPA ids cited) ---
    ("300 mg", "300mg", None),  # NPPA id 1 (Acetylsalicylic acid)
    ("0.5 MG", "0.5mg", None),  # NPPA distinct strength inventory
    ("4 MG/ML", "4mg/ml", None),  # brief section C example
    ("5%", "5%wv", None),  # bare % is w/v implied
    ("100 MCG", "0.1mg", None),  # mcg -> mg conversion
    ("40 IU/ ML", "40iu/ml", None),  # brief section C example
    ("INHALER 100 MCG", "0.1mg", None),  # form word leaks into strength_raw
    ("1 g", "1000mg", None),  # JAP pid 1928 Cefoperazone, spaced unit
    ("0.90%", "0.9%wv", None),  # NPPA Sodium chloride family
    ("2.50%", "2.5%wv", None),  # measured NPPA percent normalization
    ("0.05 %", "0.05%wv", None),  # measured NPPA percent spacing
    ("22.75 MG", "22.75mg", None),  # measured NPPA decimal mg
    ("500 MCG", "0.5mg", None),  # measured NPPA mcg
    ("37.5 mcg", "0.0375mg", None),  # NPPA Levothyroxine 37.5 mcg (id 85)
    ("88 MCG", "0.088mg", None),  # NPPA Levothyroxine 88 MCG (id 582)
    ("150 mg", "150mg", None),  # NPPA id 99 Paracetamol Injection
    ("2.5 MG", "2.5mg", None),  # measured NPPA strength
    ("300 mcg", "0.3mg", None),  # measured NPPA strength
    ("52 MGOF\nLEVONORGESTREL", "52mg", None),  # NPPA id 502 spaceless defect
    # --- concentrations ---
    ("125 mg per 5 ml", "25mg/ml", None),  # JAP pid 886 Paracetamol susp.
    ("30mg/5ml", "6mg/ml", None),  # JAP Fexofenadine suspension style
    ("50 mg/ml", "50mg/ml", None),  # implicit per-1-ml volume
    ("1 mg/ml", "1mg/ml", None),  # NPPA id 79 HYDROXOCOBALAMIN
    ("10 mg per ml", "10mg/ml", None),  # JAP pid 1498 Frusemide injection
    ("1mg per 2ml", "0.5mg/ml", None),  # JAP pid 1033 Budesonide nebuliser
    ("60 MG/ 0.6 ML", "100mg/ml", None),  # NPPA id 417 Enoxaparin
    ("20 mg/ 2 ml", "10mg/ml", None),  # measured NPPA concentration
    ("10000 IU/ML", "10000iu/ml", None),  # NPPA id 421 Erythropoietin
    ("5000 IU/ML", "5000iu/ml", None),  # measured NPPA IU concentration
    ("10 GM/ 15 ML", "666.667mg/ml", None),  # measured NPPA gm concentration
    ("440 MG/ 50 ML", "8.8mg/ml", None),  # measured NPPA concentration
    ("100 MG/16.7 ML", "5.98802mg/ml", None),  # measured NPPA concentration
    ("10 mg/ml (1ml &\n2ml Pack)", "10mg/ml", None),  # NPPA id 44 pack note
    # --- IU / KU ---
    ("1500000 IU", "1500000iu", None),  # measured NPPA IU (commas stripped)
    ("300 IU", "300iu", None),  # measured NPPA IU
    ("5000 KU", "5000ku", None),  # measured NPPA KU passthrough
    # --- combos: sorted, order-insensitive ---
    ("6/400 MCG", "0.006mg+0.4mg", None),  # NPPA id 462 Formoteral+Budesonide
    ("6/100 MCG", "0.006mg+0.1mg", None),  # NPPA id 461
    ("100/25 mg", "25mg+100mg", None),  # NPPA id 87 Lopinavir+Ritonavir
    ("50/500/25 MG", "25mg+50mg+500mg", None),  # NPPA id 211 triple combo
    ("2000/250 MG", "250mg+2000mg", None),  # NPPA id 739 Piperacillin+Tazo
    ("125/31.25 MG", "31.25mg+125mg", None),  # NPPA id 193 Amoxy+Clav
    ("0.4/0.1 MG", "0.1mg+0.4mg", None),  # NPPA id 264 Buprenorphine+Naloxone
    ("2%/0.005MG", "0.005mg+2%wv", None),  # NPPA Lidocaine+Adrenaline style
    ("5% (A) + 0.9% (B)", "0.9%wv+5%wv", None),  # NPPA id 65 Glucose+NaCl
    ("500 MG (A)\n+125 MG(B)", "125mg+500mg", None),  # NPPA Amoxicillin+Clav
    ("40 MG(A) + 240\nMG(B)", "40mg+240mg", None),  # NPPA Artemether+Lumef.
    ("0.03 MG(A) + 0.15\nMG(B)", "0.03mg+0.15mg", None),  # NPPA Ethinylest.+Levo
    ("800 MG(A)+160\nMG(B)", "160mg+800mg", None),  # NPPA Co-trimoxazole
    ("60 (A) + 30 mg (B)", "30mg+60mg", None),  # NPPA id 7 bare-number inherit
    ("600 (A) + 300 MG (B)", "300mg+600mg", None),  # NPPA id 152 same defect
    ("100 mg elemental iron\n(A) + 500mcg (B)", "0.5mg+100mg", None),  # id 50
    ("100 mg(A) + 1 Tablet\n(750 mg+ 37.5 mg) (B)", "37.5mg+100mg+750mg", None),
    ("0.5% with 7.5% Glucose", "0.5%wv+7.5%wv", None),  # NPPA id 260 Bupivac.
    ("5 % with 7.5 %", "5%wv+7.5%wv", None),  # NPPA id 593 Lidocaine heavy
    ("25 %  in 500ml pack for packages in non-glass container", "25%wv", None),
    ("500mg/100ml  in 100ml pack for packages in non-glass container",
     "5mg/ml", None),
    # --- unknown: valid rows, queued with reasons, never faked ---
    ("--", None, "absent_strength"),  # brief section C example
    (None, None, "absent_strength"),
    ("1% (A) + 1:200000\n(5 mcg/ml) (B)", None, "ratio_strength"),  # id 86
    ("0.5ml contains:\nDiphtheria Toxoid ?5Lf (?\n2IU) Tetanus Toxoid ? 5Lf "
     "(?\n40IU) 5 LF", None, "degenerate_composition"),  # NPPA id 133 vaccine
    ("4 MCG to 6 MCG", None, "range_strength"),  # NPPA id 549 range
    ("2-5%", None, "range_strength"),  # measured NPPA range
    ("1000000", None, "missing_unit"),  # NPPA id 22 Benzyl penicillin units
    ("500 ml", None, "volume_not_strength"),  # NPPA id 116 Ringer Lactate
    ("0.5 ML", None, "volume_not_strength"),  # NPPA id 608 Measles vaccine
]


@pytest.mark.parametrize(("raw", "expected", "reason"), STRENGTH_GOLDENS)
def test_parse_strength_goldens(raw, expected, reason):
    canon, got_reason = parse_strength(raw)
    assert canon == expected
    assert got_reason == reason


def test_parse_strength_parts_keep_positional_order():
    # Slash-combo positional assignment (brief section C limitation probe):
    # file order must be preserved for molecule binding, sorted for the key.
    parts, reason = parse_strength_parts("6/400 MCG")  # NPPA id 462
    assert reason is None
    assert parts == ["0.006mg", "0.4mg"]
    canon, _ = parse_strength("6/400 MCG")
    assert canon == "0.006mg+0.4mg"


def test_parse_strength_indian_commas_synthetic():
    # No comma-grouped strength survives in the current seeds ("1500000 IU"
    # is stored comma-free); the grammar still strips Indian grouping.
    assert parse_strength("15,00,000 IU") == ("1500000iu", None)


# ---------------------------------------------------------------------------
# Text cleanup goldens
# ---------------------------------------------------------------------------


def test_clean_text_nbsp_and_quality_markers():
    # JAP pid 162 carries U+00A0 between Olmesartan and 20mg.
    raw = "Olmesartan 20mg, Amlodipine 5mg Tablets"
    assert " " not in clean_text(raw)
    assert clean_text("Ibuprofen Tablets IP 200 mg") == "Ibuprofen Tablets 200 mg"
    assert fold("ACETYLSALICYLIC ACID") == "acetylsalicylic acid"


def test_clean_text_preserves_greek():
    assert "α" in clean_text("α-β Arteether Tablets")


# ---------------------------------------------------------------------------
# Form goldens: (raw, expected_base, expected_family)
# ---------------------------------------------------------------------------

FORM_GOLDENS: list[tuple[str, str, str]] = [
    ("TABLET", "tablet", "tablet"),
    ("Tablet", "tablet", "tablet"),
    ("CAPSULE", "capsule", "capsule"),  # Phase 3.5: distinct base (was collapse)
    ("Capsule", "capsule", "capsule"),  # Phase 3.5: distinct base (was collapse)
    ("CAPS", "capsule", "capsule"),  # Phase 3.5: short form is capsule
    ("CAP", "capsule", "capsule"),  # Phase 3.5: short form is capsule
    ("INJECTION", "injection", "injectable"),
    ("Injection", "injection", "injectable"),
    ("POWDER FOR INJECTION", "injection", "injectable"),
    ("INFUSION", "infusion", "injectable"),
    ("ORAL LIQUID", "solution", "liquid"),
    ("SYRUP", "syrup", "liquid"),
    ("SUSPENSION", "suspension", "liquid"),
    ("ORAL SUSPENSION", "suspension", "liquid"),
    ("DRY SYRUP", "dry_syrup", "liquid"),
    ("INHALER", "inhaler", "inhalation"),
    ("Inhalation", "inhalation", "inhalation"),
    ("DROPS", "drops", "drops"),
    ("EYE DROPS", "eye_drops", "eye_drops"),
    ("EAR DROPS", "ear_drops", "ear_drops"),
    ("CREAM", "cream", "topical"),
    ("GEL", "gel", "topical"),
    ("OINTMENT", "ointment", "topical"),
    ("LOTION", "lotion", "topical"),
    ("MODIFIED RELEASE TABLET", "tablet", "tablet"),
    ("MODIFIED RELEASE CAPSULE", "capsule", "capsule"),  # Phase 3.5: capsule base kept
    ("ER CAPSULE", "capsule", "capsule"),  # Phase 3.5: capsule base kept (was tablet)
    ("EFFERVESCENT/ DISPERSIBLE/\nENTERIC COATED TABLET", "tablet", "tablet"),
    ("TABLET DT", "tablet", "tablet"),
    ("SUPPOSITORY", "suppository", "suppository"),
    ("NASAL SPRAY", "nasal_spray", "inhalation"),
    ("VACCINE", "vaccine", "vaccine"),
    ("CONDOM", "condom", "condom"),
    ("SACHET", "sachet", "sachet"),
]


@pytest.mark.parametrize(("raw", "base", "family"), FORM_GOLDENS)
def test_canonical_form_goldens(raw, base, family):
    got = canonical_form(raw)
    assert got == base
    assert form_family(got) == family


def test_eye_ear_drops_never_share_family():
    assert form_family(canonical_form("EYE DROPS")) != form_family(
        canonical_form("EAR DROPS")
    )


def test_extract_modifiers():
    assert extract_modifiers("MODIFIED RELEASE TABLET") == ("modified_release",)
    assert extract_modifiers("Sustained Release Tablets") == ("sustained_release",)
    assert extract_modifiers("Enteric Coated Tablets") == ("enteric_coated",)
    assert extract_modifiers("Plain Tablets") == ()


# ---------------------------------------------------------------------------
# Pack goldens: (unitSize, expected_kind, expected_value)
# ---------------------------------------------------------------------------

PACK_GOLDENS: list[tuple[str, str, float]] = [
    ("10's", "count", 10.0),
    ("100's", "count", 100.0),
    ("1's", "count", 1.0),
    ("30 ml", "volume_ml", 30.0),
    ("170 ml Bottle", "volume_ml", 170.0),
    ("Vial", "vial", 1.0),
    ("Vial & Wfi", "vial", 1.0),
    ("10 ml Vial", "volume_ml", 10.0),
    ("10 ml & wfi", "volume_ml", 10.0),
    ("200 MDI", "dose_count", 200.0),
    ("200 md", "dose_count", 200.0),
    ("70 MD", "dose_count", 70.0),
    ("Cartridge 3 ml", "volume_ml", 3.0),
    ("1 Sachet", "count", 1.0),
    ("15 g tubes", "weight_g", 15.0),
    ("5 gm Tube", "weight_g", 5.0),
    ("2 ml Ampoule", "volume_ml", 2.0),
    ("5 ml Amp.", "volume_ml", 5.0),
    ("0.5 ml", "volume_ml", 0.5),
    ("1’s in Mono Carton", "count", 1.0),  # curly apostrophe, measured
    ("60 s in container", "count", 60.0),  # JAP Rotacaps shape
    ("30's Bottle", "count", 30.0),
    ("500g Screw-Cap jar with scoop", "weight_g", 500.0),
    ("1's Tin 250g", "weight_g", 250.0),  # weight wins over tin count
    ("60 doses in MDI inhaler", "dose_count", 60.0),
    ("100 Ear Buds", "count", 100.0),
    ("Pack of 4 Sanitary Napkins", "count", 4.0),
    ("Three in Mono-Pack", "count", 3.0),
    ("Pair in Mono-Pack", "count", 2.0),
    ("Combipack of 6 Tablets in Mono Carton", "count", 6.0),
]

PACK_UNPARSEABLE = [
    "30 capsules and 1 inhaler",  # combo pack: valid row, no divisor
    "KIT",
    "Screw-cap plastic container",
    "2 cm X 3 m in Monopack",  # dressing dimensions, not a divisor
]


@pytest.mark.parametrize(("raw", "kind", "value"), PACK_GOLDENS)
def test_parse_pack_goldens(raw, kind, value):
    pack, reason = parse_pack(raw)
    assert reason is None
    assert pack is not None
    assert pack.kind == kind
    assert pack.value == value


@pytest.mark.parametrize("raw", PACK_UNPARSEABLE)
def test_parse_pack_unparseable(raw):
    pack, reason = parse_pack(raw)
    assert pack is None
    assert reason == "unparseable_pack"


def test_make_key_pairs_preferred():
    key = make_key(["budesonide", "formoterol"], ["0.2mg", "0.006mg"], "inhaler")
    assert key.key() == "budesonide:0.2mg+formoterol:0.006mg|inhaler"
    unknown = make_key(["water for injection"], [None], "injection")
    assert unknown.key() == "water for injection:|injection"


# ---------------------------------------------------------------------------
# JAP composition goldens: (product_id, molecules, strengths, form_raw,
# modifiers); all from data/processed/drugs.db jap_products.
# ---------------------------------------------------------------------------

COMPOSITION_GOLDENS: list[tuple[int, str, tuple, tuple, str | None, tuple]] = [
    (242, "Ibuprofen Tablets IP 200 mg",
     ("Ibuprofen",), ("200mg",), "Tablets", ()),
    (1, "Aceclofenac 100mg and Paracetamol 325mg Tablets",
     ("Aceclofenac", "Paracetamol"), ("100mg", "325mg"), "Tablets", ()),
    (189, "Metformin Hydrochloride Sustained Release Tablets IP 1000 mg",
     ("Metformin Hydrochloride",), ("1000mg",), "Tablets",
     ("sustained_release",)),
    (886, "Paracetamol Paediatric Oral Suspension IP 125 mg per 5 ml",
     ("Paracetamol",), ("25mg/ml",), "Oral Suspension", ()),
    (822, "Rabeprazole 20mg (Enteric Coated) and Domperidone 30mg"
          " (Sustained Release) Capsules",
     ("Rabeprazole", "Domperidone"), ("20mg", "30mg"), "Capsules",
     ("enteric_coated", "sustained_release")),
    (1899, "Formoterol 6mcg and Budesonide 200mcg Rotacaps",
     ("Formoterol", "Budesonide"), ("0.006mg", "0.2mg"), "Rotacaps", ()),
    (1804, "Combipack of Mifepristone Tablets IP 200mg\u00a0 (1 Tablet)"
           " & Misoprostol Tablets IP 200mcg (4 Tablets)",
     ("Mifepristone", "Misoprostol"), ("200mg", "0.2mg"), "Tablets", ()),
    (1679, "Lignocaine and Adrenaline Injection IP (2%w/v and 1:80000)",
     ("Lignocaine", "Adrenaline"), ("2%wv", None), "Injection", ()),
    (1928, "Cefoperazone Injection IP 1 g",
     ("Cefoperazone",), ("1000mg",), "Injection", ()),
    # JAP pid 162 carries U+00A0 between Olmesartan and 20mg (NBSP trap).
    (162, "Olmesartan\u00a020mg, Amlodipine 5mg and Hydrochlorothiazide 12.5mg"
          " Tablets",
     ("Olmesartan", "Amlodipine", "Hydrochlorothiazide"),
     ("20mg", "5mg", "12.5mg"), "Tablets", ()),
    (2118, "Aspirin Enteric Coated Tablets IP 75mg",
     ("Aspirin",), ("75mg",), "Tablets", ("enteric_coated",)),
    (361, "Formoterol Fumarate 6mcg and Budesonide 200mcg Inhaler",
     ("Formoterol Fumarate", "Budesonide"), ("0.006mg", "0.2mg"),
     "Inhaler", ()),
    (2078, "Water for Injection amp polypack 5 ml",
     ("Water for Injection",), (None,), "polypack", ()),
    (191, "Janaushadhi Nirmal (Nicotine Polacrilex Chewing Gum 2 mg))",
     ("Nicotine Polacrilex",), ("2mg",), "Chewing Gum", ()),
    (2312, "Co-trimoxazole (Sulphamethoxazole 100mg and Trimethoprim 20mg)"
           " Tablets IP",
     ("Sulphamethoxazole", "Trimethoprim"), ("100mg", "20mg"), "Tablets", ()),
]


@pytest.mark.parametrize(
    ("pid", "name", "molecules", "strengths", "form", "modifiers"),
    COMPOSITION_GOLDENS,
)
def test_extract_jap_composition_goldens(
    pid, name, molecules, strengths, form, modifiers
):
    import sqlite3
    from pathlib import Path

    seed = Path(__file__).parent.parent.parent / "data" / "raw"
    if seed.exists():  # pin the golden to the real seed row when present
        with sqlite3.connect(seed.parent / "processed" / "drugs.db") as conn:
            row = conn.execute(
                "SELECT generic_name FROM jap_products WHERE product_id = ?",
                (pid,),
            ).fetchone()
            assert row is not None and row[0] == name
    comp = extract_jap_composition(name)
    assert comp.ok, comp.notes
    assert comp.molecules == molecules
    assert comp.strengths == strengths
    assert comp.form_raw == form
    assert comp.modifiers == modifiers


def test_extract_jap_composition_ratio_flags_low_confidence():
    # JAP pid 1679: ratio component kept raw, never faked, flagged.
    comp = extract_jap_composition(
        "Lignocaine and Adrenaline Injection IP (2%w/v and 1:80000)")
    assert comp.ok and comp.low_confidence
    assert comp.strengths == ("2%wv", None)


def test_extract_jap_composition_complex_routing():
    # JAP pid 993 Vitamin B-Complex + pid 1907 Menthol +/- mix.
    assert extract_jap_composition(
        "Vitamin B-Complex Tablets (B1 10mg, B2 10mg)").reason == (
        "complex_composition")
    assert extract_jap_composition(
        "Menthol (55 mg ± 5.) Cinnamon Capsules").reason == (
        "complex_composition")


ALIAS_GOLDENS: list[tuple[str, str]] = [
    ("acetylsalicylic acid", "aspirin"),
    ("frusemide", "furosemide"),
    ("formoteral", "formoterol"),
    ("amoxycillin", "amoxicillin"),
    ("metformin hydrochloride", "metformin"),
    ("s(-)amlodipine", "amlodipine"),
    ("s(-) amlodipine", "amlodipine"),
    ("levo-thyroxine", "levothyroxine"),
    ("cetrizine", "cetirizine"),
    ("nimesulid", "nimesulide"),
    ("medroxyprogesteroneacetate", "medroxyprogesterone acetate"),
    ("amlodipine besilate", "amlodipine"),
    ("diclofenac diethylamine", "diclofenac"),
    ("losartan potassium", "losartan"),
    ("formoterol fumarate dihydrate", "formoterol"),
    # Guards: base-name words that merely end in salt-like syllables stay.
    ("calcium phosphate", "calcium phosphate"),
    ("hydrochlorothiazide", "hydrochlorothiazide"),
    ("sodium valproate", "sodium valproate"),
    ("sodium chloride", "sodium chloride"),
]


@pytest.mark.parametrize(("raw", "expected"), ALIAS_GOLDENS)
def test_canonical_molecules_aliases(raw, expected):
    assert canonical_molecules([raw]) == [expected]


def test_canonical_molecules_plain_keeps_prealias_form():
    # Stage-1 keys use cleaned but unaliased molecules.
    assert canonical_molecules(["Acetylsalicylic Acid"]) == ["aspirin"]
    assert canonical_molecules(["Acetylsalicylic Acid"], use_alias=False) == [
        "acetylsalicylic acid"]


NPPA_SPLIT_GOLDENS: list[tuple[str, int | None, list[str]]] = [
    ("Glucose (A) +\nSodium Chloride (B)", None,
     ["Glucose", "Sodium Chloride"]),
    ("CO-TRIMOXAZOLE (SULPHAMETHOXAZOLE(A)+TRIMETHOPRIM(B)]", None,
     ["SULPHAMETHOXAZOLE", "TRIMETHOPRIM"]),
    ("Artesunate (A) + Sulphadoxine -Pyrimethamine (B)", 3,
     ["Artesunate", "Sulphadoxine", "Pyrimethamine"]),
    ("AMPHOTERICIN B - LIPOSOMAL", 1, ["AMPHOTERICIN B - LIPOSOMAL"]),
    ("ASCORBIC ACID (VITAMIN C)", None, ["ASCORBIC ACID"]),
    ("PHYTOMENADIONE (VITAMINK1)10 MG", None, ["PHYTOMENADIONE"]),
    ("Lignocaine (A)+Adrenaline (B)", None, ["Lignocaine", "Adrenaline"]),
]


@pytest.mark.parametrize(("raw", "n", "expected"), NPPA_SPLIT_GOLDENS)
def test_split_nppa_molecules_goldens(raw, n, expected):
    assert split_nppa_molecules(raw, n) == expected
