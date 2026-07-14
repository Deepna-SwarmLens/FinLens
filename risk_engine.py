"""
risk_engine.py -- turns extracted line items into ratios, risk scores per
dimension, a composite score, and evidence flags -- every one of which keeps
a pointer back to the source page(s) it was computed from.

Scoring is now COMPANY-TYPE AWARE: industrial companies and banks use
different ratio sets, different thresholds (config.THRESHOLDS vs
config.BANK_THRESHOLDS), and different dimension/weight tables, because they
are not comparable businesses -- see config.py's BANK_THRESHOLDS docstring
for why (banks are high-leverage by design, don't carry inventory or a
current/non-current split, etc.).

Scoring is also COVERAGE-GATED: a dimension only contributes to the
composite if enough of its own metrics actually extracted, and the composite
itself is only shown as a number if enough of its total weight is backed by
real data. This replaces the old behaviour where a composite score silently
renormalized over whatever fragment of the model had data -- which could
turn a single lucky metric (e.g. one ROE value) into a "100/LOW RISK"
verdict when almost nothing else had extracted. See config.py's
MIN_DIMENSION_COVERAGE/MIN_COMPOSITE_COVERAGE for the full rationale.
"""

import json
import math
import sys
from config import (
    THRESHOLDS, DIMENSION_METRICS, COMPOSITE_WEIGHTS,
    BANK_THRESHOLDS, BANK_DIMENSION_METRICS, BANK_COMPOSITE_WEIGHTS,
    MANUAL_METRICS, MANUAL_DIMENSIONS,
    MIN_DIMENSION_COVERAGE, MIN_COMPOSITE_COVERAGE,
)


def safe_div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def _scoring_config(company_type):
    """Returns (thresholds, dimension_metrics, composite_weights) for a company_type."""
    if company_type == "bank":
        return BANK_THRESHOLDS, BANK_DIMENSION_METRICS, BANK_COMPOSITE_WEIGHTS
    return THRESHOLDS, DIMENSION_METRICS, COMPOSITE_WEIGHTS


def _sources(fin, *fields):
    srcs = []
    for f in fields:
        item = fin.get(f)
        if item:
            srcs.append(item["source"])
    # de-duplicate by (statement, pdf_page)
    seen = set()
    uniq = []
    for s in srcs:
        key = (s["statement"], s["pdf_page"])
        if key not in seen:
            seen.add(key)
            uniq.append(s)
    return uniq


