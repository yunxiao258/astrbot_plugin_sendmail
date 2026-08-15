# -*- coding: utf-8 -*-
"""sendmail 插件单元测试：命令解析、MIME 构建、发送流程、权限与频率限制"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, r"D:\astrbot\data\plugins")
sys.path.insert(0, r"D:\astrbot\data\plugins\astrbot_plugin_sendmail")

from astrbot_plugin_sendmail.main import (  # noqa: E402
    SMTP_PRESETS,
    SendMailPlugin,
    _UrlPlaceholder,
    _to_html,
)


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


# ==================== Agent Mail CLI 通道 ====================

class FakeProc:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestAgentlySend(unittest.TestCase):
    def test_send_success_with_url_attachment(self):
        p = make_plugin(send_channel="agently")
        p._agently_cmd = lambda: "agently-cli"
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
        self.assertEqual(cmd[0], "agently-cli")
        self.assertEqual(cmd[1:3], ["message", "+send"])
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
        # 真实环境：应解析出可用的 agently-cli 路径（配置指定优先）
        p = make_plugin(agently_cli_path="C:\\tools\\agently-cli.exe")
        self.assertEqual(p._agently_cmd(), "C:\\tools\\agently-cli.exe")
        p2 = make_plugin(agently_cli_path="")
        cmd = p2._agently_cmd()
        self.assertTrue(cmd, "应解析出非空命令")
        self.assertTrue(os.path.exists(cmd.split(" ")[0]) or os.path.exists(cmd))

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


if __name__ == "__main__":
    unittest.main()
