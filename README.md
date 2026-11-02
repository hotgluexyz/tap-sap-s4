# tap-sap-s4

A new, independent SAP S/4HANA **OData V2, Basic Auth, full-table** Singer tap.
Scaffolded from the cached Hotglue cookiecutter; uses `hotglue-singer-sdk==1.0.52`.
It neither imports nor extends a legacy SAP connector.

## Streams

All paths are beneath `/sap/opu/odata/sap/`. All streams use `FULL_TABLE` with
**no replication key**. Keys and scalar properties come from captured SAP EDMX:

| Singer stream | Service / entity set | Exact key properties | Fields |
|---|---|---|---|
| `company_codes` | `API_COMPANYCODE_SRV/A_CompanyCode` | `CompanyCode` | 21 |
| `gl_accounts` | `API_GLACCOUNTINCHARTOFACCOUNTS_SRV/A_GLAccountInChartOfAccounts` | `ChartOfAccounts`, `GLAccount` | 19 |
| `gl_account_texts` | `API_GLACCOUNTINCHARTOFACCOUNTS_SRV/A_GLAccountText` | `ChartOfAccounts`, `GLAccount`, `Language` | 6 |
| `journal_entry_items` | `API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube` | `CompanyCode`, `FiscalYear`, `AccountingDocument`, `AccountingDocumentItem` | 270 |

`tap_sap_s4/metadata/{company,gl,items}.xml` are verbatim snapshots of the supplied
`company-metadata.txt`, `gl-service.txt`, and `items-metadata.txt`. `schema.py` resolves
entity-set types and keys directly from these files. Discovery is offline, so metadata
updates require replacing the snapshots and retesting. No guessed sample schemas.

## Configuration

| Setting | Type | Required | Default | Meaning |
|---|---|---|---|---|
| `base_url` | string | yes | none | HTTPS origin, e.g. `https://sap.example.com:443`; no API path, userinfo, query, or fragment |
| `username` | string | yes | none | Sensitive Basic Auth username |
| `password` | string | yes | none | Sensitive Basic Auth password |
| `sap_client` | string | no | omitted | Optional `sap-client` query option; leading zeros preserved |
| `company_code` | string | no | omitted | Optional exact `CompanyCode` OData filter, only for company codes and journal items |
| `verify_ssl` | boolean | no | `true` | Verify server TLS certificate; disable only in controlled tests |
| `page_size` | integer, 1–10000 | no | `1000` | Advisory `Prefer: odata.maxpagesize=N` header; SAP may ignore this preference |

G/L account and text entity sets have no CompanyCode property and remain chart-wide,
even when `company_code` is set. Filter string literals escape embedded apostrophes.
There are no baked sandbox hosts, clients, companies, or credentials.

Example (save actual secrets only under ignored `.secrets/`, never commit them):

```json
{
  "base_url": "https://sap.example.com",
  "username": "YOUR_USERNAME",
  "password": "YOUR_PASSWORD",
  "verify_ssl": true,
  "page_size": 1000
}
```

`--config=ENV` supports `TAP_SAP_S4_*` variables; see `.env.example` and `--about`.

## Install and run

Use this connector's own seeded Python 3.10 environment:

```bash
uv venv --python 3.10 --seed .venv
uv pip install --python .venv/bin/python -e '.[test]'
mkdir -p .secrets
.venv/bin/tap-sap-s4 --config .secrets/config.json --discover > .secrets/catalog.json
# Select only wanted streams/fields in a copy of the discovered Singer catalog.
.venv/bin/tap-sap-s4 --config .secrets/config.json --catalog .secrets/catalog-selected.json > .secrets/data.singer
.venv/bin/pytest -q
.venv/bin/ruff check --config ruff.toml tap_sap_s4 tests
.venv/bin/ruff format --config ruff.toml --check tap_sap_s4 tests
```

## Transport and data contracts

- Reads `d.results`; follows relative or absolute `d.__next` links, retaining original
  query options when omitted from the next link. Server-provided options take precedence.
- No `$top` is injected: in OData it limits the **whole result**, not just page size.
  Server-driven pagination is authoritative. The page-size preference is not guaranteed
  by OData V2 SAP services; a server may return a larger response.
- Basic Auth is attached only after validating same scheme/host/effective port.
  Cross-origin, userinfo-bearing, fragment-bearing and malformed next links fail closed.
  HTTP redirects are disabled and fail rather than forwarding credentials.
- At most five attempts per request on 429, 5xx and transient connection/read errors.
  Numeric or HTTP-date `Retry-After` is respected on 429, otherwise exponential jitter.
  Retry count is bounded; server-requested wait duration is not capped. Other 4xx fail.
- Account/document identifiers, composite keys and OData Decimal values remain strings
  (no float conversion or loss of leading zeros). `Edm.Time` remains an OData duration string.
- `/Date(milliseconds[+/-HHMM])/` becomes UTC ISO dates or date-times according to SAP's
  `sap:display-format="Date"`; offset suffixes do not alter UTC epoch milliseconds.
  ISO date-times are normalized to UTC. Nulls remain null. Invalid dates fail visibly.
- Only captured scalar properties are emitted. Navigation/deferred metadata is omitted.
  Properties are nullable to accommodate SAP's empty dates; EDMX keys remain catalog keys.

## Verification and limitations

Tests use public SDK discovery plus an ephemeral real HTTPS server and CLI subprocesses.
They cover Singer catalog/SCHEMA/RECORD output, keys/field counts, schema types, pagination,
query preservation, filters, date conversion, TLS verification, redirects, auth safety,
and bounded 429 retries. Tests require `openssl` to generate disposable certificates.

No incremental extraction, delta tokens, deletion capture, relationship expansion,
snapshot isolation, or resumable page state is claimed. Full scans can be expensive and
the source can change during a scan. Live SAP acceptance and downstream target/ETL testing
must be performed separately with authorized credentials; mock transport tests are not
live API acceptance.

MIT — see `LICENSE`.
