"""
validate.py -- runs the extracted line items through six layers of sanity
checks BEFORE the risk engine trusts them, and attaches a validation report
to the year record so a bad extraction shows up as a visible flag instead
of a silently wrong risk score.

Layers (cheapest -> most powerful, matching the design doc):
    1. Format       -- is the value even a sane number?
    2. Range        -- is it in a plausible range for what it represents?
    3. Accounting   -- do the double-entry identities hold?              (⭐)
    4. Business logic -- is the combination of values physically possible? (⭐⭐⭐)
    5. Historical   -- is the YoY move plausible vs the prior year?       (⭐⭐⭐⭐⭐)
    6. Extraction   -- hook for re-extraction of anything Layers 1-5 flagged
                       as HIGH severity (needs the PDF; batch_extract.py
                       wires this up because it has pdf_path, this module
                       doesn't re-read PDFs itself).

Design notes:
- Every flag carries: layer, severity (INFO/WARN/HIGH), field(s), message,
  and the values involved, plus the source citation(s) already attached to
  the extracted line items -- so a flag is exactly as traceable as the
  extraction itself.
- This module is deliberately deterministic (no re-extraction, no ML) --
  layer 6 is a re-extraction *hook*, wired up by the caller.
- Runs against ONE scope's extracted dict (e.g. extracted["consolidated"]),
  same shape the risk engine already consumes.
- Units: extractor.py stores values exactly as printed in the report -- it
  does no scaling of its own. This validator assumes values are in the
  stated reporting unit (commonly Rs. million for large listed Indian
  companies, per the Ind-AS annual reports this pipeline targets -- see
  auto_detect.py). If a report uses a different unit (crore, lakh,
  thousand, etc.), adjust RANGE_RULES accordingly.
"""

import json
import sys


# ---------------------------------------------------------------------------
# Severity levels, in order of how much they should worry you
# ---------------------------------------------------------------------------
INFO, WARN, HIGH = "INFO", "WARN", "HIGH"


def _flag(layer, severity, field, message, **values):
    return {"layer": layer, "severity": severity, "field": field, "message": message, "values": values}


def _val(fin, field, year="current_year"):
    item = fin.get(field)
    return item[year] if item else None


def _sources(fin, *fields):
    srcs = []
    seen = set()
    for f in fields:
        item = fin.get(f)
        if not item:
            continue
        s = item["source"]
        key = (s["scope"], s["pdf_page"])
        if key not in seen:
            seen.add(key)
            srcs.append(s)
    return srcs


def _pct_change(cur, prior):
    if cur is None or prior in (None, 0):
        return None
    return (cur - prior) / abs(prior)


# ---------------------------------------------------------------------------
# LAYER 1 -- Format validation
# ---------------------------------------------------------------------------
# Fields that can legitimately be negative (net cash lines can be net
# outflows; everything else on a balance sheet / P&L should be >= 0).
CAN_BE_NEGATIVE = {"net_cash_from_investing", "net_cash_from_financing", "net_cash_from_operating"}

# Every field the extractor can produce, so Layer 1 can tell "genuinely
# missing" apart from "not applicable to this statement".
ALL_FIELDS = [
    "total_non_current_assets", "total_current_assets", "total_assets", "inventories",
    "cash_and_equivalents", "trade_receivables", "total_equity",
    "total_non_current_liabilities", "total_current_liabilities", "total_liabilities",
    "total_equity_and_liabilities", "msme_dues", "trade_payables_other",
    "trade_payables_undifferentiated", "borrowings",
    "revenue_from_operations", "total_income", "total_expenses", "finance_costs",
    "profit_before_tax", "total_tax_expense", "profit_for_the_year",
    "net_cash_from_operating", "net_cash_from_investing", "net_cash_from_financing", "capex",
]

MANDATORY_FIELDS = [
    "total_assets", "total_equity", "total_liabilities", "total_equity_and_liabilities",
    "revenue_from_operations", "profit_for_the_year",
]

# Banks don't have "Revenue from Operations" as a concept -- Interest Earned
# + Other Income is the closest analog, but it's structurally different
# (a lending spread, not sales revenue), so it isn't substituted in here.
# This is a policy placeholder until bank-specific income fields are added.
BANK_MANDATORY_FIELDS = [
    "total_assets", "total_equity", "total_liabilities", "profit_for_the_year",
]


