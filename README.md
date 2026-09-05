# SameDrug

Drug price transparency for India: NPPA ceiling prices vs Jan Aushadhi equivalents.

## The Problem

The same molecule sells at wildly different prices across brands, with spreads of 5–50x between the costliest branded formulation and the cheapest equivalent. Patients have no simple way to compare what a branded drug should cost at most against its Jan Aushadhi alternative.

## What it does

- Ingests the NPPA scheduled-formulation ceiling-price publication.
- Ingests the Jan Aushadhi (PMBJP) product price list.
- Serves equivalence lookups: a branded drug's NPPA ceiling vs its cheapest Jan Aushadhi equivalent.

## Disclaimers

- Informational only. This is NOT medical advice.
- Verify prices and substitutions with a pharmacist.
- Data © NPPA / PMBJP, attributed, not affiliated.

## Data sources

- NPPA ceiling publication — URL: TODO(verify).
- Jan Aushadhi price list — URL: TODO(verify).

## Quickstart

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install -e ".[dev]"
.venv\Scripts\python.exe -m pytest
```

## Roadmap

| Phase | Name | Status |
| ----- | ---- | ------ |
| 0 | Scaffold | Current |
| 1 | Sources | Planned |
| 2 | Parsers | Planned |
| 3 | Normalization | Planned |
| 4 | DB | Planned |
| 5 | API | Planned |
| 6 | UI | Planned |
| 7 | Deploy | Planned |
| 8 | Refresh automation | Planned |
| 9 | Release | Planned |

## License

MIT
