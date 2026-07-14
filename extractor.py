"""
extractor.py -- pulls tagged financial line items out of an Ind-AS format
annual report PDF, with a source page citation attached to every number.

Usage:
    python extractor.py <path_to_pdf> <company_name> <fiscal_year_label> <out_json>

Design notes:
- Statement pages are FOUND by keyword anchors (config.STATEMENT_ANCHORS),
  not hardcoded page numbers -- so this should generalize to other
  Ind-AS-format annual reports (most BSE/NSE-listed Indian companies follow
  this layout for standalone + consolidated financials).
- Each page is tagged "standalone" or "consolidated" by tracking which scope
  divider was most recently seen while scanning forward through the whole
  document -- not by searching a fixed window of nearby pages, since the gap
  between a scope divider and the actual statement page (e.g. across the
  Independent Auditor's Report) can be much larger than a few pages.
- Every extracted number carries: value, pdf_page (1-indexed), printed_page
  (the page number printed on the page itself, if found), and statement type.
- reconcile_liabilities_columns() self-corrects an isolated current/prior-year
  column reversal on the liabilities page, detected via the accounting
  identity (Assets = Equity + Liabilities) rather than a hardcoded direction
  -- see its docstring for why a global swap assumption would be unsafe.
"""

import sys
import json
import re
import copy
import pdfplumber
from config import (STATEMENT_ANCHORS, STATEMENT_SCOPE_MARKERS, LINE_ITEMS_COMPILED,
                    BANK_STATEMENT_ANCHORS, BANK_LINE_ITEMS_COMPILED)


AUDITOR_PATTERN = re.compile(
    r"for\s+([A-Z][A-Za-z&.,]+(?:\s+[A-Za-z&.,]+){0,4}?\s+(?:LLP|& Co\.?|& Associates|& Company))\b"
)


def extract_auditor_name(pdf, scan_pages=None):
    """Scans the whole doc (or first `scan_pages`) for the statutory auditor's
    signature line, e.g. 'for Deloitte Haskins & Sells LLP\nChartered Accountants'."""
    n = scan_pages or len(pdf.pages)
    for i in range(min(n, len(pdf.pages))):
        text = pdf.pages[i].extract_text() or ""
        m = AUDITOR_PATTERN.search(text)
        if m:
            return " ".join(m.group(1).split())
    return None


def clean_number(raw):
    """'1,234' -> 1234.0 ; '(1,234)' -> -1234.0 ; '-' or '^' -> None"""
    if raw is None:
        return None
    s = raw.strip()
    if s in ("-", "^", ""):
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace(",", "")
    try:
        val = float(s)
    except ValueError:
        return None
    return -val if neg else val


def printed_page_number(text):
    """Wipro-style reports print a page number as the first standalone line."""
    for line in text.split("\n"):
        line = line.strip()
        if line.isdigit() and 1 <= len(line) <= 4:
            return line
    return None


def locate_statement_pages(pdf, verbose=False, anchors=None):
    anchors = anchors if anchors is not None else STATEMENT_ANCHORS
    """
    Scan every page once, tracking two things simultaneously:
      1. which statement type(s) match on this page (STATEMENT_ANCHORS)
      2. which scope (standalone/consolidated) is currently in effect

    Scope is tracked as running state carried FORWARD across the whole
    document, rather than searched in a fixed window around each anchor page.
    Real annual reports declare a scope with a section divider/header
    ("Standalone Financial Statements", "Consolidated Financial Statements")
    that holds for many pages afterward -- until the next divider changes it
    -- it is not a page-local property. A fixed window search breaks as soon
    as the gap between the divider and the actual statement page exceeds the
    window, which happens routinely once the Independent Auditor's Report
    (often 10-20 pages) sits between them. Tracking scope as forward-
    persisting state has no such distance limit, and also naturally handles
    reports where the marker text repeats as a running header on every page
    of that section.

    Only the FIRST matching page per (statement_type, scope) is kept. Anchor
    phrases like "total assets" or "revenue from operations" can also appear
    later in segment-reporting notes or subsidiary schedules -- the primary
    financial statements always appear before the notes in report order, so
    first-match-wins reliably picks the real statement page over a note. It
    also gives some protection against a five-year-highlights table earlier
    in the document: those sit in the front matter, before any scope divider
    has been seen, so they carry scope "unknown" and get skipped below --
    same as a page with no scope declared ever was under the old logic.
    """
    located = {}  # statement_type -> {scope: page_idx}
    current_scope = "unknown"
    for i, page in enumerate(pdf.pages):
        text = page.extract_text() or ""
        low = text.lower()

        for scope, markers in STATEMENT_SCOPE_MARKERS.items():
            if any(m in low for m in markers):
                current_scope = scope
                break  # a page declaring both scopes at once isn't a real layout; first match wins

        if current_scope not in ("standalone", "consolidated"):
            continue

        for stype, keywords in anchors.items():
            if all(kw in low for kw in keywords):
                located.setdefault(stype, {})
                if current_scope not in located[stype]:
                    located[stype][current_scope] = i
                    if verbose:
                        print(f"    [DEBUG] {stype} ({current_scope}): FIRST match, using page {i+1}")
                elif verbose:
                    print(f"    [DEBUG] {stype} ({current_scope}): also matches on page {i+1}, "
                          f"but page {located[stype][current_scope]+1} already won (first-match-wins)")
    return {
        stype: [(page_idx, scope) for scope, page_idx in scopes.items()]
        for stype, scopes in located.items()
    }