def layer1_format(fin, mandatory_fields=None):
    mandatory_fields = mandatory_fields if mandatory_fields is not None else MANDATORY_FIELDS
    flags = []
    for field in mandatory_fields:
        item = fin.get(field)
        if item is None:
            flags.append(_flag(1, HIGH, field, "Mandatory field was not extracted at all (missing from the PDF match)."))
            continue
        for year in ("current_year", "prior_year"):
            v = item.get(year)
            if v is None:
                flags.append(_flag(1, HIGH, field, f"{year} value is None -- extraction failed or field is genuinely blank."))
            elif v < 0 and field not in CAN_BE_NEGATIVE:
                flags.append(_flag(1, HIGH, field, f"{year} value is negative, which shouldn't happen for this field.", value=v))
    # negative values on non-mandatory fields that also shouldn't be negative
    for field, item in fin.items():
        if field in MANDATORY_FIELDS or field in CAN_BE_NEGATIVE:
            continue
        for year in ("current_year", "prior_year"):
            v = item.get(year)
            if v is not None and v < 0:
                flags.append(_flag(1, WARN, field, f"{year} value is negative; unusual for this field.", value=v))
    return flags


# ---------------------------------------------------------------------------
# LAYER 2 -- Range validation
# ---------------------------------------------------------------------------
# (field, min, max) -- values are in the same unit the source PDF states
# (Rs. million for large-cap Ind-AS filers like Wipro; see module docstring).
# Lower bounds only exist for fields that structurally cannot be near-zero
# for a BSE/NSE-listed operating company -- revenue and total assets. Fields
# like finance_costs, borrowings, or trade_payables_undifferentiated get no
# floor because a genuinely debt-free company can print an honest zero there;
# an arbitrary positive floor would misflag correct extractions on those.
# Upper bounds remain a generous sanity fence, not a real-world bound; tune
# per company size if you extend to non-Wipro scale companies.
RANGE_RULES = [
    ("revenue_from_operations", 10, 10_000_000),
    ("total_assets", 10, 50_000_000),
    ("total_current_assets", 0, 50_000_000),
    ("finance_costs", 0, 1_000_000),
    ("trade_payables_undifferentiated", 0, 5_000_000),
    ("borrowings", 0, 10_000_000),
]


def layer2_range(fin):
    flags = []
    for field, lo, hi in RANGE_RULES:
        item = fin.get(field)
        if not item:
            continue
        for year in ("current_year", "prior_year"):
            v = item.get(year)
            if v is None:
                continue
            if v < lo:
                flags.append(_flag(2, HIGH, field, f"{year} value {v} is below the plausible floor ({lo}) -- for a listed company this is almost always a mis-extracted digit rather than a real value.", value=v))
            elif v > hi:
                flags.append(_flag(2, WARN, field, f"{year} value {v} exceeds the plausible ceiling ({hi}) -- check for a units mismatch (crore vs million) or a stray extra digit.", value=v))
    return flags


# ---------------------------------------------------------------------------
# LAYER 3 -- Accounting equation validation ⭐
# ---------------------------------------------------------------------------
# Tolerance as a fraction of total_assets -- real Ind-AS statements round to
# the nearest lakh/crore printed, so allow a small rounding slack rather than
# demanding exact equality.
BALANCE_SHEET_TOLERANCE_PCT = 0.005  # 0.5%


