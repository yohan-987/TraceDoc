#!/usr/bin/env python3
"""Deterministic generator for contracts/fixtures (batch C1).

Draws every fixture page with Pillow, records OCR words from the same drawing calls (so every box lands on
visible text), and derives fields, signals, assertions, entities, findings, priority, jobs and expected files.
Re-run:  python3 generate.py   (needs Pillow; output is deterministic; originals/D-03.pdf is created once and kept).
All people, numbers and ID formats are fictional.
"""
import hashlib
import json
import math
import random
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
W, H, DPI = 1240, 1754, 150
PIPELINE = "0.1.0"
NOW = "2026-11-01T09:00:00Z"
FONT_DIR = "/usr/share/fonts/truetype/dejavu/"
F = lambda name, size: ImageFont.truetype(FONT_DIR + name, size)
REG, BOLD = "DejaVuSans.ttf", "DejaVuSans-Bold.ttf"
f_title, f_head, f_label, f_value, f_small, f_row = F(BOLD, 46), F(BOLD, 30), F(REG, 25), F(REG, 32), F(REG, 22), F(REG, 28)
f_bold_val = F(BOLD, 30)
GRAY, BLACK, LINE = (95, 95, 95), (0, 0, 0), (170, 170, 170)

r2 = lambda v: round(v + 0.0, 2)
r4 = lambda v: round(v, 4)


def sha_bytes(b):
    return hashlib.sha256(b).hexdigest()


def sha_str(s):
    return hashlib.sha256(s.encode()).hexdigest()


# ----------------------------------------------------------------------------------- canvas with OCR capture
class Canvas:
    def __init__(self, doc_id, base_conf, seed):
        self.doc_id = doc_id
        self.img = Image.new("RGB", (W, H), (255, 255, 255))
        self.d = ImageDraw.Draw(self.img)
        self.words, self.lines, self.fields = [], [], []
        self.rng = random.Random(seed)
        self.base = base_conf

    def text(self, x, y, s, font, fill=BLACK, right=None, conf=None):
        if right is not None:
            x = right - self.d.textlength(s, font=font)
        asc, desc = font.getmetrics()
        h = asc + desc
        self.d.text((x, y), s, font=font, fill=fill)
        toks, idxs, li = s.split(" "), [], len(self.lines)
        for i, t in enumerate(toks):
            prefix = " ".join(toks[:i]) + (" " if i else "")
            x0 = x + self.d.textlength(prefix, font=font)
            x1 = x0 + self.d.textlength(t, font=font)
            c = conf if conf is not None else min(0.999, max(0.5, self.base + self.rng.gauss(0, 0.01)))
            self.words.append({"text": t, "px": [x0 - 1, y, x1 + 1, y + h], "conf": round(c, 3), "line": li})
            idxs.append(len(self.words) - 1)
        px = self.union(idxs)
        self.lines.append({"text": s, "px": px, "conf": round(sum(self.words[i]["conf"] for i in idxs) / len(idxs), 3), "words": idxs})
        return idxs

    def union(self, idxs):
        b = [self.words[i]["px"] for i in idxs]
        return [min(v[0] for v in b), min(v[1] for v in b), max(v[2] for v in b), max(v[3] for v in b)]

    def field(self, name, idxs, ext_conf, validation="NOT_CHECKED", norm=None):
        x0, y0, x1, y1 = self.union(idxs)
        pad = 4
        raw = " ".join(self.words[i]["text"] for i in idxs)
        n = len(self.fields) + 1
        self.fields.append({
            "fieldId": f"{self.doc_id}-F{n:02d}", "docId": self.doc_id, "name": name, "rawValue": raw,
            "normalizedValue": norm if norm is not None else raw,
            "ocrConfidence": r2(sum(self.words[i]["conf"] for i in idxs) / len(idxs)),
            "extractionConfidence": ext_conf, "validationStatus": validation,
            "region": {"docId": self.doc_id, "page": 1, "bbox": self.nb([x0 - pad, y0 - pad, x1 + pad, y1 + pad]), "label": name},
            "source": "ocr", "_px": [x0 - pad, y0 - pad, x1 + pad, y1 + pad],
        })

    @staticmethod
    def nb(px):
        return [r4(px[0] / W), r4(px[1] / H), r4((px[2] - px[0]) / W), r4((px[3] - px[1]) / H)]

    def sig_box(self, x0, y0, x1, y1, seed):
        self.d.rectangle([x0, y0, x1, y1], outline=LINE, width=2)
        rng, pts, x = random.Random(seed), [], x0 + 25
        while x < x1 - 30:
            pts.append((x, (y0 + y1) / 2 + rng.randint(-18, 18)))
            x += 22
        self.d.line(pts, fill=(20, 20, 90), width=3)
        self.text(x0, y0 - 30, "Signature", f_small, GRAY)


# ----------------------------------------------------------------------------------- normalizers
def n_date(s):
    d, m, y = s.split("/")
    return f"{y}-{m}-{d}"


def n_amount(s):
    return s.replace(",", "")


def n_name(s):
    return " ".join(w.capitalize() for w in s.split())


def n_id(s):
    return s.replace(" ", "").upper()


def row_field(c, y, label, value, name, ext, validation="NOT_CHECKED", norm=None, x=100, conf=None):
    c.text(x, y, label, f_label, GRAY)
    idxs = c.text(x, y + 34, value, f_value, conf=conf)
    c.field(name, idxs, ext, validation, norm if norm is not None else value)
    return idxs


# ----------------------------------------------------------------------------------- page renderers
def render_id_card(doc_id, v, ext, seed, base=0.985):
    c = Canvas(doc_id, base, seed)
    c.d.rounded_rectangle([80, 100, 1160, 840], radius=30, outline=(60, 60, 60), width=4, fill=(246, 249, 252))
    c.text(120, 130, "SAMPLE IDENTITY CARD", f_head)
    c.text(700, 138, "(FICTIONAL)", f_label, GRAY)
    c.d.rectangle([120, 230, 380, 600], fill=(205, 210, 216), outline=LINE, width=2)
    c.text(205, 400, "PHOTO", f_label, GRAY)
    x = 420
    row_field(c, 210, "Name", v["full_name"], "full_name", ext["full_name"], norm=n_name(v["full_name"]), x=x)
    row_field(c, 310, "Date of Birth", v["dob"], "dob", ext["dob"], "VALID", n_date(v["dob"]), x=x)
    row_field(c, 410, "ID Number", v["id_number"], "id_number", ext["id_number"], "VALID", n_id(v["id_number"]), x=x)
    row_field(c, 510, "Address (permanent)", v["address_permanent"], "address_permanent", ext["address_permanent"], x=x)
    row_field(c, 610, "Date of Issue", v["issue_date"], "issue_date", ext["issue_date"], "VALID", n_date(v["issue_date"]), x=x)
    c.sig_box(x, 745, 1100, 815, seed + 1)
    c.text(80, 880, "Specimen document for software testing only", f_small, GRAY)
    return c


