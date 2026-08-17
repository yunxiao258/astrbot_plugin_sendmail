# -*- coding: utf-8 -*-
"""sendmail 插件单元测试：命令解析、MIME 构建、发送流程、权限与频率限制"""
import asyncio
import datetime
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

from astrbot.api.all import MessageChain
from astrbot.api.message_components import Plain
from astrbot.core.provider.entities import LLMResponse

sys.path.insert(0, r"D:\astrbot\data\plugins")
sys.path.insert(0, r"D:\astrbot\data\plugins\astrbot_plugin_sendmail")

from astrbot_plugin_sendmail.main import (  # noqa: E402
    MAIL_TEMPLATES,
    SMTP_PRESETS,
    SendMailPlugin,
    _UrlPlaceholder,
    _to_html,
    render_template,
)
from astrbot_plugin_sendmail.mail_templates import render_text  # noqa: E402


class FakeEvent:
    def __init__(self, message_str="", is_admin=True):
        self.message_str = message_str
        self._is_admin = is_admin

    def is_admin(self):
        return self._is_admin

    def chain_result(self, chain):
        return chain


def reply_text(result):
    return "".join(getattr(c, "text", "") for c in result)


class FakeSMTP:
    """smtplib.SMTP_SSL/SMTP 替身：记录调用参数"""

    instances = []

    def __init__(self, *args, **kwargs):
        self.args = args
        self.login_args = None
        self.sendmail_args = None
        self.quit_called = False
        FakeSMTP.instances.append(self)

    def login(self, user, auth):
        self.login_args = (user, auth)

    def sendmail(self, sender, recipients, message):
        self.sendmail_args = (sender, recipients, message)

    def starttls(self):
        pass

    def quit(self):
        self.quit_called = True


class FakeResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass


class FakeClient:
    """httpx.Client 替身：get 返回固定内容或抛错"""

    def __init__(self, data, raise_on=()):
        self.data = data
        self.raise_on = raise_on

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        if url in self.raise_on:
            raise RuntimeError("network down")
        return FakeResp(self.data)


def make_plugin(**overrides):
    cfg = {
        "smtp_provider": "qq",
        "smtp_host": "",
        "smtp_port": 465,
        "smtp_ssl": True,
        "smtp_user": "sender@qq.com",
        "smtp_auth_code": "authcode123",
        "mail_from_name": "AstrBot",
        "mail_html": True,
        "body_max_chars": 5000,
        "attach_max_mb": 10,
        "send_interval": 0,
    }
    cfg.update(overrides)
    p = SendMailPlugin(None, cfg)
    p._last_send_at = 0.0
    p._history = []  # 隔离真实发送日志，避免测试间状态泄漏
    # 历史记录仅内存，不写真实 plugin_data
    def append_mem(record):
        p._history.append(record)
        p._history = p._history[-50:]
    p._append_history = append_mem
    return p


class FakeContext:
    """Star.context 替身：记录推送与 LLM provider"""

    def __init__(self):
        self.sent = []
        self.provider = None

    async def send_message(self, session, chain):
        self.sent.append((session, "".join(getattr(c, "text", "") for c in chain.chain)))
        return True

    def get_using_provider(self, umo=None):
        return self.provider


class FakeProvider:
    """LLM provider 替身：text_chat 返回固定总结或抛错"""

    def __init__(self, result="这是 AI 总结", err=False):
        self.result = result
        self.err = err

    async def text_chat(self, prompt=None, **kwargs):
        if self.err:
            raise RuntimeError("LLM 不可用")
        return LLMResponse(role="assistant", result_chain=MessageChain([Plain(self.result)]))


# ==================== 解析 ====================

class TestParse(unittest.TestCase):
    def test_split_fields_three(self):
        p = make_plugin()
        self.assertEqual(p._split_fields("a@b.com | 主题 | 正文"), ("a@b.com", "主题", "正文"))

    def test_split_fields_body_contains_pipe(self):
        p = make_plugin()
        self.assertEqual(
            p._split_fields("a@b.com | 主题 | 正文 | 还有 | 管道"),
            ("a@b.com", "主题", "正文 | 还有 | 管道"),
        )

    def test_split_fields_two_parts(self):
        p = make_plugin()
        self.assertEqual(p._split_fields("a@b.com | 主题"), ("a@b.com", "主题", ""))

    def test_split_fields_single_part(self):
        p = make_plugin()
        self.assertEqual(p._split_fields("a@b.com"), ("a@b.com", "", ""))

    def test_extract_attachments(self):
        p = make_plugin()
        files, rest = p._extract_attachments("a@b.com | 主题 | 正文 --附件=https://x.com/a.pdf,/tmp/b.zip")
        self.assertEqual(files, ["https://x.com/a.pdf", "/tmp/b.zip"])
        self.assertEqual(rest, "a@b.com | 主题 | 正文")

    def test_extract_attachments_short_form(self):
        p = make_plugin()
        files, rest = p._extract_attachments("正文 -a=/tmp/a.txt")
        self.assertEqual(files, ["/tmp/a.txt"])
        self.assertEqual(rest, "正文")

    def test_extract_attachments_none(self):
        p = make_plugin()
        files, rest = p._extract_attachments("a@b.com | 主题 | 正文")
        self.assertEqual(files, [])
        self.assertEqual(rest, "a@b.com | 主题 | 正文")

    def test_recipients_parse_and_filter(self):
        p = make_plugin()
        got = p._parse_recipients("a@b.com, b@c.com，d@e.com; 无效邮箱, a@b.com")
        self.assertEqual(got, ["a@b.com", "b@c.com", "d@e.com"])

    def test_recipients_empty(self):
        p = make_plugin()
        self.assertEqual(p._parse_recipients(""), [])
        self.assertEqual(p._parse_recipients("不是邮箱"), [])

    def test_classify_attachment(self):
        p = make_plugin()
        self.assertEqual(p._classify_attachment("https://x.com/a.pdf")[0], "url")
        self.assertEqual(p._classify_attachment("HTTP://X.COM/A.PDF")[0], "url")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            path = f.name
        try:
            self.assertEqual(p._classify_attachment(path), ("file", path))
        finally:
            os.unlink(path)
        self.assertEqual(p._classify_attachment("/no/such/file.zip"), ("invalid", "/no/such/file.zip"))

    def test_filename_from_url(self):
        self.assertEqual(SendMailPlugin._filename_from_url("https://x.com/report.pdf"), "report.pdf")
        self.assertEqual(SendMailPlugin._filename_from_url("https://x.com/a%20b.txt"), "a b.txt")
        self.assertEqual(SendMailPlugin._filename_from_url("https://x.com/"), "attachment.bin")
        self.assertEqual(SendMailPlugin._filename_from_url("https://x.com/dl?id=1"), "attachment.bin")