def extract_line_items(pdf, page_idx, stype, company, fy_label, scope, verbose=False, items_compiled=None):
    items_compiled = items_compiled if items_compiled is not None else LINE_ITEMS_COMPILED
    text = pdf.pages[page_idx].extract_text() or ""
    # Some older-format PDFs (confirmed: WIPRO_FY17) render the ₹ glyph as a
    # stray backtick due to a font-subset mapping quirk. This only shows up
    # on TOTAL/subtotal rows and section-opening lines (standard Indian
    # financial-statement convention prints the currency symbol once per
    # column group), which is exactly why total_assets,
    # total_equity_and_liabilities, revenue_from_operations, and
    # profit_for_the_year failed while ordinary line items on the same page
    # extracted fine. Strip both the literal glyph and its backtick
    # stand-in so the \s+ before each NUM group in config.py's patterns
    # isn't broken by a non-whitespace symbol sitting between the label and
    # the number. Safe no-op on every PDF that doesn't have this quirk.
    text = text.replace("`", " ").replace("₹", " ")
    printed = printed_page_number(text)
    patterns = items_compiled.get(stype, {})

    # Fields where a line item legitimately appears twice on a balance sheet
    # page (once as a non-current item, once as current) -- take the LAST
    # match, which corresponds to the current-assets/liabilities figure.
    LAST_MATCH_FIELDS = {"trade_receivables"}
    # Fields that split across non-current AND current sections and should be
    # SUMMED across all matches to get the true total (e.g. borrowings, or
    # MSME dues which can appear in both sections).
    SUM_MATCH_FIELDS = {"msme_dues", "trade_payables_other", "borrowings", "trade_payables_undifferentiated"}

    results = {}
    for field, pattern in patterns.items():
        matches = list(pattern.finditer(text))
        if not matches:
            continue
        if field in SUM_MATCH_FIELDS:
            # Sum whatever matches were found; fall back to None only when
            # there were NO matches for a year, not when the matches summed
            # to a genuine zero (e.g. a debt-free company's borrowings, or
            # zero MSME dues -- both real, meaningful values that should be
            # scored, not treated as "not extracted").
            cur_vals = [v for v in (clean_number(m.group(1)) for m in matches) if v is not None]
            prior_vals = [v for v in (clean_number(m.group(2)) for m in matches) if v is not None]
            cur_val = sum(cur_vals) if cur_vals else None
            prior_val = sum(prior_vals) if prior_vals else None
        else:
            m = matches[-1] if field in LAST_MATCH_FIELDS else matches[0]
            cur_val = clean_number(m.group(1))
            prior_val = clean_number(m.group(2))
            if verbose and field == "revenue_from_operations" and cur_val is not None and cur_val < 10:
                start = max(0, m.start() - 150)
                print(f"    [DEBUG] {field} on page {page_idx+1} ({scope}) resolved to a suspiciously "
                      f"tiny value {cur_val} -- likely wrong page/match, not a real revenue figure. Context:")
                print("    " + repr(text[start:m.end() + 50]))
        results[field] = {
            "current_year": cur_val,
            "prior_year": prior_val,
            "source": {
                "company": company,
                "report": fy_label,
                "scope": scope,
                "pdf_page": page_idx + 1,
                "printed_page": printed,
                "statement": stype,
            },
        }

    if verbose and stype in ("balance_sheet_assets", "balance_sheet_liab_equity"):
        expected = (("total_assets", "total_equity") if stype == "balance_sheet_assets"
                    else ("total_liabilities", "total_equity_and_liabilities"))
        missing = [f for f in expected if f not in results]
        if missing:
            print(f"    [DEBUG] {stype} page {page_idx+1} ({scope}): {missing} did not match. Page text:")
            print("    " + repr(text[:3000]))

    # Some Ind-AS layouts (confirmed for TCS, standalone and consolidated,
    # across FY22/FY23) never print an explicit "Total Liabilities" subtotal
    # line -- the page goes straight from "Total current liabilities" to
    # "TOTAL EQUITY AND LIABILITIES". When the direct regex match is absent
    # but both liabilities subtotals ARE present, derive it: this is exact
    # arithmetic the report itself omits printing, not an approximation, and
    # was verified against the accounting identity (Assets = Equity +
    # Liabilities) using TCS's own printed numbers before being added here.
    # Marked as "derived" in the source so it's distinguishable downstream
    # from a directly-extracted value.
    if ("total_liabilities" not in results
            and "total_non_current_liabilities" in results
            and "total_current_liabilities" in results):
        nca, ca = results["total_non_current_liabilities"], results["total_current_liabilities"]
        cur_val = (nca["current_year"] + ca["current_year"]
                   if nca["current_year"] is not None and ca["current_year"] is not None else None)
        prior_val = (nca["prior_year"] + ca["prior_year"]
                     if nca["prior_year"] is not None and ca["prior_year"] is not None else None)
        if cur_val is not None or prior_val is not None:
            results["total_liabilities"] = {
                "current_year": cur_val,
                "prior_year": prior_val,
                "source": {
                    "company": company,
                    "report": fy_label,
                    "scope": scope,
                    "pdf_page": page_idx + 1,
                    "printed_page": printed,
                    "statement": stype,
                    "derived": True,
                    "derived_from": ["total_non_current_liabilities", "total_current_liabilities"],
                },
            }
    return results


