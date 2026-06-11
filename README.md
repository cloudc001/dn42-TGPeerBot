# DN42-TGPeerBot

[English](#english) | [中文](#中文)

## English

DN42-TGPeerBot is a Telegram bot for DN42 network operators. It helps users log in with DN42 registry email verification, create or manage WireGuard/BGP peers, query node status, and run common DN42 diagnostic tools.

This project is based on [Potat0000/dn42-bot](https://github.com/Potat0000/dn42-bot). 

My bot is deployed at [@Cloudc001Bot](https://t.me/Cloudc001Bot). Welcome to peer with me!


### What changed in this fork

- Adds an SSH backend for low-memory nodes.
- Keeps the original HTTP agent backend for compatibility.
- Adds a fixed node-side command script: `/usr/local/sbin/dn42-agentctl`.
- Supports optional Telegram SOCKS5/HTTP proxy.
- Uses a local DN42 registry cache for faster login email lookup.
- Checks registry freshness once per minute and reclones when the local cache is behind.
- Adds a per-user request guard: if a previous command has not replied yet, later messages from the same user are ignored until completion or timeout.
- Adds clearer whois failure handling during login.
- Adds user-facing `/autopeer` dry-run and deploy workflow from free-form peer text.

### Architecture

```text
Telegram user
    |
    v
Telegram Bot Server
    |
    +-- agent backend --> per-node HTTP agent
    |
    +-- ssh backend ----> SSH key -> sudo /usr/local/sbin/dn42-agentctl
```

The SSH backend is recommended for small VPS nodes because it does not require a resident Python HTTP agent on every node. The bot server connects only when an operation is needed.

### Features

- User login by DN42 ASN and registry email verification.
- Privileged login code for operator-controlled access.
- Peer creation, modification, removal, restart, and status query.
- AutoPeer workflow from free-form peer text. Logged-in users can create peers only for their own ASN; privileged users can operate on behalf of another ASN.
- Multi-node peer selection and node availability checks.
- WireGuard and BIRD configuration generation.
- DN42 tools: ping, tcping, traceroute, route lookup, AS path lookup, whois, dig, and NS lookup.
- Statistics commands: peer list, ranking, basic DN42 info, and optional FlapAlerted integration.

### Repository layout

```text
agent/             Optional HTTP node agent backend.
deploy/node/       SSH backend node command and deployment templates.
server/            Telegram bot server.
```

### Requirements

- Python 3.10 or newer is recommended.
- `git` on the bot server when local DN42 registry cache is enabled.
- Telegram bot token from BotFather.
- Linux nodes with BIRD and WireGuard installed.
- For node diagnostics: `ping`, `traceroute`, `birdc`, `wg`, and optional `tcping`/`vnstat`.

### Quick start

1. Install server dependencies:

```bash
cd server
pip install -r requirements.txt
```

2. Create a private runtime config:

```bash
cp server/config.example.py server/config.py
```

3. Edit `server/config.py`:

- Set `BOT_TOKEN`, `CONTACT`, `DN42_ASN`, and `SERVERS`.
- Choose `BACKEND = "ssh"` or `BACKEND = "agent"`.
- Configure email delivery in `send_email`.
- Keep `server/config.py` private. It is ignored by `.gitignore`.

4. Run the server:

```bash
cd server
python main.py
```

### SSH backend deployment

The SSH backend is the preferred deployment mode for public, multi-node, low-memory environments.

On every DN42 node:

1. Create a dedicated user, for example `dn42bot`.
2. Add the bot server SSH public key to that user's `authorized_keys`.
3. Install `deploy/node/dn42-agentctl` as `/usr/local/sbin/dn42-agentctl`.
4. Copy `deploy/node/dn42-agentctl.example.json` to `/etc/dn42-agentctl/config.json`.
5. Fill in the node's public DN42 addresses, WireGuard public key, BIRD table names, and node policy.
6. Restrict sudo with `deploy/node/sudoers.dn42bot.example`.

The bot server should run only the fixed command:

```bash
sudo /usr/local/sbin/dn42-agentctl
```

Avoid storing root passwords in the bot server. Use SSH keys and restricted sudo.

### Agent backend deployment

The agent backend keeps compatibility with the original server-plus-agent design.

On every node:

```bash
cd agent
cp agent_config.example.json agent_config.json
pip install -r requirements.txt
python main.py
```

In production, expose the agent API only to the bot server and protect it with a strong shared secret.

### Important server config keys

| Key | Purpose |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token. |
| `CONTACT` | Operator contact shown in error messages. |
| `DN42_ASN` | Your own DN42 ASN. |
| `BACKEND` | `ssh` or `agent`. |
| `SERVERS` | Region code to display name mapping. |
| `ENDPOINT` | Default node domain suffix. |
| `TELEGRAM_PROXY` | Optional SOCKS5/HTTP proxy for Telegram API. |
| `WHOIS_ADDRESS` | Whois fallback server. |
| `DN42_REGISTRY_ENABLED` | Enable local DN42 registry lookup. |
| `DN42_REGISTRY_REPOS` | Registry clone sources, tried in order. |
| `REQUEST_REPLY_TIMEOUT` | Per-message reply timeout in seconds. |
| `PRIVILEGE_CODE` | Optional operator privilege code. |
| `AUTOPEER_DEFAULT_MTU` | Default MTU used by `/autopeer` when not provided. |
| `AUTOPEER_USE_DEEPSEEK` | DeepSeek-first parsing switch. Local parser is fallback/supplement. |
| `DEEPSEEK_BASE_URL` | DeepSeek-compatible API base URL. |
| `DEEPSEEK_MODEL` | DeepSeek model name for free-form parsing. |

### AutoPeer

`/autopeer` is available to logged-in users. It accepts free-form peer information, extracts a strict internal schema, performs node-side dry-run validation, and only deploys after explicit confirmation. Non-privileged users can only create peers for their own verified ASN; privileged users can operate on behalf of another ASN.

Example:

```text
/autopeer add HK
asn 4242423777
sg.dn42.cloudc.dev:52257
public key EPgqX4UDrzirYgPbDf3t6+6XBWozuUEg4L2zWcPO5Xk=
ll fe80::1234
mtu 1420
```

Workflow:

1. Parse target node, ASN, endpoint, WireGuard public key, peer link-local, MTU, MP-BGP, and extended next-hop.
2. Validate required fields and value ranges.
3. Call the target node's fixed `dn42-agentctl autopeer_dryrun` action.
4. Show generated WireGuard and BIRD config, target paths, commands, and conflicts.
5. Deploy only after the user replies `yes`.
6. Delete AutoPeer-created peers through the normal `/remove` command. `/remove` only removes peers for the user's currently logged-in ASN.

When `AUTOPEER_USE_DEEPSEEK = True` and `DEEPSEEK_API_KEY` is available in the environment, `/autopeer` sends free-form peer text to DeepSeek first. The local parser is used only when DeepSeek is unavailable or when DeepSeek leaves fields empty. The model output is never executed directly; deployment is still performed by deterministic schema validation and node-side fixed actions.

### DN42 registry cache

When `DN42_REGISTRY_ENABLED = True`, the bot clones the DN42 registry locally and uses registry files to look up login email addresses. This is usually faster and more reliable than querying whois for every login.

The server checks the remote registry HEAD once per minute. If the local clone is behind, the registry directory is recloned. If all remotes are unavailable, the current local cache remains available.

### Security checklist before publishing

- Do not commit `server/config.py`.
- Do not commit `agent/agent_config.json`.
- Do not commit Telegram bot tokens, SMTP passwords, API keys, SSH keys, WireGuard private keys, node root passwords, or runtime databases.
- Keep only templates such as `server/config.example.py` and `deploy/node/dn42-agentctl.example.json`.
- Use a dedicated SSH user and restricted sudo for the SSH backend.
- Use strong shared secrets and firewall rules for the agent backend.

## Have a try

My bot is deployed at [@Cloudc001Bot](https://t.me/Cloudc001Bot). Welcome to peer with me!


## 中文

DN42-TGPeerBot 是一个面向 DN42 网络运营者的 Telegram 机器人。它可以帮助用户通过 DN42 registry 邮箱验证登录，创建和管理 WireGuard/BGP peer，查看节点状态，并执行常见 DN42 网络诊断命令。

本项目基于 [Potat0000/dn42-bot](https://github.com/Potat0000/dn42-bot) 修改。

我的自动peer机器人部署在 [@Cloudc001Bot](https://t.me/Cloudc001Bot). 欢迎与我peer！

### 本分支的主要改动

- 新增 SSH 后端，适合低内存 VPS 节点。
- 保留原 HTTP agent 后端，兼容原项目架构。
- 新增节点侧固定命令脚本：`/usr/local/sbin/dn42-agentctl`。
- 支持 Telegram SOCKS5/HTTP 代理。
- 使用本地 DN42 registry 缓存优先查询登录邮箱。
- 每分钟检查一次 registry 是否为最新版本；如果本地落后，则重新 clone。
- 新增用户请求保护：同一用户上一条消息尚未产生回复时，不处理后续消息，直到完成或超时。
- 登录时对 whois 故障给出更明确的错误提示。
- 新增面向用户的 `/autopeer` 自由文本 dry-run 和确认部署流程。

### 架构

```text
Telegram 用户
    |
    v
Telegram Bot Server
    |
    +-- agent backend --> 每个节点运行 HTTP agent
    |
    +-- ssh backend ----> SSH key -> sudo /usr/local/sbin/dn42-agentctl
```

对于公开项目、多节点管理、低内存 VPS，推荐使用 SSH 后端。节点上不需要常驻 Python HTTP agent，机器人服务器只在需要执行操作时通过 SSH 调用固定脚本。

### 功能

- 基于 DN42 ASN 和 registry 邮箱验证码登录。
- 支持特权代码登录。
- 创建、修改、删除、重启和查看 peer。
- AutoPeer 支持从自由文本创建 peer。普通登录用户只能为自己的 ASN 创建，管理员可代操作其他 ASN。
- 支持多节点选择和节点可用性检查。
- 生成 WireGuard 和 BIRD 配置。
- DN42 工具：ping、tcping、traceroute、route lookup、AS path lookup、whois、dig、NS lookup。
- 统计命令：peer list、ranking、DN42 基础信息，以及可选 FlapAlerted 集成。

### 目录结构

```text
agent/             可选 HTTP 节点 agent 后端。
deploy/node/       SSH 后端节点脚本和部署模板。
server/            Telegram 机器人服务端。
```

### 运行要求

- 推荐 Python 3.10 或更高版本。
- 启用本地 DN42 registry 缓存时，机器人服务器需要安装 `git`。
- Telegram BotFather 创建的 bot token。
- DN42 节点需要安装 BIRD 和 WireGuard。
- 诊断命令依赖 `ping`、`traceroute`、`birdc`、`wg`，可选 `tcping`/`vnstat`。

### 快速开始

1. 安装服务端依赖：

```bash
cd server
pip install -r requirements.txt
```

2. 创建私有运行配置：

```bash
cp server/config.example.py server/config.py
```

3. 编辑 `server/config.py`：

- 设置 `BOT_TOKEN`、`CONTACT`、`DN42_ASN` 和 `SERVERS`。
- 选择 `BACKEND = "ssh"` 或 `BACKEND = "agent"`。
- 在 `send_email` 中配置邮件发送方式。
- 不要公开 `server/config.py`，该文件已被 `.gitignore` 忽略。

4. 启动服务：

```bash
cd server
python main.py
```

### SSH 后端部署

SSH 后端适合公开、多节点、低内存环境。

在每个 DN42 节点上：

1. 创建专用用户，例如 `dn42bot`。
2. 将机器人服务器的 SSH 公钥写入该用户的 `authorized_keys`。
3. 将 `deploy/node/dn42-agentctl` 安装为 `/usr/local/sbin/dn42-agentctl`。
4. 将 `deploy/node/dn42-agentctl.example.json` 复制为 `/etc/dn42-agentctl/config.json`。
5. 填写节点的 DN42 地址、WireGuard 公钥、BIRD 表名和开放策略。
6. 使用 `deploy/node/sudoers.dn42bot.example` 限制 sudo 权限。

机器人服务器应只允许执行固定命令：

```bash
sudo /usr/local/sbin/dn42-agentctl
```

不要在机器人服务器保存 root 密码。推荐使用 SSH key 和受限 sudo。

### Agent 后端部署

Agent 后端用于兼容原项目的 server + agent 架构。

在每个节点上：

```bash
cd agent
cp agent_config.example.json agent_config.json
pip install -r requirements.txt
python main.py
```

生产环境中，agent API 应只允许机器人服务器访问，并使用强随机共享密钥。

### 重要服务端配置

| 配置项 | 用途 |
| --- | --- |
| `BOT_TOKEN` | Telegram bot token。 |
| `CONTACT` | 错误提示中显示的管理员联系方式。 |
| `DN42_ASN` | 你自己的 DN42 ASN。 |
| `BACKEND` | `ssh` 或 `agent`。 |
| `SERVERS` | 节点代码到显示名称的映射。 |
| `ENDPOINT` | 默认节点域名后缀。 |
| `TELEGRAM_PROXY` | Telegram API 使用的可选 SOCKS5/HTTP 代理。 |
| `WHOIS_ADDRESS` | whois fallback 服务器。 |
| `DN42_REGISTRY_ENABLED` | 是否启用本地 DN42 registry 查询。 |
| `DN42_REGISTRY_REPOS` | registry clone 来源，按顺序尝试。 |
| `REQUEST_REPLY_TIMEOUT` | 单条消息回复超时时间，单位秒。 |
| `PRIVILEGE_CODE` | 可选特权登录代码。 |
| `AUTOPEER_DEFAULT_MTU` | `/autopeer` 未提供 MTU 时使用的默认值。 |
| `AUTOPEER_USE_DEEPSEEK` | DeepSeek 优先解析开关；本地解析只做 fallback/补漏。 |
| `DEEPSEEK_BASE_URL` | DeepSeek 兼容 API 地址。 |
| `DEEPSEEK_MODEL` | 用于自由文本解析的 DeepSeek 模型名。 |

### AutoPeer

`/autopeer` 面向已登录用户开放。它可以接收自由格式 peer 信息，提取严格结构，调用节点侧 dry-run 校验，并且只有用户明确回复 `yes` 后才会部署。普通用户只能为自己已验证登录的 ASN 创建 peer；管理员可代操作其他 ASN。

示例：

```text
/autopeer add HK
asn 4242423777
sg.dn42.cloudc.dev:52257
public key EPgqX4UDrzirYgPbDf3t6+6XBWozuUEg4L2zWcPO5Xk=
ll fe80::1234
mtu 1420
```

流程：

1. 解析目标节点、ASN、endpoint、WireGuard 公钥、对端 link-local、MTU、MP-BGP 和 extended next-hop。
2. 校验必填字段和值范围。
3. 调用目标节点固定动作 `dn42-agentctl autopeer_dryrun`。
4. 展示将生成的 WireGuard/BIRD 配置、目标路径、将执行的命令和冲突信息。
5. 用户回复 `yes` 后才执行部署。
6. AutoPeer 创建的 peer 统一通过普通 `/remove` 命令删除。`/remove` 只会删除当前登录 ASN 的 peer。

当 `AUTOPEER_USE_DEEPSEEK = True` 且环境变量中存在 `DEEPSEEK_API_KEY` 时，`/autopeer` 会先把自由格式 peer 文本交给 DeepSeek 解析。本地解析只在 DeepSeek 不可用或字段为空时做 fallback/补漏。模型输出不会被直接执行，部署仍由严格 schema 校验和节点侧固定动作完成。

### DN42 registry 本地缓存

当 `DN42_REGISTRY_ENABLED = True` 时，机器人会在本地 clone DN42 registry，并优先从 registry 文件中查询登录邮箱。这通常比每次登录都查询 whois 更快、更稳定。

服务端每分钟检查一次远端 registry HEAD。如果本地版本落后，会重新 clone registry 目录。如果远端暂时不可用，则保留并继续使用现有本地缓存。

## 尝试一下

我的自动peer机器人部署在 [@Cloudc001Bot](https://t.me/Cloudc001Bot). 欢迎与我peer！
