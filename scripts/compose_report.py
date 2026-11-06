#!/usr/bin/env python3
"""Compose a patent big-data report by orchestrating the patent MCP.

Calls the upstream patent-mcp-server tools and assembles a structured JSON
payload rendered into a professional HTML / Markdown report. Supports
``--dry-run`` which returns a well-formed skeleton from the bundled sample data
WITHOUT contacting the MCP.

Workflow (real run):
  1. Resolve the canonical enterprise name (fuzzy search if only a keyword).
  2. Query patent_stats (统计 / 趋势) and patent_search (records).
  3. Build unified report JSON with domain sections (专利统计 / 申请授权趋势 / 类型分布 / 状态分布 / 检索明细).
  4. Optionally render HTML + Markdown.

This file never prints secrets; MCP credentials live in the server's own .env.
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import pathlib
from typing import Any, Dict, List, Mapping, Optional, Sequence

from common import REPORT_BANNER, REPORT_TYPE, attach_pagination, call_tool_once, collect_detail_pages, paid_call_scope, json_dumps, load_json_file, print_json
import mcp_client
from render_report import render_html, render_markdown, html_to_pdf

SAMPLE_PATH = pathlib.Path(__file__).resolve().parent.parent / "assets" / "report.example.json"

# Patent MCP tools.
T_FUZZY = "patent_bigdata_fuzzy_search"
T_SEARCH = "patent_bigdata_patent_search"
T_STATS = "patent_bigdata_patent_stats"


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #




def _is_empty_signal(value: Any) -> bool:
    """Recognize documented no-data text without classifying it as an API failure."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    return bool(text) and any(token in text for token in (
        "查询数据为空", "暂无数据", "未查询到", "未检索到", "查询结果为空",
    ))

def _is_api_error(value: Any) -> bool:
    """Recognize failures without treating a documented empty result as an error."""
    if value is None:
        return False
    if isinstance(value, str):
        text = value.strip()
        if any(s in text for s in ("查询数据为空", "暂无数据", "未查询到", "未检索到")):
            return False
        return any(s in text for s in (
            "接口调用失败", "查询失败", "Error executing tool", "validation error",
            "状态码：4", "状态码：5", "密钥错误", "签名错误", "未配置",
        ))
    if not isinstance(value, dict):
        return False
    if value.get("isError") is True or value.get("success") is False:
        return True
    if value.get("_error") or value.get("error") or value.get("error_message"):
        return True
    if "code" in value and value.get("code") not in (None, 0, 200, "0", "200", "SUCCESS", "success"):
        return True
    if set(value) == {"text"} and value.get("text"):
        return not any(s in str(value["text"]) for s in ("查询数据为空", "暂无数据", "未查询到", "未检索到"))
    return any(_is_api_error(v) for v in value.values() if isinstance(v, (dict, str)))


def _first_list(value: Any) -> List[Any]:
    if _is_api_error(value):
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("resultList", "list", "items", "records", "data"):
            if isinstance(value.get(key), list):
                return value[key]
    return []


def _first_record(value: Any) -> Dict[str, Any]:
    for record in _first_list(value):
        if isinstance(record, dict) and _has_evidence(record):
            return record
    if isinstance(value, dict) and not _is_api_error(value) and _has_evidence(value):
        return value
    return {}


def _has_evidence(value: Any) -> bool:
    if value is None or value in ("", "-") or _is_empty_signal(value):
        return False
    if _is_api_error(value):
        return False
    if isinstance(value, dict):
        return any(_has_evidence(v) for k, v in value.items() if k not in {"sections", "_raw"})
    if isinstance(value, list):
        return any(_has_evidence(v) for v in value)
    return True


def _text(value: Any, limit: int = 0) -> str:
    if value is None or _is_empty_signal(value):
        return ""
    if isinstance(value, (dict, list)):
        t = json.dumps(value, ensure_ascii=False)
    else:
        t = str(value)
    t = " ".join(t.split())
    if limit and len(t) > limit:
        return t[: limit - 1].rstrip() + "…"
    return t


def _int(value: Any) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _safe_call(tool: str, arguments: Dict[str, Any]) -> Any:
    try:
        result = call_tool_once(mcp_client.call_tool, tool, arguments)
        # Detect API error responses (405, etc.) and return error marker
        if _is_api_error(result):
            return {"_error": "API错误", "_raw": result}
        return result
    except Exception as exc:
        return {"_error": str(exc)}


def _safe_total(payload: Any) -> Any:
    if isinstance(payload, dict):
        if _is_api_error(payload):
            return None
        return payload.get("total")
    return None


def _collect_detail_pages(tool: str, arguments: Dict[str, Any], max_pages: int) -> Dict[str, Any]:
    """Total-driven detail collection within an explicit paid-page budget (default 1 page).

    The first paid page serves both as report data and as total discovery; it is
    never refetched. Additional pages are only requested inside the budget.
    """
    return collect_detail_pages(
        _safe_call, tool, dict(arguments), get_rows=_first_list, get_total=_safe_total,
        is_error=_is_api_error, max_pages=max_pages,
    )


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

def resolve_enterprise_name(raw: str) -> Dict[str, Any]:
    raw = (raw or "").strip()
    if not raw:
        return {"keyword": "", "enterprise": "", "resolved": False, "reason": "关键词为空"}
    if any(suffix in raw for suffix in ("公司", "集团", "有限", "院", "厂", "中心", "事务所", "合作社", "合伙")):
        return {"keyword": raw, "enterprise": raw, "resolved": True, "reason": "视为企业全称"}
    fuzzy = _safe_call(T_FUZZY, {"matchKeyword": raw, "pageSize": 1})
    record = _first_record(fuzzy)
    total = _int(_safe_total(fuzzy))
    name = str(record.get("name") or "").strip()
    if name and total == 1:
        return {"keyword": raw, "enterprise": name, "resolved": True, "reason": "模糊查询唯一候选补全", "fuzzy_total": total, "record": record, "fuzzy_requested": True}
    if name and total and total > 1:
        return {"keyword": raw, "enterprise": raw, "resolved": False, "reason": f"模糊查询返回 {total} 个候选，未静默采用首条", "fuzzy_total": total, "fuzzy_requested": True}
    return {"keyword": raw, "enterprise": raw, "resolved": False, "reason": "模糊查询未命中唯一企业全称，按原始关键词查询", "fuzzy_requested": True}


