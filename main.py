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
import datetime
import html
import json
import os
import re
import shlex
import shutil
import smtplib
import subprocess
import tempfile
import time
import urllib.parse
import uuid
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

# 内置邮件模板（模板快捷发送）。AstrBot 以 data.plugins.xxx.main 包路径加载插件，
# 故优先相对导入；测试等场景兜底为顶层导入
try:
    from . import mail_templates as _mail_templates
except (ImportError, ValueError):
    import mail_templates as _mail_templates  # type: ignore

MAIL_TEMPLATES = _mail_templates.MAIL_TEMPLATES
render_template = _mail_templates.render_template

PLUGIN_NAME = "astrbot_plugin_sendmail"
PLUGIN_AUTHOR = "云晓"
PLUGIN_DESC = "邮件助手：管理员让 AI 发送/读取邮件，定时总结推送新邮件"
PLUGIN_VERSION = "1.4.0"

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
# 子命令识别（rule/summary/template/send），负向断言避免误吞 send@qq.com 这类普通收件人
SUBCOMMAND_RE = re.compile(r"^(rule|summary|template|send)(?![@\w-])(?:\s+(.*))?$", re.I | re.S)

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
    "模板快捷发送：/邮件 send <模板名> <收件人> 占位符=值 ...\n"
    "  /邮件 template list 查看模板与占位符说明\n"
    "转发规则：/邮件 rule add 发件人=关键词 主题=关键词 转发=目标邮箱\n"
    "  /邮件 rule list / rule remove <规则ID>（新邮件自动匹配转发）\n"
    "定时摘要：/邮件 summary now 立即检查新邮件并推送摘要\n"
    "  （定时推送由配置 summary_schedule_mode/time/weekday + summary_targets 控制）\n"
    "━━━━━━━━━━━━━━━━━━━━━━\n"
    "AI 联动：对 AI 说「帮我发邮件到 xxx，主题 yyy，内容 zzz」\n"
    "即会由 AI 调用 send_mail 工具自动发送；说「看看我的邮件」\n"
    "AI 会调用 read_mail 工具读取邮箱并总结（均仅管理员生效）"
)

RULE_HELP = (
    "📮 转发规则用法（仅管理员）\n"
    "  /邮件 rule add 发件人=关键词 主题=关键词 [收件箱=inbox] 转发=目标邮箱\n"
    "  /邮件 rule list\n"
    "  /邮件 rule remove <规则ID>\n"
    "条件之间为「或」关系：任一条件命中即转发（SMTP 通道，发件人为配置账号）。\n"
    "规则持久化在 plugin_data/astrbot_plugin_sendmail/forward_rules.json"
)

SUMMARY_HELP = (
    "📮 定时摘要推送用法（仅管理员）\n"
    "  /邮件 summary now —— 立即检查新邮件并生成摘要推送到 summary_targets\n"
    "相关配置：\n"
    "  summary_push_enabled —— 是否启用定时摘要推送（默认 true）\n"
    "  summary_schedule_mode —— daily（每天）/ weekly（每周）\n"
    "  summary_schedule_time —— 触发时间 HH:MM（默认 09:00）\n"
    "  summary_schedule_weekday —— 每周模式触发日（1=周一 .. 7=周日）\n"
    "  summary_targets —— 推送目标会话（格式 平台:GroupMessage/FriendMessage:会话ID，逗号分隔）"
)

