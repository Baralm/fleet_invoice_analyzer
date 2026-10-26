"""Create an Azure Content Understanding analyzer from azure/analyzer_definition.json.

Usage: python scripts/create_analyzer.py LineItemExtractV2
       python scripts/create_analyzer.py LineItemExtract --replace

Without --replace, Azure refuses to overwrite an existing analyzer ID. With
--replace, the existing analyzer's definition is replaced in place; back it up
first. After it succeeds, make sure AZURE_CU_ANALYZER_ID in .env matches.
"""
from pathlib import Path
import json, os, sys, time
import requests
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")

args = sys.argv[1:]
replace = "--replace" in args
args = [arg for arg in args if arg != "--replace"]
if len(args) != 1:
    sys.exit("Usage: python scripts/create_analyzer.py <analyzer-id> [--replace]")
analyzer_id = args[0]
endpoint = os.environ["AZURE_CU_ENDPOINT"].rstrip("/")
api_version = os.getenv("AZURE_CU_API_VERSION", "2025-11-01")
headers = {"Ocp-Apim-Subscription-Key": os.environ["AZURE_CU_API_KEY"]}
definition = json.loads((ROOT / "azure" / "analyzer_definition.json").read_text())

response = requests.put(
    f"{endpoint}/contentunderstanding/analyzers/{analyzer_id}?api-version={api_version}"
    + ("&allowReplace=true" if replace else ""),
    headers={**headers, "Content-Type": "application/json"},
    json=definition, timeout=60,
)
if response.status_code >= 400:
    sys.exit(f"Create failed: HTTP {response.status_code}\n{response.text}")

operation_url = response.headers.get("operation-location")
deadline = time.monotonic() + 300
while operation_url and time.monotonic() < deadline:
    status = requests.get(operation_url, headers=headers, timeout=30).json()
    state = str(status.get("status", "")).lower()
    if state == "succeeded":
        break
    if state in {"failed", "canceled", "cancelled"}:
        sys.exit(f"Create {state}: {json.dumps(status.get('error'), indent=1)}")
    time.sleep(2)
else:
    if operation_url:
        sys.exit("Timed out waiting for analyzer creation.")

print(f"Analyzer '{analyzer_id}' is ready. Set AZURE_CU_ANALYZER_ID={analyzer_id} in .env.")