# Fields that live on the liabilities page and would ALL be affected the same
# way by a page-level current/prior-year column reversal (as opposed to a
# single mis-extracted field, which this deliberately does not try to fix).
LIABILITIES_PAGE_FIELDS = [
    "total_liabilities",
    "total_non_current_liabilities",
    "total_current_liabilities",
    "total_equity_and_liabilities",
]

BALANCE_SHEET_TOLERANCE_PCT = 0.005  # matches validate.py's Layer 3 tolerance


def _accounting_identity_holds(fin, tol_frac=BALANCE_SHEET_TOLERANCE_PCT):
    """
    Checks Assets = Equity + Liabilities independently for each year. Trusts
    total_assets/total_equity (from the assets page, not in question here)
    as ground truth and checks total_liabilities (from the liabilities page)
    against them. A year with a missing value isn't counted as a failure --
    this function is only meant to arbitrate between "as extracted" and
    "swapped", not to duplicate Layer 3's own missing-data flags.

    Requires at least one year to have actually been checked before
    returning True: if every year is missing a value, there is nothing to
    verify, and silently returning True in that situation would claim the
    identity holds without ever having tested it.
    """
    checked = 0
    for year in ("current_year", "prior_year"):
        assets_item = fin.get("total_assets")
        equity_item = fin.get("total_equity")
        liab_item = fin.get("total_liabilities")
        if not (assets_item and equity_item and liab_item):
            continue
        assets, equity, liab = assets_item.get(year), equity_item.get(year), liab_item.get(year)
        if assets is None or equity is None or liab is None:
            continue
        checked += 1
        if abs(assets - (equity + liab)) > tol_frac * assets:
            return False
    return checked > 0


