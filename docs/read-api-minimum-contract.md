# 七栏目只读 API 最低契约

> 文档状态：第 3 项最低展示契约草案，供 API 与独立验收使用。本文只定义公开投影，不授权真实来源接入、真实推荐发布或前端放行。
>
> 统一约束：`model_probability` 与 `EV` 固定为 `null`；证据分不是胜率。缺失数据不得编造，使用快照状态或字段级 `null`/`unknown`/`pending`。不得返回凭证、源 URL、原始 Agent 响应、Claim、内部日志、真实数据库路径或未发布草稿。

## 1. 统一响应与状态

所有读取接口使用：

```json
{
  "data": [],
  "meta": {
    "status": "ready",
    "snapshot_id": "snap-2026-11-14-001",
    "as_of": "2026-11-14T08:00:00+08:00",
    "total": 1,
    "limit": 20,
    "offset": 0,
    "schema_version": "read-v1",
    "model_probability": null,
    "EV": null
  }
}
```

快照状态：`not_configured`（无公开快照，不等于无赛事）、`empty`（已成功取得且确实无结果）、`failed`（取得/生成失败）、`stale`（超过 `valid_until`）、`ready`（有效且有数据）。错误对象固定为：

```json
{"category":"HARD_BLOCK","code":"INVALID_FILTER","message":"只读视图请求未完成","retryable":false,"field":"business_day","stage":"projection","request_id":"req-001"}
```

筛选同时提供 `sport` 与 `business_day` 时，必须在同一 `match_id`/同一比赛身份上同时满足，禁止由不同腿分别以 `any()` 命中。分页参数为 `limit`（1–100）与 `offset`（非负整数）。`admin`、`reviewer` 可查看获准公开投影；`viewer` 只能查看已发布/冻结及获准公开内容。所有角色均不得看到草稿或内部敏感材料。

## 2. 比赛赛程：`GET /api/schedules`

|字段|类型|来源|缺失状态|可见性|
|---|---|---|---|---|
|`match_id`/`event_id`|string|获准赛程快照|身份不完整为`pending`|三角色|
|`sport`|`football\|basketball`|快照|不得推断|三角色|
|`business_day`|ISO date|快照|`pending`|三角色|
|`timezone`|string|快照|`null`|三角色|
|`kickoff`|RFC3339|快照|`null`/`pending`|三角色|
|`league`|string|快照|`null`|三角色|
|`home_team`/`away_team`|string|快照身份|任一缺失则`pending`|三角色|
|`period`|enum|业务快照|`null`|三角色|
|`sale_status`|enum|市场快照|`unknown`|三角色|
|`sales_deadline`|RFC3339/null|销售快照|`null`|三角色|
|`exclusion_reasons`|string[]|规则投影|无排除为`[]`|三角色|
|`source_snapshot_at`|RFC3339/null|快照元数据|`null`|三角色|

合成 JSON：
```json
{"match_id":"m-foot-001","sport":"football","business_day":"2026-11-14","timezone":"Asia/Shanghai","kickoff":"2026-11-14T20:00:00+08:00","league":"Synthetic League","home_team":"Home A","away_team":"Away B","period":"middle","sale_status":"on_sale","sales_deadline":"2026-11-14T19:45:00+08:00","exclusion_reasons":[],"source_snapshot_at":"2026-11-14T08:00:00+08:00"}
```

**第3项必需**：安全投影、身份/时间/状态字段（已有来源才返回）、分页、联合身份筛选、快照状态、读取权限。**真实源待接**：官方赛程真实性、销售状态真实性、持续更新。

## 3. 今日分析：`GET /api/analyses`

|字段|类型|来源|缺失状态|可见性|
|---|---|---|---|---|
|`analysis_id`/`match_id`|string|批准分析快照|`null`/`pending`|admin/reviewer；viewer仅获准摘要|
|`status`|enum|任务/分析公开状态|`pending`/`failed`|按角色|
|`task_stage`|enum/null|任务投影|未接线为`null`|admin/reviewer；viewer摘要|
|`evidence_status`|enum|证据摘要|`unknown`|按授权|
|`evidence_score`|number/null|评分服务|未评分为`null`|按授权公开|
|`quality_gaps`|string[]|质量摘要|无缺口为`[]`|按授权公开|
|`evidence_as_of`|RFC3339/null|证据快照|`null`|按授权公开|
|`strongest_counterevidence`|string/null|研究摘要|`null`|按授权公开|
|`covered_paths`/`excluded_paths`/`failure_paths`|string[]|研究摘要|未研究为`[]`+`pending`|按授权公开|
|`withdraw_if`|string[]|撤销条件摘要|未定义为`[]`|按授权公开|
|`source_published_at`/`source_effective_at`|RFC3339/null|来源元数据|`null`|不含URL|
|`model_probability`/`EV`|null|未验收模型|固定`null`|三角色|

