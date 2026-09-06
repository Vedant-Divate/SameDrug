"""Verified Jan Aushadhi (JAP) transport contract.

Source of truth: docs/sources.md — every constant below was measured from
real HTTP responses and files on disk (see that doc for the full evidence).
Where docs/DESIGN.md Phase 1 disagrees, this module (and docs/sources.md)
win: DESIGN.md predates verification.
"""

JAP_BASE_URL = "https://janaushadhi.gov.in:8443"

JAP_TOKEN_URL = f"{JAP_BASE_URL}/auth/generateGuestToken"

JAP_PRODUCTS_URL = f"{JAP_BASE_URL}/api/v1/website/getAllProductForWeb"

# Exact one-shot full-pull body: pageIndex is 0-based, server honors
# pageSize=2439 (verified: totalElement 2439, isLastPage true, ~467 KB).
JAP_FETCH_PAYLOAD = {
    "pageIndex": 0,
    "pageSize": 2439,
    "searchText": "",
    "columnName": "drug_code",
    "orderBy": "asc",
}

# Honest bot identity: name + purpose + repo placeholder.
USER_AGENT = (
    "SameDrug/0.1 (public-interest drug-price transparency; "
    "repo: TODO(user) add public repo URL)"
)
