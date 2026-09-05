
# Data Sources — Verified & Inspected (2026-09-05)

Every fact below was measured directly from files on disk or proven by
manual HTTP replication. No third-party claims were inherited without
re-verification. (Third-party research pegged the compendium at "907
formulations" — our own count is 915 rows / 866 unique keys; the
discrepancy is documented below.)

## Jan Aushadhi (PMBJP) — Tier A (automated)

Transport (2 requests per refresh, reverse-engineered from the public SPA):

1. GET https://janaushadhi.gov.in:8443/auth/generateGuestToken
   Returns a guest JWT (role: ROLE_GUEST). Validity: 30 minutes.
   Regenerate on 401/500 (backend returns 500, not 401, on missing/expired
   token — verified empirically).
2. POST https://janaushadhi.gov.in:8443/api/v1/website/getAllProductForWeb
   Content-Type: application/json
   Authorization: Bearer <guest token>
   Body: {"pageIndex":0,"pageSize":2439,"searchText":"",
          "columnName":"drug_code","orderBy":"asc"}
   One-shot full pull — server honors pageSize=2439 (verified:
   totalElement 2439, Count 2439, isLastPage true, 466,945 bytes).
   NOTE: pageIndex is 0-based.

- Seed file: data/raw/jap_products.json — 2,439 products
- SHA-256: D51391A3D63C647BC2623D36FB118A347F1E8E36D7169E41CDE9088847CF7A6A
- Fields: productId, genericName, groupName, drugCode, unitSize, mrp, status
- Notes:
  - status: 1 = active, 0 = inactive → inactive rows go to reject queue
  - MRP is PER PACK → per-unit price = MRP ÷ parsed pack size
  - CSV-button export cross-validated identical rows (serialNo 11–20)
    against the API — two independent acquisition paths agree

## NPPA Ceiling Prices — Tier B (manual export, validated on drop)

Acquisition: IPDMS dashboard (nppaipdms.gov.in) manual export. The export
URL is session-scoped (hospitalCode/seatId params), so by design the
pipeline never fetches NPPA automatically; humans export, the pipeline
validates.

Seed files:

data/raw/nppa_ceiling_all.csv
- 920 lines = 4 preamble rows + header (line 5) + 915 data rows
- 866 unique (formulation, strength) keys; 19 keys have multiple rows
- Multiplicity = PACK VARIANTS, not NLEM duplicates: same NLEM + same SO
  number, different price qualifiers — glass/non-glass containers, pack
  volumes (100/250/500/1000 ml), unit bases (per-ml, per-pack,
  per-metered-dose), pack-size thresholds (<10ml vs >=10ml)
- Data model: (formulation, strength, pack-variant, unit-basis) -> price
- Exactly 1 true duplicate row (Intermediate-acting Insulin 40 IU/ml,
  Rs 17.40 twice) — genuine source data-quality defect
- Columns: SL No, NLEM Version (2011/2015/2022), Formulation,
  Dosage & Strength, SO Number, SO Date, Ceiling Price
- Ceiling Price format: "₹ 0.28(1 Tablet)" — value + unit-basis qualifier;
  read file as UTF-8 (utf-8-sig); some symbols corrupted in source
  ("?" where >= belongs, TD Vaccine row)
- SHA-256: 385A9AFCC59704BAF8621DD8824E56CB58E05B382EC7C73B1DA2AEB4B17EBA0A

data/raw/nppa_ceiling_special.csv
- 22 data rows, same 4-row preamble, DIFFERENT schema (no NLEM column)
- Special-feature packs (non-glass containers, dual-chamber bags)
- SHA-256: E8F8984BDAA0646B0614F5B8D35CB3E85D2AFA8CAC442767BF3F1ADEC4BA496D

As-on date: 25-Mar-2026 (SO 1575/1581-1584(E)) — snapshot, not live;
orders issued after this date are not included until NPPA regenerates.

Refresh procedure: manual IPDMS re-export → drop into data/raw/ →
pipeline validates (SHA-256 manifest, schema, row count; zero-rows alarm).
Staleness alarm: flag if as-on date > 6 months old.

