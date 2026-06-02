#!/usr/bin/env python3
import cgi
import hashlib
import html
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from pypdf import PdfReader
except Exception:  # pragma: no cover - surfaced in /api/health
    PdfReader = None


ROOT = Path(__file__).resolve().parents[1]
CASE_DIR = ROOT / "case_study"
IS_VERCEL = bool(os.environ.get("VERCEL"))
DEFAULT_DB_PATH = Path("/tmp/northwind.sqlite3") if IS_VERCEL else ROOT / "data" / "northwind.sqlite3"
DB_PATH = Path(os.environ.get("DATABASE_PATH", DEFAULT_DB_PATH))
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR", "/tmp/northwind-uploads" if IS_VERCEL else str(ROOT / "app" / "uploads")))
STATIC_DIR = ROOT / "app" / "static"
POLICY_DIR = CASE_DIR / "policies"
SAMPLE_DIR = CASE_DIR / "submissions"


def now_iso():
    return datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def row_to_dict(row):
    return dict(row) if row is not None else None


def normalize_space(text):
    return re.sub(r"\s+", " ", text or "").strip()


def slugify(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "submission"


def read_pdf(path):
    if PdfReader is None:
        return ""
    reader = PdfReader(str(path))
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def extract_text(path):
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return read_pdf(path)
    if suffix in {".txt", ".text", ".md"}:
        return path.read_text(errors="replace")
    if suffix in {".jpg", ".jpeg", ".png", ".webp"}:
        return f"[image receipt uploaded: {path.name}; OCR is not available in the local runtime]"
    return path.read_text(errors="replace")


@dataclass
class Clause:
    doc_id: str
    title: str
    section: str
    text: str
    source: str

    def as_citation(self):
        label = self.doc_id
        if self.section:
            label += f" sec. {self.section}"
        return {"label": label, "quote": self.text[:650], "source": self.source}


class PolicyIndex:
    def __init__(self, policy_dir):
        self.policy_dir = Path(policy_dir)
        self.clauses = []
        self._load()

    def _load(self):
        if not self.policy_dir.exists():
            return
        for path in sorted(self.policy_dir.glob("*.pdf")):
            text = normalize_space(read_pdf(path))
            if not text:
                continue
            # The supplied PDFs concatenate several policy documents. Split on each
            # embedded "Document: XYZ-000" marker while retaining the heading.
            starts = [m.start() for m in re.finditer(r"[A-Z][A-Za-z &/-]+ Policy Document: [A-Z]+-\d+", text)]
            starts += [m.start() for m in re.finditer(r"[A-Z][A-Za-z &/-]+ Standard Document: [A-Z]+-\d+", text)]
            starts += [m.start() for m in re.finditer(r"[A-Z][A-Za-z &/-]+ Schedule Document: [A-Z]+-\d+", text)]
            starts = sorted(set([0] + starts + [len(text)]))
            docs = []
            for a, b in zip(starts, starts[1:]):
                part = text[a:b].strip()
                if "Document:" in part:
                    docs.append(part)
            if not docs:
                docs = [text]
            for doc in docs:
                doc_id = (re.search(r"Document:\s*([A-Z]+-\d+)", doc) or re.search(r"\b(TEP-\d{3}|COC-\d{3}|REC-\d{3}|SEC-\d{3}|SUS-\d{3})\b", doc))
                doc_id = doc_id.group(1) if doc_id else path.stem.upper()
                title = doc.split(" Document:")[0][:100]
                markers = list(re.finditer(r"(?<![\d.])(\d+(?:\.\d+)*\.)\s+", doc))
                if not markers:
                    self.clauses.append(Clause(doc_id, title, "", doc[:900], path.name))
                    continue
                for idx, match in enumerate(markers):
                    section = match.group(1).rstrip(".")
                    end = markers[idx + 1].start() if idx + 1 < len(markers) else len(doc)
                    body = normalize_space(doc[match.start():end])
                    if len(body) > 90:
                        self.clauses.append(Clause(doc_id, title, section, body[:1200], path.name))

    def search(self, query, limit=4):
        tokens = [t for t in re.findall(r"[a-z0-9]+", query.lower()) if len(t) > 2]
        if not tokens:
            return []
        scored = []
        for clause in self.clauses:
            hay = (clause.doc_id + " " + clause.title + " " + clause.text).lower()
            score = 0.0
            for token in tokens:
                if token in hay:
                    score += 1.0 + min(hay.count(token), 4) * 0.15
            if score:
                scored.append((score, clause))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [c for _, c in scored[:limit]]

    def cite(self, query, fallback="business purpose documentation"):
        results = self.search(query, 3) or self.search(fallback, 3)
        return [c.as_citation() for c in results[:3]]


POLICIES = PolicyIndex(POLICY_DIR)


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(
            """
            create table if not exists employees (
              id text primary key,
              name text not null,
              grade integer,
              title text,
              department text,
              manager_id text,
              home_base text,
              trip_purpose text,
              trip_dates text,
              created_at text not null
            );
            create table if not exists submissions (
              id text primary key,
              employee_id text not null references employees(id),
              purpose text,
              trip_dates text,
              status text not null,
              source text,
              total_amount real default 0,
              created_at text not null,
              updated_at text not null
            );
            create table if not exists items (
              id text primary key,
              submission_id text not null references submissions(id),
              filename text not null,
              stored_path text,
              extracted_text text,
              merchant text,
              transaction_date text,
              amount real,
              category text,
              verdict text,
              confidence real,
              reasoning text,
              citations text,
              override_verdict text,
              override_comment text,
              override_by text,
              override_at text,
              created_at text not null
            );
            create table if not exists audit_log (
              id integer primary key autoincrement,
              entity_type text not null,
              entity_id text not null,
              action text not null,
              payload text,
              created_at text not null
            );
            """
        )
    seed_employees_and_samples()


def seed_employees_and_samples():
    if not SAMPLE_DIR.exists():
        return
    with db() as conn:
        for info_file in sorted(SAMPLE_DIR.glob("*/employee_info.json")):
            info = json.loads(info_file.read_text())
            conn.execute(
                """
                insert or ignore into employees
                (id, name, grade, title, department, manager_id, home_base, trip_purpose, trip_dates, created_at)
                values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    info["employee_id"],
                    info["name"],
                    info.get("grade"),
                    info.get("title"),
                    info.get("department"),
                    info.get("manager_id"),
                    info.get("home_base"),
                    info.get("trip_purpose"),
                    info.get("trip_dates"),
                    now_iso(),
                ),
            )
            sample_key = info_file.parent.name
            existing = conn.execute("select id from submissions where source = ?", (sample_key,)).fetchone()
            if existing:
                continue
            submission_id = str(uuid.uuid4())
            conn.execute(
                """
                insert into submissions
                (id, employee_id, purpose, trip_dates, status, source, created_at, updated_at)
                values (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    submission_id,
                    info["employee_id"],
                    info.get("trip_purpose"),
                    info.get("trip_dates"),
                    "reviewed",
                    sample_key,
                    now_iso(),
                    now_iso(),
                ),
            )
            for receipt in sorted((info_file.parent / "receipts").glob("*")):
                add_item(conn, submission_id, receipt.name, receipt, copy_file=False)
            refresh_submission_totals(conn, submission_id)


def refresh_submission_totals(conn, submission_id):
    total = conn.execute("select coalesce(sum(amount), 0) as total from items where submission_id = ?", (submission_id,)).fetchone()["total"]
    bad = conn.execute(
        "select count(*) as c from items where submission_id = ? and coalesce(override_verdict, verdict) in ('flagged', 'rejected', 'needs_review')",
        (submission_id,),
    ).fetchone()["c"]
    status = "needs_review" if bad else "reviewed"
    conn.execute("update submissions set total_amount = ?, status = ?, updated_at = ? where id = ?", (total, status, now_iso(), submission_id))


def parse_amount(text):
    patterns = [
        r"GRAND TOTAL\s+\$?(-?\d+(?:,\d{3})*(?:\.\d{2})?)",
        r"Total Charged\s+\$?(-?\d+(?:,\d{3})*(?:\.\d{2})?)",
        r"\bTOTAL\s+\$?(-?\d+(?:,\d{3})*(?:\.\d{2})?)",
        r"\bTotal\s+\$?(-?\d+(?:,\d{3})*(?:\.\d{2})?)",
    ]
    for pattern in patterns:
        matches = re.findall(pattern, text, re.I)
        if matches:
            return float(matches[-1].replace(",", ""))
    amounts = re.findall(r"\$(-?\d+(?:,\d{3})*(?:\.\d{2})?)", text)
    return float(amounts[-1].replace(",", "")) if amounts else None


def parse_date(text):
    months = "Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec|January|February|March|April|June|July|August|September|October|November|December"
    match = re.search(rf"(\d{{1,2}}\s+(?:{months})\s+\d{{4}}|(?:{months})\s+\d{{1,2}},?\s+\d{{4}})", text, re.I)
    return match.group(1) if match else ""


def parse_merchant(text, filename):
    lines = [normalize_space(line.strip("= -")) for line in text.splitlines()]
    lines = [line for line in lines if line and not set(line) <= {"="}]
    if lines:
        return lines[0][:100]
    return Path(filename).stem.replace("_", " ").title()


def category_for(text, filename):
    hay = f"{filename} {text}".lower()
    if any(k in hay for k in ["airlines", "air lines", "e-ticket", "flight", "passenger:"]):
        return "air_travel"
    if any(k in hay for k in ["uber", "lyft", "taxi", "rideshare", "trip receipt"]):
        return "ground_transport"
    if any(k in hay for k in ["hotel", "marriott", "hyatt", "hilton", "check-in", "lodging"]):
        return "lodging"
    if any(k in hay for k in ["conference", "registration", "workshop"]):
        return "conference"
    if any(k in hay for k in ["breakfast", "lunch", "dinner", "restaurant", "tacos", "coffee", "barbecue", "sushi"]):
        return "meal"
    return "other"


def detect_city_tier(text):
    hay = text.lower()
    if any(city in hay for city in ["boston", "seattle", "new york", "san francisco"]):
        return "Tier 1"
    if any(city in hay for city in ["denver", "chicago", "austin"]):
        return "Tier 2"
    return "unknown"


def review_receipt(text, filename, employee):
    clean = normalize_space(text)
    amount = parse_amount(clean)
    category = category_for(clean, filename)
    verdict = "compliant"
    confidence = 0.78
    reasons = []
    citations_query = "business purpose documentation reasonableness receipt requirements"

    if not clean or clean.startswith("[image receipt"):
        verdict = "needs_review"
        confidence = 0.25
        reasons.append("The receipt was uploaded but text could not be extracted locally; a reviewer should verify vendor, date, amount, and itemization.")
        citations_query = "photographs of paper receipts legible receipt required content"
    elif amount is None:
        verdict = "needs_review"
        confidence = 0.35
        reasons.append("No clear total amount was extracted from the receipt.")
        citations_query = "required receipt content total amount"

    hay = clean.lower()
    if category == "air_travel":
        citations_query = "air travel class of service premium economy first class booking requirements"
        if "first class" in hay:
            verdict = "rejected"
            confidence = 0.94
            reasons.append("First class air travel is never reimbursable.")
        elif "business class" in hay and "international" not in hay:
            verdict = "rejected"
            confidence = 0.9
            reasons.append("Business class appears on a domestic itinerary without an international 10-hour segment.")
        elif any(k in hay for k in ["premium select", "premium economy", "comfort+"]):
            durations = [int(h) + int(m) / 60 for h, m in re.findall(r"Duration\s+(\d+)h\s+(\d+)m", clean, re.I)]
            if durations and max(durations) >= 6:
                reasons.append("Premium economy is allowed because at least one scheduled flight segment is 6 hours or more.")
                confidence = 0.86
            else:
                verdict = "flagged"
                confidence = 0.72
                reasons.append("Premium economy requires a scheduled segment of 6 hours or more; the receipt did not establish that threshold.")
        else:
            reasons.append("Domestic economy or standard cabin airfare appears consistent with the air travel policy.")
    elif category == "ground_transport":
        citations_query = "rideshare taxi standard service reimbursable business transportation tip"
        if any(k in hay for k in ["uber black", "lyft lux", "premium"]):
            verdict = "flagged"
            confidence = 0.8
            reasons.append("Premium rideshare categories require an explanation.")
        else:
            reasons.append("Standard rideshare/taxi appears tied to airport or business-trip transportation.")
    elif category == "lodging":
        citations_query = "lodging policy rate cap concur corporate rate reasonable lodging"
        tier = detect_city_tier(clean)
        room_rates = [float(x.replace(",", "")) for x in re.findall(r"Room\s+\$?(\d+(?:,\d{3})*(?:\.\d{2})?)", clean, re.I)]
        cap = 325 if tier == "Tier 1" else 250 if tier == "Tier 2" else None
        if "booked outside concur" in hay:
            verdict = "flagged"
            confidence = 0.86
            reasons.append("The receipt says it was booked outside Concur and no corporate-rate adjustment was applied.")
        if cap and room_rates and max(room_rates) > cap:
            verdict = "flagged"
            confidence = max(confidence, 0.82)
            reasons.append(f"Room rate ${max(room_rates):.2f} is above the inferred {tier} lodging cap of ${cap:.2f}.")
        if not reasons:
            reasons.append("Lodging appears within ordinary business-trip parameters based on extracted rate and dates.")
    elif category == "conference":
        citations_query = "conference attendance registration fee meals included no separate meal reimbursement"
        reasons.append("Conference registration is tied to the stated trip purpose; reviewer should note included meals when assessing same-day meal claims.")
    elif category == "meal":
        citations_query = "meals entertainment caps alcohol solo travel itemized receipt tip"
        alcohol_terms = ["beer", "wine", "cocktail", "hefeweizen", "fireman's", "ale ", "vodka", "whiskey"]
        if any(term in hay for term in alcohol_terms):
            verdict = "rejected"
            confidence = 0.92
            reasons.append("Alcohol appears on a solo-travel meal receipt and is not reimbursable absent sanctioned client entertainment.")
        pretax = None
        m = re.search(r"Subtotal\s+\$?(\d+(?:,\d{3})*(?:\.\d{2})?)", clean, re.I)
        if m:
            pretax = float(m.group(1).replace(",", ""))
        meal_cap = 85
        high_cost = detect_city_tier(clean) == "Tier 1"
        if high_cost:
            meal_cap *= 1.25
        hosted_external = ("client" in hay or "prospect" in hay or "external partner" in hay) and "no external" not in hay
        if "dinner" in filename.lower() and amount and amount > meal_cap and not hosted_external:
            verdict = "flagged" if verdict == "compliant" else verdict
            confidence = max(confidence, 0.84)
            reasons.append(f"Dinner total ${amount:.2f} exceeds the solo/itemized dinner benchmark used by the reviewer engine.")
        tip_match = re.search(r"Tip\s+\$?(-?\d+(?:,\d{3})*(?:\.\d{2})?)", clean, re.I)
        if tip_match and pretax:
            tip = float(tip_match.group(1).replace(",", ""))
            if tip > pretax * 0.2 + 0.01:
                verdict = "flagged" if verdict == "compliant" else verdict
                reasons.append("Tip appears above 20% of the pre-tax meal total.")
        if not reasons:
            reasons.append("Meal receipt is itemized and appears reasonable for the trip context.")
    else:
        reasons.append("The expense has a documented business purpose but did not match a specialized category with high confidence.")

    if amount is not None and amount < 25 and category not in {"meal"}:
        confidence = min(0.9, confidence + 0.05)

    citations = POLICIES.cite(citations_query)
    return {
        "merchant": parse_merchant(text, filename),
        "transaction_date": parse_date(clean),
        "amount": amount,
        "category": category,
        "verdict": verdict,
        "confidence": round(confidence, 2),
        "reasoning": " ".join(reasons),
        "citations": citations,
    }


def add_item(conn, submission_id, filename, source_path, copy_file=True):
    if copy_file:
        target_dir = UPLOAD_DIR / submission_id
        target_dir.mkdir(parents=True, exist_ok=True)
        safe_name = f"{uuid.uuid4().hex[:8]}-{Path(filename).name}"
        target = target_dir / safe_name
        shutil.copyfile(source_path, target)
    else:
        target = source_path
    text = extract_text(target)
    sub = conn.execute("select * from submissions where id = ?", (submission_id,)).fetchone()
    employee = conn.execute("select * from employees where id = ?", (sub["employee_id"],)).fetchone()
    review = review_receipt(text, filename, employee)
    item_id = str(uuid.uuid4())
    conn.execute(
        """
        insert into items
        (id, submission_id, filename, stored_path, extracted_text, merchant, transaction_date, amount,
         category, verdict, confidence, reasoning, citations, created_at)
        values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            submission_id,
            filename,
            str(target.relative_to(ROOT)) if target.is_relative_to(ROOT) else str(target),
            text,
            review["merchant"],
            review["transaction_date"],
            review["amount"],
            review["category"],
            review["verdict"],
            review["confidence"],
            review["reasoning"],
            json.dumps(review["citations"]),
            now_iso(),
        ),
    )
    conn.execute("insert into audit_log (entity_type, entity_id, action, payload, created_at) values (?, ?, ?, ?, ?)", ("item", item_id, "created", json.dumps(review), now_iso()))
    return item_id


def serialize_submission(conn, submission_id):
    sub = row_to_dict(conn.execute("select s.*, e.name employee_name, e.grade, e.department from submissions s join employees e on e.id = s.employee_id where s.id = ?", (submission_id,)).fetchone())
    if not sub:
        return None
    items = []
    for row in conn.execute("select * from items where submission_id = ? order by created_at, filename", (submission_id,)):
        item = row_to_dict(row)
        item["citations"] = json.loads(item["citations"] or "[]")
        item["effective_verdict"] = item["override_verdict"] or item["verdict"]
        items.append(item)
    sub["items"] = items
    return sub


class Handler(BaseHTTPRequestHandler):
    server_version = "NorthwindReview/1.0"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def send_json(self, data, status=200):
        body = json.dumps(data, indent=2).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_error_json(self, message, status=400):
        self.send_json({"error": message}, status)

    def read_json(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_GET(self):
        init_db()
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/":
            return self.serve_static("index.html")
        if path.startswith("/static/"):
            return self.serve_static(path.removeprefix("/static/"))
            
        if path == "/api/health":
            return self.send_json({
                "ok": True,
                "pdf_support": PdfReader is not None,
                "policy_clauses": len(POLICIES.clauses),
            })
        if path == "/api/employees":
            employee_id = parsed.path.split("/")[-1]
            with db() as conn:
                rows = [
                    row_to_dict(r)
                    for r in conn.execute(
                        "select * from employees order by name"
                    )
                ]
            return self.send_json(rows)
        if path == "/api/submissions":
            params = parse_qs(parsed.query)
            where, values = [], []
            if params.get("employee_id"):
                where.append("s.employee_id = ?")
                values.append(params["employee_id"][0])
            if params.get("status"):
                where.append("s.status = ?")
                values.append(params["status"][0])
            sql = "select s.*, e.name employee_name from submissions s join employees e on e.id = s.employee_id"
            if where:
                sql += " where " + " and ".join(where)
            sql += " order by s.created_at desc"
            with db() as conn:
                rows = [row_to_dict(r) for r in conn.execute(sql, values)]
            return self.send_json(rows)
        if path.startswith("/api/submissions/"):
            submission_id = path.split("/")[-1]
            with db() as conn:
                sub = serialize_submission(conn, submission_id)
            return self.send_json(sub) if sub else self.send_error_json("Submission not found", 404)
        return self.send_error(404)

    def serve_static(self, rel):
        rel = rel or "index.html"
        path = (STATIC_DIR / rel).resolve()
        if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.exists():
            return self.send_error(404)
        content_type = "text/html" if path.suffix == ".html" else "text/css" if path.suffix == ".css" else "application/javascript"
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        init_db()
        parsed = urlparse(self.path)
        if parsed.path == "/api/employees":
            payload = self.read_json()
            employee_id = payload.get("id") or "NW-" + uuid.uuid4().hex[:6].upper()
            with db() as conn:
                conn.execute(
                    """
                    insert into employees
                    (id, name, grade, title, department, manager_id, home_base, trip_purpose, trip_dates, created_at)
                    values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        employee_id,
                        payload.get("name") or "New Employee",
                        payload.get("grade") or 1,
                        payload.get("title", ""),
                        payload.get("department", ""),
                        payload.get("manager_id", ""),
                        payload.get("home_base", ""),
                        payload.get("trip_purpose", ""),
                        payload.get("trip_dates", ""),
                        now_iso(),
                    ),
                )
            return self.send_json({"id": employee_id}, 201)
        if parsed.path == "/api/submissions":
            return self.create_submission()
        if parsed.path.startswith("/api/items/") and parsed.path.endswith("/override"):
            item_id = parsed.path.split("/")[-2]
            payload = self.read_json()
            verdict = payload.get("verdict")
            comment = payload.get("comment", "").strip()
            if verdict not in {"compliant", "flagged", "rejected", "needs_review"}:
                return self.send_error_json("Invalid override verdict", 400)
            if not comment:
                return self.send_error_json("Override comment is required", 400)
            with db() as conn:
                row = conn.execute("select submission_id from items where id = ?", (item_id,)).fetchone()
                if not row:
                    return self.send_error_json("Item not found", 404)
                conn.execute(
                    "update items set override_verdict = ?, override_comment = ?, override_by = ?, override_at = ? where id = ?",
                    (verdict, comment, payload.get("reviewer", "finance-reviewer"), now_iso(), item_id),
                )
                conn.execute("insert into audit_log (entity_type, entity_id, action, payload, created_at) values (?, ?, ?, ?, ?)", ("item", item_id, "override", json.dumps(payload), now_iso()))
                refresh_submission_totals(conn, row["submission_id"])
            return self.send_json({"ok": True})
        if parsed.path == "/api/ask":
            payload = self.read_json()
            return self.send_json(answer_question(payload.get("question", "")))
        return self.send_error(404)

    def create_submission(self):
        form = cgi.FieldStorage(fp=self.rfile, headers=self.headers, environ={"REQUEST_METHOD": "POST", "CONTENT_TYPE": self.headers.get("Content-Type")})
        employee_id = form.getfirst("employee_id")
        purpose = form.getfirst("purpose") or ""
        trip_dates = form.getfirst("trip_dates") or ""
        if not employee_id:
            return self.send_error_json("employee_id is required", 400)
        receipt_fields = form["receipts"] if "receipts" in form else []
        if not isinstance(receipt_fields, list):
            receipt_fields = [receipt_fields]
        with db() as conn:
            employee = conn.execute("select * from employees where id = ?", (employee_id,)).fetchone()
            if not employee:
                return self.send_error_json("Employee not found", 404)
            submission_id = str(uuid.uuid4())
            conn.execute(
                "insert into submissions (id, employee_id, purpose, trip_dates, status, source, created_at, updated_at) values (?, ?, ?, ?, ?, ?, ?, ?)",
                (submission_id, employee_id, purpose or employee["trip_purpose"], trip_dates or employee["trip_dates"], "processing", "manual", now_iso(), now_iso()),
            )
            for field in receipt_fields:
                if not getattr(field, "filename", ""):
                    continue
                temp = UPLOAD_DIR / f"tmp-{uuid.uuid4().hex}-{Path(field.filename).name}"
                with temp.open("wb") as f:
                    shutil.copyfileobj(field.file, f)
                add_item(conn, submission_id, field.filename, temp, copy_file=True)
                temp.unlink(missing_ok=True)
            refresh_submission_totals(conn, submission_id)
            sub = serialize_submission(conn, submission_id)
        return self.send_json(sub, 201)