def compute_industrial_ratios(fin):
    """fin: the 'current_year'/'prior_year' dict of extracted values for one scope."""

    def val(field, year="current_year"):
        item = fin.get(field)
        return item[year] if item else None

    ratios = {}
    trade_payables_total = {}
    borrowings_val = {}

    for year in ("current_year", "prior_year"):
        ca = val("total_current_assets", year)
        cl = val("total_current_liabilities", year)
        inv = val("inventories", year)
        tl = val("total_liabilities", year)
        te = val("total_equity", year)
        pbt = val("profit_before_tax", year)
        fc = val("finance_costs", year)
        np_ = val("profit_for_the_year", year)
        rev = val("revenue_from_operations", year)
        ocf = val("net_cash_from_operating", year)
        texp = val("total_expenses", year)
        tinc = val("total_income", year)

        # trade payables total: prefer the MSME/non-MSME split when present
        # (standalone-style layout), else fall back to the undifferentiated
        # line (consolidated-style layout).
        msme = val("msme_dues", year)
        other_tp = val("trade_payables_other", year)
        undiff_tp = val("trade_payables_undifferentiated", year)
        if msme is not None or other_tp is not None:
            tp_total = (msme or 0) + (other_tp or 0)
        else:
            tp_total = undiff_tp
        trade_payables_total[year] = tp_total
        borrowings_val[year] = val("borrowings", year)

        current_ratio = safe_div(ca, cl)
        quick_ratio = safe_div((ca - inv) if (ca is not None and inv is not None) else None, cl)
        debt_to_equity = safe_div(tl, te)
        interest_coverage = safe_div((pbt + fc) if (pbt is not None and fc is not None) else None, fc)
        net_margin = safe_div(np_, rev)
        roe = safe_div(np_, te)
        cash_conversion = safe_div(ocf, np_)
        opex_to_revenue = safe_div(texp, tinc)
        dpo_days = safe_div(tp_total, texp)
        dpo_days = dpo_days * 365 if dpo_days is not None else None
        msme_dues_pct_revenue = safe_div(msme, rev)

        ratios[year] = {
            "current_ratio": current_ratio,
            "quick_ratio": quick_ratio,
            "debt_to_equity": debt_to_equity,
            "interest_coverage": interest_coverage,
            "net_margin": net_margin,
            "roe": roe,
            "cash_conversion": cash_conversion,
            "opex_to_revenue": opex_to_revenue,
            "dpo_days": dpo_days,
            "msme_dues_pct_revenue": msme_dues_pct_revenue,
            "pat_cagr_3yr": None,  # only computable with 3+ years -- filled in by build_multi_year_metrics
        }

    cur_rev = val("revenue_from_operations", "current_year")
    prior_rev = val("revenue_from_operations", "prior_year")
    revenue_growth = safe_div(
        (cur_rev - prior_rev) if (cur_rev is not None and prior_rev is not None) else None, prior_rev
    )
    ratios["current_year"]["revenue_growth"] = revenue_growth
    ratios["prior_year"]["revenue_growth"] = None  # no prior-prior data available

    cur_borrow, prior_borrow = borrowings_val["current_year"], borrowings_val["prior_year"]
    debt_growth = safe_div(
        (cur_borrow - prior_borrow) if (cur_borrow is not None and prior_borrow is not None) else None, prior_borrow
    )
    ratios["current_year"]["debt_growth_minus_revenue_growth"] = (
        (debt_growth - revenue_growth) if (debt_growth is not None and revenue_growth is not None) else None
    )
    ratios["prior_year"]["debt_growth_minus_revenue_growth"] = None

    evidence_sources = {
        "current_ratio": _sources(fin, "total_current_assets", "total_current_liabilities"),
        "quick_ratio": _sources(fin, "total_current_assets", "inventories", "total_current_liabilities"),
        "debt_to_equity": _sources(fin, "total_liabilities", "total_equity"),
        "interest_coverage": _sources(fin, "profit_before_tax", "finance_costs"),
        "net_margin": _sources(fin, "profit_for_the_year", "revenue_from_operations"),
        "roe": _sources(fin, "profit_for_the_year", "total_equity"),
        "revenue_growth": _sources(fin, "revenue_from_operations"),
        "cash_conversion": _sources(fin, "net_cash_from_operating", "profit_for_the_year"),
        "opex_to_revenue": _sources(fin, "total_expenses", "total_income"),
        "dpo_days": _sources(fin, "msme_dues", "trade_payables_other", "trade_payables_undifferentiated", "total_expenses"),
        "debt_growth_minus_revenue_growth": _sources(fin, "borrowings", "revenue_from_operations"),
        "msme_dues_pct_revenue": _sources(fin, "msme_dues", "revenue_from_operations"),
        "pat_cagr_3yr": [],  # filled in by build_multi_year_metrics, which has its own multi-year sources
    }

    return ratios, evidence_sources


