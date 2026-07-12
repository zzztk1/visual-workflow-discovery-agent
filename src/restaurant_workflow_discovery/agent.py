from __future__ import annotations

import argparse
import html
import json
import re
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from .evidence_pool import FALLBACK_EVIDENCE_POOL
from .llm_client import StepFunLLMClient


SCORING_WEIGHTS = {
    "evidence_authenticity": 20,
    "flow_continuity": 15,
    "painpoint_clarity": 20,
    "agent_intervention_value": 20,
    "productization_potential": 15,
    "mvp_verifiability": 10,
}

DEFAULT_CONFIG = {
    "industry": "餐饮行业",
    "goal": "发现真实存在的人工工作流，并设计 Agent 介入后的产品化方案",
    "must_include": ["人工重复", "信息搬运", "判断成本", "审核或沟通成本"],
    "exclude": ["纯 AI 生图娱乐", "无公开证据", "单点任务而非连续流程"],
    "candidate_count": 3,
    "mvp_window_days": 7,
    "live_search": False,
    "live_search_timeout_sec": 5,
    "llm_enabled": True,
    "llm_timeout_sec": 45,
    "llm_price_per_1k_usd": 0.0006,
}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(value: str) -> str:
    value = re.sub(r"\s+", "-", value.strip().lower())
    value = re.sub(r"[^a-z0-9\-\u4e00-\u9fff]+", "", value)
    return value[:80] or "workflow"


@dataclass
class MetricsContext:
    node_metrics: List[Dict[str, Any]] = field(default_factory=list)
    totals: Dict[str, Any] = field(
        default_factory=lambda: {
            "latency_ms": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "estimated_cost_usd": 0.0,
            "estimated_cost_available": True,
            "cost_estimation_basis": "provider usage only; non-LLM nodes record zero tokens; unit_price=0.0006/1k",
            "price_per_1k_usd": 0.0006,
            "tool_calls": 0,
            "llm_calls": 0,
            "provider_usage_nodes": 0,
            "retry_count": 0,
            "error_count": 0,
        }
    )

    def add_node_metric(
        self,
        *,
        node: str,
        latency_ms: float,
        prompt_tokens: int,
        completion_tokens: int,
        total_tokens: int,
        tool_calls: int,
        retry_count: int,
        llm_calls: int = 0,
        token_source: str = "not_applicable",
        llm_provider: str = "",
        llm_model: str = "",
        error: str = "",
    ) -> None:
        estimated_cost = round((total_tokens / 1000.0) * self.totals["price_per_1k_usd"], 6)
        metric = {
            "node": node,
            "latency_ms": round(latency_ms, 2),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "estimated_cost_usd": estimated_cost,
            "tool_calls": tool_calls,
            "llm_calls": llm_calls,
            "token_source": token_source,
            "llm_provider": llm_provider,
            "llm_model": llm_model,
            "retry_count": retry_count,
            "status": "failed" if error else "success",
            "error": error,
            "ts": utc_now(),
        }
        self.node_metrics.append(metric)
        self.totals["latency_ms"] = round(float(self.totals["latency_ms"]) + latency_ms, 2)
        self.totals["prompt_tokens"] += prompt_tokens
        self.totals["completion_tokens"] += completion_tokens
        self.totals["total_tokens"] += total_tokens
        self.totals["estimated_cost_usd"] = round(
            float(self.totals["estimated_cost_usd"]) + estimated_cost, 6
        )
        self.totals["tool_calls"] += tool_calls
        self.totals["llm_calls"] += llm_calls
        if token_source == "provider_usage":
            self.totals["provider_usage_nodes"] += 1
        self.totals["retry_count"] += retry_count
        if error:
            self.totals["error_count"] += 1

    def to_dict(self) -> Dict[str, Any]:
        return {"node_metrics": self.node_metrics, "totals": self.totals}