# ==================== HTML 转换 ====================

class TestToHtml(unittest.TestCase):
    def test_plain_text_escaped(self):
        out = _to_html("5 < 10 & 换行\n第二行")
        self.assertIn("&lt;", out)
        self.assertIn("&amp;", out)
        self.assertIn("<br>", out)

    def test_html_passed_through(self):
        raw = "<b>加粗</b><br>第二行"
        self.assertEqual(_to_html(raw), raw)


# ==================== MIME 构建 ====================

class TestBuildMime(unittest.TestCase):
    def test_html_body(self):
        p = make_plugin()
        msg = p._build_mime(["a@b.com"], "主题", "你好", [])
        self.assertEqual(msg["To"], "a@b.com")
        self.assertEqual(str(msg["Subject"]), "主题")
        self.assertIn("text/html", msg.as_string())
        body = msg.get_payload()[0].get_payload(decode=True).decode("utf-8")
        self.assertIn("你好", body)
        self.assertIn("<p>", body)

    def test_plain_body_when_disabled(self):
        p = make_plugin(mail_html=False)
        msg = p._build_mime(["a@b.com"], "主题", "你好", [])
        self.assertIn("text/plain", msg.as_string())

    def test_local_file_attachment(self):
        p = make_plugin()
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(b"PDFDATA")
            path = f.name
        try:
            msg = p._build_mime(["a@b.com"], "主题", "正文", [("file", path)])
            parts = msg.get_payload()
            # 正文 + 附件
            self.assertEqual(len(parts), 2)
            self.assertEqual(parts[1].get_payload(decode=True), b"PDFDATA")
            self.assertIn("attachment", parts[1]["Content-Disposition"])
        finally:
            os.unlink(path)

    def test_url_attachment_placeholder(self):
        p = make_plugin()
        msg = p._build_mime(
            ["a@b.com"], "主题", "正文", [("url", "https://x.com/report.pdf")]
        )
        parts = msg.get_payload()
        self.assertIsInstance(parts[1], _UrlPlaceholder)
        self.assertEqual(parts[1].name, "report.pdf")


# ==================== 发送 ====================

class TestSendSync(unittest.TestCase):
    def test_send_success_with_url_attachment(self):
        p = make_plugin()
        msg = p._build_mime(
            ["a@b.com", "c@d.com"], "主题", "正文", [("url", "https://x.com/r.pdf")]
        )
        FakeSMTP.instances = []
        with mock.patch("astrbot_plugin_sendmail.main.smtplib.SMTP_SSL", FakeSMTP), \
             mock.patch("astrbot_plugin_sendmail.main.httpx.Client",
                        lambda **k: FakeClient(b"PDFBYTES")):
            failed = p._send_sync(msg, 10)
        self.assertEqual(failed, [])
        inst = FakeSMTP.instances[0]
        self.assertEqual(inst.login_args, ("sender@qq.com", "authcode123"))
        self.assertEqual(inst.sendmail_args[1], ["a@b.com", "c@d.com"])
        self.assertIn("text/html", inst.sendmail_args[2])
        self.assertTrue(inst.quit_called)
        # 占位已被真实附件替换
        self.assertNotIsInstance(msg.get_payload()[1], _UrlPlaceholder)
        self.assertEqual(msg.get_payload()[1].get_payload(decode=True), b"PDFBYTES")

    def test_send_missing_credentials(self):
        p = make_plugin(smtp_user="", smtp_auth_code="")
        msg = p._build_mime(["a@b.com"], "主题", "正文", [])
        with self.assertRaises(RuntimeError):
            p._send_sync(msg, 10)

    def test_send_download_failure_fallback(self):
        p = make_plugin()
        msg = p._build_mime(["a@b.com"], "主题", "正文", [("url", "https://x.com/r.pdf")])
        FakeSMTP.instances = []
        with mock.patch("astrbot_plugin_sendmail.main.smtplib.SMTP_SSL", FakeSMTP), \
             mock.patch("astrbot_plugin_sendmail.main.httpx.Client",
                        lambda **k: FakeClient(b"", raise_on={"https://x.com/r.pdf"})):
            failed = p._send_sync(msg, 10)
        self.assertEqual(failed, ["r.pdf"])
        # 发送流程不被附件失败阻断，降级内容占位
        self.assertEqual(
            msg.get_payload()[1].get_payload(decode=True), "(附件下载失败)".encode()
        )

    def test_send_attachment_too_large(self):
        p = make_plugin(attach_max_mb=1)
        msg = p._build_mime(["a@b.com"], "主题", "正文", [("url", "https://x.com/big.pdf")])
        FakeSMTP.instances = []
        with mock.patch("astrbot_plugin_sendmail.main.smtplib.SMTP_SSL", FakeSMTP), \
             mock.patch("astrbot_plugin_sendmail.main.httpx.Client",
                        lambda **k: FakeClient(b"X" * (2 * 1024 * 1024))):
            failed = p._send_sync(msg, 1)
        self.assertIn("big.pdf", failed[0])

    def test_smtp_settings_presets(self):
        p = make_plugin(smtp_provider="qq")
        self.assertEqual(p._smtp_settings(), SMTP_PRESETS["qq"])
        p = make_plugin(smtp_provider="163")
        self.assertEqual(p._smtp_settings(), SMTP_PRESETS["163"])
        p = make_plugin(smtp_provider="gmail")
        self.assertEqual(p._smtp_settings(), SMTP_PRESETS["gmail"])

    def test_smtp_settings_custom_and_fallback(self):
        p = make_plugin(smtp_provider="custom", smtp_host="mail.example.com", smtp_port=587, smtp_ssl=False)
        self.assertEqual(p._smtp_settings(), ("mail.example.com", 587, False))
        p = make_plugin(smtp_provider="custom", smtp_host="")
        self.assertEqual(p._smtp_settings(), SMTP_PRESETS["qq"])


# ==================== 指令入口 ====================

