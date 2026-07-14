"""
auto_detect.py -- pulls the company name and fiscal year label straight out
of a report PDF, so batch_extract.py doesn't need them passed in by hand.

Detection uses multiple fallback strategies because report wording varies
year to year (audit opinion phrasing isn't perfectly standardized across a
company's own filings, let alone across companies):

  Company name:
    1. PDF title/subject/author metadata (most robust -- carries the company
       name basically always, regardless of report wording)
    2. The audit opinion paragraph: "...Financial Statements of <Company
       Name> ("the Company")..." (tolerant of standalone/consolidated/Ind AS
       wording variants)
    3. Most frequently repeated "X Limited"/"X Ltd" pattern on the cover
       pages (first 5 pages) -- last resort

  Fiscal year: anchored on the actual primary balance sheet page (must
  contain "total assets" AND "as at" AND "March 31, YYYY"), not just any
  page that happens to mention a date -- avoids picking up a stray
  five-year-highlights table or an unrelated schedule.
"""

import re
import os
from collections import Counter


# Entities that legitimately show up in report text/metadata but are never
# the reporting company itself:
#   - Certifying Authorities (NSDL, CDSL, e-Mudhra, etc.) whose name gets
#     stamped into Title/Subject/Author when the PDF was digitally signed.
#   - Depositories (NSDL/CDSL again) that show up in the "Corporate
#     Information" / ISIN-disclosure boilerplate on the first couple of
#     cover pages, which is exactly what Strategy 3 scans -- so this list
#     has to be checked there too, not just against metadata.
COMPANY_BLACKLIST = {
    "national securities depository limited",
    "central depository services limited",
    "central depository services (india) limited",
    "nsdl e-governance infrastructure limited",
    "e-mudhra limited",
    "(n)code solutions",
}

COMPANY_NAME_PATTERN = re.compile(
    r'(?:audited the (?:accompanying\s+)?(?:standalone\s+|consolidated\s+)?(?:Ind\s*AS\s+)?[Ff]inancial [Ss]tatements of'
    r'|to the [Mm]embers of)'
    # Bounded to {1,30}/{1,40} chars per chunk, max 7 chunks -- a real
    # company name is short and single-line; without this bound, the
    # non-greedy '+?' will cross an entire boilerplate paragraph to reach
    # the NEXT unrelated "Limited"/"Ltd" if the true company name isn't the
    # first one mentioned after "of".
    r'\s+([A-Z][A-Za-z0-9&.,\'\-]{1,30}?(?:\s[A-Za-z0-9&.,\'\-]{1,40}?){0,6}\s(?:Limited|Ltd\.?))\b',
    re.IGNORECASE,
)
COVER_NAME_PATTERN = re.compile(
    r"([A-Z][A-Za-z]+"
    r"(?:\s+(?:and|of|the)\b|\s+&|\s+[A-Z][A-Za-z]+){0,5}"
    r"\s(?:Limited|Ltd\.?))"
)
METADATA_NAME_PATTERN = re.compile(r"([A-Z][A-Za-z0-9&.,'\-\s]{2,60}?\s(?:Limited|Ltd\.?))")
FY_END_PATTERN = re.compile(r'March 31,\s*(\d{4})')

# Filename-based detection: '<CompanyToken>_FY<YY>.pdf' (e.g. 'TCS_FY22.pdf').
# When a pipeline's PDFs are named this way, the filename is a far more
# reliable signal than PDF metadata or audit-opinion wording -- whoever named
# the file already knows unambiguously which company and year it is, so
# there's no year-to-year wording variance to chase. batch_extract.py tries
# this FIRST and only falls back to detect_company_name()/detect_fiscal_year()
# on the PDF content for files that don't follow the convention.
FILENAME_PATTERN = re.compile(r'^([A-Za-z0-9&\-]+?)_FY(\d{2,4})(?:[_\-].*)?$', re.IGNORECASE)