def render_form(doc_id, v, ext, seed, base=0.955, tint=None):
    c = Canvas(doc_id, base, seed)
    c.text(100, 100, "LOAN APPLICATION FORM", f_title)
    c.text(100, 170, "Sample form (fictional data)", f_label, GRAY)
    c.d.line([100, 230, 1140, 230], fill=LINE, width=2)
    c.text(100, 250, "Applicant details", f_head)
    y = 320
    rows = [("Full name", "full_name", n_name(v["full_name"]), None), ("Date of birth", "dob", n_date(v["dob"]), "VALID"),
            ("ID number", "id_number", n_id(v["id_number"]), "VALID"), ("Permanent address", "address_permanent", None, None),
            ("Current address", "address_current", None, None), ("Requested loan amount", "requested_amount", n_amount(v["requested_amount"]), "VALID"),
            ("Application date", "application_date", n_date(v["application_date"]), "VALID")]
    if "guarantor_full_name" in v:
        rows += [("Guarantor full name", "guarantor_full_name", n_name(v["guarantor_full_name"]), None),
                 ("Guarantor ID number", "guarantor_id_number", n_id(v["guarantor_id_number"]), "VALID")]
    for label, name, norm, val in rows:
        if tint == name:  # slightly different background and noise behind this value: a visible cue for the tamper story
            tw = c.d.textlength(v[name], font=f_value)
            box = [92, y + 28, int(100 + tw + 14), y + 80]
            c.d.rectangle(box, fill=(244, 244, 238))
            nr = random.Random(seed + 7)
            for _ in range(500):
                px, py = nr.randint(box[0], box[2]), nr.randint(box[1], box[3])
                g = nr.randint(226, 252)
                c.d.point((px, py), fill=(g, g, g))
        row_field(c, y, label, v[name], name, ext[name], val or "NOT_CHECKED", norm if norm is not None else v[name])
        y += 108
    c.sig_box(100, y + 50, 600, y + 120, seed + 1)
    return c


TXN = [("03/09/2026", "Salary credit", "42,000.00"), ("09/09/2026", "Refund - online order", "1,250.50"),
       ("17/09/2026", "Transfer in", "5,000.00"), ("26/09/2026", "Interest credit", "312.25")]


def render_statement(doc_id, v, ext, seed, base=0.995):
    c = Canvas(doc_id, base, seed)
    c.text(100, 100, "SAMPLE BANK", f_title)
    c.text(100, 170, "Statement of Account (fictional)", f_label, GRAY)
    c.d.line([100, 230, 1140, 230], fill=LINE, width=2)
    row_field(c, 270, "Account holder", v["full_name"], "full_name", ext["full_name"], norm=n_name(v["full_name"]))
    row_field(c, 380, "Account number", v["account_number"], "account_number", ext["account_number"], norm=n_id(v["account_number"]))
    row_field(c, 490, "Statement date", v["statement_date"], "statement_date", ext["statement_date"], "VALID", n_date(v["statement_date"]))
    c.text(100, 640, "Deposits this period", f_head)
    c.d.line([100, 690, 1140, 690], fill=(60, 60, 60), width=2)
    c.text(110, 700, "Date", f_label, GRAY)
    c.text(330, 700, "Description", f_label, GRAY)
    c.text(0, 700, "Amount", f_label, GRAY, right=1130)
    y = 750
    for i, (d, desc, amt) in enumerate(TXN):
        c.text(110, y, d, f_row)
        c.text(330, y, desc, f_row)
        idxs = c.text(0, y, amt, f_row, right=1130)
        c.field("txn_amount", idxs, ext["txn_amount"][i], "VALID", n_amount(amt))
        c.d.line([100, y + 50, 1140, y + 50], fill=(220, 220, 220), width=1)
        y += 64
    y += 24
    c.d.line([100, y - 8, 1140, y - 8], fill=(60, 60, 60), width=2)
    c.text(330, y, "Total deposits", f_bold_val)
    idxs = c.text(0, y, v["statement_total"], f_bold_val, right=1130)
    c.field("statement_total", idxs, ext["statement_total"], "VALID", n_amount(v["statement_total"]))
    y += 110
    c.text(330, y, "Closing balance", f_bold_val)
    idxs = c.text(0, y, v["account_balance"], f_bold_val, right=1130)
    c.field("account_balance", idxs, ext["account_balance"], "VALID", n_amount(v["account_balance"]))
    c.text(100, 1500, "Computer generated specimen statement. Not a real account.", f_small, GRAY)
    return c


# ----------------------------------------------------------------------------------- document specs
IMG_AN = {"ocr": "AVAILABLE", "pdf.textlayer": "NOT_APPLICABLE", "pdf.fonts": "NOT_APPLICABLE", "image.ela": "AVAILABLE",
          "image.noise": "AVAILABLE", "image.copymove": "AVAILABLE", "signature": "AVAILABLE"}
PDF_AN = {"ocr": "AVAILABLE", "pdf.textlayer": "AVAILABLE", "pdf.fonts": "AVAILABLE", "image.ela": "NOT_APPLICABLE",
          "image.noise": "NOT_APPLICABLE", "image.copymove": "NOT_APPLICABLE", "signature": "NOT_APPLICABLE"}