def _liabilities_breakdown_consistent(fin, tol_frac=BALANCE_SHEET_TOLERANCE_PCT):
    """
    Secondary confidence check: current + non-current liabilities should sum
    to total_liabilities, independently of the Assets = Equity + Liabilities
    identity. This exists because reconcile_liabilities_columns swaps ALL
    liabilities-page fields together as one matched set -- which is correct
    when the whole page's columns were reversed, but would be actively wrong
    if, hypothetically, only the total_liabilities/total_equity_and_liabilities
    lines were reversed while the current/non-current breakdown lines were
    extracted correctly. In that scenario, swapping everything together could
    make the top-line identity pass by coincidence while silently corrupting
    previously-correct breakdown fields. Requiring both checks to pass before
    accepting a swap catches that case: a swap that fixes the top-line
    identity but breaks the breakdown sum is rejected rather than applied.
    Like _accounting_identity_holds, a year with missing values isn't counted
    against this check -- it just isn't informative for that year.
    """
    checked = 0
    for year in ("current_year", "prior_year"):
        nca_item = fin.get("total_non_current_liabilities")
        ca_item = fin.get("total_current_liabilities")
        total_item = fin.get("total_liabilities")
        if not (nca_item and ca_item and total_item):
            continue
        nca, ca, total = nca_item.get(year), ca_item.get(year), total_item.get(year)
        if nca is None or ca is None or total is None:
            continue
        checked += 1
        if abs((nca + ca) - total) > tol_frac * total:
            return False
    return checked > 0


def reconcile_liabilities_columns(fin):
    """
    Self-corrects an isolated current-year/prior-year column reversal on the
    liabilities page, detected via the accounting identity rather than
    assumed.

    Why not just fix the current_year/prior_year mapping in the regex or in
    extract_line_items directly? Because the SAME current_year=group(1),
    prior_year=group(2) assumption is correct on the assets page, the P&L
    page, and the cash flow page -- including in the very report where the
    liabilities page reverses it. A global fix (e.g. "swap group order")
    would repair this one page and break every other page that's currently
    extracting correctly, in this report and in every other company's
    report. The reversal is a property of how pdfplumber happened to
    flatten THIS page's table into text, not a property of the document,
    the company, or the report format in general -- so the correction has
    to be detected and applied per page, not assumed as a fixed direction.

    Approach: if the accounting identity doesn't hold as extracted, check
    whether swapping current_year <-> prior_year TOGETHER, as a matched set,
    across every liabilities-page field (not just total_liabilities) makes
    BOTH the top-line identity (Assets = Equity + Liabilities) AND the
    internal breakdown (current + non-current = total) hold. Requiring both
    guards against a swap that happens to fix the top-line number while
    corrupting a breakdown field that didn't actually need reversing (see
    _liabilities_breakdown_consistent's docstring). If so, apply the swap
    and mark the affected fields' sources as corrected, so the correction is
    visible in the output rather than silent. If neither the as-extracted
    nor the swapped version satisfies both checks, the data is left
    untouched and Layer 3 in validate.py will flag it as before -- this
    function only acts when it can confirm the fix actually resolves the
    mismatch.
    """
    present_fields = [f for f in LIABILITIES_PAGE_FIELDS if f in fin]
    if not present_fields or "total_assets" not in fin or "total_equity" not in fin:
        return fin, False  # nothing to check against

    if _accounting_identity_holds(fin):
        return fin, False  # already consistent, no correction needed

    candidate = copy.deepcopy(fin)
    for field in present_fields:
        item = candidate[field]
        item["current_year"], item["prior_year"] = item["prior_year"], item["current_year"]

    if not _accounting_identity_holds(candidate) or not _liabilities_breakdown_consistent(candidate):
        # Swapping either didn't fix the top-line identity, or "fixed" it
        # while breaking internal consistency -- not a confirmed simple
        # column reversal, so don't guess further. Leave as extracted;
        # Layer 3 will flag the mismatch as it did before this function
        # existed.
        return fin, False

    for field in present_fields:
        candidate[field]["source"]["corrected_column_swap"] = True
    return candidate, True


