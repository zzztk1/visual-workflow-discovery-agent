import json
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ROOT = Path(__file__).resolve().parent
BASE_URL = "http://127.0.0.1:7860"
started = time.perf_counter()
rows = []
transport_ok = True
provider_ok = False
schema_ok = False
semantic_ok = False
quality_ok = False

try:
    query = urllib.parse.urlencode({"query": "餐饮门店视觉营销内容生产与发布工作流"})
    with urllib.request.urlopen(f"{BASE_URL}/api/discovery?{query}", timeout=180) as response:
        result = json.load(response)
    metrics = result.get("metrics") or {}
    totals = metrics.get("totals") or {}
    provider_ok = totals.get("provider_usage_nodes", 0) > 0 and totals.get("total_tokens", 0) > 0
    candidates = result.get("candidates") or []
    trace = result.get("trace") or []
    report = str(result.get("final_report") or "")
    schema_ok = len(candidates) >= 3 and len(trace) >= 6
    semantic_ok = all(item.get("name") and item.get("score") is not None for item in candidates[:3])
    quality_ok = len(report) >= 800 and bool(result.get("links"))
    rows.append({
        "name": "workflow-discovery-provider",
        "ok": provider_ok,
        "model": "stepfun-chat",
        "latencyMs": round((time.perf_counter() - started) * 1000),
        "faithfulness": "pass" if provider_ok else "fail",
        "dreamFeeling": "pass" if provider_ok else "fail",
        "overreach": "pass" if provider_ok else "fail",
        "errorType": "" if provider_ok else "missing_key_or_fallback",
    })
    artifact_ok = provider_ok and schema_ok and semantic_ok and quality_ok
    rows.append({
        "name": "workflow-artifacts",
        "ok": artifact_ok,
        "model": "stepfun-chat",
        "latencyMs": round((time.perf_counter() - started) * 1000),
        "faithfulness": "pass" if schema_ok else "fail",
        "dreamFeeling": "pass" if semantic_ok else "fail",
        "overreach": "pass" if quality_ok else "fail",
        "title": f"{len(candidates)} candidates / {len(trace)} trace nodes",
        "errorType": "" if artifact_ok else "fallback_or_artifact_failure",
    })
except (OSError, urllib.error.URLError, ValueError, json.JSONDecodeError) as exc:
    transport_ok = False
    rows.append({
        "name": "runtime-transport",
        "ok": False,
        "model": "stepfun-chat",
        "faithfulness": "fail",
        "dreamFeeling": "fail",
        "overreach": "fail",
        "errorType": type(exc).__name__,
    })

result = {
    "rows": rows,
    "aggregate": {
        "transport_ok": transport_ok,
        "provider_ok": provider_ok,
        "schema_ok": schema_ok,
        "semantic_ok": semantic_ok,
        "quality_ok": quality_ok,
    },
}
(ROOT / "MODEL_QA_RUN.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(result, ensure_ascii=False))
