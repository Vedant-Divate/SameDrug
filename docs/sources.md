
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


## Addendum — post-Phase-1 measurements (2026-09-06)

JAP source (pipeline built in Phase 1; see data/processed/drugs.db):
- Measured: 387/2,439 rows have mrp=0.00 — interpreted as "price not yet
  published" (corroborated by the PMBI site footnote on zero-MRP products),
  not free; excluded from price comparisons at the equivalents layer.
- Measured: 19 genericNames contain U+00A0 non-breaking spaces, and some
  contain Greek letters (alpha/beta). normalize.py must apply NFKC and
  NBSP-to-space conversion before matching, or joins will silently miss.
- Live-fetch verification: two full pulls through the built fetcher matched
  the seed byte-for-byte across all 2,439 rows.

## Addendum — post-Phase-2 measurements (2026-09-06)

NPPA ingestion complete (nppa_ceiling_prices table, see data/processed/drugs.db):
- 937 price cells across both CSVs; 69 distinct qualifiers, 100% classified
  by the parser grammar; zero unparseable rejects. Full inventory preserved
  in the Phase 2 report and parser tests.
- 936 kept rows; exactly 1 duplicate rejected (Intermediate-acting Insulin
  40 IU/ml, kept once).
- Spec corrections (found by measurement): data rows carry ONE leading empty
  column, not two; the 100ml pair is Glass=Rs 22.99 / Non-Glass=Rs 20.80.
- The special-feature file is a variant-price subset: all 10 of its
  formulations already appear in the all-file.
- Distinct formulation names: 437; (formulation, strength) keys: 866.
- Order-sample PDFs remain in data/raw (gitignored) and move to
  tests/fixtures/ when the Para-5 order-parser phase begins.

## Addendum — post-Phase-3.5 fixes and verified findings (2026-09-06)

- Cross-form capsule/tablet defect (22 rows): Step-0 audit proved 22/22
  faithful NPPA labels, zero splitter mis-reads; capsule made distinct
  base form; post-fix leak count 0 (verified by independent query).
  Equivalents 289 -> 302: 22 re-matched to genuine same-form rows + 14
  concentration matches - 1 false positive. Count ROSE because wrong
  matches became right matches.
- Variant selection now pack-condition + NLEM-vintage aware (new column
  nppa_nlem_version; chosen: 2022=291, 2015=9, NULL=2). Dicyclomine
  injection corrected from -837.5% (old-vintage Rs 0.2/ml row) to
  +44.85% on the Rs 3.40 (<10ml, NLEM-2022) row.
- GENUINE JAP-above-ceiling findings (kept, verified): pheniramine
  22.75mg inj Rs 1.875/ml vs ceiling Rs 1.415/ml (-32.5%);
  dexamethasone 4mg/ml -8.1% = tax artifact (ceilings tax-exclusive,
  JAP MRP tax-inclusive; 5.21 x 1.08 = 5.63 exactly).
- NPPA qualifier semantics: "(1 ML)" = concentration on small-volume
  injections but total content on large-volume infusions (metronidazole);
  resolved by measured match outcomes.
- Tier-C agreement: imatinib 400mg ceiling Rs 325.64 matches the
  anti-cancer category PDF value.
- Post-fix state: 321/866 NPPA keys matched (37.1%); 302 equivalences.
  Both dicyclomine rows independently verified as distinct formulation
  keys: injection (NLEM 2022, pack-condition variant, +44.85%) and
  tablet (NLEM 2015, +27.7% — JAP Rs 0.094/tab vs Rs 0.13 ceiling,
  cross-checked against the original seed CSV row 148).

## Addendum — post-Phase-4 measurements (2026-09-06)

API layer verified live (read-only DB enforced by test):
- /api/equivalents/paracetamol:500mg|tablet: ceiling Rs 0.93 (2022,
  SO 1575(E), sl_no=706) vs JAP Rs 0.656 (product 982) = 29.46% savings,
  exact/high, full provenance. Math independently recomputed.
- Dicyclomine 10mg/ml injection via API: Rs 3.40, NLEM 2022,
  pack_condition_match, +44.85% — Phase 3.5 fix locked at HTTP layer
  (integration test).
- /api/jap/1493 (mrp=0): returns excluded_reason price_not_published —
  the zero-MRP gate is user-visible honesty, not silent omission.
- Key denominators clarified: 866 = raw (formulation, strength) keys
  (Phase 2 measurement); 867 = canonical triples after normalization;
  /api/stats uses 867, the match ladder reports both.
- Fuzzy review band (0.85-0.92) held two candidates: OMEPRAZOLE vs
  Esomeprazole (90.9 — different INNs, never join; the band prevented
  a clinically wrong auto-match) and CARBOXY METHYL CELLULOSE vs
  Carboxymethlycellulose (91.3 — genuine JAP typo, future alias).
- Pipeline determinism: full match re-run produced identical stats
  (302 equivalents, same ladder and savings) — rebuilds are
  reproducible.
- Alias backlog (named): vitamin d3 (cholecalciferol) for the 60000iu
  sachet rows; brand-name table for search (Dolo etc.); Paracetamol
  150mg injection pack-variant matching (6 unmatched rows, NLEM 2015).
- Incident note: a git index.lock collision (IDE pollers) interrupted
  cleanup; resolved via diff-based verification — all test files
  confirmed byte-identical to HEAD, no work lost.