def compute_bank_ratios(fin):
    """
    Bank-specific ratios: capital adequacy, asset quality, provisioning,
    funding mix, profitability, and efficiency -- the metrics that actually
    drive bank risk, as opposed to reusing industrial ratios that don't
    apply to a bank's business model (see config.BANK_THRESHOLDS docstring).

    Percentage fields (crar_pct, gross_npa_pct, etc.) are extracted as raw
    printed percentages (e.g. 14.2 for "14.2%") and converted to fractions
    here, matching how every other percentage-based ratio in this pipeline
    is stored (e.g. net_margin=0.10 means 10%).

    Falls back to None wherever the underlying disclosure wasn't extracted.
    Bank annual reports vary in format far more than industrial Ind-AS
    filings, so incomplete coverage here is the norm, not the exception --
    that's exactly what the coverage gating in build_risk_report() and
    config.MIN_DIMENSION_COVERAGE/MIN_COMPOSITE_COVERAGE exist to handle
    honestly instead of silently.
    """

    def val(field, year="current_year"):
        item = fin.get(field)
        return item[year] if item else None

    def pct(field, year="current_year"):
        v = val(field, year)
        return v / 100 if v is not None else None

    ratios = {}
    for year in ("current_year", "prior_year"):
        pat = val("profit_for_the_year", year)
        total_equity = val("total_equity", year)
        total_assets = val("total_assets", year)

        ratios[year] = {
            "crar_pct": pct("crar_pct", year),
            "gross_npa_pct": pct("gross_npa_pct", year),
            "net_npa_pct": pct("net_npa_pct", year),
            "provision_coverage_pct": pct("provision_coverage_pct", year),
            "casa_pct": pct("casa_pct", year),
            "nim_pct": pct("nim_pct", year),
            "cost_to_income_pct": pct("cost_to_income_pct", year),
            "roe": safe_div(pat, total_equity),
            "roa": safe_div(pat, total_assets),
            "pat_cagr_3yr": None,  # only computable with 3+ years -- filled in by build_multi_year_metrics
        }

    evidence_sources = {
        "crar_pct": _sources(fin, "crar_pct"),
        "gross_npa_pct": _sources(fin, "gross_npa_pct"),
        "net_npa_pct": _sources(fin, "net_npa_pct"),
        "provision_coverage_pct": _sources(fin, "provision_coverage_pct"),
        "casa_pct": _sources(fin, "casa_pct"),
        "nim_pct": _sources(fin, "nim_pct"),
        "cost_to_income_pct": _sources(fin, "cost_to_income_pct"),
        "roe": _sources(fin, "profit_for_the_year", "total_equity"),
        "roa": _sources(fin, "profit_for_the_year", "total_assets"),
        "pat_cagr_3yr": [],  # filled in by build_multi_year_metrics
    }

    return ratios, evidence_sources


# These thresholds are heuristic bands, not certainties, so a metric score
# should never be a flat, perfect 100 (and since dimension/composite scores
# are weighted averages of metric scores, that flows through to keep those
# below 100 too). But a value that clears the "safe" line by a little vs. a
# lot is genuinely different -- so instead of a flat ceiling, scores beyond
# "safe" keep climbing on a curve that approaches SCORE_CEILING but never
# reaches it. SAFE_LINE_SCORE is what a value exactly at the "safe" line
# scores (both branches agree here, so the curve is continuous); anything
# beyond keeps rising towards SCORE_CEILING with diminishing returns, so two
# companies that both clear the bar are still told apart, and a value can
# never land on exactly SCORE_CEILING or 100.
SCORE_CEILING = 99.9
SAFE_LINE_SCORE = 90.0
# How fast a score approaches SCORE_CEILING once a value clears "safe". This is
# scaled by (excess / band width), and several of this pipeline's real bands are
# narrow (e.g. quick_ratio's safe-distress band is only 0.3) -- a genuinely
# healthy company can clear such a band by 3-4x without being an outlier at all.
# EXCESS_DECAY=1.0 saturated almost every dimension to 99.9 for any solidly
# healthy company, recreating the exact "everything looks the same" problem
# this curve was meant to fix. 0.2 keeps scores visibly distinct out to roughly
# 10-15x the band width beyond "safe", which comfortably covers realistic
# ranges for these metrics.
EXCESS_DECAY = 0.2


