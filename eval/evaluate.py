#!/usr/bin/env python3
"""
Evaluation harness for the Northwind expense review system.

Usage:
  python eval/evaluate.py expected.json --base-url http://127.0.0.1:8000

Expected JSON shape:
{
  "submissions": [
    {
      "name": "heldout_case",
      "employee": {
        "name": "Avery Stone",
        "grade": 5,
        "title": "Manager",
        "department": "Ops",
        "home_base": "Irvine, CA",
        "trip_purpose": "Client visit",
        "trip_dates": "2025-07-01 to 2025-07-03"
      },
      "receipts": ["path/to/receipt.pdf", "path/to/receipt.txt"],
      "expected_items": [
        {"filename_contains": "dinner", "verdict": "flagged", "category": "meal", "must_cite": "TEP-002"}
      ]
    }
  ],
  "questions": [
    {"question": "When is premium economy allowed?", "should_refuse": false, "must_cite": "TEP-005"},
    {"question": "What is the weather in Austin?", "should_refuse": true}
  ]
}
"""
import argparse
import json
import mimetypes
import os
import urllib.error
import urllib.request
from pathlib import Path


def request_json(method, url, payload=None, headers=None):
    data = None
    headers = headers or {}
    if payload is not None:
        data = json.dumps(payload).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode())


def multipart(fields, files):
    boundary = "----northwind-eval-boundary"
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    for name, path in files:
        path = Path(path)
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"; filename=\"{path.name}\"\r\nContent-Type: {ctype}\r\n\r\n".encode()
        )
        chunks.append(path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def post_multipart(url, fields, files):
    body, headers = multipart(fields, files)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def cite_labels(item_or_answer):
    labels = []
    for citation in item_or_answer.get("citations", []):
        labels.append(citation.get("label", ""))
    return " ".join(labels)


def evaluate(expected, base_url):
    results = {
        "item_total": 0,
        "verdict_correct": 0,
        "category_correct": 0,
        "citation_correct": 0,
        "qa_total": 0,
        "qa_refusal_correct": 0,
        "qa_citation_correct": 0,
        "failures": [],
    }

    for case in expected.get("submissions", []):
        emp_payload = case["employee"]
        emp = request_json("POST", f"{base_url}/api/employees", emp_payload)
        fields = {
            "employee_id": emp["id"],
            "purpose": emp_payload.get("trip_purpose", ""),
            "trip_dates": emp_payload.get("trip_dates", ""),
        }
        sub = post_multipart(f"{base_url}/api/submissions", fields, [("receipts", p) for p in case.get("receipts", [])])
        for exp in case.get("expected_items", []):
            results["item_total"] += 1
            match = next((i for i in sub["items"] if exp.get("filename_contains", "").lower() in i["filename"].lower()), None)
            if not match:
                results["failures"].append(f"{case.get('name')}: no item matching {exp.get('filename_contains')}")
                continue
            effective = match.get("effective_verdict") or match.get("verdict")
            if not exp.get("verdict") or effective == exp["verdict"]:
                results["verdict_correct"] += 1
            else:
                results["failures"].append(f"{match['filename']}: expected verdict {exp['verdict']}, got {effective}")
            if not exp.get("category") or match.get("category") == exp["category"]:
                results["category_correct"] += 1
            else:
                results["failures"].append(f"{match['filename']}: expected category {exp['category']}, got {match.get('category')}")
            if not exp.get("must_cite") or exp["must_cite"] in cite_labels(match):
                results["citation_correct"] += 1
            else:
                results["failures"].append(f"{match['filename']}: missing citation {exp['must_cite']}")

    for q in expected.get("questions", []):
        results["qa_total"] += 1
        ans = request_json("POST", f"{base_url}/api/ask", {"question": q["question"]})
        if bool(ans.get("refused")) == bool(q.get("should_refuse")):
            results["qa_refusal_correct"] += 1
        else:
            results["failures"].append(f"question {q['question']!r}: refusal expected {q.get('should_refuse')}, got {ans.get('refused')}")
        if q.get("must_cite"):
            if q["must_cite"] in cite_labels(ans):
                results["qa_citation_correct"] += 1
            else:
                results["failures"].append(f"question {q['question']!r}: missing citation {q['must_cite']}")
        else:
            results["qa_citation_correct"] += 1

    def ratio(num, den):
        return round(num / den, 3) if den else None

    results["metrics"] = {
        "verdict_accuracy": ratio(results["verdict_correct"], results["item_total"]),
        "category_accuracy": ratio(results["category_correct"], results["item_total"]),
        "citation_correctness": ratio(results["citation_correct"], results["item_total"]),
        "qa_refusal_accuracy": ratio(results["qa_refusal_correct"], results["qa_total"]),
        "qa_citation_correctness": ratio(results["qa_citation_correct"], results["qa_total"]),
    }
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("expected_json")
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    args = parser.parse_args()
    expected = json.loads(Path(args.expected_json).read_text())
    print(json.dumps(evaluate(expected, args.base_url.rstrip("/")), indent=2))


if __name__ == "__main__":
    main()