# --------------------------------------------------------------------------- #
# Enterprise profile helpers (from fuzzy_search record)
# --------------------------------------------------------------------------- #

def _extract_profile(record: Dict[str, Any]) -> Dict[str, Any]:
    """Extract enterprise profile fields from a fuzzy_search record."""
    return {
        "name": _text(record.get("name")),
        "reg_capital": record.get("regCapitalValue"),
        "reg_capital_coin": _text(record.get("regCapitalCoinType")),
        "annual_turnover": _text(record.get("annualTurnover")),
        "oper_status": _text(record.get("operStatus")),
        "enterprise_type": _text(record.get("enterpriseType")),
        "found_time": _text(record.get("foundTime")),
        "legal_rep": _text(record.get("legalRepresentative")),
        "address": _text(record.get("address")),
        "homepage": _text(record.get("homepage")),
    }


def _format_capital(val: Any, coin: str = "") -> str:
    """Format capital value: 10995210218.0 -> '109.95 亿'."""
    try:
        v = float(val)
        if v >= 1e8:
            s = f"{v / 1e8:.2f} 亿"
        elif v >= 1e4:
            s = f"{v / 1e4:.2f} 万"
        else:
            s = f"{v:.0f}"
        if coin:
            s += f" {coin}"
        return s
    except (TypeError, ValueError):
        return _text(val) if val else "-"


def _enrich_metrics_with_profile(metrics: List[Dict[str, Any]], record: Any) -> List[Dict[str, Any]]:
    """Append enterprise profile metrics from a fuzzy_search record."""
    if not isinstance(record, dict):
        return metrics
    _prof = _extract_profile(record)
    if _prof.get("reg_capital") and _prof["reg_capital"] not in ("-", "", None):
        metrics.append({"label": "注册资本", "value": _format_capital(_prof["reg_capital"], _prof.get("reg_capital_coin", "")), "hint": "工商登记注册资本"})
    if _prof.get("found_time") and _prof["found_time"] != "-":
        metrics.append({"label": "成立时间", "value": _prof["found_time"], "hint": "工商登记成立日期"})
    if _prof.get("oper_status") and _prof["oper_status"] != "-":
        metrics.append({"label": "经营状态", "value": _prof["oper_status"], "hint": "工商登记经营状态"})
    if _prof.get("enterprise_type") and _prof["enterprise_type"] != "-":
        metrics.append({"label": "企业类型", "value": _prof["enterprise_type"], "hint": "工商登记企业类型"})
    if _prof.get("legal_rep") and _prof["legal_rep"] != "-":
        metrics.append({"label": "法定代表人", "value": _prof["legal_rep"], "hint": "工商登记法定代表人"})
    return metrics


# Legacy sample-scoped metric labels removed from composer output; used only by
# apply_content_review() to clean older payloads.
SAMPLE_METRIC_LABELS = {"专利类型数", "申请人/发明人数"}


# --------------------------------------------------------------------------- #
# Section builders
# --------------------------------------------------------------------------- #

def build_subject(raw: str, resolved: Mapping[str, Any], keyword_type: str) -> Dict[str, Any]:
    return {
        "enterprise": resolved.get("enterprise") or raw,
        "matchKeyword": resolved.get("enterprise") or raw,
        "keywordType": keyword_type,
        "match_raw": raw,
        "resolved": bool(resolved.get("resolved")),
        "resolve_reason": resolved.get("reason", ""),
        "searchKeywordType": "申请人",
        "fuzzy_total": resolved.get("fuzzy_total"),
        "requires_confirmation": bool((resolved.get("fuzzy_total") or 0) > 1),
        "fuzzy_requested": bool(resolved.get("fuzzy_requested")),
    }


def requested_products(subject: Mapping[str, Any]) -> List[Dict[str, str]]:
    products: List[Dict[str, str]] = []
    if subject.get("fuzzy_requested"):
        products.append({"name": "企业模糊查询", "product_id": "675cea1f0e009a9ea37edaa1"})
    if not subject.get("requires_confirmation"):
        products.extend([
            {"name": "专利检索", "product_id": "66b338e274bf098447db7f37"},
            {"name": "专利统计", "product_id": "66d5b7df537c3f61d646c230"},
        ])
    return products


