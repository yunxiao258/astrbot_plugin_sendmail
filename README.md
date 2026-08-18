# astrbot_plugin_sendmail

邮件发送助手插件（AstrBot），版本 **1.4.0**，许可证 **MIT**。
管理员可以通过 `/邮件` 命令发送邮件，也可以直接让 AI 调用 `send_mail` / `read_mail` 工具自动收发邮件；新邮件支持定时摘要推送、按规则自动转发、内置模板快捷发送。

## 功能特性

- 命令式发信：`/邮件 收件人 | 主题 | 正文`（仅管理员，支持别名 `/发邮件`、`/mail`）
- 附件支持：URL（自动下载）或服务器本地路径，多个附件用逗号分隔，超限自动跳过
- HTML 正文：正文含 HTML 标签时按 HTML 渲染，纯文本自动转义并保留换行
- 双发送通道：SMTP（QQ/163/Gmail/自定义授权码）与 Agent Mail（腾讯 Agent Mail CLI）
- 定时摘要推送：每日/每周调度（或手动 `/邮件 summary now`），新邮件按 message_id 去重后推送到指定会话，可带 LLM 总结
- 转发规则：按发件人/主题/收件箱条件匹配新邮件，自动 SMTP 转发到目标邮箱
- 模板快捷发送：内置 5 个邮件模板（请假申请/周报提交/生日祝福/会议通知/感谢信），占位符渲染一键发送
- AI 联动：对 AI 说「帮我发邮件到 xxx，主题 yyy，内容 zzz」，AI 会调用 `send_mail` 工具自动发送；说「看看我的邮件」「帮我找下老板的邮件」等，AI 会调用 `read_mail` 工具读取收件箱并总结（均仅管理员生效）
- 安全防护：仅管理员可用、发送频率限制、并发锁、发送历史日志（最近 50 条）

## 使用方法

```text
/邮件 收件人 | 主题 | 正文
/邮件 收件人 | 主题 | 正文 --附件=链接或路径
/邮件 帮助
```

- 收件人多个用英文/中文逗号或分号分隔，自动过滤非法邮箱并去重
- 正文可换行、可包含 `|`（仅按前两个 `|` 分隔收件人/主题/正文）
- 附件支持 URL（自动下载）或服务器本地路径，多个用逗号分隔；`--附件=` 与 `-a=` 均可
- 正文超过 `body_max_chars` 时自动截断并提示

示例：

```text
/邮件 a@qq.com,b@163.com | 周报 | 本周数据见附件 --附件=https://example.com/report.pdf
```

AI 用法示例（对 AI 说）：

```text
帮我发封邮件到 boss@example.com，主题是「季度总结」，内容写上周的销量数据
看看我邮箱里最新的几封邮件，总结一下
搜索邮件里关于「报销」的内容
```

### 子命令一览

| 子命令 | 说明 |
| --- | --- |
| `/邮件 rule ...` | 转发规则管理（add / list / remove） |
| `/邮件 summary now` | 立即检查新邮件并推送摘要到 `summary_targets` |
| `/邮件 template list` | 查看全部内置模板与占位符说明 |
| `/邮件 template <模板名>` | 查看单个模板详情 |
| `/邮件 send <模板名> <收件人> 占位符=值 ...` | 模板快捷发送 |

## 转发规则

新邮件自动匹配规则，命中即用 SMTP 自动转发（发件人为配置账号，正文附原邮件文本摘要，主题带 `[转发]` 前缀）。规则持久化在 `plugin_data/astrbot_plugin_sendmail/forward_rules.json`。

```text
/邮件 rule add 发件人=关键词 主题=关键词 [收件箱=inbox] 转发=目标邮箱
/邮件 rule list
/邮件 rule remove <规则ID>
```

- 条件为「或」关系：任一条件命中即匹配（发件人匹配显示名与邮箱地址，大小写不敏感）；一条规则命中即转发，不重复转发
- 规则至少需要一个条件（发件人=/主题=/收件箱=）；无任何条件的规则永不匹配
- 转发失败仅记日志，不影响新邮件推送主流程

示例：

```text
/邮件 rule add 发件人=boss@ 主题=报销 转发=backup@example.com
/邮件 rule add 收件箱=inbox 转发=archive@example.com
```