合成 JSON：
```json
{"analysis_id":"a-001","match_id":"m-foot-001","status":"completed","task_stage":"evidence_review","evidence_status":"partial","evidence_score":68,"quality_gaps":["阵容待核"],"evidence_as_of":"2026-11-14T09:00:00+08:00","strongest_counterevidence":"关键球员出场状态未确认","covered_paths":["常规赛果"],"excluded_paths":["临场突发"],"failure_paths":["阵容确认冲突"],"withdraw_if":["官方阵容与快照冲突"],"source_published_at":null,"source_effective_at":null,"model_probability":null,"EV":null}
```

**第3项必需**：公开摘要、状态轴、证据分/缺口（有数据才返回）、权限和脱敏。任务状态与分析阶段分别表达：`status` 为 `pending/failed/completed/cancelled`，`task_stage` 为 `collecting/researching/drafting/evidence_review` 或 `null`，只表示已登记研究阶段，不接受 `decision_validated` 作为阶段别名。最终决策验证另属服务 `decision_status=validated_server_execution`，不得混入此字段。`evidence_status` 固定为 `unknown/partial/qualified/conflict`；未接评分为 `unknown`，缺口或未达标为 `partial`，仓储评分 eligible 且无缺口才可登记 `qualified`，已记录硬冲突为 `conflict`。公开批准仅授权披露，不认证分数；投影只校验类型、枚举及 qualified 必需的分数≥70/无显式缺口，真实 eligible 出处仍须服务器登记保证。**真实源待接**：来源真实性、039运行接线、原文语义支持。

## 4. 今日推荐：`GET /api/recommendations`

|字段|类型|来源|缺失状态|可见性|
|---|---|---|---|---|
|`publication_id`|string|发布记录|无发布为`not_published`|viewer仅published/frozen|
|`recommendation_type`|string enum|决策/发布记录|`null`|按授权|
|`recommendation_status`|string enum|独立决策结果|不可成串由`no_pair_reason`表达|按授权|
|`period`|enum|发布槽位|`null`|按授权|
|`pass_types`|string[]|发布投影|`[]`|按授权|
|`legs`|object[]|不可变原票|结构不足为`insufficient_frozen_structure`|viewer脱敏|
|`reasons`|string[]/string|公开理由摘要|`[]`|按授权|
|`risk`|enum/string|公开风险摘要|`unknown`|按授权|
|`no_pair_reason`/`withdrawal_reason`|string/null|决策/发布状态|`null`|按授权|
|`publication_status`|enum|发布记录|`not_published`|按授权|
|`model_probability`/`EV`|null|未验收模型|固定`null`|三角色|

合成 JSON：
```json
{"publication_id":"pub-001","recommendation_type":"regular_pair","recommendation_status":"conditional_low_confidence","period":"middle","pass_types":["二串一"],"legs":[],"reasons":["存在可行组合但证据分未达70"],"risk":null,"no_pair_reason":null,"withdrawal_reason":null,"publication_status":"published","model_probability":null,"EV":null}
```

**第3项必需**：已发布/冻结安全投影、条件票状态保留、风险/理由、概率与EV为null。**第4项补齐**：复盘关联与复盘 HTTP；推荐字段仅按已有发布原票补齐，不新增推荐业务规则。**真实源待接**：官方盘口、来源真实性、真实推荐授权。

## 5. 推荐台账：`GET /api/ledger`

|字段|类型|来源|缺失状态|可见性|
|---|---|---|---|---|
|`publication_id`|string|发布记录|`null`|已发布/冻结|
|`frozen_at`|RFC3339/null|冻结记录|`null`|脱敏公开|
|`legs[].options[].original_odds`|decimal-string/null|每腿原票选项|选项结构不足为`null`|脱敏公开|
|`legs[].options[].line`|decimal-string/null|每腿原票选项|无盘口为`null`|脱敏公开|
|`replaces_publication_id`|string/null|替代链|`null`|脱敏公开|
|`selection_hash`|string|原票指纹|结构不足为`null`|脱敏公开|
|`publication_status`/`settlement_status`|enum/null|发布/结算|`pending`|按授权|
|`leg_status[]`|enum[]|逐腿结算摘要|逐腿缺结果为`pending`|按授权|
|`pair_status`|enum|整票结算摘要|确定一腿`miss`即`miss`；待核信息另列|按授权|

合成 JSON：
```json
{"publication_id":"pub-001","created_at":"2026-11-14T10:00:00+08:00","frozen_at":null,"replaces_publication_id":null,"selection_hash":"hash-synthetic-001","publication_status":"frozen","settlement_status":"pending","leg_status":["pending","pending"],"pair_status":"pending","legs":[{"leg_id":"leg-1","options":[{"option_id":"opt-1","original_odds":"1.50","line":null}]},{"leg_id":"leg-2","options":[{"option_id":"opt-2","original_odds":"1.60","line":null}]}]}
```

