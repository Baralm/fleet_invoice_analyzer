# Fleet Invoice Analyzer

A FastAPI service that turns a fleet-repair invoice (`.pdf` or `.txt`) into validated JSON: header fields, **billed** line items only, totals, and a reconciliation report. Extraction is done by an Azure Content Understanding custom analyzer; arithmetic checks and schema validation are done in code.

```
POST /extract  (multipart file)
  → Azure Content Understanding analyzer (OCR + layout + GPT-5 field extraction)
  → normalize   (unwrap Azure field values, map types, coerce numbers)
  → reconcile   (Decimal math: items vs subtotal, subtotal + tax vs total)
  → validate    (strict Pydantic schema)
  → JSON response, or a structured 4xx/5xx error
```

Example response (abridged):

```json
{
  "work_order_number": "41187", "unit_number": "3906", "vin": "3AKJGLDR1MSMB2290",
  "service_date": "2026-08-14", "vendor_name": "GREAT LAKES TRUCK CENTER",
  "line_items": [
    {"line_type": "labor", "part_number": null, "description": "Diagnose DEF system",
     "quantity": 1.0, "unit_price": 158.0, "amount": 158.0},
    {"line_type": "core_charge", "part_number": "A0001421589C", "description": "DEF pump core charge",
     "quantity": 1.0, "unit_price": 150.0, "amount": 150.0}
  ],
  "subtotal": 1795.43, "tax": 76.87, "total": 1872.3,
  "validation": {
    "line_items_sum_matches_subtotal": true,
    "notes": ["Excluded unbilled item (declined): R&R DEF tank heater, parts & labor"]
  }
}
```

---

## 1. How to run it

