"""Evaluate extraction against hand-labelled ground truth in samples/labels.json.

Usage: python scripts/evaluate.py [--mode local_demo|azure]

Every sample (.txt and .pdf) is sent through the real POST /extract endpoint, so
schema validation, error handling, and reconciliation are exercised exactly as
in production. Results are printed and saved to evaluation/results_<mode>.json.
"""
from collections import Counter
from pathlib import Path
import argparse, json, os, sys, time, warnings

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument("--mode", choices=["local_demo", "azure"], default="local_demo")
args = parser.parse_args()
os.environ["EXTRACTION_MODE"] = args.mode  # set before load_dotenv so .env does not override

warnings.filterwarnings("ignore")
from fastapi.testclient import TestClient
from app.main import app

labels = {k: v for k, v in json.loads((ROOT / "samples" / "labels.json").read_text()).items()
          if not k.startswith("_")}
files = sorted(p for p in (ROOT / "samples").iterdir()
               if p.suffix.lower() in {".txt", ".pdf"} and p.stem in labels)
client = TestClient(app)


def close(a, b) -> bool:
    return a is not None and abs(float(a) - float(b)) < 0.005


def score(path: Path) -> dict:
    gold = labels[path.stem]
    started = time.perf_counter()
    response = client.post("/extract", files={"file": (path.name, path.read_bytes())})
    row = {"file": path.name, "source": gold["source"], "http_status": response.status_code,
           "seconds": round(time.perf_counter() - started, 2), "expected_items": len(gold["line_items"])}
    if response.status_code != 200:
        detail = response.json().get("detail")
        row.update(schema_valid=False, extracted_items=0, typed_matches=0, amount_matches=0,
                   header_correct=0, totals_correct=0, reconciliation_flag_correct=False,
                   exclusion_leaks=[], error=detail.get("message") if isinstance(detail, dict) else detail)
        return row

    data = response.json()
    items = data["line_items"]
    want_typed = Counter((t, round(a, 2)) for t, a in gold["line_items"])
    got_typed = Counter((i["line_type"], round(i["amount"], 2)) for i in items)
    want_amounts = Counter(round(a, 2) for _, a in gold["line_items"])
    got_amounts = Counter(round(i["amount"], 2) for i in items)
    leaks = [i["description"] for i in items
             if any(k.lower() in i["description"].lower() for k in gold["excluded_keywords"])]
    header_checks = [
        data["work_order_number"] == gold["work_order_number"],
        data["unit_number"] == gold["unit_number"],
        data["service_date"] == gold["service_date"],
        gold["vendor_name"].lower() in data["vendor_name"].lower(),
    ]
    totals_checks = [close(data[k], gold[k]) for k in ("subtotal", "tax", "total")]
    row.update(
        schema_valid=True,
        extracted_items=len(items),
        typed_matches=sum((want_typed & got_typed).values()),
        amount_matches=sum((want_amounts & got_amounts).values()),
        header_correct=sum(header_checks),
        totals_correct=sum(totals_checks),
        reconciliation_flag_correct=(
            data["validation"]["line_items_sum_matches_subtotal"] == gold["expect_sum_matches"]),
        exclusion_leaks=leaks,
        notes=data["validation"]["notes"],
        missing=sorted((want_typed - got_typed).elements()),
        unexpected=sorted((got_typed - want_typed).elements()),
        error=None,
    )
    return row


def f1(tp: int, extracted: int, expected: int) -> tuple[float, float, float]:
    precision = tp / extracted if extracted else 0.0
    recall = tp / expected if expected else 0.0
    f = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return round(precision, 3), round(recall, 3), round(f, 3)


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    extracted = sum(r["extracted_items"] for r in rows)
    expected = sum(r["expected_items"] for r in rows)
    precision, recall, typed_f1 = f1(sum(r["typed_matches"] for r in rows), extracted, expected)
    return {
        "documents": n,
        "schema_valid_rate": round(sum(r["schema_valid"] for r in rows) / n, 3),
        "line_item_precision": precision,
        "line_item_recall": recall,
        "line_item_f1": typed_f1,
        "amount_only_f1": f1(sum(r["amount_matches"] for r in rows), extracted, expected)[2],
        "header_field_accuracy": round(sum(r["header_correct"] for r in rows) / (4 * n), 3),
        "totals_accuracy": round(sum(r["totals_correct"] for r in rows) / (3 * n), 3),
        "reconciliation_flag_accuracy": round(sum(r["reconciliation_flag_correct"] for r in rows) / n, 3),
        "unbilled_items_leaked": sum(len(r["exclusion_leaks"]) for r in rows),
        "mean_seconds_per_document": round(sum(r["seconds"] for r in rows) / n, 2),
    }


rows = [score(path) for path in files]
print(f"Mode: {args.mode}\n")
print(f"{'file':26} {'http':>4} {'items':>7} {'typed':>6} {'header':>6} {'totals':>6} {'recon':>5} {'leaks':>5}  error")
for r in rows:
    print(f"{r['file']:26} {r['http_status']:>4} {r['extracted_items']:>3}/{r['expected_items']:<3} "
          f"{r['typed_matches']:>6} {r['header_correct']:>4}/4 {r['totals_correct']:>4}/3 "
          f"{'ok' if r['reconciliation_flag_correct'] else 'BAD':>5} {len(r['exclusion_leaks']):>5}  {r['error'] or ''}")
    for label in ("missing", "unexpected"):
        if r.get(label):
            print(f"{'':28}{label}: {r[label]}")

summary = {"all": summarize(rows)}
for source in ("provided", "created"):
    subset = [r for r in rows if r["source"] == source]
    if subset:
        summary[source] = summarize(subset)
print("\nAggregate metrics")
print(json.dumps(summary, indent=2))

out_dir = ROOT / "evaluation"
out_dir.mkdir(exist_ok=True)
(out_dir / f"results_{args.mode}.json").write_text(
    json.dumps({"mode": args.mode, "summary": summary, "documents": rows}, indent=2))
print(f"\nSaved evaluation/results_{args.mode}.json")
