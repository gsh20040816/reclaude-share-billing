"""Telegram native command menus. Scopes affect discovery, never authorization."""
import json

from . import store

USER_COMMANDS = [
    ("quota", "账号额度 · 窗口用量及容量估算"),
    ("accounts", "登录账号池 · 状态与席位"),
    ("units", "计费账号 · 组织下的订阅与成本"),
    ("report", "全员账单与待认领费用"),
    ("bind", "绑定设备归属 · 查看用法"),
    ("revoke", "撤销设备 · 查看用法"),
    ("id", "查看用户 ID、聊天 ID 和话题 ID"),
    ("me", "我的概览 · 设备与本月账单"),
    ("devices", "活跃设备 · 加 all 查看历史"),
    ("bill", "查看我的账单"),
    ("login", "登录设备 · 查看授权链接用法"),
    ("help", "命令帮助"),
    ("cancel", "取消当前操作"),
]
ADMIN_COMMANDS = [
    ("account_add", "添加账号 · 按提示填写"),
    ("sync", "立即采集用量"),
    ("health", "检查服务与采集状态"),
    ("periods", "查看租期与成本"),
    ("period", "添加首期 · 自动读取或填写时间"),
    ("renew", "续费 · 从上一期结束时刻接续"),
    ("unit_link", "换号 · 关联到原计费席位"),
    ("rates", "查看模型倍率"),
    ("rate", "设置模型倍率 · 查看用法"),
    ("account_del", "停用账号 · 查看用法"),
    ("dryrun", "切换演练模式 · 查看用法"),
]


def configure(bot):
    """Reconcile menus on startup; report failures without exposing API credentials."""
    def call(method, **params):
        if not bot.tg.call(method, **params).get("ok"):
            raise RuntimeError(f"Telegram menu setup failed: {method}")

    scopes = [{"type": "chat", "chat_id": uid} for uid in sorted(bot.admins)]
    scopes += [{"type": "chat_member", "chat_id": chat, "user_id": uid}
               for chat in sorted(bot.allowed) if chat < 0 for uid in sorted(bot.admins)]
    previous = json.loads(store.get_setting(bot.conn, "telegram_menu_scopes", "[]"))
    for scope in previous:
        if scope not in scopes:
            call("deleteMyCommands", scope=scope, language_code="")
    def commands(items):
        return [{"command": cmd, "description": desc} for cmd, desc in items]
    call("setMyCommands", commands=commands(USER_COMMANDS), scope={"type": "default"}, language_code="")
    # Remember scopes before writing them, so a later restart can clean up partial success.
    store.set_setting(bot.conn, "telegram_menu_scopes", json.dumps(scopes))
    for scope in scopes:
        call("setMyCommands", commands=commands(ADMIN_COMMANDS + [(cmd, '活跃设备 · 加 all 查看历史' if cmd == 'devices' else desc) for cmd, desc in USER_COMMANDS]),
             scope=scope, language_code="")
    call("setChatMenuButton", menu_button={"type": "commands"})
    for uid in sorted(bot.admins):
        call("setChatMenuButton", chat_id=uid, menu_button={"type": "commands"})
