# MCP 工具参考 — patent-mcp-server

本 skill 连接的 MCP server：`handaas-mcp-server/patent-mcp-server`（“专利大数据”）。

> **重要**：专利统计类工具入参为 `matchKeyword`（**企业全称** / 注册号 / 统一社会信用代码 / 企业 id）+ `keywordType`；
> `patent_search` 的 `matchKeyword` 可为专利名称 / 申请号·公开号 / 申请人 / 代理机构；当用户只给企业关键词时，必须先调关键词模糊查询补全全称。

## 通用约定

- `keywordType` 枚举：`name`（企业名称）/ `nameId`（企业 id）/ `regNumber`（注册号）/ `socialCreditCode`（统一社会信用代码）。
- 分页：`pageIndex` 从 1 开始；`pageSize` 单页最多 50。仅 `patent_bigdata_patent_search`（专利检索明细）支持分页，本 skill 按显式页数预算采集（CLI `--max-pages`，默认 1 页；首屏同时作报告数据与 total 发现，不重复请求）；统计（stats）为聚合接口，关键词模糊查询仅用于主体确认，均单次调用、不分页遍历。
- `patentType` 枚举：`发明申请` / `实用新型` / `发明授权` / `外观设计`。

---

## 工具清单

### 1. `patent_bigdata_fuzzy_search` — 关键词模糊查询企业

用途：根据企业名称 / 人名 / 品牌 / 产品 / 岗位等关键词模糊查询企业列表，用于补全企业全称。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `matchKeyword` | string | 是 | 匹配关键词 |
| `pageIndex` | int | 否 | 分页开始位置（默认 1） |
| `pageSize` | int | 否 | 单页最多 50 |

返回：`total` + 企业列表（`name`、`nameId`、`regCapitalValue`、`foundTime`、`operStatus`、`address`、`legalRepresentative`、`enterpriseType`、`catchReason` 命中原因等）。

product_id：`675cea1f0e009a9ea37edaa1`。

---

### 2. `patent_bigdata_patent_search` — 专利检索

用途：按专利名称 / 申请号 / 公开号 / 申请人 / 代理机构查询专利信息，可按专利类型过滤。适合专利布局查询、技术情报分析、侵权规避。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `matchKeyword` | string | 是 | 专利名称 / 申请号 / 公开号 / 申请人 / 代理机构 |
| `keywordType` | string | 否 | 搜索方式：专利名称 / 申请号·公开号 / 申请人 / 代理机构（默认匹配全部） |
| `patentType` | string | 否 | 专利类型：发明申请 / 实用新型 / 发明授权 / 外观设计 |
| `pageIndex` | int | 否 | 从 1 开始 |
| `pageSize` | int | 否 | 单页最多 50（默认 10） |

返回（list + `total`）：`patentName`（专利名称）、`patentApplicationNum`（申请号）、`patentPubNum`（公告号）、`patentApplicantName`（申请人）、`patentType`（专利类型）、`patentApplicationDate`（申请日期）、`patentPubDate`（公告日期）、`calPatentLegalStatus`（法律状态）、`patentAgency`（代理机构）、`patentIPC`（IPC 分类号）等。

product_id：`66b338e274bf098447db7f37`。

---

### 3. `patent_bigdata_patent_stats` — 专利统计

用途：按企业主体返回专利总数、各类型数量、专利申请/授权趋势、专利类型分布、状态分布。

| 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `matchKeyword` | string | 是 | 企业名称 / 注册号 / 统一社会信用代码 / 企业 id（无全称则先调 fuzzy_search） |
| `keywordType` | string | 否 | 主体类型：name / nameId / regNumber / socialCreditCode |

返回：`patentCount`（专利数量）、`inventionAppPatentCount`（发明申请）、`inventionLicPatentCount`（发明授权）、`utilityModelPatentCount`（实用新型）、`appearanceDesignPatentCount`（外观设计）、`patentTypeAppTimeStat`（申请趋势 list of {year,...}）、`patentTypePubTimeStat`（授权趋势 list of {year,...}）、`patentDivAppLegalStat`（状态分布 list of {patentDivAppLegal,count}）、`patentTypeStat`（按年份分组的类型分布 dict）。

product_id：`66d5b7df537c3f61d646c230`。

---

## 推荐调用顺序（报告编排）

1. （若仅有关键词）`patent_bigdata_fuzzy_search` → 取 `name` 作为全称。
2. `patent_bigdata_patent_stats` → 统计指标 + 申请/授权趋势 + 类型分布 + 状态分布。
3. `patent_bigdata_patent_search` → 检索明细（按需可加 `patentType` 过滤）。

> 单次报告通常调用 2-3 个工具；stats / search 入参均为企业主体 `matchKeyword` + `keywordType`。