DOCS = [
    dict(docId="D-01", caseId="CASE-001", docType="id_card", filename="id_card_aarav.png", mime="image/png", ext="png", an=dict(IMG_AN), seed=11,
         v=dict(full_name="AARAV KUMAR SHARMA", dob="12/08/2004", id_number="TD-48213-77", address_permanent="14 Maple Court, Riverton", issue_date="03/02/2023"),
         e=dict(full_name=0.96, dob=0.95, id_number=0.94, address_permanent=0.90, issue_date=0.93), docdate="issue_date"),
    dict(docId="D-02", caseId="CASE-001", docType="application_form", filename="loan_application_aarav.png", mime="image/png", ext="png",
         an={**IMG_AN, "signature": "UNAVAILABLE"}, seed=12, tint="requested_amount",
         v=dict(full_name="Aarav Sharma", dob="12/08/2001", id_number="TD-48213-77", address_permanent="14 Maple Court, Riverton",
                address_current="Flat 9, Lakeview Residency, Riverton", requested_amount="250,000.00", application_date="15/10/2026"),
         e=dict(full_name=0.91, dob=0.91, id_number=0.92, address_permanent=0.88, address_current=0.86, requested_amount=0.93, application_date=0.92),
         docdate="application_date"),
    dict(docId="D-03", caseId="CASE-001", docType="bank_statement", filename="bank_statement_aarav.pdf", mime="application/pdf", ext="pdf", an=dict(PDF_AN), seed=13,
         v=dict(full_name="AARAV KUMAR SHARMA", account_number="AC-5521-009981", statement_date="30/09/2026", statement_total="49,562.75", account_balance="86,430.10"),
         e=dict(full_name=0.98, account_number=0.98, statement_date=0.98, txn_amount=[0.98, 0.98, 0.98, 0.98], statement_total=0.96, account_balance=0.98),
         docdate="statement_date"),
    dict(docId="D-11", caseId="CASE-002", docType="id_card", filename="id_card_priya_nair.png", mime="image/png", ext="png", an=dict(IMG_AN), seed=21,
         v=dict(full_name="PRIYA NAIR", dob="05/03/1996", id_number="TD-61020-15", address_permanent="22 Harbor Lane, Westport", issue_date="17/06/2022"),
         e=dict(full_name=0.97, dob=0.96, id_number=0.95, address_permanent=0.91, issue_date=0.94), docdate="issue_date"),
    dict(docId="D-12", caseId="CASE-002", docType="id_card", filename="id_card_priya_nayar.png", mime="image/png", ext="png", an=dict(IMG_AN), seed=22,
         v=dict(full_name="PRIYA NAYAR", dob="21/11/1971", id_number="TD-61020-51", address_permanent="7 Orchard Road, Westport", issue_date="09/01/2021"),
         e=dict(full_name=0.96, dob=0.95, id_number=0.95, address_permanent=0.90, issue_date=0.93), docdate="issue_date"),
    dict(docId="D-13", caseId="CASE-002", docType="application_form", filename="loan_application_priya.png", mime="image/png", ext="png", an=dict(IMG_AN), seed=23,
         v=dict(full_name="Priya Nair", dob="05/03/1996", id_number="TD-61020-15", address_permanent="22 Harbor Lane, Westport",
                address_current="22 Harbor Lane, Westport", requested_amount="120,000.00", application_date="20/10/2026",
                guarantor_full_name="Priya Nayar", guarantor_id_number="TD-61020-51"),
         e=dict(full_name=0.93, dob=0.92, id_number=0.93, address_permanent=0.89, address_current=0.89, requested_amount=0.94, application_date=0.93,
                guarantor_full_name=0.90, guarantor_id_number=0.91), docdate="application_date"),
]
BASES = {"D-01": 0.985, "D-02": 0.955, "D-03": 0.995, "D-11": 0.98, "D-12": 0.98, "D-13": 0.96}

# --------------------------------------------------------------------------------------------- build pages
for sub in ("pages", "ocr", "analyze"):
    shutil.rmtree(HERE / sub, ignore_errors=True)
    (HERE / sub).mkdir()
(HERE / "originals").mkdir(exist_ok=True)  # kept between runs: Pillow stamps the current time into PDFs

built = {}
for d in DOCS:
    kind = d["docType"]
    if kind == "id_card":
        c = render_id_card(d["docId"], d["v"], d["e"], d["seed"], BASES[d["docId"]])
    elif kind == "application_form":
        c = render_form(d["docId"], d["v"], d["e"], d["seed"], BASES[d["docId"]], d.get("tint"))
    else:
        c = render_statement(d["docId"], d["v"], d["e"], d["seed"], BASES[d["docId"]])
    page_name = f"{d['docId']}-page-1"
    png = HERE / "pages" / f"{page_name}.png"
    c.img.save(png, optimize=True)
    orig = HERE / "originals" / f"{d['docId']}.{d['ext']}"
    if d["ext"] == "pdf":
        if not orig.exists():  # created once; its SHA-256 is referenced by many fixtures. Delete it to recreate (hashes change)
            c.img.save(orig, "PDF", resolution=float(DPI))
    else:
        shutil.copyfile(png, orig)
    d["sha256"] = sha_bytes(orig.read_bytes())
    # pixel check: every field box must contain dark (text) pixels
    gray = c.img.convert("L")
    for fld in c.fields:
        x0, y0, x1, y1 = [int(round(v)) for v in fld["_px"]]
        assert gray.crop((x0, y0, x1, y1)).getextrema()[0] < 110, f"{fld['fieldId']} box has no visible text"
        # the box must also be tight: not mostly blank
    ocr = {
        "docId": d["docId"], "page": 1, "widthPx": W, "heightPx": H, "renderDpi": DPI, "engine": "fixture", "engineVersion": "0",
        "words": [{"text": w["text"], "bbox": Canvas.nb(w["px"]), "ocrConfidence": w["conf"], "lineIndex": w["line"]} for w in c.words],
        "lines": [{"text": ln["text"], "bbox": Canvas.nb(ln["px"]), "ocrConfidence": ln["conf"], "wordIndices": ln["words"]} for ln in c.lines],
    }
    (HERE / "ocr" / f"{page_name}.json").write_text(json.dumps(ocr, indent=1) + "\n")
    d["canvas"] = c
    built[d["docId"]] = d


def prov(analyzer, status, doc_sha, config=None, model=None):
    p = {"analyzer": analyzer, "analyzerVersion": "0.1"}
    if model:
        p["modelVersion"] = model
    p.update({"config": config or {}, "inputSha256": doc_sha, "processedAt": NOW, "status": status})
    return p


def region(c, name, pad=0):
    fld = next(f for f in c.fields if f["name"] == name)
    return fld


def iou(a, b):
    ax0, ay0, ax1, ay1 = a[0], a[1], a[0] + a[2], a[1] + a[3]
    bx0, by0, bx1, by1 = b[0], b[1], b[0] + b[2], b[1] + b[3]
    iw, ih = max(0, min(ax1, bx1) - max(ax0, bx0)), max(0, min(ay1, by1) - max(ay0, by0))
    inter = iw * ih
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


