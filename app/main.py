import json, os, re, tempfile
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from fastapi import FastAPI, UploadFile, File, HTTPException
from pydantic import ValidationError
from dotenv import load_dotenv
from .schema import InvoiceExtraction
from .extractor import extract_document

load_dotenv()
app = FastAPI(title="Fleet Invoice Analyzer", version="0.1.0")

def money(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

def reconcile(raw: dict) -> dict:
    """Normalize schema, recompute arithmetic checks, and preserve mismatch notes."""
    notes = list((raw.get("validation") or {}).get("notes", []))
    items = raw.get("line_items", [])
    item_sum = sum((money(item["amount"]) for item in items), Decimal("0.00"))
    subtotal = money(raw["subtotal"])
    tax = money(raw["tax"])
    total = money(raw["total"])
    matches = item_sum == subtotal
    if not matches:
        notes.append(f"Line item amounts sum to {item_sum:.2f}, but invoice subtotal is {subtotal:.2f}.")
    if money(subtotal + tax) != total:
        notes.append(f"Subtotal plus tax is {money(subtotal + tax):.2f}, but invoice total is {total:.2f}.")
    raw["subtotal"], raw["tax"], raw["total"] = float(subtotal), float(tax), float(total)
    raw["validation"] = {"line_items_sum_matches_subtotal": matches, "notes": notes}
    return raw

def _unwrap_analyzer_value(value):
    if isinstance(value, list):
        return [_unwrap_analyzer_value(item) for item in value]
    if not isinstance(value, dict):
        return value

    for key in ("valueObject", "valueArray"):
        if key in value:
            return _unwrap_analyzer_value(value[key])
    for key in (
        "valueString",
        "valueNumber",
        "valueInteger",
        "valueBoolean",
        "valueDate",
        "valueTime",
    ):
        if key in value:
            return value[key]
    if "type" in value:
        return None
    return {key: _unwrap_analyzer_value(item) for key, item in value.items()}

def _to_number(field: str, value, required: bool = False):
    """Accept analyzer numbers and simple currency strings such as "$1,234.50"."""
    if value is None and not required:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    if isinstance(value, str):
        cleaned = value.strip().replace("$", "").replace(",", "")
        if re.fullmatch(r"-?\d+(?:\.\d+)?", cleaned):
            return float(cleaned)
    raise HTTPException(status_code=502, detail={
        "error": "extraction_failed",
        "message": f"Azure returned a missing or non-numeric value for '{field}'. "
                   "Check that this analyzer field has type 'number'.",
    })

def normalize_analyzer_result(raw: dict) -> dict:
    """Convert Azure Content Understanding field values into the API schema."""
    if isinstance(raw.get("contents"), list):
        if not raw["contents"]:
            diagnostics = {
                key: raw[key]
                for key in ("analyzerId", "apiVersion", "status")
                if isinstance(raw.get(key), (str, int, float, bool))
            }
            diagnostics["contents_count"] = 0
            if isinstance(raw.get("warnings"), list):
                diagnostics["warnings_count"] = len(raw["warnings"])
            raise HTTPException(status_code=502, detail={
                "error": "extraction_failed",
                "message": "Azure Content Understanding returned no extracted content.",
                "diagnostics": diagnostics,
            })
        content = raw["contents"][0]
        raw = content.get("fields", content)

    if not isinstance(raw, dict):
        raise HTTPException(status_code=502, detail={
            "error": "extraction_failed",
            "message": "Azure Content Understanding returned an invalid result."
        })

    fields = _unwrap_analyzer_value(raw)
    fields.setdefault("vin", None)
    if "subtotal" not in fields and "sub_total" in fields:
        fields["subtotal"] = fields.pop("sub_total")
    type_names = {
        "labor": "labor",
        "part": "part",
        "parts": "part",
        "fee": "fee",
        "discount": "discount",
        "core": "core_charge",
        "core_charge": "core_charge",
    }
    normalized_items = []
    for item in fields.get("line_items", []):
        if not isinstance(item, dict):
            continue
        line_type = str(item.get("line_type", "")).strip().lower()
        if line_type == "misc":
            line_type = "discount" if item.get("amount", 0) < 0 else "fee"
        item["line_type"] = type_names.get(line_type, line_type)
        for nullable_field in ("part_number", "quantity", "unit_price"):
            item.setdefault(nullable_field, None)
        if item.get("part_number") == "":
            item["part_number"] = None
        for numeric_field in ("quantity", "unit_price"):
            item[numeric_field] = _to_number(f"line_items.{numeric_field}", item[numeric_field])
        item["amount"] = _to_number("line_items.amount", item.get("amount"), required=True)
        normalized_items.append({
            key: item[key]
            for key in (
                "line_type", "part_number", "description", "quantity", "unit_price", "amount"
            )
            if key in item
        })
    fields["line_items"] = normalized_items
    fields.pop("validation", None)
    # Record unbilled (recommended/declined) work the analyzer set aside, so the
    # exclusion is visible in the response rather than silent.
    excluded_notes = [
        f"Excluded unbilled item ({item.get('reason') or 'not billed'}): {item.get('description')}"
        for item in fields.get("excluded_items") or []
        if isinstance(item, dict) and item.get("description")
    ]
    if excluded_notes:
        fields["validation"] = {"notes": excluded_notes}
    for numeric_field in ("subtotal", "tax", "total"):
        fields[numeric_field] = _to_number(numeric_field, fields.get(numeric_field), required=True)

    allowed_fields = {
        "work_order_number",
        "unit_number",
        "vin",
        "service_date",
        "vendor_name",
        "line_items",
        "subtotal",
        "tax",
        "total",
        "validation",
    }
    return {key: value for key, value in fields.items() if key in allowed_fields}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/extract", response_model=InvoiceExtraction)
async def extract_invoice(file: UploadFile = File(...)):
    name = file.filename or "document"
    suffix = Path(name).suffix.lower()
    if suffix not in {".pdf", ".txt"}:
        raise HTTPException(status_code=415, detail="Only PDF and TXT documents are supported.")
    data = await file.read()
    if not data:
        raise HTTPException(status_code=400, detail="Uploaded document is empty.")
    if len(data) > int(os.getenv("MAX_UPLOAD_BYTES", "20000000")):
        raise HTTPException(status_code=413, detail="Document exceeds configured upload limit.")
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(data)
            temp_path = Path(tmp.name)
        raw = normalize_analyzer_result(extract_document(temp_path))
        raw = reconcile(raw)
        return InvoiceExtraction.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "extraction_schema_validation_failed",
            "message": "The extracted result did not match the required schema.",
            "issues": exc.errors()
        })
    except HTTPException:
        raise
    except Exception as exc:
        # Avoid leaking keys, full document contents, or raw upstream response.
        raise HTTPException(status_code=502, detail={
            "error": "extraction_failed",
            "message": str(exc) if os.getenv("DEBUG_ERRORS", "false").lower() == "true"
                       else "Extraction failed. Check service configuration and logs."
        })
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
