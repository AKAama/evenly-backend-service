"""中文可读的 HTTP 访问日志：把 method+path 映射成业务语义。"""

from __future__ import annotations

import re
from typing import Callable

# (method, path_regex, 中文动作名) — 更具体的规则放前面
_ROUTE_RULES: list[tuple[str, re.Pattern[str], str]] = [
    # 探活
    ("GET", re.compile(r"^/$"), "根路径探活"),
    ("GET", re.compile(r"^/health$"), "健康检查"),
    ("GET", re.compile(r"^/ready$"), "就绪检查"),
    # 认证
    ("POST", re.compile(r"^/auth/send-verification$"), "发送注册验证码"),
    ("POST", re.compile(r"^/auth/verify-code$"), "校验验证码"),
    ("POST", re.compile(r"^/auth/register$"), "注册账号"),
    ("POST", re.compile(r"^/auth/login$"), "邮箱登录"),
    ("POST", re.compile(r"^/auth/apple$"), "Apple 登录"),
    ("POST", re.compile(r"^/auth/logout$"), "退出登录"),
    ("POST", re.compile(r"^/auth/password-reset/send$"), "发送重置密码验证码"),
    ("POST", re.compile(r"^/auth/password-reset$"), "重置密码"),
    # 用户
    ("GET", re.compile(r"^/users/me$"), "获取当前用户"),
    ("PUT", re.compile(r"^/users/me$"), "更新个人资料"),
    ("GET", re.compile(r"^/users/me/auth-methods$"), "查看登录方式"),
    ("PUT", re.compile(r"^/users/me/username$"), "修改用户名"),
    ("POST", re.compile(r"^/users/me/avatar$"), "上传头像"),
    ("PUT", re.compile(r"^/users/me/password$"), "修改密码"),
    ("POST", re.compile(r"^/users/me/password/setup/send$"), "发送设置密码验证码"),
    ("PUT", re.compile(r"^/users/me/password/setup$"), "设置密码"),
    ("POST", re.compile(r"^/users/me/email/send-verification$"), "发送换绑邮箱验证码"),
    ("PUT", re.compile(r"^/users/me/email$"), "更换邮箱"),
    ("PUT", re.compile(r"^/users/me/push-devices/"), "注册推送设备"),
    ("DELETE", re.compile(r"^/users/me/push-devices/"), "注销推送设备"),
    ("GET", re.compile(r"^/users/me/deactivation-preview$"), "注销预览"),
    ("POST", re.compile(r"^/users/me/deactivate$"), "注销账号"),
    ("DELETE", re.compile(r"^/users/me$"), "删除账号（旧接口）"),
    ("GET", re.compile(r"^/users/search$"), "搜索用户"),
    # 账本
    ("POST", re.compile(r"^/ledgers$"), "创建账本"),
    ("GET", re.compile(r"^/ledgers$"), "账本列表"),
    ("GET", re.compile(r"^/ledgers/invitations/pending$"), "待处理邀请"),
    ("POST", re.compile(r"^/ledgers/invitations/[^/]+/accept$"), "接受邀请"),
    ("POST", re.compile(r"^/ledgers/invitations/[^/]+/reject$"), "拒绝邀请"),
    ("GET", re.compile(r"^/ledgers/invite-links/[^/]+/preview$"), "预览邀请链接"),
    ("POST", re.compile(r"^/ledgers/invite-links/[^/]+/join$"), "通过链接加入账本"),
    ("GET", re.compile(r"^/ledgers/[^/]+/invite-link$"), "获取/创建邀请链接"),
    ("POST", re.compile(r"^/ledgers/[^/]+/invite-link/rotate$"), "轮换邀请链接"),
    ("GET", re.compile(r"^/ledgers/[^/]+/overview$"), "账本总览"),
    ("GET", re.compile(r"^/ledgers/[^/]+$"), "账本详情"),
    ("PATCH", re.compile(r"^/ledgers/[^/]+$"), "更新账本"),
    ("DELETE", re.compile(r"^/ledgers/[^/]+$"), "删除账本"),
    ("POST", re.compile(r"^/ledgers/[^/]+/cover$"), "上传账本封面"),
    ("DELETE", re.compile(r"^/ledgers/[^/]+/cover$"), "删除账本封面"),
    ("POST", re.compile(r"^/ledgers/[^/]+/members$"), "添加成员"),
    ("GET", re.compile(r"^/ledgers/[^/]+/members$"), "成员列表"),
    ("DELETE", re.compile(r"^/ledgers/[^/]+/members/me$"), "退出账本"),
    ("DELETE", re.compile(r"^/ledgers/[^/]+/members/"), "移除成员"),
    # 结算
    ("GET", re.compile(r"^/ledgers/[^/]+/settlements$"), "结算建议"),
    ("GET", re.compile(r"^/ledgers/[^/]+/settlements/history$"), "结算历史"),
    ("POST", re.compile(r"^/ledgers/[^/]+/settlements$"), "确认结算"),
    # 支出
    ("GET", re.compile(r"^/expenses/ledgers/[^/]+/voice-session$"), "语音记账（需 WebSocket）"),
    ("POST", re.compile(r"^/expenses/ledgers/[^/]+/voice-draft$"), "语音草稿"),
    ("POST", re.compile(r"^/expenses/ledgers/[^/]+/expenses$"), "记一笔"),
    ("GET", re.compile(r"^/expenses/ledgers/[^/]+/expenses$"), "支出列表"),
    ("PUT", re.compile(r"^/expenses/[^/]+$"), "修改支出"),
    ("PATCH", re.compile(r"^/expenses/[^/]+/refund$"), "设置退款"),
    ("POST", re.compile(r"^/expenses/[^/]+/confirm$"), "确认支出"),
    ("POST", re.compile(r"^/expenses/[^/]+/reject$"), "拒绝支出"),
    ("DELETE", re.compile(r"^/expenses/[^/]+$"), "删除支出"),
    ("GET", re.compile(r"^/expenses/[^/]+$"), "支出详情"),
    # 审计
    ("GET", re.compile(r"^/admin/audit-events$"), "审计事件列表"),
    ("GET", re.compile(r"^/admin/audit-events/summary$"), "审计日汇总"),
    ("POST", re.compile(r"^/audit/events$"), "客户端上报审计"),
    # 平台账号
    ("GET", re.compile(r"^/admin/platform-users$"), "平台账号列表"),
    ("POST", re.compile(r"^/admin/platform-users$"), "创建平台账号"),
    # 运营后台
    ("GET", re.compile(r"^/admin/users$"), "运营·用户列表"),
    ("GET", re.compile(r"^/admin/users/[^/]+$"), "运营·用户详情"),
    ("PATCH", re.compile(r"^/admin/users/[^/]+/badge$"), "运营·设置用户徽章"),
    ("POST", re.compile(r"^/admin/users/[^/]+/deactivate$"), "运营·强制注销用户"),
    ("POST", re.compile(r"^/admin/users/[^/]+/reset-password$"), "运营·重置用户密码"),
    ("GET", re.compile(r"^/admin/badges$"), "运营·徽章列表"),
    ("POST", re.compile(r"^/admin/badges$"), "运营·创建徽章"),
    ("PATCH", re.compile(r"^/admin/badges/[^/]+$"), "运营·更新徽章"),
    ("DELETE", re.compile(r"^/admin/badges/[^/]+$"), "运营·删除徽章"),
    ("GET", re.compile(r"^/admin/ledgers$"), "运营·账本列表"),
    ("GET", re.compile(r"^/admin/ledgers/[^/]+/overview$"), "运营·账本总览"),
    # 测试
    ("POST", re.compile(r"^/test/"), "测试接口"),
]