class TestCommand(unittest.TestCase):
    def test_non_admin_rejected(self):
        p = make_plugin()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 a@b.com | 主题 | 正文", is_admin=False)))
        self.assertIn("仅管理员", reply_text(result))

    def test_help(self):
        p = make_plugin()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 帮助")))
        self.assertIn("邮件发送助手用法", reply_text(result))
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件")))
        self.assertIn("邮件发送助手用法", reply_text(result))

    def test_invalid_recipients(self):
        p = make_plugin()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 不是邮箱 | 主题 | 正文")))
        self.assertIn("收件人无效", reply_text(result))

    def test_empty_body(self):
        p = make_plugin()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 a@b.com | 主题 |")))
        self.assertIn("正文为空", reply_text(result))

    def test_rate_limit(self):
        p = make_plugin(send_interval=30)
        p._last_send_at = time.time()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 a@b.com | 主题 | 正文")))
        self.assertIn("发送太频繁", reply_text(result))

    def test_success_path(self):
        p = make_plugin()
        sent = {}

        def fake_send(msg, max_mb):
            sent["to"] = msg["To"]
            sent["subject"] = msg["Subject"]
            return []

        p._send_sync = fake_send
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 a@b.com,b@c.com | 周报 | 本周数据")))
        text = reply_text(result)
        self.assertIn("已发送到", text)
        self.assertIn("a@b.com", text)
        self.assertEqual(sent["to"], "a@b.com, b@c.com")
        self.assertEqual(str(sent["subject"]), "周报")
        self.assertEqual(len(p._history), 1)

    def test_success_with_invalid_attachment_warned(self):
        p = make_plugin()
        p._send_sync = lambda msg, max_mb: []
        result = asyncio.run(p.cmd_mail(
            FakeEvent("/邮件 a@b.com | 主题 | 正文 --附件=/no/such/file.zip")
        ))
        text = reply_text(result)
        self.assertIn("已发送到", text)
        self.assertIn("附件无效已忽略", text)

    def test_body_truncated(self):
        p = make_plugin(body_max_chars=100)
        long_body = "哈" * 300
        p._send_sync = lambda msg, max_mb: []
        result = asyncio.run(p.cmd_mail(FakeEvent(f"/邮件 a@b.com | 主题 | {long_body}")))
        text = reply_text(result)
        self.assertIn("已截取前 100 字", text)

    def test_send_failure_reported(self):
        p = make_plugin()

        def boom(msg, max_mb):
            raise RuntimeError("smtp 认证失败")

        p._send_sync = boom
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 a@b.com | 主题 | 正文")))
        self.assertIn("发送失败", reply_text(result))

    def test_aliases(self):
        p = make_plugin()
        p._send_sync = lambda msg, max_mb: []
        for cmd in ("/发邮件 a@b.com | 主题 | 正文", "/mail a@b.com | 主题 | 正文"):
            p._last_send_at = 0.0
            result = asyncio.run(p.cmd_mail(FakeEvent(cmd)))
            self.assertIn("已发送到", reply_text(result), cmd)


# ==================== AI 工具（send_mail） ====================

class TestAiTool(unittest.TestCase):
    def test_non_admin_rejected(self):
        p = make_plugin()
        result = asyncio.run(p.ai_send_mail(
            FakeEvent(is_admin=False), "a@b.com", "主题", "正文"
        ))
        self.assertIn("仅管理员", result)

    def test_invalid_recipients(self):
        p = make_plugin()
        result = asyncio.run(p.ai_send_mail(
            FakeEvent(), "不是邮箱", "主题", "正文"
        ))
        self.assertIn("收件人无效", result)

    def test_success_smtp_channel(self):
        p = make_plugin()
        p._send_sync = lambda msg, max_mb: []
        result = asyncio.run(p.ai_send_mail(
            FakeEvent(), "a@b.com,c@d.com", "周报", "本周数据"
        ))
        self.assertIn("已发送到", result)
        self.assertIn("a@b.com", result)
        self.assertEqual(len(p._history), 1)
        self.assertEqual(p._history[0]["channel"], "smtp")
        self.assertEqual(p._history[0]["subject"], "周报")

    def test_success_agently_channel(self):
        p = make_plugin(send_channel="agently")
        p._send_agently_sync = lambda *args: []
        result = asyncio.run(p.ai_send_mail(
            FakeEvent(), "a@b.com", "主题", "正文"
        ))
        self.assertIn("已发送到", result)
        self.assertEqual(p._history[0]["channel"], "agently")

    def test_attachments_parsed(self):
        p = make_plugin()
        p._send_sync = lambda msg, max_mb: []
        result = asyncio.run(p.ai_send_mail(
            FakeEvent(), "a@b.com", "主题", "正文",
            attachments="https://x.com/a.pdf, https://y.com/b.png",
        ))
        self.assertIn("已发送到", result)
        self.assertEqual(p._history[0]["attachments"], ["https://x.com/a.pdf", "https://y.com/b.png"])

    def test_empty_attachments_ok(self):
        p = make_plugin()
        p._send_sync = lambda msg, max_mb: []
        result = asyncio.run(p.ai_send_mail(
            FakeEvent(), "a@b.com", "主题", "正文", attachments="  "
        ))
        self.assertIn("已发送到", result)
        self.assertEqual(p._history[0]["attachments"], [])


# ==================== AI 工具（read_mail） ====================

def fake_list_resp():
    return {
        "ok": True,
        "data": {
            "data": [
                {
                    "message_id": "msg_abc",
                    "subject": "季度报告",
                    "from": {"email": "boss@example.com", "name": "Boss"},
                    "created_at": "2026-08-15T10:00:00Z",
                    "snippet": "这是摘要内容",
                }
            ]
        }
    }


class TestMailText(unittest.TestCase):
    def test_html_to_plain_text(self):
        self.assertEqual(
            SendMailPlugin._mail_text("<p>你好<b>世界</b></p>"),
            "你好 世界",  # 标签位置转换为空格
        )

    def test_truncated(self):
        text = SendMailPlugin._mail_text("<p>" + "哈" * 600 + "</p>", max_chars=100)
        self.assertEqual(len(text), 101)  # 100 字 + 省略号
        self.assertTrue(text.endswith("…"))