def build_metrics(stats: Mapping[str, Any], search_total: Any, core: Optional[Mapping[str, Any]] = None) -> List[Dict[str, Any]]:
    metrics: List[Dict[str, Any]] = []
    core = core or {}
    s = stats if isinstance(stats, dict) else {}
    # 真实结构：patentTypeStat 是扁平 dict {inventionAppPatentCount, ...}
    type_raw = s.get("patentTypeStat")
    type_raw = type_raw if isinstance(type_raw, dict) else {}
    inv_app_n = _int(type_raw.get("inventionAppPatentCount"))
    inv_lic_n = _int(type_raw.get("inventionLicPatentCount"))
    util_n = _int(type_raw.get("utilityModelPatentCount"))
    design_n = _int(type_raw.get("appearanceDesignPatentCount"))
    known_type_counts = [n for n in (inv_app_n, inv_lic_n, util_n, design_n) if n is not None]
    total_n = sum(known_type_counts) if known_type_counts else _int(s.get("patentCount"))

    metrics.append({"label": "专利总数", "value": str(total_n) if total_n is not None else "-", "hint": "企业专利总量"})
    metrics.append({"label": "发明申请", "value": str(inv_app_n) if inv_app_n is not None else "-", "hint": "发明申请专利数量"})
    # 申请量和授权量是各自的累计存量，并非同一申请队列。这里只展示
    # 上游原始计数，不能相除生成申请结果比例。
    metrics.append({"label": "发明授权", "value": str(inv_lic_n) if inv_lic_n is not None else "-", "hint": "发明授权专利数量"})
    metrics.append({"label": "实用新型", "value": str(util_n) if util_n is not None else "-", "hint": "实用新型专利数量"})
    metrics.append({"label": "外观设计", "value": str(design_n) if design_n is not None else "-", "hint": "外观设计专利数量"})
    if total_n and inv_app_n:
        share = (inv_app_n + (inv_lic_n or 0)) / total_n * 100
        metrics.append({"label": "发明型占比", "value": f"{share:.0f}%", "hint": "发明申请+授权 / 总数"})
    # 有效率：有效 / (有效+失效+审查中+失败)（不计“其他”程序性状态）
    validity = core.get("validity_stat") or []
    if validity:
        vmap = {r.get("有效性"): int(r.get("数量", 0) or 0) for r in validity}
        denom = sum(vmap.get(k, 0) for k in ("有效", "失效", "审查中", "失败"))
        if denom:
            valid_rate = vmap.get("有效", 0) / denom * 100
            metrics.append({"label": "专利有效率", "value": f"{valid_rate:.1f}%", "hint": "有效 /（有效+失效+审查中+失败）", "delta": "有效" if valid_rate >= 50 else None})
    metrics.append({"label": "检索结果", "value": (_text(search_total) if search_total is not None else "-"), "hint": "本次检索命中专利条数"})
    return [m for m in metrics if m.get("value") not in ("", None, "-")]


def build_caliber(subject: Mapping[str, Any]) -> Dict[str, Any]:
    detail_filter = "申请人=目标企业"
    if subject.get("patentType"):
        detail_filter += f"，专利类型={subject['patentType']}"
    products = [item["name"] for item in requested_products(subject)]
    data_scope = f"专利统计（企业整体口径）；专利检索明细（明细筛选：{detail_filter}）；申请/授权趋势、类型分布、状态分布"
    if products == ["企业模糊查询"]:
        data_scope = "仅执行企业模糊查询；主体未唯一确认，未调用专利统计或专利检索"
    return {
        "match_target": subject.get("enterprise") or subject.get("match_raw"),
        "match_type": f"专利统计按企业主体匹配（keywordType={subject.get('keywordType', 'name')}）；检索支持专利名称/申请号/申请人/代理机构",
        "data_scope": data_scope,
        "products": products,
        "limit": "数据来自专利公开数据库；少量字段可能存在更新延迟。",
    }


def _stat_rows(stats: Mapping[str, Any], key: str, label_key: str, count_key: str = "count") -> List[Dict[str, Any]]:
    """Generic {label, count} extraction — kept for compatibility."""
    rows = []
    for item in _first_list(stats.get(key)):
        if not isinstance(item, dict) or not _has_evidence(item):
            continue
        label = item.get(label_key) or item.get("year") or item.get("patentDivAppLegal") or "-"
        rows.append({
            "名称/类型/状态/年份": _text(label),
            "数量": _text(item.get(count_key) or item.get("patentCount") or item.get("value") or "-"),
        })
    return rows


_TREND_TYPE_KEYS = (
    ("inventionAppPatentCount", "发明申请"),
    ("inventionLicPatentCount", "发明授权"),
    ("utilityModelPatentCount", "实用新型"),
    ("appearanceDesignPatentCount", "外观设计"),
)


def _trend_total_rows(stats: Mapping[str, Any], key: str) -> List[Dict[str, Any]]:
    """Sum the 4 patent-type counts per year into a single total-per-year series.

    Real shape: [{year, inventionAppPatentCount, inventionLicPatentCount,
                  utilityModelPatentCount, appearanceDesignPatentCount}, ...]
    """
    rows: List[Dict[str, Any]] = []
    for item in _first_list(stats.get(key)):
        if not isinstance(item, dict) or not _has_evidence(item):
            continue
        year = item.get("year") or "-"
        total = 0
        has_any = False
        for type_key, _ in _TREND_TYPE_KEYS:
            n = _int(item.get(type_key))
            if n is not None:
                total += n
                has_any = True
        if has_any:
            rows.append({"名称/类型/状态/年份": _text(year), "数量": str(total)})
    return rows


def _trend_multi_rows(stats: Mapping[str, Any], key: str) -> List[Dict[str, Any]]:
    """Build multi-series trend rows: one row per year with 4 type columns.

    Output: [{年份: "2017", "发明申请": 15, "发明授权": 11, "实用新型": 13, "外观设计": 14}, ...]
    Missing type counts default to 0 so the multi_line chart renders continuous series.
    """
    rows: List[Dict[str, Any]] = []
    for item in _first_list(stats.get(key)):
        if not isinstance(item, dict) or not _has_evidence(item):
            continue
        year = item.get("year")
        if year is None:
            continue
        row: Dict[str, Any] = {"年份": _text(year)}
        has_any = False
        for type_key, label in _TREND_TYPE_KEYS:
            n = _int(item.get(type_key))
            row[label] = n or 0
            if n:
                has_any = True
        if has_any:
            rows.append(row)
    return rows


# 专利有效性分桶：将细法律状态归并为 4 大类
# 有效：授权（仍有效）；失效：终止/放弃/无效；审查中：公布/实质审查；失败：驳回/撤回
# 顺序很关键：失败(驳回/撤回)与失效(无效/放弃/终止)是终局信号，须先于审查中(公布/实质审查)判定，
# 否则“发明专利申请公布后的驳回”会被“公布”误判为审查中。
_VALIDITY_BUCKETS = [
    ("失败", ("驳回", "撤回")),
    ("失效", ("终止", "放弃", "无效")),
    ("有效", ("授权",)),
    ("审查中", ("公布", "实质审查")),
]
# 转移/变更/著录等程序性状态不计入有效率分母