def layer3_accounting(fin):
    flags = []
    for year in ("current_year", "prior_year"):
        assets = _val(fin, "total_assets", year)
        equity = _val(fin, "total_equity", year)
        liab = _val(fin, "total_liabilities", year)
        eq_and_liab = _val(fin, "total_equity_and_liabilities", year)

        # Assets == Equity + Liabilities
        if assets is not None and equity is not None and liab is not None:
            diff = assets - (equity + liab)
            tol = BALANCE_SHEET_TOLERANCE_PCT * assets
            if abs(diff) > tol:
                flags.append(_flag(3, HIGH, "total_assets/total_equity/total_liabilities",
                                    f"{year}: Assets ({assets}) != Equity + Liabilities ({equity + liab}); off by {diff:.0f}, "
                                    f"exceeds {BALANCE_SHEET_TOLERANCE_PCT*100:.1f}% tolerance.",
                                    assets=assets, equity=equity, liabilities=liab, diff=diff,
                                    sources=_sources(fin, "total_assets", "total_equity", "total_liabilities")))

        # The two balance sheet totals (assets side vs equity+liab side)
        # should also literally match each other -- this catches a bad
        # extraction on either page even if the Assets=Equity+Liab identity
        # above happens to hold by coincidence.
        if assets is not None and eq_and_liab is not None and abs(assets - eq_and_liab) > BALANCE_SHEET_TOLERANCE_PCT * assets:
            flags.append(_flag(3, HIGH, "total_assets/total_equity_and_liabilities",
                                f"{year}: Balance sheet doesn't balance -- Total Assets ({assets}) != "
                                f"Total Equity and Liabilities ({eq_and_liab}).",
                                assets=assets, equity_and_liabilities=eq_and_liab,
                                sources=_sources(fin, "total_assets", "total_equity_and_liabilities")))

        # PAT = PBT - Tax
        pbt = _val(fin, "profit_before_tax", year)
        tax = _val(fin, "total_tax_expense", year)
        pat = _val(fin, "profit_for_the_year", year)
        if pbt is not None and tax is not None and pat is not None:
            expected_pat = pbt - tax
            diff = pat - expected_pat
            tol = max(1.0, 0.005 * abs(pbt))
            if abs(diff) > tol:
                flags.append(_flag(3, HIGH, "profit_for_the_year",
                                    f"{year}: PAT ({pat}) != PBT - Tax ({expected_pat:.0f}); off by {diff:.0f}. "
                                    f"One of PBT/Tax/PAT was likely mis-extracted.",
                                    pbt=pbt, tax=tax, pat=pat, expected_pat=expected_pat,
                                    sources=_sources(fin, "profit_before_tax", "total_tax_expense", "profit_for_the_year")))

        # Current assets + non-current assets == total assets
        ca = _val(fin, "total_current_assets", year)
        nca = _val(fin, "total_non_current_assets", year)
        if ca is not None and nca is not None and assets is not None:
            diff = assets - (ca + nca)
            if abs(diff) > BALANCE_SHEET_TOLERANCE_PCT * assets:
                flags.append(_flag(3, HIGH, "total_current_assets/total_non_current_assets",
                                    f"{year}: Current + Non-current assets ({ca + nca:.0f}) != Total assets ({assets}); off by {diff:.0f}.",
                                    current=ca, non_current=nca, total=assets,
                                    sources=_sources(fin, "total_current_assets", "total_non_current_assets", "total_assets")))
    return flags


# ---------------------------------------------------------------------------
# LAYER 4 -- Business logic validation ⭐⭐⭐
# (impossible/suspicious combinations, regardless of whether each field
# individually looked fine)
# ---------------------------------------------------------------------------
def layer4_business_logic(fin):
    flags = []
    for year in ("current_year", "prior_year"):
        rev = _val(fin, "revenue_from_operations", year)
        pat = _val(fin, "profit_for_the_year", year)
        pbt = _val(fin, "profit_before_tax", year)
        fc = _val(fin, "finance_costs", year)
        ca = _val(fin, "total_current_assets", year)
        assets = _val(fin, "total_assets", year)
        borrow = _val(fin, "borrowings", year)
        tp = _val(fin, "trade_payables_undifferentiated", year) or _val(fin, "trade_payables_other", year)

        if rev is not None and pat is not None and pat > rev:
            flags.append(_flag(4, HIGH, "profit_for_the_year",
                                f"{year}: PAT ({pat}) > Revenue ({rev}) -- impossible for an operating company.",
                                pat=pat, revenue=rev, sources=_sources(fin, "profit_for_the_year", "revenue_from_operations")))

        if rev is not None and pbt is not None and pbt > rev:
            flags.append(_flag(4, HIGH, "profit_before_tax",
                                f"{year}: PBT ({pbt}) > Revenue ({rev}) -- implies negative total costs; almost always an extraction error.",
                                pbt=pbt, revenue=rev, sources=_sources(fin, "profit_before_tax", "revenue_from_operations")))

        if rev is not None and fc is not None and fc > rev:
            flags.append(_flag(4, WARN, "finance_costs",
                                f"{year}: Finance costs ({fc}) > Revenue ({rev}) -- unusual outside a near-insolvent company.",
                                finance_costs=fc, revenue=rev, sources=_sources(fin, "finance_costs", "revenue_from_operations")))

        if ca is not None and assets is not None and ca > assets:
            flags.append(_flag(4, HIGH, "total_current_assets",
                                f"{year}: Current assets ({ca}) > Total assets ({assets}) -- impossible.",
                                current_assets=ca, total_assets=assets,
                                sources=_sources(fin, "total_current_assets", "total_assets")))

        if tp is not None and rev is not None and rev > 0 and tp > 2 * rev:
            flags.append(_flag(4, WARN, "trade_payables",
                                f"{year}: Trade payables ({tp}) > 2x Revenue ({rev}) -- suspicious, check for a units or duplicate-line error.",
                                trade_payables=tp, revenue=rev))

        if borrow is not None and assets is not None and borrow > assets:
            flags.append(_flag(4, WARN, "borrowings",
                                f"{year}: Borrowings ({borrow}) > Total assets ({assets}) -- usually wrong.",
                                borrowings=borrow, total_assets=assets,
                                sources=_sources(fin, "borrowings", "total_assets")))
    return flags


