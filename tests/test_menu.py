from unittest.mock import Mock

import pytest

from test_bot import bot, ADMIN, _msg
from app import menu


def test_menus_limit_admin_commands_to_authorized_scopes(bot):
    bot.allowed.add(-100123)
    bot.tg.call = Mock(return_value={"ok": True})
    menu.configure(bot)
    calls = [c.kwargs for c in bot.tg.call.call_args_list if c.args[0] == 'setMyCommands']
    public = next(c for c in calls if c['scope']['type'] == 'default')
    assert 'account_add' not in {c['command'] for c in public['commands']}
    assert len(calls) == 3
    for c in calls:
        if c['scope']['type'] != 'default':
            assert 'account_add' in {cmd['command'] for cmd in c['commands']}
            assert c['scope'].get('user_id', c['scope']['chat_id']) == ADMIN
    assert all(c['scope']['type'] != 'all_chat_administrators' for c in calls)


def test_removed_admin_menu_is_deleted(bot):
    bot.tg.call = Mock(return_value={"ok": True})
    menu.configure(bot)
    bot.admins.clear()
    bot.tg.call.reset_mock()
    menu.configure(bot)
    bot.tg.call.assert_any_call('deleteMyCommands', scope={'type': 'chat', 'chat_id': ADMIN}, language_code='')


def test_menu_failure_does_not_expose_token(bot):
    bot.tg.call = Mock(return_value={"ok": False, "error": "secret-token"})
    with pytest.raises(RuntimeError) as error:
        menu.configure(bot)
    assert 'secret-token' not in str(error.value)


def test_cancel_from_menu_clears_flow(bot):
    bot.flows[ADMIN] = {'cmd': 'account_add', 'step': 'password', 'data': {}}
    bot.handle({'message': _msg(text='/cancel')})
    assert ADMIN not in bot.flows
    assert '已取消' in bot.tg.sent[-1]
