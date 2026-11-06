# 报告输出 — 专利大数据报告

本文件规定 `patent-report` skill 产出的报告结构、质量底线与渲染工作流。所有产物遵循 `AGENTS.md` 的统一 JSON 骨架与本 skill 的领域裁剪。

## 默认展示模式

- HTML：可分享 / 可交付的可视化报告；独立本地文件，内嵌 CSS，无调试 / 内部段落。
- Markdown：知识库 / wiki / PRD / 后续手工编辑。
- JSON：系统集成或二次处理。

`compose_report.py` 通过 `--report-output <path>` 同时产出 HTML + Markdown；`--output <path>` 产出 JSON。`render_report.py` 可基于已有 JSON 重渲染。

## 报告结构（7 章）

1. **报告摘要**：分析对象、数据覆盖范围、核心发现、关键指标卡。
2. **查询对象与口径**：企业全称、匹配方式、数据范围、产品、局限。
3. **数据总览**：专利总数、发明申请、发明授权、实用新型、外观设计、检索结果数等指标卡。
   - 发明申请与发明授权是各自累计存量，不对应同一申请队列。报告只展示原始计数，不计算申请成功率、授权转化率或授权/申请存量比。
4. **核心分析**（专利专属子章节，由 `core_analysis.sections` 驱动渲染）：
   - 专利统计（KV：总数 / 发明申请 / 发明授权 / 实用新型 / 外观设计）。
   - 专利申请授权趋势（表：年份 / 数量）。
   - 专利授权趋势（表：年份 / 数量）。
   - 专利类型分布（表：类型 / 数量）。
   - 专利状态分布（表：法律状态 / 数量）。
   - 专利检索明细（表：专利名称 / 申请号 / 申请人 / 专利类型 / 申请日期 / 法律状态 / 代理机构）。
5. **代表性记录**：关键专利记录 Top N（名称 / 类型 / 申请日期 / 法律状态）。
6. **特征与洞察**：结构化解读（资产规模 / 发明占比 / 类型集中度 / 申请活跃度），每条含 `feature` / `evidence` / `interpretation`。
7. **数据口径与来源**：MCP server、数据产品、生成时间、是否 dry-run。

## 质量底线

- 企业整体统计与专利检索明细是不同口径；使用 `--patent-type` 时必须在口径和明细章节注明“申请人 + 专利类型”筛选，不把整体总量解释为筛选总量。

- 报告脱离 Skill 上下文也可独立阅读；正文只见专利事实与结构化数据。
- 绝不出现工具名、入参（如 `matchKeyword=...`）、product_id、内部字段名、空表、调试信息。
- HTML 采用研究报告视觉风格：A4 风、灰色顶部条纹、蓝色报告横幅、左侧目录 / 范围侧栏、深蓝章节标题、深蓝表头、浅蓝斑马行、打印友好分页。
- 数据为空时明确说明数据范围 / 口径，不渲染空表、不臆造事实。
- 绝不打印 `secret_id` / `secret_key` / 签名 / token / 原始签名请求。
- 自动补全企业全称时，在报告口径中说明“由关键词模糊查询补全”。

### 数据格式约束（铁律）

以下约束适用于 compose_report.py 组装数据与 render_report.py 渲染输出的全过程：

1. **嵌套 JSON 字符串必须解析**：MCP 返回的某些字段（如 `regCapital`、`addressValue`、`subscriptionDetail`）可能是 JSON 字符串（例：`{"coinType":"人民币","value":430000000.0}`）。compose 层必须调用 `_unwrap_json_str()` / `_parse_reg_capital()` / `_flatten_addr()` 解析为可读文本（如"4.30 亿 人民币"、"浙江省杭州市滨江区..."）。绝不在报告正文、表格单元格或指标值中输出原始 JSON 字符串。

2. **section 标题必须用中文**：`core_analysis.sections` 数组中每个 section 的 `title` 字段必须使用中文（如"企业基本信息"、"对外投资"、"股东信息"）。`key` 字段用英文 snake_case 供程序索引，但 `title` 绝不可显示英文 key。即使缺少 sections 数组，渲染器回退逻辑也内置了 `_TITLE_MAP` 映射。

3. **指标值可读化**：所有 `metrics` 的 `value` 字段必须格式化为人类可读形式：
   - 金额：`10995210218.0` → `109.95 亿 人民币`（≥1 亿用亿，≥1 万用万）
   - 地址：嵌套 dict → 省+市+区拼接 或取 `value` 字段
   - 比率：`0.8858` → `88.58%`
   - 日期：保持 `yyyy-MM-dd` 格式
   - "-" 表示字段缺失（MCP 未返回）；`0` 表示真实为零

4. **企业画像指标提取**：有 fuzzy_search 的 skill 必须从返回的 record 中提取 `regCapitalValue` / `foundTime` / `operStatus` / `enterpriseType` / `legalRepresentative`，通过 `_enrich_metrics_with_profile()` 追加为指标卡。

5. **分布派生指标**：专利的专利类型数、申请人数等不得从检索明细当前页计算后冒充企业全量；compose 不再生成这类样本指标。

## 内容审校清单（交付前逐项核对）

1. `source` / `raw` 原值与报告标签并存；修订只写 `output/content-quality-review/`。
2. 申请、授权、有效、失效、失败五个口径分开；每次出现百分比，写明分母。
3. 当前年度不参与趋势同比；近两年任一 <10 件时不写“上升/下降”；申请趋势须带公开滞后说明。
4. 类型集中度看统计口径，不看检索明细页；`其他` 不得当作单一类型参与 CR3。
5. 洞察结构为「证据（数字/年份/分母）→ 限制」；不得由聚合量推断技术实力、研发投入、专利质量或商业价值。
6. 标题、机构名、法律状态保留上游原文，不在正文做同义改写；需归并时仅归并可证明的别名，并保留原值。

## 工作流

```bash
# 1. 干跑（不调真实 API，用样例数据组装报告骨架）
python scripts/compose_report.py \
  --enterprise "示例科技有限公司" \
  --dry-run \
  --output output/patent.json \
  --report-output output/patent.html

# 2. 真实查询 + 渲染（需 MCP 连接就绪）
python scripts/compose_report.py \
  --enterprise "示例科技有限公司" \
  --output output/patent.json \
  --report-output output/patent.html

# 3. 按专利类型过滤
python scripts/compose_report.py --enterprise "示例科技有限公司" --patent-type 发明申请 --report-output output/patent_invention.html

# 4. 重渲染已有 JSON
python scripts/render_report.py --input output/patent.json --output output/patent.html
python scripts/render_report.py --input output/patent.json --output output/patent.md
```

返回：JSON 路径、HTML 路径、Markdown 路径，以及企业全称映射与数据口径摘要。