def score_metric(name, value, threshold=None):
    """Returns a 0-SCORE_CEILING score (higher = safer) for one ratio value
    against its threshold band. Values between distress and safe are scored
    linearly; values beyond safe keep climbing asymptotically towards
    SCORE_CEILING rather than flattening to one number."""
    if value is None or threshold is None:
        return None
    safe, distress, direction = threshold["safe"], threshold["distress"], threshold["direction"]
    band = abs(safe - distress) or 1.0  # guard divide-by-zero if safe == distress

    if direction == "higher_is_safer":
        excess = value - safe
    else:  # lower_is_safer
        excess = safe - value

    if excess >= 0:
        headroom = SCORE_CEILING - SAFE_LINE_SCORE
        return round(SCORE_CEILING - headroom * math.exp(-EXCESS_DECAY * excess / band), 2)

    if direction == "higher_is_safer":
        if value <= distress:
            return 0.0
        return round(SAFE_LINE_SCORE * (value - distress) / (safe - distress), 2)
    else:
        if value >= distress:
            return 0.0
        return round(SAFE_LINE_SCORE * (distress - value) / (distress - safe), 2)


def risk_label(score):
    if score is None:
        return "N/A"
    if score >= 70:
        return "LOW"
    if score >= 45:
        return "MEDIUM"
    return "HIGH"


def _score_dimensions(metric_scores, dimension_metrics):
    """
    Scores each dimension, gated on config.MIN_DIMENSION_COVERAGE: a
    dimension only gets a numeric score if at least that fraction of its
    own metrics actually have a score. Below that, the dimension's score is
    None and its risk_label reads "N/A (insufficient data)" instead of
    quietly averaging over whichever one metric happened to be available.
    """
    dimension_scores = {}
    for dim, metrics in dimension_metrics.items():
        available = [m for m in metrics if metric_scores.get(m, {}).get("score") is not None]
        coverage = round(len(available) / len(metrics), 2) if metrics else 0.0
        scores = [metric_scores[m]["score"] for m in available]
        raw_score = round(sum(scores) / len(scores), 1) if scores else None
        enough_data = coverage >= MIN_DIMENSION_COVERAGE
        dim_score = raw_score if enough_data else None
        dimension_scores[dim] = {
            "score": dim_score,
            "metrics": metrics,
            "metrics_available": available,
            "coverage": coverage,
            "risk_label": risk_label(dim_score) if enough_data else "N/A (insufficient data)",
        }
    return dimension_scores


def _finalize_composite(dimension_scores, composite_weights):
    """
    Weighted-averages dimension scores into a composite, gated on
    config.MIN_COMPOSITE_COVERAGE: the composite is only reported as a
    number if at least that fraction of the TOTAL composite weight is
    backed by dimensions that themselves passed the per-dimension coverage
    bar. Otherwise composite_score is None and composite_risk_label reads
    "INSUFFICIENT DATA" -- this is the fix for the bug where a single
    well-scoring dimension could get renormalized all the way up to a
    misleading "100".

    Returns (composite_score, composite_risk_label, data_coverage) where
    data_coverage is the fraction of total weight actually backed by data,
    surfaced in the risk report and in the dashboard/LLM payload so low
    confidence is visible, not just implied.
    """
    weighted_sum, weight_total = 0.0, 0.0
    for dim, w in composite_weights.items():
        s = dimension_scores.get(dim, {}).get("score")
        if s is not None:
            weighted_sum += s * w
            weight_total += w

    total_weight = sum(composite_weights.values()) or 1.0
    data_coverage = round(weight_total / total_weight, 2)

    if data_coverage >= MIN_COMPOSITE_COVERAGE:
        composite = round(weighted_sum / weight_total, 1)
        composite_label = risk_label(composite)
    else:
        composite = None
        composite_label = "INSUFFICIENT DATA"

    return composite, composite_label, data_coverage