class TestAiReadMail(unittest.TestCase):
    def test_non_admin_rejected(self):
        p = make_plugin()
        result = asyncio.run(p.ai_read_mail(FakeEvent(is_admin=False)))
        self.assertIn("仅管理员", result)

    def test_list_success(self):
        p = make_plugin()
        calls = []
        p._agently_cli_run = lambda args, timeout=60: (calls.append(args) or fake_list_resp())
        result = asyncio.run(p.ai_read_mail(FakeEvent(), limit=3))
        self.assertIn("季度报告", result)
        self.assertIn("boss@example.com", result)
        self.assertEqual(calls[0][:4], ["message", "+list", "--dir", "inbox"])

    def test_search_with_query(self):
        p = make_plugin()
        calls = []
        p._agently_cli_run = lambda args, timeout=60: (calls.append(args) or fake_list_resp())
        result = asyncio.run(p.ai_read_mail(FakeEvent(), query="报告"))
        self.assertIn("季度报告", result)
        self.assertEqual(calls[0][:4], ["message", "+search", "--q", "报告"])

    def test_include_body_reads_full(self):
        p = make_plugin()
        read_calls = []

        def fake_run(args, timeout=60):
            if args[1] == "+read":
                read_calls.append(args)
                return {"ok": True, "data": {"body": "<p>正文内容<b>详情</b></p>"}}
            return fake_list_resp()

        p._agently_cli_run = fake_run
        result = asyncio.run(p.ai_read_mail(FakeEvent(), include_body=True))
        self.assertIn("正文: 正文内容 详情", result)
        self.assertEqual(read_calls, [["message", "+read", "--id", "msg_abc"]])

    def test_empty_result(self):
        p = make_plugin()
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": []}}
        result = asyncio.run(p.ai_read_mail(FakeEvent()))
        self.assertIn("没有找到相关邮件", result)

    def test_cli_failure_reported(self):
        p = make_plugin()

        def boom(args, timeout=60):
            raise RuntimeError("CLI 未安装")

        p._agently_cli_run = boom
        result = asyncio.run(p.ai_read_mail(FakeEvent()))
        self.assertIn("读取邮件失败", result)

    def test_include_body_limits_to_five(self):
        p = make_plugin()
        calls = []
        p._agently_cli_run = lambda args, timeout=60: (
            calls.append(args) or {"ok": True, "data": {"data": []}}
        )
        asyncio.run(p.ai_read_mail(FakeEvent(), limit=10, include_body=True))
        self.assertIn("--limit", calls[0])
        self.assertEqual(calls[0][calls[0].index("--limit") + 1], "5")


# ==================== Agent Mail CLI 通道 ====================

class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestAgentlySend(unittest.TestCase):
    def test_send_success_with_url_attachment(self):
        p = make_plugin(send_channel="agently")
        p._agently_cmd = lambda: ["node", r"C:\tools\agently-cli\scripts\run.js"]
        calls = {}

        def fake_run(cmd, **kwargs):
            calls["cmd"] = cmd
            calls["cwd"] = kwargs.get("cwd")
            return FakeProc(0, json.dumps({"ok": True, "data": {}}))

        with mock.patch("astrbot_plugin_sendmail.main.subprocess.run", fake_run), \
             mock.patch("astrbot_plugin_sendmail.main.httpx.Client",
                        lambda **k: FakeClient(b"PDFBYTES")):
            failed = p._send_agently_sync(
                ["a@b.com", "c@d.com"], "主题", "你好", [("url", "https://x.com/r.pdf")], 10
            )
        self.assertEqual(failed, [])
        cmd = calls["cmd"]
        self.assertEqual(cmd[0:2], ["node", r"C:\tools\agently-cli\scripts\run.js"])
        self.assertEqual(cmd[2:4], ["message", "+send"])
        self.assertIn("--confirmed", cmd)
        self.assertIn("--to", cmd)
        self.assertIn("a@b.com", cmd)
        self.assertIn("c@d.com", cmd)
        self.assertIn("--subject", cmd)
        # 附件相对路径（位于临时工作目录）
        att = cmd[cmd.index("--attachment") + 1]
        self.assertIn("r.pdf", att)
        self.assertNotIn(":", att)  # 不能是绝对路径
        # 临时工作目录已清理
        self.assertFalse(os.path.exists(calls["cwd"]))

    def test_send_local_file_attachment(self):
        p = make_plugin(send_channel="agently")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc(0, json.dumps({"ok": True}))

        with tempfile.NamedTemporaryFile(delete=False, suffix=".zip") as f:
            f.write(b"ZIP")
            path = f.name
        try:
            with mock.patch("astrbot_plugin_sendmail.main.subprocess.run", fake_run):
                failed = p._send_agently_sync(["a@b.com"], "主题", "正文", [("file", path)], 10)
        finally:
            os.unlink(path)
        self.assertEqual(failed, [])
        att = calls[0][calls[0].index("--attachment") + 1]
        self.assertIn(".zip", att)

    def test_send_plain_body_format(self):
        p = make_plugin(send_channel="agently", mail_html=False)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc(0, '{"ok": true}')

        with mock.patch("astrbot_plugin_sendmail.main.subprocess.run", fake_run):
            p._send_agently_sync(["a@b.com"], "主题", "纯文本", [], 10)
        self.assertIn("--body-format", calls[0])
        self.assertEqual(calls[0][calls[0].index("--body-format") + 1], "plain")

    def test_send_html_body_auto(self):
        p = make_plugin(send_channel="agently")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc(0, '{"ok": true}')

        with mock.patch("astrbot_plugin_sendmail.main.subprocess.run", fake_run):
            p._send_agently_sync(["a@b.com"], "主题", "你好\n第二行", [], 10)
        body = calls[0][calls[0].index("--body") + 1]
        self.assertIn("<p>", body)
        self.assertNotIn("--body-format", calls[0])

    def test_send_cli_error_reported(self):
        p = make_plugin(send_channel="agently")
        with mock.patch("astrbot_plugin_sendmail.main.subprocess.run",
                        lambda *a, **k: FakeProc(1, "", "认证失败")):
            with self.assertRaises(RuntimeError) as ctx:
                p._send_agently_sync(["a@b.com"], "主题", "正文", [], 10)
        self.assertIn("认证失败", str(ctx.exception))

    def test_send_not_ok_json(self):
        p = make_plugin(send_channel="agently")
        with mock.patch("astrbot_plugin_sendmail.main.subprocess.run",
                        lambda *a, **k: FakeProc(0, json.dumps({"ok": False, "message": "quota exceeded"}))):
            with self.assertRaises(RuntimeError) as ctx:
                p._send_agently_sync(["a@b.com"], "主题", "正文", [], 10)
        self.assertIn("quota exceeded", str(ctx.exception))

    def test_send_cli_missing(self):
        p = make_plugin(send_channel="agently")

        def missing(cmd, **kwargs):
            raise FileNotFoundError(cmd[0])

        with mock.patch("astrbot_plugin_sendmail.main.subprocess.run", missing):
            with self.assertRaises(RuntimeError) as ctx:
                p._send_agently_sync(["a@b.com"], "主题", "正文", [], 10)
        self.assertIn("npm install -g", str(ctx.exception))

    def test_send_download_failure_keeps_sending(self):
        p = make_plugin(send_channel="agently")
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc(0, '{"ok": true}')

        with mock.patch("astrbot_plugin_sendmail.main.subprocess.run", fake_run), \
             mock.patch("astrbot_plugin_sendmail.main.httpx.Client",
                        lambda **k: FakeClient(b"", raise_on={"https://x.com/bad.pdf"})):
            failed = p._send_agently_sync(
                ["a@b.com"], "主题", "正文", [("url", "https://x.com/bad.pdf")], 10
            )
        self.assertEqual(failed, ["bad.pdf"])
        # 失败的附件不进命令参数
        self.assertNotIn("--attachment", calls[0])

    def test_url_attachment_too_large(self):
        p = make_plugin(send_channel="agently", agently_attach_max_mb=1)
        calls = []

        def fake_run(cmd, **kwargs):
            calls.append(cmd)
            return FakeProc(0, '{"ok": true}')

        with mock.patch("astrbot_plugin_sendmail.main.subprocess.run", fake_run), \
             mock.patch("astrbot_plugin_sendmail.main.httpx.Client",
                        lambda **k: FakeClient(b"X" * (2 * 1024 * 1024))):
            failed = p._send_agently_sync(
                ["a@b.com"], "主题", "正文", [("url", "https://x.com/big.pdf")], 1
            )
        self.assertIn("big.pdf", failed[0])
        self.assertNotIn("--attachment", calls[0])