# --------------------------------------------------------------------------------------------- signals
signals = {d["docId"]: [] for d in DOCS}
amt = next(f for f in built["D-02"]["canvas"].fields if f["name"] == "requested_amount")
ax0, ay0, ax1, ay1 = amt["_px"]
ela_box = Canvas.nb([ax0 - 6, ay0 - 6, ax1 + 6, ay1 + 6])
noise_box = Canvas.nb([ax0 - 20, ay0 - 12, ax1 + 30, ay1 + 14])
assert 0.3 <= iou(ela_box, noise_box) < 0.95, iou(ela_box, noise_box)
sha02 = built["D-02"]["sha256"]
signals["D-02"] = [
    {"signalId": "D-02-S01", "detector": "image.ela", "type": "ELA_ANOMALY",
     "regions": [{"docId": "D-02", "page": 1, "bbox": ela_box, "label": "requested_amount"}], "confidence": 0.62,
     "measurements": {"residualRatio": 2.4, "blockSizePx": 16}, "provenance": prov("image.ela", "AVAILABLE", sha02, {"jpegQuality": 90, "blockSize": 16, "zThreshold": 2.5})},
    {"signalId": "D-02-S02", "detector": "image.noise", "type": "NOISE_INCONSISTENCY",
     "regions": [{"docId": "D-02", "page": 1, "bbox": noise_box, "label": "requested_amount"}], "confidence": 0.58,
     "measurements": {"varianceRatio": 3.1, "windowPx": 32}, "provenance": prov("image.noise", "AVAILABLE", sha02, {"windowPx": 32, "zThreshold": 2.0})},
]
tot = next(f for f in built["D-03"]["canvas"].fields if f["name"] == "statement_total")
signals["D-03"] = [
    {"signalId": "D-03-S01", "detector": "pdf.textlayer", "type": "TEXT_LAYER_MISMATCH",
     "regions": [{"docId": "D-03", "page": 1, "bbox": tot["region"]["bbox"], "label": "statement_total"}], "confidence": 0.72,
     "measurements": {"renderedText": "49,562.75", "textLayerText": "48,562.75", "differingChars": 1},
     "provenance": prov("pdf.textlayer", "AVAILABLE", built["D-03"]["sha256"], {"minWordDiffs": 1})},
]

# --------------------------------------------------------------------------------------------- analyze responses
for d in DOCS:
    c = d["canvas"]
    d["fields_out"] = [{k: v for k, v in f.items() if k != "_px"} for f in c.fields]
    resp = {
        "docId": d["docId"], "pipelineVersion": PIPELINE,
        "pages": [{"docId": d["docId"], "page": 1, "renderPath": f"contracts/fixtures/pages/{d['docId']}-page-1.png", "renderDpi": DPI,
                   "rotationDeg": 0, "deskewDeg": 0.0, "docType": d["docType"], "docTypeConfidence": 0.96 if d["docType"] != "application_form" else 0.93}],
        "fields": d["fields_out"], "signals": signals[d["docId"]], "analyzerStatuses": d["an"],
        "provenance": [prov(a, s, d["sha256"], {}, "fixture" if a == "ocr" else None) for a, s in d["an"].items()],
    }
    d["analyze"] = resp
    (HERE / "analyze" / f"{d['docId']}.json").write_text(json.dumps(resp, indent=2) + "\n")

# --------------------------------------------------------------------------------------------- assertions
SEM = lambda name: name[len("guarantor_"):] if name.startswith("guarantor_") else name


def build_assertions(case_id, doc_ids, entity_of):
    out = []
    for did in doc_ids:
        d = built[did]
        dd = next(f for f in d["fields_out"] if f["name"] == d["docdate"])["normalizedValue"]
        for i, f in enumerate(d["fields_out"], 1):
            ent = entity_of(did, f["name"])
            a = {"assertionId": f"{did}-A{i:02d}", "caseId": case_id, "docId": did, "fieldId": f["fieldId"], "entityId": ent["entityId"],
                 "entityRole": ent["role"], "semanticType": SEM(f["name"]), "normalizedValue": f["normalizedValue"], "documentDate": dd}
            if SEM(f["name"]) in ("account_balance", "statement_total"):
                a["effectiveDate"] = dd
            a.update({"extractionConfidence": f["extractionConfidence"], "region": f["region"]})
            out.append(a)
    return out


ENT1 = {"entityId": "E-001", "role": "applicant"}
ENT2 = {"entityId": "E-002", "role": "account_holder"}
ENT2G = {"entityId": "E-002", "role": "guarantor"}
ASSERT = {
    "CASE-001": build_assertions("CASE-001", ["D-01", "D-02", "D-03"], lambda did, n: ENT2 if did == "D-03" else ENT1),
    "CASE-002": build_assertions("CASE-002", ["D-11", "D-12", "D-13"],
                                 lambda did, n: ENT2G if (did == "D-12" or n.startswith("guarantor_")) else ENT1),
}
A = {a["assertionId"]: a for v in ASSERT.values() for a in v}


def aid(case, doc, sem, nth=0):
    return [a for a in ASSERT[case] if a["docId"] == doc and a["semanticType"] == sem][nth]["assertionId"]


ENTITIES = {
    "CASE-001": [
        {"entityId": "E-001", "caseId": "CASE-001", "role": "applicant", "resolution": "RESOLVED", "basis": ["id_number exact match: TD-48213-77 (D-01, D-02)"],
         "assertionIds": [a["assertionId"] for a in ASSERT["CASE-001"] if a["entityId"] == "E-001"]},
        {"entityId": "E-002", "caseId": "CASE-001", "role": "account_holder", "resolution": "SUGGESTED",
         "basis": ["name similarity 100 to E-001 (suggested only, not merged)", "no cross-document strong identifier match"],
         "suggestedLinks": [{"entityId": "E-001", "score": 100.0, "method": "name_token_sort"}],
         "assertionIds": [a["assertionId"] for a in ASSERT["CASE-001"] if a["entityId"] == "E-002"]},
    ],
    "CASE-002": [
        {"entityId": "E-001", "caseId": "CASE-002", "role": "applicant", "resolution": "RESOLVED", "basis": ["id_number exact match: TD-61020-15 (D-11, D-13)", "different id_number from E-002, kept separate despite similar names (token-sort ratio 85.7)"],
         "assertionIds": [a["assertionId"] for a in ASSERT["CASE-002"] if a["entityId"] == "E-001"]},
        {"entityId": "E-002", "caseId": "CASE-002", "role": "guarantor", "resolution": "RESOLVED",
         "basis": ["id_number exact match: TD-61020-51 (D-12, D-13)", "different id_number from E-001, kept separate despite similar names (token-sort ratio 85.7)"],
         "assertionIds": [a["assertionId"] for a in ASSERT["CASE-002"] if a["entityId"] == "E-002"]},
    ],
}