def build_risk_report(extracted, scope="consolidated", company_type="industrial"):
    fin = extracted[scope]
    thresholds, dimension_metrics, composite_weights = _scoring_config(company_type)

    if company_type == "bank":
        ratios, evidence_sources = compute_bank_ratios(fin)
    else:
        ratios, evidence_sources = compute_industrial_ratios(fin)

    cur = ratios["current_year"]
    prior = ratios["prior_year"]

    metric_scores = {}
    for name, value in cur.items():
        metric_scores[name] = {
            "value": value,
            "prior_value": prior.get(name),
            "score": score_metric(name, value, thresholds.get(name)),
            "threshold": thresholds.get(name),
            "sources": evidence_sources.get(name, []),
        }

    dimension_scores = _score_dimensions(metric_scores, dimension_metrics)
    composite, composite_label, data_coverage = _finalize_composite(dimension_scores, composite_weights)

    return {
        "scope": scope,
        "company_type": company_type,
        "metrics": metric_scores,
        "dimensions": dimension_scores,
        "composite_score": composite,
        "composite_risk_label": composite_label,
        "data_coverage": data_coverage,
    }


def apply_manual_inputs(risk_report, manual_inputs, company_type="industrial"):
    """
    manual_inputs: dict of {metric_name: value} matching config.MANUAL_METRICS keys
    (booleans as True/False, counts/percentages as numbers). Missing metrics are
    left as None ("N/A - needs manual input") rather than assumed safe.

    Mutates and returns risk_report with the manual dimensions added/updated and
    the composite score recalculated across ALL dimensions (extracted + manual),
    using the same coverage-gated logic as build_risk_report().
    """
    manual_inputs = manual_inputs or {}
    _, _, composite_weights = _scoring_config(company_type)

    for metric_name, cfg in MANUAL_METRICS.items():
        raw = manual_inputs.get(metric_name)
        if isinstance(raw, bool):
            raw = 1 if raw else 0
        risk_report["metrics"][metric_name] = {
            "value": raw,
            "prior_value": None,
            "score": score_metric(metric_name, raw, threshold=cfg),
            "threshold": cfg,
            "sources": [{"note": "manual input -- not extracted from the PDF"}] if raw is not None else [],
        }

    for dim, metric_names in MANUAL_DIMENSIONS.items():
        existing = risk_report["dimensions"].get(dim)
        all_metric_names = list(existing["metrics"]) + metric_names if existing else list(metric_names)
        available = [m for m in all_metric_names if risk_report["metrics"].get(m, {}).get("score") is not None]
        coverage = round(len(available) / len(all_metric_names), 2) if all_metric_names else 0.0
        scores = [risk_report["metrics"][m]["score"] for m in available]
        raw_score = round(sum(scores) / len(scores), 1) if scores else None
        enough_data = coverage >= MIN_DIMENSION_COVERAGE
        dim_score = raw_score if enough_data else None
        risk_report["dimensions"][dim] = {
            "score": dim_score,
            "metrics": all_metric_names,
            "metrics_available": available,
            "coverage": coverage,
            "risk_label": risk_label(dim_score) if enough_data else "N/A (insufficient data)",
        }

    composite, composite_label, data_coverage = _finalize_composite(risk_report["dimensions"], composite_weights)
    risk_report["composite_score"] = composite
    risk_report["composite_risk_label"] = composite_label
    risk_report["data_coverage"] = data_coverage

    return risk_report


