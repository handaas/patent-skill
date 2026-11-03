# patent-skill：已验收示例

本目录只展示 **5 个完全通过的真实正向场景**（最低要求4例）。

每例均为真实、非空、非缓存调用；技术检查、独立业务检查和离线浏览器检查全部通过。不把空数据、报错处理或仅生成页面计作通过。历史验收记录保留在本地 test-records/；本目录的 HTML/Markdown 由原 JSON 重渲染，不代表重新查询或重新验收。

| 场景 | 真实使用需求 | 报告 |
|---|---|---|
| [企业专利总体口径清单](09-overall-portfolio-scope/README.md) | 整理华为技术有限公司的企业专利总体统计、申请/公开趋势、法律状态分布和前 10 条代表记录；累计申请与累计授权分别展示，不计算申请成功率。 | [HTML](09-overall-portfolio-scope/report.html) / [MD](09-overall-portfolio-scope/report.md) / [JSON](09-overall-portfolio-scope/report.json) |
| [发明申请筛选作用域核查](10-invention-application-scope/README.md) | 检索比亚迪股份有限公司前 10 条发明申请专利，核对筛选明细与企业整体统计的口径差异，并检查申请日期字段；不要计算授权成功率。 | [HTML](10-invention-application-scope/report.html) / [MD](10-invention-application-scope/report.md) / [JSON](10-invention-application-scope/report.json) |
| [发明授权类型与当前状态边界核查](12-granted-invention-status-boundary/README.md) | 抽取腾讯科技（深圳）有限公司前 6 条发明授权专利，核对名称、申请号、申请人、类型和返回的法律状态；明确发明授权类型不等于权利当前有效。 | [HTML](12-granted-invention-status-boundary/report.html) / [MD](12-granted-invention-status-boundary/report.md) / [JSON](12-granted-invention-status-boundary/report.json) |
| [消费电子外观专利抽样](05-design-patent/README.md) | 查看小米科技有限责任公司的外观设计专利样本和类型占比。 | [HTML](05-design-patent/report.html) / [MD](05-design-patent/report.md) / [JSON](05-design-patent/report.json) |
| [专利快速抽样报告](07-small-sample/README.md) | 对北京京东世纪贸易有限公司生成仅含前 5 条明细的专利快报。 | [HTML](07-small-sample/report.html) / [MD](07-small-sample/report.md) / [JSON](07-small-sample/report.json) |

- [逐例验收结果](results.json) · [业务复核](review.json) · [证据摘要](evidence-index.json)
- 完整测试记录仅存于本地 test-records/live-replaced-20261027/（仅本地审计用，不随本目录发布），不属于可展示示例。
- 当前数据与结论限于原查询时点及公开样本；覆盖率不代表准确率或低风险。
