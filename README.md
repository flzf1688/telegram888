# Telegram888 / 连客

独立工作空间开发版；不是旧系统的复制。源代码和运行环境位于开发服务器。

## 当前实现
- HTTPS 登录、一次性管理员初始化、服务端会话、来源校验。
- PostgreSQL 通讯录、分组、CSV/XLSX 预览与确认导入、号码去重、联系许可和拒绝状态。
- Telethon SQLite .session 受限解析、加密保存、明确启停；Telegram API ID/Hash 后台配置。
- 固定 SOCKS5 代理配置与 Telegram DC TCP 检测，不静默回退直连。
- 私聊同步、文本历史、持久化发送队列、平台消息 ID、失败/结果未知状态。
- 获许可且带 Telegram 用户名的联系人任务，默认暂停，固定账号归属。
- OpenAI Responses API 的结构化接待决策、产品资料、客服主账号配置、逐会话授权、人工接管、拒绝停止、调用次数预算。

## 验证边界
自动化测试与浏览器验收使用独立 test 数据库。开发业务库不灌演示账号和联系人。
尚未提供真实 Telegram / OpenAI 凭据，不能声明真实账号上线、消息实际到达或模型回复已验收。代理也必须使用实际供应商凭据测试。
当前不支持 Tdata 转换、手机号验证码登录、非文本消息发送、自动客服到达确认、租户隔离、多服务器连接调度和大规模压测。不得把本版本当作已通过大规模运行验收的系统。
首版产品知识以受限文本上下文实现，不是向量库或模型训练。

## 运行
Python 3.14，PostgreSQL，本地 Unix socket；依赖以 requirements.lock 固定。
配置文件在 /etc/telegram888/app.env（不入仓库）；VAULT_KEY 独立保护，数据库备份恢复需要配套密钥。
服务 telegram888.service 仅一个 uvicorn worker，连接运行器使用 PostgreSQL advisory lock。禁止擅自增加 workers 或复制真实会话到其他节点。
真实入口使用 HTTPS 8444，UI 设计预览仍位于 HTTP 8088（不接收真实凭据）。
管理员只可初始化一次；初始化码只存于服务器 root 私有文件，管理员创建后不再有效。

## 测试
创建独立 telegram888_test 数据库并授权 telegram888 数据库角色后，运行：
`runuser -u telegram888 -- /opt/telegram888/tests/run.sh`
测试用密钥仅用于合成数据，禁止用于业务环境。

## 外部发送语义
发送请求持久化后进入 queued；只有识别 Telegram 消息 ID 才记为 sent。
RPC 明确拒绝为 failed，超时/重启中的请求为 uncertain，不自动重试未知结果。
暂停只能阻止尚未调用的发送，已经发给 Telegram 的请求无法撤销。
OpenAI 模型只能给出受约束决策；配置的客服链接由服务端附加，不接受模型任意重定向。
OpenAI 连接检查只检查鉴权与模型可访问性，不代表生成质量验收。

## 后续里程碑
真实测试会话与代理验证 → OpenAI 模型质量和转交测试 → 数据迁移版本管理与备份恢复演练 → 多租户和连接服务拆分 → 分级容量测试。