def diagnose_missing_anchors(pdf, anchors=("total assets", "revenue from operations", "profit for the year",
                                           "total equity and liabilities")):
    """
    For a PDF where extraction came back nearly empty (e.g. HDFC, Yes Bank),
    checks whether each anchor phrase exists ANYWHERE in the document at all
    -- not just whether locate_statement_pages() found it in the right
    place. If a phrase never appears in the whole document, that's a real
    format difference (e.g. a bank's RBI Schedule III balance sheet doesn't
    use "Total Assets" the same way an industrial company's does) and no
    amount of page-location fixing will help; if it DOES appear somewhere,
    the bug is in scope-tracking or anchor logic, not the report format.
    """
    hits = {a: 0 for a in anchors}
    for page in pdf.pages:
        low = (page.extract_text() or "").lower()
        for a in anchors:
            if a in low:
                hits[a] += 1
    print("    [DEBUG] anchor phrase presence across the whole document:")
    for a, count in hits.items():
        print(f"      {a!r}: found on {count} page(s)" if count else f"      {a!r}: NEVER found in this document")
    return hits


def derive_total_equity_if_missing(fin):
    """
    Some reports (e.g. L&T, standalone and consolidated, FY23-25) never print
    an explicit single "Total Equity" line -- equity gets broken into
    components attributable to owners/non-controlling interests without a
    combined subtotal on the face of the balance sheet. When total_equity is
    absent but total_equity_and_liabilities and total_liabilities both exist,
    derive it: Total Equity = Total Equity and Liabilities - Total
    Liabilities. This is exact arithmetic from the accounting identity
    (Assets = Equity + Liabilities), not an approximation -- the same
    reasoning already verified for the total_liabilities fallback above.
    """
    if "total_equity" in fin:
        return fin
    teal = fin.get("total_equity_and_liabilities")
    tl = fin.get("total_liabilities")
    if not (teal and tl):
        return fin
    cur = (teal["current_year"] - tl["current_year"]
           if teal["current_year"] is not None and tl["current_year"] is not None else None)
    prior = (teal["prior_year"] - tl["prior_year"]
             if teal["prior_year"] is not None and tl["prior_year"] is not None else None)
    if cur is None and prior is None:
        return fin
    fin["total_equity"] = {
        "current_year": cur,
        "prior_year": prior,
        "source": {
            **tl["source"],
            "derived": True,
            "derived_from": ["total_equity_and_liabilities", "total_liabilities"],
        },
    }
    return fin


def diagnose_bank_format(pdf, required_marker="capital and liabilities",
                         supporting_markers=("schedule", "interest earned", "interest expended")):
    """
    Banks file under RBI Schedule III (Banking Regulation Act, Form A/Form B)
    -- their balance sheet is headed "CAPITAL AND LIABILITIES", not a
    generic "schedule"/"profit and loss account" mention. Requiring that
    specific phrase avoids false positives like a table of contents, which
    lists "Profit and Loss Account" and "Schedules to the Financial
    Statements" as section titles without being the real statement.
    """
    for i, page in enumerate(pdf.pages):
        low = (page.extract_text() or "").lower()
        if required_marker not in low:
            continue
        hits = [required_marker] + [m for m in supporting_markers if m in low]
        print(f"    [DEBUG] page {i+1}: likely the real bank balance sheet/P&L "
              f"(markers found: {hits}). Page text:")
        print("    " + repr(page.extract_text()[:3000]))
        return i
    print(f"    [DEBUG] no page found containing {required_marker!r}")
    return None


def detect_company_type(pdf):
    """
    Bank vs industrial, based on the same marker that made
    diagnose_bank_format() reliable: "capital and liabilities" is specific
    to RBI Schedule III Form A and essentially never appears in an Ind-AS
    industrial company's report. No page cap -- an earlier version capped
    this at 100 pages, which missed HDFC's balance sheet entirely (it's
    around page 220 in a 400+ page report) and silently misclassified it
    as "industrial". Scanning the whole document costs one extra pass but
    is the only way to be sure, same as diagnose_bank_format().
    """
    for page in pdf.pages:
        if "capital and liabilities" in (page.extract_text() or "").lower():
            return "bank"
    return "industrial"