class TestAgentlyCommand(unittest.TestCase):
    def test_agently_cmd_resolved(self):
        # 真实环境：应解析出可用的 agently-cli 命令（配置指定优先）
        p = make_plugin(agently_cli_path="C:\\tools\\agently-cli.exe")
        self.assertEqual(p._agently_cmd(), ["C:\\tools\\agently-cli.exe"])
        p2 = make_plugin(agently_cli_path="")
        cmd = p2._agently_cmd()
        self.assertTrue(cmd, "应解析出非空命令")
        self.assertTrue(os.path.exists(cmd[0]), f"命令应存在: {cmd}")

    def test_agently_shim_resolved_to_node(self):
        # Windows npm .cmd shim 应解析为 [node.exe, 包主入口]，绕过 cmd.exe 换行截断
        shim = shutil.which("agently-cli")
        if not shim:
            self.skipTest("本机未安装 agently-cli")
        inner = SendMailPlugin._agently_from_shim(shim)
        self.assertIsNotNone(inner)
        self.assertEqual(len(inner), 2)
        self.assertTrue(os.path.exists(inner[0]), f"node 应存在: {inner[0]}")
        self.assertTrue(os.path.exists(inner[1]), f"主入口应存在: {inner[1]}")
        self.assertTrue(inner[1].endswith((".js", ".cjs", ".mjs")))

    def test_agently_channel_dispatch_success(self):
        p = make_plugin(send_channel="agently")
        calls = []

        def fake_send(recipients, subject, body, attachments, max_mb):
            calls.append((recipients, subject))
            return []

        p._send_agently_sync = fake_send
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 a@b.com | 主题 | 正文")))
        text = reply_text(result)
        self.assertIn("已发送到", text)
        self.assertEqual(calls, [(["a@b.com"], "主题")])
        self.assertEqual(p._history[0]["channel"], "agently")

    def test_agently_channel_dispatch_failure(self):
        p = make_plugin(send_channel="agently")

        def boom(recipients, subject, body, attachments, max_mb):
            raise RuntimeError("agently 未授权")

        p._send_agently_sync = boom
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 a@b.com | 主题 | 正文")))
        self.assertIn("发送失败", reply_text(result))

    def test_channel_dirty_value_falls_back_smtp(self):
        # 非法通道回退 smtp（走 SMTP 构建流程）
        p = make_plugin(send_channel="别的东西")
        calls = []

        def fake_send(msg, max_mb):
            calls.append(msg["To"])
            return []

        p._send_sync = fake_send
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 a@b.com | 主题 | 正文")))
        self.assertIn("已发送到", reply_text(result))
        self.assertEqual(calls, ["a@b.com"])
        self.assertEqual(p._history[0]["channel"], "smtp")


# ==================== 定时邮件总结推送 ====================

MAIL_ITEMS = [
    {
        "message_id": "msg_old1",
        "subject": "旧邮件",
        "from": {"email": "a@b.com"},
        "created_at": "2026-08-15T09:00:00Z",
        "snippet": "旧摘要",
    },
    {
        "message_id": "msg_new1",
        "subject": "新邮件一",
        "from": {"email": "boss@example.com"},
        "created_at": "2026-08-15T10:00:00Z",
        "snippet": "新摘要一",
    },
]


def make_watcher_plugin(**overrides):
    """构造带临时 seen 文件与 fake context 的插件"""
    cfg = {"auto_summary_enabled": True, "auto_summary_interval": 30,
           "auto_summary_targets": "云晓:GroupMessage:1", "auto_summary_llm": True,
           "auto_summary_max_mails": 5}
    cfg.update(overrides)
    p = make_plugin(**cfg)
    p._seen_path = os.path.join(tempfile.mkdtemp(prefix="seen_"), "seen.json")
    p.context = FakeContext()
    return p


class TestAutoSummary(unittest.TestCase):
    def test_parse_targets(self):
        p = make_plugin()
        self.assertEqual(
            p._parse_targets(" 云晓:GroupMessage:1, 凌阳:GroupMessage:2;云晓:GroupMessage:1"),
            ["云晓:GroupMessage:1", "凌阳:GroupMessage:2"],
        )
        self.assertEqual(p._parse_targets("  ,,  "), [])

    def test_no_targets_skips(self):
        p = make_watcher_plugin(auto_summary_targets="")
        p._agently_cli_run = lambda args, timeout=60: (_ for _ in ()).throw(
            AssertionError("不应读取邮箱")
        )
        asyncio.run(p._check_new_mails())

    def test_first_run_baseline_only(self):
        p = make_watcher_plugin()
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        asyncio.run(p._check_new_mails())
        self.assertEqual(p.context.sent, [])  # 首次不推送
        with open(p._seen_path, encoding="utf-8") as f:
            seen = set(json.load(f))
        self.assertEqual(seen, {"msg_old1", "msg_new1"})

    def test_new_mail_pushed(self):
        p = make_watcher_plugin()
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p.context.provider = FakeProvider()
        # 预置基线：只有旧邮件
        p._save_seen_ids({"msg_old1"})
        asyncio.run(p._check_new_mails())
        self.assertEqual(len(p.context.sent), 1)
        session, text = p.context.sent[0]
        self.assertEqual(session, "云晓:GroupMessage:1")
        self.assertIn("新邮件提醒（1 封）", text)
        self.assertIn("新邮件一", text)
        self.assertIn("boss@example.com", text)
        self.assertIn("AI 总结", text)
        self.assertIn("这是 AI 总结", text)
        with open(p._seen_path, encoding="utf-8") as f:
            self.assertEqual(set(json.load(f)), {"msg_old1", "msg_new1"})

    def test_no_new_mail_silent(self):
        p = make_watcher_plugin()
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1", "msg_new1"})
        asyncio.run(p._check_new_mails())
        self.assertEqual(p.context.sent, [])

    def test_llm_failure_falls_back_to_detail(self):
        p = make_watcher_plugin()
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1"})
        p.context.provider = FakeProvider(err=True)
        asyncio.run(p._check_new_mails())
        _, text = p.context.sent[0]
        self.assertIn("新邮件一", text)
        self.assertNotIn("AI 总结", text)

    def test_llm_disabled(self):
        p = make_watcher_plugin(auto_summary_llm=False)
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1"})
        asyncio.run(p._check_new_mails())
        _, text = p.context.sent[0]
        self.assertNotIn("AI 总结", text)

    def test_llm_called_with_detail(self):
        p = make_watcher_plugin()
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1"})
        seen_prompts = []

        class Recorder(FakeProvider):
            async def text_chat(self, prompt=None, **kwargs):
                seen_prompts.append(prompt)
                return await super().text_chat(prompt=prompt, **kwargs)

        p.context.provider = Recorder()
        asyncio.run(p._check_new_mails())
        self.assertTrue(seen_prompts)
        self.assertIn("新邮件一", seen_prompts[0])

    def test_max_mails_limit(self):
        many = MAIL_ITEMS + [
            {"message_id": f"msg_new{i}", "subject": f"新邮件{i}",
             "from": {"email": "x@y.com"}, "created_at": "2026-08-15T11:00:00Z",
             "snippet": "摘要"}
            for i in range(2, 7)
        ]
        p = make_watcher_plugin(auto_summary_max_mails=3)
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": many}}
        p._save_seen_ids({"msg_old1"})
        asyncio.run(p._check_new_mails())
        self.assertEqual(len(p.context.sent), 1)
        _, text = p.context.sent[0]
        self.assertIn("新邮件提醒（3 封）", text)
        self.assertNotIn("新邮件5", text)

    def test_cli_failure_skips(self):
        p = make_watcher_plugin()

        def boom(args, timeout=60):
            raise RuntimeError("网络错误")

        p._agently_cli_run = boom
        asyncio.run(p._check_new_mails())  # 不抛异常，静默跳过
        self.assertEqual(p.context.sent, [])


