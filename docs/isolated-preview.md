# 隔离预览装配与启动前置条件（未启动）

## 文件边界与同源上下文

后端负责GET /、GET /api/session、preview.py及作者测试。前端负责项目根目录templates/index.html、static/app.css、static/app.js。模板变量session_context及GET /api/session.data均为{actor_id:string,role:admin|reviewer|viewer,csrf_token:非空string}，来自服务器逐请求principal_loader，页面及API响应no-store。前端不缓存Token到持久存储、不从请求参数构造角色。

所有入口含静态文件继续认证；匿名、非Principal对象、未知角色、空/空白actor_id或空/空白服务端CSRF上下文均401，响应不包含角色/actor/CSRF。有效Principal访问无权操作为403；写请求缺失或错误X-CSRF为403。此区分由app.before_request对页面、静态及所有API统一执行，不能由子接口接受未知角色补兼容。没有登录/身份提供方时不提供默认admin，不允许信任X-Role/X-Actor等任意请求头作为认证。CSRF由已认证服务端会话提供；X-CSRF写请求必须与当前会话相符。真实认证适配器、会话签发/轮换与浏览器登录尚未接线，不把测试Principal当真实登录。

首批前端允许：同源GET七栏目、发布详情、复盘详情/历史/统计（按角色）。admin/reviewer可对已存在publication_id显式确认POST /api/reviews以及POST /api/reviews/{id}/comments；附X-CSRF与Idempotency-Key。意见GET仅追踪元数据。任务创建需受信registry_id/batch_id；发布需完整可信任务链；未有安全准备来源时显示未接入，不拼原票、不注入客户端评分。admin结算/纠错虽有服务API，首批无可信赛果适配与安全输入界面，不开放前端按钮。后端权限始终最终生效。

## 预览工厂

sports_app.preview.create_preview(preview_dir=绝对路径,principal_loader=已配置可信认证函数)。仅允许新项目.preview子树内的新/空目录，拒绝已有数据库目录。仅建立隔离空SQLite、读取视图及复盘服务；不载入真实库，不播种假数据，不配置执行器/发布链。默认任务/发布501，公开快照未配置如实显示。导入模块不建库、不启动网络/调度。

## 可复现启动方案（前置未满足，不执行）

1. Chief确认端口与访问范围；默认建议仅127.0.0.1，不绑定0.0.0.0，不改变防火墙。
2. 指定本机已批准可信认证工厂模块，模块必须提供逐请求认证，不允许临时固定身份绕过。通过create_preview构造app并导出为独立本地factory，使用项目隔离依赖环境。
3. 由Chief确认新空目录：/Users/ycy/ycy/sports-app/.preview/<本次预览ID>。不得复用真实data或测试工件。
4. 使用已确认的项目虚拟环境Python：`PYTHONPATH=src <独立环境Python绝对路径> -m flask --app <已批准认证工厂模块>:create_app run --host 127.0.0.1 --port <已确认端口> --no-debugger --no-reload`。此命令模板当前不能直接执行：认证模块、端口及访问授权未确定。
5. 先核对实际URL、登录与角色，再做浏览器验收；不能将进程启动等同功能通过。停止由启动者终止受管进程，不配置自启动。

当前缺口：可信浏览器认证及会话签发、确认端口/范围、前端文件落盘和浏览器独立验收。无真实执行器/来源/部署批准。后台默认空数据可预览安全空态，但不代表赛事功能真实接入。