def _bucket_validity(label: str) -> str:
    """Map a fine legal-status label to one of 有效/失效/审查中/失败/其他."""
    if not label:
        return "其他"
    for bucket, tokens in _VALIDITY_BUCKETS:
        if any(tok in label for tok in tokens):
            return bucket
    return "其他"


def _validity_rows(stats: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Aggregate patentDivAppLegalStat into 有效/失效/审查中/失败 buckets."""
    bucket_count: Dict[str, int] = {}
    for item in _first_list(stats.get("patentDivAppLegalStat")):
        if not isinstance(item, dict) or not _has_evidence(item):
            continue
        label = _text(item.get("patentDivAppLegal"))
        n = _int(item.get("patentCount"))
        if n is None or not label:
            continue
        bucket = _bucket_validity(label)
        bucket_count[bucket] = bucket_count.get(bucket, 0) + n
    # ordered
    order = ["有效", "失效", "审查中", "失败", "其他"]
    return [{"有效性": b, "数量": bucket_count.get(b, 0)} for b in order if bucket_count.get(b)]


def _status_rows(stats: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """patentDivAppLegalStat: [{patentDivAppLegal, patentCount}, ...]"""
    rows = []
    for item in _first_list(stats.get("patentDivAppLegalStat")):
        if not isinstance(item, dict) or not _has_evidence(item):
            continue
        label = item.get("patentDivAppLegal") or "-"
        n = _int(item.get("patentCount"))
        if n is not None:
            rows.append({"名称/类型/状态/年份": _text(label), "数量": str(n)})
    return rows


def _same_subject(target: str, applicant: Any) -> bool:
    def norm(value: Any) -> str:
        return "".join(ch for ch in _text(value).lower() if ch.isalnum() or "\u4e00" <= ch <= "\u9fff")
    left, right = norm(target), norm(applicant)
    return bool(left and right and (left in right or right in left))


def build_core_analysis(search: Any, stats: Mapping[str, Any], target: str = "", patent_type: Optional[str] = None) -> Dict[str, Any]:
    s = stats if isinstance(stats, dict) else {}

    TYPE_LABELS = (
        ("inventionAppPatentCount", "发明申请"),
        ("inventionLicPatentCount", "发明授权"),
        ("utilityModelPatentCount", "实用新型"),
        ("appearanceDesignPatentCount", "外观设计"),
    )

    # patentTypeStat: 真实结构是扁平 dict {inventionAppPatentCount: N, ...}
    type_raw = s.get("patentTypeStat")
    type_counts: Dict[str, int] = {}
    if isinstance(type_raw, dict):
        for key, label in TYPE_LABELS:
            n = _int(type_raw.get(key))
            if n is not None:
                type_counts[label] = n
    # 兜底：嵌套形式 {year: {...}}
    if not type_counts and isinstance(type_raw, dict):
        for _, val in type_raw.items():
            if isinstance(val, dict):
                for key, label in TYPE_LABELS:
                    n = _int(val.get(key))
                    if n is not None:
                        type_counts[label] = type_counts.get(label, 0) + n

    total_count = sum(type_counts.values())

    # 专利统计 KV
    stats_kv: Dict[str, Any] = {}
    if total_count:
        stats_kv["专利总数"] = str(total_count)
    for label in ("发明申请", "发明授权", "实用新型", "外观设计"):
        if type_counts.get(label):
            stats_kv[label + "专利数"] = str(type_counts[label])

    # 检索明细表
    search_rows = []
    total = None
    if isinstance(search, dict):
        total = search.get("total")
    for item in _first_list(search):
        if not isinstance(item, dict) or not _has_evidence(item):
            continue
        if target and not _same_subject(target, item.get("patentApplicantName")):
            continue
        search_rows.append({
            "专利名称": _text(item.get("patentName")) or "-",
            "申请号": _text(item.get("patentApplicationNum")) or "-",
            "申请人": _text(item.get("patentApplicantName")) or "-",
            "专利类型": _text(item.get("patentType")) or "-",
            "申请日期": _text(item.get("patentApplicationDate")) or "-",
            "法律状态": _text(item.get("calPatentLegalStatus")) or "-",
            "代理机构": _text(item.get("patentAgency")) or "-",
        })

    # 趋势：patentTypeAppTimeStat/PubTimeStat 每行含 year + 4 种类型 count
    # 改为 multi_line：4 条类型序列（发明申请/发明授权/实用新型/外观设计）
    apply_trend = _trend_multi_rows(s, "patentTypeAppTimeStat")
    grant_trend = _trend_multi_rows(s, "patentTypePubTimeStat")
    # 状态分布：patentDivAppLegalStat 每行含 patentDivAppLegal + patentCount
    status_stat = _status_rows(s)
    # 有效性分桶（有效/失效/审查中/失败）
    validity_stat = _validity_rows(s)
    # 类型分布
    type_stat = [{"名称/类型/状态/年份": label, "数量": str(n)} for label, n in type_counts.items() if n]

    sections = [
        {"key": "patent_stats", "title": "专利统计", "kind": "kv"},
        {"key": "apply_trend", "title": "专利申请趋势（分类型）", "kind": "multi_line", "note": "按年度统计 4 类专利（发明申请/发明授权/实用新型/外观设计）申请数量",
         "chart": {"x": "年份", "series": ["发明申请", "发明授权", "实用新型", "外观设计"], "area": False},
         "columns": [("年份", "年份"), ("发明申请", "发明申请"), ("发明授权", "发明授权"), ("实用新型", "实用新型"), ("外观设计", "外观设计")]},
        {"key": "grant_trend", "title": "专利授权趋势（分类型）", "kind": "multi_line", "note": "按年度统计 4 类专利授权数量",
         "chart": {"x": "年份", "series": ["发明申请", "发明授权", "实用新型", "外观设计"], "area": False},
         "columns": [("年份", "年份"), ("发明申请", "发明申请"), ("发明授权", "发明授权"), ("实用新型", "实用新型"), ("外观设计", "外观设计")]},
        {"key": "validity_stat", "title": "专利有效性结构", "kind": "donut", "note": "将法律状态归并为 有效/失效/审查中/失败",
         "chart": {"name": "有效性", "value": "数量", "donut": True}, "columns": [("有效性", "有效性"), ("数量", "数量")]},
        {"key": "type_stat", "title": "专利类型分布", "kind": "pie", "note": "按专利类型统计数量", "chart": {"name": "名称/类型/状态/年份", "value": "数量", "donut": True}, "columns": [("类型", "名称/类型/状态/年份"), ("数量", "数量")]},
        {"key": "status_stat", "title": "专利法律状态分布", "kind": "bar", "note": "按专利法律状态统计数量", "chart": {"name": "名称/类型/状态/年份", "value": "数量", "orient": "v"}, "columns": [("状态", "名称/类型/状态/年份"), ("数量", "数量")]},
        {"key": "search_records", "title": "专利检索明细", "kind": "table", "note": (
            f"明细筛选：申请人={target or '目标企业'}" + (f"、专利类型={patent_type}" if patent_type else "")
            + (f"；上游总量 {total} 条，本次采集 {len(search_rows)} 条" if total is not None
               else f"；上游未返回总量，本次采集 {len(search_rows)} 条")
        ),
         "columns": [("专利名称", "专利名称"), ("申请号", "申请号"), ("申请人", "申请人"), ("专利类型", "专利类型"), ("申请日期", "申请日期"), ("法律状态", "法律状态"), ("代理机构", "代理机构")]},
    ]

    return {
        "sections": sections,
        "patent_stats": stats_kv,
        "apply_trend": apply_trend,
        "grant_trend": grant_trend,
        "validity_stat": validity_stat,
        "type_stat": type_stat,
        "status_stat": status_stat,
        "search_records": search_rows,
    }


def _type_stat_rows(stats: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """patentTypeStat 是按年份分组嵌套的 dict；这里汇总成全局类型分布。"""
    counts: Dict[str, int] = {}
    raw = stats.get("patentTypeStat")
    if isinstance(raw, dict):
        # 嵌套形式：{year: {inventionAppPatentCount: x, ...}}
        for _, val in raw.items():
            if isinstance(val, dict):
                for label, key in (
                    ("发明申请", "inventionAppPatentCount"),
                    ("发明授权", "inventionLicPatentCount"),
                    ("实用新型", "utilityModelPatentCount"),
                    ("外观设计", "appearanceDesignPatentCount"),
                ):
                    n = _int(val.get(key))
                    if n is not None:
                        counts[label] = counts.get(label, 0) + n
    rows = []
    if counts:
        for label in ("发明申请", "发明授权", "实用新型", "外观设计"):
            if counts.get(label):
                rows.append({"名称/类型/状态/年份": label, "数量": str(counts[label])})
    # 兜底：若没解析到嵌套，按顶层字段汇总
    if not rows:
        top = stats if isinstance(stats, dict) else {}
        for label, key in (
            ("发明申请", "inventionAppPatentCount"),
            ("发明授权", "inventionLicPatentCount"),
            ("实用新型", "utilityModelPatentCount"),
            ("外观设计", "appearanceDesignPatentCount"),
        ):
            n = _int(top.get(key))
            if n is not None:
                rows.append({"名称/类型/状态/年份": label, "数量": str(n)})
    return rows


def build_records(core: Mapping[str, Any]) -> List[Dict[str, Any]]:
    out = []
    for item in core.get("search_records") or []:
        out.append({
            "专利名称": item.get("专利名称") or "-",
            "专利类型": item.get("专利类型") or "-",
            "申请日期": item.get("申请日期") or "-",
            "法律状态": item.get("法律状态") or "-",
        })
    return out[:20]


def _period_year(value: Any) -> Optional[int]:
    text = _text(value)
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def _trend_analysis(rows: List[Mapping[str, Any]], as_of_year: Optional[int] = None) -> Dict[str, Any]:
    """Compute a reliable year-over-year trend from a yearly patent series.

    Supports the legacy single-series rows ({名称/类型/状态/年份, 数量}) and the
    multi-series rows ({年份, 发明申请, 发明授权, 实用新型, 外观设计}) produced
    by _trend_multi_rows. Rules:
      * the current calendar year is partial and is excluded from peak/YoY;
      * fewer than 3 complete years, or either of the last two complete years
        below 10 patents, is reported as insufficient rather than as a trend;
      * direction and comparison years are returned separately so the report
        does not print the same percentage twice.
    """
    points: List[tuple[Any, float]] = []
    for r in rows:
        if "数量" in r:
            try:
                n = float(str(r.get("数量", 0)).replace(",", ""))
            except (TypeError, ValueError):
                n = 0.0
            points.append((r.get("名称/类型/状态/年份", "-"), n))
        else:
            total = 0.0
            for _, label in _TREND_TYPE_KEYS:
                try:
                    total += float(str(r.get(label, 0)).replace(",", ""))
                except (TypeError, ValueError):
                    pass
            points.append((r.get("年份", "-"), total))
    if not points:
        return {}

    partial_period = None
    if as_of_year is not None:
        complete = [(p, n) for p, n in points if _period_year(p) != as_of_year]
        if len(complete) >= 2 and len(complete) < len(points):
            partial_period = _text(points[-1][0])
            points = complete
    if len(points) < 3:
        return {"insufficient": "完整年度不足 3 期，未作趋势研判", "partial_period": partial_period}

    last_n = points[-1][1]
    prev_n = points[-2][1]
    if min(last_n, prev_n) < 10:
        return {
            "insufficient": "近两个完整年度数量过少（<10 件/年），未作同比研判",
            "partial_period": partial_period,
            "last_period": _text(points[-1][0]),
            "last": last_n,
        }

    peak_idx = max(range(len(points)), key=lambda i: points[i][1])
    pct = (last_n - prev_n) / prev_n * 100 if prev_n > 0 else 0.0
    if abs(pct) <= 5:
        direction = "基本持平"
        yoy = ""
    else:
        direction = f"{'上升' if pct > 0 else '下降'} {abs(pct):.0f}%"
        verb = "上升" if pct > 0 else "下降"
        yoy = f"{_text(points[-1][0])} 年较 {_text(points[-2][0])} 年{verb} {abs(pct):.0f}%"
    return {
        "peak_period": _text(points[peak_idx][0]),
        "peak_value": points[peak_idx][1],
        "direction": direction,
        "yoy": yoy,
        "last": last_n,
        "compare_period": _text(points[-1][0]),
        "base_period": _text(points[-2][0]),
        "partial_period": partial_period,
    }


def _concentration(rows: List[Mapping[str, Any]], top_n: int = 3, name_key: str = "名称/类型/状态/年份", value_key: str = "数量") -> Dict[str, Any]:
    """Compute top-N concentration (CRn) and dominant category."""
    items = []
    for r in rows:
        try:
            items.append((r.get(name_key, "-"), float(str(r.get(value_key, 0)).replace(",", ""))))
        except (TypeError, ValueError):
            items.append((r.get(name_key, "-"), 0.0))
    total = sum(v for _, v in items)
    if not total:
        return {}
    items.sort(key=lambda x: x[1], reverse=True)
    cr = sum(v for _, v in items[:top_n]) / total * 100
    return {"top": items[0][0], "top_share": items[0][1] / total * 100, "cr": cr, "total": total}


def build_insights(subject: Mapping[str, Any], core: Mapping[str, Any], metrics: Sequence[Mapping[str, Any]], as_of_year: Optional[int] = None) -> List[Dict[str, Any]]:
    insights: List[Dict[str, Any]] = []
    metric_map = {m["label"]: str(m["value"]) for m in metrics}
    total = metric_map.get("专利总数")
    inv_app = metric_map.get("发明申请")
    inv_lic = metric_map.get("发明授权")

    if total:
        insights.append({
            "feature": "专利资产规模",
            "evidence": f"企业专利总数 {total} 件。",
            "interpretation": "该数量描述公开数据库中的累计专利记录规模；不能单独证明技术储备、研发投入、专利质量或商业价值。",
        })
    if inv_app or inv_lic:
        parts = []
        if inv_app:
            parts.append(f"发明申请 {inv_app}")
        if inv_lic:
            parts.append(f"发明授权 {inv_lic}")
        insights.append({
            "feature": "发明申请与授权存量",
            "evidence": "、".join(parts) + "。两项均为累计存量，口径不对应同一申请队列，不能相除推算申请结果比例。",
            "interpretation": "两项分别描述公开申请记录和授权记录规模；当前权利状态及技术、商业意义仍需逐件核验。",
        })
    type_stat = core.get("type_stat") or []
    if type_stat:
        conc = _concentration(type_stat, 3)
        if conc:
            insights.append({
                "feature": "专利类型集中度",
                "evidence": f"“{conc['top']}”占比约 {conc['top_share']:.0f}%，前 3 类合计 {conc['cr']:.0f}%（CR3）。",
                "interpretation": "类型集中度仅描述返回记录的类型构成，不能单独推断研发活动范围或技术布局质量。",
            })
    status_stat = core.get("status_stat") or []
    if status_stat:
        conc = _concentration(status_stat, 2)
        if conc:
            insights.append({
                "feature": "专利法律状态集中度",
                "evidence": f"“{conc['top']}”为记录最多的法律状态，占全部法律状态记录的 {conc['top_share']:.0f}%（含程序性状态）。",
                "interpretation": "该占比的分母是全部法律状态记录，不等同于“有效率”（有效率分母剔除转移、变更等程序性状态）；失效或失败原因需逐件核查。",
            })
    # 专利有效性分桶 + 有效率 + 失效/失败风险（HIGHEST VALUE）
    validity = core.get("validity_stat") or []
    if validity:
        vmap = {r.get("有效性"): int(r.get("数量", 0) or 0) for r in validity}
        denom = sum(vmap.get(k, 0) for k in ("有效", "失效", "审查中", "失败"))
        if denom:
            valid_n = vmap.get("有效", 0)
            invalid_n = vmap.get("失效", 0)
            failed_n = vmap.get("失败", 0)
            valid_rate = valid_n / denom * 100
            invalid_rate = invalid_n / denom * 100
            failed_rate = failed_n / denom * 100
            adverse_rate = invalid_rate + failed_rate
            risk = "偏高" if adverse_rate >= 40 else ("中等" if adverse_rate >= 20 else "较低")
            # Reuse the canonical metric display so KPI and insight never show
            # different rounding for the same effective-rate indicator.
            valid_display = metric_map.get("专利有效率") or f"{valid_rate:.1f}%"
            insights.append({
                "feature": "专利法律状态结构",
                "evidence": f"纳入有效率分母的状态中：有效 {valid_n} 件（占 {valid_display}）、失效 {invalid_n} 件（{invalid_rate:.0f}%）、审查中 {vmap.get('审查中', 0)} 件、失败 {failed_n} 件（{failed_rate:.0f}%）。",
                "interpretation": f"当前状态口径下有效占比 {valid_display}，失效与失败合计 {adverse_rate:.0f}%（水平{risk}）；两者含义不同，具体原因需逐件核查法律状态。",
            })
    apply_trend = core.get("apply_trend") or []
    if apply_trend:
        ta = _trend_analysis(apply_trend, as_of_year)
        if ta and not ta.get("insufficient"):
            extra = f"；{ta['partial_period']} 年数据截至生成时点，未纳入趋势比较" if ta.get("partial_period") else ""
            insights.append({
                "feature": "申请趋势研判",
                "evidence": f"峰值出现在“{ta['peak_period']}”（{ta['peak_value']:.0f} 件）；{ta['yoy'] or '近两个完整年度基本持平'}{extra}。",
                "interpretation": "趋势仅描述申请数量变化，不能单独证明研发投入或业务周期变化；近年申请量还可能受公开滞后影响。",
            })
    grant_trend = core.get("grant_trend") or []
    if grant_trend:
        ta = _trend_analysis(grant_trend, as_of_year)
        if ta and not ta.get("insufficient") and ta.get("direction") != "基本持平":
            extra = f"；{ta['partial_period']} 年数据截至生成时点，未纳入趋势比较" if ta.get("partial_period") else ""
            insights.append({
                "feature": "授权趋势研判",
                "evidence": f"授权峰值在“{ta['peak_period']}”（{ta['peak_value']:.0f} 件）；{ta['yoy'] or ta['direction']}{extra}。",
                "interpretation": "该趋势仅描述各报告期授权数量，不能单独证明研发质量、技术壁垒或申请转化速度。",
            })
    if not insights:
        insights.append({
            "feature": "数据完整性",
            "evidence": "部分维度未返回有效数据。",
            "interpretation": "建议核对匹配关键词是否为企业全称，或检查 MCP 连接与上游数据产品覆盖范围。",
        })
    return insights


def build_abstract(subject: Mapping[str, Any], core: Mapping[str, Any], metrics: Sequence[Mapping[str, Any]]) -> str:
    name = subject.get("enterprise") or subject.get("match_raw") or "目标企业"
    if subject.get("requires_confirmation"):
        return f"原始关键词“{subject.get('match_raw') or name}”返回多个候选主体，待人工确认；本报告未调用专利详情，也不形成主体专利结论。"
    if not metrics and not any(_has_evidence(v) for k, v in core.items() if k != "sections"):
        return f"本次未获得“{name}”的可用专利证据，核心维度覆盖率为 0；不得据此判断专利数量、类型或法律状态。"
    parts = [f"本报告以“{name}”为分析对象，基于专利公开数据，系统呈现企业专利统计、申请/授权趋势、类型分布、状态分布与专利检索明细。"]
    if metrics:
        kv = "、".join(f"{m['label']} {m['value']}" for m in metrics[:5])
        parts.append(f"关键指标包括：{kv}。")
    parts.append("报告同时对专利类型与申请、授权趋势作结构化观察；聚合数量不能替代逐件法律状态和权利要求核验。")
    return "".join(parts)


# --------------------------------------------------------------------------- #
# Dry-run sample
# --------------------------------------------------------------------------- #

def build_dry_run_payload(raw: str, keyword_type: str) -> Dict[str, Any]:
    try:
        sample = load_json_file(SAMPLE_PATH)
    except Exception:
        sample = {}
    sample = sample if isinstance(sample, dict) else {}
    subject = sample.get("subject") or {"enterprise": raw, "matchKeyword": raw, "keywordType": keyword_type, "match_raw": raw}
    subject = {**subject, "match_raw": raw, "keywordType": keyword_type}
    core = sample.get("core_analysis") or {}
    metrics = sample.get("metrics") or []
    return apply_content_review(_assemble(subject, core, metrics, dry_run=True))


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def _generated_year(value: Any) -> Optional[int]:
    text = _text(value)
    return int(text[:4]) if len(text) >= 4 and text[:4].isdigit() else None


def _content_quality_report(core: Mapping[str, Any]) -> Dict[str, Any]:
    """Section-fill rate from the final declared sections and actual bodies.

    A reported 0 is populated; only None/""/[]/{} count as empty. This is not
    an accuracy or evidence-quality score.
    """
    ca = core if isinstance(core, dict) else {}
    sections = [s for s in (ca.get("sections") or []) if isinstance(s, dict) and s.get("key")]

    def filled(value: Any) -> bool:
        return value is not None and value != "" and value != [] and value != {}

    if sections:
        total = len(sections)
        populated = sum(1 for spec in sections if filled(ca.get(spec["key"])))
    else:
        keys = [k for k in ca if k != "sections"]
        total = max(1, len(keys))
        populated = sum(1 for key in keys if filled(ca.get(key)))
    return {
        "total_sections": total,
        "populated_sections": populated,
        "empty_sections": total - populated,
        "coverage_pct": round(populated / max(1, total) * 100),
    }


def _assemble(subject: Mapping[str, Any], core: Mapping[str, Any], metrics: Sequence[Mapping[str, Any]], *, dry_run: bool) -> Dict[str, Any]:
    abstract = build_abstract(subject, core, metrics)
    records = build_records(core)
    generated_at = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    insights = build_insights(subject, core, metrics, _generated_year(generated_at))
    quality_report = _content_quality_report(core)
    if quality_report["populated_sections"] == 0:
        import sys
        print("⚠️ 质量门禁警告: 所有核心分析维度均无数据", file=sys.stderr)
    title = f"{subject.get('enterprise') or '目标企业'} 专利大数据报告"
    report = {
        "report_type": REPORT_TYPE,
        "title": title,
        "banner": REPORT_BANNER,
        "subject": dict(subject),
        "abstract": abstract,
        "summary": abstract,
        "executive_summary": [item["interpretation"] for item in insights][:5] or [abstract[:120]],
        "metrics": list(metrics),
        "caliber": build_caliber(subject),
        "core_analysis": dict(core),
        "representative_records": records,
        "insights": insights,
        "data_source": {
            "mcp_server": "patent-mcp-server",
            "products": requested_products(subject),
            "generated_at": generated_at,
            "dry_run": dry_run,
            "quality_report": quality_report,
        },
    }
    return report


def apply_content_review(report: Mapping[str, Any]) -> Dict[str, Any]:
    """Rebuild patent narrative content from an existing report payload.

    Pure/in-memory: uses only fields already present in the payload (core analysis,
    metrics, subject, generated_at). It does not call the MCP and does not
    invent evidence. It (1) drops sample-scoped metrics that were mislabeled as
    enterprise-wide, (2) rebuilds trend/status insights with partial-year and
    low-volume guards, and (3) refreshes abstract/summary/executive_summary so
    they match the corrected insights.
    """
    out = copy.deepcopy(dict(report))
    metrics = [m for m in (out.get("metrics") or []) if isinstance(m, dict) and m.get("label") not in SAMPLE_METRIC_LABELS]
    out["metrics"] = metrics
    subject = out.get("subject") or {}
    core = out.get("core_analysis") or {}
    source = out.get("data_source") or {}
    insights = build_insights(subject, core, metrics, _generated_year(source.get("generated_at")))
    out["insights"] = insights
    out["executive_summary"] = [item["interpretation"] for item in insights][:5] or [build_abstract(subject, core, metrics)[:120]]
    abstract = build_abstract(subject, core, metrics)
    out["abstract"] = abstract
    out["summary"] = abstract
    out.setdefault("data_source", {})["quality_report"] = _content_quality_report(core)
    return out


@paid_call_scope
def build_payload(raw: str, keyword_type: str, patent_type: Optional[str], page_size: int, max_pages: Optional[int] = 1) -> Dict[str, Any]:
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValueError("max_pages 必须为正整数")
    resolved = resolve_enterprise_name(raw)
    if (resolved.get("fuzzy_total") or 0) > 1:
        subject = build_subject(raw, resolved, keyword_type)
        subject["patentType"] = patent_type
        core = build_core_analysis({}, {}, raw, patent_type)
        return _assemble(subject, core, [], dry_run=False)
    enterprise = resolved["enterprise"]
    mk_args: Dict[str, Any] = {"matchKeyword": enterprise, "keywordType": keyword_type}
    search_args: Dict[str, Any] = {"matchKeyword": enterprise, "keywordType": "申请人", "pageIndex": 1, "pageSize": page_size}
    if patent_type:
        search_args["patentType"] = patent_type
    stats = _safe_call(T_STATS, mk_args)
    search = _collect_detail_pages(T_SEARCH, search_args, max_pages)
    search_total = _safe_total(search) if isinstance(search, dict) else None

    subject = build_subject(raw, resolved, keyword_type)
    subject["patentType"] = patent_type
    core = build_core_analysis(search, stats, enterprise, patent_type)
    metrics = build_metrics(stats if isinstance(stats, dict) else {}, search_total, core)
    # --- Enterprise profile enrichment (from fuzzy_search) ---
    _enrich_metrics_with_profile(metrics, resolved.get("record") if isinstance(resolved, dict) else None)
    report = apply_content_review(_assemble(subject, core, metrics, dry_run=False))
    return attach_pagination(report, {"专利检索明细": search})


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Compose a patent big-data report via the patent MCP.")
    parser.add_argument("--enterprise", required=True, help="企业全称或关键词（关键词将自动模糊补全）")
    parser.add_argument("--keyword-type", default="name", help="主体类型：name/nameId/regNumber/socialCreditCode")
    parser.add_argument("--patent-type", default=None, help="可选专利类型过滤（发明申请 / 实用新型 / 发明授权 / 外观设计）")
    parser.add_argument("--page-size", type=int, default=10, help="专利检索明细分页大小（最多 50）")
    parser.add_argument("--max-pages", type=int, default=1, help="专利检索明细最多查询页数（正整数，默认 1 页；按报告结论需要先计划预算再显式扩大）")
    parser.add_argument("--dry-run", action="store_true", help="不调用真实 MCP，使用样例数据组装报告骨架")
    parser.add_argument("--output", help="输出 JSON 路径；省略则打印到 stdout")
    parser.add_argument("--report-output", help="同时输出 HTML 报告（.html）与 Markdown 报告（.md）")
    parser.add_argument("--pdf-output", help="额外输出 PDF 报告（.pdf）；需要 Playwright + Chromium")
    args = parser.parse_args()

    if args.max_pages < 1:
        parser.error("--max-pages 必须为正整数")
    if args.dry_run:
        payload = build_dry_run_payload(args.enterprise, args.keyword_type)
    else:
        payload = build_payload(args.enterprise, args.keyword_type, args.patent_type, args.page_size, args.max_pages)

    if args.output:
        out = pathlib.Path(args.output).expanduser()
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json_dumps(payload, pretty=True), encoding="utf-8")
        print_json({"ok": True, "json": str(out), "dry_run": args.dry_run})
    else:
        print_json(payload)

    if args.report_output:
        base_out = pathlib.Path(args.report_output).expanduser()
        base_out.parent.mkdir(parents=True, exist_ok=True)
        html_path = base_out.with_suffix(".html") if base_out.suffix.lower() not in (".html", ".htm") else base_out
        md_path = html_path.with_suffix(".md")
        html_path.write_text(render_html(payload), encoding="utf-8")
        md_path.write_text(render_markdown(payload), encoding="utf-8")
        if args.pdf_output:
            pdf_path = pathlib.Path(args.pdf_output).expanduser()
            pdf_path.parent.mkdir(parents=True, exist_ok=True)
            html_to_pdf(render_html(payload), str(pdf_path))
        print_json({"ok": True, "html": str(html_path), "markdown": str(md_path), "pdf": str(pdf_path) if args.pdf_output else None, "dry_run": args.dry_run})


if __name__ == "__main__":
    main()
