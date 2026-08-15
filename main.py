# -*- coding: utf-8 -*-
"""AstrBot 邮件发送助手插件：管理员按命令发送邮件。

功能：
- `/邮件 收件人 | 主题 | 正文`：发送邮件，收件人多个用逗号分隔
- `/邮件 收件人 | 主题 | 正文 --附件=URL或路径,多个逗号分隔`：携带附件
- `/邮件 帮助`：查看用法
- 支持 HTML 正文（配置 mail_html 开启时），QQ/Gmail/163/自定义 SMTP 预设
- 仅管理员可用，带发送频率限制，发送日志落盘便于排查
"""

import asyncio
import html
import json
import os
import re
import shutil
import smtplib
import subprocess
import tempfile
import time
import urllib.parse
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate

import httpx

from astrbot.api import AstrBotConfig, llm_tool, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_sendmail"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "邮件发送助手：管理员按命令或让 AI 调用工具发送邮件"
PLUGIN_VERSION = "1.1.1"

# 邮件服务商预设：provider -> (host, port, ssl)
SMTP_PRESETS = {
    "qq": ("smtp.qq.com", 465, True),
    "gmail": ("smtp.gmail.com", 465, True),
    "163": ("smtp.163.com", 465, True),
}

# 收件人分隔符：逗号/分号（中英文）
RECIPIENT_SPLIT_RE = re.compile(r"[,，;；]+")
# 附件参数：--附件=<v> 或 -a=<v>
ATTACH_PARAM_RE = re.compile(r"(?:--附件|-a)\s*=\s*([^\s]+)", re.I)
# 简单邮箱格式校验
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]{2,}$")
# 是否包含 HTML 标签
HTML_TAG_RE = re.compile(r"<[a-zA-Z/][^>]*>")
# URL 识别
URL_RE = re.compile(r"^https?://", re.I)

HELP_TEXT = (
    "📧 邮件发送助手用法（仅管理员）\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "/邮件 收件人 | 主题 | 正文\n"
    "  收件人多个用逗号分隔；正文可换行、可含 |\n"
    "/邮件 收件人 | 主题 | 正文 --附件=链接或路径\n"
    "  附件支持 URL 或服务器本地路径，多个用逗号分隔\n"
    "/邮件 帮助\n"
    "示例：/邮件 a@qq.com,b@163.com | 周报 | 本周数据见附件\n"
    "--附件=https://example.com/report.pdf\n"
    "正文包含 HTML 标签时按 HTML 渲染，普通文本自动转义。\n"
    "发送通道：smtp（SMTP 授权码）/ agently（Agent Mail CLI）\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "AI 联动：对 AI 说「帮我发邮件到 xxx，主题 yyy，内容 zzz」\n"
    "即会由 AI 调用 send_mail 工具自动发送（同样仅管理员生效）"
)


def _to_html(text: str) -> str:
    """普通文本转 HTML：含标签视为已写好的 HTML 原样发送，否则转义并保留换行"""
    if HTML_TAG_RE.search(text):
        return text
    return "<p>" + html.escape(text).replace("\n", "<br>\n") + "</p>"


def _file_part(name: str, data: bytes) -> MIMEApplication:
    """构造附件 MIME 部分（文件名编码兼容中文）"""
    part = MIMEApplication(data, _subtype="octet-stream")
    part.add_header("Content-Disposition", "attachment", filename=("utf-8", "", name))
    return part


