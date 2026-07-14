"""
llm_analysis.py -- generates a plain-English risk narrative from the
extracted financials + computed risk_report, using the Groq API.

This is a SUMMARIZATION layer on top of numbers FinLens already computed and
verified -- the model is never given raw PDF text and is never asked to
compute a number itself. It only narrates values this pipeline already
extracted, scored, and cited; every number in its prompt is passed explicitly,
and it's told not to invent anything beyond that.

Requires:
    pip install groq
    export GROQ_API_KEY=...

Never raises on a missing key/package/network failure -- by the time this
runs, the extraction and risk score are already saved to disk, so a broken
LLM call should degrade to "no narrative this run", not blow up the batch.
"""

import os
import json
from datetime import datetime, timezone

try:
    from groq import Groq
except ImportError:
    Groq = None

# Groq model to use. llama-3.3-70b-versatile is a solid default for this kind
# of structured-JSON summarization task; swap to a smaller/faster model
# (e.g. llama-3.1-8b-instant) if you want lower latency and don't mind a bit
# less nuance in the narrative. Check https://console.groq.com/docs/models
# for the current list -- Groq deprecates/renames models more often than
# Anthropic does.
MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """You are a credit/equity risk analyst assistant. You will be given \
a JSON payload of ALREADY-COMPUTED financial ratios, risk scores, and validation \
flags for one Indian company, extracted from its Ind-AS annual report by a \
rules-based pipeline -- not by you.

Rules:
- Do NOT invent, estimate, or "correct" any number. Only reference values present \
in the payload.
- If data_coverage is low, or a dimension reads "N/A (insufficient data)" or \
composite_risk_label is "INSUFFICIENT DATA", say so plainly -- do not imply a \
confidence the data doesn't support, and do not paper over the gap.
- Be specific: cite the actual metric name and value from the payload when making \
a claim (e.g. "Net NPA at 3.4% is above the distress band" not "asset quality looks weak").
- Keep the tone factual and neutral, like an analyst's note -- not marketing copy, \
not alarmist.
- Respond with ONLY a JSON object, no markdown code fences, no preamble, no \
trailing text, matching exactly this schema:
{
  "headline": "one sentence, at most 25 words, plain-English verdict",
  "key_strengths": ["...", "..."],
  "key_concerns": ["...", "..."],
  "data_caveats": ["...", "..."],
  "watch_items": ["...", "..."]
}
Where:
- key_strengths: 2-4 items, each citing a specific metric+value. Empty list if genuinely none.
- key_concerns: 2-4 items, each citing a specific metric+value or a validation flag. Empty list if genuinely none.
- data_caveats: gaps/limitations to flag -- low data_coverage, "insufficient data" \
dimensions, a FAIL/WARN validation status, fields still needing manual input. Empty \
list only if coverage is high and validation passed cleanly.
- watch_items: 1-3 concrete next steps for a human reviewer (e.g. "verify borrowings \
figure against source PDF", "supply manual_inputs.json for Compliance/Legal", \
"re-run extraction once bank_key_ratios patterns are confirmed against this filing").
"""


def _build_payload(company, fy_label, report_label, company_type, risk_report,
                    validation=None, multi_year=None, cross_year_validation=None):
    payload = {
        "company": company,
        "fiscal_year": fy_label,
        "report_label": report_label,
        "company_type": company_type,
        "composite_score": risk_report.get("composite_score"),
        "composite_risk_label": risk_report.get("composite_risk_label"),
        "data_coverage": risk_report.get("data_coverage"),
        "dimensions": {
            dim: {
                "score": d.get("score"),
                "risk_label": d.get("risk_label"),
                "coverage": d.get("coverage"),
            }
            for dim, d in risk_report.get("dimensions", {}).items()
        },
        "metrics": {
            name: {"value": m.get("value"), "prior_value": m.get("prior_value"), "score": m.get("score")}
            for name, m in risk_report.get("metrics", {}).items()
        },
    }
    if validation:
        payload["validation_status"] = validation.get("status")
        payload["validation_high_flags"] = [
            {"field": f["field"], "message": f["message"]}
            for f in validation.get("flags", [])
            if f.get("severity") == "HIGH"
        ]
    if multi_year:
        payload["multi_year"] = {k: v for k, v in multi_year.items() if k != "pat_cagr_sources"}
    if cross_year_validation:
        payload["cross_year_validation"] = cross_year_validation
    return payload


def generate_llm_analysis(company, fy_label, report_label, company_type, risk_report,
                           validation=None, multi_year=None, cross_year_validation=None,
                           model=MODEL):
    """
    Returns a dict to store under year_record["llm_analysis"]:
      {"headline", "key_strengths", "key_concerns", "data_caveats", "watch_items",
       "generated_at", "model", "based_on_composite_score"}
    or, if generation wasn't possible, {"error": "..."} -- callers should check
    for "error" rather than assume the narrative fields exist.
    """
    if Groq is None:
        return {"error": "groq package not installed -- run: pip install groq"}
    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        return {"error": "GROQ_API_KEY not set -- LLM analysis skipped"}

    payload = _build_payload(company, fy_label, report_label, company_type, risk_report,
                              validation, multi_year, cross_year_validation)

    try:
        client = Groq(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            max_tokens=1000,
            temperature=0.3,
            response_format={"type": "json_object"},  # Groq's JSON mode -- supported on llama-3.x models
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, indent=2)},
            ],
        )
        text = response.choices[0].message.content.strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:]
        parsed = json.loads(text.strip())
    except json.JSONDecodeError as e:
        return {"error": f"LLM returned non-JSON output: {e}"}
    except Exception as e:
        return {"error": f"LLM analysis failed: {e}"}

    parsed.setdefault("key_strengths", [])
    parsed.setdefault("key_concerns", [])
    parsed.setdefault("data_caveats", [])
    parsed.setdefault("watch_items", [])
    parsed["generated_at"] = datetime.now(timezone.utc).isoformat()
    parsed["model"] = model
    parsed["based_on_composite_score"] = risk_report.get("composite_score")
    return parsed


if __name__ == "__main__":
    import sys
    in_json = sys.argv[1]
    with open(in_json) as f:
        data = json.load(f)
    result = generate_llm_analysis(
        company=data["company"],
        fy_label=data["fiscal_year"],
        report_label=data.get("report_label"),
        company_type=data.get("company_type", "industrial"),
        risk_report=data["risk_report"],
        validation=data.get("validation"),
    )
    print(json.dumps(result, indent=2))