# sport_app

足球与篮球比赛分析、预测及推荐的内部研究网页 App。

计划栏目：比赛赛程、今日分析、今日推荐、推荐台账、复盘分析、分析体系、推荐体系。

## 当前状态

开发基线（WIP），不是内部预览版或正式发布。包含后端计算与存储原语、最小 HTTP 骨架、作者及独立测试；核心闭环与七栏目尚未验收，前端接线门禁仍关闭。

第六轮冻结回归由 Chief 执行：115 项通过、退出码 0，源码与测试运行前后哈希一致。详见 [执行记录](verification/core-run-06.txt) 与 [版本哈希](verification/core-run-06.json)。这些结果仅覆盖合成隔离测试，不证明来源真实性、真实 Codex 或真实推荐发布。

## 尚未实现／未验收

- 来源自动抓取与真实性验证、原文语义支持审核、批准规则注册表。
- 完整业务发布门禁及 HTTP 接线；当前分析任务和发布确认 API 明确返回 501，不表示排队或发布成功。
- 真实 Codex 手动任务执行、结算复盘闭环及七栏目 UI 集成。
- 条件票质量降级仅限服务器冻结的常规二串一条件状态；其他硬门禁仍须集成验收。
- SQLite 隔离测试仅覆盖同一数据库文件的 DELETE／WAL 模式，不推广到外部库或缓存。

## 测试复现

需要 Python 3.12+、Flask 3.x 和 pytest；请使用新项目独立环境及合成数据，不复用原项目环境。默认 pytest 配置只发现作者测试，完整回归必须显式包含独立目录：

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src .venv/bin/python -m pytest \
  -p no:cacheprovider tests/author tests/independent \
  --basetemp=tests/independent/.tmp-local-review -vv -ra
```

每次复现使用新建的项目内专属临时目录。该命令不启动服务、真实 Agent 或推荐发布。

## 边界

- 手动触发分析，真实推荐由管理员人工确认发布。
- 证据分不等于胜率；未验收的模型概率及 EV 保持 null。
- 不提供投注、支付或下单，不承诺命中率或收益。
- 不提交凭证、真实数据库、台账、日志或缓存。