def detect_from_filename(filename):
    """
    Parses '<CompanyToken>_FY<YY>.pdf' into (company_token, fy_label,
    report_label). Returns (None, None, None) if the filename doesn't match
    -- callers must fall back to the PDF-content strategies in that case,
    not assume every file is named this way.
    """
    base = os.path.splitext(os.path.basename(filename))[0]
    m = FILENAME_PATTERN.match(base)
    if not m:
        return None, None, None
    company_token, yy = m.group(1), m.group(2)
    yy2 = yy[-2:]
    latest_year = 2000 + int(yy2)  # assumes 21st century; fine for this pipeline's era
    fy_label = f"FY{yy2}"
    report_label = f"FY{latest_year - 1}-{yy2} Annual Report"
    return company_token, fy_label, report_label


def detect_company_name(pdf, scan_pages=250, verbose=False):
    # Strategy 1: PDF metadata (Title/Subject/Author often carry the name
    # regardless of how the audit report happens to be worded that year)
    meta = pdf.metadata or {}
    for key in ("Title", "Subject", "Author"):
        raw = (meta.get(key) or "").strip()
        m = METADATA_NAME_PATTERN.search(raw)
        if m:
            candidate = " ".join(m.group(1).split())
            if candidate.lower() in COMPANY_BLACKLIST:
                if verbose:
                    print(f"    ignoring PDF metadata[{key}]={raw!r} -- matches known "
                          f"certifying-authority/signing-entity name, not a report company")
                continue
            if verbose:
                print(f"    company detected via PDF metadata[{key}]: {raw!r}")
            return candidate

    # Strategy 2: audit opinion paragraph
    for i in range(min(scan_pages, len(pdf.pages))):
        text = pdf.pages[i].extract_text() or ""
        m = COMPANY_NAME_PATTERN.search(text)
        if m:
            candidate = " ".join(m.group(1).split())
            if candidate.lower() in COMPANY_BLACKLIST:
                if verbose:
                    print(f"    ignoring audit-opinion match {candidate!r} on page {i+1} -- "
                          f"matches known non-company entity")
                continue
            if verbose:
                print(f"    company detected via audit opinion, page {i+1}: {candidate!r}")
            return candidate
        elif "financial statements of" in text.lower() or "to the members of" in text.lower():
            # TEMP DEBUG: the anchor phrase is on this page but the regex
            # still didn't match -- print the surrounding text so we can see
            # the actual wording instead of guessing at another variant.
            low = text.lower()
            idx = low.find("financial statements of")
            if idx == -1:
                idx = low.find("to the members of")
            print(f"    [DEBUG] page {i+1}: anchor phrase found but regex did not match. Context:")
            print("    " + repr(text[max(0, idx-20):idx+150]))

    # Strategy 3: most frequently repeated "X Limited" on the cover pages
    counter = Counter()
    for i in range(min(5, len(pdf.pages))):
        text = pdf.pages[i].extract_text() or ""
        for m in COVER_NAME_PATTERN.finditer(text):
            name = " ".join(m.group(1).split())
            if name.lower() in COMPANY_BLACKLIST:
                continue  # e.g. NSDL/CDSL depository disclosure on the corporate-info page
            counter[name] += 1
    if counter:
        name, _ = counter.most_common(1)[0]
        if verbose:
            print(f"    company detected via cover-page frequency: {name!r}")
        return name

    return None


def detect_fiscal_year(pdf, scan_pages=350, verbose=False):
    n = min(scan_pages, len(pdf.pages))
    for i in range(n):
        text = pdf.pages[i].extract_text() or ""
        low = text.lower()
        # Anchor on the real primary balance sheet: must have the "total
        # assets" subtotal AND an "as at March 31, YYYY" style header --
        # this rules out five-year-highlights tables and other incidental
        # date mentions that showed up earlier in the document.
        if "total assets" in low and "as at" in low and "march 31" in low:
            years = [int(y) for y in FY_END_PATTERN.findall(text)]
            if years:
                latest_year = max(years)
                fy_label = f"FY{str(latest_year)[-2:]}"
                report_label = f"FY{latest_year - 1}-{str(latest_year)[-2:]} Annual Report"
                if verbose:
                    print(f"    fiscal year detected on page {i+1}: {fy_label} (years seen: {sorted(years)})")
                return fy_label, report_label
    return None, None