def recompute_after_multiyear(risk_report, multi_year, company_type="industrial"):
    """
    Called once 3+ years of PAT history are available (see
    build_multi_year_metrics below): fills in pat_cagr_3yr's real value in
    place of the None it started as, rescoring the Profitability dimension
    and the composite with the same coverage-gated logic as everywhere
    else. Mutates and returns risk_report.
    """
    thresholds, dimension_metrics, composite_weights = _scoring_config(company_type)
    cagr = multi_year.get("pat_cagr_3yr")
    if cagr is None or "pat_cagr_3yr" not in risk_report["metrics"]:
        return risk_report

    risk_report["metrics"]["pat_cagr_3yr"]["value"] = cagr
    risk_report["metrics"]["pat_cagr_3yr"]["score"] = score_metric("pat_cagr_3yr", cagr, thresholds.get("pat_cagr_3yr"))
    risk_report["metrics"]["pat_cagr_3yr"]["sources"] = multi_year.get("pat_cagr_sources", [])

    prof_dim = "Profitability"
    if prof_dim in dimension_metrics:
        metrics = dimension_metrics[prof_dim]
        available = [m for m in metrics if risk_report["metrics"].get(m, {}).get("score") is not None]
        coverage = round(len(available) / len(metrics), 2) if metrics else 0.0
        scores = [risk_report["metrics"][m]["score"] for m in available]
        raw_score = round(sum(scores) / len(scores), 1) if scores else None
        enough_data = coverage >= MIN_DIMENSION_COVERAGE
        risk_report["dimensions"][prof_dim] = {
            "score": raw_score if enough_data else None,
            "metrics": metrics,
            "metrics_available": available,
            "coverage": coverage,
            "risk_label": (risk_label(raw_score) if enough_data else "N/A (insufficient data)"),
        }

    composite, composite_label, data_coverage = _finalize_composite(risk_report["dimensions"], composite_weights)
    risk_report["composite_score"] = composite
    risk_report["composite_risk_label"] = composite_label
    risk_report["data_coverage"] = data_coverage

    return risk_report


def build_multi_year_metrics(combined, scope="consolidated"):
    """
    combined: the dict loaded from a company's combined.json (batch_extract.py
    output), i.e. {"years_available": [...], "by_year": {fy_label: year_record}}.

    Computes metrics that need 3+ years of history (currently: 3-yr PAT CAGR,
    same definition for industrials and banks since profit_for_the_year is
    extracted for both) and a simple auditor-change flag between the two most
    recent years. Returns a dict to merge into the latest year's risk_report.
    """
    years = sorted(combined["years_available"])
    if len(years) < 1:
        return {}

    result = {"years_used": years}

    # 3-yr PAT CAGR needs profit_for_the_year from a start point 3 fiscal
    # years before the latest, i.e. 4 data points spanning 3 periods of growth.
    if len(years) >= 4:
        start_year, end_year = years[-4], years[-1]
        start_pat = combined["by_year"][start_year]["extracted"][scope].get("profit_for_the_year", {}).get("current_year")
        end_pat = combined["by_year"][end_year]["extracted"][scope].get("profit_for_the_year", {}).get("current_year")
        if start_pat and end_pat and start_pat > 0:
            cagr = (end_pat / start_pat) ** (1 / 3) - 1
            result["pat_cagr_3yr"] = round(cagr, 4)
            result["pat_cagr_sources"] = [
                {"fiscal_year": start_year, "value": start_pat},
                {"fiscal_year": end_year, "value": end_pat},
            ]
    else:
        result["pat_cagr_3yr"] = None
        result["pat_cagr_note"] = f"Needs 4 fiscal years of data (3 years of growth); {len(years)} currently loaded."

    # Auditor change between the two most recent years
    if len(years) >= 2:
        prev_auditor = combined["by_year"][years[-2]].get("auditor")
        latest_auditor = combined["by_year"][years[-1]].get("auditor")
        if prev_auditor and latest_auditor:
            result["auditor_changed"] = prev_auditor != latest_auditor
            result["auditor_prev"] = prev_auditor
            result["auditor_latest"] = latest_auditor

    return result


if __name__ == "__main__":
    in_json, out_json = sys.argv[1], sys.argv[2]
    with open(in_json) as f:
        data = json.load(f)
    company_type = data.get("company_type", "industrial")
    report = build_risk_report(data["extracted"], scope="consolidated", company_type=company_type)
    with open(out_json, "w") as f:
        json.dump(report, f, indent=2)
    print(f"Composite risk score: {report['composite_score']} ({report['composite_risk_label']}) "
          f"[data_coverage={report['data_coverage']}] -> {out_json}")