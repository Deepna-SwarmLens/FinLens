"""
batch_extract.py -- drop any number of annual report PDFs into a folder and
run this. For each PDF it:
  1. auto-detects the company name + fiscal year
  2. runs the line-item extractor (with page citations)
  3. runs the risk engine
  4. writes data/<company_slug>/<fy_label>.json (single-year)
And across all PDFs for the same company, it also writes:
  5. data/<company_slug>/combined.json (multi-year, for trend charts)

Usage:
    python batch_extract.py <uploads_dir> <data_out_dir>

Re-running is safe: files are keyed by (company, fiscal year), so
re-uploading the same year's report just overwrites that one entry, and a
new year gets added onto the existing combined.json.
"""

import sys
import os
import re
import json
import glob
import pdfplumber
import validate
print("validate imported from:", validate.__file__)
from extractor import run_extraction
from risk_engine import (
    build_risk_report, apply_manual_inputs, build_multi_year_metrics,
    recompute_after_multiyear,
)
from llm_analysis import generate_llm_analysis
from auto_detect import detect_company_name, detect_fiscal_year, detect_from_filename
from validate import validate_scope, HIGH, BANK_MANDATORY_FIELDS

from dotenv import load_dotenv
load_dotenv()

def slugify(name):
    return re.sub(r'[^a-z0-9]+', '_', name.lower()).strip('_')


def process_one_pdf(pdf_path, data_out_dir):
    company, fy_label, report_label = detect_from_filename(pdf_path)
    if company and fy_label:
        print(f"  Detected from filename: {company}, {report_label}")
    else:
        with pdfplumber.open(pdf_path) as pdf:
            company = detect_company_name(pdf)
            fy_label, report_label = detect_fiscal_year(pdf)

        if not company or not fy_label:
            print(f"  SKIPPED {os.path.basename(pdf_path)}: could not auto-detect "
                  f"company={company!r} fiscal_year={fy_label!r}. "
                  f"This report's phrasing may not match the Ind-AS pattern this "
                  f"tool expects -- check auto_detect.py.")
            return None

        print(f"  Detected from PDF content: {company}, {report_label}")

    extracted, citations, auditor, company_type = run_extraction(pdf_path, company, report_label, verbose=True)  # TEMP: verbose=True for debugging
    if company_type == "bank":
        print(f"  NOTE: detected as a bank (RBI Schedule III format) -- using bank-specific fields, "
              f"which is a newer, less-tested code path than the industrial one.")

    mandatory_fields = BANK_MANDATORY_FIELDS if company_type == "bank" else None
    validation = validate_scope(extracted, scope="consolidated", mandatory_fields=mandatory_fields)
    if validation["status"] != "PASS":
        print(f"  VALIDATION {validation['status']}: {validation['flag_counts']}")
        for flag in validation["flags"]:
            if flag["severity"] == HIGH:
                print(f"    [HIGH][L{flag['layer']}] {flag['field']}: {flag['message']}")
        if validation["fields_needing_reextraction"]:
            print(f"  -> flagged for re-extraction (not yet auto-re-run, see Layer 6 note in validate.py): "
                  f"{validation['fields_needing_reextraction']}")

    if validation["status"] == "FAIL":
        print(f"  WARNING: validation FAILED ({validation['flag_counts']}) — risk score below may be unreliable.")

    risk_report = build_risk_report(extracted, scope="consolidated", company_type=company_type)
    print(f"  Risk score: {risk_report['composite_score']} ({risk_report['composite_risk_label']}) "
          f"[data_coverage={risk_report['data_coverage']}]")

    company_slug = slugify(company)
    company_dir = os.path.join(data_out_dir, company_slug)
    os.makedirs(company_dir, exist_ok=True)

    manual_path = os.path.join(company_dir, "manual_inputs.json")
    if os.path.exists(manual_path):
        with open(manual_path) as f:
            manual_inputs = {k: v for k, v in json.load(f).items() if not k.startswith("_") and v is not None}
        risk_report = apply_manual_inputs(risk_report, manual_inputs, company_type=company_type)
        print(f"  Applied manual inputs from {manual_path} ({len(manual_inputs)} fields)")

    year_record = {
        "company": company,
        "company_type": company_type,
        "fiscal_year": fy_label,
        "report_label": report_label,
        "source_pdf": os.path.basename(pdf_path),
        "auditor": auditor,
        "extracted": extracted,
        "validation": validation,
        "risk_report": risk_report,
    }
    out_path = os.path.join(company_dir, f"{fy_label}.json")
    with open(out_path, "w") as f:
        json.dump(year_record, f, indent=2)

    return company_slug, fy_label, year_record


