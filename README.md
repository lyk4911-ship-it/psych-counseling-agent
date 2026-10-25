# 接着聊 · 中文熟人式聊天原型

在原有心理支持对话引擎上增加账户与持久化记录。目标是重新打开页面后能接着聊，并在相关时参考同一用户之前说过的事情。

## 快速启动

推荐 Python 3.12（最低 3.10）。在仓库根目录运行：

```bash
python3 -m venv venv
venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
venv/bin/python -m uvicorn api_server:app --host 0.0.0.0 --port 8000
```

用浏览器访问服务根路径，创建普通用户账户；通过服务提供的网页使用，而不是直接打开本地 HTML 文件。普通账户与管理员的 HTTP Basic 登录相互独立。默认没有内置账户或密码。

账户、历史、记忆管理不需要模型密钥。真正的聊天生成需要在 `.env` 或运行环境中配置 `DEEPSEEK_API_KEY` 或 `OPENAI_API_KEY`；两者均有值时后者优先。默认服务为 DeepSeek，使用其他兼容服务时同时设置 `OPENAI_BASE_URL` 和 `OPENAI_MODEL`。

```bash
venv/bin/python -m pip install -r requirements-dev.txt
venv/bin/python -m unittest discover -s tests -v
```

测试使用临时 SQLite 数据库，并替换模型调用；不会花费模型额度，也不会更改已有账户。它验证账户、记录及模型接入结构，不证明真实模型的陪伴质量。

## 当前功能

- 注册、登录、退出、修改昵称和密码；修改密码撤销其他设备的登录。
- 密码使用 PBKDF2-SHA256 加盐哈希；随机登录令牌只保存哈希，Cookie 登录默认七天过期。
- 用户之间的会话、消息和记忆相互隔离；普通 `/chat` 只接受服务端保存的状态，不能通过客户端 `state` 或 `allow_show_report` 开启内部权限。
- 会话列表、新建、改名、删除、消息分页；刷新、重新登录和服务重启后保留记录。
- 生成成功后原子保存用户消息、回复和状态。上游失败不存半条对话；同一对话的生成有租约保护，`request_id` 支持重试去重。
- “记住的事”支持手动添加、编辑、删除。聊天会参考少量相关记忆，以及该账户最近 300 条用户消息中检索出的最多 6 条原话。
- 历史召回使用中文二元词片段/英文词和近期消息排序，不依赖向量数据库；不是无限记忆或语义检索。原始完整聊天记录与模型最近约十轮的工作上下文分别存储。
- 个人 JSON 数据导出、删除全部对话、验证密码后注销账户。
- 管理员可查看持久化会话、账户列表和单账户记录。现有 `/tester`、内部报告及旧报告存档保留。

新网页默认关闭旧版空闲主动消息，先保证跨次接续；兼容 API 仍接受 `proactive_ping`。

## 数据及删除语义

数据库默认是 `data/accounts.sqlite3`，可用 `APP_DB_PATH` 指定持久磁盘上的绝对路径。`data/`、数据库及其 WAL 文件、`.env` 和日志已忽略，不应提交到 GitHub。数据库文件权限默认 0600，新数据目录默认 0700；它仍是未加密的 SQLite 文件，主机管理员可读取。

- 删除一段对话会删除它的消息、重试记录，并清理可能受影响的派生状态。
- 删除全部对话会保留用户手动保存的记忆。
- 编辑/删除记忆会清理派生状态，但不删除聊天原文；原文仍可能被检索。需要移除历史内容时，同时删除对应对话。
- 注销账户级联删除登录、对话、消息、记忆及重试记录。这里是应用层删除，不承诺对磁盘残留、外部备份或已导出副本做安全擦除。
- 新账户的普通聊天不再复制到共享 `logs/session.jsonl`；以用户数据库为记录来源。
- 旧的匿名聊天日志和 `logs/show_memory.jsonl` 不自动绑定给任何新用户，避免把历史记录错误交给首个注册者。旧报告仍仅由管理员查看；命令行与内部测试者的旧日志行为独立保留。
- 管理员具备读取所有账户记录的权限。模型生成会把当前对话及选中的历史资料发送给所配置的模型服务。

备份时使用 SQLite 的在线备份机制，例如：

```bash
venv/bin/python - <<'PY'
import sqlite3
with sqlite3.connect('data/accounts.sqlite3') as source:
    with sqlite3.connect('/path/on/private-backup-volume/accounts.sqlite3') as target:
        source.backup(target)
PY
```

不要在服务写入期间只复制主数据库而忽略 WAL。保护备份文件权限；账户删除不会自动清理外部备份。

## API 概览

| 接口 | 说明 |
|---|---|
| `POST /auth/register`, `POST /auth/login` | JSON 用户名/密码；成功后设置登录与 CSRF Cookie |
| `GET /auth/me`, `POST /auth/logout` | 当前用户、撤销当前登录 |
| `PATCH /account`, `POST /account/password`, `DELETE /account` | 昵称、改密码、注销 |
| `GET /account/export`, `DELETE /history` | 导出本人数据、清空对话 |
| `GET/POST /conversations` | 分页列出/新建；列表支持 `limit`、`offset` |
| `GET/PATCH/DELETE /conversations/{id}` | 消息分页、改名、删除；消息支持 `limit`、`before` |
| `POST /chat` | `text`、`conversation_id`、可选 `request_id`；必须登录 |
| `GET/POST /memories`, `PATCH/DELETE /memories/{id}` | 管理自己的显式记忆 |
| `GET /health` | 数据库可用状态与 `model_configured`；服务健康不代表模型已连接 |
| `GET /admin/users`, `GET /admin/sessions` | 管理员账户/会话列表 |
| `GET /admin/users/{id}/export` | 管理员读取单账户完整记录 |
| `POST /admin/chat` | 管理员独立的测试对话与内部报告 |

除注册/登录外，修改用户数据的请求须在 `X-CSRF-Token` 请求头传入本次登录的 `psych_csrf` Cookie 值，同时发送 `psych_auth` Cookie。网页会自动处理。常见错误：401 未登录/过期；403 请求验证失败；404 无权访问或记录不存在；409 重复请求冲突/正在生成；503 模型未配置或暂时不可用。

旧客户端需升级：`/chat` 现在需要登录及 CSRF；传入的客户端状态不再可信。旧测试页使用 `/admin/chat`，不会把匿名测试数据写入真实用户账户。

## 部署范围

当前适合单实例的小型作品/受控试用。公网使用时通过 HTTPS 反向代理访问，设置 `COOKIE_SECURE=1`；用环境变量配置独立管理员凭据。默认同源网页，`CORS_ORIGINS` 仅在确有需要时配置明确来源，不使用带凭据的通配符。

注册和登录有每进程、每来源地址的有限节流；多进程或反向代理部署需要网关层限流，并正确配置可信代理。不要信任外部随意提供的转发 IP。当前没有邮箱验证、邮件找回密码、OAuth、管理员代重置密码或生产级审计系统；未接入邮件服务时不会声称可以找回密码。

心理支持规则及历史量表标签仍是原型功能，不是已验证的诊断或治疗。现有固定脚本评分是启发式回归指标。研究方案应分别验证记忆准确性、自然使用和用户体验。
