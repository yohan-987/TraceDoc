"""Keyword lexicon for the page classifier (fictional-document vocabulary; no ID formats of any country).

Each rule is (label, regex, weight, kind). ``kind`` is "title" for phrases that name the document type and
"kw" for supporting vocabulary. Weights are raw points; app/classify.py turns them into 0..1 scores using the
thresholds in config/classifier.json. Patterns run on lower-cased OCR text with word boundaries.
"""
from __future__ import annotations

from typing import Dict, List, Tuple

LEXICON_VERSION = "1"

Rule = Tuple[str, str, float, str]

CLASSES = ("id_card", "application_form", "bank_statement", "other")

LEXICON: Dict[str, List[Rule]] = {
    "id_card": [
        ("identity card", r"identity\s+card|identification\s+card|national\s+id(?:entity)?|id\s+card", 4.0, "title"),
        ("republic of", r"republic\s+of|government\s+of|kingdom\s+of", 1.0, "kw"),
        ("date of birth", r"date\s+of\s+birth|birth\s+date|dob", 2.0, "kw"),
        ("id number", r"id\s+(?:number|no)|identity\s+(?:number|no)|card\s+(?:number|no)|document\s+(?:number|no)", 2.5, "kw"),
        ("surname", r"surname|family\s+name", 1.5, "kw"),
        ("given names", r"given\s+names?", 1.5, "kw"),
        ("nationality", r"nationality|citizenship", 2.0, "kw"),
        ("expiry", r"date\s+of\s+expiry|expiry\s+date|expires?|valid\s+(?:until|thru|through)", 2.0, "kw"),
        ("issue", r"date\s+of\s+issue|issue\s+date|issued\s+(?:on|by)|issuing\s+authority", 1.5, "kw"),
        ("place of birth", r"place\s+of\s+birth", 1.5, "kw"),
        ("sex", r"sex|gender", 0.8, "kw"),
        ("holder", r"holder(?:'s)?\s+signature|signature\s+of\s+(?:the\s+)?holder|bearer", 1.2, "kw"),
    ],
    "application_form": [
        ("application form", r"application\s+form|loan\s+application|application\s+for|account\s+opening\s+form|application\s+(?:no|number)", 4.0, "title"),
        ("applicant", r"applicant|co-?applicant|guarantor", 2.0, "kw"),
        ("full name", r"full\s+name|name\s+of\s+(?:the\s+)?applicant", 1.5, "kw"),
        ("address", r"(?:permanent|current|residential|mailing)\s+address|address", 1.2, "kw"),
        ("date of birth", r"date\s+of\s+birth|birth\s+date|dob", 1.5, "kw"),
        ("employment", r"occupation|employer|designation|monthly\s+income|annual\s+income|employment", 1.5, "kw"),
        ("loan request", r"requested\s+amount|amount\s+requested|loan\s+amount|purpose\s+of\s+(?:the\s+)?loan|tenure", 2.5, "kw"),
        ("marital status", r"marital\s+status", 1.5, "kw"),
        ("contact", r"phone|mobile|e-?mail|contact\s+(?:number|no)", 0.7, "kw"),
        ("declaration", r"declaration|i\s+hereby\s+declare|i\s+declare\s+that|true\s+and\s+correct", 2.5, "kw"),
        ("signature", r"signature|signed|date\s+of\s+signing", 1.2, "kw"),
        ("form instructions", r"please\s+(?:tick|select|fill|print)|block\s+letters|for\s+office\s+use(?:\s+only)?", 1.5, "kw"),
    ],
    "bank_statement": [
        ("account statement", r"account\s+statement|bank\s+statement|statement\s+of\s+account|statement\s+period|statement\s+date", 4.0, "title"),
        ("account number", r"account\s+(?:number|no)|a/c\s+(?:number|no)", 2.5, "kw"),
        ("balance lines", r"opening\s+balance|closing\s+balance|balance\s+brought\s+forward|balance\s+carried\s+forward|available\s+balance|ledger\s+balance", 3.0, "kw"),
        ("balance", r"balance", 1.0, "kw"),
        ("debit", r"debits?|withdrawals?", 1.5, "kw"),
        ("credit", r"credits?|deposits?", 1.2, "kw"),
        ("transaction", r"transactions?|txn", 1.5, "kw"),
        ("table header", r"narration|particulars|description", 0.8, "kw"),
        ("branch", r"branch", 0.8, "kw"),
        ("reference", r"cheque|chq|ref(?:erence)?\s+(?:no|number)", 0.8, "kw"),
    ],
    "other": [
        ("invoice", r"tax\s+invoice|invoice|bill\s+to|invoice\s+(?:no|number)", 3.5, "title"),
        ("receipt", r"receipt|payment\s+received", 3.0, "title"),
        ("letter", r"dear\s+(?:sir|madam|mr|ms|mrs)|sincerely|yours\s+faithfully|yours\s+sincerely|to\s+whom\s+it\s+may\s+concern", 3.0, "kw"),
        ("certificate", r"certificate|this\s+is\s+to\s+certify|certify\s+that", 3.5, "title"),
        ("utility bill", r"utility\s+bill|electricity\s+bill|water\s+bill|meter\s+reading|units\s+consumed", 3.0, "title"),
        ("amount due", r"amount\s+due|due\s+date|total\s+due|please\s+pay", 2.0, "kw"),
        ("agreement", r"agreement|terms\s+and\s+conditions|hereinafter|witness\s+whereof", 3.0, "title"),
        ("order", r"purchase\s+order|quotation|delivery\s+note|packing\s+slip", 3.0, "title"),
        ("memo", r"minutes\s+of\s+(?:the\s+)?meeting|agenda|memorandum|\bmemo\b", 2.5, "kw"),
        ("quantity", r"quantity|qty|unit\s+price|subtotal|sub-total", 1.5, "kw"),
    ],
}

# Header words of a transaction table; a line with two or more of these counts as a table header row.
TABLE_HEADER_TERMS = (
    r"date", r"description|narration|particulars|details", r"debits?|withdrawals?", r"credits?|deposits?", r"balance",
)
