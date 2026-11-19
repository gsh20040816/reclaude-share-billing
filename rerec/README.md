# rerec

macOS、Linux 和 Windows 共用同一个单文件客户端。Windows 需要 Git Bash、Windows 版 Python 3.10+ 和可从 Git Bash 启动的官方 reclaude。按[仓库首页的安装命令](../README.md#安装-rerec-客户端)装到 `~/.local/bin/rerec`。也可以把本目录里的单文件 `rerec` 复制到 PATH 中并赋予执行权限。服务端沿用本仓库 Bot 的 Python 环境。

管理员在 Bot 配置中加 `"rerec_port": 8187`，重启 Bot，把 `https://rerec.shgao.dev/` 反向代理到 `http://127.0.0.1:8187/`。实际授权和释放席位需要已有配置 `dry_run=false`。Bot 进程处理 Telegram 和 HTTP 两个入口的写请求，共用写队列、会话池和 SQLite。不要另外启动一份 Bot。

1. 私聊 Bot 发送 `/rerec`，在终端跑 `rerec pair`，输入密钥（默认服务地址为 `https://rerec.shgao.dev`，自建实例在 Bot 配置里填写 `rerec_public_url`，用户复制 Bot 回复中带 `REREC_URL` 的配对命令）。每台机器可以单独配对。`/rerec reset` 撤销本人所有密钥。
2. `rerec` 启动 Claude Code。没有有效绑定时，它会启动官方登录，由服务端批准并登记设备。`rerec login` 只完成登录。已有官方绑定的用户切换前，设备须已绑定到本人的 Telegram 身份。
3. `rerec switch fable` 或 `rerec switch opus` 换账号，然后在原工作目录启动指定模型。也可以用官方 CLI 能识别的完整模型名。省略模型会提示输入。服务端检查 5 小时、全模型周额度和适用的模型专项周额度；能复用本机绑定或直接分配时优先。满位时先询问是否释放本人闲置超过 90 分钟的设备，其他人的设备要求闲置超过 6 小时。时间按真实请求计算。
4. 换号会停止本用户的 Claude/ReClaude 终端进程，包括其他终端里的会话，等待退出后再迁移。历史会话可通过 Claude Code 的恢复入口打开。脚本沿用原迁移工具的白名单镜像策略：以来源账号为准，覆盖目标中的同名历史，并清理只在目标白名单目录内的文件。覆盖和清理前在目标账号目录保存备份。项目目录信任按路径合并，目标设备凭据保留。中断后重新跑 rerec 可接着处理已建立的目标绑定。

本地配置在 `~/.config/rerec/client.json`，切换进度放在 `~/.local/state/rerec/`，支持 XDG_CONFIG_HOME 和 XDG_STATE_HOME。

### 与普通 Claude Code 共用配置

共享源是本机的 `~/.claude/`。启动、登录或换号时，rerec 把账号里对应的条目链接到这里。默认共用 `CLAUDE.md`、`agents/`、`commands/`、`output-styles/`、`hooks/`、`scripts/`、`handoff/`、`keybindings.json` 和各个自定义 skill。账号独有的文件会补到共享目录；同名内容不同用共享版，账号原件保存在该设备目录的 `.rerec-link-backup-*`。已有的用户自建链接保留。新建的 skill 在下次启动时接入；`skills/synced/` 留在各账号。项目记忆继续随历史复制。

启动和登录时，从 `~/.claude/settings.json` 复制共享设置。换号时先叠加旧账号的共享字段，再叠加普通 Claude 的共享字段。同名顶层字段整项替换，只在目标账号存在的字段保留。`env`、`apiKeyHelper`、`awsAuthRefresh`、`awsCredentialExport`、`gcpAuthRefresh`、`forceLoginMethod`、`forceLoginOrgUUID` 和 `modelOverrides` 始终取目标账号自己的值。其余字段都参与复制，包括 hooks 触发规则和权限偏好。

可在 `~/.config/rerec/migration.json` 通过 `links` 数组选择上述条目，例如 `{"links":["CLAUDE.md","skills"]}`。省略该字段采用默认清单。移出清单后，该账号下次启动会把原受管链接还原为独立副本。关闭 skills 共享时，换号沿用原来的 skills 复制逻辑。Windows 创建符号链接需要启用“开发人员模式”或具备创建符号链接的权限。
