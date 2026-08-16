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
from astrbot.api.all import MessageChain
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

PLUGIN_NAME = "astrbot_plugin_sendmail"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "邮件助手：管理员让 AI 发送/读取邮件，定时总结推送新邮件"
PLUGIN_VERSION = "1.3.0"

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
    "即会由 AI 调用 send_mail 工具自动发送；说「看看我的邮件」\n"
    "AI 会调用 read_mail 工具读取邮箱并总结（均仅管理员生效）"
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
    """邮件发送助手：管理员按命令发送邮件，AI 工具发送/读取，定时总结推送"""

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
        self._seen_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin_data",
            PLUGIN_NAME,
            "seen_mail_ids.json",
        )
        self._load_history()
        self._mail_watcher_task = None
        self._last_targets_warn: float = 0.0
        logger.info(f"【{PLUGIN_NAME}】邮件发送助手插件初始化完成")

    # ==================== 定时任务生命周期 ====================

    async def initialize(self) -> None:
        """插件加载/重载时启动定时邮件检查任务"""
        await self._start_mail_watcher()

    @filter.on_astrbot_loaded()
    async def _start_mail_watcher(self) -> None:
        """启动定时邮件检查任务（幂等：重复调用不会重复启动）"""
        if not self._bool_cfg("auto_summary_enabled", True):
            logger.info("【sendmail】定时邮件总结已禁用")
            return
        if self._mail_watcher_task and not self._mail_watcher_task.done():
            return
        self._mail_watcher_task = asyncio.create_task(self._mail_watcher_loop())
        self._mail_watcher_task.add_done_callback(
            lambda t: (
                logger.error(f"定时邮件总结任务异常退出: {t.exception()}")
                if not t.cancelled() and t.exception()
                else None
            )
        )
        logger.info("【sendmail】定时邮件总结任务已启动")

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

    def _agently_cmd(self) -> list[str]:
        """agently-cli 启动命令（列表形式，供 subprocess 直接执行）。

        优先使用配置指定的可执行文件；否则解析 PATH 中的 npm 全局安装：
        Windows 下 npm 装的是 .cmd shim，经 cmd.exe 执行时命令行中的换行会被
        截断（多行正文会丢行），故解析到 node.exe + 包内主入口（scripts/run.js）
        直接调用，彻底绕过 cmd.exe。
        """
        cfg = str(self.config.get("agently_cli_path", "")).strip()
        if cfg:
            if cfg.lower().endswith((".cmd", ".bat")):
                # 用户指定了 shim，仍尝试解析为 node + 主入口
                inner = self._agently_from_shim(cfg)
                if inner:
                    return inner
            return [cfg]
        exe = shutil.which("agently-cli") or shutil.which("agently-cli.exe") or ""
        if not exe:
            return ["agently-cli"]
        if exe.lower().endswith((".cmd", ".bat")):
            inner = self._agently_from_shim(exe)
            if inner:
                return inner
        return [exe]

    @staticmethod
    def _agently_from_shim(shim: str) -> list[str] | None:
        """将 npm .cmd shim 解析为 [node.exe, 包主入口]；失败返回 None"""
        npm_root = os.path.dirname(os.path.abspath(shim))
        pkg_dir = os.path.join(
            npm_root, "node_modules", "@tencent-qqmail", "agently-cli"
        )
        if not os.path.isdir(pkg_dir):
            return None
        entry = "scripts/run.js"
        pkg_file = os.path.join(pkg_dir, "package.json")
        try:
            with open(pkg_file, encoding="utf-8") as f:
                bin_map = json.load(f).get("bin") or {}
            if bin_map:
                entry = next(iter(bin_map.values()))
        except Exception:
            pass
        main_js = os.path.join(pkg_dir, entry)
        if not os.path.isfile(main_js):
            return None
        node = shutil.which("node") or os.environ.get("NODE_EXE") or ""
        if not node:
            return None
        return [node, main_js]

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
            cmd = self._agently_cmd() + ["message", "+send", "--confirmed"]
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

    def _agently_cli_run(self, args: list[str], timeout: int = 60) -> dict:
        """执行 Agent Mail CLI 并解析 JSON 输出（供邮件读取等操作使用）"""
        cmd = self._agently_cmd() + args
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, encoding="utf-8", timeout=timeout
            )
        except FileNotFoundError:
            raise RuntimeError(self._agently_missing_hint())
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"❌ Agent Mail CLI 执行超时（{timeout} 秒），请重试")
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            raise RuntimeError(f"❌ Agent Mail CLI 执行失败: {err or out or f'exit {proc.returncode}'}")
        try:
            data = json.loads(out)
        except json.JSONDecodeError:
            raise RuntimeError(f"❌ Agent Mail CLI 返回异常: {out[:200] or err[:200] or '无输出'}")
        if not data.get("ok", True):
            raise RuntimeError(
                f"❌ Agent Mail CLI 执行失败: {data.get('message') or data.get('error') or data}"
            )
        return data

    @staticmethod
    def _mail_text(body_html: str, max_chars: int = 500) -> str:
        """邮件 HTML 正文转纯文本（去标签、反转义、压缩空白、截断）"""
        text = re.sub(r"<[^>]+>", " ", body_html or "")
        text = html.unescape(text)
        text = re.sub(r"\s+", " ", text).strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "…"
        return text

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

    @llm_tool(name="read_mail")
    async def ai_read_mail(
        self,
        event: AstrMessageEvent,
        query: str = "",
        limit: int = 5,
        include_body: bool = False,
    ):
        """读取 Agent Mail 邮箱中的邮件，供 AI 总结汇报（仅管理员可用）。

        Args:
            query(string): 搜索关键词，可选；在主题与正文中检索，留空则列出收件箱最新邮件
            limit(number): 返回邮件条数，1-10
            include_body(boolean): 是否同时读取邮件正文，默认 false 只返回标题与摘要
        """
        if not event.is_admin():
            return "❌ 仅管理员可以使用邮件读取功能。"
        limit = max(1, min(int(limit or 5), 10))
        if include_body:
            limit = min(limit, 5)  # 避免超出 CLI 每分钟请求配额
        try:
            if (query or "").strip():
                data = await asyncio.to_thread(
                    self._agently_cli_run,
                    ["message", "+search", "--q", query.strip(), "--limit", str(limit)],
                )
            else:
                data = await asyncio.to_thread(
                    self._agently_cli_run,
                    ["message", "+list", "--dir", "inbox", "--limit", str(limit)],
                )
        except Exception as e:
            return f"❌ 读取邮件失败: {e}"

        items = (((data or {}).get("data") or {}).get("data")) or []
        if not items:
            return "📭 邮箱中没有找到相关邮件。"

        lines: list[str] = []
        for i, it in enumerate(items, 1):
            subject = it.get("subject") or "(无主题)"
            frm = (it.get("from") or {}).get("email") or "?"
            created = (it.get("created_at") or "")[:16].replace("T", " ")
            snippet = (it.get("snippet") or "").strip()
            lines.append(f"{i}. {subject}\n   发件人: {frm}  时间: {created}")
            mid = it.get("message_id")
            if include_body and mid:
                try:
                    full = await asyncio.to_thread(
                        self._agently_cli_run, ["message", "+read", "--id", mid]
                    )
                    body = ((full or {}).get("data") or {}).get("body") or ""
                    lines.append(f"   正文: {self._mail_text(body)}")
                except Exception as e:
                    lines.append(f"   正文读取失败: {e}")
            elif snippet:
                lines.append(f"   摘要: {snippet}")
        return f"📬 邮箱邮件（{len(items)} 封）:\n" + "\n".join(lines)

    # ==================== 定时邮件总结推送 ====================

    async def terminate(self) -> None:
        """插件停用/重载时取消定时任务"""
        if self._mail_watcher_task and not self._mail_watcher_task.done():
            self._mail_watcher_task.cancel()
            self._mail_watcher_task = None

    async def _mail_watcher_loop(self) -> None:
        """定时轮询收件箱，发现新邮件推送到配置目标"""
        while True:
            try:
                await self._check_new_mails()
            except Exception as e:
                logger.error(f"定时检查邮件失败: {e}")
            interval = max(10, self._int_cfg("auto_summary_interval", 30))
            await asyncio.sleep(interval)

    def _load_seen_ids(self) -> set[str]:
        try:
            with open(self._seen_path, encoding="utf-8") as f:
                data = json.load(f)
            return set(data) if isinstance(data, list) else set()
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return set()

    def _save_seen_ids(self, ids: set[str]) -> None:
        try:
            os.makedirs(os.path.dirname(self._seen_path), exist_ok=True)
            with open(self._seen_path, "w", encoding="utf-8") as f:
                json.dump(sorted(ids), f, ensure_ascii=False)
        except OSError as e:
            logger.error(f"保存已读邮件记录失败: {e}")

    @staticmethod
    def _parse_targets(raw: str) -> list[str]:
        """解析推送目标会话（unified_msg_origin，逗号/分号分隔，去空白去重）"""
        seen: set[str] = set()
        out: list[str] = []
        for t in re.split(r"[,，;；]+", raw or ""):
            t = t.strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        return out

    @staticmethod
    def _valid_target_umo(umo: str) -> bool:
        """校验目标会话是否为合法的 unified_msg_origin（平台:消息类型:会话ID）"""
        parts = umo.split(":", 2)
        if len(parts) != 3 or not parts[0] or not parts[2]:
            return False
        return parts[1] in ("GroupMessage", "FriendMessage", "OtherMessage")

    async def _check_new_mails(self) -> None:
        """检查收件箱新邮件（按 message_id 去重），推送到配置目标会话"""
        targets = self._parse_targets(str(self.config.get("auto_summary_targets", "") or ""))
        if not targets:
            # 未配置目标会话：每 10 分钟最多提醒一次，避免日志刷屏
            if time.time() - self._last_targets_warn >= 600:
                self._last_targets_warn = time.time()
                logger.warning(
                    "【sendmail】auto_summary_targets 未配置，定时邮件总结不会推送；"
                    "请在插件配置中填写目标会话（如 云晓:GroupMessage:群号）后重载插件"
                )
            return

        try:
            data = await asyncio.to_thread(
                self._agently_cli_run,
                ["message", "+list", "--dir", "inbox", "--limit", "50"],
            )
        except Exception as e:
            logger.warning(f"【sendmail】读取收件箱失败: {e}")
            return

        items = (((data or {}).get("data") or {}).get("data")) or []
        if not items:
            return

        current_ids = {it.get("message_id") for it in items if it.get("message_id")}
        if not current_ids:
            return

        seen = self._load_seen_ids()
        if not seen:
            # 首次运行：只记录基线，不推送历史邮件
            self._save_seen_ids(current_ids)
            logger.info(f"【sendmail】已建立邮件基线（{len(current_ids)} 封），后续新邮件将推送")
            return

        new_items = [it for it in items if it.get("message_id") and it.get("message_id") not in seen]
        if not new_items:
            return

        max_mails = max(1, min(self._int_cfg("auto_summary_max_mails", 5), 10))
        new_items = new_items[:max_mails]
        seen.update(current_ids)
        self._save_seen_ids(seen)

        targets = [t for t in targets if self._valid_target_umo(t)]
        if not targets:
            logger.warning(
                "【sendmail】auto_summary_targets 无合法目标会话"
                "（格式: 平台:GroupMessage/FriendMessage:会话ID，如 云晓:GroupMessage:群号）"
            )
            return

        text = await self._build_summary_text(new_items)
        chain = MessageChain([Plain(text)])
        for target in targets:
            try:
                await self.context.send_message(target, chain)
            except Exception as e:
                logger.error(f"【sendmail】推送到 {target} 失败: {e}")
        logger.info(f"【sendmail】已推送 {len(new_items)} 封新邮件总结到 {len(targets)} 个会话")

    async def _build_summary_text(self, items: list[dict]) -> str:
        """构造推送文本：LLM 总结（可用时）+ 每封邮件明细"""
        lines: list[str] = [f"📬 新邮件提醒（{len(items)} 封）"]
        detail: list[str] = []
        for i, it in enumerate(items, 1):
            subject = it.get("subject") or "(无主题)"
            frm = (it.get("from") or {}).get("email") or "?"
            created = (it.get("created_at") or "")[:16].replace("T", " ")
            snippet = (it.get("snippet") or "").strip()
            detail.append(f"【{i}】{subject}\n   发件人: {frm}  时间: {created}\n   摘要: {snippet or '（无摘要）'}")

        if self._bool_cfg("auto_summary_llm", True):
            try:
                summary = await self._llm_summary(detail)
                if summary:
                    lines.append(f"🤖 AI 总结:\n{summary}\n")
            except Exception as e:
                logger.warning(f"【sendmail】LLM 总结失败，回退明细推送: {e}")
        lines.extend(detail)
        return "\n".join(lines)

    async def _llm_summary(self, detail: list[str]) -> str:
        """调用 AstrBot 当前聊天 provider 生成邮件总结"""
        if not self.context:
            raise RuntimeError("context 未初始化")
        prov = self.context.get_using_provider()
        if prov is None:
            raise RuntimeError("未找到可用的 LLM provider")
        prompt = (
            "你是邮件助手。请将下面的新邮件内容总结成一段简洁的中文总结"
            "（要点式亦可），不要遗漏发件人与主题信息：\n\n"
            + "\n".join(detail)
        )
        resp = await asyncio.wait_for(prov.text_chat(prompt=prompt), timeout=60)
        chain = getattr(resp, "result_chain", None)
        text = "".join(getattr(c, "text", "") for c in (chain.chain if chain else [])).strip()
        if getattr(resp, "role", "") == "err":
            raise RuntimeError(text or "LLM 返回错误")
        return text


class _UrlPlaceholder:
    """URL 附件占位：保存 URL 与文件名，发送线程内下载后替换为真实 MIME 附件"""

    def __init__(self, url: str, name: str):
        self.url = url
        self.name = name
