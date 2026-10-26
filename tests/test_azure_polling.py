from app import extractor


class FakeResponse:
    def __init__(self, status_code, body, headers=None):
        self.status_code = status_code
        self._body = body
        self.headers = headers or {}

    def raise_for_status(self):
        pass

    def json(self):
        return self._body


def test_azure_polls_when_202_body_contains_placeholder_result(monkeypatch, tmp_path):
    endpoint = "https://example.services.ai.azure.com"
    operation_url = f"{endpoint}/contentunderstanding/analyzerResults/op-1?api-version=2025-11-01"
    monkeypatch.setenv("AZURE_CU_ENDPOINT", endpoint)
    monkeypatch.setenv("AZURE_CU_API_KEY", "test-key")
    monkeypatch.setenv("AZURE_CU_ANALYZER_ID", "LineItemExtract")
    final_result = {"analyzerId": "LineItemExtract", "contents": [{"fields": {"tax": {}}}]}
    polls = []

    def fake_post(url, **kwargs):
        return FakeResponse(202, {
            "id": "op-1",
            "status": "Running",
            "result": {"analyzerId": "LineItemExtract", "warnings": [], "contents": []},
        }, {"operation-location": operation_url})

    def fake_get(url, **kwargs):
        polls.append(url)
        return FakeResponse(200, {"id": "op-1", "status": "Succeeded", "result": final_result})

    monkeypatch.setattr(extractor.requests, "post", fake_post)
    monkeypatch.setattr(extractor.requests, "get", fake_get)
    invoice = tmp_path / "invoice.pdf"
    invoice.write_bytes(b"%PDF")

    assert extractor.extract_with_azure(invoice) == final_result
    assert polls == [operation_url]