def rebuild_combined(data_out_dir, company_slug):
    company_dir = os.path.join(data_out_dir, company_slug)
    year_files = sorted(glob.glob(os.path.join(company_dir, "FY*.json")))
    years = []
    for yf in year_files:
        with open(yf) as f:
            years.append(json.load(f))
    years.sort(key=lambda y: y["fiscal_year"])

    combined = {
        "company": years[0]["company"] if years else company_slug,
        "years_available": [y["fiscal_year"] for y in years],
        "by_year": {y["fiscal_year"]: y for y in years},
    }

    if not years:
        with open(os.path.join(company_dir, "combined.json"), "w") as f:
            json.dump(combined, f, indent=2)
        return combined

    latest = years[-1]
    company_type = latest.get("company_type", "industrial")

    multi_year = build_multi_year_metrics(combined)
    if multi_year.get("pat_cagr_3yr") is not None:
        latest["risk_report"] = recompute_after_multiyear(latest["risk_report"], multi_year, company_type=company_type)
        combined["by_year"][latest["fiscal_year"]] = latest
    combined["multi_year_metrics"] = multi_year

    cross_year_validation = None
    if len(years) >= 2:
        from validate import layer5_cross_year
        cross_flags = layer5_cross_year(combined, scope="consolidated")
        high = [f for f in cross_flags if f["severity"] == "HIGH"]
        combined["cross_year_validation"] = {
            "status": "FAIL" if high else ("WARN" if cross_flags else "PASS"),
            "flags": cross_flags,
        }
        cross_year_validation = combined["cross_year_validation"]
        if cross_flags:
            print(f"  CROSS-YEAR VALIDATION: {len(cross_flags)} flag(s) comparing independently-extracted years")
            for flag in cross_flags:
                print(f"    [{flag['severity']}] {flag['field']}: {flag['message']}")

    # LLM narrative analysis. The latest year gets the fully-updated
    # (post multi-year, post cross-year-validation) context. Older years
    # only get their own single-year risk_report/validation -- multi_year
    # and cross_year_validation describe the trend *up to the latest year*,
    # which wasn't known as of an earlier filing, so it isn't passed for
    # those. Never blocks the pipeline -- see llm_analysis.py's own docstring.
    #
    # Backfill: any year missing llm_analysis (or whose last attempt errored)
    # gets one generated here too, not just latest -- otherwise older years
    # stay permanently blank even after re-running the batch.
    changed_years = []
    for y in years:
        is_latest = y["fiscal_year"] == latest["fiscal_year"]
        existing = y.get("llm_analysis")
        if existing and "error" not in existing:
            continue  # already have a usable narrative for this year

        llm_result = generate_llm_analysis(
            company=y["company"],
            fy_label=y["fiscal_year"],
            report_label=y.get("report_label"),
            company_type=y.get("company_type", company_type),
            risk_report=y["risk_report"],
            validation=y.get("validation"),
            multi_year=multi_year if is_latest else None,
            cross_year_validation=cross_year_validation if is_latest else None,
        )
        y["llm_analysis"] = llm_result
        combined["by_year"][y["fiscal_year"]] = y
        changed_years.append(y)
        if "error" in llm_result:
            print(f"  LLM analysis ({y['fiscal_year']}): {llm_result['error']}")
        else:
            print(f"  LLM analysis ({y['fiscal_year']}): {llm_result.get('headline', '')}")

    # persist each updated year back to its own file too
    for y in changed_years:
        with open(os.path.join(company_dir, f"{y['fiscal_year']}.json"), "w") as f:
            json.dump(y, f, indent=2)

    with open(os.path.join(company_dir, "combined.json"), "w") as f:
        json.dump(combined, f, indent=2)
    return combined


def run_batch(uploads_dir, data_out_dir):
    pdf_paths = sorted(glob.glob(os.path.join(uploads_dir, "*.pdf")))
    if not pdf_paths:
        print(f"No PDFs found in {uploads_dir}")
        return

    touched_companies = set()
    for pdf_path in pdf_paths:
        print(f"Processing {os.path.basename(pdf_path)} ...")
        result = process_one_pdf(pdf_path, data_out_dir)
        if result:
            company_slug, fy_label, _ = result
            touched_companies.add(company_slug)
            print(f"  -> saved {company_slug}/{fy_label}.json")

    for company_slug in touched_companies:
        combined = rebuild_combined(data_out_dir, company_slug)
        print(f"{combined['company']}: years available = {combined['years_available']}")


if __name__ == "__main__":
    uploads_dir = sys.argv[1] if len(sys.argv) > 1 else "/mnt/user-data/uploads"
    data_out_dir = sys.argv[2] if len(sys.argv) > 2 else "data"
    run_batch(uploads_dir, data_out_dir)