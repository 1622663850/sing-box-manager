# 独立 Xray 中文管理脚本

`xray-manager.sh` 是可单独下载运行的脚本，入口命令 **`xray`**。它不 source、不执行、不更新 `sing-box-manager.sh`，使用 Xray 原生 JSON、原生核心命令及自己的服务和状态目录。两个脚本都保留在本仓库，分别发布和更新。

## 安装与打开

```bash
curl -fL https://raw.githubusercontent.com/1622663850/sing-box-manager/main/xray-manager.sh -o /root/xray-manager.sh
sudo bash /root/xray-manager.sh setup
sudo xray install
sudo xray
```

`setup` 安装菜单入口与空配置；`install` 安装官方最新稳定核心，校验 Release ZIP 的 SHA256，首次安装后启动空入站服务。空配置没有公网代理监听端口。测试固定 **Xray v26.10.31**，安装其他稳定版本时先用其原生 `run -test -config` 校验当前配置。支持 Linux/systemd、Bash 4+、amd64/arm64；依赖 curl、jq、awk、Python 3、OpenSSL、ss、flock、sysctl 等，缺少时按系统包管理器安装。

| 对象 | 独立位置 |
|---|---|
| 菜单入口 | `/usr/local/bin/xray` |
| 核心和可选 Geo 数据 | `/usr/local/lib/xray-manager/` |
| 服务 | `xray.service` |
| 原生配置 | `/etc/xray/config.json` |
| 连接信息、默认地址、锁、备份 | `/var/lib/xray-manager/` |

发现其他方式安装的 Xray 二进制、配置目录、服务或 drop-in 时拒绝自动接管，不覆盖已有资源。已有 sing-box 节点、服务、配置和自启不会被此脚本安装操作修改。端口属于整台服务器：同协议的同号监听会冲突，TCP 和 UDP 可以共用同号端口；脚本不自动改防火墙或 OCI 安全规则。

## 管理入口

主菜单延续原面板的编号习惯：1 安装、2 卸载、3 启动、4 自启开关、5 重启、6 停止并关自启、7 日志、8 添加入站、9 查看配置、10 更新核心、11 修改、12 删除、13 备份恢复/批量导出/诊断、14 更新独立脚本、15 BBR、16 TCP BBR 恢复、17 ACME、18 多用户、19 出站 IP 优先级、20 出站节点管理。

菜单使用可着色的 `●` 显示运行和自启状态；子菜单取消/失败后保留在相应菜单。入站、用户和出站均可通过序号选择，0 返回；纯数字节点名通过 `name:名称` 明确选择。写入先确认或由已确认向导调用事务，完整原生校验、备份、按需启动/重启并在失败时回滚。新增入站会自动启动停止的 Xray，编辑/删除保留原停止状态；开机自启由独立开关控制。

## 当前协议模板

- VLESS RAW/TCP，无 TLS、TLS、REALITY + Vision；WebSocket 无 TLS/TLS；gRPC TLS/REALITY（无 Vision）；XHTTP TLS/REALITY。
- VMess RAW/TCP，无 TLS/TLS；WebSocket 无 TLS/TLS；gRPC TLS。
- Trojan + TLS、Shadowsocks 2022 AES-128-GCM、Hysteria 2。

