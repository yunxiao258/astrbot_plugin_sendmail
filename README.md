# astrbot_plugin_sendmail

邮件发送助手插件（AstrBot）。管理员可以通过 `/邮件` 命令发送邮件，也可以直接让 AI 调用 `send_mail` 工具自动发送。

## 功能特性

- 命令式发信：`/邮件 收件人 | 主题 | 正文`（仅管理员）
- 附件支持：URL 或服务器本地路径，多个附件用逗号分隔
- HTML 正文：正文含 HTML 标签时按 HTML 渲染，纯文本自动转义并保留换行
- 双发送通道：SMTP（QQ/163/Gmail 授权码）与 Agent Mail（腾讯 Agent Mail CLI）
- AI 联动：对 AI 说「帮我发邮件到 xxx，主题 yyy，内容 zzz」，AI 会调用 `send_mail` 工具自动发送（同样仅管理员生效）
- 安全防护：仅管理员可用、发送频率限制、并发锁、发送历史日志

## 使用方法

```text
/邮件 收件人 | 主题 | 正文
/邮件 收件人 | 主题 | 正文 --附件=链接或路径
/邮件 帮助
```

- 收件人多个用英文/中文逗号或分号分隔
- 正文可换行、可包含 `|`
- 附件支持 URL（自动下载）或服务器本地路径

示例：

```text
/邮件 a@qq.com,b@163.com | 周报 | 本周数据见附件 --附件=https://example.com/report.pdf
```

AI 用法示例（对 AI 说）：

```text
帮我发封邮件到 boss@example.com，主题是「季度总结」，内容写上周的销量数据
```

## 配置项

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `send_channel` | 发送通道：`smtp` / `agently` | `smtp` |
| `smtp_provider` | 服务商预设：`qq` / `gmail` / `163` / `custom` | `qq` |
| `smtp_host` | 自定义 SMTP 服务器（custom 时生效） | 空 |
| `smtp_port` | 自定义 SMTP 端口（custom 时生效） | `465` |
| `smtp_ssl` | 是否 SSL 连接（465 走 SSL，587 可关掉走 STARTTLS） | `true` |
| `smtp_user` | 发件邮箱账号 | 空 |
| `smtp_auth_code` | SMTP 授权码（非登录密码） | 空 |
| `mail_from_name` | 发件人显示名称 | `AstrBot` |
| `mail_html` | 是否按 HTML 发送正文 | `true` |
| `body_max_chars` | 正文最大长度（超出截断） | `5000` |
| `attach_max_mb` | 单附件大小上限（MB） | `10` |
| `send_interval` | 两次发送最小间隔（秒） | `30` |
| `agently_attach_max_mb` | Agent Mail 通道附件上限（MB，服务端上限 20） | `20` |
| `agently_cli_path` | agently-cli 可执行文件路径（留空自动在 PATH 中查找） | 空 |

## 发送通道

### SMTP（默认）

1. 在邮箱设置中开启 SMTP 服务并获取**授权码**（非登录密码）
2. 填写 `smtp_user`、`smtp_auth_code`，按需选择 `smtp_provider`

### Agent Mail（agently）

1. 安装 CLI 并授权：

   ```bash
   npm install -g @tencent-qqmail/agently-cli
   agently-cli auth login
   ```

2. 按浏览器提示完成 OAuth 授权（验证方式：`agently-cli +me`）
3. 将 `send_channel` 改为 `agently`，保存配置并重载插件

注意：CLI 授权保存在本机用户凭证中，AstrBot 需与授权使用同一系统用户运行。

## 开发与测试

```bash
python -m unittest test_sendmail -v
```

测试共 52 个，覆盖解析、HTML 转换、MIME 构建、SMTP 发送、Agent Mail 通道与 AI 工具。
