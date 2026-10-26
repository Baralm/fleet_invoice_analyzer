from fastapi.testclient import TestClient

from app import main


def test_extract_returns_clear_error_when_analyzer_contents_are_empty(monkeypatch):
    monkeypatch.setattr(main, "extract_document", lambda path: {"contents": []})

    response = TestClient(main.app).post(
        "/extract",
        files={"file": ("invoice.txt", b"invoice", "text/plain")},
    )

    assert response.status_code == 502
    assert response.json()["detail"] == {
        "error": "extraction_failed",
        "message": "Azure Content Understanding returned no extracted content.",
        "diagnostics": {"contents_count": 0},
    }


def test_empty_contents_error_includes_safe_analyzer_metadata():
    try:
        main.normalize_analyzer_result({
            "analyzerId": "LineItemExtract",
            "apiVersion": "2025-11-01",
            "status": "Succeeded",
            "warnings": [],
            "contents": [],
        })
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 502
        assert exc.detail["diagnostics"] == {
            "analyzerId": "LineItemExtract",
            "apiVersion": "2025-11-01",
            "status": "Succeeded",
            "contents_count": 0,
            "warnings_count": 0,
        }
    else:
        raise AssertionError("Expected an HTTP 502 for empty analyzer contents")


def test_extract_normalizes_content_understanding_field_values(monkeypatch):
    monkeypatch.setattr(
        main,
        "extract_document",
        lambda path: {
            "contents": [{
                "fields": {
                    "work_order_number": {"type": "string", "valueString": "41187"},
                    "unit_number": {"type": "string", "valueString": "3906"},
                    "vin": {"type": "string", "valueString": "3AKJGLDR1MSMB2290"},
                    "service_date": {"type": "date", "valueDate": "2026-08-14"},
                    "vendor_name": {"type": "string", "valueString": "Example Fleet"},
                    "line_items": {
                        "type": "array",
                        "valueArray": [
                            {"type": "object", "valueObject": {
                                "line_type": {"type": "string", "valueString": "LABOR"},
                                "description": {"type": "string", "valueString": "Repair"},
                                "amount": {"type": "number", "valueNumber": 100},
                            }},
                            {"type": "object", "valueObject": {
                                "line_type": {"type": "string", "valueString": "CORE"},
                                "description": {"type": "string", "valueString": "Core charge"},
                                "amount": {"type": "number", "valueNumber": 20},
                            }},
                            {"type": "object", "valueObject": {
                                "line_type": {"type": "string", "valueString": "MISC"},
                                "description": {"type": "string", "valueString": "Credit"},
                                "amount": {"type": "number", "valueNumber": -10},
                            }},
                        ],
                    },
                    "sub_total": {"type": "number", "valueNumber": 110},
                    "tax": {"type": "string", "valueString": "8.8"},
                    "total": {"type": "number", "valueNumber": 118.8},
                    "unmodeled_field": {"type": "string", "valueString": "ignored"},
                },
            }],
        },
    )

    response = TestClient(main.app).post(
        "/extract",
        files={"file": ("invoice.pdf", b"pdf", "application/pdf")},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["subtotal"] == 110
    assert [item["line_type"] for item in data["line_items"]] == [
        "labor", "core_charge", "discount"
    ]
    assert data["validation"]["line_items_sum_matches_subtotal"] is True


def test_currency_string_tax_is_converted_to_number():
    result = main.normalize_analyzer_result({
        "line_items": [{"line_type": "labor", "description": "Repair", "amount": "$1,000.00"}],
        "subtotal": 1000, "tax": "$35.78", "total": 1035.78,
    })
    assert result["tax"] == 35.78
    assert result["line_items"][0]["amount"] == 1000.0


def test_free_text_tax_returns_clear_error():
    try:
        main.normalize_analyzer_result({
            "line_items": [], "subtotal": 903.4, "total": 942.65,
            "tax": "CA STATE TAX 6.25% 33.84; LA COUNTY TAX 1.00% 5.41",
        })
    except Exception as exc:
        assert getattr(exc, "status_code", None) == 502
        assert "'tax'" in exc.detail["message"]
    else:
        raise AssertionError("Expected an HTTP 502 for non-numeric tax")


def test_excluded_items_are_reported_in_validation_notes():
    raw = main.normalize_analyzer_result({
        "work_order_number": "41187", "unit_number": "3906", "service_date": "2026-08-14",
        "vendor_name": "Example Fleet",
        "line_items": [{"line_type": "labor", "description": "Repair", "amount": 100}],
        "excluded_items": [{"description": "R&R DEF tank heater", "reason": "declined",
                            "quoted_amount": 612}],
        "subtotal": 100, "tax": 0, "total": 100,
    })
    result = main.InvoiceExtraction.model_validate(main.reconcile(raw))
    assert len(result.line_items) == 1
    assert result.validation.notes == ["Excluded unbilled item (declined): R&R DEF tank heater"]