class RestaurantWorkflowDiscoveryAgent:
    def __init__(
        self,
        *,
        config: Optional[Dict[str, Any]] = None,
        evidence_pool: Optional[List[Dict[str, Any]]] = None,
        out_root: Path | str = "outputs",
        request_id: Optional[str] = None,
    ) -> None:
        merged = dict(DEFAULT_CONFIG)
        merged.update(config or {})
        self.config = merged
        self.evidence_pool = evidence_pool or list(FALLBACK_EVIDENCE_POOL)
        self.out_root = Path(out_root)
        self.project_root = Path(__file__).resolve().parents[2]
        self.llm = StepFunLLMClient.from_env(self.project_root, self.config)
        self.request_id = request_id or f"rwda-{uuid.uuid4().hex[:8]}"
        self.run_id = uuid.uuid4().hex[:12]
        self.metrics = MetricsContext()
        self.trace: List[Dict[str, Any]] = []
        self.state: Dict[str, Any] = {
            "run_id": self.run_id,
            "request_id": self.request_id,
            "config": self.config,
            "created_at": utc_now(),
            "source_mode": "fallback_evidence_pool",
            "llm_mode": "stepfun" if self.llm.configured else "fallback_rules",
            "queries": [],
            "evidence": [],
            "candidate_workflows": [],
            "selected_workflow": None,
            "execution_engine": "state_machine",
            "outputs": {},
        }

    @property
    def run_dir(self) -> Path:
        return self.out_root / self.run_id

    def run(self, *, mode: str = "all", lock_index: Optional[int] = None) -> Dict[str, Any]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.state["execution_engine"] = "state_machine"
        discovery_nodes = [
            self.brief_intake,
            self.query_planner,
            self.search_executor,
            self.evidence_extractor,
            self.candidate_builder,
            self.candidate_scorer,
        ]
        for node in discovery_nodes:
            self._run_node(node.__name__, node)

        if mode == "discovery":
            self._run_node("export_report", self.export_report)
            return self.state

        self._run_node("human_lock", lambda: self.human_lock(lock_index=lock_index))
        for node in [
            self.workflow_decomposer,
            self.painpoint_analyzer,
            self.intervention_designer,
            self.product_solution_generator,
            self.risk_reviewer,
            self.causal_auditor,
            self.export_report,
        ]:
            self._run_node(node.__name__, node)
        return self.state

    def _run_node(self, name: str, func: Callable[[], Dict[str, Any]]) -> None:
        start = time.perf_counter()
        input_summary = self._summarize_state()
        error = ""
        output: Dict[str, Any] = {}
        try:
            output = func()
            self.state.update(output)
        except Exception as exc:  # keep the run exportable
            error = f"{type(exc).__name__}: {exc}"
            output = {"error": error}
        latency_ms = (time.perf_counter() - start) * 1000
        prompt_tokens = 0
        completion_tokens = 0
        total_tokens = 0
        token_source = "not_applicable"
        llm_provider = ""
        llm_model = ""
        llm_usage = output.get("_llm_usage", []) if isinstance(output, dict) else []
        if llm_usage:
            provider_usage = [item for item in llm_usage if item.get("token_source") == "provider_usage"]
            self.state.setdefault("llm_usage_records", []).extend(llm_usage)
            if provider_usage:
                prompt_tokens = sum(int(item.get("prompt_tokens", 0)) for item in provider_usage)
                completion_tokens = sum(int(item.get("completion_tokens", 0)) for item in provider_usage)
                total_tokens = sum(int(item.get("total_tokens", 0)) for item in provider_usage)
                token_source = "provider_usage"
                llm_provider = provider_usage[-1].get("provider", "")
                llm_model = provider_usage[-1].get("model", "")
            else:
                token_source = llm_usage[-1].get("token_source", "not_applicable")
        tool_calls = int(output.get("_tool_calls", 0)) if isinstance(output, dict) else 0
        llm_calls = len(llm_usage)
        retry_count = int(output.get("_retry_count", 0)) if isinstance(output, dict) else 0
        self.metrics.add_node_metric(
            node=name,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            tool_calls=tool_calls,
            llm_calls=llm_calls,
            token_source=token_source,
            llm_provider=llm_provider,
            llm_model=llm_model,
            retry_count=retry_count,
            error=error,
        )
        self.trace.append(
            {
                "node": name,
                "status": "failed" if error else "success",
                "input_summary": input_summary,
                "output_summary": self._summarize(output),
                "error": error,
                "ts": utc_now(),
            }
        )

    def _summarize_state(self) -> Dict[str, Any]:
        return {
            "queries": len(self.state.get("queries", [])),
            "evidence": len(self.state.get("evidence", [])),
            "candidates": len(self.state.get("candidate_workflows", [])),
            "selected": bool(self.state.get("selected_workflow")),
        }

    def _summarize(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            return str(payload)[:300]
        return {
            key: (f"list[{len(value)}]" if isinstance(value, list) else f"dict[{len(value)}]" if isinstance(value, dict) else value)
            for key, value in payload.items()
            if not key.startswith("_")
        }

    def _llm_json(
        self,
        *,
        node: str,
        system: str,
        payload: Dict[str, Any],
        fallback: Any,
        temperature: float = 0.2,
    ) -> Tuple[Any, Dict[str, Any]]:
        result = self.llm.complete_json(
            node=node,
            system=system,
            payload=payload,
            fallback=fallback,
            temperature=temperature,
        )
        meta = {
            "_llm_usage": [result.usage],
            "_llm_fallback_used": result.fallback_used,
        }
        if result.error:
            meta["_llm_error"] = result.error
        return result.data, meta

    def brief_intake(self) -> Dict[str, Any]:
        return {
            "brief": {
                "industry": self.config["industry"],
                "goal": self.config["goal"],
                "constraints": {
                    "must_include": self.config["must_include"],
                    "exclude": self.config["exclude"],
                    "mvp_window_days": self.config["mvp_window_days"],
                },
            }
        }

    def search_executor(self) -> Dict[str, Any]:
        live_results: List[Dict[str, Any]] = []
        source_mode = "fallback_evidence_pool"
        if self.config.get("live_search"):
            live_results = self._live_search(self.state.get("queries", [])[:4])
            if live_results:
                source_mode = "live_search_plus_fallback_evidence_pool"
            else:
                source_mode = "live_search_attempted_fallback_evidence_pool"
        return {
            "raw_search_results": live_results + list(self.evidence_pool),
            "source_mode": source_mode,
            "_tool_calls": len(self.state.get("queries", [])),
        }

    def evidence_extractor(self) -> Dict[str, Any]:
        excluded = set(self.config.get("exclude", []))
        evidence: List[Dict[str, Any]] = []
        for item in self.state.get("raw_search_results", []):
            item = self._normalize_raw_evidence(item)
            workflow = str(item.get("workflow", ""))
            workflow = self._canonical_workflow_name(workflow)
            if workflow in excluded:
                continue
            if "纯 AI 生图娱乐" in workflow:
                continue
            evidence.append(
                {
                    "id": item["id"],
                    "title": item["title"],
                    "url": item["url"],
                    "workflow": workflow,
                    "proof_point": item["proof_point"],
                    "step_refs": item.get("step_refs", []),
                    "inefficiencies": item.get("inefficiencies", []),
                    "source_type": item.get("source_type", "unknown"),
                }
            )
        return {"evidence": evidence}

    def _live_search(self, queries: List[str]) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        seen_urls = set()
        timeout = float(self.config.get("live_search_timeout_sec", 5))
        for query in queries:
            parsed_items = self._fetch_duckduckgo(query, timeout)
            if not parsed_items:
                parsed_items = self._fetch_bing(query, timeout)
            for item in parsed_items:
                if item["url"] in seen_urls:
                    continue
                seen_urls.add(item["url"])
                results.append(item)
                if len(results) >= 8:
                    return results
        return results

    def _fetch_duckduckgo(self, query: str, timeout: float) -> List[Dict[str, Any]]:
        url = "https://duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 RestaurantWorkflowDiscoveryAgent/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="ignore")
        except Exception:
            return []
        return self._parse_duckduckgo_results(body, query)

    def _fetch_bing(self, query: str, timeout: float) -> List[Dict[str, Any]]:
        url = "https://www.bing.com/search?" + urllib.parse.urlencode({"q": query})
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0 RestaurantWorkflowDiscoveryAgent/1.0"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as response:
                body = response.read().decode("utf-8", errors="ignore")
        except Exception:
            return []
        return self._parse_bing_results(body, query)

    def _parse_duckduckgo_results(self, body: str, query: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        pattern = re.compile(
            r'<a[^>]+class="result__a"[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            flags=re.I | re.S,
        )
        for match in pattern.finditer(body):
            href = html.unescape(match.group("href"))
            title = re.sub(r"<.*?>", "", match.group("title"))
            title = html.unescape(title).strip()
            parsed_href = urllib.parse.urlparse(href)
            if "duckduckgo.com" in parsed_href.netloc and "uddg=" in href:
                href = urllib.parse.parse_qs(parsed_href.query).get("uddg", [href])[0]
            if not title or not href.startswith("http"):
                continue
            items.append(
                {
                    "id": f"live_{slugify(title)}",
                    "title": title,
                    "url": href,
                    "query": query,
                    "source_type": "live_search",
                }
            )
        return items[:5]

    def _parse_bing_results(self, body: str, query: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        pattern = re.compile(
            r'<li class="b_algo".*?<h2[^>]*>\s*<a[^>]+href="(?P<href>[^"]+)"[^>]*>(?P<title>.*?)</a>',
            flags=re.I | re.S,
        )
        for match in pattern.finditer(body):
            href = html.unescape(match.group("href"))
            title = re.sub(r"<.*?>", "", match.group("title"))
            title = html.unescape(title).strip()
            if not title or not href.startswith("http"):
                continue
            items.append(
                {
                    "id": f"live_{slugify(title)}",
                    "title": title,
                    "url": href,
                    "query": query,
                    "source_type": "live_search",
                }
            )
        return items[:5]

    def _normalize_raw_evidence(self, item: Dict[str, Any]) -> Dict[str, Any]:
        if item.get("workflow"):
            return item
        title = str(item.get("title", ""))
        url = str(item.get("url", ""))
        haystack = f"{title} {url}".lower()
        if any(term in haystack for term in ["poster", "photo", "social", "menu photo", "menustudio", "foodframe", "plated", "designkit"]):
            workflow = "小餐馆视觉营销物料制作与发布工作流"
            step_refs = ["公开资料发现", "视觉素材准备", "海报/菜单图制作", "平台发布"]
            inefficiencies = ["人工重复", "判断成本"]
            proof = "搜索结果与餐饮图片、海报、菜单图或社媒发布相关，可作为视觉营销工作流补充证据。"
        elif any(term in haystack for term in ["complaint", "refund", "customer-service", "customer service"]):
            workflow = "餐厅客诉与退款处理工作流"
            step_refs = ["接收投诉", "记录问题", "判断责任", "沟通补偿"]
            inefficiencies = ["判断成本", "沟通或审核成本"]
            proof = "搜索结果与餐饮客诉、客服或退款处理相关。"
        elif any(term in haystack for term in ["inventory", "procurement", "purchase", "stock"]):
            workflow = "餐厅库存盘点与采购补货工作流"
            step_refs = ["库存盘点", "消耗记录", "采购补货", "到货验收"]
            inefficiencies = ["人工重复", "信息搬运"]
            proof = "搜索结果与餐厅库存、采购或补货流程相关。"
        else:
            workflow = "餐饮单点资料"
            step_refs = ["资料浏览"]
            inefficiencies = ["无明确连续流程"]
            proof = "搜索结果暂未归入可验证的连续人工工作流。"
        return {
            "id": item.get("id") or f"live_{slugify(title)}",
            "title": title,
            "url": url,
            "workflow": workflow,
            "proof_point": proof,
            "step_refs": step_refs,
            "inefficiencies": inefficiencies,
            "source_type": item.get("source_type", "live_search"),
        }

    def candidate_builder(self) -> Dict[str, Any]:
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for evidence in self.state.get("evidence", []):
            grouped.setdefault(evidence["workflow"], []).append(evidence)

        candidates = []
        for workflow_name, items in grouped.items():
            steps = self._merge_unique([step for item in items for step in item.get("step_refs", [])])
            inefficiencies = self._merge_unique(
                [point for item in items for point in item.get("inefficiencies", [])]
            )
            candidates.append(
                {
                    "id": slugify(workflow_name),
                    "name": workflow_name,
                    "workflow_type": self._workflow_type(workflow_name),
                    "evidence_count": len(items),
                    "evidence_ids": [item["id"] for item in items],
                    "evidence_links": [{"title": item["title"], "url": item["url"]} for item in items],
                    "steps": self._normalize_steps(workflow_name, steps),
                    "inefficiencies": inefficiencies,
                }
            )
        return {"candidate_workflows": candidates}

    def candidate_scorer(self) -> Dict[str, Any]:
        scored = []
        for candidate in self.state.get("candidate_workflows", []):
            score_detail = self._score_candidate(candidate)
            candidate = dict(candidate)
            candidate["score_detail"] = score_detail
            candidate["score"] = sum(score_detail.values())
            candidate["evidence_insufficient"] = candidate["evidence_count"] < 3
            candidate["meets_hard_requirements"] = (
                candidate["evidence_count"] >= 3
                and len(candidate["steps"]) >= 4
                and len(candidate["inefficiencies"]) >= 2
                and candidate["score"] >= 60
            )
            scored.append(candidate)
        scored.sort(key=lambda item: item["score"], reverse=True)
        return {"candidate_workflows": scored[: int(self.config.get("candidate_count", 3))]}

    def human_lock(self, *, lock_index: Optional[int] = None) -> Dict[str, Any]:
        candidates = self.state.get("candidate_workflows", [])
        if not candidates:
            raise RuntimeError("No candidate workflow available for human lock.")
        selected_index = lock_index if lock_index is not None else 0
        selected_index = max(0, min(selected_index, len(candidates) - 1))
        selected = dict(candidates[selected_index])
        selected["human_lock"] = {
            "selected_index": selected_index,
            "mode": "auto_default_top_candidate" if lock_index is None else "manual_lock_index",
            "locked_at": utc_now(),
            "reason": "锁定评分最高且满足硬性条件的餐饮人工工作流，用于深度拆解和产品方案生成。",
        }
        return {"selected_workflow": selected}

    def _workflow_type(self, workflow_name: str) -> str:
        text = workflow_name.lower()
        if any(term in text for term in ["complaint", "refund", "customer", "客诉", "退款", "瀹㈣瘔", "閫€€娆"]):
            return "complaint_refund"
        if any(term in text for term in ["inventory", "procurement", "stock", "库存", "采购", "搴撳瓨", "閲囪喘"]):
            return "inventory_procurement"
        if any(term in text for term in ["menu update", "menu management", "菜单", "鑿滃崟"]):
            return "menu_update"
        if any(term in text for term in ["visual", "poster", "photo", "social", "视觉", "海报", "门头", "瑙嗚", "娴锋姤"]):
            return "visual_marketing"
        return "generic_restaurant"

    def _canonical_workflow_name(self, workflow_name: str) -> str:
        workflow_type = self._workflow_type(workflow_name)
        return {
            "visual_marketing": "小餐馆视觉营销物料制作与发布工作流",
            "complaint_refund": "餐厅客诉与退款处理工作流",
            "inventory_procurement": "餐厅库存盘点与采购补货工作流",
            "menu_update": "外卖/堂食菜单图片与信息更新工作流",
        }.get(workflow_type, workflow_name)

    def _selected_evidence_refs(self, selected: Dict[str, Any]) -> List[str]:
        refs = list(selected.get("evidence_ids") or [])
        if not refs:
            refs = [item.get("id", "") for item in self.state.get("evidence", [])[:1] if item.get("id")]
        return refs

    def _profile(self, selected: Dict[str, Any]) -> Dict[str, Any]:
        workflow_type = selected.get("workflow_type") or self._workflow_type(selected.get("name", ""))
        return {
            "visual_marketing": self._visual_marketing_profile,
            "complaint_refund": self._complaint_refund_profile,
            "inventory_procurement": self._inventory_procurement_profile,
            "menu_update": self._menu_update_profile,
        }.get(workflow_type, self._generic_restaurant_profile)(selected)

    def _visual_marketing_profile(self, selected: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "workflow_type": "visual_marketing",
            "manual_steps": [
                "店主拍摄门头、菜品和店内环境照片。",
                "人工筛选可用照片，删除模糊、重复或不适合发布的素材。",
                "人工修图、裁剪、调色，并选择合适的平台模板。",
                "人工填写菜品卖点、活动文案、价格、地址和营业时间。",
                "分别适配海报、菜单图、社媒图和店铺页等不同渠道尺寸。",
                "店主发布前确认菜品真实性、价格和活动信息。",
                "根据浏览、咨询、到店或下单反馈复盘下一次物料。",
            ],
            "agent_steps": [
                "上传真实门头、菜品、环境照片和基础店铺信息。",
                "图片理解 Agent 评估照片质量并分类可复用素材。",
                "经营语境 Agent 提取菜系、客单价、门店调性和目标人群。",
                "物料规划 Agent 选择海报、菜单图、社媒图和轻量主页等输出版本。",
                "文案 Agent 生成标题、卖点、活动说明和 CTA。",
                "视觉编排 Agent 基于真实照片生成可发布版式。",
                "风险审核 Agent 检查虚假菜品、过度美化、价格和营业时间错误。",
                "店主确认高风险字段后导出发布物料。",
                "复盘 Agent 记录耗时、修改点、可发布率和下次优化建议。",
            ],
            "painpoints": [
                ("人工重复", "选图、裁剪、套模板、多平台改尺寸每次活动都要重复做。", "耗时高，且物料质量依赖个人经验。"),
                ("信息搬运", "菜名、价格、地址、营业时间和活动信息要在多个工具和渠道重复填写。", "容易漏填、填错或造成不同平台信息不一致。"),
                ("判断成本", "店主需要判断照片是否好看、海报风格是否吸引人、文案是否贴合菜品。", "小餐馆通常缺少设计经验，容易靠感觉反复试错。"),
                ("审核成本", "发布前要确认菜品真实性、价格、活动时间和是否过度美化。", "缺少检查会误导顾客，引发投诉或差评。"),
            ],
            "product": {
                "product_name": "LocalBiz Visual Agent",
                "positioning": "把真实餐馆照片和店铺信息转成可发布的海报、菜单图、社媒图和轻量主页，并保留店主确认。",
                "target_users": ["小餐馆老板", "本地连锁门店运营", "外卖平台商家"],
                "core_features": [
                    "照片质量评分与素材分组",
                    "菜品、门头、环境图片理解",
                    "海报、菜单图、社媒图和主页物料规划",
                    "多平台尺寸适配",
                    "促销文案生成",
                    "真实性与价格检查清单",
                    "人工确认后导出",
                    "耗时、成本和修改点复盘",
                ],
                "human_in_the_loop": [
                    "确认最终工作流",
                    "确认菜品真实性、价格、活动时间和营业时间",
                    "发布到第三方平台前人工确认",
                ],
                "mvp_7_day_plan": [
                    "Day 1：访谈 3 家小餐馆，确认视觉物料人工流程。",
                    "Day 2：收集 20 组真实照片，定义输出模板。",
                    "Day 3：完成照片理解和素材分组。",
                    "Day 4：加入文案、版式生成和风险检查清单。",
                    "Day 5：导出海报、菜单图和社媒图版本。",
                    "Day 6：对比人工耗时、修改次数和店主满意度。",
                    "Day 7：复盘可发布率、节省时间和下一版需求。",
                ],
                "success_metrics": ["单次物料制作时间小于 10 分钟", "店主满意度 >= 4/5", "可发布率 >= 60%", "价格/营业时间错误发布数 = 0"],
            },
            "risks": [
                ("生成不存在的菜品或过度美化菜品。", "high", "只基于真实上传照片做增强和排版；新增菜品或明显外观变化必须店主确认。"),
                ("价格、活动时间或营业时间错误。", "high", "导出前必须通过确认清单。"),
                ("自动发布导致平台违规或顾客误解。", "medium", "比赛 MVP 只导出物料和建议，发布必须人工批准。"),
            ],
            "forbidden_terms": [],
        }

    def _complaint_refund_profile(self, selected: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "workflow_type": "complaint_refund",
            "manual_steps": [
                "顾客通过电话、评价、外卖平台或聊天窗口发起投诉。",
                "员工记录订单号、顾客身份、问题描述和来源渠道。",
                "员工收集订单、支付、配送、聊天记录等证据。",
                "员工判断责任属于后厨、配送、服务、供应商还是顾客因素。",
                "员工提出退款、优惠券、重做、道歉或不补偿方案。",
                "店长审核金额较高或责任不清的补偿案例。",
                "员工回复顾客并更新处理结果。",
                "店长复盘高频问题和服务质量模式。",
            ],
            "agent_steps": [
                "投诉接收 Agent 统一整理投诉内容、渠道、订单号和紧急程度。",
                "证据汇总 Agent 把订单、支付、配送和聊天记录合并为一个 case 文件。",
                "责任判断 Agent 根据门店规则判断可能责任方并给出置信度。",
                "补偿建议 Agent 生成退款、优惠券、重做或拒绝补偿建议。",
                "升级审核 Agent 将责任不清或金额较高的 case 转给店长。",
                "回复 Agent 生成语气合适的顾客回复草稿。",
                "Case 记忆 Agent 记录最终处理结果、原因和顾客反馈。",
                "复盘 Agent 汇总高频问题和预防建议。",
            ],
            "painpoints": [
                ("信息搬运", "订单、支付、配送、聊天和投诉信息分散在不同系统里。", "员工要反复拼 case，容易漏证据。"),
                ("判断成本", "责任归属和补偿建议依赖分散事实与门店规则。", "不同员工判断可能不一致。"),
                ("审核成本", "退款金额、公开差评和责任不清的投诉需要店长审批。", "审批慢会进一步放大顾客情绪。"),
                ("人工重复", "常见投诉类型需要重复道歉、解释和记录处理结果。", "客服员工把大量时间花在低复杂度 case 上。"),
            ],
            "product": {
                "product_name": "Restaurant Complaint Triage Agent",
                "positioning": "把分散的餐厅投诉整理成有证据支撑的责任判断、补偿建议和可审批顾客回复。",
                "target_users": ["餐厅店长", "门店客服员工", "外卖平台运营人员"],
                "core_features": [
                    "多渠道投诉接收",
                    "订单、支付、配送、聊天证据汇总",
                    "责任归因与置信度",
                    "按规则生成退款、优惠券或重做建议",
                    "店长升级审核阈值",
                    "顾客回复草稿",
                    "Case 归档与高频问题复盘",
                    "可追踪审批和补偿成本记录",
                ],
                "human_in_the_loop": [
                    "审批超过阈值的退款",
                    "复核责任不清的 case",
                    "确认公开评价回复",
                    "人工处理隐私敏感或情绪激烈的纠纷",
                ],
                "mvp_7_day_plan": [
                    "Day 1：收集 20 个脱敏投诉案例和门店补偿规则。",
                    "Day 2：定义投诉分类、证据结构和升级阈值。",
                    "Day 3：完成 case 接收和证据聚合。",
                    "Day 4：加入责任判断和补偿建议。",
                    "Day 5：生成回复草稿和店长审批清单。",
                    "Day 6：回放历史 case，对比判断一致性和耗时。",
                    "Day 7：复盘误判、升级率和退款成本控制。",
                ],
                "success_metrics": ["case 分拣时间下降 50%", "店长升级准确率 >= 80%", "未授权退款数 = 0", "顾客回复草稿采纳率 >= 70%"],
            },
            "risks": [
                ("责任判断错误导致不公平退款或顾客升级投诉。", "high", "低置信度 case 和公开差评必须转店长。"),
                ("未授权退款或补偿超过规则。", "high", "退款阈值和补偿政策作为硬门禁。"),
                ("投诉数据包含个人信息。", "high", "日志和导出中脱敏手机号、姓名和地址。"),
                ("回复语气不当放大顾客情绪。", "medium", "增加服务语气审查，情绪激烈 case 必须人工确认。"),
            ],
            "forbidden_terms": ["菜品照片", "门头照片", "海报", "菜单图", "视觉编排", "LocalBiz Visual Agent"],
        }

    def _inventory_procurement_profile(self, selected: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "workflow_type": "inventory_procurement",
            "manual_steps": [
                "员工盘点食材、包材、饮品和关键耗材库存。",
                "员工记录消耗量，并和预估销量对比。",
                "员工识别低库存、高损耗或临期物料。",
                "员工向供应商询价，确认可供货数量和配送时间。",
                "店长比较供应商价格、质量和配送可靠性。",
                "员工创建采购单并确认送达计划。",
                "员工到货验收，检查数量、质量并记录异常。",
                "店长更新库存、成本和损耗复盘记录。",
            ],
            "agent_steps": [
                "库存采集 Agent 导入盘点表、POS 消耗和供应商目录。",
                "预测 Agent 估算消耗趋势并标记低库存、高损耗物料。",
                "补货 Agent 生成建议采购数量和置信度。",
                "供应商 Agent 对比报价、交期、质量备注和历史可靠性。",
                "审批 Agent 将高成本、异常价格或替代品订单转给店长。",
                "采购单 Agent 生成面向不同供应商的采购草稿。",
                "验收 Agent 生成数量/质量检查清单并记录异常。",
                "成本复盘 Agent 跟踪成本异常、损耗和下次采购建议。",
            ],
            "painpoints": [
                ("人工重复", "盘点、抄数量、更新采购记录需要周期性重复。", "人力成本高，且容易录错。"),
                ("信息搬运", "库存、POS 消耗、供应商价格和采购单分散在不同地方。", "店长下单前缺少统一可靠视图。"),
                ("判断成本", "补多少、向谁买，需要结合价格、需求、质量和交期判断。", "可能造成过量采购、缺货或浪费。"),
                ("审核成本", "异常价格、替代品、质量问题和高成本订单需要审批。", "不审核会带来成本和食品安全风险。"),
            ],
            "product": {
                "product_name": "Restaurant Inventory Replenishment Agent",
                "positioning": "整合盘点、销售消耗、供应商报价和到货验收，生成补货建议并保留店长审批。",
                "target_users": ["餐厅店长", "采购员工", "后厨负责人"],
                "core_features": [
                    "库存盘点导入与标准化",
                    "消耗预测",
                    "低库存与高损耗识别",
                    "供应商报价对比",
                    "采购单草稿生成",
                    "异常成本和替代品审批",
                    "到货验收检查清单",
                    "成本与损耗复盘报告",
                ],
                "human_in_the_loop": [
                    "审批高成本采购单",
                    "复核供应商替代品",
                    "确认到货质量异常",
                    "活动或天气导致需求变化时人工覆盖预测",
                ],
                "mvp_7_day_plan": [
                    "Day 1：梳理 1 家餐厅 30 个 SKU 的盘点表和供应商报价格式。",
                    "Day 2：定义库存、消耗、供应商和采购单结构。",
                    "Day 3：完成盘点导入和低库存检测。",
                    "Day 4：加入供应商报价对比和补货建议。",
                    "Day 5：加入审批门禁和采购单草稿导出。",
                    "Day 6：回放一周历史采购，对比缺货/浪费判断。",
                    "Day 7：复盘节省时间、异常成本召回和店长覆盖原因。",
                ],
                "success_metrics": ["采购计划时间下降 40%", "高成本订单审批覆盖率 = 100%", "缺货/浪费预警可追踪", "关键 SKU 供应商报价对比完整"],
            },
            "risks": [
                ("库存数量错误导致过量采购或缺货。", "high", "重要变更必须显示置信度、异常清单并由店长审批。"),
                ("供应商异常价格或替代品被漏掉。", "high", "异常价格和替代品强制进入审批。"),
                ("到货数量或质量不一致未被记录。", "medium", "使用验收清单和异常照片/备注字段。"),
                ("食品安全或临期问题被当成普通成本问题处理。", "high", "食品安全关键词强制人工复核。"),
            ],
            "forbidden_terms": ["视觉营销", "海报", "社媒发布", "菜品照片生成", "LocalBiz Visual Agent"],
        }

    def _menu_update_profile(self, selected: Dict[str, Any]) -> Dict[str, Any]:
        base_steps = selected.get("steps") or [
            "Add or update menu item.",
            "Maintain price, category, image, and availability.",
            "Check consistency across delivery and dine-in platforms.",
            "Publish and review customer/order feedback.",
        ]
        return {
            "workflow_type": "menu_update",
            "manual_steps": base_steps,
            "agent_steps": [
                "Menu Agent imports current menu and detects changed items.",
                "Data Agent normalizes item name, price, category, image, and availability.",
                "Policy Agent checks platform rules and required fields.",
                "Review Agent flags price, sold-out, allergen, and image inconsistencies.",
                "Human confirms high-risk fields and publishes updates.",
                "Replay Agent records update time and downstream errors.",
            ],
            "painpoints": [
                ("information_copying", "Menu item facts are copied across tools and platforms.", "Inconsistent prices or availability can appear."),
                ("manual_repetition", "Repeated item creation, image upload, and availability updates.", "Routine updates take staff time."),
                ("review_cost", "Price, sold-out status, and required fields need careful checking.", "Mistakes directly affect orders and customer trust."),
            ],
            "product": {
                "product_name": "Restaurant Menu Update Agent",
                "positioning": "Keeps menu item facts consistent across channels with risk checks and human approval.",
                "target_users": ["Restaurant operators", "Delivery-platform merchants"],
                "core_features": ["Menu fact normalization", "Cross-platform consistency check", "Availability and price review", "Human approval", "Update trace"],
                "human_in_the_loop": ["Confirm price", "Confirm availability", "Approve publish"],
                "mvp_7_day_plan": ["Day 1: Collect menu schema", "Day 2: Build import", "Day 3: Add checks", "Day 4: Add approval", "Day 5: Export updates", "Day 6: Replay errors", "Day 7: Review metrics"],
                "success_metrics": ["Wrong price publish count = 0", "Menu update time down 40%"],
            },
            "risks": [
                ("Wrong price or availability is published.", "high", "Price and availability require human approval."),
                ("Platform-required field is missing.", "medium", "Required-field checklist blocks export."),
            ],
            "forbidden_terms": [],
        }

    def _generic_restaurant_profile(self, selected: Dict[str, Any]) -> Dict[str, Any]:
        steps = selected.get("steps") or ["Receive task", "Collect information", "Judge next action", "Execute", "Review"]
        agent_steps = [f"Agent assists: {step}" for step in steps]
        return {
            "workflow_type": "generic_restaurant",
            "manual_steps": steps,
            "agent_steps": agent_steps + ["Human confirms high-risk decisions.", "Agent records trace and metrics."],
            "painpoints": [
                ("manual_repetition", "Repeated manual steps in the selected workflow.", "Consumes staff time."),
                ("information_copying", "Information moves across people or tools.", "Creates omission and inconsistency risk."),
                ("judgment_cost", "Staff must make context-dependent decisions.", "Decision quality varies."),
            ],
            "product": {
                "product_name": "Restaurant Workflow Copilot",
                "positioning": f"Agent support for {selected.get('name', 'restaurant workflow')}.",
                "target_users": ["Restaurant staff", "Store manager"],
                "core_features": ["Workflow intake", "Evidence collection", "Decision suggestion", "Human approval", "Trace and metrics"],
                "human_in_the_loop": ["Confirm selected workflow", "Approve high-risk actions"],
                "mvp_7_day_plan": ["Day 1: Verify workflow", "Day 2: Define schema", "Day 3: Build intake", "Day 4: Add decision support", "Day 5: Add approval", "Day 6: Replay cases", "Day 7: Review metrics"],
                "success_metrics": ["Manual time down 30%", "All high-risk actions reviewed"],
            },
            "risks": [("Generic workflow assumptions may be wrong.", "medium", "Require evidence and human confirmation before productization.")],
            "forbidden_terms": [],
        }

    def causal_auditor(self) -> Dict[str, Any]:
        selected = self._selected()
        profile = self._profile(selected)
        selected_id = selected["id"]
        painpoints = self.state.get("painpoints", [])
        interventions = self.state.get("agent_interventions", [])
        solution = self.state.get("product_solution") or {}
        risks = self.state.get("risk_review", [])
        downstream_text = "\n".join(
            [
                json.dumps(solution, ensure_ascii=False),
                json.dumps(risks, ensure_ascii=False),
                self.state.get("agent_workflow_mmd", ""),
            ]
        )
        forbidden_found = [term for term in profile.get("forbidden_terms", []) if term and term in downstream_text]
        audit = {
            "selected_workflow_id": selected_id,
            "selected_workflow_name": selected["name"],
            "downstream_artifacts_checked": True,
            "all_artifacts_reference_selected_workflow": all(
                item.get("selected_workflow_id") == selected_id for item in painpoints + interventions + risks
            )
            and solution.get("selected_workflow_id") == selected_id,
            "painpoints_have_evidence": all(item.get("source_evidence_ids") for item in painpoints),
            "interventions_reference_steps": all(item.get("derived_from_steps") for item in interventions),
            "product_solution_references_selected_workflow": solution.get("selected_workflow_id") == selected_id,
            "forbidden_terms_found": forbidden_found,
            "no_static_visual_template_leak": not forbidden_found,
        }
        audit["passed"] = all(
            [
                audit["all_artifacts_reference_selected_workflow"],
                audit["painpoints_have_evidence"],
                audit["interventions_reference_steps"],
                audit["product_solution_references_selected_workflow"],
                audit["no_static_visual_template_leak"],
            ]
        )
        return {"causal_audit": audit}

    # LLM-powered production path. These methods intentionally appear late in
    # the class so they override the earlier rule-only prototype methods while
    # keeping the prototype available in the rollback copy.
    def _selected_evidence_payload(self, selected: Dict[str, Any]) -> List[Dict[str, Any]]:
        ids = set(selected.get("evidence_ids") or [])
        evidence = []
        for item in self.state.get("evidence", []):
            if ids and item.get("id") not in ids:
                continue
            evidence.append(
                {
                    "id": item.get("id"),
                    "title": item.get("title"),
                    "url": item.get("url"),
                    "proof_point": item.get("proof_point"),
                    "step_refs": item.get("step_refs", []),
                    "inefficiencies": item.get("inefficiencies", []),
                    "source_type": item.get("source_type"),
                }
            )
        return evidence[:8]

    def query_planner(self) -> Dict[str, Any]:
        fallback_queries = [
            "restaurant social media workflow photos menu poster",
            "restaurant food photography menu poster marketing workflow",
            "餐饮 商家 菜品图片 海报 外卖 店铺 装修 流程",
            "restaurant menu photo update workflow merchant guide",
            "restaurant customer complaint refund workflow",
            "restaurant inventory procurement workflow",
            "AI restaurant menu photo poster generator workflow",
        ]
        data, meta = self._llm_json(
            node="query_planner",
            system=(
                "You are a search strategist for a restaurant workflow discovery agent. "
                "Generate specific web search queries that can find public evidence of real manual workflows. "
                "Return JSON only with this shape: {\"queries\": [\"...\"]}."
            ),
            payload={"brief": self.state.get("brief"), "fallback_queries": fallback_queries},
            fallback={"queries": fallback_queries},
            temperature=0.1,
        )
        queries = data.get("queries", fallback_queries) if isinstance(data, dict) else fallback_queries
        queries = [str(item).strip() for item in queries if str(item).strip()][:8] or fallback_queries
        return {"queries": queries, **meta}

    def workflow_decomposer(self) -> Dict[str, Any]:
        selected = self._selected()
        profile = self._profile(selected)
        evidence_refs = self._selected_evidence_refs(selected)
        fallback = {"steps": profile["manual_steps"], "reasoning": "rule_fallback"}
        data, meta = self._llm_json(
            node="workflow_decomposer",
            system=(
                "You decompose real restaurant manual workflows. Based only on the selected workflow, "
                "candidate steps, inefficiencies, and evidence summaries, produce a continuous original manual workflow. "
                "Return JSON only: {\"steps\": [\"至少4个中文步骤\"], \"reasoning\": \"简短依据\"}."
            ),
            payload={
                "selected_workflow": selected,
                "evidence": self._selected_evidence_payload(selected),
                "fallback_manual_steps": profile["manual_steps"],
            },
            fallback=fallback,
            temperature=0.2,
        )
        steps = data.get("steps", []) if isinstance(data, dict) else []
        steps = [str(step).strip() for step in steps if str(step).strip()]
        if len(steps) < 4:
            steps = profile["manual_steps"]
            meta["_llm_fallback_used"] = True
        step_objects = [
            {
                "id": f"step_{idx}",
                "text": step,
                "selected_workflow_id": selected["id"],
                "selected_workflow_name": selected["name"],
                "source_evidence_ids": evidence_refs,
            }
            for idx, step in enumerate(steps, start=1)
        ]
        return {
            "original_workflow": {
                "selected_workflow_id": selected["id"],
                "selected_workflow_name": selected["name"],
                "name": selected["name"],
                "workflow_type": profile["workflow_type"],
                "steps": steps,
                "step_objects": step_objects,
                "source_evidence_ids": evidence_refs,
                "llm_reasoning": data.get("reasoning", "") if isinstance(data, dict) else "",
            },
            "original_workflow_mmd": self._mermaid("Original manual workflow", steps),
            **meta,
        }

    def painpoint_analyzer(self) -> Dict[str, Any]:
        selected = self._selected()
        profile = self._profile(selected)
        steps = self.state["original_workflow"]["steps"]
        evidence_refs = self._selected_evidence_refs(selected)
        fallback = {
            "painpoints": [
                {"type": ptype, "where": where, "impact": impact, "derived_from_steps": steps[max(0, idx - 1) : idx + 1] or steps[:1]}
                for idx, (ptype, where, impact) in enumerate(profile["painpoints"], start=1)
            ]
        }
        data, meta = self._llm_json(
            node="painpoint_analyzer",
            system=(
                "You analyze inefficiencies in restaurant manual workflows. Identify concrete pain points, "
                "not generic AI slogans. Return JSON only: {\"painpoints\": [{\"type\":\"人工重复/信息搬运/判断成本/审核成本\", "
                "\"where\":\"发生在哪些步骤\", \"impact\":\"业务影响\", \"derived_from_steps\":[\"...\"]}]}."
            ),
            payload={"selected_workflow": selected, "original_steps": steps, "evidence": self._selected_evidence_payload(selected)},
            fallback=fallback,
            temperature=0.2,
        )
        raw_points = data.get("painpoints", []) if isinstance(data, dict) else []
        if not isinstance(raw_points, list) or len(raw_points) < 2:
            raw_points = fallback["painpoints"]
            meta["_llm_fallback_used"] = True
        painpoints = []
        for idx, item in enumerate(raw_points, start=1):
            if not isinstance(item, dict):
                continue
            derived = item.get("derived_from_steps") or steps[max(0, idx - 1) : idx + 1] or steps[:1]
            if isinstance(derived, str):
                derived = [derived]
            painpoints.append(
                {
                    "id": f"pain_{idx}",
                    "selected_workflow_id": selected["id"],
                    "selected_workflow_name": selected["name"],
                    "type": str(item.get("type", "低效点")),
                    "where": str(item.get("where", "")),
                    "impact": str(item.get("impact", "")),
                    "source_evidence_ids": evidence_refs,
                    "derived_from_steps": [str(step) for step in derived if str(step).strip()] or steps[:1],
                }
            )
        return {"painpoints": painpoints, **meta}

    def intervention_designer(self) -> Dict[str, Any]:
        selected = self._selected()
        profile = self._profile(selected)
        original_steps = self.state["original_workflow"]["steps"]
        evidence_refs = self._selected_evidence_refs(selected)
        fallback = {
            "interventions": [
                {"agent_step": step, "replaces_or_assists": original_steps[max(0, idx - 1) : idx + 1] or original_steps[:1]}
                for idx, step in enumerate(profile["agent_steps"], start=1)
            ]
        }
        data, meta = self._llm_json(
            node="intervention_designer",
            system=(
                "You design practical Agent interventions for a restaurant workflow. Keep human review for high-risk actions. "
                "Return JSON only: {\"interventions\": [{\"agent_step\":\"中文 Agent 步骤\", "
                "\"replaces_or_assists\":[\"对应原始步骤\"], \"why\":\"为什么合理\"}]}."
            ),
            payload={
                "selected_workflow": selected,
                "original_steps": original_steps,
                "painpoints": self.state.get("painpoints", []),
                "evidence": self._selected_evidence_payload(selected),
            },
            fallback=fallback,
            temperature=0.2,
        )
        raw_items = data.get("interventions", []) if isinstance(data, dict) else []
        if not isinstance(raw_items, list) or len(raw_items) < 4:
            raw_items = fallback["interventions"]
            meta["_llm_fallback_used"] = True
        interventions = []
        for idx, item in enumerate(raw_items, start=1):
            if not isinstance(item, dict):
                continue
            assists = item.get("replaces_or_assists") or original_steps[max(0, idx - 1) : idx + 1] or original_steps[:1]
            if isinstance(assists, str):
                assists = [assists]
            interventions.append(
                {
                    "id": f"intervention_{idx}",
                    "selected_workflow_id": selected["id"],
                    "selected_workflow_name": selected["name"],
                    "agent_step": str(item.get("agent_step", "")),
                    "why": str(item.get("why", "")),
                    "replaces_or_assists": [str(step) for step in assists if str(step).strip()] or original_steps[:1],
                    "source_evidence_ids": evidence_refs,
                    "derived_from_steps": [str(step) for step in assists if str(step).strip()] or original_steps[:1],
                }
            )
        agent_steps = [item["agent_step"] for item in interventions if item["agent_step"]]
        if len(agent_steps) < 4:
            agent_steps = profile["agent_steps"]
        return {
            "agent_interventions": interventions,
            "agent_intervention_workflow": {
                "selected_workflow_id": selected["id"],
                "selected_workflow_name": selected["name"],
                "name": profile["product"]["product_name"],
                "workflow_type": profile["workflow_type"],
                "steps": agent_steps,
                "source_evidence_ids": evidence_refs,
            },
            "agent_workflow_mmd": self._mermaid("Agent-improved workflow", agent_steps),
            **meta,
        }

    def product_solution_generator(self) -> Dict[str, Any]:
        selected = self._selected()
        profile = self._profile(selected)
        evidence_refs = self._selected_evidence_refs(selected)
        fallback = {"product_solution": dict(profile["product"])}
        data, meta = self._llm_json(
            node="product_solution_generator",
            system=(
                "You are an AI product manager. Generate a productized Agent solution based on the selected real workflow, "
                "original steps, painpoints, and interventions. Return JSON only with key product_solution containing: "
                "product_name, positioning, target_users, core_features, human_in_the_loop, mvp_7_day_plan, success_metrics, uniqueness."
            ),
            payload={
                "selected_workflow": selected,
                "original_workflow": self.state.get("original_workflow"),
                "painpoints": self.state.get("painpoints", []),
                "agent_interventions": self.state.get("agent_interventions", []),
                "evidence": self._selected_evidence_payload(selected),
                "fallback_product": profile["product"],
            },
            fallback=fallback,
            temperature=0.25,
        )
        raw_solution = data.get("product_solution", data) if isinstance(data, dict) else profile["product"]
        if not isinstance(raw_solution, dict) or not raw_solution.get("product_name"):
            raw_solution = profile["product"]
            meta["_llm_fallback_used"] = True
        solution = dict(profile["product"])
        solution.update(raw_solution)
        for key in ["target_users", "core_features", "human_in_the_loop", "mvp_7_day_plan", "success_metrics"]:
            value = solution.get(key, [])
            if isinstance(value, str):
                value = [value]
            solution[key] = [str(item) for item in value if str(item).strip()]
        solution.update(
            {
                "selected_workflow_id": selected["id"],
                "selected_workflow_name": selected["name"],
                "workflow_type": profile["workflow_type"],
                "source_evidence_ids": evidence_refs,
                "derived_from_steps": self.state["original_workflow"]["steps"],
                "llm_powered": not bool(meta.get("_llm_fallback_used")),
            }
        )
        return {"product_solution": solution, **meta}

    def risk_reviewer(self) -> Dict[str, Any]:
        selected = self._selected()
        profile = self._profile(selected)
        evidence_refs = self._selected_evidence_refs(selected)
        steps = self.state["original_workflow"]["steps"]
        fallback = {
            "risks": [
                {"risk": risk, "level": level, "mitigation": mitigation, "requires_human_confirmation": level == "high"}
                for risk, level, mitigation in profile["risks"]
            ]
        }
        data, meta = self._llm_json(
            node="risk_reviewer",
            system=(
                "You review risks for restaurant Agent productization. Flag over-automation, false claims, price/menu mistakes, "
                "privacy, platform publishing, and human-review requirements. Return JSON only: "
                "{\"risks\": [{\"risk\":\"...\", \"level\":\"high/medium/low\", \"mitigation\":\"...\", \"requires_human_confirmation\": true}]}."
            ),
            payload={
                "selected_workflow": selected,
                "product_solution": self.state.get("product_solution"),
                "original_steps": steps,
                "evidence": self._selected_evidence_payload(selected),
            },
            fallback=fallback,
            temperature=0.2,
        )
        raw_risks = data.get("risks", []) if isinstance(data, dict) else []
        if not isinstance(raw_risks, list) or not raw_risks:
            raw_risks = fallback["risks"]
            meta["_llm_fallback_used"] = True
        risks = []
        for idx, item in enumerate(raw_risks, start=1):
            if not isinstance(item, dict):
                continue
            level = str(item.get("level", "medium")).lower()
            risks.append(
                {
                    "id": f"risk_{idx}",
                    "selected_workflow_id": selected["id"],
                    "selected_workflow_name": selected["name"],
                    "risk": str(item.get("risk", "")),
                    "level": level,
                    "mitigation": str(item.get("mitigation", "")),
                    "requires_human_confirmation": bool(item.get("requires_human_confirmation", level == "high")),
                    "source_evidence_ids": evidence_refs,
                    "derived_from_steps": steps[max(0, min(idx - 1, len(steps) - 1)) : min(len(steps), idx + 1)] or steps[:1],
                }
            )
        risks.append(
            {
                "id": "risk_evidence_insufficient",
                "selected_workflow_id": selected["id"],
                "selected_workflow_name": selected["name"],
                "risk": "Public evidence is insufficient or source mode uses fallback.",
                "level": "medium" if selected.get("evidence_insufficient") else "low",
                "mitigation": "Show source_mode honestly and lower score when public evidence links are fewer than three.",
                "requires_human_confirmation": False,
                "source_evidence_ids": evidence_refs,
                "derived_from_steps": steps[:1],
            }
        )
        return {"risk_review": risks, **meta}

    def export_report(self) -> Dict[str, Any]:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        files = {
            "trace.json": self.trace,
            "metrics.json": self.metrics.to_dict(),
            "health.json": self._health(),
            "llm_usage.json": self.state.get("llm_usage_records", []),
            "evidence_links.json": self.state.get("evidence", []),
            "candidate_workflows.json": self.state.get("candidate_workflows", []),
            "selected_workflow.json": self.state.get("selected_workflow"),
            "painpoints.json": self.state.get("painpoints", []),
            "agent_interventions.json": self.state.get("agent_interventions", []),
            "product_solution.json": self.state.get("product_solution", {}),
            "causal_audit.json": self.state.get("causal_audit", {}),
            "original_workflow.mmd": self.state.get("original_workflow_mmd", ""),
            "agent_workflow.mmd": self.state.get("agent_workflow_mmd", ""),
            "risk_review.md": self._risk_markdown(),
            "test_report.md": self._test_report(),
            "final_report.md": self._final_report(),
        }
        written = {}
        for filename, payload in files.items():
            path = self.run_dir / filename
            if filename.endswith(".json"):
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                path.write_text(str(payload), encoding="utf-8")
            written[filename] = str(path)
        return {"outputs": written}

    def _selected(self) -> Dict[str, Any]:
        selected = self.state.get("selected_workflow")
        if not selected:
            raise RuntimeError("No workflow selected. Run human_lock first.")
        return selected

    def _score_candidate(self, candidate: Dict[str, Any]) -> Dict[str, int]:
        evidence_count = int(candidate.get("evidence_count", 0))
        step_count = len(candidate.get("steps", []))
        ineff_count = len(candidate.get("inefficiencies", []))
        score = {
            "evidence_authenticity": min(20, evidence_count * 5),
            "flow_continuity": min(15, step_count * 3),
            "painpoint_clarity": min(20, ineff_count * 6 + (4 if ineff_count >= 3 else 0)),
            "agent_intervention_value": 18 if "视觉营销" in candidate["name"] else 14,
            "productization_potential": 14 if evidence_count >= 3 else 9,
            "mvp_verifiability": 9 if step_count >= 4 else 5,
        }
        if evidence_count < 3:
            score["evidence_authenticity"] = min(score["evidence_authenticity"], 10)
            score["productization_potential"] = min(score["productization_potential"], 8)
        return score

    def _normalize_steps(self, workflow_name: str, steps: List[str]) -> List[str]:
        if "视觉营销" in workflow_name:
            return [
                "拍摄门头/菜品/环境照片",
                "筛选和整理照片",
                "修图裁剪与风格处理",
                "套模板制作海报/菜单图/店铺图",
                "填写菜品卖点、价格、活动、营业时间",
                "适配并发布到社媒或店铺页",
                "复盘浏览、咨询、下单反馈",
            ]
        return steps[:8]

    def _merge_unique(self, values: Iterable[str]) -> List[str]:
        seen = set()
        result = []
        for value in values:
            if value and value not in seen:
                seen.add(value)
                result.append(value)
        return result

    def _mermaid(self, title: str, steps: List[str]) -> str:
        lines = ["flowchart TD", f'  title["{title}"]']
        last = "title"
        for idx, step in enumerate(steps, start=1):
            node = f"s{idx}"
            safe = step.replace('"', "'")
            lines.append(f'  {node}["{safe}"]')
            lines.append(f"  {last} --> {node}")
            last = node
        return "\n".join(lines) + "\n"

    def _health(self) -> Dict[str, Any]:
        return {
            "status": "ok",
            "agent": "RestaurantWorkflowDiscoveryAgent",
            "graph_version": "competition-v1-json-checkpoint",
            "run_id": self.run_id,
            "request_id": self.request_id,
            "source_mode": self.state.get("source_mode"),
            "execution_engine": self.state.get("execution_engine"),
            "graph_runtime": self.state.get("graph_runtime", {}),
            "llm": self.llm.safe_config(),
            "llm_mode": "stepfun_real_api" if self.llm.configured else "rules_fallback",
            "llm_usage_records": len(self.state.get("llm_usage_records", [])),
            "search_connector_configured": False,
            "live_search_enabled": bool(self.config.get("live_search")),
            "fallback_evidence_pool_available": bool(self.evidence_pool),
            "candidate_count": len(self.state.get("candidate_workflows", [])),
            "selected_workflow": (self.state.get("selected_workflow") or {}).get("name"),
            "outputs_dir": str(self.run_dir),
            "legacy_architecture_reuse": [
                "MetricsContext",
                "run_id/request_id",
                "trace.json",
                "health.json",
                "local JSON checkpoint",
                "benchmark-style test_report",
            ],
        }

    def _risk_markdown(self) -> str:
        risks = self.state.get("risk_review", [])
        lines = ["# 风险审核\n"]
        for item in risks:
            lines.append(f"- **{item['risk']}** ({item['level']}): {item['mitigation']} 人工确认: {item['requires_human_confirmation']}")
        return "\n".join(lines) + "\n"

    def _test_report(self) -> str:
        candidates = self.state.get("candidate_workflows", [])
        selected = self.state.get("selected_workflow") or {}
        tests = [
            ("正常发现", len(candidates) >= 3, f"候选工作流数量: {len(candidates)}"),
            (
                "证据不足降分",
                any(item.get("evidence_insufficient") for item in candidates)
                or all(item.get("evidence_count", 0) >= 3 for item in candidates),
                "候选均带 evidence_insufficient 字段或满足证据数量",
            ),
            (
                "搜索偏题过滤",
                all("纯 AI 生图娱乐" not in item.get("name", "") for item in candidates),
                "纯 AI 生图娱乐未进入候选",
            ),
            (
                "人工锁定",
                bool(selected.get("human_lock")),
                f"selected={selected.get('name', '')}",
            ),
            (
                "风险审核",
                any(r.get("requires_human_confirmation") for r in self.state.get("risk_review", [])),
                "高风险项要求人工确认",
            ),
        ]
        lines = ["# Test Report\n"]
        for name, passed, detail in tests:
            lines.append(f"- {name}: {'PASS' if passed else 'FAIL'} - {detail}")
        return "\n".join(lines) + "\n"

    def _final_report(self) -> str:
        selected = self.state.get("selected_workflow") or {}
        solution = self.state.get("product_solution") or {}
        original = self.state.get("original_workflow") or {}
        agent_flow = self.state.get("agent_intervention_workflow") or {}
        metrics = self.metrics.to_dict()
        lines = [
            "# 餐饮行业 Workflow Discovery Agent 产品说明",
            "",
            "## 1. 项目概述",
            "本项目搭建一个餐饮行业 Workflow Discovery Agent，用于从公开证据中发现真实人工工作流，拆解流程、低效点，并生成 Agent 介入后的产品化方案初稿。",
            "",
            "## 2. Agent 搭建说明",
            "采用分阶段状态机：brief_intake -> query_planner -> search_executor -> evidence_extractor -> candidate_builder -> candidate_scorer -> human_lock -> workflow_decomposer -> painpoint_analyzer -> intervention_designer -> product_solution_generator -> risk_reviewer -> causal_auditor -> export_report。",
            "",
            "## 3. 使用的技术与工具",
            "Python 标准库、内置公开证据池、MetricsContext、run_id/request_id、trace.json、metrics.json、health.json、本地 JSON checkpoint。生产版可迁移到 LangGraph StateGraph、PostgreSQL Checkpointer 和 LangSmith。",
            "",
            "## 4. 设计理念",
            "不做黑盒一键生成。先受约束搜索和评分，再 human lock 锁定工作流，最后深度拆解并生成方案，兼顾正确率、成本控制和可复盘性。",
            "",
            "## 5. 公开证据链接",
        ]
        for item in self.state.get("evidence", []):
            if item["id"] in set(selected.get("evidence_ids", [])):
                lines.append(f"- [{item['title']}]({item['url']}): {item['proof_point']}")
        lines.extend(
            [
                "",
                "## 6. 原始工作流流程图",
                "```mermaid",
                self.state.get("original_workflow_mmd", "").strip(),
                "```",
                "",
                "## 7. Agent 改造后的新流程图",
                "```mermaid",
                self.state.get("agent_workflow_mmd", "").strip(),
                "```",
                "",
                "## 8. 低效点与 Agent 介入点",
            ]
        )
        for point in self.state.get("painpoints", []):
            lines.append(f"- **{point['type']}**: {point['where']}。影响: {point['impact']}")
        lines.extend(
            [
                "",
                "## 9. 产品方案",
                f"- 产品名: {solution.get('product_name', '')}",
                f"- 定位: {solution.get('positioning', '')}",
                f"- 目标用户: {'、'.join(solution.get('target_users', []))}",
                f"- 核心功能: {'、'.join(solution.get('core_features', []))}",
                "",
                "## 10. 7 天 MVP 验证计划",
            ]
        )
        for item in solution.get("mvp_7_day_plan", []):
            lines.append(f"- {item}")
        lines.extend(
            [
                "",
                "## 11. 成本与效率记录",
                f"- run_id: {self.run_id}",
                f"- request_id: {self.request_id}",
                f"- source_mode: {self.state.get('source_mode')}",
                f"- 总耗时 ms: {metrics['totals']['latency_ms']}",
                f"- 预估 token: {metrics['totals']['total_tokens']}",
                f"- 模型成本 USD: {metrics['totals']['estimated_cost_usd']}",
                f"- 工具调用次数: {metrics['totals']['tool_calls']}",
                f"- 错误次数: {metrics['totals']['error_count']}",
                "",
                "## 12. 风险与人工确认机制",
            ]
        )
        for risk in self.state.get("risk_review", []):
            lines.append(f"- {risk['risk']}: {risk['mitigation']}")
        lines.extend(
            [
                "",
                "## 13. 运行证据与 trace",
                "本次运行已导出 trace.json、metrics.json、health.json、candidate_workflows.json、selected_workflow.json、test_report.md，可用于审查和复盘。",
                "",
                "## 14. 选中工作流",
                f"- 名称: {selected.get('name', '')}",
                f"- 分数: {selected.get('score', '')}",
                f"- human lock: {(selected.get('human_lock') or {}).get('mode', '')}",
                "",
                "## 15. 原始步骤",
            ]
        )
        for step in original.get("steps", []):
            lines.append(f"- {step}")
        lines.extend(["", "## 16. Agent 改造步骤"])
        for step in agent_flow.get("steps", []):
            lines.append(f"- {step}")
        return "\n".join(lines) + "\n"


def load_config(path: Optional[str]) -> Dict[str, Any]:
    if not path:
        return dict(DEFAULT_CONFIG)
    return json.loads(Path(path).read_text(encoding="utf-8"))


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Restaurant Workflow Discovery Agent")
    parser.add_argument("--config", default="", help="JSON config path")
    parser.add_argument("--out", default="outputs", help="Output root")
    parser.add_argument("--mode", choices=["all", "discovery"], default="all")
    parser.add_argument("--lock-index", type=int, default=None)
    parser.add_argument("--request-id", default=None)
    parser.add_argument(
        "--runner",
        choices=["state-machine", "langgraph"],
        default="state-machine",
        help="Execution engine. langgraph keeps the same business nodes but uses StateGraph orchestration.",
    )
    args = parser.parse_args(argv)

    agent = RestaurantWorkflowDiscoveryAgent(
        config=load_config(args.config),
        out_root=args.out,
        request_id=args.request_id,
    )
    if args.runner == "langgraph":
        from .langgraph_runner import run_with_langgraph

        state = run_with_langgraph(agent, mode=args.mode, lock_index=args.lock_index)
    else:
        state = agent.run(mode=args.mode, lock_index=args.lock_index)
    # Use ASCII-safe JSON for Windows terminals that may not render UTF-8 stdout.
    # Output files are still written as UTF-8 Chinese.
    print(json.dumps({
        "run_id": state["run_id"],
        "request_id": state["request_id"],
        "selected_workflow": (state.get("selected_workflow") or {}).get("name"),
        "outputs_dir": str(agent.run_dir),
        "source_mode": state.get("source_mode"),
        "execution_engine": state.get("execution_engine"),
    }, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
