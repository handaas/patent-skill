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
import datetime as dt
import json
import pathlib
import sys
from typing import Any, Dict, List, Mapping, Optional

from common import REPORT_BANNER, REPORT_TYPE, json_dumps, load_json_file, print_json
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


def _is_api_error(value: Any) -> bool:
    """Detect MCP API error responses (not empty data, but actual failures like 405)."""
    if value is None:
        return False
    if isinstance(value, str):
        return any(s in value for s in ("接口调用失败", "查询失败", "状态码：4", "状态码：5"))
    if isinstance(value, dict):
        for v in value.values():
            if isinstance(v, str) and any(s in v for s in ("接口调用失败", "查询失败", "状态码：4", "状态码：5")):
                return True
    return False

def _first_list(value: Any) -> List[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        if _is_api_error(value):
            return []
        for key in ("resultList", "list", "items", "data"):
            if isinstance(value.get(key), list):
                return value[key]
    if value in (None, "", {}):
        return []
    return [value]


def _first_record(value: Any) -> Dict[str, Any]:
    for record in _first_list(value):
        if isinstance(record, dict):
            return record
    if isinstance(value, dict):
        return value
    return {}


def _text(value: Any, limit: int = 0) -> str:
    if value is None:
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
        result = mcp_client.call_tool(tool, arguments)
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
    name = str(record.get("name") or "").strip()
    if name:
        return {"keyword": raw, "enterprise": name, "resolved": True, "reason": "由关键词模糊查询补全", "fuzzy_total": _int(_safe_total(fuzzy)), "record": record}
    return {"keyword": raw, "enterprise": raw, "resolved": False, "reason": "模糊查询未命中企业全称，按关键词直查"}


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


def _derive_core_metrics(metrics: List[Dict[str, Any]], core: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Derive additional metrics from core analysis sections."""
    search = core.get("search_records", []) if isinstance(core, dict) else []
    stats = core.get("patent_stats", {}) if isinstance(core, dict) else {}
    if isinstance(search, list) and search:
        try:
            types = set(str(r.get("专利类型", "")) for r in search if r.get("专利类型"))
            if types:
                metrics.append({"label": "专利类型数", "value": str(len(types)), "hint": "不同专利类型数量"})
            applicants = set(str(r.get("申请人", "")) for r in search if r.get("申请人"))
            if applicants:
                metrics.append({"label": "申请人/发明人数", "value": str(len(applicants)), "hint": "相关专利申请人数量"})
        except Exception:
            pass
    return metrics


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
    }


def build_metrics(stats: Mapping[str, Any], search_total: Any, core: Mapping[str, Any] = None) -> List[Dict[str, Any]]:
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
    total_n = sum(n for n in (inv_app_n, inv_lic_n, util_n, design_n) if n is not None) or _int(s.get("patentCount"))

    metrics.append({"label": "专利总数", "value": str(total_n) if total_n else "-", "hint": "企业专利总量"})
    metrics.append({"label": "发明申请", "value": str(inv_app_n) if inv_app_n is not None else "-", "hint": "发明申请专利数量"})
    if inv_app_n and inv_lic_n is not None:
        rate = inv_lic_n / inv_app_n * 100 if inv_app_n else 0
        metrics.append({"label": "发明授权", "value": str(inv_lic_n) if inv_lic_n is not None else "-", "hint": "发明授权专利数量", "delta": f"授权率 {rate:.0f}%"})
    else:
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
            metrics.append({"label": "专利有效率", "value": f"{valid_rate:.1f}%", "hint": "有效专利 / 已审专利（不含程序性状态）", "delta": "有效" if valid_rate >= 50 else None})
    metrics.append({"label": "检索结果", "value": (_text(search_total) if search_total is not None else "-"), "hint": "本次检索命中专利条数"})
    return [m for m in metrics if m.get("value") not in ("", None, "-")]


def build_caliber(subject: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "match_target": subject.get("enterprise") or subject.get("match_raw"),
        "match_type": f"专利统计按企业主体匹配（keywordType={subject.get('keywordType', 'name')}）；检索支持专利名称/申请号/申请人/代理机构",
        "data_scope": "专利统计、专利检索明细、专利申请/授权趋势、专利类型分布、专利状态分布",
        "products": ["专利检索", "专利统计"],
        "limit": "数据来自专利公开数据库；少量字段可能存在更新延迟。",
    }


def _stat_rows(stats: Mapping[str, Any], key: str, label_key: str, count_key: str = "count") -> List[Dict[str, Any]]:
    """Generic {label, count} extraction — kept for compatibility."""
    rows = []
    for item in _first_list(stats.get(key)):
        if not isinstance(item, dict):
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
        if not isinstance(item, dict):
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
        if not isinstance(item, dict):
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
        if not isinstance(item, dict):
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
        if not isinstance(item, dict):
            continue
        label = item.get("patentDivAppLegal") or "-"
        n = _int(item.get("patentCount"))
        if n is not None:
            rows.append({"名称/类型/状态/年份": _text(label), "数量": str(n)})
    return rows


def build_core_analysis(search: Any, stats: Mapping[str, Any]) -> Dict[str, Any]:
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
        if not isinstance(item, dict):
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
        {"key": "search_records", "title": "专利检索明细", "kind": "table", "note": f"本次检索命中 {total if total is not None else '若干'} 条，展示前 {len(search_rows)} 条",
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


def _trend_analysis(rows: List[Mapping[str, Any]]) -> Dict[str, Any]:
    """Compute trend direction, peak, and YoY change from a {period,count} series.

    Supports both the legacy single-series rows ({名称/类型/状态/年份, 数量}) and
    the multi-series rows ({年份, 发明申请, 发明授权, 实用新型, 外观设计}) produced
    by _trend_multi_rows — in the latter case the 4 type columns are summed.
    """
    nums = []
    periods = []
    for r in rows:
        if "数量" in r:
            try:
                nums.append(float(str(r.get("数量", 0)).replace(",", "")))
            except (TypeError, ValueError):
                nums.append(0.0)
            periods.append(r.get("名称/类型/状态/年份", "-"))
        else:
            # multi-series: sum the 4 type columns
            total = 0.0
            for _, label in _TREND_TYPE_KEYS:
                try:
                    total += float(str(r.get(label, 0)).replace(",", ""))
                except (TypeError, ValueError):
                    pass
            nums.append(total)
            periods.append(r.get("年份", "-"))
    if not nums:
        return {}
    peak_idx = max(range(len(nums)), key=lambda i: nums[i])
    direction = "持平"
    yoy = ""
    if len(nums) >= 2:
        last, prev = nums[-1], nums[-2]
        if prev > 0:
            pct = (last - prev) / prev * 100
            if pct > 5:
                direction = f"上升 {pct:.0f}%"
            elif pct < -5:
                direction = f"下降 {abs(pct):.0f}%"
            yoy = f"同比 {pct:+.0f}%"
    return {"peak_period": periods[peak_idx], "peak_value": nums[peak_idx], "direction": direction, "yoy": yoy, "last": nums[-1]}


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


def build_insights(subject: Mapping[str, Any], core: Mapping[str, Any], metrics: List[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    insights: List[Dict[str, Any]] = []
    metric_map = {m["label"]: str(m["value"]) for m in metrics}
    total = metric_map.get("专利总数")
    inv_app = metric_map.get("发明申请")
    inv_lic = metric_map.get("发明授权")

    if total:
        insights.append({
            "feature": "专利资产规模",
            "evidence": f"企业专利总数 {total} 件。",
            "interpretation": "专利总量反映企业技术储备广度；结合发明/实用新型/外观设计的比例可评估研发深度。",
        })
    if inv_app or inv_lic:
        parts = []
        if inv_app:
            parts.append(f"发明申请 {inv_app}")
        if inv_lic:
            parts.append(f"发明授权 {inv_lic}")
        try:
            app_n = float(inv_app) if inv_app else 0
            lic_n = float(inv_lic) if inv_lic else 0
            grant_rate = (lic_n / app_n * 100) if app_n else 0
            rate_clause = f"，授权率约 {grant_rate:.0f}%" if app_n else ""
        except (TypeError, ValueError):
            rate_clause = ""
        insights.append({
            "feature": "发明型专利含金量",
            "evidence": "、".join(parts) + "。"
                + (f"授权率反映核心技术被专利局认可的程度，是创新实力的硬指标{rate_clause}。" if rate_clause else "发明型专利含金量较高，授权量反映核心技术被专利局认可的程度。"),
            "interpretation": "发明授权占比越高，企业底层技术壁垒越强；若以实用新型/外观设计为主，则偏向产品迭代与外形保护。",
        })
    type_stat = core.get("type_stat") or []
    if type_stat:
        conc = _concentration(type_stat, 3)
        if conc:
            insights.append({
                "feature": "专利类型集中度",
                "evidence": f"“{conc['top']}”占比约 {conc['top_share']:.0f}%，前 3 类合计 {conc['cr']:.0f}%（CR3）。",
                "interpretation": "类型集中度反映企业研发方向；CR3 越高说明技术布局越聚焦，反之则研发面较宽。",
            })
    status_stat = core.get("status_stat") or []
    if status_stat:
        conc = _concentration(status_stat, 2)
        if conc:
            insights.append({
                "feature": "专利有效性结构",
                "evidence": f"“{conc['top']}”为主流状态，占比约 {conc['top_share']:.0f}%。",
                "interpretation": "有效专利占比反映专利资产健康度；失效/驳回较多可能源于未缴年费或审查未通过，建议关注核心专利的维持。",
            })
    # 专利有效性分桶 + 有效率 + 失效风险（HIGHEST VALUE）
    validity = core.get("validity_stat") or []
    if validity:
        vmap = {r.get("有效性"): int(r.get("数量", 0) or 0) for r in validity}
        denom = sum(vmap.get(k, 0) for k in ("有效", "失效", "审查中", "失败"))
        if denom:
            valid_n = vmap.get("有效", 0)
            invalid_n = vmap.get("失效", 0)
            valid_rate = valid_n / denom * 100
            invalid_rate = invalid_n / denom * 100 if denom else 0
            risk = "偏高" if invalid_rate >= 40 else ("中等" if invalid_rate >= 20 else "较低")
            insights.append({
                "feature": "专利有效率与失效风险",
                "evidence": f"已审专利中 有效 {valid_n} 件（{valid_rate:.0f}%）、失效 {invalid_n} 件（{invalid_rate:.0f}%）、审查中 {vmap.get('审查中', 0)} 件、失败 {vmap.get('失败', 0)} 件。",
                "interpretation": f"有效率为专利资产质量的核心指标；当前有效率约 {valid_rate:.0f}%，失效风险{risk}。失效多源于未缴年费或主动放弃，建议核查核心专利的年费缴纳状态，避免关键技术保护失效。",
            })
    apply_trend = core.get("apply_trend") or []
    if apply_trend:
        ta = _trend_analysis(apply_trend)
        if ta:
            insights.append({
                "feature": "申请趋势研判",
                "evidence": f"峰值出现在“{ta['peak_period']}”（{ta['peak_value']:.0f} 件），近年趋势{ta['direction']}，{ta.get('yoy', '')}。",
                "interpretation": "趋势上升表明研发投入持续加码、新业务技术布局活跃；趋势下降可能意味着阶段性技术成熟或投入收缩，需结合业务周期判断。",
            })
    grant_trend = core.get("grant_trend") or []
    if grant_trend:
        ta = _trend_analysis(grant_trend)
        if ta and ta.get("direction") != "持平":
            insights.append({
                "feature": "授权趋势研判",
                "evidence": f"授权峰值在“{ta['peak_period']}”（{ta['peak_value']:.0f} 件），近年{ta['direction']}。",
                "interpretation": "授权趋势反映核心技术被认可的速度；授权量稳步增长通常意味着研发质量稳定、技术壁垒持续加固。",
            })
    if not insights:
        insights.append({
            "feature": "数据完整性",
            "evidence": "部分维度未返回有效数据。",
            "interpretation": "建议核对匹配关键词是否为企业全称，或检查 MCP 连接与上游数据产品覆盖范围。",
        })
    return insights


def build_abstract(subject: Mapping[str, Any], core: Mapping[str, Any], metrics: List[Mapping[str, Any]]) -> str:
    name = subject.get("enterprise") or subject.get("match_raw") or "目标企业"
    parts = [f"本报告以“{name}”为分析对象，基于专利公开数据，系统呈现企业专利统计、申请/授权趋势、类型分布、状态分布与专利检索明细。"]
    if metrics:
        kv = "、".join(f"{m['label']} {m['value']}" for m in metrics[:5])
        parts.append(f"关键指标包括：{kv}。")
    parts.append("报告同时给出专利类型集中度与近期申请活跃度的结构化解读，便于技术资产管理、竞争分析与研发决策参考。")
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
    return _assemble(subject, core, metrics, dry_run=True)


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #

def _assemble(subject: Mapping[str, Any], core: Mapping[str, Any], metrics: List[Mapping[str, Any]], *, dry_run: bool) -> Dict[str, Any]:
    abstract = build_abstract(subject, core, metrics)
    records = build_records(core)
    insights = build_insights(subject, core, metrics)
    # Quality gate: count populated core-analysis sections.
    ca = core if isinstance(core, dict) else {}
    secs = ca.get("sections", [])
    if secs:
        total_secs = len(secs)
        populated = sum(1 for s in secs if isinstance(s, dict) and ca.get(s.get("key")) not in (None, "", [], {}))
    else:
        total_secs = max(1, len([k for k in ca if k != "sections"]))
        populated = sum(1 for k in ca if k != "sections" and ca.get(k) not in (None, "", [], {}))
    quality_report = {
        "total_sections": total_secs,
        "populated_sections": populated,
        "empty_sections": total_secs - populated,
        "coverage_pct": round(populated / max(1, total_secs) * 100),
    }
    if populated == 0:
        import sys
        print("⚠️ 质量门禁警告: 所有核心分析维度均无数据", file=sys.stderr)
    title = f"{subject.get('enterprise') or '目标企业'} 专利大数据报告"
    return {
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
            "products": [
                {"name": "专利检索", "product_id": "66b338e274bf098447db7f37"},
                {"name": "专利统计", "product_id": "66d5b7df537c3f61d646c230"},
                {"name": "企业模糊查询", "product_id": "675cea1f0e009a9ea37edaa1"},
            ],
            "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            "dry_run": dry_run,
            "quality_report": quality_report,
        },
    }


def build_payload(raw: str, keyword_type: str, patent_type: Optional[str], page_size: int) -> Dict[str, Any]:
    resolved = resolve_enterprise_name(raw)
    enterprise = resolved["enterprise"]
    mk_args: Dict[str, Any] = {"matchKeyword": enterprise, "keywordType": keyword_type}
    search_args: Dict[str, Any] = {"matchKeyword": enterprise, "keywordType": keyword_type, "pageIndex": 1, "pageSize": page_size}
    if patent_type:
        search_args["patentType"] = patent_type
    stats = _safe_call(T_STATS, mk_args)
    search = _safe_call(T_SEARCH, search_args)
    search_total = _safe_total(search) if isinstance(search, dict) else None

    subject = build_subject(raw, resolved, keyword_type)
    core = build_core_analysis(search, stats)
    metrics = build_metrics(stats if isinstance(stats, dict) else {}, search_total, core)
    _derive_core_metrics(metrics, core if isinstance(core, dict) else {})
    # --- Enterprise profile enrichment (from fuzzy_search) ---
    _enrich_metrics_with_profile(metrics, resolved.get("record") if isinstance(resolved, dict) else None)
    return _assemble(subject, core, metrics, dry_run=False)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def main() -> None:
    parser = argparse.ArgumentParser(description="Compose a patent big-data report via the patent MCP.")
    parser.add_argument("--enterprise", required=True, help="企业全称或关键词（关键词将自动模糊补全）")
    parser.add_argument("--keyword-type", default="name", help="主体类型：name/nameId/regNumber/socialCreditCode")
    parser.add_argument("--patent-type", default=None, help="可选专利类型过滤（发明申请 / 实用新型 / 发明授权 / 外观设计）")
    parser.add_argument("--page-size", type=int, default=10, help="专利检索明细分页大小（最多 50）")
    parser.add_argument("--dry-run", action="store_true", help="不调用真实 MCP，使用样例数据组装报告骨架")
    parser.add_argument("--output", help="输出 JSON 路径；省略则打印到 stdout")
    parser.add_argument("--report-output", help="同时输出 HTML 报告（.html）与 Markdown 报告（.md）")
    parser.add_argument("--pdf-output", help="额外输出 PDF 报告（.pdf）；需要 Playwright + Chromium")
    args = parser.parse_args()

    if args.dry_run:
        payload = build_dry_run_payload(args.enterprise, args.keyword_type)
    else:
        payload = build_payload(args.enterprise, args.keyword_type, args.patent_type, args.page_size)

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