这些模板使用固定版 Xray 支持的字段，协议表不会混入未注册的 TUIC、AnyTLS、Snell、ShadowTLS。RAW 是当前 TCP 传输名称；REALITY 客户端用新版 `password` 存放公钥，`Hash32` 不作为公钥。Hy2 的原生协议名是 `hysteria`、协议/传输两处 version=2，认证在客户端 `hysteriaSettings.auth`。协议依据见 [Xray 官方文档](https://xtls.github.io/config/) 和 [v26.10.31 源码](https://github.com/XTLS/Xray-core/tree/v26.10.31)。

新增/修改模板支持认证、端口、对应传输路径/服务名、TLS SNI 与证书、REALITY 密钥和 short ID。TLS 证书支持 `.pem/.cer/.crt`、私钥通常为 `.key`，文件内容均须 PEM；可直接选择 ACME 已申请的有效证书，检查 SAN、时间和密钥配对。运行 ACME 菜单使用外部独立项目，不读取账户/DNS Token 配置，不自动申请或续签证书。多个核心可引用同一已有证书文件，续签及重载仍由原证书维护方式负责。

VLESS、VMess、Trojan、Hy2 可按用户添加/删除独立认证并导出；SS2022 当前是单密码模板。用户操作保留其他用户和共享传输，至少保留一个用户。客户端导出保留 TLS 校验、SNI、REALITY、公钥、short ID、flow、WS/gRPC/XHTTP 等必要信息；未知或无法安全表达的导入配置拒绝向导导出，不静默去掉安全设置。

## 出站、IPv6 与 BBR

出站管理支持协议向导、粘贴分享链接、原生 Xray outbound JSON/JSONC 与每行一个链接的文本文件。支持单对象、数组、仅包含 outbounds 的对象；不会把 sing-box JSON 当成 Xray 格式。新增只追加，不改变路由；绑定入站时增加优先 `inboundTag → outboundTag` 规则，取消只移除面板登记的原样规则；外部改过/重复的规则拒绝自动处理，路由/代理引用未解除时禁止删除出站。完整 JSON 粘贴不回显，以单独一行 `END` 结束。

IPv6 优先使用 `freedom.streamSettings.sockopt.domainStrategy=UseIPv6v4`，IPv4 优先使用 `UseIPv4v6`，DNS 保持 `UseIP` 查询两类地址。开启 IPv6 优先前强制 IPv6 HTTPS 直连检测公网出口，失败保留原设置。只调整 freedom，已有独立代理与 DNS 服务器保留。这是地址解析优先级，**不保证 IPv6 网络连接失败后重连 IPv4**。全局 DNS `UseIPv6` 是只查 AAAA，不能当作优先策略。

系统 TCP BBR 对整台 VPS 的两个核心共享：本脚本只备份和管理自己的持久化文件；存在其他管理方式的 BBR 持久配置时拒绝冲突恢复。菜单 15 还可为 Hy2 显式设置 `finalmask.quicParams.congestion=bbr`、`bbrProfile=standard`，并在独立元数据记录开启前字段。恢复保留缺失/null 与其他 UDP 混淆参数；未指定时核心仍可能默认使用 BBR，不猜测改成 Reno。Hy2 也支持 Salamander：原生字段为 `streamSettings.finalmask.udp`，分享链接保留混淆密码。

## JSONC、备份与安全边界

支持 `//`、`/*...*/`、`#` 注释并在配置变更/格式化时保留原注释和未改字段排版。不是完整 JSON5，不承诺尾逗号。配置、客户端和备份权限为 600，状态目录为 700；列表不打印认证，完整服务端 JSON 和分享链接属于明确的凭据导出操作。备份包含 Xray 配置原文、客户端元数据和默认地址，不包含证书私钥文件、系统服务、核心和全局内核状态；证书需在原路径另行保留。卸载仅停止/卸载本脚本拥有的 Xray 核心和 unit，保留配置、证书、备份与菜单。

新版 Xray Freedom 默认保护私网目的地址。本脚本不会为了方便代理而放开生产私网。需要访问内网时由用户按官方原生配置明确设置最小范围 `finalRules`。自动化转发测试只在隔离临时配置中允许 `127.0.0.1/32` 和实际测试端口，不把这些测试规则部署到生产。

## 常用命令

```bash
sudo xray
sudo xray status
sudo xray add
sudo xray edit
sudo xray client NODE SERVER all
sudo xray server example.com
sudo xray backup
sudo xray export json /root/xray-clients.json
sudo xray outbound-add /root/xray-outbounds.json
sudo xray outbound-bind NODE OUTBOUND
sudo xray outbound-bind NODE -
sudo xray users
sudo xray ip-preference ipv6
sudo xray quic-bbr NODE
sudo xray quic-bbr-disable NODE
sudo xray update
sudo xray update-panel
sudo xray core run -test -config /etc/xray/config.json
```

独立验证见 `.github/workflows/xray-ci.yml`、`tests/test_xray*.py`、`tests/smoke_xray.py` 和 `tests/smoke_xray_management.py`。配置检查、loopback 测试和真实 systemd 测试分别记录，不把本机回环成功当作公网客户端已验收。
