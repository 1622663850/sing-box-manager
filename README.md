# sing-box-manager

Linux 上的中文终端管理脚本。安装面板后，输入 `sudo sing-box` 打开菜单；root 用户直接输入 `sing-box`。管理命令与官方核心使用不同路径，保留官方命令透传。

## 功能

| 菜单 | 功能 | 行为 |
|---|---|---|
| 1 | 安装 sing-box | 从官方 GitHub Release 下载最新稳定核心；已安装时更新 |
| 2 | 卸载 sing-box | 停止并禁用服务，删除核心及 unit，保留面板、配置和备份 |
| 3 | 启动 | 检查配置后启动，并检查服务状态 |
| 4 | 设置开机自启 | 启用开机启动，当前运行状态不变 |
| 5 | 重新启动 | 检查配置后重启，并检查服务状态 |
| 6 | 禁用 | 立即停止服务，同时关闭开机自启 |
| 7 | 查看日志 | 显示最近 100 行服务日志 |
| 8 | 添加配置 | 协议向导、导入单个入站或导入完整 JSON |
| 9 | 查看配置 | 配置列表、完整 JSON、客户端信息、配置检查 |

面板是交互式终端程序，没有 Web 服务、数据库或常驻面板进程。

## 系统要求

- Linux，Bash 4 或更新版本，**正在运行的 systemd**。
- x86_64 / amd64 或 aarch64 / ARM64。
- 管理操作使用 root 或 sudo；安装核心时能访问 GitHub API 和官方 Release 下载地址。
- 依赖安装分支包括 Debian / Ubuntu 的 `apt-get`、RHEL 系的 `dnf` / `yum`、openSUSE 的 `zypper`、Arch 的 `pacman`。这是支持的安装路径，不代表每个发行版均完成真机验证。
- 依赖包括 `curl`、`jq`、`tar`、`gzip`、`sha256sum`、`flock`、`ss`、`openssl` 和 CA 证书；缺少时尝试由上述包管理器安装。

**不支持 Alpine / OpenRC、OpenWrt，以及未启用 systemd 的容器或 WSL。** 不自动接管其他脚本或包管理器已安装的 sing-box。

## 首次使用

本仓库默认是私有仓库。先登录有访问权限的 GitHub 账号，在仓库中下载 `sing-box-manager.sh`，再将它上传至 Linux 服务器。无需下载整个仓库即可运行。

```bash
# 在存放脚本的目录执行；请使用 bash，不要使用 sh。
sudo bash sing-box-manager.sh setup
sudo sing-box
```

`setup` 安装面板入口及必要依赖，核心由菜单 1 安装。首次使用顺序：**1 安装核心 → 8 添加配置 → 3 启动 → 4 设置开机自启**。

若服务器已安装 Git、具备此私有仓库读取权限，并已配置对应的 GitHub SSH 密钥，也可以：

```bash
git clone git@github.com:1622663850/sing-box-manager.git
cd sing-box-manager
sudo bash sing-box-manager.sh setup
sudo sing-box
```

私有仓库不能通过未认证的 `raw.githubusercontent.com` 下载命令获取脚本。首次安装的基础配置不含入站，需要添加配置后才有可连接的节点。

## 添加与查看配置

执行 `sudo sing-box add` 或选择菜单 8：

1. **VLESS + REALITY + Vision**：自动生成 UUID、密钥及 short ID；填写握手域名和监听端口。
2. **VLESS + WebSocket，无 TLS**：自动生成 UUID；填写端口和 WS 路径。
3. **VLESS TCP，无 TLS**：自动生成 UUID；填写监听端口。
4. **Shadowsocks 2022**：使用 `2022-blake3-aes-128-gcm`，自动生成随机密钥。
5. **导入单个 inbound JSON**：追加至现有 `.inbounds`，保留其他配置；检查名称、端口冲突。
6. **导入完整 JSON**：交互确认后替换当前主配置，保留旧配置备份。

文件导入使用严格 JSON，不接受注释或 JSONC。单入站导入文件应是包含 `type`、`tag`、`listen_port` 的一个对象，完整配置应是完整对象。相对文件路径按服务工作目录 `/var/lib/sing-box-manager` 解析，证书等文件建议使用绝对路径。

向导可填写客户端连接用的公网 IP / 域名；填入后显示客户端 outbound JSON 和分享链接，也可稍后查看：

```bash
sudo sing-box info
sudo sing-box client my-node
sudo sing-box client my-node example.com
sudo sing-box add-file /root/inbound.json
sudo sing-box import /root/config.json
sudo sing-box validate
```

客户端导出用于本脚本四种向导支持的配置结构。其他导入配置不保证能导出；不支持的结构应使用原有客户端配置。导入的 REALITY 节点若没有对应公钥元数据，将拒绝生成不完整客户端信息。导出的 JSON 是 **outbound 对象**，应放入客户端完整配置的 `outbounds` 数组。

服务正在运行时，配置保存成功后自动重启；新配置启动失败会尝试恢复旧配置和服务。服务原本停止时，保存配置不会自动启动。

### 网络配置

