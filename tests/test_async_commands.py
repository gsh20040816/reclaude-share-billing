import threading
from test_bot import bot, _msg
from app import store


def test_slow_write_does_not_block_read_or_sync(bot):
    entered, release = threading.Event(), threading.Event()
    def handle(upd):
        if upd['message']['text'].startswith('/login'):
            entered.set()
            assert release.wait(3)
        return
    bot.handle = handle
    slow = bot.dispatch({'message': _msg(text='/login pending')})
    assert entered.wait(1)
    try:
        bot.dispatch({'message': _msg(text='/health')}).result(timeout=1)
        bot.dispatch({'message': _msg(text='/sync')}).result(timeout=1)
        assert not slow.done()
    finally:
        release.set()
        slow.result(timeout=2)
        bot.stop()


def test_mutations_preserve_order_and_flows(bot):
    first, release = threading.Event(), threading.Event()
    seen=[]
    def handle(upd):
        text=upd['message']['text']
        if text=='/account_add':
            first.set()
            assert release.wait(3)
        seen.append(text)
    bot.handle=handle
    a=bot.dispatch({'message':_msg(text='/account_add')})
    assert first.wait(1)
    b=bot.dispatch({'message':_msg(text='email@example.invalid')})
    assert not b.done()
    release.set()
    a.result(timeout=2); b.result(timeout=2)
    assert seen==['/account_add','email@example.invalid']
    bot.stop()


def test_read_command_preserves_pending_flow_and_concurrent_registration(bot):
    bot.flows[2039687026]={'cmd':'account_add','step':'email','data':{}}
    futures=[bot.dispatch({'message':_msg(text='/help')}) for _ in range(8)]
    for future in futures: future.result(timeout=3)
    assert bot.flows[2039687026]['step']=='email'
    assert bot.conn.execute('SELECT COUNT(*) FROM users WHERE tg_user_id=2039687026').fetchone()[0]==1
    bot.stop()


def test_worker_failure_rolls_back_and_later_command_runs(bot):
    def handle(upd):
        bot.conn.execute("INSERT INTO settings VALUES ('uncommitted','1')")
        raise ValueError('test failure')
    bot.handle=handle
    bot.dispatch({'message':_msg(text='/renew bad')}).result(timeout=2)
    assert store.get_setting(bot.conn,'uncommitted') is None
    assert 'test failure' in bot.tg.sent[-1]
    bot.handle=lambda upd: None
    bot.dispatch({'message':_msg(text='/renew next')}).result(timeout=2)
    bot.stop()


def test_report_save_never_runs_in_read_lane(bot):
    assert bot.command_lane({'message':_msg(text='/report SAVE')})=='write'
    assert bot.command_lane({'message':_msg(text='/report refresh')})=='write'
    assert bot.command_lane({'message':_msg(text='/report')})=='read'
    bot.stop()