# --------------------------------------------------------------------------------------------- findings (CASE-001)
def lcs(a, b):
    dp = [[0] * (len(b) + 1) for _ in range(len(a) + 1)]
    for i in range(len(a)):
        for j in range(len(b)):
            dp[i + 1][j + 1] = dp[i][j] + 1 if a[i] == b[j] else max(dp[i][j + 1], dp[i + 1][j])
    return dp[-1][-1]


def token_sort_ratio(x, y):
    sx, sy = " ".join(sorted(x.lower().split())), " ".join(sorted(y.lower().split()))
    return 100.0 * 2 * lcs(sx, sy) / (len(sx) + len(sy))


def case_sha(doc_ids):
    return sha_str("\n".join(sorted(built[i]["sha256"] for i in doc_ids)))


CMP_CFG = {"nameMatchMin": 92, "nameVariantMin": 75, "unresolvedConfidenceCap": 0.3}
ev_counter = [0]


def evidence(kind, desc, regions, assertion_ids, meas, provenance):
    ev_counter[0] += 1
    e = {"evidenceId": f"E-{ev_counter[0]:04d}", "kind": kind, "description": desc, "regions": regions}
    if assertion_ids:
        e["assertionIds"] = assertion_ids
    e.update({"measurements": meas, "provenance": provenance})
    return e


def pair_finding(fid, case, ftype, status, sev, conf, a1, a2, rule_id, rule_type, desc, meas, verify, docs):
    ev = evidence("assertion_pair", desc, [A[a1]["region"], A[a2]["region"]], [a1, a2],
                  {"ruleId": rule_id, "ruleType": rule_type, **meas}, prov("comparability", "AVAILABLE", case_sha(docs), CMP_CFG))
    return {"findingId": fid, "caseId": case, "category": "cross_document", "type": ftype, "observationStatus": status, "severity": sev,
            "confidence": conf, "evidence": [ev], "needsVerification": verify}


c1 = "CASE-001"
dob1, dob2 = aid(c1, "D-01", "dob"), aid(c1, "D-02", "dob")
nm1, nm2, nm3 = aid(c1, "D-01", "full_name"), aid(c1, "D-02", "full_name"), aid(c1, "D-03", "full_name")
F1 = pair_finding("F-0001", c1, "DOB_CONFLICT", "INCONSISTENT", "high", r2(min(A[dob1]["extractionConfidence"], A[dob2]["extractionConfidence"])),
                  dob1, dob2, "R-DOB-MUST-AGREE", "MUST_AGREE", "dob differs for the same resolved applicant (rule: R-DOB-MUST-AGREE, MUST_AGREE)",
                  {"valueA": A[dob1]["normalizedValue"], "valueB": A[dob2]["normalizedValue"], "entityResolution": "RESOLVED", "dayMonthSwap": "false",
                   "legitimateDifferenceRules": "none apply to semantic type dob"},
                  ["Inspect both date-of-birth regions on the original documents"], ["D-01", "D-02"])
ratio12 = token_sort_ratio(A[nm1]["normalizedValue"], A[nm2]["normalizedValue"])
ratio23 = token_sort_ratio(A[nm2]["normalizedValue"], A[nm3]["normalizedValue"])
ratio13 = token_sort_ratio(A[nm1]["normalizedValue"], A[nm3]["normalizedValue"])
assert 75 <= ratio12 < 92 and 75 <= ratio23 < 92 and ratio13 >= 92, (ratio12, ratio23, ratio13)
F2 = pair_finding("F-0002", c1, "NAME_VARIANT_SPELLING", "INCONSISTENT", "low", r2(min(A[nm1]["extractionConfidence"], A[nm2]["extractionConfidence"])),
                  nm1, nm2, "R-NAME-MUST-AGREE", "MUST_AGREE", "full_name differs slightly for the same resolved applicant (rule: R-NAME-MUST-AGREE, MUST_AGREE)",
                  {"valueA": A[nm1]["normalizedValue"], "valueB": A[nm2]["normalizedValue"], "tokenSortRatio": round(ratio12, 1), "entityResolution": "RESOLVED",
                   "legitimateDifferenceRules": "none apply to semantic type full_name"},
                  ["Compare the name regions on both documents; a missing middle name is a common variation"], ["D-01", "D-02"])
F3 = pair_finding("F-0003", c1, "NAME_VARIANT_SPELLING", "INCONSISTENT", "low", CMP_CFG["unresolvedConfidenceCap"],
                  nm2, nm3, "R-NAME-MUST-AGREE", "MUST_AGREE",
                  "full_name differs slightly; the account holder is only a SUGGESTED link to the applicant, so confidence is capped (rule: R-NAME-MUST-AGREE)",
                  {"valueA": A[nm2]["normalizedValue"], "valueB": A[nm3]["normalizedValue"], "tokenSortRatio": round(ratio23, 1), "entityResolution": "SUGGESTED",
                   "confidenceCapped": "true", "legitimateDifferenceRules": "none apply to semantic type full_name"},
                  ["Confirm that the account holder is the same person as the applicant", "Compare the name regions on both documents"], ["D-02", "D-03"])
txn_ids = [a["assertionId"] for a in ASSERT[c1] if a["semanticType"] == "txn_amount"]
tot_id = aid(c1, "D-03", "statement_total")
computed = sum(float(A[i]["normalizedValue"]) for i in txn_ids)
printed = float(A[tot_id]["normalizedValue"])
assert abs(printed - computed - 1000.0) < 1e-6
ev4 = evidence("rule_result", "Printed total does not equal the sum of the listed amounts (rule: R-STMT-TOTAL-ARITH, ARITHMETIC)",
               [A[tot_id]["region"]] + [A[i]["region"] for i in txn_ids], [tot_id] + txn_ids,
               {"ruleId": "R-STMT-TOTAL-ARITH", "ruleType": "ARITHMETIC", "printedTotal": A[tot_id]["normalizedValue"], "computedTotal": f"{computed:.2f}",
                "difference": f"{printed - computed:.2f}", "itemCount": len(txn_ids)}, prov("contextRules", "AVAILABLE", built["D-03"]["sha256"], {"arithmeticToleranceCents": 0}))
F4 = {"findingId": "F-0004", "caseId": c1, "category": "context", "type": "STATEMENT_TOTAL_MISMATCH", "observationStatus": "INCONSISTENT", "severity": "medium",
      "confidence": r2(min(A[i]["extractionConfidence"] for i in [tot_id] + txn_ids)), "evidence": [ev4],
      "needsVerification": ["Recompute the listed amounts and compare with the printed total on the original document"]}