- 在云安全组和服务器防火墙放行所选监听端口。VLESS 模板使用 TCP，Shadowsocks 模板使用 TCP 和 UDP；脚本不自动修改防火墙。
- WS / TCP 的“无 TLS”模板不提供传输层加密，可用于已有安全隧道或由反向代理提供 TLS 的部署；客户端必须与实际部署一致。
- REALITY 握手域名由使用者选择，需从服务器访问并适合 TLS 1.3 握手；配置检查不能代替实际连接验证。
- Shadowsocks 2022 客户端与服务器需保持实际时钟同步，不要求使用相同的显示时区。
- 当前没有 TLS 证书自动申请、续期或反向代理自动配置功能。

## 常用命令

```bash
sudo sing-box                     # 中文面板
sudo sing-box install             # 安装 / 更新官方最新稳定核心
sudo sing-box install 1.14.3       # 指定正式版本，版本号仅作示例
sudo sing-box start               # 启动，不更改开机自启设置
sudo sing-box enable              # 开机自启，不立即启动
sudo sing-box restart             # 重新启动
sudo sing-box disable             # 停止并关闭开机自启
sudo sing-box stop                # 仅停止，保留开机自启设置
sudo sing-box status              # 状态与版本
sudo sing-box log                 # 最近 100 行日志
sudo sing-box log --follow        # 持续日志，Ctrl+C 退出
sudo sing-box help                # 命令帮助
```

原生命令透传示例：

```bash
sing-box version
sing-box generate uuid
sudo sing-box check -c /etc/sing-box/config.json
sudo sing-box core help
sudo sing-box core run -c /root/another-config.json
```

`core`（或 `bin`）后面的参数直接交给核心。`core run` 会在前台运行独立进程；使用已有服务的端口前先停止服务。

## 安装保护与文件位置

安装从 SagerNet 官方 Release API 获取指定架构文件及 SHA256 digest。下载地址必须匹配官方仓库，摘要校验通过后才执行核心；发行文件缺少可用摘要时拒绝安装。升级前检查现有配置与新核心是否兼容，安装失败时尝试恢复原核心及服务文件。

发现其他安装方式的命令、配置目录、服务或 service override 时拒绝覆盖。请先用原管理方式备份和卸载，再使用本脚本。修改操作使用文件锁，配置写入采用临时文件检查和原子替换。

| 路径 | 用途 |
|---|---|
| `/usr/local/bin/sing-box` | 面板脚本入口 |
| `/usr/local/lib/sing-box-manager/sing-box` | 官方核心 |
| `/usr/local/lib/sing-box-manager/.managed` | 管理标记 |
| `/etc/sing-box/config.json` | 实际服务配置 |
| `/etc/systemd/system/sing-box.service` | systemd unit |
| `/var/lib/sing-box-manager/clients/` | 向导节点连接元数据 |
| `/var/lib/sing-box-manager/backups/` | 配置、卸载及更新备份 |

配置 JSON、连接元数据、配置备份权限为 `600`，配置和状态目录权限为 `700`。完整配置及连接信息包含凭据；请勿提交真实 UUID、密码、私钥、备份到仓库。日志由 systemd journal 管理。

## 卸载与更新面板

```bash
sudo sing-box uninstall       # 交互确认；卸载核心和服务
sudo sing-box remove-panel    # 核心已卸载后，移除面板命令
```

卸载核心后仍可运行 `sing-box`，通过菜单 1 重新安装。配置、连接信息及备份始终保留；`remove-panel` 也保留这些文件和管理标记，便于恢复。它不是“删除所有数据”的操作。

更新面板时，下载仓库中的新脚本并再次运行 `sudo bash sing-box-manager.sh setup`；更新核心使用菜单 1 或 `sudo sing-box install`。

## 验证与限制

仓库中的 `tests/test_manager.py` 提供离线回归测试，使用临时目录和模拟的服务命令，不在测试机器上安装服务或修改系统配置：

```bash
bash -n sing-box-manager.sh
python3 -m unittest discover -s tests -v
# 使用已取得的可信官方核心，额外检查生成的配置：
SB_TEST_CORE=/absolute/path/to/sing-box python3 -m unittest discover -s tests -v
# 用真实客户端和服务端核心测试本机回环代理转发：
python3 tests/smoke_core.py --core /absolute/path/to/sing-box
```

首次发布已用官方 v1.14.3（amd64）完成 Bash 语法检查、ShellCheck、33 项回归测试，以及 VLESS TCP、VLESS WS、Shadowsocks 2022 的真实核心回环转发。REALITY 只完成服务端和客户端配置检查，未进行公网握手验证。GitHub Actions 会重复静态检查、回归和回环测试；系统服务启停与回滚使用模拟 systemctl 验证。

离线服务模拟和 `sing-box check` 不能证明服务器网络、目标域名、防火墙及客户端端到端连通性。真机部署后应检查服务日志，并用实际客户端验证连接。支持范围之外的发行版和初始化系统不作兼容承诺。

## 参考与许可证

- [sing-box 官方安装与服务管理](https://sing-box.sagernet.org/installation/package-manager/)
- [sing-box 官方配置文档](https://sing-box.sagernet.org/configuration/)
- [SagerNet/sing-box 官方发行版](https://github.com/SagerNet/sing-box/releases)
- [233boy/sing-box](https://github.com/233boy/sing-box)：参考其终端菜单、命令透传和管理思路；本仓库独立实现，没有复制其脚本代码。

本仓库脚本采用 [MIT License](LICENSE)。sing-box 核心独立下载，遵循其自身许可证；本仓库不重新分发核心二进制。