TEMPLATE_HELP = (
    "📧 模板快捷发送用法（仅管理员）\n"
    "  /邮件 send <模板名> <收件人> 占位符=值 占位符=值 ...\n"
    "  /邮件 template list —— 查看全部模板与占位符\n"
    "  /邮件 template <模板名> —— 查看单个模板详情\n"
    "示例：/邮件 send 请假申请 a@b.com 名字=张三 日期=2026-08-18\n"
    "  原因=家中有事 天数=1 领导称呼=王经理 同事=李四"
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
        # 插件数据目录（plugin_data/astrbot_plugin_sendmail/）
        self._data_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "plugin_data",
            PLUGIN_NAME,
        )
        self._history_path = os.path.join(self._data_dir, "send_log.json")
        self._seen_path = os.path.join(self._data_dir, "seen_mail_ids.json")
        self._rules_path = os.path.join(self._data_dir, "forward_rules.json")
        self._load_history()
        self._rules: list[dict] = self._load_rules()
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
            tmp = self._history_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._history, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._history_path)
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
                size_mb = max(1, self._int_cfg("attach_max_mb", 10))
                try:
                    if os.path.getsize(value) > size_mb * 1024 * 1024:
                        logger.warning(
                            f"本地附件超过 {size_mb}MB 上限，已跳过: {value}"
                        )
                        continue
                except OSError as e:
                    logger.warning(f"读取本地附件大小失败，已跳过: {value}: {e}")
                    continue
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
                # npm 允许 "bin" 为字符串或对象；字符串时直接作为入口
                entry = (
                    bin_map
                    if isinstance(bin_map, str)
                    else next(iter(bin_map.values()))
                )
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
        """邮件发送主入口（仅管理员）：普通发送 / rule / summary / template / send 子命令"""
        if not event.is_admin():
            return self._send_text(event, "❌ 仅管理员可使用邮件发送功能。")

        args = (getattr(event, "message_str", "") or "").strip()
        args = re.sub(r"^[\\/／]?\s*(邮件|发邮件|mail)\s*", "", args, flags=re.I).strip()
        if not args or args in ("帮助", "help", "Help", "HELP"):
            return self._send_text(event, HELP_TEXT)

        # 子命令路由：转发规则 / 定时摘要 / 模板
        sub = SUBCOMMAND_RE.match(args)
        if sub:
            name, rest = sub.group(1).lower(), (sub.group(2) or "").strip()
            if name == "rule":
                return self._send_text(event, self._cmd_rule(rest))
            if name == "summary":
                return self._send_text(event, await self._cmd_summary(rest))
            if name == "template":
                return self._send_text(event, self._cmd_template(rest))
            if name == "send":
                return self._send_text(event, await self._cmd_template_send(event, rest))

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
                    # _build_mime 含本地附件读盘，放入线程避免阻塞事件循环
                    msg = await asyncio.to_thread(
                        self._build_mime, recipients, subject, body, attachments
                    )
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
        try:
            limit = max(1, min(int(limit or 5), 10))
        except (TypeError, ValueError):
            limit = 5
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
        """定时轮询收件箱：按配置间隔推送新邮件提醒，并在调度时刻触发定时摘要推送"""
        next_summary_at = None
        while True:
            try:
                # 定时摘要推送：到点检查新邮件并生成摘要推送到 summary_targets
                if self._bool_cfg("summary_push_enabled", True):
                    now = datetime.datetime.now()
                    if next_summary_at is None:
                        next_summary_at = self._next_summary_at(now)
                    if now >= next_summary_at:
                        try:
                            await self._run_scheduled_summary()
                        except Exception as e:
                            logger.error(f"定时摘要推送失败: {e}")
                        next_summary_at = self._next_summary_at(now)
                await self._check_new_mails()
            except Exception as e:
                logger.error(f"定时检查邮件失败: {e}")
            interval = max(10, self._int_cfg("auto_summary_interval", 30))
            await asyncio.sleep(interval)

    def _next_summary_at(self, now: datetime.datetime) -> datetime.datetime:
        """计算下一次定时摘要推送的触发时间（基于配置的调度模式与时间）"""
        mode = str(self.config.get("summary_schedule_mode", "daily") or "").strip().lower()
        if mode != "weekly":
            mode = "daily"
        t = str(self.config.get("summary_schedule_time", "09:00") or "09:00").strip()
        m = re.match(r"^(\d{1,2}):(\d{1,2})$", t)
        hour, minute = (int(m.group(1)), int(m.group(2))) if m else (9, 0)
        hour = min(max(hour, 0), 23)
        minute = min(max(minute, 0), 59)
        candidate = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if mode == "daily":
            if candidate <= now:
                candidate += datetime.timedelta(days=1)
            return candidate
        # weekly：每周 weekday 触发（配置 1=周一 .. 7=周日，映射 python weekday 0-6）
        wd = max(1, min(self._int_cfg("summary_schedule_weekday", 1), 7))
        target = (wd - 1) % 7
        days_ahead = (target - candidate.weekday()) % 7
        if days_ahead == 0 and candidate <= now:
            days_ahead = 7
        return candidate + datetime.timedelta(days=days_ahead)

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
            tmp = self._seen_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(sorted(ids), f, ensure_ascii=False)
            os.replace(tmp, self._seen_path)
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
        """检查收件箱新邮件（按 message_id 去重），推送到配置目标会话；并按转发规则自动转发"""
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

        status, new_items = await self._fetch_new_items()
        if status != "ok":
            return

        # 转发规则：命中规则的新邮件自动用 SMTP 转发（失败仅记日志，不阻塞主流程）
        await self._try_forward_mails(new_items)

        targets = [t for t in targets if self._valid_target_umo(t)]
        if not targets:
            logger.warning(
                "【sendmail】auto_summary_targets 无合法目标会话"
                "（格式: 平台:GroupMessage/FriendMessage:会话ID，如 云晓:GroupMessage:群号）"
            )
            # 目标全非法：不消费邮件，下一轮仍可重试
            return

        max_mails = max(1, min(self._int_cfg("auto_summary_max_mails", 5), 10))
        new_items = new_items[:max_mails]

        text = await self._build_summary_text(new_items)
        chain = MessageChain([Plain(text)])
        ok_targets = 0
        for target in targets:
            try:
                await self.context.send_message(target, chain)
                ok_targets += 1
            except Exception as e:
                logger.error(f"【sendmail】推送到 {target} 失败: {e}")
        if ok_targets == 0:
            logger.warning(
                "【sendmail】全部推送目标发送失败，不标记已读，下一轮将重试"
            )
            return
        # 仅标记实际推送的邮件，避免积压超过上限的部分被静默丢弃
        seen = self._load_seen_ids()
        seen.update(it.get("message_id") for it in new_items)
        self._save_seen_ids(seen)
        logger.info(f"【sendmail】已推送 {len(new_items)} 封新邮件总结到 {len(targets)} 个会话")

    async def _fetch_new_items(self) -> tuple[str, list[dict]]:
        """读取收件箱并按 seen 机制（message_id）去重。

        返回 (状态, 新邮件列表)：
        - ("error", [])：读取失败（已记录日志）
        - ("baseline", [])：首次运行仅建立基线，不推送历史邮件
        - ("empty", [])：无未推送过的新邮件
        - ("ok", [items...])：存在新邮件（列表非空）
        """
        try:
            data = await asyncio.to_thread(
                self._agently_cli_run,
                ["message", "+list", "--dir", "inbox", "--limit", "50"],
            )
        except Exception as e:
            logger.warning(f"【sendmail】读取收件箱失败: {e}")
            return "error", []

        items = (((data or {}).get("data") or {}).get("data")) or []
        current_ids = {it.get("message_id") for it in items if it.get("message_id")}
        if not items or not current_ids:
            return "empty", []

        seen = self._load_seen_ids()
        if not seen:
            # 首次运行：只记录基线，不推送历史邮件
            self._save_seen_ids(current_ids)
            logger.info(f"【sendmail】已建立邮件基线（{len(current_ids)} 封），后续新邮件将推送")
            return "baseline", []

        new_items = [it for it in items if it.get("message_id") and it.get("message_id") not in seen]
        if not new_items:
            return "empty", []
        return "ok", new_items

    # ==================== 转发规则 ====================

    async def _try_forward_mails(self, new_items: list[dict]) -> None:
        """按转发规则逐条匹配新邮件，命中则用 SMTP 自动转发（失败仅记日志，不阻塞主流程）"""
        rules = [
            r for r in self._rules
            if r.get("enabled", True) and (r.get("action_to") or "").strip()
        ]
        if not rules:
            return
        for item in new_items:
            for rule in rules:
                if self._rule_matches(rule, item):
                    to = str(rule["action_to"]).strip()
                    try:
                        await asyncio.to_thread(self._forward_one_sync, item, to)
                        logger.info(
                            f"【sendmail】已按规则 {rule.get('id')} 转发邮件"
                            f"「{item.get('subject') or '(无主题)'}」到 {to}"
                        )
                    except Exception as e:
                        logger.error(
                            f"【sendmail】按规则 {rule.get('id')} 转发邮件失败"
                            f"（目标 {to}）: {e}"
                        )
                    break  # 命中一条规则即转发，不重复转发

    def _forward_one_sync(self, item: dict, to: str) -> None:
        """同步转发一封邮件：发件人为配置账号（SMTP），正文附原邮件文本摘要"""
        subject = f"[转发] {item.get('subject') or '(无主题)'}"
        body = self._build_forward_body(item)
        max_mb = max(1, self._int_cfg("attach_max_mb", 10))
        msg = self._build_mime([to], subject, body, [])
        self._send_sync(msg, max_mb)

    @staticmethod
    def _build_forward_body(item: dict, max_chars: int = 2000) -> str:
        """构造转发正文：原始邮件的文本摘要（发件人/主题/时间/摘要）"""
        frm_obj = item.get("from")
        if isinstance(frm_obj, dict):
            name = frm_obj.get("name") or ""
            frm = frm_obj.get("email") or "?"
        else:
            name, frm = "", str(frm_obj or "?")
        lines = ["这是一封由邮件助手自动转发的邮件，原邮件内容如下：", ""]
        lines.append(f"发件人: {f'{name} <{frm}>' if name else frm}")
        lines.append(f"主题: {item.get('subject') or '(无主题)'}")
        lines.append(f"时间: {(item.get('created_at') or '')[:16].replace('T', ' ')}")
        snippet = (item.get("snippet") or "").strip()
        if snippet:
            lines.append(f"摘要: {snippet}")
        body = "\n".join(lines)
        return body[:max_chars]

    def _rule_matches(self, rule: dict, mail: dict) -> bool:
        """规则匹配：发件人包含 / 主题包含 / 收件箱名，任一条件命中即匹配"""
        if not rule.get("enabled", True):
            return False
        frm_text = self._mail_from_text(mail)
        subject = str(mail.get("subject") or "")
        folder = str(mail.get("folder") or mail.get("folder_name") or "")
        conds: list[bool] = []
        fc = str(rule.get("from_contains") or "").strip()
        if fc:
            conds.append(fc.lower() in frm_text.lower())
        sc = str(rule.get("subject_contains") or "").strip()
        if sc:
            conds.append(sc.lower() in subject.lower())
        fl = str(rule.get("folder") or "").strip()
        if fl:
            conds.append(fl.lower() == folder.lower())
        if not conds:
            # 没有任何条件：视为永不匹配，避免误转发
            return False
        return any(conds)

    @staticmethod
    def _mail_from_text(mail: dict) -> str:
        """提取邮件发件人文本（name + email）"""
        frm_obj = mail.get("from")
        if isinstance(frm_obj, dict):
            return f"{frm_obj.get('name') or ''} {frm_obj.get('email') or ''}".strip()
        return str(frm_obj or "")

    # ==================== 规则持久化（独立 JSON 原子写） ====================

    def _load_rules(self) -> list[dict]:
        """加载转发规则（独立 JSON 文件，损坏/缺失时返回空列表）"""
        try:
            with open(self._rules_path, encoding="utf-8") as f:
                data = json.load(f)
            rules = data.get("rules") if isinstance(data, dict) else data
            return [r for r in rules if isinstance(r, dict)] if isinstance(rules, list) else []
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save_rules(self) -> None:
        """保存转发规则（独立 JSON，原子写：先写临时文件再 os.replace）"""
        try:
            os.makedirs(os.path.dirname(self._rules_path), exist_ok=True)
            tmp = self._rules_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"rules": self._rules}, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self._rules_path)
        except OSError as e:
            logger.error(f"保存转发规则失败: {e}")

    def _cmd_rule(self, rest: str) -> str:
        """转发规则管理命令：add / list / remove"""
        if not rest:
            return RULE_HELP
        action, _, args2 = rest.partition(" ")
        action = action.strip().lower()
        args2 = args2.strip()
        if action == "list":
            return self._rule_list_text()
        if action == "add":
            return self._rule_add(args2)
        if action in ("remove", "del"):
            return self._rule_remove(args2)
        return RULE_HELP

    def _rule_add(self, args2: str) -> str:
        """添加转发规则：mail rule add 发件人=关键词 主题=关键词 [收件箱=inbox] 转发=目标邮箱"""
        kv = self._parse_kv(args2)
        to = (kv.get("to") or kv.get("转发") or kv.get("转发到") or "").strip()
        if not to or not EMAIL_RE.match(to):
            return (
                "❌ 转发目标邮箱无效。用法: /邮件 rule add 发件人=关键词 主题=关键词 转发=目标邮箱\n"
                "可选条件: 收件箱=inbox"
            )
        from_contains = (kv.get("from") or kv.get("发件人") or "").strip()
        subject_contains = (kv.get("subject") or kv.get("主题") or "").strip()
        folder = (kv.get("folder") or kv.get("收件箱") or "").strip()
        if not (from_contains or subject_contains or folder):
            return "❌ 规则至少需要一个条件（发件人=/主题=/收件箱=）。"
        rule = {
            "id": uuid.uuid4().hex[:8],
            "from_contains": from_contains,
            "subject_contains": subject_contains,
            "folder": folder,
            "action_to": to,
            "enabled": True,
            "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        self._rules.append(rule)
        self._save_rules()
        conds = "、".join(
            c for c in (
                f"发件人包含「{from_contains}」" if from_contains else "",
                f"主题包含「{subject_contains}」" if subject_contains else "",
                f"收件箱「{folder}」" if folder else "",
            ) if c
        )
        return f"✅ 转发规则已添加（ID: {rule['id']}）\n条件: {conds}\n动作: 转发到 {to}\n「/邮件 rule list」查看全部规则。"

    def _rule_list_text(self) -> str:
        """规则列表文本"""
        if not self._rules:
            return "📭 暂无转发规则。用「/邮件 rule add 发件人=关键词 转发=目标邮箱」添加。"
        lines = [f"📮 转发规则（共 {len(self._rules)} 条）"]
        for i, r in enumerate(self._rules, 1):
            conds = []
            if r.get("from_contains"):
                conds.append(f"发件人包含「{r['from_contains']}」")
            if r.get("subject_contains"):
                conds.append(f"主题包含「{r['subject_contains']}」")
            if r.get("folder"):
                conds.append(f"收件箱「{r['folder']}」")
            state = "✅ 启用" if r.get("enabled", True) else "⏸ 停用"
            lines.append(
                f"{i}. ID {r.get('id', '-')} | {'、'.join(conds) or '（无条件）'}"
                f" | 转发到 {r.get('action_to', '?')} | {state}"
            )
        lines.append("「/邮件 rule remove <规则ID>」删除规则")
        return "\n".join(lines)

    def _rule_remove(self, args2: str) -> str:
        """删除规则：mail rule remove <规则ID>"""
        rid = (args2 or "").strip()
        if not rid:
            return "❌ 用法: /邮件 rule remove <规则ID>（ID 用「/邮件 rule list」查看）"
        before = len(self._rules)
        self._rules = [r for r in self._rules if str(r.get("id")) != rid]
        if len(self._rules) == before:
            return f"❌ 未找到 ID 为 {rid} 的规则。"
        self._save_rules()
        return f"✅ 已删除转发规则 {rid}。"

    # ==================== 定时摘要推送 ====================

    async def _run_scheduled_summary(self, manual: bool = False) -> str:
        """执行一次摘要推送：检查新邮件并生成摘要推送到 summary_targets 配置的目标。

        已推送的邮件按现有 seen 机制去重；manual=True（/邮件 summary now）时
        返回完整文本（含摘要内容），定时触发时返回值仅作日志记录。
        """
        if not self.context:
            return "❌ 插件 context 未初始化，无法推送摘要。"
        targets = self._parse_targets(str(self.config.get("summary_targets", "") or ""))
        targets = [t for t in targets if self._valid_target_umo(t)]
        if not targets:
            return (
                "❌ 未配置合法的 summary_targets（格式: 平台:GroupMessage/FriendMessage:会话ID，"
                "多个用逗号分隔，如 云晓:GroupMessage:群号）。"
            )

        status, new_items = await self._fetch_new_items()
        if status == "error":
            return "❌ 读取收件箱失败，请检查 agently-cli 安装与授权（详情见后台日志）。"
        if status != "ok" or not new_items:
            return "📭 没有新邮件，跳过摘要推送。"

        max_mails = max(1, min(self._int_cfg("auto_summary_max_mails", 5), 10))
        new_items = new_items[:max_mails]

        text = await self._build_summary_text(new_items)
        chain = MessageChain([Plain(text)])
        ok_targets = 0
        for target in targets:
            try:
                await self.context.send_message(target, chain)
                ok_targets += 1
            except Exception as e:
                logger.error(f"【sendmail】摘要推送到 {target} 失败: {e}")
        if ok_targets == 0:
            logger.warning("【sendmail】全部摘要推送目标发送失败，未标记已读，下一轮将重试")
            return "❌ 全部推送目标发送失败，本次未标记已读（下轮自动重试）。"

        seen = self._load_seen_ids()
        seen.update(it.get("message_id") for it in new_items)
        self._save_seen_ids(seen)
        result = f"✅ 已将 {len(new_items)} 封新邮件摘要推送到 {ok_targets} 个会话。"
        logger.info(f"【sendmail】定时摘要推送完成: {result}")
        if manual:
            return f"{result}\n\n{text}"
        return result

    async def _cmd_summary(self, rest: str) -> str:
        """定时摘要手动触发命令：mail summary now"""
        if rest.strip().lower() not in ("now", "立即", "现在"):
            return SUMMARY_HELP
        return await self._run_scheduled_summary(manual=True)

    # ==================== 模板快捷发送 ====================

    @staticmethod
    def _parse_kv(raw: str) -> dict:
        """解析空格分隔的 k=v 键值对（值可用引号包裹以包含空格），返回 {k: v}"""
        out: dict = {}
        try:
            tokens = shlex.split(raw or "")
        except ValueError:
            tokens = (raw or "").split()
        for tok in tokens:
            if "=" in tok:
                k, _, v = tok.partition("=")
                out[k.strip()] = v.strip()
            else:
                out[tok.strip()] = ""
        return out

    def _cmd_template(self, rest: str) -> str:
        """模板查看命令：template list / template <模板名>"""
        name = rest.strip()
        if not name or name.lower() in ("list", "列表"):
            lines = [f"📧 邮件模板（共 {len(MAIL_TEMPLATES)} 个）"]
            for tname, tpl in MAIL_TEMPLATES.items():
                phs = "、".join(
                    f"{{{p}}}（{tpl.get('placeholders_desc', {}).get(p, '')}）"
                    for p in (tpl.get("placeholders") or [])
                )
                lines.append(f"▸ {tname}：{tpl.get('desc')}\n  占位符: {phs}")
            lines.append("发送: /邮件 send <模板名> <收件人> 占位符=值 ...")
            return "\n".join(lines)
        tpl = MAIL_TEMPLATES.get(name)
        if not tpl:
            return f"❌ 模板「{name}」不存在。可用模板: {', '.join(MAIL_TEMPLATES)}"
        lines = [
            f"📧 模板「{name}」：{tpl.get('desc')}",
            f"主题: {tpl.get('subject')}",
            "正文:",
            str(tpl.get("body") or ""),
            "占位符: " + "、".join(f"{{{p}}}" for p in (tpl.get("placeholders") or [])),
        ]
        lines.append(
            "示例: /邮件 send " + name + " a@b.com "
            + " ".join(f"{p}=值" for p in (tpl.get("placeholders") or [])[:3])
        )
        return "\n".join(lines)

    async def _cmd_template_send(self, event: AstrMessageEvent, rest: str) -> str:
        """模板快捷发送：mail send <模板名> <收件人> [占位符=值 ...]（走 _dispatch_send 链路）"""
        m = re.match(r"^(\S+)\s+(\S+)(?:\s+(.*))?$", rest or "", re.S)
        if not m:
            return TEMPLATE_HELP
        name, recipients_raw, kv_raw = m.group(1).strip(), m.group(2).strip(), (m.group(3) or "").strip()
        tpl = MAIL_TEMPLATES.get(name)
        if not tpl:
            return (
                f"❌ 模板「{name}」不存在。可用模板: {', '.join(MAIL_TEMPLATES)}\n"
                "（/邮件 template list 查看模板与占位符说明）"
            )
        recipients = self._parse_recipients(recipients_raw)
        if not recipients:
            return "❌ 收件人无效。用法: /邮件 send <模板名> <收件人> 占位符=值 ..."
        values = self._parse_kv(kv_raw)
        placeholders = tpl.get("placeholders") or []
        missing = [p for p in placeholders if not (values.get(p) or "").strip()]
        if missing:
            return (
                "❌ 缺少模板占位符: " + "、".join(f"「{p}」" for p in missing) + "\n"
                "用法: /邮件 send " + name + " <收件人> 占位符=值 ..."
                "（/邮件 template list 查看全部占位符说明）"
            )
        unknown = [k for k in values if k not in placeholders]
        if unknown:
            logger.warning(f"【sendmail】模板发送忽略未知占位符: {unknown}")
        subject, body = render_template(tpl, values)
        return await self._dispatch_send(event, recipients, subject, body, [])

    async def _build_summary_text(self, items: list[dict]) -> str:
        """构造推送文本：LLM 总结（可用时）+ 每封邮件明细"""
        lines: list[str] = [f"📬 新邮件提醒（{len(items)} 封）"]
        detail: list[str] = []
        for i, it in enumerate(items, 1):
            subject = it.get("subject") or "(无主题)"
            frm_obj = it.get("from")
            frm = (
                frm_obj.get("email")
                if isinstance(frm_obj, dict)
                else str(frm_obj or "?")
            )
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