# events -> findings
ev_cfg = {"iouThreshold": 0.3, "agreementBonus": 0.05}
s_ela, s_noise = signals["D-02"]
union_px = [min(s["regions"][0]["bbox"][0] for s in (s_ela, s_noise)), min(s["regions"][0]["bbox"][1] for s in (s_ela, s_noise))]
ux1 = max(s["regions"][0]["bbox"][0] + s["regions"][0]["bbox"][2] for s in (s_ela, s_noise))
uy1 = max(s["regions"][0]["bbox"][1] + s["regions"][0]["bbox"][3] for s in (s_ela, s_noise))
union_bbox = [r4(union_px[0]), r4(union_px[1]), r4(ux1 - union_px[0]), r4(uy1 - union_px[1])]
EV1 = {"eventId": "EV-001", "caseId": c1, "docId": "D-02", "page": 1, "regions": [{"docId": "D-02", "page": 1, "bbox": union_bbox, "label": "requested_amount"}],
       "signalIds": ["D-02-S01", "D-02-S02"], "detectors": ["image.ela", "image.noise"], "confidence": r2(max(0.62, 0.58) + 0.05),
       "provenance": prov("eventGrouper", "AVAILABLE", sha02, ev_cfg)}
EV2 = {"eventId": "EV-002", "caseId": c1, "docId": "D-03", "page": 1, "regions": signals["D-03"][0]["regions"], "signalIds": ["D-03-S01"],
       "detectors": ["pdf.textlayer"], "confidence": 0.72, "provenance": prov("eventGrouper", "AVAILABLE", built["D-03"]["sha256"], ev_cfg)}
sev_of = lambda conf: "low" if conf < 0.4 else ("medium" if conf < 0.75 else "high")
F5 = {"findingId": "F-0005", "caseId": c1, "category": "visual", "type": "REGION_EVENT", "observationStatus": "POTENTIAL_ANOMALY", "severity": sev_of(EV1["confidence"]),
      "confidence": EV1["confidence"],
      "evidence": [evidence("forensic_signal", "Overlapping signals from image.ela and image.noise flag the same region on D-02 page 1 (requested_amount)",
                            [s_ela["regions"][0], s_noise["regions"][0]], [aid(c1, "D-02", "requested_amount")],
                            {"D-02-S01.residualRatio": 2.4, "D-02-S01.blockSizePx": 16, "D-02-S02.varianceRatio": 3.1, "D-02-S02.windowPx": 32,
                             "eventId": "EV-001", "signalIds": "D-02-S01,D-02-S02", "detectors": "image.ela,image.noise"}, EV1["provenance"])],
      "needsVerification": ["Inspect the flagged region on the original document", "Overlapping cues can share one cause, such as image recompression; do not read them in isolation"]}
F6 = {"findingId": "F-0006", "caseId": c1, "category": "pdf_structure", "type": "TEXT_LAYER_MISMATCH", "observationStatus": "POTENTIAL_ANOMALY", "severity": sev_of(0.72),
      "confidence": 0.72,
      "evidence": [evidence("text_layer_diff", "Text layer mismatch reported by pdf.textlayer on D-03 page 1 (statement_total)",
                            signals["D-03"][0]["regions"], [tot_id],
                            {"renderedText": "49,562.75", "textLayerText": "48,562.75", "differingChars": 1,
                             "eventId": "EV-002", "signalIds": "D-03-S01", "detectors": "pdf.textlayer"},
                            signals["D-03"][0]["provenance"])],
      "needsVerification": ["Compare the rendered text with the text layer value in a PDF viewer", "OCR layers added to scans can also cause this signal"]}
FINDINGS = {"CASE-001": [F1, F2, F3, F4, F5, F6], "CASE-002": []}

# --------------------------------------------------------------------------------------------- priority (reference calculation)
SEV_W = {"info": 0.05, "low": 0.15, "medium": 0.40, "high": 0.70}
CAT_W = {"cross_document": 1.0, "context": 0.9, "visual": 0.8, "pdf_structure": 0.7, "extraction": 0.4}
BANDS = [(0.25, "low"), (0.50, "moderate"), (0.75, "high")]
WEAK_CAP = 0.49
_ENGINE = json.loads((HERE.parent.parent / "backend" / "config" / "engine.json").read_text())["priority"]
EXPECTED_ANALYZERS = _ENGINE["expectedAnalyzers"]
LOW_COVERAGE_BELOW = _ENGINE["lowCoverageBelow"]
PRI_CFG = {**{f"severityWeight_{k}": v for k, v in SEV_W.items()}, **{f"categoryWeight_{k}": v for k, v in CAT_W.items()},
           "bandLowBelow": 0.25, "bandModerateBelow": 0.50, "bandHighBelow": 0.75, "weakEvidenceCap": WEAK_CAP,
           "lowCoverageBelow": LOW_COVERAGE_BELOW, "expectedAnalyzers": ",".join(EXPECTED_ANALYZERS)}


def effective_statuses(reported):
    """Reported statuses plus UNAVAILABLE for every expected analyzer the document did not report (a family whose reported
    members are all NOT_APPLICABLE does not apply). Mirrors backend/src/engine/priorityEngine.ts effectiveStatuses."""
    statuses, missing = list(reported.values()), []
    for aid in EXPECTED_ANALYZERS:
        if aid in reported:
            if reported[aid] in ("UNAVAILABLE", "ERROR"):
                missing.append(aid)
            continue
        siblings = [st for other, st in reported.items() if other.split(".")[0] == aid.split(".")[0]]
        if siblings and all(st == "NOT_APPLICABLE" for st in siblings):
            statuses.append("NOT_APPLICABLE")
            continue
        statuses.append("UNAVAILABLE")
        missing.append(aid)
    return statuses, sorted(missing)


def band(score):
    for lim, name in BANDS:
        if score < lim:
            return name
    return "critical"


def priority(findings, analyzers_status, missing=()):
    by_cat = {}
    for f in findings:
        by_cat.setdefault(f["category"], []).append(f)
    comb = 1.0
    for cat, fs in by_cat.items():
        prod = 1.0
        for f in fs:
            prod *= 1 - SEV_W[f["severity"]] * f["confidence"]
        s = 1 - prod
        if all(f["severity"] in ("info", "low") for f in fs):
            s = min(s, WEAK_CAP)
        comb *= 1 - CAT_W[cat] * s
    score = round(1 - comb, 3) if findings else 0.0
    wsum = sum(SEV_W[f["severity"]] for f in findings)
    conf = round(sum(SEV_W[f["severity"]] * f["confidence"] for f in findings) / wsum, 2) if findings else 0.0
    ran = sum(1 for s in analyzers_status if s == "AVAILABLE")
    expected = sum(1 for s in analyzers_status if s in ("AVAILABLE", "UNAVAILABLE", "ERROR"))
    coverage = round(ran / expected, 3) if expected else 0.0
    out = {"priorityBand": band(score), "score": score, "confidence": conf, "coverage": coverage,
           "contributingFindingIds": [f["findingId"] for f in findings]}
    if missing:
        out["missingAnalyzers"] = list(missing)
    if coverage < LOW_COVERAGE_BELOW:
        out["incompleteCoverage"] = True
    return out