## 定时摘要推送

插件按 `auto_summary_interval`（默认 30 秒）轮询收件箱（最新 50 封），按 **message_id 去重**（seen 机制）：

- 首次运行只建立基线（不推送历史邮件），之后的才算新邮件
- 有新邮件时推送到 `auto_summary_targets` 指定的会话，内容含 AI 总结（`auto_summary_llm` 开启且 provider 可用时）+ 每封的主题/发件人/时间/摘要；LLM 不可用时自动回退明细推送
- 每轮最多推送 `auto_summary_max_mails` 封（防刷屏）
- 推送成功才标记已读；全部目标发送失败时不标记，下一轮自动重试

除轮询推送外，还支持按调度时间点生成摘要推送（推送到 `summary_targets`）：

| 配置项 | 说明 |
| --- | --- |
| `summary_push_enabled` | 是否启用定时摘要推送（默认 true） |
| `summary_schedule_mode` | `daily`（每天）/ `weekly`（每周） |
| `summary_schedule_time` | 触发时间 HH:MM（默认 `09:00`） |
| `summary_schedule_weekday` | 每周模式触发日（1=周一 .. 7=周日，默认 1） |
| `summary_targets` | 推送目标会话（`平台:GroupMessage/FriendMessage:会话ID`，逗号分隔） |

手动触发：

```text
/邮件 summary now
```

立即检查新邮件并推送摘要（与定时推送共用 seen 去重，已推送过的邮件不会重复推）。

## 模板快捷发送

内置 5 个邮件模板，通过 `/邮件 send` 一键渲染发送；占位符用 `{占位符}` 写在主题与正文中，渲染仅替换模板声明的白名单占位符（防 format 注入），缺失的占位符会被命令层拦截提示。

```text
/邮件 send <模板名> <收件人> 占位符=值 占位符=值 ...
/邮件 template list          # 查看全部模板与占位符说明
/邮件 template <模板名>      # 查看单个模板详情
```

示例：

```text
/邮件 send 请假申请 a@b.com 名字=张三 日期=2026-08-18 原因=家中有事 天数=1 领导称呼=王经理 同事=李四
```

| 模板 | 用途 | 占位符 |
| --- | --- | --- |
| 请假申请 | 向领导提交请假申请 | 名字、领导称呼、原因、天数、日期、同事 |
| 周报提交 | 向领导提交本周工作周报 | 名字、领导称呼、日期、本周工作、下周计划、问题需求 |
| 生日祝福 | 给同事/朋友发送生日祝福 | 名字、祝愿、署名 |
| 会议通知 | 通知相关人员参会 | 会议主题、日期、时间、地点、组织者 |
| 感谢信 | 向对方表达感谢 | 名字、事件、感谢内容、署名、日期 |

占位符值含空格时可用引号包裹（如 `原因="临时出差 3 天"`）。

## AI 工具

| 工具 | 说明 | 参数 |
| --- | --- | --- |
| `send_mail` | 发送邮件 | `to` 收件人（逗号分隔）、`subject` 主题、`body` 正文、`attachments` 附件（可选，URL/路径逗号分隔） |
| `read_mail` | 读取邮件 | `query` 搜索关键词（可选，留空列出收件箱最新）、`limit` 条数 1-10、`include_body` 是否读正文（默认 false，开启时上限 5 封） |

两个工具均仅管理员可调用（AI 对话中由 LLM function calling 触发）。