# ==================== 转发规则匹配 ====================

def make_rule(**kw):
    """构造规则字典（默认启用、无条件）"""
    base = {"id": "r1", "from_contains": "", "subject_contains": "",
            "folder": "", "action_to": "fwd@example.com", "enabled": True}
    base.update(kw)
    return base


class TestRuleMatching(unittest.TestCase):
    def setUp(self):
        self.p = make_plugin()

    def test_from_contains_match(self):
        rule = make_rule(from_contains="boss@")
        mail = {"subject": "周报", "from": {"email": "boss@example.com", "name": "Boss"}}
        self.assertTrue(self.p._rule_matches(rule, mail))

    def test_from_contains_no_match(self):
        rule = make_rule(from_contains="ceo@")
        mail = {"subject": "周报", "from": {"email": "boss@example.com"}}
        self.assertFalse(self.p._rule_matches(rule, mail))

    def test_from_matches_name(self):
        # 发件人条件对显示名也生效
        rule = make_rule(from_contains="老板")
        mail = {"subject": "x", "from": {"email": "boss@example.com", "name": "老板"}}
        self.assertTrue(self.p._rule_matches(rule, mail))

    def test_subject_contains_match(self):
        rule = make_rule(subject_contains="报销")
        mail = {"subject": "本季度报销单", "from": {"email": "a@b.com"}}
        self.assertTrue(self.p._rule_matches(rule, mail))

    def test_folder_match(self):
        rule = make_rule(folder="inbox")
        self.assertTrue(self.p._rule_matches(rule, {"subject": "x", "folder": "inbox"}))
        self.assertFalse(self.p._rule_matches(rule, {"subject": "x", "folder": "spam"}))

    def test_any_condition_matches(self):
        # 发件人不中但主题中：任一命中即匹配
        rule = make_rule(from_contains="nobody@", subject_contains="会议")
        mail = {"subject": "会议通知", "from": {"email": "boss@x.com"}}
        self.assertTrue(self.p._rule_matches(rule, mail))

    def test_no_condition_never_matches(self):
        rule = make_rule(from_contains="", subject_contains="", folder="")
        self.assertFalse(self.p._rule_matches(rule, {"subject": "任意"}))

    def test_disabled_never_matches(self):
        rule = make_rule(from_contains="boss@", enabled=False)
        mail = {"subject": "x", "from": {"email": "boss@example.com"}}
        self.assertFalse(self.p._rule_matches(rule, mail))

    def test_case_insensitive(self):
        rule = make_rule(from_contains="BOSS")
        mail = {"subject": "x", "from": {"email": "boss@example.com"}}
        self.assertTrue(self.p._rule_matches(rule, mail))


# ==================== 转发规则持久化（独立 JSON 原子写） ====================

