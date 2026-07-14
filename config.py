"""
FinLens extraction & risk-scoring configuration.

This file centralizes everything that changes per-company or per-report-format:
statement anchor keywords (used to LOCATE the right pages), line-item regex
patterns (used to PULL numbers off those pages), and risk thresholds (used to
SCORE the ratios). The extractor and risk engine should not need to change
when you add a new company PDF -- only this file should.
"""

import re

# ---------------------------------------------------------------------------
# STATEMENT ANCHORS
# Keyword sets used to identify which page a statement lives on. A page must
# contain ALL keywords in a given tuple (case-insensitive) to match.
# ---------------------------------------------------------------------------
STATEMENT_ANCHORS = {
    # Both keywords are required specifically to exclude condensed MD&A
    # "Financial Position" summary tables (seen in HCL's report) -- those
    # satisfy "total assets"/"total equity and liabilities" but never say
    # "Total non-current assets/liabilities" (they show "(a) Non-current
    # liabilities" as a bare line item, no "Total" subtotal), because they're
    # a 2-3 year condensed commentary table, not the actual balance sheet.
    # MD&A commentary appears BEFORE the real financial statements in report
    # order, so without this, first-match-wins picks the MD&A table instead.
    "balance_sheet_assets": ["total assets", "total non-current assets"],
    "balance_sheet_liab_equity": ["total equity and liabilities", "total non-current liabilities"],
    "profit_and_loss": ["revenue from operations", "profit for the year"],
    # Section headers, not net-direction phrasing -- "net cash used/generated"
    # flips wording depending on whether the year was a net inflow or outflow,
    # so anchor on the stable "cash flows from X activities" headers instead.
    "cash_flow_operating_investing": [
        "cash flows from operating activities",
        "cash flows from investing activities",
    ],
    "cash_flow_financing": ["cash flows from financing activities"],
}

# Whether a page belongs to "standalone" or "consolidated" financials is
# determined by which of these header phrases appears on it (or nearby).
STATEMENT_SCOPE_MARKERS = {
    "standalone": ["standalone financial statement"],
    "consolidated": ["consolidated financial statement"],
}

# ---------------------------------------------------------------------------
# LINE ITEMS
# Regex applied per statement page. Each pattern must capture exactly two
# numeric groups: (current_year_value, prior_year_value), matching the
# Ind-AS report layout of "Label  <year N>  <year N-1>".
# Numbers may contain commas and be wrapped in parentheses for negatives.
# ---------------------------------------------------------------------------
NUM = r"(?:\(?-?[\d,]+\.?\d*\)?|-|\^)"

