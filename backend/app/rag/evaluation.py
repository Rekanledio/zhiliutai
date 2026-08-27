from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class EvalCase:
    case_id: str
    query: str
    relevant_chunk_ids: frozenset[str]


def load_eval_cases(path: Path) -> tuple[EvalCase, ...]:
    raw_cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw_cases, list):
        raise ValueError("评测集必须是数组")
    cases: list[EvalCase] = []
    for raw in raw_cases:
        if not isinstance(raw, dict):
            raise ValueError("评测用例结构无效")
        case_id = raw.get("id")
        query = raw.get("query")
        relevant = raw.get("relevant_chunk_ids")
        if (
            not isinstance(case_id, str)
            or not case_id
            or not isinstance(query, str)
            or not query
            or not isinstance(relevant, list)
            or not relevant
            or any(not isinstance(chunk_id, str) or not chunk_id for chunk_id in relevant)
        ):
            raise ValueError("评测用例字段无效")
        cases.append(EvalCase(case_id, query, frozenset(relevant)))
    return tuple(cases)


def evaluate_rankings(
    cases: Sequence[EvalCase],
    rankings: Mapping[str, Sequence[str]],
    *,
    k: int = 5,
) -> dict[str, Any]:
    if k <= 0:
        raise ValueError("评测 k 必须为正数")
    if not cases:
        raise ValueError("评测集不能为空")
    hits = 0
    reciprocal_rank = 0.0
    relevant_total = 0
    for case in cases:
        ranking = list(rankings.get(case.case_id, ()))
        top_k = ranking[:k]
        relevant_total += len(case.relevant_chunk_ids)
        hit_ids = set(top_k) & case.relevant_chunk_ids
        hits += len(hit_ids)
        for rank, chunk_id in enumerate(top_k, start=1):
            if chunk_id in case.relevant_chunk_ids:
                reciprocal_rank += 1.0 / rank
                break
    case_count = len(cases)
    return {
        "case_count": case_count,
        "k": k,
        "recall_at_k": hits / relevant_total if relevant_total else 0.0,
        "mrr_at_k": reciprocal_rank / case_count,
        "hit_rate_at_k": sum(
            bool(set(list(rankings.get(case.case_id, ()))[:k]) & case.relevant_chunk_ids)
            for case in cases
        )
        / case_count,
    }