def finding_docs(f):
    return {r["docId"] for e in f["evidence"] for r in e["regions"]}


def build_priority(case, doc_ids):
    fs = FINDINGS[case]
    per_doc, allst, allmiss = {}, [], set()
    for did in doc_ids:
        statuses, missing = effective_statuses(built[did]["an"])
        per_doc[did] = priority([f for f in fs if did in finding_docs(f)], statuses, missing)
        allst += statuses
        allmiss.update(missing)
    return {"perCase": priority(fs, allst, sorted(allmiss)), "perDocument": per_doc,
            "provenance": prov("priorityEngine", "AVAILABLE", case_sha(doc_ids), PRI_CFG)}


CASE_DOCS = {"CASE-001": ["D-01", "D-02", "D-03"], "CASE-002": ["D-11", "D-12", "D-13"]}
for case, docs in CASE_DOCS.items():
    out = {"assertions": ASSERT[case], "entities": ENTITIES[case], "findings": FINDINGS[case],
           "events": [EV1, EV2] if case == "CASE-001" else [], "priority": build_priority(case, docs)}
    (HERE / f"{case}.reasoning.json").write_text(json.dumps(out, indent=2) + "\n")

# --------------------------------------------------------------------------------------------- case bundles (documents, jobs, log)
def job_for(d, n):
    an = d["an"]
    prog = {}
    for a, s in an.items():
        if s == "AVAILABLE":
            prog[a] = {"state": "DONE", "analyzerStatus": s, "startedAt": "2026-11-01T09:00:05Z", "finishedAt": "2026-11-01T09:00:20Z"}
        elif s == "NOT_APPLICABLE":
            prog[a] = {"state": "NOT_APPLICABLE", "analyzerStatus": s}
        else:
            prog[a] = {"state": s if s in ("UNAVAILABLE", "ERROR") else "ERROR", "analyzerStatus": s, "startedAt": "2026-11-01T09:00:05Z",
                       "finishedAt": "2026-11-01T09:00:06Z", "message": "signature model not loaded (fixture: honest UNAVAILABLE, not clean)"}
    partial = any(s in ("UNAVAILABLE", "ERROR") for s in an.values())
    return {"jobId": f"JOB-{n:04d}", "caseId": d["caseId"], "docId": d["docId"], "pipelineVersion": PIPELINE, "status": "PARTIAL" if partial else "DONE",
            "analyzers": prog, "attempts": 1, "maxAttempts": 3, "createdAt": "2026-11-01T08:55:30Z", "updatedAt": "2026-11-01T09:00:25Z"}


def inv(case, n, at, actor, typ, target, payload):
    return {"eventId": f"IE-{case[-3:]}-{n:03d}", "caseId": case, "at": at, "actor": actor, "type": typ, "targetId": target, "payload": payload}


for case, docs in CASE_DOCS.items():
    dl = [built[i] for i in docs]
    log = [inv(case, k + 1, f"2026-11-01T08:55:{10 + k:02d}Z", "analyst-1", "UPLOAD", d["docId"], {"filename": d["filename"], "sha256": d["sha256"]}) for k, d in enumerate(dl)]
    if case == "CASE-001":
        log += [inv(case, 4, "2026-11-01T09:10:00Z", "analyst-1", "REVIEW", "F-0001", {"status": "CONFIRMED"}),
                inv(case, 5, "2026-11-01T09:11:00Z", "analyst-1", "COMMENT", "F-0001", {"text": "Year differs on the two documents; asking the applicant for the original."}),
                inv(case, 6, "2026-11-01T09:12:00Z", "analyst-1", "REVIEW", "F-0002", {"status": "DISMISSED", "reason": "Middle name omitted on the form"})]
    bundle = {"case": {"caseId": case, "title": "Loan application, Aarav Sharma" if case == "CASE-001" else "Loan application with guarantor, Priya Nair",
                       "createdAt": "2026-11-01T08:50:00Z", "createdBy": "analyst-1"},
              "documents": [{"docId": d["docId"], "caseId": case, "filename": d["filename"], "sha256": d["sha256"], "mimeType": d["mime"], "pageCount": 1,
                             "processing": d["an"], "pipelineVersion": PIPELINE, "receivedAt": f"2026-11-01T08:55:{10 + k:02d}Z"} for k, d in enumerate(dl)],
              "jobs": [job_for(d, n) for n, d in enumerate(dl, 1 if case == "CASE-001" else 4)], "investigationEvents": log}
    (HERE / f"{case}.case.json").write_text(json.dumps(bundle, indent=2) + "\n")

# --------------------------------------------------------------------------------------------- graphs
def graph(case, docs, matches, conflicts, suggested):
    nodes = [{"id": d, "kind": "document", "label": f"{d} {built[d]['docType']}", "docType": built[d]["docType"]} for d in docs]
    nodes += [{"id": e["entityId"], "kind": "entity", "label": f"{e['role']} ({e['entityId']})", "role": e["role"], "resolution": e["resolution"]} for e in ENTITIES[case]]
    edges, n = [], 0
    for e in ENTITIES[case]:
        for did in sorted({A[a]["docId"] for a in e["assertionIds"]}):
            n += 1
            edges.append({"id": f"G-{n:03d}", "source": e["entityId"], "target": did, "kind": "contains"})
    for s, t, label in matches:
        n += 1
        edges.append({"id": f"G-{n:03d}", "source": s, "target": t, "kind": "match", "label": label})
    for s, t, fid, label in conflicts:
        n += 1
        edges.append({"id": f"G-{n:03d}", "source": s, "target": t, "kind": "conflict", "findingId": fid, "label": label})
    for s, t, label in suggested:
        n += 1
        edges.append({"id": f"G-{n:03d}", "source": s, "target": t, "kind": "suggested", "label": label})
    return {"caseId": case, "nodes": nodes, "edges": edges}


