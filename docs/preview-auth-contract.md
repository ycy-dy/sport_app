# 隔离预览配对认证（非正式业务账号）

仅明确配置的回环origin（http://127.0.0.1:已确认端口 或 localhost）可装配。Host必须精确匹配，Origin对所有写请求严格同源；任何Forwarded/X-Forwarded-*拒绝，不按代理头判身份。尚未确认端口，不启动。

本机受限签发入口sports_app.pair_issuer.issue_interactive(auth)要求stdin/stdout/stderr及/dev/tty均为真实终端，显式选择预登记preview身份并输入YES；只写/dev/tty，不写stdout/stderr/文件/日志，不由Bot工具调用。底层issue(actor_id)为本机API，不提供HTTP注册/签发/角色选择。配对码256bit随机、120秒寿命、原子单次消费。挑战最多5次失败、5分钟失效；同提交码摘要跨挑战累计5次失败后冷却300秒，恰等端点恢复。不同假码不计入actor或全库，不影响无关合法码。更换随机猜测仍可绕过该计数，不是完整限速/抗DoS措施；不声称存在每码错误猜测计数。

认证目录0700、认证数据库及SQLite辅助文件0600、当前用户所有；拒绝软链接/硬链接/组或他人可写祖先。新SQLite库以O_EXCL/O_NOFOLLOW及0600创建；已有不明路径不自动chmod。仅允许项目内隔离目录，同UID恶意进程及终端滚屏不在此机制保护范围。

最小匿名入口：GET /auth/login（自包含HTML，无匿名静态资源），POST /auth/pair（{code}，服务端挑战Cookie+同源Origin）；其余含静态保持认证。登录页仅回环、no-store、禁iframe；不接受URL码或HTTP角色。认证失败固定401/429，不回显输入。

Cookie为独立预览随机会话ID，HttpOnly/SameSite=Strict/Path=/，HTTPS启Secure；回环HTTP不宣称TLS保护。服务器仅保存ID摘要，绝对30分钟/空闲10分钟，CSRF另随机，逐请求验证；重新配对成功撤销当前旧会话并轮换ID/CSRF，退出POST /auth/logout须当前会话CSRF，撤销并清Cookie。过期/撤销401，合法角色越权403。cookie/session与真实账号库不共享。

服务端与签发器共用私有instance.json，字段固定instance_id/origin/database=app.sqlite3/actors，与库内不可变摘要绑定；服务初始化显式建立，重接使用attach_instance(config)。用户真实终端命令：`PYTHONPATH=src <隔离Python> -m sports_app.pair_issuer --config <已批准instance.json绝对路径>`。CLI只打开既有库(mode=rw)，不建表/迁移，无角色、origin、库位置覆盖参数；错误仅TTY固定安全信息及退出码。当前端口/配置未确认，不执行该命令；Bot不得代执行。实例初始化数据库与描述文件尚非跨资源原子提交，写描述失败会留下无法加载的绑定实例，应废弃该隔离实例，不自动修补或签发。

配对认证只在新隔离预览装配显式注入；不接受请求头身份、固定admin或测试模式开关。合成测试可构造本地身份映射但不能HTTP提升权限。当前认证身份仅表示本机操作者授权浏览器，不证明真实业务身份、真实来源或真实AI；无执行器/发布链仍不可用。
