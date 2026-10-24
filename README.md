# sing-box-manager

Linux 上的中文终端管理脚本。安装面板后，输入 `sudo sing-box` 打开菜单；root 用户直接输入 `sing-box`。管理命令与官方核心使用不同路径，保留官方命令透传。

## 功能

| 菜单 | 功能 | 行为 |
|---|---|---|
| 1 | 安装 sing-box | 首次安装官方最新稳定核心；已安装时提示使用更新选项 |
| 2 | 卸载 sing-box | 停止并禁用服务，删除核心及 unit，保留面板、配置和备份 |
| 3 | 启动 | 检查配置后启动，并检查服务状态 |
| 4 | 设置开机自启 | 启用开机启动，当前运行状态不变 |
| 5 | 重新启动 | 检查配置后重启，并检查服务状态 |
| 6 | 禁用 | 立即停止服务，同时关闭开机自启 |
| 7 | 查看日志 | 显示最近 100 行服务日志 |
| 8 | 添加配置 | 协议向导、导入单个入站或导入完整 JSON |
| 9 | 查看配置 | 配置列表及完整分享链接、完整 JSON、客户端信息、配置检查 |
| 10 | 更新 sing-box 核心 | 独立更新已安装的核心，保留现有配置，失败时尝试回滚 |
| 11 | 修改配置 | 编辑已有节点，校验并备份，按需重启，失败时恢复配置和连接信息 |
| 12 | 删除配置 | 确认后删除节点及连接信息，阻止删除仍被引用的节点，失败回滚 |
| 13 | 配置维护 | 成套备份、列表、恢复、批量客户端导出、只读诊断 |
| 14 | 更新面板脚本 | 获取 GitHub 最新脚本，检查并备份旧版后替换，成功后重新载入菜单 |
| 15 | 开启 TCP BBR | 确认后检查内核能力、备份参数和专属配置，启用并持久化，失败恢复 |

面板是交互式终端程序，没有 Web 服务、数据库或常驻面板进程。

## 系统要求

- Linux，Bash 4 或更新版本，**正在运行的 systemd**。
- x86_64 / amd64 或 aarch64 / ARM64。
- 管理操作使用 root 或 sudo；安装核心时能访问 GitHub API 和官方 Release 下载地址。
- 依赖安装分支包括 Debian / Ubuntu 的 `apt-get`、RHEL 系的 `dnf` / `yum`、openSUSE 的 `zypper`、Arch 的 `pacman`。这是支持的安装路径，不代表每个发行版均完成真机验证。
- 依赖包括 `curl`、`jq`、`awk`、`python3`、`tar`、`gzip`、`sha256sum`、`flock`、`ss`、`openssl` 和 CA 证书；缺少时尝试由上述包管理器安装。注释保留使用内嵌的 Python 3 标准库代码，不需要安装 pip 包或额外脚本文件。

**不支持 Alpine / OpenRC、OpenWrt，以及未启用 systemd 的容器或 WSL。** 不自动接管其他脚本或包管理器已安装的 sing-box。

## 首次使用：直接下载运行

本项目采用 MIT 许可证。仓库公开后，下面的命令可直接在 Linux 服务器上下载并运行，无需登录 GitHub。请在可写目录中执行，使用 `bash` 运行。

### 下载、安装面板并打开菜单

```bash
curl -fL --retry 3 \
  https://raw.githubusercontent.com/1622663850/sing-box-manager/main/sing-box-manager.sh \
  -o sing-box-manager.sh && \
sudo bash sing-box-manager.sh setup && \
sudo sing-box
```

已经使用 **root** 登录时，可以直接复制这一行：

```bash
curl -fL --retry 3 https://raw.githubusercontent.com/1622663850/sing-box-manager/main/sing-box-manager.sh -o sing-box-manager.sh && bash sing-box-manager.sh setup && sing-box
```

`setup` 安装或更新面板脚本及必要依赖，核心由菜单 **1** 安装。首次使用顺序：**1 安装核心 → 8 添加配置 → 3 启动 → 4 设置开机自启**。以后输入 `sudo sing-box` 打开菜单，root 用户直接输入 `sing-box`。

如果提示没有 `curl`，Debian / Ubuntu 可先执行 `sudo apt-get update && sudo apt-get install -y curl ca-certificates`；使用 DNF 的系统可先执行 `sudo dnf install -y curl ca-certificates`。

下载命令使用 `&&` 串联，下载失败时不会继续执行脚本。脚本需要复制自身到面板入口，因此以上命令先保存为本地文件，再执行该文件。

