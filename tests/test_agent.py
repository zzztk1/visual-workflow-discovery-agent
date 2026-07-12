import json
import os
import tempfile
import unittest
from pathlib import Path

from src.restaurant_workflow_discovery.agent import RestaurantWorkflowDiscoveryAgent
from src.restaurant_workflow_discovery.evidence_pool import FALLBACK_EVIDENCE_POOL
from src.restaurant_workflow_discovery.langgraph_runner import run_with_langgraph


class RestaurantWorkflowDiscoveryAgentTests(unittest.TestCase):
    def run_agent(self, evidence_pool=None, mode="all", lock_index=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        agent = RestaurantWorkflowDiscoveryAgent(
            config={"llm_enabled": False},
            evidence_pool=evidence_pool or list(FALLBACK_EVIDENCE_POOL),
            out_root=Path(tmp.name),
            request_id="test-request",
        )
        state = agent.run(mode=mode, lock_index=lock_index)
        return agent, state

    def test_normal_discovery_outputs_candidates(self):
        _, state = self.run_agent()
        candidates = state["candidate_workflows"]
        self.assertGreaterEqual(len(candidates), 3)
        for candidate in candidates:
            self.assertIn("score", candidate)
            self.assertIn("steps", candidate)
            self.assertIn("evidence_links", candidate)

    def test_evidence_insufficient_is_flagged_and_scored_down(self):
        limited_pool = [
            {
                "id": "only_one",
                "title": "Only one evidence",
                "url": "https://example.com/one",
                "workflow": "餐饮单证据工作流",
                "proof_point": "Only one proof.",
                "step_refs": ["A", "B", "C", "D"],
                "inefficiencies": ["人工重复", "信息搬运"],
                "source_type": "test",
            }
        ]
        _, state = self.run_agent(evidence_pool=limited_pool, mode="discovery")
        candidate = state["candidate_workflows"][0]
        self.assertTrue(candidate["evidence_insufficient"])
        self.assertLessEqual(candidate["score_detail"]["evidence_authenticity"], 10)

    def test_noise_workflow_is_filtered(self):
        _, state = self.run_agent(mode="discovery")
        self.assertTrue(
            all("纯 AI 生图娱乐" not in item["name"] for item in state["candidate_workflows"])
        )

    def test_human_lock_only_deep_dives_selected_candidate(self):
        _, state = self.run_agent(lock_index=1)
        selected = state["selected_workflow"]
        self.assertEqual(selected["human_lock"]["selected_index"], 1)
        self.assertEqual(state["original_workflow"]["name"], selected["name"])

    def test_risk_review_requires_human_confirmation(self):
        _, state = self.run_agent()
        risks = state["risk_review"]
        self.assertTrue(any(item["requires_human_confirmation"] for item in risks))

    def test_expected_output_files_exist(self):
        agent, _ = self.run_agent()
        expected = {
            "final_report.md",
            "trace.json",
            "metrics.json",
            "health.json",
            "llm_usage.json",
            "evidence_links.json",
            "candidate_workflows.json",
            "selected_workflow.json",
            "painpoints.json",
            "agent_interventions.json",
            "product_solution.json",
            "causal_audit.json",
            "original_workflow.mmd",
            "agent_workflow.mmd",
            "risk_review.md",
            "test_report.md",
        }
        actual = {path.name for path in agent.run_dir.iterdir()}
        self.assertTrue(expected.issubset(actual))
        metrics = json.loads((agent.run_dir / "metrics.json").read_text(encoding="utf-8"))
        self.assertIn("total_tokens", metrics["totals"])
        self.assertEqual(metrics["totals"]["total_tokens"], 0)
        forbidden_source = "estimated" + "_local"
        self.assertFalse(any(item.get("token_source") == forbidden_source for item in metrics["node_metrics"]))

    def test_lock_index_changes_downstream_artifacts(self):
        runs = []
        for index in range(3):
            _, state = self.run_agent(lock_index=index)
            runs.append(
                {
                    "selected_name": state["selected_workflow"]["name"],
                    "product_name": state["product_solution"]["product_name"],
                    "agent_mmd": state["agent_workflow_mmd"],
                    "risk_review": json.dumps(state["risk_review"], ensure_ascii=False),
                }
            )
        self.assertEqual(len({item["selected_name"] for item in runs}), 3)
        self.assertEqual(len({item["product_name"] for item in runs}), 3)
        self.assertEqual(len({item["agent_mmd"] for item in runs}), 3)
        self.assertEqual(len({item["risk_review"] for item in runs}), 3)

    def test_no_visual_template_leak_for_non_visual_profiles(self):
        forbidden = ["菜品照片", "门头照片", "海报", "菜单图", "视觉编排", "LocalBiz Visual Agent"]
        for workflow_type in {"complaint_refund", "inventory_procurement"}:
            _, state = self.run_agent(lock_index=self._candidate_index(workflow_type))
            downstream = "\n".join(
                [
                    state["agent_workflow_mmd"],
                    json.dumps(state["product_solution"], ensure_ascii=False),
                    json.dumps(state["risk_review"], ensure_ascii=False),
                ]
            )
            self.assertTrue(state["causal_audit"]["no_static_visual_template_leak"])
            self.assertFalse([term for term in forbidden if term in downstream])

    def test_evidence_binding_is_present(self):
        _, state = self.run_agent(lock_index=1)
        selected_id = state["selected_workflow"]["id"]
        for painpoint in state["painpoints"]:
            self.assertEqual(painpoint["selected_workflow_id"], selected_id)
            self.assertTrue(painpoint["source_evidence_ids"])
            self.assertTrue(painpoint["derived_from_steps"])
        for intervention in state["agent_interventions"]:
            self.assertEqual(intervention["selected_workflow_id"], selected_id)
            self.assertTrue(intervention["source_evidence_ids"])
            self.assertTrue(intervention["derived_from_steps"])
        self.assertEqual(state["product_solution"]["selected_workflow_id"], selected_id)
        self.assertTrue(state["causal_audit"]["passed"])

    def test_discovery_mode_does_not_generate_final_solution(self):
        _, state = self.run_agent(mode="discovery")
        self.assertIsNone(state.get("selected_workflow"))
        self.assertNotIn("product_solution", state)
        self.assertNotIn("agent_intervention_workflow", state)

    def test_langgraph_runner_executes_same_node_chain(self):
        try:
            import langgraph  # noqa: F401
        except ImportError:
            self.skipTest("langgraph optional dependency is not installed")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        agent = RestaurantWorkflowDiscoveryAgent(
            config={"llm_enabled": False},
            evidence_pool=list(FALLBACK_EVIDENCE_POOL),
            out_root=Path(tmp.name),
            request_id="test-langgraph",
        )
        state = run_with_langgraph(agent, mode="all", lock_index=0)
        self.assertEqual(state["execution_engine"], "langgraph_stategraph")
        self.assertEqual(state["selected_workflow"]["human_lock"]["selected_index"], 0)
        self.assertTrue(state["causal_audit"]["passed"])
        health = json.loads((agent.run_dir / "health.json").read_text(encoding="utf-8"))
        self.assertEqual(health["execution_engine"], "langgraph_stategraph")
        self.assertEqual(health["graph_runtime"]["runner"], "langgraph")

    def test_real_stepfun_llm_usage_when_enabled(self):
        if os.getenv("RUN_REAL_LLM_TEST") != "true":
            self.skipTest("set RUN_REAL_LLM_TEST=true to spend real StepFun tokens")

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        agent = RestaurantWorkflowDiscoveryAgent(
            config={"llm_enabled": True},
            evidence_pool=list(FALLBACK_EVIDENCE_POOL),
            out_root=Path(tmp.name),
            request_id="test-real-llm",
        )
        if not agent.llm.configured:
            self.skipTest("StepFun env is not configured")
        state = agent.run(mode="all", lock_index=0)
        metrics = agent.metrics.to_dict()
        self.assertGreater(metrics["totals"]["llm_calls"], 0)
        self.assertGreater(metrics["totals"]["provider_usage_nodes"], 0)
        usage = json.loads((agent.run_dir / "llm_usage.json").read_text(encoding="utf-8"))
        self.assertTrue(any(item.get("token_source") == "provider_usage" for item in usage))
        self.assertTrue(state["product_solution"]["product_name"])

    def _candidate_index(self, workflow_type):
        _, state = self.run_agent(mode="discovery")
        for index, candidate in enumerate(state["candidate_workflows"]):
            if candidate.get("workflow_type") == workflow_type:
                return index
        self.fail(f"No candidate with workflow_type={workflow_type}")


if __name__ == "__main__":
    unittest.main()
