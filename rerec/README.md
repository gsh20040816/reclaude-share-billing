# rerec

macOS 和 Linux 客户端需要 Bash、Python 3.10+ 和已安装的官方 reclaude。按[仓库首页的安装命令](../README.md#安装-rerec-客户端)装到 `~/.local/bin/rerec`。也可以把本目录里的单文件 `rerec` 复制到 PATH 中并赋予执行权限。服务端沿用本仓库 Bot 的 Python 环境。

管理员在 Bot 配置中加 `"rerec_port": 8187`，重启 Bot，把 HTTPS 路径 `/rerec` 反向代理到 `http://127.0.0.1:8187/rerec`。实际授权和释放席位需要已有配置 `dry_run=false`。Bot 进程处理 Telegram 和 HTTP 两个入口的写请求，共用写队列、会话池和 SQLite。不要另外启动一份 Bot。

1. 私聊 Bot 发送 `/rerec`，在终端跑 `rerec pair`，输入服务地址和密钥。每台机器可以单独配对。`/rerec reset` 撤销本人所有密钥。
2. `rerec` 启动 Claude Code。没有有效绑定时，它会启动官方登录，由服务端批准并登记设备。`rerec login` 只完成登录。已有官方绑定的用户切换前，设备须已绑定到本人的 Telegram 身份。
3. `rerec switch fable` 或 `rerec switch opus` 换账号，然后在原工作目录启动指定模型。也可以用官方 CLI 能识别的完整模型名。省略模型会提示输入。服务端检查 5 小时、全模型周额度和适用的模型专项周额度；能复用本机绑定或直接分配时优先。满位时先询问是否释放本人闲置超过 90 分钟的设备，其他人的设备要求闲置超过 6 小时。时间按真实请求计算。
4. 换号会停止本用户的 Claude/ReClaude 终端进程，包括其他终端里的会话，等待退出后再迁移。历史会话可通过 Claude Code 的恢复入口打开。脚本沿用原迁移工具的白名单镜像策略：以来源账号为准，覆盖目标中的同名历史，并清理只在目标白名单目录内的文件。覆盖和清理前在目标账号目录保存备份。项目目录信任按路径合并，目标设备凭据保留。中断后重新跑 rerec 可接着处理已建立的目标绑定。

本地配置在 `~/.config/rerec/client.json`，切换进度放在 `~/.local/state/rerec/`，支持 XDG_CONFIG_HOME 和 XDG_STATE_HOME。