class SendMailPlugin(Star):
    """邮件发送助手：管理员按命令发送邮件"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config or {}
        self._last_send_at: float = 0.0
        self._send_lock = False
        self._history: list[dict] = []
        self._history_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin_data",
            PLUGIN_NAME,
            "send_log.json",
        )
        self._load_history()
        logger.info(f"【{PLUGIN_NAME}】邮件发送助手插件初始化完成")

    # ==================== 配置安全取值 ====================

    def _int_cfg(self, key: str, default: int) -> int:
        """安全读取整数配置：脏值回退默认值"""
        try:
            v = self.config.get(key, default)
            return int(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    def _bool_cfg(self, key: str, default: bool) -> bool:
        """安全读取布尔配置：脏值回退默认值"""
        v = self.config.get(key, default)
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            low = v.strip().lower()
            if low in ("1", "true", "yes", "on", "是"):
                return True
            if low in ("0", "false", "no", "off", "否", ""):
                return False
            return default
        if isinstance(v, (int, float)):
            return bool(v)
        return default

    # ==================== 发送日志 ====================

    def _load_history(self):
        """加载历史发送日志（仅保留最近 50 条）"""
        try:
            if os.path.exists(self._history_path):
                with open(self._history_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    self._history = data[-50:]
        except Exception as e:
            logger.warning(f"加载邮件发送日志失败: {e}")

    def _append_history(self, record: dict):
        """追加一条发送记录并落盘"""
        self._history.append(record)
        self._history = self._history[-50:]
        try:
            os.makedirs(os.path.dirname(self._history_path), exist_ok=True)
            with open(self._history_path, "w", encoding="utf-8") as f:
                json.dump(self._history, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存邮件发送日志失败: {e}")

    # ==================== 解析 ====================

    @staticmethod
    def _extract_attachments(args: str) -> tuple[list[str], str]:
        """提取 --附件= 参数，返回 (附件列表, 去除参数后的剩余文本)"""
        files: list[str] = []
        for att in ATTACH_PARAM_RE.findall(args):
            for part in re.split(r"[,，;；]+", att):
                if part.strip():
                    files.append(part.strip())
        rest = ATTACH_PARAM_RE.sub("", args).strip()
        return files, rest

    @staticmethod
    def _split_fields(args: str) -> tuple[str, str, str]:
        """按 | 分隔成 (收件人, 主题, 正文)；正文允许包含 |"""
        parts = args.split("|", 2)
        if len(parts) == 3:
            return parts[0].strip(), parts[1].strip(), parts[2].strip()
        if len(parts) == 2:
            return parts[0].strip(), parts[1].strip(), ""
        return parts[0].strip(), "", ""

    @staticmethod
    def _parse_recipients(raw: str) -> list[str]:
        """解析收件人列表并过滤非法邮箱（去重、去空白）"""
        seen, out = set(), []
        for r in RECIPIENT_SPLIT_RE.split(raw):
            r = r.strip()
            if r and r not in seen and EMAIL_RE.match(r):
                seen.add(r)
                out.append(r)
        return out

    @staticmethod
    def _classify_attachment(spec: str) -> tuple[str, str]:
        """附件规格分类：url / file / invalid"""
        if URL_RE.match(spec):
            return "url", spec
        if os.path.isfile(spec):
            return "file", spec
        return "invalid", spec

    @staticmethod
    def _filename_from_url(url: str) -> str:
        """从 URL 提取文件名（路径最后一段，解码）；无文件名或无扩展名时给默认名"""
        path = urllib.parse.urlparse(url).path
        name = urllib.parse.unquote(os.path.basename(path)) if path else ""
        name = name.strip()
        if not name or "." not in name:
            return "attachment.bin"
        return name

    # ==================== MIME 构建 ====================

    def _build_mime(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        attachments: list[tuple[str, str]],
    ) -> MIMEMultipart:
        """构建邮件 MIME（正文 + 附件）。URL 附件以占位 part 标记，发送线程内下载替换。"""
        sender = str(self.config.get("smtp_user", "")).strip()
        from_name = str(self.config.get("mail_from_name", "AstrBot")).strip() or "AstrBot"
        msg = MIMEMultipart()
        msg["From"] = formataddr((str(Header(from_name, "utf-8")), sender))
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = Header(subject or f"来自机器人 {time.strftime('%Y-%m-%d %H:%M')}", "utf-8")
        msg["Date"] = formatdate(localtime=True)

        if self._bool_cfg("mail_html", True):
            msg.attach(MIMEText(_to_html(body), "html", "utf-8"))
        else:
            msg.attach(MIMEText(body, "plain", "utf-8"))

        for kind, value in attachments:
            if kind == "url":
                msg.attach(_UrlPlaceholder(value, self._filename_from_url(value)))
            else:
                with open(value, "rb") as f:
                    data = f.read()
                msg.attach(_file_part(os.path.basename(value), data))
        return msg

    # ==================== 发送 ====================

    def _smtp_settings(self) -> tuple[str, int, bool]:
        """获取 SMTP 连接设置：(host, port, ssl)"""
        provider = str(self.config.get("smtp_provider", "qq")).strip().lower()
        preset = SMTP_PRESETS.get(provider)
        if preset:
            return preset
        host = str(self.config.get("smtp_host", "")).strip()
        if not host:
            logger.warning("smtp_provider=custom 但未配置 smtp_host，回退 QQ 邮箱")
            return SMTP_PRESETS["qq"]
        return host, max(1, self._int_cfg("smtp_port", 465)), self._bool_cfg("smtp_ssl", True)

    def _send_sync(self, msg: MIMEMultipart, max_mb: int) -> list[str]:
        """同步发送邮件（smtplib/httpx 均为同步库，由调用方放入线程）；返回下载失败的附件名列表"""
        user = str(self.config.get("smtp_user", "")).strip()
        auth = str(self.config.get("smtp_auth_code", "")).strip()
        if not user or not auth:
            raise RuntimeError("未配置 smtp_user / smtp_auth_code")

        # 下载 URL 附件并替换占位部分
        failed: list[str] = []
        payload = msg.get_payload()
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            for i, sub in enumerate(payload):
                if isinstance(sub, _UrlPlaceholder):
                    try:
                        resp = client.get(sub.url)
                        resp.raise_for_status()
                        data = resp.content
                        if len(data) > max_mb * 1024 * 1024:
                            failed.append(f"{sub.name}(超过 {max_mb}MB)")
                            payload[i] = _file_part(sub.name, "(附件超过大小限制，未下载)".encode())
                            continue
                        payload[i] = _file_part(sub.name, data)
                    except Exception as e:
                        logger.warning(f"下载附件失败 {sub.url}: {e}")
                        failed.append(sub.name)
                        payload[i] = _file_part(sub.name, "(附件下载失败)".encode())

        host, port, ssl = self._smtp_settings()
        if ssl:
            server = smtplib.SMTP_SSL(host, port, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls()
        try:
            server.login(user, auth)
            server.sendmail(user, [r.strip() for r in msg["To"].split(",")], msg.as_string())
        finally:
            try:
                server.quit()
            except Exception:
                pass
        return failed

    # ==================== Agent Mail CLI 通道 ====================

    def _agently_cmd(self) -> str:
        """agently-cli 可执行文件：配置指定优先，其次解析 PATH 中的真实 exe。
        Windows 下 npm 全局安装的是 .cmd shim，subprocess 无法直接执行，需解析到 node_modules 中的真实 exe。"""
        cfg = str(self.config.get("agently_cli_path", "")).strip()
        if cfg:
            return cfg
        exe = shutil.which("agently-cli") or shutil.which("agently-cli.exe") or ""
        if not exe:
            return "agently-cli"
        if exe.lower().endswith((".cmd", ".bat")):
            base = os.path.dirname(exe)
            for npm_root in (base, os.path.dirname(base)):
                real = os.path.join(
                    npm_root, "node_modules", "@tencent-qqmail",
                    "agently-cli", "agently-cli.exe",
                )
                if os.path.isfile(real):
                    return real
        return exe

    @staticmethod
    def _agently_missing_hint() -> str:
        return (
            "❌ 未安装或无法执行 agently-cli。请安装并授权：\n"
            "npm install -g @tencent-qqmail/agently-cli\n"
            "agently-cli auth login"
        )

    def _send_agently_sync(
        self,
        recipients: list[str],
        subject: str,
        body: str,
        attachments: list[tuple[str, str]],
        max_mb: int,
    ) -> list[str]:
        """通过 Agent Mail CLI 发送（CLI 附件路径必须相对当前目录，故先归集到临时目录）；
        返回下载失败的附件名列表；发送失败抛 RuntimeError。"""
        failed: list[str] = []
        workdir = tempfile.mkdtemp(prefix="agently_")
        attach_args: list[str] = []
        try:
            # 1) 归集附件到临时工作目录（URL 下载 / 本地复制），生成相对路径参数
            with httpx.Client(timeout=30, follow_redirects=True) as client:
                for i, (kind, value) in enumerate(attachments):
                    if kind == "url":
                        name = self._filename_from_url(value)
                        try:
                            resp = client.get(value)
                            resp.raise_for_status()
                            data = resp.content
                            if len(data) > max_mb * 1024 * 1024:
                                failed.append(f"{name}(超过 {max_mb}MB)")
                                continue
                            local = os.path.join(workdir, f"{i}_{name}")
                            with open(local, "wb") as f:
                                f.write(data)
                            attach_args += ["--attachment", os.path.basename(local)]
                        except Exception as e:
                            logger.warning(f"下载附件失败 {value}: {e}")
                            failed.append(name)
                    else:
                        local = os.path.join(workdir, f"{i}_{os.path.basename(value)}")
                        shutil.copy2(value, local)
                        attach_args += ["--attachment", os.path.basename(local)]

            # 2) 组装并执行 CLI
            cmd = [self._agently_cmd(), "message", "+send", "--confirmed"]
            for r in recipients:
                cmd += ["--to", r]
            cmd += ["--subject", subject]
            html_mode = self._bool_cfg("mail_html", True)
            if html_mode:
                cmd += ["--body", _to_html(body)]
            else:
                cmd += ["--body", body, "--body-format", "plain"]
            cmd += attach_args

            try:
                proc = subprocess.run(
                    cmd,
                    cwd=workdir,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=180,
                )
            except FileNotFoundError:
                raise RuntimeError(self._agently_missing_hint())
            except subprocess.TimeoutExpired:
                raise RuntimeError("❌ Agent Mail CLI 发送超时（180 秒），请重试")

            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                raise RuntimeError(f"❌ Agent Mail CLI 发送失败: {err or out or f'exit {proc.returncode}'}")

            # 3) 解析 JSON 结果
            try:
                data = json.loads(out)
            except json.JSONDecodeError:
                if "ok" in out or "成功" in out:
                    return failed
                raise RuntimeError(f"❌ Agent Mail CLI 返回异常: {out or err or '无输出'}")
            if not data.get("ok", True):
                raise RuntimeError(
                    f"❌ Agent Mail CLI 发送失败: {data.get('message') or data.get('error') or data}"
                )
            return failed
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # ==================== 指令 ====================

    def _send_text(self, event: AstrMessageEvent, text: str):
        """构造纯文本回复结果"""
        return event.chain_result([Plain(text)])

    @filter.command("邮件", alias={"发邮件", "mail"}, priority=200)
    async def cmd_mail(self, event: AstrMessageEvent):
        """邮件发送主入口（仅管理员）"""
        if not event.is_admin():
            return self._send_text(event, "❌ 仅管理员可使用邮件发送功能。")

        args = (getattr(event, "message_str", "") or "").strip()
        args = re.sub(r"^[\\/／]?\s*(邮件|发邮件|mail)\s*", "", args, flags=re.I).strip()
        if not args or args in ("帮助", "help", "Help", "HELP"):
            return self._send_text(event, HELP_TEXT)

        attachments_spec, rest = self._extract_attachments(args)
        recipients_raw, subject, body = self._split_fields(rest)
        recipients = self._parse_recipients(recipients_raw)
        if not recipients:
            return self._send_text(
                event,
                "❌ 收件人无效。用法: /邮件 收件人 | 主题 | 正文\n"
                "多个收件人用逗号分隔；「/邮件 帮助」查看完整说明。",
            )
        if not body:
            return self._send_text(
                event,
                "❌ 正文为空。用法: /邮件 收件人 | 主题 | 正文（正文允许包含 |）",
            )

        msg = await self._dispatch_send(event, recipients, subject, body, attachments_spec)
        return self._send_text(event, msg)

    async def _dispatch_send(
        self,
        event: AstrMessageEvent,
        recipients: list[str],
        subject: str,
        body: str,
        attachments_spec: list[str],
    ) -> str:
        """执行发送流程（校验、频率限制、通道分发、记录），返回结果文本；供命令与 LLM 工具共用。"""
        max_chars = max(100, self._int_cfg("body_max_chars", 5000))
        truncated = len(body) > max_chars
        if truncated:
            body = body[:max_chars]

        # 频率限制 + 并发保护
        interval = max(1, self._int_cfg("send_interval", 30))
        wait = interval - (time.time() - self._last_send_at)
        if wait > 0:
            return f"⏳ 发送太频繁，请 {int(wait)} 秒后再试（send_interval={interval}s）。"
        if self._send_lock:
            return "⏳ 正在发送上一封邮件，请稍候。"

        max_mb = max(1, self._int_cfg("attach_max_mb", 10))
        attach_failed: list[str] = []
        attachments: list[tuple[str, str]] = []
        for spec in attachments_spec:
            kind, value = self._classify_attachment(spec)
            if kind == "invalid":
                attach_failed.append(spec)
            else:
                attachments.append((kind, value))

        channel = str(self.config.get("send_channel", "smtp")).strip().lower()
        if channel != "agently":
            channel = "smtp"

        self._send_lock = True
        try:
            if channel == "agently":
                max_mb = min(max_mb, max(1, self._int_cfg("agently_attach_max_mb", 20)))
                failed = await asyncio.to_thread(
                    self._send_agently_sync, recipients, subject, body, attachments, max_mb
                )
            else:
                try:
                    msg = self._build_mime(recipients, subject, body, attachments)
                except Exception as e:
                    logger.error(f"构建邮件失败: {e}")
                    return f"❌ 构建邮件失败: {e}"
                failed = await asyncio.to_thread(self._send_sync, msg, max_mb)
        except Exception as e:
            logger.error(f"发送邮件失败: {e}")
            return f"❌ 发送失败: {e}"
        finally:
            self._send_lock = False
            self._last_send_at = time.time()

        self._append_history({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "to": recipients,
            "subject": subject,
            "from": (
                str(self.config.get("smtp_user", "")).strip()
                if channel == "smtp"
                else "agently-cli"
            ),
            "channel": channel,
            "attachments": [a[1] for a in attachments],
        })

        lines = [f"✅ 邮件已发送到 {', '.join(recipients)}（共 {len(recipients)} 封）"]
        if truncated:
            lines.append(f"⚠️ 正文过长，已截取前 {max_chars} 字")
        if attach_failed:
            lines.append(f"⚠️ 附件无效已忽略: {', '.join(attach_failed)}")
        if failed:
            lines.append(f"⚠️ 以下附件下载失败: {', '.join(failed)}")
        return "\n".join(lines)

    @llm_tool(name="send_mail")
    async def ai_send_mail(
        self,
        event: AstrMessageEvent,
        to: str,
        subject: str,
        body: str,
        attachments: str = "",
    ):
        """发送邮件到指定邮箱（仅管理员可用）。

        Args:
            to(string): 收件人邮箱地址，多个收件人用英文逗号分隔
            subject(string): 邮件主题
            body(string): 邮件正文，支持 HTML 标签
            attachments(string): 附件地址，可选；多个附件用英文逗号分隔，每个附件为 URL 或服务器本地文件路径
        """
        if not event.is_admin():
            return "❌ 仅管理员可以使用发送邮件功能。"
        recipients = self._parse_recipients(to)
        if not recipients:
            return "❌ 收件人无效，请提供有效的邮箱地址。"
        specs = [a.strip() for a in attachments.split(",") if a.strip()] if attachments else []
        return await self._dispatch_send(event, recipients, subject, body, specs)


class _UrlPlaceholder:
    """URL 附件占位：保存 URL 与文件名，发送线程内下载后替换为真实 MIME 附件"""

    def __init__(self, url: str, name: str):
        self.url = url
        self.name = name
