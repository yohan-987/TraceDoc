"""Fictional OCR pages with every extractable field, and the values a reader would expect from them.

The expected values below are authored together with the pages (they are the ground truth of these SYNTHETIC pages only).
They are not the project's samples/manifest.json, which is the source of truth for the dev samples.
"""
from tests.classifier_pages import ocr_page

ID_LINES = ["REPUBLIC OF EXAMPLELAND", "NATIONAL IDENTITY CARD", "Full name: JANE ALICE SAMPLE",
            "Date of birth: 12/08/1990", "ID number: 000-FICTIONAL-01", "Permanent address: 12 Fictional Road",
            "Example Town 000000", "Nationality: EXAMPLELANDER"]
ID_EXPECTED = {"full_name": "JANE ALICE SAMPLE", "dob": "1990-08-12", "id_number": "000-FICTIONAL-01",
               "address_permanent": "12 Fictional Road Example Town 000000"}

FORM_LINES = ["LOAN APPLICATION FORM", "Full name of applicant: John Q Sample", "Date of birth: 01/02/1985",
              "Permanent address: 12 Fictional Road", "Example Town 000000", "Current address: 7 Sample Street",
              "Sample City 111111", "Requested amount: 250,000.00", "Signature: ________"]
FORM_EXPECTED = {"full_name": "John Q Sample", "dob": "1985-02-01",
                 "address_permanent": "12 Fictional Road Example Town 000000",
                 "address_current": "7 Sample Street Sample City 111111", "requested_amount": "250000.00"}  # + signature region


def _row(date, desc, debit, credit, balance):
    cells = [(date, 80), (desc, 250)]
    if debit:
        cells.append((debit, 700))
    if credit:
        cells.append((credit, 850))
    cells.append((balance, 1000))
    return cells


STATEMENT_LINES = ["SAMPLE BANK OF EXAMPLELAND", "ACCOUNT STATEMENT", "Account holder: JANE ALICE SAMPLE",
                   "Account number: 0000 1234 5678", "Statement period: 01/09/2026 to 30/09/2026",
                   "Opening balance: 1,250.00",
                   [("Date", 80), ("Description", 250), ("Debit", 700), ("Credit", 850), ("Balance", 1000)],
                   _row("02/09/2026", "Salary credit", "", "2,400.00", "3,650.00"),
                   _row("05/09/2026", "Rent payment", "800.00", "", "2,850.00"),
                   _row("12/09/2026", "Grocery store", "120.50", "", "2,729.50"),
                   "Closing balance: 2,733.70", "Total 3,324.70"]
STATEMENT_EXPECTED = {"account_holder": "JANE ALICE SAMPLE", "account_number": "000012345678",
                      "opening_balance": "1250.00", "closing_balance": "2733.70", "total": "3324.70"}


def id_card(lines=None, **kw):
    return ocr_page(lines or ID_LINES, width=1011, height=638, **kw)


def application_form(lines=None, **kw):
    return ocr_page(lines or FORM_LINES, **kw)


def bank_statement(lines=None, **kw):
    return ocr_page(lines or STATEMENT_LINES, **kw)


def replace_line(lines, startswith, new):
    return [new if isinstance(ln, str) and ln.startswith(startswith) else ln for ln in lines]


def without_line(lines, startswith):
    return [ln for ln in lines if not (isinstance(ln, str) and ln.startswith(startswith))]
