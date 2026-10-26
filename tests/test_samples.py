from pathlib import Path

import pytest

from app.extractor import local_demo_extract, read_document
from app.main import reconcile
from app.schema import InvoiceExtraction

SAMPLES = Path(__file__).resolve().parents[1] / "samples"
EXPECTED_ITEMS = {"inv_07_harborline": 7, "inv_08_greatlakes": 7, "inv_09_desertsun": 6}


@pytest.mark.parametrize("stem", sorted(EXPECTED_ITEMS))
@pytest.mark.parametrize("suffix", [".txt", ".pdf"])
def test_sample_extracts_billed_items_and_reconciles(stem, suffix):
    raw = reconcile(local_demo_extract(read_document(SAMPLES / f"{stem}{suffix}")))
    result = InvoiceExtraction.model_validate(raw)
    assert len(result.line_items) == EXPECTED_ITEMS[stem]
    assert result.validation.line_items_sum_matches_subtotal is True
    assert result.validation.notes == []


@pytest.mark.parametrize("stem", sorted(EXPECTED_ITEMS))
def test_sample_txt_and_pdf_extract_identically(stem):
    txt = local_demo_extract(read_document(SAMPLES / f"{stem}.txt"))
    pdf = local_demo_extract(read_document(SAMPLES / f"{stem}.pdf"))
    assert txt == pdf


def test_harborline_sums_multiple_tax_lines():
    raw = local_demo_extract(read_document(SAMPLES / "inv_07_harborline.txt"))
    assert raw["tax"] == 39.25


def test_unsupported_layout_raises_instead_of_returning_placeholders():
    with pytest.raises(ValueError, match="Unsupported invoice layout"):
        local_demo_extract("ACME REPAIR\nInvoice 123\nTotal due $50.00")