### 使用 Git 克隆

```bash
git clone https://github.com/1622663850/sing-box-manager.git
cd sing-box-manager
sudo bash sing-box-manager.sh setup && \
sudo sing-box
```

也可以[直接下载脚本](https://raw.githubusercontent.com/1622663850/sing-box-manager/main/sing-box-manager.sh)，上传到服务器后运行。首次安装的基础配置不含入站，需要添加配置后才有可连接的节点。

## 添加与查看配置

执行 `sudo sing-box add` 或选择菜单 8：

向导输入支持 Backspace、Delete、左右光标编辑；输错整行可按 Ctrl+U 清空，直接回车使用提示中的默认值。

1. **VLESS + TCP + REALITY + Vision**：自动生成 UUID、密钥及 short ID；填写握手域名和监听端口。
2. **VLESS + WebSocket，无 TLS**：自动生成 UUID；填写端口和 WS 路径。
3. **VLESS TCP，无 TLS**：自动生成 UUID；填写监听端口。
4. **Shadowsocks 2022**：使用 `2022-blake3-aes-128-gcm`，自动生成随机密钥。
5. **导入单个 inbound JSON**：追加至现有 `.inbounds`，保留其他配置；检查名称、端口冲突。
6. **导入完整 JSON**：交互确认后替换当前主配置，保留旧配置备份。
7. **Hysteria 2**：自动生成密码，使用 UDP 和 TLS。
8. **TUIC**：自动生成 UUID 和密码，使用 UDP 和 TLS。
9. **AnyTLS**：自动生成密码，使用 TCP 和 TLS。
10. **VLESS + gRPC + TLS**：自动生成 UUID，填写 gRPC 服务名及证书信息。
11. **VMess + TCP**：自动生成 UUID，使用 AEAD（alterId 为 0），不额外启用 TLS。
12. **VMess + TCP + TLS**。
13. **VMess + WebSocket**：填写 WS 路径，不额外启用 TLS。
14. **VMess + WebSocket + TLS**。
15. **VMess + gRPC + TLS**：填写 gRPC 服务名。
16. **VLESS + gRPC + REALITY**：填写 gRPC 服务名及支持 TLS 1.3 / HTTP/2 的握手域名；自动生成 UUID、密钥及 short ID，**不启用 Vision**。

普通 TLS 模板需要已有的证书域名/IP、服务器上的证书（建议完整证书链）及无口令私钥绝对路径。证书文件可使用 `.pem`、`.cer`、`.crt` 扩展名，私钥通常使用 `.key` 扩展名；证书和私钥的文件内容都必须为 PEM 格式，扩展名不代表实际编码格式。向导检查文件可读性、证书有效期、域名/IP 匹配和密钥配对，随后使用真实核心校验完整配置；失败时不写入配置。客户端导出默认验证证书，不设置 `insecure`。使用自签证书或私有 CA 时，须在客户端另外配置可信证书；分享链接不会包含证书或私钥，也不能代替证书信任配置。证书签发和续期仍由用户管理。

Vision 只用于此处的 TCP + REALITY 模板；gRPC + REALITY 不设置 `flow`。gRPC 服务名须为 1–128 位字母、数字、点、短横线或下划线，并以字母或数字开头，两端须一致。

v1.4.1 起，主配置和配置导入支持字符串之外的 `//` 单行注释和 `/* ... */` 块注释；字符串内的 `https://`、`//`、转义引号和反斜杠会完整保留。其余部分仍须符合 JSON 语法，不支持尾随逗号、单引号或未闭合的块注释。单入站导入文件应是包含 `type`、`tag`、`listen_port` 的一个对象，完整配置应是完整对象。相对文件路径按服务工作目录 `/var/lib/sing-box-manager` 解析，证书等文件建议使用绝对路径。

v1.4.2 起，修改并保存配置也会保留原注释和排版：修改端口、UUID、密码等字段时，只替换对应的值；其余字段的 `//`、`/* ... */` 注释、空格、换行和转义写法保持原样。新增节点保留已有节点的注释，删除节点时该节点内部的注释随节点删除，其余节点保留。新插入的内容使用 JSON 排版。保留处理失败或结果与目标数据不一致时拒绝保存，不会静默去掉注释。保存仍先备份、检查完整配置并在失败时回滚。

成套备份增加配置原文 `config_source`，恢复时核对它与配置数据一致后还原注释和排版；没有该字段的旧备份仍能恢复配置数据，但无法找回旧备份未保存的注释。显式导入完整配置以传入文件为准，保留导入文件自身注释，不合并旧配置的注释。启动、重启、检查、列表、导出和无变化保存同样保留原文件。核心始终另外检查最终原始配置，注释保留不会绕过核心校验。

向导可填写客户端连接用的公网 IP / 域名；填入后显示客户端 outbound JSON 和分享链接，也可稍后查看：

```bash
sudo sing-box info
sudo sing-box client my-node
sudo sing-box client my-node example.com
sudo sing-box add-file /root/inbound.json
sudo sing-box import /root/config.json
sudo sing-box validate
```

客户端导出支持上述协议向导生成的配置，提供 sing-box outbound JSON 和分享链接（VMess 为 Base64 JSON 链接）。不同客户端对协议及链接格式的支持有差异，以生成的 sing-box JSON 为准。其他导入配置不保证能导出；超出模板的 TLS、传输或认证设置会被拒绝导出。导入的 REALITY 节点若没有对应公钥元数据，将拒绝生成不完整客户端信息。导出的 JSON 是 **outbound 对象**，应放入客户端完整配置的 `outbounds` 数组。

“查看配置 → 1. 配置列表和完整分享链接”会按配置名称显示可整行复制的分享链接，供其他客户端导入。地址优先级为：命令显式传入的地址 → 单个配置保存的地址 → 全局默认地址 → Hysteria 2 当前配置的 `tls.server_name`。因此 Hysteria 2 没有单独填写公网 IP/域名时，可直接用证书域名生成链接；这个域名需解析到实际服务器。REALITY 仍需配置对应的公钥，缺失或过期的连接元数据不会被复用。分享链接含连接凭据，请妥善保管。

通过“查看配置 → 5. 设置默认公网 IP/域名”统一设置一次，已有和新增配置在未指定独立连接地址时都可使用；不修改服务端监听配置，无需重启服务。也可直接运行：

```bash
sudo sing-box server example.com     # 设置默认域名，也可以填写公网 IP
sudo sing-box server                 # 交互设置，回车保留当前值
sudo sing-box server -               # 清除全局默认地址
```

新增配置时默认显示全局地址；未设置全局地址的 Hysteria 2 显示证书域名作为默认值。选项 3 临时输入的地址不会修改已保存的连接信息。

## 修改已有配置

主菜单选择 **11. 修改配置**，或“查看配置 → 6. 修改配置”，也可运行：

```bash
sudo sing-box edit                 # 列出配置并选择名称
sudo sing-box edit my-node         # 直接编辑指定节点
```

支持所有 14 种当前向导模板，按原协议显示相关字段：名称、监听端口、UUID/密码、WebSocket 路径、gRPC 服务名、TLS 域名和证书/私钥路径、REALITY 握手域名、密钥重新生成、short ID，以及客户端连接地址。回车保留原值；UUID、密码和 short ID 输入不回显，输入 `@new` 可重新生成。REALITY 私钥不直接显示，通过选择“重新生成”同步更新密钥对。客户端地址输入 `-` 可清除节点独立地址，恢复使用全局默认地址或 Hy2 的 SNI 回退。

保存前会确认并校验完整配置。名称或端口与其他节点冲突时拒绝保存；沿用自身端口不会被误判为冲突。仅修改客户端地址不会重启服务；服务端配置改变时，运行中的服务会自动重启，原本停止的服务保持停止。配置和连接元数据一起备份，写入或重启失败时尝试恢复，更新成功后配置列表中的分享链接同步变化。

编辑保留其他节点、节点顺序及未编辑字段，不切换协议类型。原名称被路由或其他配置引用时拒绝直接改名；复杂、多用户或超出向导范围的导入配置请使用完整 JSON 导入。取消编辑不写入配置。修改端口、认证或传输参数后，请重新导入分享链接，并按需调整云安全规则。

服务正在运行时，配置保存成功后自动重启；新配置启动失败会尝试恢复旧配置和服务。服务原本停止时，保存配置不会自动启动。

## 删除、备份恢复、批量导出与诊断

v1.4.0 增加主菜单 **12/13**，也可从“查看配置 → 7/8”进入。命令示例：

```bash
sudo sing-box delete my-node                   # 确认后删除单个节点
sudo sing-box backup                           # 输出新建成套备份的绝对路径
sudo sing-box backups                          # 列出成套备份
sudo sing-box restore /root/my-backup.json      # 确认后恢复本面板成套备份
sudo sing-box export links /root/nodes.txt      # 一行一个完整分享链接
sudo sing-box export base64 /root/nodes.b64     # Base64 编码的链接集合
sudo sing-box export json /root/outbounds.json # {"outbounds":[...]}，保留节点名称
sudo sing-box diagnose                         # 只读诊断，发现问题返回非零
```

删除仅移除指定节点，保留其他节点和顺序；若节点名称被路由或其他配置引用则拒绝操作。删除最后一个节点后配置仍可存在，但不再提供代理入站。删除和恢复都要求终端确认，运行中的服务在配置改变时会短暂重启。

**成套备份**为权限 `600` 的 JSON，位于 `/var/lib/sing-box-manager/backups/bundle-*.json`，包含完整服务端配置、全部节点连接元数据和默认连接地址。新增、删除、恢复操作会先生成一致备份；写入、重启或中断失败时恢复原配置与连接信息。备份含认证信息和 REALITY 私钥，不能公开上传。它**不包含外部 TLS 证书/私钥文件、核心二进制、systemd unit、云规则或定时任务**：跨机恢复必须另行安全保留证书文件并放回原路径，先安装兼容核心。旧版自动保存的单个配置 JSON 仍使用 `sing-box import` 导入，不等同于成套恢复；不会自动删除旧备份。

批量导出默认采用各节点独立地址、全局地址以及 Hy2 SNI 的已有优先级，任何节点无法导出时整个操作失败，不生成缺少节点的文件。输出必须为尚不存在的绝对路径，文件权限为 `600`，不会覆盖已有文件。JSON 是 **outbound 集合**，需要合并到客户端配置，不是完整可直接启动的客户端文件；Base64 是静态链接集合，不创建公网订阅服务，客户端是否支持这种文件格式需另行确认。

只读诊断聚合版本、核心配置校验、运行状态、各节点 TCP/UDP 本机监听、TLS 域名/有效期/密钥配对、14 天到期提醒、系统 CA 证书链验证及客户端导出检查。诊断不打印节点密码、UUID、私钥或分享链接，不自动修改防火墙。系统 CA 验证失败可能是缺中间证书，也可能是自签/私有 CA；不会把这种情况直接判为服务不可用。本机监听不能证明云安全规则已放通，也不能代替真实客户端测试。

### 网络配置

- 在云安全组和服务器防火墙放行所选监听端口。VLESS、VMess、AnyTLS 使用 TCP，Hysteria 2 和 TUIC 使用 UDP，Shadowsocks 使用 TCP 和 UDP；脚本不自动修改防火墙。同端口的 TCP/UDP 共用仍按原有保守冲突检查拒绝，请为各节点选择不同端口。
- WS / TCP 的“无 TLS”模板不提供传输层加密，可用于已有安全隧道或由反向代理提供 TLS 的部署；客户端必须与实际部署一致。
- REALITY 握手域名由使用者选择，需从服务器访问并适合 TLS 1.3 握手；配置检查不能代替实际连接验证。
- Shadowsocks 2022 客户端与服务器需保持实际时钟同步，不要求使用相同的显示时区。
- 当前没有 TLS 证书自动申请、续期或反向代理自动配置功能。

## 常用命令

```bash
sudo sing-box                     # 中文面板
sudo sing-box install             # 首次安装官方最新稳定核心
sudo sing-box install 1.14.3       # 首次安装指定正式版本，版本号仅作示例
sudo sing-box update              # 独立更新到官方最新稳定核心
sudo sing-box update-panel        # 更新本项目面板脚本到 GitHub main 最新版本，自动备份
sudo sing-box bbr-status          # 只读查看当前 TCP 算法、默认 qdisc 和已注册算法
sudo sing-box bbr                 # 确认后启用系统 TCP BBR，并写入持久化配置
sudo sing-box update 1.14.3        # 将已安装核心更新为指定正式版本，版本号仅作示例
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

## 开启 TCP BBR

v1.6.0 起选择主菜单 **15. 开启 TCP BBR**，或执行 `sudo sing-box bbr`。先显示内核及当前参数，确认后才加载所需模块、修改系统设置。只读检查使用 `sudo sing-box bbr-status`。部署或更新面板本身不会自动启用 BBR。

设置仅包含 `net.ipv4.tcp_congestion_control=bbr` 和 `net.core.default_qdisc=fq`，写入 `/etc/sysctl.d/99-sing-box-manager-bbr.conf`（权限 `644`）。不会修改其他 sysctl 文件。更新前将旧参数保存至 `/var/lib/sing-box-manager/backups/bbr-*/runtime.json`，已有本项目专属文件另保存为 `sysctl.conf`，备份权限为 `600`；写入、验证或中断失败时恢复原参数及文件。不支持的内核、模块加载受限、同名文件不属于本项目、符号链接或后续持久配置冲突时拒绝设置。已有持久化设置和运行参数都符合目标时不重复写入或备份。

系统会按 `sysctl.d` 同名文件优先级和加载顺序检查覆盖，并另外核对 `/etc/sysctl.conf`，以兼顾 systemd 和 procps；遇到歧义值或冲突会保守拒绝，请先人工核对。确认等待期间如果参数或文件被其他操作改动，也会取消此次设置。脚本不安装或更换内核，不下载第三方加速脚本；需要内核提供 `bbr`，未注册时尝试通过已有 `modprobe` 加载 `tcp_bbr`。失败后可能保留已加载的模块，不会强制卸载可能正在被使用的模块。

这是整台 VPS 的 **TCP 默认设置**，不能替代 Hy2/TUIC 的 UDP/QUIC 内部拥塞控制，也不承诺必然提速。已有连接及监听 socket 可能继续采用旧算法；若需让现有 sing-box 入站监听重新建立，请自行选择菜单 **5. 重新启动**，该操作会短暂中断节点连接。BBR 启用选项不会自动重启服务或重启服务器，不使用 `tc` 重建现有网卡队列；设置默认 qdisc 不等于现有网卡的 qdisc 已被替换。

依据：[Linux TCP 参数](https://www.kernel.org/doc/html/latest/networking/ip-sysctl.html)、[默认 qdisc](https://www.kernel.org/doc/html/latest/admin-guide/sysctl/net.html)、[sysctl.d 优先级](https://www.freedesktop.org/software/systemd/man/latest/sysctl.d.html)。

## 卸载与更新面板

```bash
sudo sing-box uninstall       # 交互确认；卸载核心和服务
sudo sing-box remove-panel    # 核心已卸载后，移除面板命令
```

卸载核心后仍可运行 `sing-box`，通过菜单 1 重新安装。配置、连接信息及备份始终保留；`remove-panel` 也保留这些文件和管理标记，便于恢复。它不是“删除所有数据”的操作。

**更新核心：**选择菜单 **10. 更新 sing-box**，或运行 `sudo sing-box update`。可以附加正式版本号，例如 `sudo sing-box update 1.14.3`；指定较旧版本会切换到该版本。更新会检查现有配置与新核心的兼容性，保留原运行状态及开机自启设置，失败时尝试恢复旧核心和服务文件。尚未安装核心时，更新选项会提示先安装。

**更新面板脚本：**v1.5.0 起选择主菜单 **14. 更新面板脚本** 即可一键更新，成功后自动重新载入新菜单。也可运行 `sudo sing-box update-panel`，命令结束后重新运行 `sudo sing-box` 打开菜单。菜单 **10** 继续用于更新核心。

更新通过固定的本项目 GitHub Contents API 获取 `main` 分支最新文件，校验响应、文件大小、Git Blob 摘要、脚本标识、版本号、Bash 语法和帮助入口。旧面板在替换前保存至 `/var/lib/sing-box-manager/backups/panel-时间-随机值.sh`，权限为 `600`；新入口原子替换为 `755`。如果下载、校验、检查或写入失败则保留旧版，替换期间失败或中断会尝试恢复备份。相同内容会直接提示已是最新版本，不重复备份和写入。Git Blob 摘要用于核对下载内容与 GitHub 响应的一致性，不是独立代码签名。

此操作只更新管理面板入口，保留现有服务端 JSON/JSONC 及注释、连接元数据、默认地址、核心、服务运行状态和开机自启设置；核心未安装时也能更新面板。不修改本地 Git 仓库检出的源码。面板备份可用 `sudo bash /var/lib/sing-box-manager/backups/panel-实际备份文件.sh setup` 恢复，然后重新打开面板。

尚无菜单 14 的旧版 VPS，先执行一次上面的“下载、安装面板并打开菜单”命令；之后即可在菜单中更新。菜单 1 仅用于首次安装，已存在核心时会提示改用更新选项。

## 验证与限制

仓库中的 `tests/test_manager.py` 提供离线回归测试，使用临时目录和模拟的服务命令，不在测试机器上安装服务或修改系统配置：

```bash
bash -n sing-box-manager.sh
python3 -m unittest discover -s tests -v
# 使用已取得的可信官方核心，额外检查生成的配置：
SB_TEST_CORE=/absolute/path/to/sing-box python3 -m unittest discover -s tests -v
# 用真实客户端和服务端核心测试本机回环代理转发：
python3 tests/smoke_core.py --core /absolute/path/to/sing-box
# 可选：需要 root + systemd，创建独立临时服务验证真实管理操作，完成后自动清理：
sudo python3 tests/smoke_management.py --core /absolute/path/to/sing-box
```

测试覆盖安装/更新/卸载和服务命令、14 种协议生成与编辑、客户端分享链接、TLS 检查、默认地址、文件权限、锁竞争、冲突保护和失败恢复。`tests/test_management.py` 补充成套备份、恢复、删除、批量导出、诊断、元数据事务失败与 TERM 中断恢复；离线回归用模拟的 systemctl 验证故障路径。

真实核心回环测试覆盖全部 14 种模板的 HTTP 转发；TCP REALITY Vision 与 gRPC REALITY 使用本地 TLS 1.3 目标验证握手。Hysteria 2、TUIC、Shadowsocks 2022 还通过 SOCKS5 UDP ASSOCIATE 向本机 UDP echo 目标转发并验证精确回包。TLS 客户端显式信任临时测试证书，不使用 `insecure`。

可选的 `smoke_management.py` 创建随机命名的 `sing-box-manager-smoke-*.service`，用真实核心验证新增、修改、删除和恢复后的重启、HTTP 转发，检查三种导出格式的一致性与诊断只读行为；只操作临时配置及临时 unit，不触碰生产服务。测试需要 Python 3、Bash、jq、OpenSSL，以及相应测试所需的 systemd/ss。仓库 GitHub Actions 工作流执行 Bash/ShellCheck、离线回归、真实核心协议回环测试；本轮修复静态检查问题，保留针对 trap 间接调用的局部注释。

离线服务模拟和 `sing-box check` 不能证明服务器网络、目标域名、防火墙及客户端端到端连通性。真机部署后应检查服务日志，并用实际客户端验证连接。支持范围之外的发行版和初始化系统不作兼容承诺。

## 参考与许可证

2026-10-23 对照同类开源项目，优先补齐单机终端管理的日常缺口：

| 对照功能 | 官方项目证据 | 本项目处理 |
|---|---|---|
| 节点增改查删 | [233boy/sing-box](https://github.com/233boy/sing-box/blob/main/README.md) 的 `del` / `ddel` | 增加带引用保护的单节点删除 |
| 可恢复备份 | [3x-ui 备份恢复文档](https://docs.sanaei.dev/docs/operations/backup-restore/) | 增加配置和客户端元数据成套备份、恢复前备份和失败回滚 |
| 批量导出/订阅 | [S-UI](https://github.com/alireza0/s-ui/blob/main/README.md) 的 link/json/clash 订阅 | 增加静态 links/base64/outbound JSON 导出，暂不托管 HTTP 订阅 |
| 调试与状态检查 | [233boy/sing-box](https://github.com/233boy/sing-box/blob/main/README.md) 的 `debug` / `test` | 增加不显示连接凭据的本机诊断 |

暂未纳入：自动 ACME 管理、二维码、多用户流量配额/到期、Web 面板和托管订阅。自动证书管理需要明确现有 acme.sh 的所有权、挑战端口与 DNS 凭据；流量配额需要持久统计和调度，不能仅增加字段。[官方 V2Ray API](https://sing-box.sagernet.org/configuration/experimental/v2ray-api/) 还存在构建能力限制。3x-ui 使用 Xray，仅作管理体验对照，不混用其协议配置。以上功能独立实现，没有复制对照项目代码。

- [sing-box 官方安装与服务管理](https://sing-box.sagernet.org/installation/package-manager/)
- [sing-box 官方配置文档](https://sing-box.sagernet.org/configuration/)
- [SagerNet/sing-box 官方发行版](https://github.com/SagerNet/sing-box/releases)
- [233boy/sing-box](https://github.com/233boy/sing-box)：参考其终端菜单、命令透传和管理思路；本仓库独立实现，没有复制其脚本代码。

本仓库脚本采用 [MIT License](LICENSE)。sing-box 核心独立下载，遵循其自身许可证；本仓库不重新分发核心二进制。