LINE_ITEMS = {
    "balance_sheet_assets": {
        "total_non_current_assets": rf"Total non-current assets\s+({NUM})\s+({NUM})",
        "total_current_assets": rf"Total current assets\s+({NUM})\s+({NUM})",
        "total_assets": rf"TOTAL ASSETS\s+({NUM})\s+({NUM})",
        "inventories": rf"Inventories\s+\d*\s*({NUM})\s+({NUM})",
        "cash_and_equivalents": rf"Cash and cash equivalents\s+\d*\s*({NUM})\s+({NUM})",
        # "Trade receivables" can appear twice (a non-current financial-asset
        # line, then the current-assets line) -- the extractor takes the LAST
        # match for this field, which is the current-assets figure.
        "trade_receivables": rf"Trade receivables\s+\d*\s*({NUM})\s+({NUM})",
        # In this report layout, the EQUITY section (and its TOTAL EQUITY
        # subtotal) is printed at the bottom of the ASSETS page, not the
        # liabilities page -- so this pattern lives here, not below.
        "total_equity": rf"TOTAL EQUITY\s+({NUM})\s+({NUM})",
    },
    "balance_sheet_liab_equity": {
        "total_non_current_liabilities": rf"Total non-current liabilities\s+({NUM})\s+({NUM})",
        "total_current_liabilities": rf"Total current liabilities\s+({NUM})\s+({NUM})",
        "total_liabilities": rf"TOTAL LIABILITIES\s+({NUM})\s+({NUM})",
        "total_equity_and_liabilities": rf"TOTAL EQUITY AND LIABILITIES\s+({NUM})\s+({NUM})",
        # MSMED Act disclosure -- appears once in non-current liabilities
        # (usually ~0) and once in current liabilities. Extractor sums ALL
        # matches on the combined assets+liab pages for current & prior year.
        # NOTE: this is *total dues outstanding* to micro/small enterprises,
        # not specifically amounts overdue past the 45-day MSMED limit --
        # the report doesn't always break that out, so treat this as a
        # proxy, not the literal "MSME Payment Delay Amount" metric.
        "msme_dues": rf"Total outstanding dues of micro enterprises and small enterprises\s+\d*\s*({NUM})\s+({NUM})",
        "trade_payables_other": rf"Total outstanding dues of creditors other than micro enterprises and\s+\d*\s*({NUM})\s+({NUM})\s*\n?small enterprises",
        # Fallback for report formats (often consolidated statements) that
        # don't split trade payables into MSME/non-MSME on the balance sheet
        # face -- just show one undifferentiated "Trade payables" line. This
        # pattern only matches when NOT followed by "(a)", so it won't
        # double-count on layouts that DO have the split.
        "trade_payables_undifferentiated": rf"Trade payables\s+\d+\s+({NUM})\s+({NUM})",
        # "Borrowings" appears under non-current liabilities and again under
        # current liabilities -- summed across all matches to get total debt
        # (distinct from total_liabilities, which includes non-debt items
        # like trade payables and provisions).
        "borrowings": rf"Borrowings\s+\d*\s*({NUM})\s+({NUM})",
    },
    "profit_and_loss": {
        # Note-reference skip must handle decimal-style note numbers (e.g.
        # HCL's "Revenue from operations 3.19 85,651 75,379" -- "3.19" is
        # Note 3.19, not a value). A plain \d*\s* skip stops at the "."
        # and lets the note number get misread as current_year, shifting
        # both years by one column. Matching the note ref as one explicit
        # optional unit (integer or N.NN, always followed by whitespace)
        # avoids that, while still working when there's no note ref at all.
        "revenue_from_operations": rf"Revenue from operations\s+(?:\d+(?:\.\d+)?\s+)?({NUM})\s+({NUM})",
        "total_income": rf"Total income[^\n]*?\s+({NUM})\s+({NUM})",
        "total_expenses": rf"Total expenses[^\n]*?\s+({NUM})\s+({NUM})",
        "finance_costs": rf"Finance costs\s+\d*\s*({NUM})\s+({NUM})",
        "profit_before_tax": rf"Profit before tax[^\n]*?\s+({NUM})\s+({NUM})",
        "total_tax_expense": rf"Total tax expense\s+({NUM})\s+({NUM})",
        "profit_for_the_year": rf"Profit for the year\s+({NUM})\s+({NUM})",
    },
    "cash_flow_operating_investing": {
        # PDF text extraction sometimes wraps "activities" onto a line AFTER
        # the numbers (label / numbers / "activities"), so allow up to ~20
        # stray characters (whitespace or the word "activities") between the
        # end of the label and the first number rather than requiring them
        # to be adjacent.
        "net_cash_from_operating": rf"Net cash generated from operating[\s\S]{{0,20}}?({NUM})\s+({NUM})",
        "net_cash_from_investing": rf"Net cash (?:used in|generated from/\(used in\)) investing[\s\S]{{0,20}}?({NUM})\s+({NUM})",
        "capex": rf"Payment for purchase of property,[\s\S]{{0,30}}?equipment\s+({NUM})\s+({NUM})",
    },
    "cash_flow_financing": {
        "net_cash_from_financing": rf"Net cash used in financing\s*\n?activities\s+({NUM})\s+({NUM})",
    },
}

