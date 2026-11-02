"""测试环境隔离：默认路径都指向临时目录，绝不在 /opt/data 上读写。"""
import os
import tempfile

_TMP = tempfile.mkdtemp(prefix="rc-billing-tests-")

# 必须在任何 app.* 模块被导入前设置：crypto / store / runtime 在模块级读取这些变量。
# 强制覆盖（不是 setdefault）：即使 shell 里设了真实库路径，测试也绝不能写 /opt/data。
os.environ["RC_BILLING_KEY"] = os.path.join(_TMP, "secret.key")
os.environ["RC_BILLING_DB"] = os.path.join(_TMP, "billing.db")
os.environ["RC_BILLING_CONFIG"] = os.path.join(_TMP, "config.json")
