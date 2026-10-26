"""Extraction adapters: deterministic local demo and Azure Content Understanding REST hook."""
import json, os, re, time
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import requests
from pypdf import PdfReader

SYSTEM_INSTRUCTIONS = """
Extract a fleet repair invoice/work order into the exact JSON schema requested.
Include only work/items that were actually billed. Exclude recommended, declined,
quoted, proposed, or not-performed work. Preserve each billed line item separately.
Use line_type labor, part, fee, discount, or core_charge. Do not infer missing values;
use null for unknown VIN/part number/quantity/unit price where allowed. Monetary values
must be numbers. Normalize service_date to YYYY-MM-DD. Do not invent totals.
Return JSON only.
"""

def read_document(path: Path) -> str:
    if path.suffix.lower() == ".txt":
        return path.read_text(encoding="utf-8", errors="replace")
    if path.suffix.lower() == ".pdf":
        reader = PdfReader(str(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError("Supported file types are .txt and .pdf")

def extract_with_azure(path: Path) -> dict[str, Any]:
    """Azure CU integration point.

    Configure AZURE_CU_ENDPOINT, AZURE_CU_API_KEY, and AZURE_CU_ANALYZER_ID.
    Azure Content Understanding APIs evolve; this adapter uses the analyzeBinary
    REST pattern and surfaces service errors as a handled extraction failure.
    """
    endpoint = os.getenv("AZURE_CU_ENDPOINT", "").rstrip("/")
    key = os.getenv("AZURE_CU_API_KEY", "")
    analyzer_id = os.getenv("AZURE_CU_ANALYZER_ID", "")
    api_version = os.getenv("AZURE_CU_API_VERSION", "2025-11-01")
    if not all([endpoint, key, analyzer_id]):
        raise RuntimeError("Azure CU is not configured. Set endpoint, API key, and analyzer ID.")
    url = f"{endpoint}/contentunderstanding/analyzers/{analyzer_id}:analyzeBinary?api-version={api_version}"
    headers = {
        "Ocp-Apim-Subscription-Key": key,
        "Content-Type": "application/octet-stream",
    }
    with path.open("rb") as f:
        response = requests.post(
            url, headers=headers,
            data=f, timeout=90
        )
    response.raise_for_status()
    operation = response.json()
    # A 202 body already carries a placeholder result (status "Running", empty
    # contents); only return early when the analysis has actually finished.
    initial_status = str(operation.get("status", "")).lower() if isinstance(operation, dict) else ""
    if response.status_code != 202 and initial_status in {"", "succeeded"}:
        if isinstance(operation, dict) and isinstance(operation.get("result"), dict):
            return operation["result"]
        if isinstance(operation, dict) and isinstance(operation.get("contents"), list):
            return operation

    operation_url = response.headers.get("operation-location") or (
        operation.get("operationLocation") if isinstance(operation, dict) else None
    )
    if not operation_url:
        raise RuntimeError(
            "Azure accepted the analysis request but returned no result or operation URL."
        )

    endpoint_host = urlparse(endpoint).netloc.lower()
    operation_parts = urlparse(operation_url)
    if operation_parts.scheme != "https" or operation_parts.netloc.lower() != endpoint_host:
        raise RuntimeError("Azure returned an invalid analysis operation URL.")

    deadline = time.monotonic() + float(os.getenv("AZURE_CU_POLL_TIMEOUT_SECONDS", "90"))
    poll_headers = {"Ocp-Apim-Subscription-Key": key, "x-ms-diagnostics": "true"}
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for Azure Content Understanding analysis.")
        poll_response = requests.get(operation_url, headers=poll_headers, timeout=30)
        poll_response.raise_for_status()
        operation = poll_response.json()
        if not isinstance(operation, dict):
            raise RuntimeError("Azure returned an invalid analysis operation response.")

        status = str(operation.get("status", "")).lower()
        if status == "succeeded":
            result = operation.get("result")
            if isinstance(result, dict):
                return result
            raise RuntimeError("Azure analysis succeeded but returned no result object.")
        if status in {"failed", "canceled", "cancelled"}:
            error = operation.get("error")
            if isinstance(error, dict):
                message = error.get("message") or error.get("code") or "unknown service error"
            else:
                message = "unknown service error"
            raise RuntimeError(f"Azure analysis {status}: {message}")

        retry_after = poll_response.headers.get("Retry-After", "1")
        try:
            delay = max(0.1, min(float(retry_after), 10.0))
        except ValueError:
            delay = 1.0
        time.sleep(min(delay, max(0.0, deadline - time.monotonic())))

def _amount(value: str) -> float:
    return float(value.replace("$", "").replace(",", "").strip())

def _last_amount(line: str) -> float | None:
    matches = re.findall(r"(?<![\w.])-?\$?\d[\d,]*(?:\.\d{1,2})?", line)
    return _amount(matches[-1]) if matches else None

def _normalize_invoice_date(value: str) -> str:
    value = value.strip().rstrip(".,")
    for fmt in ("%Y-%m-%d", "%m-%d-%Y", "%m/%d/%Y", "%m-%d-%y", "%m/%d/%y",
                "%b %d %Y", "%B %d %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(value, fmt).date().isoformat()
        except ValueError:
            pass
    raise ValueError(f"Unsupported invoice date: {value}")

def _header_value(patterns: tuple[str, ...], text: str, default: str | None = None) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.M)
        if match:
            return match.group(1).strip()
    return default

def _invoice_header(text: str) -> dict[str, Any]:
    work_order = _header_value((
        r"INVOICE\s*#\s*([A-Z0-9-]+)",
        r"(?:Repair\s+Order|RO)\s+No\.?\s*:\s*\n?\s*([A-Z0-9-]+)",
        r"(?:Work\s+Order|WO)(?:\s+Number|\s*#|\s+No\.?)?\s*:\s*([A-Z0-9-]+)",
    ), text)
    unit = _header_value((
        r"Customer\s+Unit\s+No\.?\s*:\s*([A-Z0-9-]+)",
        r"FLEET\s*#\s*:?\s*([A-Z0-9-]+)",
        r"Unit(?:\s+Number|\s*#)?\s*:\s*([A-Z0-9-]+)",
    ), text)
    vin = _header_value((r"VIN\s*:\s*([A-HJ-NPR-Z0-9]{17})",), text)
    date_value = _header_value((
        r"RO\s+Closed\s*:\s*([A-Za-z]+\s+\d{1,2}\s+\d{4})",
        r"\bDATE\s*:\s*(\d{1,2}[-/]\d{1,2}[-/]\d{2,4})",
        r"(?:Service\s+Date|Date)\s*:\s*(\d{4}-\d{2}-\d{2})",
    ), text)
    if not work_order or not unit or not date_value:
        raise ValueError("Invoice is missing a work order/invoice number, unit number, or service date.")

    vendor_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    vendor = re.split(r"\s+-\s+SERVICE\s+DEPARTMENT\b", vendor_line, flags=re.I)[0].strip()
    vendor = re.sub(r"\s+\|.*$", "", vendor)
    if vendor.lower() in {"invoice", "repair order"} or not vendor:
        raise ValueError("Could not identify the repair vendor from the invoice header.")
    return {
        "work_order_number": work_order,
        "unit_number": unit,
        "vin": vin,
        "service_date": _normalize_invoice_date(date_value),
        "vendor_name": vendor,
    }

def _extract_greatlakes(text: str) -> dict[str, Any]:
    """Parse the multiline Great Lakes Truck Center repair-order table."""
    header = _invoice_header(text)
    lines = [line.strip() for line in text.splitlines()]
    start = next((i for i, line in enumerate(lines) if line.lower() == "extended"), None)
    if start is None:
        return _extract_greatlakes_rows(text, header, lines)

    number_re = re.compile(r"^-?\$?\d[\d,]*(?:\.\d{1,2})?$")
    items: list[dict[str, Any]] = []
    i = start + 1
    while i < len(lines):
        kind = lines[i].upper()
        if kind in {"LINE B", "MISCELLANEOUS", "SUB-TOTAL", "SUBTOTAL"}:
            break
        if kind not in {"LABOR", "PART", "PARTS", "CORE", "CORE CHARGE"}:
            i += 1
            continue
        item_type = {"LABOR": "labor", "PART": "part", "PARTS": "part",
                     "CORE": "core_charge", "CORE CHARGE": "core_charge"}[kind]
        i += 1
        if i >= len(lines):
            break
        part_number = None
        if item_type != "labor":
            part_number = lines[i] or None
            i += 1
        if i >= len(lines):
            break
        description = lines[i]
        i += 1
        values: list[float] = []
        while i < len(lines) and len(values) < 3 and number_re.fullmatch(lines[i]):
            values.append(_amount(lines[i]))
            i += 1
        if len(values) != 3:
            raise ValueError(f"Could not parse billed row: {description}")
        quantity, unit_price, amount = values
        items.append({"line_type": item_type, "part_number": part_number,
                      "description": description, "quantity": quantity,
                      "unit_price": unit_price, "amount": amount})

    # Capture billed miscellaneous credits and fees after the declined LINE B work.
    misc_start = next((i for i, line in enumerate(lines) if line.upper() == "MISCELLANEOUS"), None)
    if misc_start is not None:
        subtotal_idx = next((i for i in range(misc_start + 1, len(lines))
                             if re.match(r"SUB[- ]?TOTAL\b", lines[i], re.I)), len(lines))
        pending: list[str] = []
        for line in lines[misc_start + 1:subtotal_idx]:
            if not line:
                continue
            value = _amount(line) if number_re.fullmatch(line) else None
            if value is not None:
                description = " ".join(pending).strip()
                if description:
                    items.append({
                        "line_type": "discount" if value < 0 else "fee",
                        "part_number": None,
                        "description": description,
                        "quantity": None,
                        "unit_price": None,
                        "amount": value,
                    })
                pending = []
            else:
                pending.append(line)

    return _invoice_totals(text, header, items)

def _extract_greatlakes_rows(text: str, header: dict[str, Any], lines: list[str]) -> dict[str, Any]:
    """Parse the Great Lakes layout when each billed row is on a single text line."""
    money = r"(-?\$?\d[\d,]*(?:\.\d{1,2})?)"
    labor_re = re.compile(rf"^LABOR\s+(.+?)\s+(\d+(?:\.\d+)?)\s*hrs?\s*x\s*{money}\s*=\s*{money}$", re.I)
    part_re = re.compile(
        rf"^(PARTS?|CORE(?:\s+CHARGE)?)\s+(\S+)\s+(.+?)\s+qty\s+(\d+(?:\.\d+)?)\s*@\s*{money}\s*=\s*{money}$",
        re.I,
    )
    misc_re = re.compile(rf"^(.+?)\s*=\s*{money}$")
    items: list[dict[str, Any]] = []
    in_misc = False
    for line in lines:
        if re.match(r"SUB[- ]?TOTAL\b", line, re.I):
            break
        if line.upper() == "MISCELLANEOUS":
            in_misc = True
            continue
        if in_misc:
            match = misc_re.match(line)
            if match:
                amount = _amount(match.group(2))
                items.append({"line_type": "discount" if amount < 0 else "fee", "part_number": None,
                              "description": match.group(1).strip(), "quantity": None,
                              "unit_price": None, "amount": amount})
            continue
        if match := labor_re.match(line):
            items.append({"line_type": "labor", "part_number": None,
                          "description": match.group(1).strip(), "quantity": float(match.group(2)),
                          "unit_price": _amount(match.group(3)), "amount": _amount(match.group(4))})
        elif match := part_re.match(line):
            kind = "core_charge" if match.group(1).upper().startswith("CORE") else "part"
            items.append({"line_type": kind, "part_number": match.group(2),
                          "description": match.group(3).strip(), "quantity": float(match.group(4)),
                          "unit_price": _amount(match.group(5)), "amount": _amount(match.group(6))})
    if not items:
        raise ValueError("Could not find the Great Lakes repair-order item table.")
    return _invoice_totals(text, header, items)

def _extract_harborline(text: str) -> dict[str, Any]:
    """Parse Harborline's numbered item table (column text or one PDF cell per line)."""
    header = _invoice_header(text)
    raw_lines = text.splitlines()
    kinds = {"LABOR": "labor", "PART": "part", "PARTS": "part", "FEE": "fee",
             "DISCOUNT": "discount", "CORE": "core_charge"}
    number_re = re.compile(r"^-?\$?\d[\d,]*(?:\.\d{1,2})?$")

    def item(kind: str, part_number: str | None, description: str, values: list[float]) -> dict[str, Any]:
        if len(values) == 3:
            quantity, unit_price, amount = values
        elif len(values) == 1:
            quantity = unit_price = None
            amount = values[0]
        else:
            raise ValueError(f"Could not parse billed row: {description}")
        return {"line_type": kinds[kind], "part_number": part_number or None,
                "description": description, "quantity": quantity,
                "unit_price": unit_price, "amount": amount}

    items: list[dict[str, Any]] = []
    header_line = next((line for line in raw_lines
                        if re.search(r"\bPART #.*\bDESCRIPTION\b", line, re.I)), None)
    if header_line is not None:
        # Fixed-width text: slice the part number and description by header columns.
        part_col = header_line.upper().index("PART #")
        desc_col = header_line.upper().index("DESCRIPTION")
        row_re = re.compile(r"^\s*\d+\s+([A-Z]+)\b")
        for line in raw_lines:
            match = row_re.match(line)
            if not match or match.group(1).upper() not in kinds:
                continue
            tokens = line[desc_col:].split()
            trailing: list[str] = []
            while tokens and number_re.fullmatch(tokens[-1]) and len(trailing) < 3:
                trailing.insert(0, tokens.pop())
            values = [_amount(value) for value in trailing]
            # Descriptions can end in a number ("axle 2"); only treat three trailing
            # numbers as qty/rate/amount when they multiply out.
            if len(values) == 3 and _money_matches(values[0] * values[1], values[2]):
                description = " ".join(tokens)
            else:
                description = " ".join(tokens + trailing[:-1])
                values = values[-1:]
            items.append(item(match.group(1).upper(), line[part_col:desc_col].strip(),
                              description, values))
    else:
        lines = [line.strip() for line in raw_lines]
        start = next((i for i, line in enumerate(lines) if line.lower() == "amount"), None)
        if start is None:
            raise ValueError("Could not find the Harborline item table.")
        i = start + 1
        while i + 1 < len(lines):
            if re.match(r"SUB[- ]?TOTAL\b", lines[i], re.I):
                break
            if not (lines[i].isdigit() and lines[i + 1].upper() in kinds):
                i += 1
                continue
            kind = lines[i + 1].upper()
            i += 2
            text_cells: list[str] = []
            while i < len(lines) and lines[i] and not number_re.fullmatch(lines[i]):
                text_cells.append(lines[i])
                i += 1
            values: list[float] = []
            while (i < len(lines) and number_re.fullmatch(lines[i])
                   and not (i + 1 < len(lines) and lines[i + 1].upper() in kinds)):
                values.append(_amount(lines[i]))
                i += 1
            if len(text_cells) == 2:
                part_number, description = text_cells
            elif len(text_cells) == 1:
                part_number, description = None, text_cells[0]
            else:
                raise ValueError("Could not parse a Harborline billed row.")
            items.append(item(kind, part_number, description, values))
    if not items:
        raise ValueError("Could not find any Harborline billed line items.")
    return _invoice_totals(text, header, items)

def _money_matches(left: float, right: float) -> bool:
    return abs(round(left, 2) - round(right, 2)) < 0.005

def _extract_desert_sun(text: str) -> dict[str, Any]:
    """Parse Desert Sun's one-line-per-charge invoice layout."""
    header = _invoice_header(text)
    lines = [line.strip() for line in text.splitlines()]
    start = next((i for i, line in enumerate(lines) if "DESCRIPTION OF WORK" in line.upper()), None)
    if start is None:
        raise ValueError("Could not find the Desert Sun work-description table.")
    separators = [i for i in range(start + 1, len(lines)) if re.fullmatch(r"-{5,}", lines[i])]
    subtotal = next((i for i in range(start + 1, len(lines))
                     if re.match(r"SUB[- ]?TOTAL\b", lines[i], re.I)), len(lines))
    end = min(separators[-1] if separators else len(lines), subtotal)

    items: list[dict[str, Any]] = []
    pending_description: list[str] = []
    amount_only = re.compile(r"^-?\$?\d[\d,]*\.\d{2}$")
    for index, line in enumerate(lines[start + 1:end], start=start + 1):
        if not line or line.lower() == "charge" or re.fullmatch(r"-{5,}", line):
            continue
        if amount_only.fullmatch(line):
            if pending_description:
                items.append(_desert_sun_item(" ".join(pending_description), _amount(line)))
                pending_description = []
            continue
        next_line = lines[index + 1] if index + 1 < end else ""
        if (re.search(r"@\s*\$?\d[\d,]*\.\d{2}\s*$", line)
                and amount_only.fullmatch(next_line)):
            pending_description.append(line)
            continue
        inline_amount = re.search(r"\s+(-?\$\d[\d,]*\.\d{2})\s*$", line)
        if inline_amount:
            description = line[:inline_amount.start()].strip()
            items.append(_desert_sun_item(description, _amount(inline_amount.group(1))))
        else:
            pending_description.append(line)
    if pending_description:
        raise ValueError("A Desert Sun work description has no matching charge amount.")
    return _invoice_totals(text, header, items)

def _desert_sun_item(description: str, amount: float) -> dict[str, Any]:
    labor = re.search(r"(\d+(?:\.\d+)?)\s*(?:hrs?|hours?)\s*@\s*\$?(\d[\d,]*(?:\.\d{2})?)", description, re.I)
    part_rate = re.search(r"\b(\d+(?:\.\d+)?)\s*@\s*\$?(\d[\d,]*\.\d{2})", description)
    part_number_match = re.search(r"\bpn\s+([A-Z0-9-]+)", description, re.I)
    if labor:
        kind = "labor"
        quantity, unit_price = float(labor.group(1)), _amount(labor.group(2))
    elif part_rate:
        kind = "part"
        quantity, unit_price = float(part_rate.group(1)), _amount(part_rate.group(2))
    elif re.search(r"mobile service|shop supplies|service charge|fee", description, re.I):
        kind = "fee"
        quantity = unit_price = None
    else:
        kind = "part"
        quantity = unit_price = None
    return {
        "line_type": kind,
        "part_number": part_number_match.group(1) if part_number_match else None,
        "description": description,
        "quantity": quantity,
        "unit_price": unit_price,
        "amount": amount,
    }

def _invoice_totals(text: str, header: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any]:
    lines = [line.strip() for line in text.splitlines()]

    def line_value(index: int) -> float | None:
        line = lines[index]
        currency_amounts = re.findall(r"-?\$\s*(\d[\d,]*(?:\.\d{1,2})?)", line)
        if currency_amounts:
            return _amount(currency_amounts[-1])
        without_percentages = re.sub(r"\d+(?:\.\d+)?\s*%", "", line)
        unmarked_amounts = re.findall(r"-?\d[\d,]*(?:\.\d{1,2})?", without_percentages)
        if unmarked_amounts:
            return _amount(unmarked_amounts[-1])
        for next_line in lines[index + 1:index + 3]:
            if not next_line:
                continue
            if re.fullmatch(r"-?\$?\d[\d,]*(?:\.\d{1,2})?", next_line):
                return _amount(next_line)
            break
        return None

    def find_total(pattern: str) -> tuple[int, float]:
        for index, line in enumerate(lines):
            if re.search(pattern, line, re.I):
                value = line_value(index)
                if value is not None:
                    return index, value
        raise ValueError(f"Invoice is missing {pattern.lower()}.")

    subtotal_index, subtotal = find_total(r"\bSUB[- ]?TOTAL\b")
    total_index, total = find_total(r"\b(?:INVOICE\s+TOTAL|TOTAL\s+AMOUNT\s+DUE)\b")
    # Invoices can list several taxes (e.g. state + county); sum every tax line
    # between the subtotal and the total.
    tax_values = [line_value(index) for index in range(subtotal_index + 1, total_index)
                  if re.search(r"\bTAX\b", lines[index], re.I)]
    if not tax_values or None in tax_values:
        raise ValueError("Invoice is missing a readable tax amount.")

    return {
        **header,
        "line_items": items,
        "subtotal": subtotal,
        "tax": round(sum(tax_values), 2),
        "total": total,
    }

def local_demo_extract(text: str) -> dict[str, Any]:
    """Extract the repository's sample invoice layouts and ITEM fixture format."""
    if re.search(r"DESCRIPTION OF WORK", text, re.I) and re.search(r"DESERT SUN", text, re.I):
        return _extract_desert_sun(text)
    if re.search(r"GREAT LAKES TRUCK CENTER", text, re.I):
        return _extract_greatlakes(text)
    if re.search(r"HARBORLINE TRAILER", text, re.I):
        return _extract_harborline(text)
    if not re.search(r"^ITEM\|", text, re.M):
        raise ValueError("Unsupported invoice layout for local_demo mode.")

    def pick(pattern, default=None):
        m = re.search(pattern, text, re.I | re.M)
        return m.group(1).strip() if m else default
    vendor = pick(r"Vendor:\s*(.+)")
    wo = pick(r"(?:Work Order|WO)(?: Number| #| No\.?)?:\s*([A-Z0-9-]+)")
    unit = pick(r"Unit(?: Number| #)?:\s*([A-Z0-9-]+)")
    vin = pick(r"VIN:\s*([A-HJ-NPR-Z0-9]{17})")
    date = pick(r"(?:Service Date|Date):\s*(\d{4}-\d{2}-\d{2})")
    if not all([vendor, wo, unit, date]):
        raise ValueError("Invoice is missing a vendor, work order number, unit number, or service date.")
    line_items = []
    # Fixture line format: ITEM|type|part|description|qty|unit_price|amount
    for line in text.splitlines():
        if line.startswith("ITEM|"):
            _, typ, part, desc, qty, unit_price, amount = line.split("|", 6)
            line_items.append({
                "line_type": typ, "part_number": None if part == "NULL" else part,
                "description": desc, "quantity": None if qty == "NULL" else float(qty),
                "unit_price": None if unit_price == "NULL" else float(unit_price),
                "amount": float(amount)
            })
    subtotal = float(pick(r"Subtotal:\s*(-?\d+(?:\.\d{1,2})?)", "0"))
    tax = float(pick(r"Tax:\s*(-?\d+(?:\.\d{1,2})?)", "0"))
    total = float(pick(r"Total:\s*(-?\d+(?:\.\d{1,2})?)", str(subtotal + tax)))
    return {"work_order_number": wo, "unit_number": unit, "vin": vin,
            "service_date": date, "vendor_name": vendor, "line_items": line_items,
            "subtotal": subtotal, "tax": tax, "total": total}

def extract_document(path: Path) -> dict[str, Any]:
    mode = os.getenv("EXTRACTION_MODE", "azure").lower()
    if mode == "local_demo":
        return local_demo_extract(read_document(path))
    if mode != "azure":
        raise RuntimeError("EXTRACTION_MODE must be 'azure' or 'local_demo'.")
    return extract_with_azure(path)
