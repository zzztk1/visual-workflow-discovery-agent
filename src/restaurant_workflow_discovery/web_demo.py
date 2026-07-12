from __future__ import annotations

import json
import mimetypes
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .agent import DEFAULT_CONFIG, RestaurantWorkflowDiscoveryAgent
from .langgraph_runner import run_with_langgraph


ROOT = Path(__file__).resolve().parents[2]
STATIC_DIR = ROOT / "web"
OUTPUT_DIR = ROOT / "outputs"


class DemoHandler(BaseHTTPRequestHandler):
    def _send_json(self, payload: object, status: int = 200) -> None:
        raw = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _send_text(self, text: str, status: int = 200, content_type: str = "text/plain") -> None:
        raw = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            return self._serve_static("index.html")
        if parsed.path == "/api/health":
            return self._send_json(
                {
                    "status": "ok",
                    "agent": "RestaurantWorkflowDiscoveryAgent",
                    "demo": "web",
                    "outputs": str(OUTPUT_DIR),
                    "fallback_evidence_pool": True,
                    "runners": ["state-machine", "langgraph"],
                }
            )
        if parsed.path in {"/api/run", "/api/discovery", "/api/deep-dive"}:
            params = parse_qs(parsed.query)
            request_id = params.get("request_id", ["web-demo"])[0]
            lock_index_raw = params.get("lock_index", [""])[0]
            lock_index = int(lock_index_raw) if lock_index_raw.strip().isdigit() else None
            config = dict(DEFAULT_CONFIG)
            config["industry"] = params.get("industry", [config["industry"]])[0]
            config["goal"] = params.get("goal", [config["goal"]])[0]
            config["live_search"] = params.get("live_search", ["true"])[0].lower() == "true"
            config["llm_enabled"] = params.get("llm_enabled", ["true"])[0].lower() == "true"
            agent = RestaurantWorkflowDiscoveryAgent(
                config=config,
                out_root=OUTPUT_DIR,
                request_id=request_id,
            )
            mode = "discovery" if parsed.path == "/api/discovery" else "all"
            runner = params.get("runner", ["state-machine"])[0]
            if runner == "langgraph":
                state = run_with_langgraph(agent, mode=mode, lock_index=lock_index)
            else:
                state = agent.run(mode=mode, lock_index=lock_index)
            return self._send_json(self._run_payload(agent, state))
        if parsed.path.startswith("/outputs/"):
            return self._serve_output(parsed.path.removeprefix("/outputs/"))
        return self._serve_static(parsed.path.lstrip("/"))

    def _run_payload(self, agent: RestaurantWorkflowDiscoveryAgent, state: dict) -> dict:
        run_dir = agent.run_dir

        def read_json(name: str):
            return json.loads((run_dir / name).read_text(encoding="utf-8"))

        def read_text(name: str):
            return (run_dir / name).read_text(encoding="utf-8")

        selected = state.get("selected_workflow") or {}
        return {
            "run_id": state["run_id"],
            "request_id": state["request_id"],
            "source_mode": state.get("source_mode"),
            "execution_engine": state.get("execution_engine"),
            "llm_mode": "stepfun_real_api" if agent.llm.configured else "rules_fallback",
            "outputs_dir": str(run_dir),
            "links": {
                "final_report": f"/outputs/{state['run_id']}/final_report.md",
                "trace": f"/outputs/{state['run_id']}/trace.json",
                "metrics": f"/outputs/{state['run_id']}/metrics.json",
                "health": f"/outputs/{state['run_id']}/health.json",
                "llm_usage": f"/outputs/{state['run_id']}/llm_usage.json",
            },
            "candidates": read_json("candidate_workflows.json"),
            "selected": selected,
            "evidence": read_json("evidence_links.json"),
            "painpoints": read_json("painpoints.json") if (run_dir / "painpoints.json").exists() else [],
            "agent_interventions": read_json("agent_interventions.json") if (run_dir / "agent_interventions.json").exists() else [],
            "product_solution": read_json("product_solution.json") if (run_dir / "product_solution.json").exists() else {},
            "causal_audit": read_json("causal_audit.json") if (run_dir / "causal_audit.json").exists() else {},
            "original_mmd": read_text("original_workflow.mmd") if (run_dir / "original_workflow.mmd").exists() else "",
            "agent_mmd": read_text("agent_workflow.mmd") if (run_dir / "agent_workflow.mmd").exists() else "",
            "metrics": read_json("metrics.json"),
            "trace": read_json("trace.json"),
            "risk_review_items": state.get("risk_review", []),
            "risk_review": read_text("risk_review.md") if (run_dir / "risk_review.md").exists() else "",
            "final_report": read_text("final_report.md"),
        }

    def _serve_static(self, relative: str) -> None:
        path = (STATIC_DIR / relative).resolve()
        if not str(path).startswith(str(STATIC_DIR.resolve())) or not path.exists():
            return self._send_text("Not found", 404)
        content_type = mimetypes.guess_type(str(path))[0] or "application/octet-stream"
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _serve_output(self, relative: str) -> None:
        path = (OUTPUT_DIR / relative).resolve()
        if not str(path).startswith(str(OUTPUT_DIR.resolve())) or not path.exists():
            return self._send_text("Not found", 404)
        content_type = mimetypes.guess_type(str(path))[0] or "text/plain"
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> int:
    host = "127.0.0.1"
    port = 7860
    server = ThreadingHTTPServer((host, port), DemoHandler)
    print(f"Restaurant Workflow Discovery Agent demo: http://{host}:{port}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