**第3项必需**：已发布/冻结可读、原票字段和替代链不变、结构不足明确标记。**第4项补齐**：复盘结果与统计 HTTP 接线。**真实源待接**：真实官方结算结果。

## 6. 复盘分析：`GET /api/reviews`、`GET /api/reviews/{review_id}`

|字段|类型|来源|缺失状态|可见性|
|---|---|---|---|---|
|`review_id`/`publication_id`|string|复盘任务/发布记录|`null`|按发布可见性|
|`leg_status`|enum[]|按腿结算服务|逐腿未知为`pending`|按授权|
|`pair_status`|enum|整票结算服务|确定一腿`miss`即`miss`；待核信息另列|按授权|
|`pending_reason`|string/null|复核摘要|`null`|按授权|
|`correction_revision`|integer|追加结算版本|初始`0`|按授权|
|`settlement_status`|enum|结算服务|`pending`|按授权|
|`statistics_denominator`|integer/null|统计服务|覆盖不足为`null`|脱敏公开|
|`leg_statistics_coverage`|enum|结构覆盖检查|`insufficient_frozen_structure`|脱敏公开|

合成 JSON：
```json
{"review_id":"review-001","publication_id":"pub-001","outcome":"miss","version":null,"leg_statistics_coverage":"complete"}
```

**第3项必需**：只读历史公开投影、覆盖不足标记、未结算状态。**第4项补齐**：复盘发起/意见、admin结算与纠错确认、追加历史版本及统计接口；reviewer可发起和提交意见但不可直接写结算，viewer只读。**真实源待接**：官方赛果真实性、篮球节分/OT核验。

## 7. 分析体系：`GET /api/systems/analysis`

|字段|类型|来源|缺失状态|可见性|
|---|---|---|---|---|
|`policy_revision`|string|获准公开版本登记|`null`/`not_configured`|三角色|
|`analysis_revision`|string|039公开版本登记|`null`/`not_configured`|三角色|
|`status`|enum|体系公开状态|`not_configured/not_integrated/partial/stale/ready`|三角色|
|`runtime_provider`|enum（固定`codex`）|运行覆盖登记|未登记为`null`|三角色|
|`coverage_status`|enum|运行覆盖登记|`not_integrated/partial`|三角色|
|`dimensions`|string[]|获准公开摘要|未登记为`[]`|三角色|
|`event_coverage`|string[]|获准公开事件摘要|未登记为`[]`|三角色|
|`validation_level`|enum|验证登记|`defined/untested/unvalidated`|三角色|
|`notices`|string[]|公开提示|无提示为`[]`|三角色|
|`model_probability`|null|未验收模型|固定`null`|三角色|
|`EV`|null|未验收模型|固定`null`|三角色|

来源为获准公开政策摘要和运行覆盖登记，不直接输出规则原文；三角色可见。

合成 JSON：
```json
{"policy_revision":"2026-11-09-required-pairs-01","analysis_revision":"2026-11-13-history-methods-01","status":"not_integrated","runtime_provider":"codex","coverage_status":"partial","dimensions":["身份","赛程","阵容"],"event_coverage":["公开摘要"],"validation_level":"defined","notices":["039运行接线尚未验收"],"model_probability":null,"EV":null}
```

**第3项必需**：版本、提供方、覆盖/验证等级和未验收提示的安全公开投影。**真实源待接**：获准政策公开摘要的正式登记与运行覆盖证据。

## 8. 推荐体系：`GET /api/systems/recommendation`

|字段|类型|来源|缺失状态|可见性|
|---|---|---|---|---|
|`policy_revision`|string|获准公开版本登记|`null`/`not_configured`|三角色|
|`analysis_revision`|string|039公开版本登记|`null`/`not_configured`|三角色|
|`status`|enum|体系公开状态|`not_configured/not_integrated/partial/stale/ready`|三角色|
|`recommendation_kinds`|enum[]|获准公开体系摘要|未登记为`[]`|三角色|
|`quality_gates`|string[]|公开门禁摘要|未登记为`[]`|三角色|
|`conditional_boundary`|string|公开条件票边界|未登记为`null`|三角色|
|`publication_gates`|string[]|公开发布门禁|未登记为`[]`|三角色|
|`prohibitions`|string[]|公开禁止事项|未登记为`[]`|三角色|
|`validation_level`|enum|验证登记|`defined/untested/unvalidated`|三角色|
|`notices`|string[]|公开提示|无提示为`[]`|三角色|
|`model_probability`|null|未验收模型|固定`null`|三角色|
|`EV`|null|未验收模型|固定`null`|三角色|

来源为获准公开推荐体系摘要；三角色可见，不输出内部规则原文或来源材料。