### Setup

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
```

### Option A: offline (no Azure account needed)

Leave `EXTRACTION_MODE=local_demo` in `.env`. The offline parsers handle the three provided sample layouts only (see [Known limitations](#4-known-limitations-and-next-steps)).

```bash
uvicorn app.main:app --reload
```

### Option B: Azure Content Understanding

1. In `.env`, set `EXTRACTION_MODE=azure` and fill in `AZURE_CU_ENDPOINT` and `AZURE_CU_API_KEY` from your Foundry resource. The resource needs a GPT-5 and a `text-embedding-3-large` deployment set as its Content Understanding defaults.
2. Create the analyzer from the version-controlled definition in [azure/analyzer_definition.json](azure/analyzer_definition.json):
   ```bash
   python scripts/create_analyzer.py LineItemExtract             # new analyzer
   python scripts/create_analyzer.py LineItemExtract --replace   # overwrite an existing one
   ```
3. Start the server: `uvicorn app.main:app --reload`

### Try it

Open http://127.0.0.1:8000/docs and use **POST /extract → Try it out**, or:

```bash
curl -X POST http://127.0.0.1:8000/extract -F "file=@samples/inv_08_greatlakes.pdf"
```

### Tests and evaluation

```bash
pytest -q                                      # 20 unit tests, no network
python scripts/evaluate.py --mode azure        # scores the analyzer (1 Azure call per file)
python scripts/evaluate.py --mode local_demo   # scores the offline parsers
```

### Configuration (`.env`)

| Variable | Default | Purpose |
|---|---|---|
| `EXTRACTION_MODE` | `azure` | `azure` or `local_demo` |
| `AZURE_CU_ENDPOINT`, `AZURE_CU_API_KEY` | none | Foundry resource endpoint and key |
| `AZURE_CU_ANALYZER_ID` | none | Analyzer to call, e.g. `LineItemExtract` |
| `AZURE_CU_API_VERSION` | `2025-11-01` | Content Understanding GA API |
| `AZURE_CU_POLL_TIMEOUT_SECONDS` | `90` | Maximum wait for an analysis to finish |
| `MAX_UPLOAD_BYTES` | `20000000` | Upload size limit (HTTP 413 above it) |
| `DEBUG_ERRORS` | `false` | Include internal error messages in 502 responses |

### Project layout

| Path | What it does |
|---|---|
| [app/main.py](app/main.py) | API endpoint, Azure result normalization, reconciliation |
| [app/extractor.py](app/extractor.py) | Azure adapter (submit + poll) and offline `local_demo` parsers |
| [app/schema.py](app/schema.py) | Pydantic output schema |
| [azure/analyzer_definition.json](azure/analyzer_definition.json) | Analyzer field schema (the extraction instructions) |
| [scripts/evaluate.py](scripts/evaluate.py) | Scores extraction against [samples/labels.json](samples/labels.json) |
| [scripts/create_analyzer.py](scripts/create_analyzer.py) | Creates or replaces the analyzer in Azure |
| [scripts/make_sample_pdfs.py](scripts/make_sample_pdfs.py) | Renders the created `.txt` invoices as PDFs |
| [tests/](tests/) | Unit tests; Azure calls are mocked |

---

## 2. Design overview and key decisions

**1. A custom Azure Content Understanding analyzer whose field schema acts as the prompt.** Content Understanding runs OCR and layout analysis, then has GPT-5 fill a typed field schema. There is no separate system prompt: each field's `description` is the instruction. Writing these precisely did most of the work. For example, `line_items` lists exactly which kinds of unbilled work to exclude; `tax` is a `number` defined as "the sum of all tax lines"; and `vendor_name` says "never the customer or bill-to company". The definition lives in source control and is deployed by script, so the analyzer can be reproduced and reviewed like code.
- *Rejected: `prebuilt-invoice`.* Its schema is fixed. It has no `labor`/`part`/`core_charge`/`fee` categories and no notion of declined versus billed work.
- *Rejected: calling an LLM directly with a prompt.* I would have to build OCR, layout handling, and output grounding myself; Content Understanding provides all three.
- *Rejected: regex/template parsing as the main path.* Every shop's layout is different. The three offline parsers in this repo show how brittle and layout-specific that approach is.

**2. "Billed only" is enforced in three layers, not trusted to the model alone.**
1. The `line_items` description tells the model to exclude recommended, declined, quoted, estimated, deferred, and "next visit" work, *even when a price is shown*.
2. A separate `excluded_items` field gives the model somewhere to put that work. This makes it more reliable at keeping such work out of `line_items`. The API reports each exclusion in `validation.notes`, so it is auditable rather than silent.
3. Reconciliation is a backstop: if declined work leaks into `line_items`, the items no longer sum to the subtotal and the response is flagged.

**3. Arithmetic is done in code, and mismatches are flagged, never fixed.** `reconcile()` uses `Decimal` with cent rounding to check that line items equal the subtotal and that subtotal plus tax equals the total. Mismatches become notes and the invoice's stated figures are left untouched, because someone paying invoices needs to see the discrepancy. *Rejected:* asking the model to validate (the original analyzer had a `validation` field; LLM arithmetic is unreliable and can't be audited), and silently "correcting" totals.

**4. Model output is treated as untrusted input.** The normalization layer:
- unwraps Azure's `{"type": ..., "valueNumber": ...}` wrappers
- maps type aliases (`PARTS` → `part`, `CORE` → `core_charge`)
- converts currency strings (`"$35.78"`) to numbers
- rejects free text where a number belongs, with a clear message naming the field

A strict Pydantic schema (`extra="forbid"`, a `Literal` type for `line_type`, ISO dates) then validates the result. Every failure becomes a structured error rather than a crash:

| Status | Meaning |
|---|---|
| 422 | Output didn't match the schema |
| 502 | Upstream failure, empty or invalid content, or a non-numeric field |
| 413 / 415 / 400 | Upload too large, wrong file type, or empty file |

Internal messages are hidden unless `DEBUG_ERRORS=true`, so keys and invoice contents don't leak.

**5. The Azure adapter is asynchronous-safe and checks the polling URL.** Content Understanding answers `202 Accepted` with a placeholder result (`status: "Running"`, empty `contents`). The adapter polls the `operation-location` URL until `Succeeded`, honoring `Retry-After`, with a timeout. Before polling, it checks that the URL is HTTPS on the configured endpoint's host, so the API key is never sent anywhere else.

**6. An offline adapter, used for tests rather than as a fallback.** `EXTRACTION_MODE=local_demo` runs deterministic parsers for the three provided layouts, so the pipeline can be developed and tested without network calls or cost. For any other layout it raises an error instead of returning placeholder data.

---

## 3. How I evaluated it, with results

### Evaluation set

Six invoices, each as `.txt` and as a text-based `.pdf` (12 documents). The expected output for each is hand-labelled in [samples/labels.json](samples/labels.json).

| Invoice | Source | What it tests |
|---|---|---|
| `inv_07_harborline` | provided | Numbered table, sublet fee without qty/rate, two tax lines, recommendation after the totals |
| `inv_08_greatlakes` | provided | Complaint/cause/correction layout, core charge, goodwill credit, declined repair with a quoted price |
| `inv_09_desertsun` | provided | One-line charges with inline rates, deposit and balance due after the total, next-visit note |
| `inv_10_pinecrest` | created | Declined and recommended rows **with prices** mixed between billed rows, core deposit, labor discount, zero tax |
| `inv_11_ironbridge` | created | Stated subtotal is $25.00 higher than the billed lines; reconciliation must flag it, not fix it |
| `inv_12_northstar` | created | Two-page invoice with items split across pages, state + city tax, deposit, recommendation on the last page |

The created invoices use fictional vendors. Their PDFs are generated with `python scripts/make_sample_pdfs.py samples/inv_1*.txt`.

### Method

[scripts/evaluate.py](scripts/evaluate.py) sends every document through the real `POST /extract` endpoint, so normalization, reconciliation, validation, and error handling are all exercised. It scores:
- **Schema validity:** HTTP 200 with a valid `InvoiceExtraction`.
- **Line-item precision, recall and F1:** each item is matched on `(line_type, amount)`. `amount_only_f1` ignores the type.
- **Header accuracy** (work order, unit, service date, vendor) and **totals accuracy** (subtotal, tax, total).
- **Reconciliation flag accuracy:** `line_items_sum_matches_subtotal` must equal the label. It must be `false` for Ironbridge.
- **Unbilled items leaked:** extracted items whose description matches the labelled recommended or declined work.

Per-document results are saved to `evaluation/results_<mode>.json`.

### Results (2026-10-26, analyzer `LineItemExtract`, API `2025-11-01`, GPT-5)

| Metric | Azure (12 docs) | local_demo (12 docs) |
|---|---|---|
| Schema-valid rate | **1.00** | 0.50 |
| Line-item F1 (type + amount) | **1.00** | 0.63 |
| Header field accuracy | **1.00** | 0.50 |
| Totals accuracy | **1.00** | 0.50 |
| Reconciliation flag accuracy | **1.00** | 0.50 |
| Unbilled items leaked | **0** | 0 |

- **Azure** extracted every billed line on all 12 documents, including items split across Northstar's two pages. It excluded all 6 pieces of unbilled work and listed them, for example `Excluded unbilled item (declined): R&R both front shock absorbers`. Ironbridge was flagged with `Line item amounts sum to 947.50, but invoice subtotal is 972.50.` and its stated subtotal was left as printed.
- **local_demo** is perfect on the three provided layouts and refuses the three new ones with `Unsupported invoice layout` (HTTP 502), as designed.

### What the evaluation caught during development

The first Azure run returned **empty content for every document**. The adapter treated Azure's `202 Running` placeholder as the final result and never polled; this is now fixed and covered by `tests/test_azure_polling.py`. The first analyzer schema also had `tax` typed as `string`, so the model returned text like `"CA STATE TAX 6.25% 33.84; LA COUNTY TAX 1.00% 5.41"`. Two field descriptions had been pasted onto the wrong fields, so the customer name appeared as `vendor_name`. Fixing the schema took the provided samples from 2 of 6 to 6 of 6 correct.

### Caveats

Twelve clean, digitally generated documents is a small set, and perfect scores on it don't show production accuracy. LLM output can vary between runs; I have not yet measured run-to-run variance. Scanned or photographed pages, handwriting, and real shop paperwork are not covered.

---

## 4. Known limitations and next steps

**Limitations**
- **Latency is too high for a blocking HTTP call on PDFs.** A request takes 7–40 s (see section 5) while the client waits on an open connection.
- **Billed-only filtering depends on the LLM following instructions.** It worked on every test case, and reconciliation catches most leaks. But a declined item whose amount happens to be counted in the subtotal would not be caught.
- **Ambiguous line types.** For example, a flat roadside call-out could reasonably be `labor` or `fee`. The schema forces one choice with no confidence attached.
- **Small minor issues.** `vendor_name` sometimes includes a department suffix ("… - SERVICE DEPARTMENT").
- **Offline parsers** support only the three provided layouts.
- **Not production-hardened.** There is no authentication, rate limiting, structured logging, request tracing, duplicate-invoice detection, or currency handling.
- **Exact-cent reconciliation** with no tolerance, and no rule for tax charged on parts only.

**With more time, in priority order**
1. **Expand the evaluation set** with 20–50 real, hand-labelled invoices, including scanned and phone-photo pages. Run each 3× to measure variance, and track per-field accuracy over time in CI.
2. **Make `/extract` asynchronous** (return a job ID, then poll or use a webhook) so slow PDFs don't hold connections open.
3. **Route uncertain results to a human.** Turn on Content Understanding confidence scores and source grounding, and send low-confidence or unreconciled invoices to a review queue instead of auto-accepting them.
4. **Cut cost and latency.** Compare `gpt-5-mini` against `gpt-5` on the evaluation set; output tokens are most of the per-invoice cost.
5. Add business rules for reconciliation tolerance, parts-only tax, missing totals, and negative-amount handling. Add duplicate detection and idempotency keys.
6. Add authentication, rate limits, structured logs (without invoice contents), and monitoring of latency, error rate, and reconciliation-failure rate.

---

## 5. Rough cost and latency per request

### Latency (measured, Azure mode, 12 documents)

| Input | Median | Range |
|---|---|---|
| `.txt` (digital text) | ~7 s | 6.7–8.4 s |
| `.pdf` (1–2 pages) | ~29 s | 18–40 s |

PDFs are slower because they go through OCR and layout analysis before field extraction. Offline `local_demo` mode takes under 100 ms.

### Cost (estimated from Azure's reported usage)

Each Content Understanding response includes a `usage` object. For the 2-page Northstar PDF it reported 2 layout pages, 2,000 contextualization tokens, 10,923 GPT-5 input tokens, and 2,125 GPT-5 output tokens.

| Component | Usage | Rate (see note) | Cost |
|---|---|---|---|
| Content extraction (layout) | 2 pages | $5 / 1,000 pages | $0.010 |
| Contextualization | 2,000 tokens | $1 / 1M tokens | $0.002 |
| GPT-5 input | 10,923 tokens | $1.25 / 1M | $0.014 |
| GPT-5 output | 2,125 tokens | $10 / 1M | $0.021 |
| **Total** | | | **≈ $0.05 per 2-page invoice** (≈ $0.02–0.03 per page) |

- Repeat runs of the same document hit the prompt cache ($0.125 / 1M input), which brings this to about $0.035. New invoices won't benefit, so ≈ $0.05 is the realistic figure.
- `.txt` files use the cheaper "minimal" extraction meter instead of layout.
- At 10,000 invoices a month this is roughly **$500/month**. Output tokens are the biggest share, so trying `gpt-5-mini` is the first cost lever.

*Rates:* the extraction and contextualization rates are the ones used in [Microsoft's pricing explainer](https://learn.microsoft.com/en-us/azure/ai-services/content-understanding/pricing-explainer) (updated 2026-10). The GPT-5 Global Standard rates are from third-party Azure price listings ([azurespeed.com](https://www.azurespeed.com/AzureAiModelPricing/Models/openai-gpt-5)). Rates vary by region and deployment type; check the official [Content Understanding](https://azure.microsoft.com/pricing/details/content-understanding/) and [Azure OpenAI](https://azure.microsoft.com/pricing/details/cognitive-services/openai-service/) pricing pages before budgeting.

---

## 6. How I used AI coding tools

The initial scaffold (FastAPI app, schema, Azure adapter, first tests) was drafted with AI assistance. I then used **Claude Code** (Anthropic's coding agent, running in VS Code) as a pair programmer to debug and finish the project:

- **Diagnosis.** It ran the tests and evaluation, found that `evaluate.py` referenced sample files that no longer existed, and found that the offline parser silently returned placeholder data (`UNKNOWN`, `2000-01-01`, $0) for unknown layouts. It then queried the Azure resource directly and traced the "empty contents" failure to the adapter returning the `202 Running` placeholder instead of polling.
- **Implementation.**
  - the polling fix
  - number coercion and clearer errors
  - the Harborline parser and the Great Lakes `.txt` parser
  - summing multiple tax lines
  - the corrected analyzer schema with `excluded_items`
  - the analyzer deployment script
  - the three created invoices and the PDF generator
  - the labelled evaluation harness
  - tests for each fix
- **Verification.** Every change was checked by running `pytest`, the evaluation in both modes, and live calls against the Azure analyzer. The results in section 3 come from those runs, not from the tool's claims.

**What I checked myself:** the arithmetic and ground-truth labels for each invoice, the analyzer field descriptions, and the decision to replace the analyzer in place (the previous definition is backed up in `azure/LineItemExtract_backup.json`).

**Where AI assistance needed correcting:** an early test run accidentally sent sample invoices to an already-running Azure-mode server instead of the intended offline one. The analyzer schema built earlier in the Foundry portal contained copy-paste errors in its descriptions. Both were caught by checking actual outputs rather than trusting them. No production credentials or real customer invoices are in this repository.
