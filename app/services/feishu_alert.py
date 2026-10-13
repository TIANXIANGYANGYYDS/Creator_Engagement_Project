"""Error cards using Banyue's app-token / interactive chat-message flow."""
from __future__ import annotations

import json
import platform
import re

import httpx


class FeishuAlertError(RuntimeError):
    pass


def safe_reason(text: str) -> str:
    # Upstream exception messages may contain a full signed URL or a header.
    text = re.sub(r"https?://\S+", "[请求地址已省略]", str(text))
    text = re.sub(r"(?i)(cookie|authorization|app_secret|access_token|refresh_token|web_session|sessionid|xsec_token|x-s)[\"']?\s*[:=][^\r\n]*",
                  "[凭据已隐藏]", text)
    return text[:512]


def error_card(report: dict, incident_id: str) -> dict:
    category = report["status"]
    labels = {
        "reauthorization_required": "登录失效，需要重新授权",
        "risk_limited": "平台要求验证或限制访问",
        "awaiting_scan": "正在等待扫码授权",
        "transport_stopped": "浏览器请求已暂停，未判定为掉线",
        "owner_unavailable": "常驻浏览器不可用，无法确认登录状态",
        "sample_failed": "样例数据检查失败，未判定为掉线",
        "monitor_error": "监测程序异常，无法确认登录状态",
        "starting": "浏览器尚未就绪",
        "transport_error": "请求超时或网络连接异常",
        "protocol_error": "上游响应格式异常",
        "program_error": "采集程序异常",
        "upstream_error": "上游服务状态异常",
        "login_expired": "登录失效，需要重新授权",
        "session_rejected": "平台拒绝当前会话",
    }
    title = "小红书会话监测异常"
    lines = [
        f"**时间**：{report['checked_at']}",
        "**等级**：ERROR",
        f"**主机**：{platform.node()}",
        "**模块**：Creator_Engagement / xiaohongshu_monitor",
        "**文件**：app/manually_execute_script/monitor_xiaohongshu_session.py",
        f"**状态**：{labels.get(category, category)}",
        f"**说明**：{safe_reason(report.get('reason', ''))}",
        f"**最近成功检查**：{report.get('last_good_at') or '尚无'}",
        f"**样例**：{report['sample_url']}",
        "**策略**：本机直连；每 30 分钟检查一条样例的互动和第一页评论；登录失效或风控时停止上游探测，同一故障仅通知一次。",
        "**处理**：查看 .local/xhs-monitor/status.json 和 .local/xhs-session/status.json；按页面要求重新扫码或完成验证。",
        f"**追踪 ID**：{incident_id}",
    ]
    return {
        "schema": "2.0",
        "config": {"update_multi": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
        "body": {"elements": [{"tag": "markdown", "content": "\n".join(lines)}]},
    }


def status_error_card(report: dict, incident_id: str) -> dict:
    names = {
        "douyin": "抖音", "toutiao": "今日头条", "xiaohongshu": "小红书",
        "weibo": "微博", "bilibili": "B站", "kuaishou": "快手", "haokan": "好看视频",
        "wechat": "微信公众号", "wechat_channels": "视频号", "sina": "新浪看点",
        "baijiahao": "百家号", "yoojia": "有驾", "sina_finance": "新浪财经头条号",
        "sohu": "搜狐号", "dongchedi": "懂车帝", "weitoutiao": "微头条",
        "netease": "网易号", "xueqiu": "雪球", "autohome": "汽车之家",
        "ixigua": "西瓜视频", "qq_stock": "腾讯自选股", "sina_blog": "新浪博客", "api": "API服务",
    }
    labels = {
        "login_expired": "登录失效", "risk_limited": "风控或访问限制",
        "session_rejected": "当前会话被平台拒绝", "transport_error": "网络或浏览器连接异常",
        "upstream_error": "上游服务异常", "protocol_error": "上游响应格式异常",
        "program_error": "程序异常", "api_error": "接口服务异常",
    }
    operations = {"interactions": "互动", "comments": "评论", "engagement": "互动与评论",
                  "author_list": "作者作品列表", "author_detail": "作者作品详情"}
    name = names.get(report["platform"], report["platform"])
    operation = operations.get(report["operation"], report["operation"])
    lines = [
        f"**时间**：{report['checked_at']}", "**等级**：ERROR", f"**主机**：{platform.node()}",
        f"**平台**：{name}", f"**接口**：{safe_reason(operation)}",
        "**模块**：Creator_Engagement / status_alerts",
        "**文件**：app/services/status_alerts.py",
        f"**状态**：{labels.get(report['status'], report['status'])}",
        f"**说明**：{safe_reason(report['reason'])}",
        f"**合并次数**：{report.get('occurrences', 1)}",
        "**策略**：仅状态错误告警；同平台、接口、错误类别 30 分钟最多一张；缺字段或空数据不告警。",
        "**处理**：按错误类型检查会话、上游状态、代理/浏览器进程及 API 日志；投递记录在 .local/status-alerts/alerts.sqlite3。",
        f"**请求 ID**：{report.get('request_id') or '参见采集日志'}",
        f"**追踪 ID**：{incident_id}",
    ]
    return {"schema": "2.0", "config": {"update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": f"{name} · {operation}状态异常"}, "template": "red"},
            "body": {"elements": [{"tag": "markdown", "content": "\n".join(lines)}]}}


class FeishuAlert:
    def __init__(self, settings, client: httpx.AsyncClient):
        self.app_id = settings.feishu_app_id
        self.secret = settings.feishu_app_secret.get_secret_value()
        self.chat_id = settings.feishu_error_re_chat_id
        self.client = client
        if not all((self.app_id, self.secret, self.chat_id)):
            raise ValueError("飞书告警缺少应用或目标群配置")

    async def _post(self, path: str, **kwargs) -> dict:
        try:
            response = await self.client.post("https://open.feishu.cn/open-apis/" + path, **kwargs)
        except httpx.HTTPError as exc:
            raise FeishuAlertError("飞书请求失败：" + type(exc).__name__) from None
        if not response.is_success:
            raise FeishuAlertError(f"飞书 HTTP {response.status_code}")
        try:
            body = response.json()
        except ValueError:
            raise FeishuAlertError("飞书返回无效 JSON") from None
        if not isinstance(body, dict) or body.get("code") != 0:
            code = body.get("code") if isinstance(body, dict) else None
            raise FeishuAlertError(f"飞书业务失败，code={code}")
        return body

    async def send(self, card: dict, incident_id: str) -> str:
        auth = await self._post("auth/v3/tenant_access_token/internal", json={
            "app_id": self.app_id, "app_secret": self.secret,
        })
        token = auth.get("tenant_access_token")
        if not token:
            raise FeishuAlertError("飞书未返回应用访问令牌")
        result = await self._post("im/v1/messages", params={"receive_id_type": "chat_id"},
            headers={"Authorization": "Bearer " + token}, json={
                "receive_id": self.chat_id, "msg_type": "interactive",
                "content": json.dumps(card, ensure_ascii=False), "uuid": incident_id,
            })
        message_id = (result.get("data") or {}).get("message_id")
        if not message_id:
            raise FeishuAlertError("飞书未确认消息 ID")
        return message_id