# ---------------------------------------------------------------------------
# LAYER 5 -- Historical (YoY) validation ⭐⭐⭐⭐⭐
# Uses current_year vs prior_year, which the extractor already captures from
# the same statement page -- no combined.json needed for this layer, though
# batch_extract.py can also call it across fiscal years using combined.json.
# ---------------------------------------------------------------------------
YOY_RULES = {
    # field -> (max plausible INCREASE, max plausible DECREASE, severity).
    # Increase and decrease bounds are intentionally asymmetric: a company
    # taking on a lot of new debt is plausible business; a company suddenly
    # showing HALF its prior borrowings, absent a disclosed repayment event,
    # is far more often a current/prior column swap than real deleveraging --
    # so the decrease side gets a tighter fence for debt/asset-like fields.
    "revenue_from_operations":  (0.80, 0.80, WARN),
    "total_assets":             (0.70, 0.70, WARN),
    "borrowings":               (1.00, 0.50, WARN),
    "profit_for_the_year":      (1.50, 1.50, WARN),   # PAT swings more naturally than revenue
    "total_current_assets":     (0.70, 0.60, WARN),
}

# Anything beyond this multiple on top of the WARN threshold is escalated,
# since a >3x jump on any of these is almost always a digit-count error
# (a leading digit dropped, or a units mismatch) rather than real business.
ESCALATE_MULTIPLE = 3.0


def layer5_historical(fin):
    flags = []
    for field, (max_increase_pct, max_decrease_pct, severity) in YOY_RULES.items():
        item = fin.get(field)
        if not item:
            continue
        cur, prior = item.get("current_year"), item.get("prior_year")
        pct = _pct_change(cur, prior)
        if pct is None:
            continue
        max_pct = max_increase_pct if pct > 0 else max_decrease_pct
        if abs(pct) > max_pct:
            sev = HIGH if abs(pct) > max_pct * ESCALATE_MULTIPLE else severity
            direction = "increase" if pct > 0 else "decline"
            flags.append(_flag(5, sev, field,
                                f"{field}: {abs(pct)*100:.0f}% YoY {direction} (prior {prior} -> current {cur}) "
                                f"exceeds the {max_pct*100:.0f}% plausibility band. Check for a missing/extra leading "
                                f"digit, a comma-parsing error, or a current/prior column swap.",
                                current=cur, prior=prior, pct_change=round(pct, 4),
                                sources=item.get("source") and [item["source"]] or []))

            # Leading-digit-missing heuristic: current is ~1/10th (or ~10x)
            # of prior, within 15% tolerance on the ratio -- a classic
            # dropped/duplicated leading digit rather than a real swing.
            if prior not in (None, 0):
                ratio = cur / prior if prior else None
                if ratio is not None:
                    if 0.85 <= ratio * 10 <= 1.15:
                        flags.append(_flag(5, HIGH, field,
                                            f"{field}: current ({cur}) looks like prior ({prior}) with a leading digit "
                                            f"dropped (ratio ~1/10). Strongly suspect a truncated extraction.",
                                            current=cur, prior=prior, ratio=round(ratio, 4)))
                    elif 8.5 <= ratio <= 11.5:
                        flags.append(_flag(5, HIGH, field,
                                            f"{field}: current ({cur}) looks like prior ({prior}) with an extra leading "
                                            f"digit inserted (ratio ~10x). Strongly suspect a duplicated/misread digit.",
                                            current=cur, prior=prior, ratio=round(ratio, 4)))
    return flags