# Compiled once at import time (case-insensitive -- see extractor.py's
# case-sensitivity fix) rather than recompiling per field per page. re's
# internal pattern cache already softens the cost of re.finditer(pattern, ...)
# somewhat, but compiling explicitly here means the flag lives in exactly one
# place instead of being repeated at every call site, and avoids relying on
# an implementation detail (the cache has a fixed size and can be evicted).
LINE_ITEMS_COMPILED = {
    stype: {field: re.compile(pattern, re.IGNORECASE) for field, pattern in fields.items()}
    for stype, fields in LINE_ITEMS.items()
}

# ---------------------------------------------------------------------------
# BANK SCHEMA (RBI Schedule III / Banking Regulation Act Form A & B)
# Parallel to STATEMENT_ANCHORS/LINE_ITEMS above, NOT a variant of them --
# banks use fundamentally different statement structure and terminology
# (verified against real Yes Bank FY22 standalone balance sheet/P&L text,
# not guessed). Two important caveats, both still open:
#   1. This page reported figures in "₹ in thousands", not crores like every
#      industrial-company report handled so far -- no unit normalization is
#      applied yet, so bank figures will be ~1000x the scale of TCS/HCL/L&T
#      figures if ever compared directly. Needs a policy decision on a
#      canonical unit before that matters.
#   2. Only verified against ONE bank, ONE year. Not yet confirmed that
#      HDFC's layout jumbles columns the same way, or that Yes Bank's other
#      years follow this same pattern.
BANK_STATEMENT_ANCHORS = {
    "bank_balance_sheet": ["capital and liabilities"],
    "bank_profit_and_loss": ["interest earned", "interest expended"],
    # UNVERIFIED (first pass -- not yet confirmed against a real filing the
    # way bank_balance_sheet/bank_profit_and_loss were confirmed against Yes
    # Bank text; treat as a starting point, not a tested pattern).
    #
    # Every listed Indian bank publishes a "Key Financial Ratios" / "Key
    # Performance Indicators" table (mandated by SEBI LODR Reg. 33/52) that
    # carries CRAR, Gross/Net NPA%, Provision Coverage, CASA, NIM, and
    # Cost-to-Income side by side, usually in the Directors' Report/MD&A
    # rather than on the face of the balance sheet or P&L. Anchoring on two
    # of those labels together should be reasonably specific to that one
    # table -- but run this against an actual bank annual report and check
    # the [DEBUG] output before trusting the extracted values.
    "bank_key_ratios": ["capital adequacy ratio", "net npa"],
}