def derive_bank_equity_and_liabilities(fin):
    """
    Banks don't print a single "Total Equity" or "Total Liabilities" line --
    equity is Capital + Reserves and Surplus (both extracted separately,
    verified against real Yes Bank text), and liabilities is then whatever's
    left of total_assets. Same accounting-identity reasoning as the
    industrial derivations above, just built from bank-specific source
    fields.
    """
    capital = fin.get("capital")
    reserves = fin.get("reserves_and_surplus")
    if capital and reserves and "total_equity" not in fin:
        cur = (capital["current_year"] + reserves["current_year"]
               if capital["current_year"] is not None and reserves["current_year"] is not None else None)
        prior = (capital["prior_year"] + reserves["prior_year"]
                 if capital["prior_year"] is not None and reserves["prior_year"] is not None else None)
        if cur is not None or prior is not None:
            fin["total_equity"] = {
                "current_year": cur, "prior_year": prior,
                "source": {**capital["source"], "derived": True,
                          "derived_from": ["capital", "reserves_and_surplus"]},
            }

    total_assets = fin.get("total_assets")
    total_equity = fin.get("total_equity")
    if total_assets and total_equity and "total_liabilities" not in fin:
        cur = (total_assets["current_year"] - total_equity["current_year"]
               if total_assets["current_year"] is not None and total_equity["current_year"] is not None else None)
        prior = (total_assets["prior_year"] - total_equity["prior_year"]
                 if total_assets["prior_year"] is not None and total_equity["prior_year"] is not None else None)
        if cur is not None or prior is not None:
            fin["total_liabilities"] = {
                "current_year": cur, "prior_year": prior,
                "source": {**total_assets["source"], "derived": True,
                          "derived_from": ["total_assets", "total_equity"]},
            }
    return fin


def run_extraction(pdf_path, company, fy_label, verbose=False):
    with pdfplumber.open(pdf_path) as pdf:
        company_type = detect_company_type(pdf)
        if verbose:
            print(f"    [DEBUG] detected company type: {company_type}")

        if company_type == "bank":
            # NOTE: bank scope-tracking is unverified -- STATEMENT_SCOPE_MARKERS
            # looks for "standalone financial statement"/"consolidated financial
            # statement", which is standard audit-report phrasing and *should*
            # appear somewhere earlier in a bank's report too, but this hasn't
            # been confirmed against real bank auditor-report text. If bank
            # data comes back empty despite locate_statement_pages finding
            # pages, scope is likely staying "unknown" -- check that first.
            located = locate_statement_pages(pdf, verbose=verbose, anchors=BANK_STATEMENT_ANCHORS)
            items_compiled = BANK_LINE_ITEMS_COMPILED
        else:
            located = locate_statement_pages(pdf, verbose=verbose)
            items_compiled = LINE_ITEMS_COMPILED

        auditor = extract_auditor_name(pdf)

        if verbose and not located:
            diagnose_missing_anchors(pdf)

        if company_type == "industrial":
            core_stypes_missing = [s for s in ("profit_and_loss", "balance_sheet_liab_equity") if s not in located]
            if verbose and core_stypes_missing:
                print(f"    [DEBUG] core statement type(s) never located at all: {core_stypes_missing} "
                      f"-- searching for a bank-format (RBI Schedule III) balance sheet/P&L instead:")
                diagnose_bank_format(pdf)

        extracted = {"standalone": {}, "consolidated": {}}
        citations = []

        for stype, hits in located.items():
            for page_idx, scope in hits:
                if scope not in ("standalone", "consolidated"):
                    continue
                items = extract_line_items(pdf, page_idx, stype, company, fy_label, scope,
                                           verbose=verbose, items_compiled=items_compiled)
                if not items:
                    continue
                extracted[scope].update(items)
                for field, data in items.items():
                    citations.append({"field": field, **data["source"]})

        for scope in ("standalone", "consolidated"):
            if not extracted[scope]:
                continue
            if company_type == "bank":
                extracted[scope] = derive_bank_equity_and_liabilities(extracted[scope])
            else:
                extracted[scope], corrected = reconcile_liabilities_columns(extracted[scope])
                if corrected:
                    print(f"  NOTE: corrected a current/prior-year column reversal on the "
                          f"{scope} liabilities page (accounting identity now holds after the swap).")
                extracted[scope] = derive_total_equity_if_missing(extracted[scope])

    return extracted, citations, auditor, company_type


if __name__ == "__main__":
    pdf_path, company, fy_label, out_json = sys.argv[1:5]
    extracted, citations, auditor, company_type = run_extraction(pdf_path, company, fy_label)
    with open(out_json, "w") as f:
        json.dump({"extracted": extracted, "citations": citations, "auditor": auditor,
                   "company_type": company_type}, f, indent=2)
    print(f"Extracted {sum(len(v) for v in extracted.values())} line items, "
          f"company_type={company_type}, auditor={auditor!r} -> {out_json}")