class TestRulePersistence(unittest.TestCase):
    def setUp(self):
        self.p = make_plugin()
        self.p._rules_path = os.path.join(tempfile.mkdtemp(prefix="rules_"), "forward_rules.json")
        self.p._rules = self.p._load_rules()

    def test_add_and_persist_atomic(self):
        text = self.p._cmd_rule("add 发件人=boss@ 主题=报销 转发=backup@example.com")
        self.assertIn("已添加", text)
        self.assertTrue(os.path.exists(self.p._rules_path))
        with open(self.p._rules_path, encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(len(data["rules"]), 1)
        r = data["rules"][0]
        self.assertEqual(r["from_contains"], "boss@")
        self.assertEqual(r["subject_contains"], "报销")
        self.assertEqual(r["action_to"], "backup@example.com")
        # 原子写：无残留临时文件
        self.assertFalse(os.path.exists(self.p._rules_path + ".tmp"))

    def test_invalid_target_rejected(self):
        text = self.p._cmd_rule("add 发件人=boss@ 转发=不是邮箱")
        self.assertIn("目标邮箱无效", text)
        self.assertEqual(self.p._rules, [])

    def test_no_condition_rejected(self):
        text = self.p._cmd_rule("add 转发=backup@example.com")
        self.assertIn("至少需要一个条件", text)
        self.assertEqual(self.p._rules, [])

    def test_list_and_remove(self):
        self.p._cmd_rule("add 发件人=a@ 转发=b@example.com")
        self.p._cmd_rule("add 主题=报销 转发=c@example.com")
        listing = self.p._cmd_rule("list")
        self.assertIn("转发规则", listing)
        self.assertIn("b@example.com", listing)
        rid = self.p._rules[0]["id"]
        self.assertIn("已删除", self.p._cmd_rule(f"remove {rid}"))
        self.assertEqual(len(self.p._rules), 1)
        with open(self.p._rules_path, encoding="utf-8") as f:
            self.assertEqual(len(json.load(f)["rules"]), 1)
        # 删除不存在的 ID
        self.assertIn("未找到", self.p._cmd_rule(f"remove {rid}"))

    def test_load_missing_file_empty(self):
        self.assertEqual(self.p._load_rules(), [])

    def test_load_corrupt_file_empty(self):
        with open(self.p._rules_path, "w", encoding="utf-8") as f:
            f.write("{corrupt")
        self.assertEqual(self.p._load_rules(), [])

    def test_rule_commands_via_cmd_mail(self):
        result = asyncio.run(self.p.cmd_mail(FakeEvent("/邮件 rule add 发件人=boss@ 转发=backup@example.com")))
        self.assertIn("已添加", reply_text(result))
        self.assertEqual(len(self.p._rules), 1)
        result = asyncio.run(self.p.cmd_mail(FakeEvent("/邮件 rule list")))
        self.assertIn("boss@", reply_text(result))
        rid = self.p._rules[0]["id"]
        result = asyncio.run(self.p.cmd_mail(FakeEvent(f"/邮件 rule remove {rid}")))
        self.assertIn("已删除", reply_text(result))
        self.assertEqual(self.p._rules, [])
        result = asyncio.run(self.p.cmd_mail(FakeEvent("/邮件 rule")))
        self.assertIn("转发规则用法", reply_text(result))


# ==================== 转发流程 ====================

class TestForwardFlow(unittest.TestCase):
    def test_build_forward_body(self):
        body = SendMailPlugin._build_forward_body({
            "subject": "季度报告",
            "from": {"email": "boss@example.com", "name": "Boss"},
            "created_at": "2026-08-15T10:00:00Z",
            "snippet": "这是摘要内容",
        })
        self.assertIn("boss@example.com", body)
        self.assertIn("季度报告", body)
        self.assertIn("2026-08-15 10:00", body)
        self.assertIn("这是摘要内容", body)

    def test_forward_in_check_new_mails(self):
        p = make_watcher_plugin()
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1"})
        p._rules = [make_rule(subject_contains="新邮件")]
        fwd = []
        p._forward_one_sync = lambda item, to: fwd.append((to, item["subject"]))
        asyncio.run(p._check_new_mails())
        self.assertEqual(fwd, [("fwd@example.com", "新邮件一")])
        # 推送流程不受影响
        self.assertEqual(len(p.context.sent), 1)

    def test_no_match_keeps_original_flow(self):
        p = make_watcher_plugin()
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1"})
        p._rules = [make_rule(from_contains="不匹配的")]
        fwd = []
        p._forward_one_sync = lambda item, to: fwd.append(to)
        asyncio.run(p._check_new_mails())
        self.assertEqual(fwd, [])
        self.assertEqual(len(p.context.sent), 1)

    def test_forward_failure_does_not_crash(self):
        p = make_watcher_plugin()
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1"})
        p._rules = [make_rule(subject_contains="新邮件")]

        def boom(item, to):
            raise RuntimeError("SMTP 认证失败")

        p._forward_one_sync = boom
        asyncio.run(p._check_new_mails())  # 不崩溃
        self.assertEqual(len(p.context.sent), 1)  # 推送照常


# ==================== 定时摘要调度 ====================

class TestSummarySchedule(unittest.TestCase):
    def setUp(self):
        self.p = make_plugin()

    def test_daily_next_today(self):
        # 08:00 未到 09:00 -> 当天 09:00
        now = datetime.datetime(2026, 8, 17, 8, 0)
        self.assertEqual(self.p._next_summary_at(now), datetime.datetime(2026, 8, 17, 9, 0))

    def test_daily_next_tomorrow(self):
        # 10:00 已过 09:00 -> 明天 09:00
        now = datetime.datetime(2026, 8, 17, 10, 0)
        self.assertEqual(self.p._next_summary_at(now), datetime.datetime(2026, 8, 18, 9, 0))

    def test_daily_custom_time(self):
        p = make_plugin(summary_schedule_time="14:30")
        now = datetime.datetime(2026, 8, 17, 13, 0)
        self.assertEqual(p._next_summary_at(now), datetime.datetime(2026, 8, 17, 14, 30))

    def test_weekly_same_day(self):
        # 2026-08-17 是周一，配置周一 09:00，08:00 未到点 -> 当天
        p = make_plugin(summary_schedule_mode="weekly", summary_schedule_weekday=1)
        now = datetime.datetime(2026, 8, 17, 8, 0)
        self.assertEqual(p._next_summary_at(now), datetime.datetime(2026, 8, 17, 9, 0))

    def test_weekly_next_week(self):
        # 周一已过 09:00 -> 下周一
        p = make_plugin(summary_schedule_mode="weekly", summary_schedule_weekday=1)
        now = datetime.datetime(2026, 8, 17, 10, 0)
        self.assertEqual(p._next_summary_at(now), datetime.datetime(2026, 8, 24, 9, 0))

    def test_weekly_other_day(self):
        # 周三触发，周一 08:00 -> 本周三
        p = make_plugin(summary_schedule_mode="weekly", summary_schedule_weekday=3)
        now = datetime.datetime(2026, 8, 17, 8, 0)
        self.assertEqual(p._next_summary_at(now), datetime.datetime(2026, 8, 19, 9, 0))

    def test_dirty_time_falls_back(self):
        p = make_plugin(summary_schedule_time="abc", summary_schedule_mode="乱七八糟")
        now = datetime.datetime(2026, 8, 17, 8, 0)
        self.assertEqual(p._next_summary_at(now), datetime.datetime(2026, 8, 17, 9, 0))


# ==================== 定时摘要推送（复用 seen 去重） ====================

class TestScheduledSummaryPush(unittest.TestCase):
    def test_push_new_and_mark_seen(self):
        p = make_watcher_plugin(summary_targets="云晓:GroupMessage:1")
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1"})
        text = asyncio.run(p._run_scheduled_summary(manual=True))
        self.assertIn("已将 1 封新邮件摘要推送到 1 个会话", text)
        self.assertIn("新邮件一", text)
        self.assertEqual(len(p.context.sent), 1)
        with open(p._seen_path, encoding="utf-8") as f:
            self.assertEqual(set(json.load(f)), {"msg_old1", "msg_new1"})

    def test_no_new_mail_no_push(self):
        p = make_watcher_plugin(summary_targets="云晓:GroupMessage:1")
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1", "msg_new1"})
        text = asyncio.run(p._run_scheduled_summary(manual=True))
        self.assertIn("没有新邮件", text)
        self.assertEqual(p.context.sent, [])

    def test_no_targets_rejected(self):
        p = make_watcher_plugin(summary_targets="")
        text = asyncio.run(p._run_scheduled_summary(manual=True))
        self.assertIn("summary_targets", text)

    def test_invalid_targets_filtered(self):
        p = make_watcher_plugin(summary_targets="不是合法目标")
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1"})
        text = asyncio.run(p._run_scheduled_summary(manual=True))
        self.assertIn("summary_targets", text)
        self.assertEqual(p.context.sent, [])

    def test_first_run_baseline_only(self):
        p = make_watcher_plugin(summary_targets="云晓:GroupMessage:1")
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        text = asyncio.run(p._run_scheduled_summary(manual=True))
        self.assertIn("没有新邮件", text)  # 基线不推送
        self.assertEqual(p.context.sent, [])
        with open(p._seen_path, encoding="utf-8") as f:
            self.assertEqual(set(json.load(f)), {"msg_old1", "msg_new1"})

    def test_manual_summary_command(self):
        p = make_watcher_plugin(summary_targets="云晓:GroupMessage:1")
        p._agently_cli_run = lambda args, timeout=60: {"ok": True, "data": {"data": MAIL_ITEMS}}
        p._save_seen_ids({"msg_old1"})
        p.context.provider = FakeProvider()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 summary now")))
        text = reply_text(result)
        self.assertIn("已将 1 封新邮件摘要推送到 1 个会话", text)
        self.assertIn("新邮件一", text)
        # summary 用法提示
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 summary")))
        self.assertIn("定时摘要推送用法", reply_text(result))


# ==================== 模板渲染 ====================

class TestTemplateRender(unittest.TestCase):
    def test_builtin_at_least_five(self):
        self.assertGreaterEqual(len(MAIL_TEMPLATES), 5)
        for name in ("请假申请", "周报提交", "生日祝福", "会议通知", "感谢信"):
            self.assertIn(name, MAIL_TEMPLATES)

    def test_render_success(self):
        tpl = MAIL_TEMPLATES["请假申请"]
        subject, body = render_template(tpl, {
            "名字": "张三", "领导称呼": "王经理", "原因": "家中有事",
            "天数": "1", "日期": "2026-08-18", "同事": "李四",
        })
        self.assertIn("请假申请 - 张三 - 2026-08-18", subject)
        self.assertIn("尊敬的王经理", body)
        self.assertIn("张三", body)
        self.assertNotIn("{", body)  # 全部占位符已替换

    def test_missing_placeholder_replaced_empty(self):
        # 渲染层：缺失占位符替换为空串（命令层会提前拦截缺失项）
        tpl = MAIL_TEMPLATES["生日祝福"]
        subject, body = render_template(tpl, {"名字": "小红", "祝愿": "万事如意"})
        self.assertIn("生日快乐，小红！", subject)
        self.assertIn("万事如意", body)

    def test_only_whitelisted_placeholders_replaced(self):
        # 白名单外的 {占位符} 原样保留，不被替换
        out = render_text("你好 {名字} {不存在的}", ["名字"], {"名字": "张三", "不存在的": "x"})
        self.assertEqual(out, "你好 张三 {不存在的}")

    def test_format_injection_safe(self):
        # 恶意值尝试访问对象属性：str.replace 仅作字面量处理，不会求值
        tpl = MAIL_TEMPLATES["感谢信"]
        subject, body = render_template(tpl, {
            "名字": "{0.__class__}", "事件": "x", "感谢内容": "y",
            "署名": "z", "日期": "d",
        })
        self.assertIn("{0.__class__}", subject)  # 原样保留，未发生属性访问
        self.assertIn("{0.__class__}", body)

    def test_unknown_keys_ignored(self):
        tpl = MAIL_TEMPLATES["生日祝福"]
        subject, body = render_template(tpl, {
            "名字": "小红", "祝愿": "好", "署名": "我", "__class__": "注入",
        })
        self.assertNotIn("注入", subject + body)


# ==================== 模板命令 ====================

class TestTemplateCommand(unittest.TestCase):
    def test_template_list(self):
        p = make_plugin()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 template list")))
        text = reply_text(result)
        self.assertIn("邮件模板", text)
        self.assertIn("请假申请", text)
        self.assertIn("占位符", text)

    def test_template_unknown(self):
        p = make_plugin()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 template 不存在的模板")))
        self.assertIn("不存在", reply_text(result))

    def test_send_template_success(self):
        p = make_plugin()
        sent = {}

        def fake_send(msg, max_mb):
            sent["to"] = msg["To"]
            sent["subject"] = str(msg["Subject"])
            return []

        p._send_sync = fake_send
        result = asyncio.run(p.cmd_mail(FakeEvent(
            "/邮件 send 生日祝福 a@b.com 名字=小红 祝愿=万事如意 署名=小李"
        )))
        text = reply_text(result)
        self.assertIn("已发送到", text)
        self.assertIn("生日快乐，小红！", sent["subject"])
        self.assertEqual(sent["to"], "a@b.com")
        self.assertEqual(len(p._history), 1)

    def test_send_template_unknown_template(self):
        p = make_plugin()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 send 不存在的模板 a@b.com 名字=x")))
        self.assertIn("不存在", reply_text(result))

    def test_send_template_invalid_recipient(self):
        p = make_plugin()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 send 生日祝福 不是邮箱 名字=x 祝愿=y 署名=z")))
        self.assertIn("收件人无效", reply_text(result))

    def test_send_template_missing_placeholder(self):
        p = make_plugin()
        p._send_sync = lambda msg, max_mb: []
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 send 生日祝福 a@b.com 名字=小红")))
        text = reply_text(result)
        self.assertIn("缺少模板占位符", text)
        self.assertIn("祝愿", text)
        self.assertIn("署名", text)

    def test_send_template_smtp_failure_reported(self):
        p = make_plugin()

        def boom(msg, max_mb):
            raise RuntimeError("smtp 认证失败")

        p._send_sync = boom
        result = asyncio.run(p.cmd_mail(FakeEvent(
            "/邮件 send 生日祝福 a@b.com 名字=小红 祝愿=好 署名=我"
        )))
        self.assertIn("发送失败", reply_text(result))

    def test_send_template_no_args_shows_help(self):
        p = make_plugin()
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 send")))
        self.assertIn("模板快捷发送用法", reply_text(result))

    def test_normal_send_not_hijacked_by_subcommand(self):
        # send@qq.com 等普通收件人不能被 send 子命令误拦截
        p = make_plugin()
        p._send_sync = lambda msg, max_mb: []
        result = asyncio.run(p.cmd_mail(FakeEvent("/邮件 sender@x.com | 主题 | 正文")))
        self.assertIn("已发送到", reply_text(result))


if __name__ == "__main__":
    unittest.main()