g1 = graph("CASE-001", CASE_DOCS["CASE-001"], [("D-01", "D-02", "id_number")],
           [("D-01", "D-02", "F-0001", "DOB_CONFLICT"), ("D-01", "D-02", "F-0002", "NAME_VARIANT_SPELLING"), ("D-02", "D-03", "F-0003", "NAME_VARIANT_SPELLING (capped)")],
           [("E-002", "E-001", "name similarity only")])
g2 = graph("CASE-002", CASE_DOCS["CASE-002"], [("D-11", "D-13", "id_number"), ("D-12", "D-13", "guarantor_id_number")], [], [])
(HERE / "CASE-001.graph.json").write_text(json.dumps(g1, indent=2) + "\n")
(HERE / "CASE-002.graph.json").write_text(json.dumps(g2, indent=2) + "\n")

# --------------------------------------------------------------------------------------------- expected outcomes
c2 = "CASE-002"
expected_findings = {
    c1: [
        {"expectedId": "EF-001", "type": "DOB_CONFLICT", "category": "cross_document", "observationStatus": "INCONSISTENT", "severity": "high",
         "assertionIds": [dob1, dob2], "ruleId": "R-DOB-MUST-AGREE", "notes": "Same resolved applicant, ID card vs form, year differs (not a day/month swap)"},
        {"expectedId": "EF-002", "type": "NAME_VARIANT_SPELLING", "category": "cross_document", "observationStatus": "INCONSISTENT", "severity": "low",
         "assertionIds": [nm1, nm2], "ruleId": "R-NAME-MUST-AGREE", "notes": "Token-sort ratio 80: middle name missing on the form, between 75 and 92"},
        {"expectedId": "EF-003", "type": "NAME_VARIANT_SPELLING", "category": "cross_document", "observationStatus": "INCONSISTENT", "severity": "low",
         "assertionIds": [nm2, nm3], "ruleId": "R-NAME-MUST-AGREE", "maxConfidence": 0.3,
         "notes": "Account holder E-002 is only a SUGGESTED link to the applicant, so the finding must be capped at low confidence and say so"},
        {"expectedId": "EF-004", "type": "STATEMENT_TOTAL_MISMATCH", "category": "context", "observationStatus": "INCONSISTENT", "severity": "medium",
         "assertionIds": [tot_id] + txn_ids, "ruleId": "R-STMT-TOTAL-ARITH", "notes": "Printed total 49,562.75 vs sum of deposits 48,562.75"},
        {"expectedId": "EF-005", "type": "REGION_EVENT", "category": "visual", "observationStatus": "POTENTIAL_ANOMALY", "severity": "medium",
         "signalIds": ["D-02-S01", "D-02-S02"], "notes": "ELA and noise signals overlap on the requested amount region: ONE event, not two findings"},
        {"expectedId": "EF-006", "type": "TEXT_LAYER_MISMATCH", "category": "pdf_structure", "observationStatus": "POTENTIAL_ANOMALY", "severity": "medium",
         "signalIds": ["D-03-S01"], "notes": "Rendered total differs from the PDF text layer"},
    ],
    c2: [],
}
expected_non = {
    c1: [
        {"nonFindingId": "NF-001", "description": "Permanent address (D-01) vs current address (D-02) differ legitimately",
         "assertionIds": [aid(c1, "D-01", "address_permanent"), aid(c1, "D-02", "address_current")], "forbiddenTypes": ["*"], "ruleId": "R-ADDR-MAY-DIFFER",
         "reason": "MAY_DIFFER: permanent and current address are different concepts"},
        {"nonFindingId": "NF-002", "description": "Requested loan amount (D-02) vs account balance (D-03)",
         "assertionIds": [aid(c1, "D-02", "requested_amount"), aid(c1, "D-03", "account_balance")], "forbiddenTypes": ["*"], "ruleId": "R-AMOUNT-NOT-COMPARABLE",
         "reason": "NOT_COMPARABLE: a request is not a balance"},
        {"nonFindingId": "NF-003", "description": "id_number agrees between D-01 and D-02",
         "assertionIds": [aid(c1, "D-01", "id_number"), aid(c1, "D-02", "id_number")], "forbiddenTypes": ["*"], "ruleId": "R-IDNUM-MUST-AGREE",
         "reason": "Values are identical after normalization"},
        {"nonFindingId": "NF-004", "description": "Account holder name (D-03) agrees with the ID card name (D-01)",
         "assertionIds": [nm1, nm3], "forbiddenTypes": ["*"], "ruleId": "R-NAME-MUST-AGREE", "reason": "Token-sort ratio 100 after normalization"},
        {"nonFindingId": "NF-005", "description": "Permanent address agrees between D-01 and D-02",
         "assertionIds": [aid(c1, "D-01", "address_permanent"), aid(c1, "D-02", "address_permanent")], "forbiddenTypes": ["*"], "ruleId": "R-ADDR-MUST-AGREE",
         "reason": "Identical text"},
    ],
    c2: [
        {"nonFindingId": "NF-101", "description": "Applicant name (D-11) vs guarantor name (D-12) look similar",
         "assertionIds": [aid(c2, "D-11", "full_name"), aid(c2, "D-12", "full_name")], "forbiddenTypes": ["*"], "ruleId": "R-NAME-MUST-AGREE",
         "reason": "Different entities (different id_number): never compared, never merged"},
        {"nonFindingId": "NF-102", "description": "Applicant DOB (D-11) vs guarantor DOB (D-12) differ",
         "assertionIds": [aid(c2, "D-11", "dob"), aid(c2, "D-12", "dob")], "forbiddenTypes": ["*"], "ruleId": "R-DOB-MUST-AGREE",
         "reason": "Different people"},
        {"nonFindingId": "NF-103", "description": "id_number uniqueness between applicant and guarantor",
         "assertionIds": [aid(c2, "D-11", "id_number"), aid(c2, "D-12", "id_number")], "forbiddenTypes": ["*"], "ruleId": "R-ID-UNIQUENESS",
         "reason": "Different id_numbers: nothing is shared"},
    ],
}
(HERE / "expected_findings.json").write_text(json.dumps(expected_findings, indent=2) + "\n")
(HERE / "expected_non_findings.json").write_text(json.dumps(expected_non, indent=2) + "\n")

for case in CASE_DOCS:
    p = json.loads((HERE / f"{case}.reasoning.json").read_text())["priority"]
    print(case, "case priority", p["perCase"], {k: v["priorityBand"] for k, v in p["perDocument"].items()})
print("token sort ratios", round(ratio12, 1), round(ratio23, 1), round(ratio13, 1))