## 配置项

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `send_channel` | 发送通道：`smtp` / `agently`（非法值回退 smtp） | `smtp` |
| `smtp_provider` | 服务商预设：`qq` / `gmail` / `163` / `custom` | `qq` |
| `smtp_host` | 自定义 SMTP 服务器（custom 时生效） | 空 |
| `smtp_port` | 自定义 SMTP 端口（custom 时生效） | `465` |
| `smtp_ssl` | 是否 SSL 连接（465 走 SSL，587 可关掉走 STARTTLS） | `true` |
| `smtp_user` | 发件邮箱账号 | 空 |
| `smtp_auth_code` | SMTP 授权码（非登录密码） | 空 |
| `mail_from_name` | 发件人显示名称 | `AstrBot` |
| `mail_html` | 是否按 HTML 发送正文 | `true` |
| `body_max_chars` | 正文最大长度（超出截断） | `5000` |
| `attach_max_mb` | 单附件大小上限（MB，本地与 URL 附件通用） | `10` |
| `send_interval` | 两次发送最小间隔（秒，防滥用） | `30` |
| `agently_attach_max_mb` | Agent Mail 通道附件上限（MB，服务端上限 20） | `20` |
| `agently_cli_path` | agently-cli 可执行文件路径（留空自动在 PATH 中查找） | 空 |
| `auto_summary_enabled` | 是否启用定时读取邮箱并自动总结推送 | `true` |
| `auto_summary_interval` | 定时检查间隔（秒，最小 10） | `30` |
| `auto_summary_targets` | 推送目标会话（unified_msg_origin，逗号分隔；留空不推送） | 空 |
| `auto_summary_llm` | 是否用 LLM 生成总结（不可用时回退明细推送） | `true` |
| `auto_summary_max_mails` | 每轮最多推送的新邮件数 | `5` |
| `summary_push_enabled` | 是否启用定时摘要推送（按 `summary_schedule_*` 调度） | `true` |
| `summary_schedule_mode` | 摘要推送调度模式：`daily`（每天）/ `weekly`（每周） | `daily` |
| `summary_schedule_time` | 摘要推送触发时间（HH:MM） | `09:00` |
| `summary_schedule_weekday` | 每周模式触发日（1=周一 .. 7=周日） | `1` |
| `summary_targets` | 定时摘要推送目标会话（unified_msg_origin，逗号分隔；留空不推送） | 空 |

## 发送通道

### SMTP（默认）

1. 在邮箱设置中开启 SMTP 服务并获取**授权码**（非登录密码）
2. 填写 `smtp_user`、`smtp_auth_code`，按需选择 `smtp_provider`（qq/gmail/163 预设，或 custom + `smtp_host`/`smtp_port`）

### Agent Mail（agently）

1. 安装 CLI 并授权：

   ```bash
   npm install -g @tencent-qqmail/agently-cli
   agently-cli auth login
   ```

2. 按浏览器提示完成 OAuth 授权（验证方式：`agently-cli +me`）
3. 将 `send_channel` 改为 `agently`，保存配置并重载插件

注意：CLI 授权保存在本机用户凭证中，AstrBot 需与授权使用同一系统用户运行；Windows 下自动解析 npm .cmd shim 为 node + 包主入口执行，避免多行正文被截断。

## 数据存储

插件数据保存在 `plugin_data/astrbot_plugin_sendmail/` 目录：

| 文件 | 说明 |
| --- | --- |
| `send_log.json` | 发送历史日志（最近 50 条，含时间/收件人/主题/通道/附件，原子写） |
| `seen_mail_ids.json` | 已推送/已读邮件的 message_id 去重集合（原子写） |
| `forward_rules.json` | 转发规则（原子写） |

## 依赖

- `httpx`：URL 附件下载（AstrBot 自带环境一般已装）
- Agent Mail 通道：需额外安装 `@tencent-qqmail/agently-cli`（npm）

## 更新记录

- **1.4.0**：新增定时摘要推送（每日/每周调度 + `/邮件 summary now` 手动触发、seen 去重）、转发规则（发件人/主题/收件箱条件匹配、SMTP 自动转发）、模板快捷发送（内置 5 模板、占位符白名单渲染）
- **1.3.0**：新增定时邮件总结推送（按配置间隔轮询收件箱，LLM 总结 + 明细推送）
- **1.2.0**：新增 `read_mail` AI 工具（读取/搜索/总结邮件）
- **1.1.1**：新增 `send_mail` AI 工具（AI 自动发送）
- **1.1.0**：新增 Agent Mail CLI 发送通道
- **1.0.0**：初始版本（命令式发信，SMTP 通道）

## 开发与测试

```bash
python -m unittest test_sendmail -v
```

测试共 120 个，覆盖命令解析、HTML 转换、MIME 构建、SMTP 发送、Agent Mail 通道、AI 工具、转发规则匹配与持久化、定时摘要调度与推送、模板渲染与命令。