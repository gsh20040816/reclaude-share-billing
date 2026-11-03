from test_bot import bot, ADMIN, _msg


def add(bot, aid, did, org, uid=None, state='active'):
    bot.conn.execute('INSERT INTO devices(account_id,rc_device_id,org_id,user_id,state,name) VALUES (?,?,?,?,?,?)',
                     (aid,did,org,uid,state,'device-'+str(did)))
    bot.conn.commit()


def test_admin_sees_unbound_and_history_and_deduplicates(bot):
    add(bot,'a1',100,1)
    add(bot,'other',100,1)
    add(bot,'a1',101,1,555,'missing')
    bot.command(_msg()['chat'],_msg(),ADMIN,'/devices all',None)
    text='\n'.join(bot.tg.sent)
    assert '全部设备' in text and '未绑定' in text and 'missing' in text
    assert text.count('device-100')==1
    assert 'a1:100' in text
    assert 'tg:555' in text


def test_nonadmin_cannot_read_others_and_admin_mine_is_scoped(bot):
    add(bot,'a1',100,1,555)
    add(bot,'a1',101,1,ADMIN)
    msg=_msg(uid=555)
    bot.command(msg['chat'],msg,555,'/devices',None)
    assert 'device-100' in bot.tg.sent[-1] and 'device-101' not in bot.tg.sent[-1]
    bot.command(msg['chat'],msg,555,'/devices all',None)
    assert '仅管理员' in bot.tg.sent[-1]
    bot.command(_msg()['chat'],_msg(),ADMIN,'/devices mine',None)
    assert 'device-101' in bot.tg.sent[-1] and 'device-100' not in bot.tg.sent[-1]


def test_large_admin_list_is_split_without_losing_devices(bot):
    for i in range(100,180): add(bot,'a1',i,1)
    bot.command(_msg()['chat'],_msg(),ADMIN,'/devices',None)
    assert len(bot.tg.sent)>1
    assert all(len(text)<3800 for text in bot.tg.sent)
    assert all(('device-'+str(i)) in '\n'.join(bot.tg.sent) for i in range(100,180))


def test_default_excludes_history_but_all_includes_it(bot):
    add(bot,'a1',100,1,ADMIN)
    add(bot,'a1',101,1,ADMIN,'missing')
    add(bot,'a1',102,1,ADMIN,'revoked')
    bot.command(_msg()['chat'],_msg(),ADMIN,'/devices',None)
    text=bot.tg.sent[-1]
    assert '活跃设备' in text and 'device-100' in text
    assert 'device-101' not in text and 'device-102' not in text
    bot.command(_msg()['chat'],_msg(),ADMIN,'/devices all',None)
    assert all('device-'+str(i) in bot.tg.sent[-1] for i in (100,101,102))