## Cross-Validation Fixtures (Tier C)

Category PDFs — parsed via extract_text() (line format:
"serial  molecule  form+strength  unit-basis  price").
extract_tables() is unusable on these (returns None/garbage — verified).

| File | Pages | Formulations | SHA-256 |
|---|---|---|---|
| nppa_cat_anticancer.pdf | 15 | 131 | 65C33E1776EA8552C41030582E9BCE9FFAA8B8BE7F12FF4332D26A875D847CFE |
| nppa_cat_cardiovascular.pdf | 8 | 66 | 8CE7D0A8216104958F049C9442DA12F5B4E125B10AF45824F2A6CBC7C342095D |
| nppa_cat_antitb.pdf | 5 | 30 | 961F0C688D441D158CFB7B17E5B55CD687CF14696E7053741DD85C30E48CAD99 |
| nppa_cat_antihiv.pdf | 5 | 29 | D7CE77C499E863B37EDADC4CC13EC74E57AF6B8E9C9ED5EEA4E9FDC554CF73B0 |
| nppa_cat_antidiabetes.pdf | 3 | 11 | FD23814AE3A0009A0D8D6E14D079FF3E72605EDFAE6041E3529CCD988F311E62 |

All "As on March, 2026". nppa_cat_antihiv.pdf page 1 is image-only
(0 chars extracted); data starts page 2.

Known parsing traps (from real inspection):
- Molecule names wrap across lines, fragments appear BEFORE and/or AFTER
  the data line (e.g. "Bendamustine" ... "9 Injection ..." "hydrochloride")
- Spaceless mangled names: "TenofovirDisproxilFumarate" (split mid-word
  across lines)
- "(p)" pediatric markers; "(?5Lf" corrupted >= symbols
- Double qualifiers: "(Per Metered Dose)( Pack)"

Role: automated cross-validation of the compendium CSV — 267 formulations,
deduped across categories (several molecules appear in 2 categories with
different SO references), compared at ±1% tolerance, mismatch report with
SO provenance.

## Parser-Development Fixtures (currently in data/raw/, move to
## tests/fixtures/ in Phase 2)

- nppa_order_sample1.pdf — Para-5 retail price order, Amoxy+Clav 400/57
  (4 pages, 113 brand rows) — SHA-256:
  6FB8FB23D9BEB4564F3FD5351EF6F7373DF65DF3ADF4B7BAF33191455ABD4F09
- nppa_order_sample2.pdf — Para-5 order, Rosuvastatin+Aspirin+Clopidogrel
  75/75/10 (2 pages, 87 brand rows) — SHA-256:
  1BA1167F5134F35BD8E700C52A17356ADEBCEAE187430E9E76A49E6A45F621BC
- Order-format rules: Hindi text unextractable (cid: markers) → anchor on
  English markers only; Indian digit grouping inconsistent across orders
  (2,26,61,27,730 vs 3210738); two near-identical "Price per Unit"
  columns (disambiguate by position); strength column order ≠ composition
  order (75/75/10 = Rosuva 10 + Aspirin 75 + Clopido 75 — parse the
  composition text, not the strength column)

## Rejected Sources

- BPPI-MRP-LIST_11122018.pdf (janaushadhi.gov.in/Data/) — 7 years stale.
  Never use for prices. Wrong prices are worse than no prices.

## Molecule Alias Evidence (seed entries for normalize.py)

Observed cross-source / intra-source name variants requiring aliasing:
- Acetylsalicylic acid (NPPA) = Aspirin (JAP)
- Frusemide = Furosemide; Levo-Thyroxine = Levothyroxine
- FORMOTERAL (NPPA, sic) = Formoterol
- Metformin Hydrochloride / HCl = Metformin
- S(-)Amlodipine = Amlodipine
- TenofovirDisproxilFumarate (mangled) = Tenofovir Disoproxil Fumarate
- Medroxyprogesteroneacetate (missing space) = Medroxyprogesterone Acetate

