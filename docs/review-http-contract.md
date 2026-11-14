# 第4项复盘HTTP契约（本地合成验证，未授权真实源）

所有写入CSRF；只引用publication_id，不接受原腿、原线、原价或调用方结算结果。默认未注入复盘服务501。角色由服务器认证提供。

| 操作 | 接口 | admin | reviewer | viewer |
|---|---|---|---|---|
| 发起复盘 | POST /api/reviews {publication_id} | 是 | 是 | 否 |
| 追加意见 | POST /api/reviews/{publication_id}/comments {comment} | 是 | 是 | 否 |
| 意见分页读取（脱敏元数据） | GET /api/reviews/{publication_id}/comments | 是 | 是 | 否 |
| 首次结算 | POST /api/reviews/{publication_id}/settle {results} | 是 | 否 | 否 |
| 确认纠错 | POST /api/reviews/{publication_id}/corrections {results,correction_reason} | 是 | 否 | 否 |
| 最新详情 | GET /api/reviews/{publication_id} | 是 | 是 | 仅published/frozen |
| 版本历史 | GET /api/reviews/{publication_id}/history | 是 | 是 | 仅published/frozen |
| 统计 | GET /api/review-statistics | 全部实际发布 | 全部实际发布 | 仅可见发布全集 |

既有 GET /api/reviews 继续第3项只读列表，不重复注册。发起/意见要求Idempotency-Key，按端点+actor作用域保存，同体重试原ID，异体409，记录不可变。发起或意见不写结算。

results是按冻结leg_id索引的赛果快照；字段限定为ResultSnapshot定义（identity为稳定Identity）；服务器严格构造，不接受market/line/odds/legs等票据替换字段。身份不符/方向不明/缺分节等保留pending，算法沿用现有确定性服务；没有真实赛果认证。首次settle在已有结果改变时必须409；corrections要求已有版本和非空理由；同体重放不增版本。各阶段判定和写入同事务，异常不留记录。

详情/历史只返回白名单：publication_id/version/outcome/原条件标签、逐腿及分支结果、待核原因、按腿ID及比赛身份组织的主客比分/分节/OT、覆盖状态；不返回source_id、原始输入JSON、actor或意见正文。意见仅管理角色分页查询：comment_id/publication_id/status/content=null/content_visibility=redacted及meta.total/limit/offset，不输出未经公开批准的意见原文；该入口仅解决记录可追踪，非全文管理工作流。待核原因统一pending_reasons数组，每项level=option/leg/branch、id、code、reason（固定公开说明或null），不覆盖已确定整票miss。详情、历史及结算写响应共用白名单投影，畸形历史字段安全拒绝，不回显原文。原票不可修改，纠错仅追加历史。统计用发布全集及每票最新版本，不以分页为分母，条件票/撤销/替代历史不删除；viewer统计按可见性另列scope，不混称全库统计。实际金额/概率/EV保持null。

400：请求体/字段/类型/缺幂等键无效。401：匿名。403：权限/CSRF。404：无记录或viewer不可见。409：状态/幂等冲突。500：固定安全错误及request_id，不回显异常。分页limit=1..100、offset>=0，历史总数独立于分页。缺结构返回覆盖不足，不猜原腿。