BANK_LINE_ITEMS = {
    "bank_balance_sheet": {
        # IMPORTANT: this must anchor on "TOTAL" at the START of a line
        # (re.MULTILINE '^'), NOT a "header ... TOTAL" context search. Tested
        # both against real jumbled two-column text: a context-style search
        # like "CAPITAL AND LIABILITIES.*TOTAL" actually grabbed the WRONG
        # value (a P&L subtotal that got interleaved mid-line, right after
        # the "Deposits" row, due to the balance-sheet/P&L side-by-side
        # column layout). Anchoring on '^TOTAL' skips those mid-line
        # artifacts and reliably finds the real total first -- confirmed it
        # recurs identically later on the same page (both sides of a bank
        # balance sheet total to the same figure by definition).
        "total_assets": rf"^TOTAL\s+({NUM})\s+({NUM})",
        "capital": rf"^Capital\s+\d+\s+({NUM})\s+({NUM})",
        "reserves_and_surplus": rf"Reserves and surplus\s+\d+\s+({NUM})\s+({NUM})",
    },
    "bank_profit_and_loss": {
        # Clean, unjumbled line in the tested sample -- no note-ref or
        # column-interleaving issue observed here.
        "profit_for_the_year": rf"Net profit/\(loss\) for the year\s+({NUM})\s+({NUM})",
    },
    # UNVERIFIED patterns -- see the anchor note above. The "Key Ratios"
    # table sometimes prints only the current period's %, sometimes current
    # + prior side by side; the second capture group is written as optional
    # ({NUM})? so a single-column layout still matches, with prior_year left
    # None (clean_number(None) already returns None, no extractor change
    # needed). Values are captured as raw percentage numbers (e.g. 3.2 for
    # "3.2%") -- risk_engine.py divides by 100 before scoring, consistent
    # with how every other percentage-based ratio in THRESHOLDS is stored
    # as a fraction.
    "bank_key_ratios": {
        "crar_pct": rf"Capital Adequacy Ratio[^\n%]*?({NUM})\s*%?\s*({NUM})?\s*%?",
        "gross_npa_pct": rf"Gross NPA(?:\s+Ratio|\s+to\s+Gross\s+Advances)?[^\n%]*?({NUM})\s*%?\s*({NUM})?\s*%?",
        "net_npa_pct": rf"Net NPA(?:\s+Ratio|\s+to\s+Net\s+Advances)?[^\n%]*?({NUM})\s*%?\s*({NUM})?\s*%?",
        "provision_coverage_pct": rf"Provision Coverage Ratio[^\n%]*?({NUM})\s*%?\s*({NUM})?\s*%?",
        "casa_pct": rf"CASA(?:\s+Ratio)?[^\n%]*?({NUM})\s*%?\s*({NUM})?\s*%?",
        "nim_pct": rf"Net Interest Margin[^\n%]*?({NUM})\s*%?\s*({NUM})?\s*%?",
        "cost_to_income_pct": rf"Cost[- ]to[- ]Income Ratio[^\n%]*?({NUM})\s*%?\s*({NUM})?\s*%?",
    },
}

BANK_LINE_ITEMS_COMPILED = {
    stype: {field: re.compile(pattern, re.IGNORECASE | re.MULTILINE) for field, pattern in fields.items()}
    for stype, fields in BANK_LINE_ITEMS.items()
}

# ---------------------------------------------------------------------------
# RISK THRESHOLDS
# All scoring bands live here, fully documented, so risk scores are explainable.
# Each ratio maps to (safe_threshold, distress_threshold, direction) where
# direction "higher_is_safer" or "lower_is_safer" tells the scorer which way
# risk increases.
# ---------------------------------------------------------------------------
THRESHOLDS = {
    "current_ratio": {"safe": 1.5, "distress": 1.0, "direction": "higher_is_safer",
                       "note": "Current assets / current liabilities. Standard range 1.5x-2.0x; below 1.0x is a red flag."},
    "quick_ratio": {"safe": 1.0, "distress": 0.7, "direction": "higher_is_safer",
                     "note": "(Current assets - inventories) / current liabilities."},
    "debt_to_equity": {"safe": 1.0, "distress": 2.0, "direction": "lower_is_safer",
                        "note": "Total liabilities / total equity. Up to 1.0x conservative, up to 2.0x acceptable in capital-intensive sectors, above 2.0x signals over-leverage."},
    "interest_coverage": {"safe": 2.5, "distress": 1.5, "direction": "higher_is_safer",
                           "note": "(Profit before tax + finance costs) / finance costs. 2.5x-3x+ is comfortable; below 1.5x signals strain; below 1x means profit doesn't cover interest."},
    "net_margin": {"safe": 0.10, "distress": 0.03, "direction": "higher_is_safer",
                    "note": "Profit for the year / revenue from operations."},
    "roe": {"safe": 0.12, "distress": 0.05, "direction": "higher_is_safer",
            "note": "Profit for the year / total equity (period-end, simplified)."},
    "revenue_growth": {"safe": 0.08, "distress": 0.0, "direction": "higher_is_safer",
                        "note": "YoY revenue growth. Negative growth for 2+ consecutive years is a red flag."},
    "cash_conversion": {"safe": 1.0, "distress": 0.6, "direction": "higher_is_safer",
                         "note": "Operating cash flow / net profit. Below 1.0 suggests earnings not backed by cash."},
    "opex_to_revenue": {"safe": 0.85, "distress": 0.95, "direction": "lower_is_safer",
                         "note": "Total expenses / total income."},
    "pat_cagr_3yr": {"safe": 0.08, "distress": 0.0, "direction": "higher_is_safer",
                      "note": "3-year CAGR of Profit After Tax. Flat (~0%) or negative over 3 years is a concern. Requires 3+ years of data."},
    "dpo_days": {"safe": 60, "distress": 90, "direction": "lower_is_safer",
                 "note": "Approx. days payable outstanding = trade payables / total expenses x 365. 30-60 days is typical; 90+ suggests cash-flow stress. (Approximation: uses total expenses as a proxy for purchases/COGS, since this report format doesn't break those out separately.)"},
    "debt_growth_minus_revenue_growth": {"safe": 0.0, "distress": 0.15, "direction": "lower_is_safer",
                                          "note": "YoY borrowings growth minus YoY revenue growth. Debt growing faster than revenue is a red flag."},
    "msme_dues_pct_revenue": {"safe": 0.0005, "distress": 0.01, "direction": "lower_is_safer",
                               "note": "Total dues to micro/small enterprises as a % of revenue. Zero overdue is the statutory standard; this is a proxy using total (not just overdue) dues."},
}