def layer5_cross_year(combined, scope="consolidated"):
    """
    Same YoY checks but walking a company's combined.json across ALL fiscal
    years on file, rather than just current-vs-prior inside one PDF. Useful
    because a single-PDF current/prior pair can itself be swapped or wrong;
    comparing FY24's "current_year" against FY25's "current_year" (i.e. two
    independently-extracted statements) is an independent cross-check.
    """
    flags = []
    years = sorted(combined.get("years_available", []))
    for field, (max_increase_pct, max_decrease_pct, severity) in YOY_RULES.items():
        series = []
        for y in years:
            fin = combined["by_year"][y]["extracted"].get(scope, {})
            item = fin.get(field)
            series.append((y, item.get("current_year") if item else None))
        for (y0, v0), (y1, v1) in zip(series, series[1:]):
            pct = _pct_change(v1, v0)
            if pct is None:
                continue
            max_pct = max_increase_pct if pct > 0 else max_decrease_pct
            if abs(pct) > max_pct:
                sev = HIGH if abs(pct) > max_pct * ESCALATE_MULTIPLE else severity
                flags.append(_flag(5, sev, field,
                                    f"{field}: {y0}->{y1} changed {pct*100:.0f}% ({v0} -> {v1}), independently "
                                    f"extracted from two different PDFs -- exceeds the {max_pct*100:.0f}% band.",
                                    from_year=y0, to_year=y1, from_value=v0, to_value=v1, pct_change=round(pct, 4)))
    return flags


# ---------------------------------------------------------------------------
# LAYER 6 -- Extraction validation (re-extract only what's flagged) ⭐⭐⭐⭐⭐
# This module doesn't touch the PDF -- it just decides WHICH fields are
# worth the cost of re-extraction, so batch_extract.py (which has pdf_path)
# can call back into extractor.py with a widened/alternate pattern only for
# these fields, then use `compare_candidates` below to pick the best value.
# ---------------------------------------------------------------------------
def fields_needing_reextraction(flags):
    """HIGH-severity flags name fields worth spending a second extraction
    pass on; WARN-severity is usually a real (if unusual) business result."""
    fields = set()
    for f in flags:
        if f["severity"] == HIGH:
            field = f["field"].split("/")[0]  # some flags cite two fields joined by '/'
            fields.add(field)
    return sorted(fields)


def compare_candidates(candidates, validate_fn):
    """
    candidates: list of numeric values for the same field, from different
    extraction attempts (e.g. the original regex match, plus a re-extraction
    with a looser/alternate pattern).
    validate_fn: callable(value) -> number of validation problems it causes
                 (e.g. re-run layers 3/4/5 with each candidate substituted in
                 and count the resulting flags) -- lower is better.
    Returns the candidate with the fewest validation problems; ties keep the
    first (original) candidate, so a re-extraction only wins on clear improvement.
    """
    if not candidates:
        return None
    scored = [(validate_fn(c), i, c) for i, c in enumerate(candidates)]
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[0][2]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------
def validate_scope(extracted, scope="consolidated", combined=None, mandatory_fields=None):
    """
    Runs layers 1-5 (and cross-year 5 if `combined` is supplied) against one
    scope's extracted line items. Returns a validation report dict.
    Pass mandatory_fields=BANK_MANDATORY_FIELDS for bank company types, since
    they don't have revenue_from_operations as a concept.
    """
    fin = extracted.get(scope, {})
    flags = []
    flags += layer1_format(fin, mandatory_fields=mandatory_fields)
    flags += layer2_range(fin)
    flags += layer3_accounting(fin)
    flags += layer4_business_logic(fin)
    flags += layer5_historical(fin)
    if combined is not None:
        flags += layer5_cross_year(combined, scope=scope)

    high = [f for f in flags if f["severity"] == HIGH]
    warn = [f for f in flags if f["severity"] == WARN]

    return {
        "scope": scope,
        "status": "FAIL" if high else ("WARN" if warn else "PASS"),
        "flag_counts": {"HIGH": len(high), "WARN": len(warn), "INFO": len(flags) - len(high) - len(warn)},
        "flags": flags,
        "fields_needing_reextraction": fields_needing_reextraction(flags),
    }


if __name__ == "__main__":
    in_json = sys.argv[1]
    scope = sys.argv[2] if len(sys.argv) > 2 else "consolidated"
    with open(in_json) as f:
        data = json.load(f)
    extracted = data["extracted"] if "extracted" in data else data
    report = validate_scope(extracted, scope=scope)
    print(json.dumps(report, indent=2, default=str))
    print(f"\nStatus: {report['status']}  ({report['flag_counts']})")