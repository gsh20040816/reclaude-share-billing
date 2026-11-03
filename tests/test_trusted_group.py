from unittest.mock import Mock
import pytest
from test_bot import bot, ADMIN, _msg


@pytest.mark.parametrize('status,member,expected',[
 ('member',None,True),('administrator',None,True),('creator',None,True),
 ('restricted',True,True),('restricted',False,False),('left',None,False),('kicked',None,False)])
def test_private_membership_checked_live(bot,status,member,expected):
 bot.allowed.add(-5437139449)
 bot.tg.call=Mock(return_value={'ok':True,'result':{'status':status,'is_member':member}})
 assert bot.allowed_chat({'id':555,'type':'private'},555) is expected
 bot.tg.call.assert_called_with('getChatMember',chat_id=-5437139449,user_id=555)
 bot.tg.call.return_value={'ok':True,'result':{'status':'left'}}
 assert not bot.allowed_chat({'id':555,'type':'private'},555)


def test_membership_error_denied_and_no_admin_escalation(bot):
 bot.allowed.add(-5437139449)
 bot.tg.call=Mock(return_value={'ok':False})
 assert not bot.allowed_chat({'id':555,'type':'private'},555)
 bot.tg.call.return_value={'ok':True,'result':{'status':'administrator'}}
 bot.handle({'message':_msg(uid=555,text='/revoke 54426')})
 assert '仅管理员' in bot.tg.sent[-1]
 assert bot.conn.execute('SELECT role FROM users WHERE tg_user_id=555').fetchone()[0]=='user'
 assert not bot.allowed_chat({'id':-999,'type':'group'},555)
 assert bot.allowed_chat({'id':ADMIN,'type':'private'},ADMIN)