def answer_question(question):
    question = normalize_space(question)
    if not question:
        return {"answer": "Ask a policy question and I will answer from the indexed policy library.", "citations": [], "refused": False}
    out_of_scope = ["weather", "sports", "stock", "recipe", "movie", "restaurant near", "write a poem", "medical", "legal advice"]
    if any(term in question.lower() for term in out_of_scope):
        return {"answer": "I can only answer questions grounded in Northwind's policy library, and this question appears outside that scope.", "citations": [], "refused": True}
    expanded = question
    q_lower = question.lower()
    if "premium economy" in q_lower or ("premium" in q_lower and "flight" in q_lower):
        expanded += " air travel class of service scheduled duration 6 hours TEP-005 economy"
    elif any(term in q_lower for term in ["uber", "lyft", "rideshare", "taxi"]):
        expanded += " ground transportation rideshare taxi TEP-006"
    elif any(term in q_lower for term in ["receipt", "itemized", "amount mismatch"]):
        expanded += " receipt requirements itemized amount mismatch TEP-007"
    elif any(term in q_lower for term in ["alcohol", "beer", "wine"]):
        expanded += " alcohol solo travel client entertainment TEP-003"
    clauses = POLICIES.search(expanded, 5)
    if not clauses:
        return {"answer": "I do not have enough support in the policy library to answer that reliably.", "citations": [], "refused": True}
    snippets = [c.as_citation() for c in clauses[:3]]
    answer = " ".join([f"{c['label']} says: {c['quote']}" for c in snippets])
    return {"answer": answer, "citations": snippets, "refused": False}


def main():
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    host = os.environ.get("HOST", "127.0.0.1")
    print(f"Northwind expense review running at http://{host}:{port}")
    print(f"Database: {DB_PATH}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