合成 JSON：
```json
{"policy_revision":"2026-11-09-required-pairs-01","analysis_revision":"2026-11-13-history-methods-01","status":"not_integrated","recommendation_kinds":["regular_pair"],"quality_gates":["evidence_score","identity","sale_status","full_branch_cost"],"conditional_boundary":"仅常规二串一；不改score/eligible","publication_gates":["reviewer_audit","admin_confirmation"],"prohibitions":["投注","支付","下单","收益承诺"],"validation_level":"defined","notices":["概率与EV未验收"],"model_probability":null,"EV":null}
```

**第3项必需**：公开体系摘要、版本、门禁、条件票边界和禁止事项。**真实源待接**：正式公开体系登记与运行覆盖证据。

## 9. Chief实施裁定（优先于上文示例与简写）

- 本节冻结第3项实施口径，不改变业务政策。足球20:00为middle（早段截至19:30，晚段自23:00起）；是否早中合并由现有分段服务决定，不能从单场示例推断。
- 沿用现有字段名：`recommendation_type`、`recommendation_status`，不新增`recommendation_kind`、`quality_status`或`original_quality_status`别名。发布生命周期沿用`status`；分析任务公开状态单独位于分析行，不与发布或结算状态混用。
- 保留现有`identity`对象及其稳定ID、sport、business_day等字段；展示名另列`home_team/away_team`为string或null，稳定ID不是展示名。缺失字符串、日期与数值为null，缺失数组为[]；另加`field_coverage`对象，按字段路径标记`available/unknown/not_integrated`，不得把缺失身份字符串写成pending。
- `reasons`固定string[]；`risk`为string或null，只取已批准公开摘要，不能由条件票自动推导high。研究摘要、路径、反证和体系文字必须来自显式批准的公开快照；内部原文存在不等于公开批准。
- 原票价格/盘口仅位于`legs[].options[].original_odds/line`；现有发布创建时间保留`created_at`，没有独立冻结时间时`frozen_at=null`。`replaces_publication_id`仅从可信审计替代记录读取，缺失为null，并遵循被引用记录的角色可见性，不能借关系泄漏隐藏历史。
- 第3项复盘维持现有`outcome`、`version`及覆盖标记；无结果为outcome=pending、version=null。逐场比分、逐腿/分支详情、纠错版本及统计完整接线留第4项，不创建虚构初始版本。结算结果只用hit/miss/pending，一腿miss与另一腿pending整票仍miss；待核原因独立表达。上文复盘示例的settlement_status=pending不覆盖已确定的整票miss。
- 错误对象保留现有category/code/message/request_id；补`retryable`为boolean、`field`为string或null、`stage`固定projection，不回显异常或原始输入。
- 第3项完成标准是全部最低字段可安全投影并有合成正例、未知值有明确覆盖状态；不要求不存在的真实源，不将合成公开快照说成真实运行证据。体系runtime_provider可声明规范codex，但coverage_status/validation_level必须真实表达未接线/未验收，不能据规范名称推断执行成功。

### 9.1 公开分析状态与内部服务映射（枚举修复交付）

- `ManualRuntime.created/running` → 公开分析 `status=pending`；`task_stage` 无已登记具体研究阶段时为 `null`，不按运行中猜成 researching。
- `ManualRuntime.completed` 仅表示适配器返回已记录；不能直接登记公开分析 `status=completed` 或 `task_stage=decision_validated`。研究结果通过独立内容验证并获公开批准后，才可登记 `status=completed`，具体研究阶段仍由登记提供。
- `ManualRuntime.failed/expired` → `status=failed`；`cancelled` → `status=cancelled`。无已接线任务为 `status=pending, task_stage=null`。
- 证据状态来自服务器评分/冲突摘要：未评分 unknown；存在质量缺口 partial；eligible且无缺口 qualified；已确认硬冲突 conflict。它不是任务阶段或发布状态，不能由公开快照批准自动生成。
- 当前第3项为显式批准快照投影，不自动读取内部任务。非法阶段/证据状态一律拒绝；其中 `decision_validated` 明确不属于本字段合法值，测试若以它作正向夹具应修夹具而非放宽枚举。

## 10. 实施边界

- **第3项本批必须实现**：安全投影、角色读取、快照状态、分页/过滤、同一比赛身份联合筛选、嵌套字段安全拒绝，以及已有数据可提供时的最低字段契约。
- **第4项补齐**：复盘发起/意见、admin 结算与纠错确认、追加历史版本与统计；不改变 reviewer 不能直接写结算的边界。
- **真实源待接**：官方赛程/盘口/赛果真实性、真实 Codex 执行、来源语义与覆盖全集、篮球节分/OT真实性、生产批准。
- 本文及合成 JSON 仅为契约证据，不代表接口已全部实现、真实 AI 已验证或允许真实推荐发布。