_METHOD_ZH = {
    "GET": "查询",
    "POST": "提交",
    "PUT": "全量更新",
    "PATCH": "部分更新",
    "DELETE": "删除",
    "OPTIONS": "预检",
    "HEAD": "探测",
}

_SOURCE_ZH = {
    "ios": "iOS",
    "console": "管理台",
    "web": "网页",
    "android": "Android",
    "api": "API",
}


def describe_request(method: str, path: str) -> str:
    """把 method+path 翻译成中文动作名。"""
    m = (method or "").upper()
    p = path or "/"
    for rule_method, pattern, label in _ROUTE_RULES:
        if rule_method == m and pattern.search(p):
            return label
    verb = _METHOD_ZH.get(m, m)
    return f"{verb} {p}"


def status_bucket(status_code: int) -> str:
    if status_code < 400:
        return "成功"
    if status_code < 500:
        return "客户端错误"
    return "服务端错误"


def format_access_line(
    *,
    method: str,
    path: str,
    status_code: int,
    duration_ms: float,
    slow: bool,
    client_source: str | None = None,
    client_ip: str | None = None,
    user_hint: str | None = None,
) -> str:
    """生成一行中文访问日志。"""
    action = describe_request(method, path)
    if slow:
        tag = "慢请求"
    elif status_code >= 500:
        tag = "服务异常"
    elif status_code >= 400:
        tag = "请求失败"
    else:
        tag = "访问"

    parts = [
        f"[{tag}] {action}",
        f"{method.upper()} {path}",
        f"状态={status_code}({status_bucket(status_code)})",
        f"耗时={duration_ms:.0f}ms",
    ]
    if client_source:
        parts.append(f"来源={_SOURCE_ZH.get(client_source, client_source)}")
    if client_ip:
        parts.append(f"IP={client_ip}")
    if user_hint:
        parts.append(f"用户={user_hint}")
    return " | ".join(parts)


def try_user_hint_from_request(request) -> str | None:
    """尽量从 JWT 里抠出 user_id（不查库），失败则忽略。"""
    try:
        from app.config import settings
        from app.services.auth import decode_token

        auth = request.headers.get("authorization") or request.headers.get("Authorization")
        token = None
        if auth and auth.lower().startswith("bearer "):
            token = auth[7:].strip()
        if not token:
            token = request.cookies.get(settings.auth_cookie_name)
        if not token:
            return None
        data = decode_token(token)
        if data and data.user_id:
            return str(data.user_id)[:8]  # 短 id，避免日志过长
    except Exception:
        return None
    return None
