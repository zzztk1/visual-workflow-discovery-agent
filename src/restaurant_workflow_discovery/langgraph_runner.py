from __future__ import annotations

from typing import Any, Callable, Dict, Optional

from .agent import RestaurantWorkflowDiscoveryAgent


DISCOVERY_NODE_NAMES = [
    "brief_intake",
    "query_planner",
    "search_executor",
    "evidence_extractor",
    "candidate_builder",
    "candidate_scorer",
]

DEEP_DIVE_NODE_NAMES = [
    "human_lock",
    "workflow_decomposer",
    "painpoint_analyzer",
    "intervention_designer",
    "product_solution_generator",
    "risk_reviewer",
    "causal_auditor",
    "export_report",
]


def run_with_langgraph(
    agent: RestaurantWorkflowDiscoveryAgent,
    *,
    mode: str = "all",
    lock_index: Optional[int] = None,
) -> Dict[str, Any]:
    """Run the existing agent nodes through LangGraph StateGraph.

    The business nodes stay owned by RestaurantWorkflowDiscoveryAgent. This
    runner only swaps the orchestration layer, which keeps the demo stable while
    making the architecture directly portable to production LangGraph usage.
    """

    try:
        from langgraph.graph import END, StateGraph
    except ImportError as exc:  # pragma: no cover - exercised only without optional dep
        raise RuntimeError(
            "LangGraph runner requested, but langgraph is not installed. "
            "Install requirements-langgraph.txt or use --runner state-machine."
        ) from exc

    if mode not in {"all", "discovery"}:
        raise ValueError("mode must be 'all' or 'discovery'")

    agent.run_dir.mkdir(parents=True, exist_ok=True)
    agent.state["execution_engine"] = "langgraph_stategraph"
    agent.state["graph_runtime"] = {
        "runner": "langgraph",
        "mode": mode,
        "human_lock_enabled": mode != "discovery",
        "planned_node_count": len(DISCOVERY_NODE_NAMES) + (1 if mode == "discovery" else len(DEEP_DIVE_NODE_NAMES)),
    }

    graph = StateGraph(dict)
    node_names = list(DISCOVERY_NODE_NAMES)
    if mode == "discovery":
        node_names.append("export_report")
    else:
        node_names.extend(DEEP_DIVE_NODE_NAMES)

    for name in node_names:
        graph.add_node(name, _make_node(agent, name, lock_index=lock_index))

    graph.set_entry_point(node_names[0])
    for previous, current in zip(node_names, node_names[1:]):
        graph.add_edge(previous, current)
    graph.add_edge(node_names[-1], END)

    compiled = graph.compile()
    final_state = compiled.invoke(dict(agent.state))
    agent.state.update(final_state)
    return agent.state


def _make_node(
    agent: RestaurantWorkflowDiscoveryAgent,
    name: str,
    *,
    lock_index: Optional[int],
) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    def node(state: Dict[str, Any]) -> Dict[str, Any]:
        agent.state.update(state)
        if name == "human_lock":
            agent._run_node(name, lambda: agent.human_lock(lock_index=lock_index))
        else:
            agent._run_node(name, getattr(agent, name))
        return dict(agent.state)

    return node