# ---------------------------------------------------------------------------
# MANUAL METRICS
# These come from the standard-range reference document but are NOT reliably
# extractable from an annual report's financial statements -- they live in
# secretarial audit reports, RoC filings, credit rating letters, litigation
# trackers, or news, none of which this pipeline reads. Rather than guess or
# default to "safe", these are left as None (shown as "N/A - needs manual
# input") until supplied via a manual_inputs.json for the company, and are
# then scored with the same score_metric() function as everything else.
#
# Boolean flags are encoded as 0 (no issue) / 1 (issue present) so the same
# higher/lower_is_safer scorer works uniformly for booleans, counts, and %s.
# ---------------------------------------------------------------------------
MANUAL_METRICS = {
    "gst_filing_delays_count":          {"safe": 0, "distress": 2, "direction": "lower_is_safer", "dimension": "Compliance", "note": "Number of late GST return filings this financial year."},
    "roc_filings_noncompliant":         {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Compliance", "note": "1 if RoC/annual filings are not fully up to date, else 0."},
    "statutory_registration_lapses":    {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Compliance", "note": "1 if any required license/registration has lapsed, else 0."},
    "active_insolvency_case":           {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Legal", "note": "1 if an insolvency/bankruptcy case is currently pending against the entity."},
    "pending_litigation_count":         {"safe": 5, "distress": 20, "direction": "lower_is_safer", "dimension": "Legal", "note": "Ongoing cases as respondent. Benchmark against company size/peers; a sharp YoY increase matters more than the absolute count."},
    "nclt_proceeding_ongoing":          {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Legal", "note": "1 if a case is pending before NCLT/NCLAT or a regulator."},
    "credit_rating_below_investment":   {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Credit", "note": "1 if external credit rating is sub-investment grade."},
    "open_charges_pct_total_debt":      {"safe": 0.55, "distress": 0.75, "direction": "lower_is_safer", "dimension": "Credit", "note": "Secured (charge-backed) debt as % of total borrowed debt."},
    "employee_count_yoy_change_pct":    {"safe": 0.0, "distress": -0.15, "direction": "higher_is_safer", "dimension": "Operational", "note": "YoY headcount change. Decline of more than 10-15% is a red flag."},
    "related_party_pct_revenue":        {"safe": 0.10, "distress": 0.30, "direction": "lower_is_safer", "dimension": "Governance", "note": "Related-party transactions as % of revenue."},
    "board_member_turnover_count":      {"safe": 1, "distress": 3, "direction": "lower_is_safer", "dimension": "Governance", "note": "Directors who joined or resigned in the last 12 months."},
    "auditor_changed_unexpectedly":     {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Governance", "note": "1 if the statutory auditor changed outside a normal rotation cycle."},
    "promoter_shareholding_declining":  {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Governance", "note": "1 if promoter/majority holding has declined over consecutive quarters."},
    "down_round_last_funding":          {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Market Position", "note": "1 if the most recent funding round was at a lower valuation than the previous one."},
    "high_sector_regulatory_risk":      {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Market Position", "note": "1 if the entity is in an inherently cyclical/capital-intensive/heavily regulated sector."},
    "adverse_media_coverage":           {"safe": 0, "distress": 1, "direction": "lower_is_safer", "dimension": "Reputation", "note": "1 if material adverse press/regulatory coverage in the last 12 months."},
}

DIMENSION_METRICS = {
    "Liquidity": ["current_ratio", "quick_ratio"],
    "Solvency": ["debt_to_equity", "interest_coverage", "debt_growth_minus_revenue_growth"],
    "Profitability": ["net_margin", "roe", "revenue_growth", "pat_cagr_3yr"],
    "Operational": ["cash_conversion", "opex_to_revenue", "dpo_days", "msme_dues_pct_revenue"],
}

# Manual-input dimensions, built dynamically from MANUAL_METRICS' "dimension"
# tags -- shown alongside the extracted dimensions above but only scored once
# a manual_inputs.json is supplied for the company (otherwise "N/A").
MANUAL_DIMENSIONS = {}
for _metric, _cfg in MANUAL_METRICS.items():
    MANUAL_DIMENSIONS.setdefault(_cfg["dimension"], []).append(_metric)

COMPOSITE_WEIGHTS = {
    "Liquidity": 0.20,
    "Solvency": 0.25,
    "Profitability": 0.25,
    "Operational": 0.10,
    "Compliance": 0.05,
    "Legal": 0.05,
    "Credit": 0.04,
    "Governance": 0.03,
    "Market Position": 0.02,
    "Reputation": 0.01,
}

# ---------------------------------------------------------------------------
# BANK RISK THRESHOLDS
# A DELIBERATELY SEPARATE set from THRESHOLDS above -- current_ratio,
# quick_ratio, debt_to_equity (as configured for industrials), dpo_days and
# msme_dues_pct_revenue do not apply to a bank: banks don't carry a
# current/non-current balance-sheet split, don't hold inventory, and are
# high-leverage BY DESIGN (deposits are the business, not distress-signaling
# debt), so scoring a bank on industrial thresholds makes every healthy bank
# look maximally risky on Solvency while saying nothing about what actually
# drives bank risk. These use the metrics banking regulators/analysts
# actually track: capital adequacy, asset quality, provisioning, funding
# mix, margin, and efficiency. Thresholds are RBI-guideline-informed
# ballpark bands for scale, not a specific bank's own historical range --
# recalibrate once you have a few real filings to check them against, the
# same way THRESHOLDS above should eventually be checked against more than
# TCS/HCL/L&T/Wipro.
# ---------------------------------------------------------------------------
BANK_THRESHOLDS = {
    "crar_pct": {"safe": 0.14, "distress": 0.10, "direction": "higher_is_safer",
                 "note": "Capital to Risk-Weighted Assets Ratio. RBI's minimum incl. capital conservation buffer is ~11.5%; comfortably capitalized banks run 14%+; approaching the regulatory minimum is a distress signal."},
    "gross_npa_pct": {"safe": 0.02, "distress": 0.06, "direction": "lower_is_safer",
                       "note": "Gross NPAs / Gross Advances. Below 2% is clean asset quality for an Indian bank; above 6% signals significant stress."},
    "net_npa_pct": {"safe": 0.01, "distress": 0.03, "direction": "lower_is_safer",
                     "note": "Net NPAs / Net Advances, i.e. after provisioning. Below 1% is healthy; above 3% is a red flag."},
    "provision_coverage_pct": {"safe": 0.70, "distress": 0.40, "direction": "higher_is_safer",
                                "note": "Provisions held / Gross NPAs. Above 70% means bad loans are well provisioned for; below 40% means losses aren't fully absorbed yet."},
    "casa_pct": {"safe": 0.40, "distress": 0.20, "direction": "higher_is_safer",
                 "note": "Current + Savings Account deposits / Total deposits. Higher CASA means cheaper, stickier funding; low CASA means heavier reliance on costlier term deposits/wholesale funding."},
    "nim_pct": {"safe": 0.032, "distress": 0.020, "direction": "higher_is_safer",
                "note": "Net Interest Margin. 3.2%+ is a healthy lending spread for Indian banks; below 2% is thin."},
    "cost_to_income_pct": {"safe": 0.45, "distress": 0.60, "direction": "lower_is_safer",
                            "note": "Operating expenses / net operating income. Below 45% is efficient; above 60% signals a bloated cost base."},
    "roe": {"safe": 0.12, "distress": 0.03, "direction": "higher_is_safer",
            "note": "Profit for the year / total equity (capital + reserves). Same formula as the industrial ROE, wider band -- bank ROEs are more cyclical."},
    "roa": {"safe": 0.010, "distress": 0.002, "direction": "higher_is_safer",
            "note": "Profit for the year / total assets. ~1%+ is considered strong for an Indian bank."},
    "pat_cagr_3yr": {"safe": 0.08, "distress": 0.0, "direction": "higher_is_safer",
                      "note": "3-year CAGR of Profit After Tax, same definition as the industrial metric. Requires 3+ years of data."},
}

BANK_DIMENSION_METRICS = {
    "Capital Adequacy": ["crar_pct"],
    "Asset Quality": ["gross_npa_pct", "net_npa_pct", "provision_coverage_pct"],
    "Funding & Liquidity": ["casa_pct"],
    "Profitability": ["roe", "roa", "nim_pct", "pat_cagr_3yr"],
    "Efficiency": ["cost_to_income_pct"],
}

# Reuses the same manual dimensions (Compliance/Legal/Credit/Governance/
# Market Position/Reputation) and MANUAL_METRICS as the industrial config --
# none of those are sector-specific. Weights on the extracted side are
# rebalanced for what actually matters to a bank's risk profile (Asset
# Quality weighted highest, matching how NPAs dominate real bank credit
# analysis); manual-dimension weights are left equal to COMPOSITE_WEIGHTS
# above so the two composite scores stay comparable in what fraction of the
# score manual input can move.
BANK_COMPOSITE_WEIGHTS = {
    "Capital Adequacy": 0.20,
    "Asset Quality": 0.25,
    "Funding & Liquidity": 0.10,
    "Profitability": 0.15,
    "Efficiency": 0.10,
    "Compliance": 0.05,
    "Legal": 0.05,
    "Credit": 0.04,
    "Governance": 0.03,
    "Market Position": 0.02,
    "Reputation": 0.01,
}

# ---------------------------------------------------------------------------
# COMPOSITE SCORE COVERAGE GATING
#
# A dimension only contributes a score to the composite if at least this
# fraction of ITS OWN metrics actually extracted (rather than averaging
# over just the one metric that happened to be available and calling that
# average the dimension's score). The composite itself is only shown as a
# number + risk label if at least this fraction of the TOTAL composite
# WEIGHT is backed by dimensions that passed that bar -- otherwise it's
# reported as "insufficient data" instead of silently renormalizing over
# whatever fragment of the scoring model had data.
#
# This exists because of a real bug: previously, if extraction filled in
# just enough for ONE dimension (e.g. Profitability, via a single ROE
# value) and nothing else, the composite renormalized entirely onto that
# one dimension and could show "100 / LOW RISK" off a single number. That
# hit banks hardest, since bank disclosure formats vary far more than
# industrial Ind-AS filings and partial extraction is the norm, not the
# exception -- see risk_engine.py's build_risk_report()/_finalize_composite().
# ---------------------------------------------------------------------------
MIN_DIMENSION_COVERAGE = 0.5
MIN_COMPOSITE_COVERAGE = 0.5