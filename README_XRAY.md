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

面板 **v1.2.4** 的 **7 查看日志** 可选择最近 100 行或实时跟随；实时查看时按 **Ctrl+C** 停止并返回日志菜单。命令行仍可使用 `sudo xray log` 和 `sudo xray log --follow`。

菜单使用可着色的 `●` 显示运行和自启状态；子菜单取消/失败后保留在相应菜单。入站、用户和出站均可通过序号选择，0 返回；纯数字节点名通过 `name:名称` 明确选择。写入先确认或由已确认向导调用事务，完整原生校验、按需启动/重启并在失败时回滚。新增入站会自动启动停止的 Xray，编辑/删除保留原停止状态；开机自启由独立开关控制。

面板 **v1.1.4** 起，添加/修改的成功状态以保存事务为准。保存完成后，即使客户端信息或分享链接展示失败，也会明确提示“配置已保存，请勿重复添加”，可到 **9 查看配置**重新导出；保存或服务事务失败仍会报告失败并沿用回滚流程。

原生 JSON 中的 TCP 传输别名 `method: "tcp"` / `network: "tcp"` 可正常导出；客户端 JSON 统一使用 `raw`，分享链接使用 `type=tcp`。

## 当前协议模板

- VLESS RAW/TCP，无 TLS、TLS、REALITY + Vision；WebSocket 无 TLS/TLS；gRPC TLS/REALITY（无 Vision）；XHTTP TLS/REALITY。
- VMess RAW/TCP，无 TLS/TLS；WebSocket 无 TLS/TLS；gRPC TLS。
- Trojan + TLS、Shadowsocks 2022 AES-128-GCM、Hysteria 2。

面板 **v1.1.2** 在添加配置子菜单新增 **20. VLESS XHTTP + REALITY + Vision（需 VLESS Encryption）**，原有编号不变。此组合必须开启 VLESS Encryption，向导默认开启并拒绝关闭；ML-DSA-65 仍可独立选择。Xray v26.10.31 通过 Encryption 连接承载 Vision，XHTTP 不具备 RAW 的直接转发优化；客户端必须支持此完整组合。原有第 9 项 XHTTP + REALITY 不带 Vision，行为保持不变。修改新模板时保留 flow 和密钥，关闭加密会在保存前被拒绝。

支持条件见 [官方客户端实现](https://github.com/XTLS/Xray-core/blob/v26.10.31/proxy/vless/outbound/outbound.go#L248) 和 [服务端实现](https://github.com/XTLS/Xray-core/blob/v26.10.31/proxy/vless/inbound/inbound.go#L532)。

这些模板使用固定版 Xray 支持的字段，协议表不会混入未注册的 TUIC、AnyTLS、Snell、ShadowTLS。RAW 是当前 TCP 传输名称；REALITY 客户端用新版 `password` 存放公钥，`Hash32` 不作为公钥。Hy2 的原生协议名是 `hysteria`、协议/传输两处 version=2，认证在客户端 `hysteriaSettings.auth`。协议依据见 [Xray 官方文档](https://xtls.github.io/config/) 和 [v26.10.31 源码](https://github.com/XTLS/Xray-core/tree/v26.10.31)。

新增/修改模板支持认证、端口、对应传输路径/服务名、TLS SNI 与证书、REALITY 密钥和 short ID。TLS 证书支持 `.pem/.cer/.crt`、私钥通常为 `.key`，文件内容均须 PEM；可直接选择 ACME 已申请的有效证书，检查 SAN、时间和密钥配对。运行 ACME 菜单使用外部独立项目，不读取账户/DNS Token 配置，不自动申请或续签证书。多个核心可引用同一已有证书文件，续签及重载仍由原证书维护方式负责。

面板 **v1.1.3** 起，导出的 VLESS、VMess、Trojan TLS 客户端显式使用 `fingerprint: "chrome"`；REALITY 导出继续使用 `chrome`。VLESS/Trojan 链接写入 `fp=chrome`，VMess 分享内容写入 `"fp":"chrome"`。已有节点重新导出、重新导入即可，无需更换密钥或重启服务器。此设置不应用到 Hysteria 2 的 QUIC 握手；客户端导入界面是否保留参数仍需实际核对。

VLESS、VMess、Trojan、Hy2 可按用户添加/删除独立认证并导出；SS2022 当前是单密码模板。用户操作保留其他用户和共享传输，至少保留一个用户。客户端导出保留 TLS 校验、SNI、REALITY、公钥、short ID、flow、WS/gRPC/XHTTP 等必要信息；未知或无法安全表达的导入配置拒绝向导导出，不静默去掉安全设置。

通过菜单 8 的协议模板新建入站时，首个认证用户自动命名为 `user1`，可直接在菜单 18 中管理；后续添加用户时拒绝重名。修改已有节点保留原用户名，JSON 导入保留原用户定义，SS2022 继续使用单密码。

面板 **v1.2.2** 导出 REALITY 节点时，从当前节点的原私钥只读派生客户端公钥，不再依赖旧连接元数据里的缓存公钥。旧元数据缺失或用户列表变化后仍可导出；此过程不生成新私钥、不更改认证或节点配置。XHTTP 上下行分离元数据的过期检查仍保留。

## XHTTP 上下行分离（面板 v1.2.0）

主菜单 **21. XHTTP 上下行分离（客户端）**，或 `sudo xray xhttp-split [入站名称]`，可为已有的 VLESS XHTTP 入站填写独立下载入口。支持 TLS（SNI、指纹、h2/h3/http/1.1）与 REALITY（SNI、指纹、客户端公钥、shortId、可选 ML-DSA-65 校验值），也支持从完整 `downloadSettings` JSON 文件导入；菜单中可关闭此功能。默认关闭，原有节点导出保持原行为。

下载入口必须已经通过 CDN、反向代理或其他入口转发至**同一个 XHTTP 入站、相同 path**。本功能不会创建第二台服务器、配置 CDN 或修改防火墙。设置只保存在该入站的客户端元数据中，不写入服务端 inbound，也不重启核心；备份/恢复、重命名及用户管理沿用现有元数据机制。上传 `auto` 在导出时明确改为 `stream-up`，`packet-up` 保持不变；服务端固定 `stream-one` 时拒绝启用分离。修改入站 path 或外部修改节点后，旧分离配置会被拒绝导出，须重新设置或关闭。

HTTP/1.1 下载入口需使用 `fingerprint: "unsafe"`（原生 Go TLS 握手，证书验证仍开启）；向导选择 HTTP/1.1 后使用此默认值，文件导入时拒绝 HTTP/1.1 搭配浏览器指纹的组合，避免 ALPN 协商到 H2 却发送 H1。默认 h2 继续使用 `chrome`。

保存后通过 **9.查看配置 → 3.客户端 JSON 和链接** 或 `xray client` 重新导出。单用户、多用户、批量 JSON/链接/Base64 导出均携带客户端 `xhttpSettings.extra.downloadSettings`；分享链接通过 URL 编码的 `extra` 字段携带，出站链接/JSON 导入也保留并校验该字段。不会导出下载侧服务端私钥、Seed 或允许不安全 TLS 的设置；暂不接受下载侧嵌套分离、`sockopt` 及本机证书文件配置。

例如一个 TLS 下载入口文件（域名、端口及 path 必须替换成实际入口）：

```json
{
  "address": "down.example.com",
  "port": 443,
  "network": "xhttp",
  "security": "tls",
  "tlsSettings": {
    "serverName": "down.example.com",
    "fingerprint": "chrome",
    "alpn": ["h2"]
  },
  "xhttpSettings": {"path": "/xray", "mode": "auto"}
}
```

```bash
sudo xray xhttp-split my-xhttp /root/download-settings.json
sudo xray client my-xhttp
# 关闭，仅恢复客户端导出为原来的上下行共用入口：
sudo xray xhttp-split my-xhttp -
```

保存时使用已安装核心做客户端配置检查，但这不能验证两个入口是否正确转发，也不能证明小火箭完整支持该配置或实际更抗封锁。导入客户端后仍需检查参数保留情况并做真实上传、下载和断线恢复测试。设计依据：[XHTTP 官方上下行分离说明](https://github.com/XTLS/Xray-core/discussions/4113)。

## 后量子加密与认证（面板 v1.1.0）

菜单 **8 添加入站 / 11 修改配置**：VLESS 模板支持开启、关闭或重新生成 VLESS Encryption 密钥（XHTTP + REALITY + Vision 必须开启）；REALITY 模板另有 ML-DSA-65 开关。除上述新模板外，新增默认关闭以兼容现有客户端，修改默认保留原开关和密钥。开启、关闭或换密钥影响整个入站的全部用户，须重新导入各用户连接信息；不会自动改变现有节点。

- **VLESS Encryption**：官方核心 `mlkem768` 生成密钥，服务端 `decryption=mlkem768x25519plus.native.600s.<Seed>`，客户端从同一 Seed 派生 `encryption=mlkem768x25519plus.native.0rtt.<Client>`；支持 RAW、TLS、REALITY、WS、gRPC、XHTTP 叠加。
- **REALITY ML-DSA-65**：官方核心 `mldsa65` 生成独立密钥，服务端 `mldsa65Seed`、客户端 `mldsa65Verify`；不替代原 REALITY X25519 密钥。首次开启、换密钥或修改握手域名时，自动检测目标 TLS 1.3、X25519MLKEM768 和超过 3500 字节的证书链；未通过不保存。
- 分享链接按官方标准保留完整 `encryption`、`pqv`、`pbk` 等客户端参数；导入分享链接和出站向导均支持对应字段。客户端 JSON / 链接不包含服务端 Seed 或私钥；服务端完整 JSON 和备份仍须保密。

测试基线为 **Xray v26.10.31**。客户端核心及其导入界面必须同时支持对应功能；仅有新版核心不保证 GUI 导入器保留长参数，导入后需核对 `encryption` / `mldsa65Verify`，必要时使用原生 Xray outbound JSON。自动派生仅处理面板生成的单密钥格式，复杂 padding/多密钥导入请用原生 JSON 管理，不会降级成 `none`。0rtt 字段表示尝试票据复用，回环测试不宣称已验证 0-RTT 性能。

参数依据：[官方 VLESS 文档](https://xtls.github.io/config/outbounds/vless.html)、[REALITY 文档](https://xtls.github.io/config/transports/reality.html)、[官方分享链接标准](https://github.com/XTLS/Xray-core/discussions/716)。实际加密转发及错误密钥拒绝验证见 `tests/smoke_xray_pq.py`。

### Clash Verge / Mihomo 的 REALITY 兼容

面板 **v1.1.1** 起，REALITY 分享链接附带 Mihomo 支持的 `support-x25519mlkem768=true` 参数。Xray v26.10.31 使用的 REALITY 服务端要求客户端提供这组混合密钥交换；Mihomo v1.19.33 默认关闭，原链接导入后可能握手失败并显示 Timeout。已有节点无需换密钥或重启，重新导出并导入链接即可。原生 Xray 导入只接受此参数为 `true`，不把它写入 Xray JSON；旧的无此参数链接仍可导入。

Mihomo v1.19.33 支持 VLESS Encryption，但尚未实现 REALITY **ML-DSA-65 验签**，会忽略链接中的 `pqv`。开启混合密钥交换能够连接，**不表示客户端验证了 ML-DSA 签名**；要求两项功能都在客户端实际生效时，使用支持完整 `encryption` 和 `mldsa65Verify` 的 Xray 客户端。面板保留完整 `pqv`，不关闭服务器的任何后量子设置。

实现依据：[Mihomo 默认开关及握手处理](https://github.com/MetaCubeX/mihomo/blob/v1.19.33/component/tls/reality.go)、[Mihomo 链接参数解析](https://github.com/MetaCubeX/mihomo/blob/v1.19.33/common/convert/v.go)、[固定 REALITY 服务端握手要求](https://github.com/XTLS/REALITY/blob/83f621788ca8/tls.go#L215)。

## 出站、IPv6 与 BBR

面板 **v1.2.1** 支持同一入站的单用户出站绑定：进入 **20 出站节点管理 → 9 绑定单个用户至出站**，依次选择节点、用户序号、出站；**10 解除单个用户绑定**只移除所选用户的受管规则。单用户规则优先于整节点规则，其他用户继续按原规则选路；再次设置整节点出站不会覆盖单用户绑定。

支持 VLESS、VMess、Trojan、Hysteria 2 的独立认证用户；当前单密码 Shadowsocks 模板不支持。未命名用户首次绑定时自动补充唯一名称（`email` 字段），UUID/密码不变；解除后保留该名称。已有重名或 `regexp:` 名称会被拒绝，避免误匹配其他用户。绑定用户的凭据或名称需先解除绑定才能更改或删除；被外部修改、缺失或重复的受管规则不会被自动覆盖。操作不产生自动历史备份。

面板 **v1.2.3** 解除单用户绑定时，不再因完整配置导入后端口等连接信息缓存过期而误拒绝；仍核对用户身份、名称和受管规则，只删除所选用户的原样规则，保留节点配置及原连接缓存。

```bash
sudo xray user-outbound-bind my-node 2 exit-a
sudo xray user-outbound-bind my-node 2 -
```

CLI 中节点与出站仍使用名称，用户使用从 1 开始的序号；交互菜单使用列表序号。绑定使用原生 `inboundTag + user → outboundTag` 规则及事务校验/失败回滚，运行中的服务会按需重启；只改变所选用户在服务端的出站选择，不更换认证，也不要求重新导入客户端。

出站管理支持协议向导、粘贴分享链接、原生 Xray outbound JSON/JSONC 与每行一个链接的文本文件。支持单对象、数组、仅包含 outbounds 的对象；不会把 sing-box JSON 当成 Xray 格式。新增只追加，不改变路由；绑定入站时增加优先 `inboundTag → outboundTag` 规则，取消只移除面板登记的原样规则；外部改过/重复的规则拒绝自动处理，路由/代理引用未解除时禁止删除出站。完整 JSON 粘贴不回显，以单独一行 `END` 结束。

IPv6 优先使用 `freedom.streamSettings.sockopt.domainStrategy=UseIPv6v4`，IPv4 优先使用 `UseIPv4v6`，DNS 保持 `UseIP` 查询两类地址。开启 IPv6 优先前强制 IPv6 HTTPS 直连检测公网出口，失败保留原设置。只调整 freedom，已有独立代理与 DNS 服务器保留。这是地址解析优先级，**不保证 IPv6 网络连接失败后重连 IPv4**。全局 DNS `UseIPv6` 是只查 AAAA，不能当作优先策略。

系统 TCP BBR 对整台 VPS 的两个核心共享：本脚本只备份和管理自己的持久化文件；存在其他管理方式的 BBR 持久配置时拒绝冲突恢复。菜单 15 还可为 Hy2 显式设置 `finalmask.quicParams.congestion=bbr`、`bbrProfile=standard`，并在独立元数据记录开启前字段。恢复保留缺失/null 与其他 UDP 混淆参数；未指定时核心仍可能默认使用 BBR，不猜测改成 Reno。Hy2 也支持 Salamander：原生字段为 `streamSettings.finalmask.udp`，分享链接保留混淆密码。

## JSONC、备份与安全边界

面板 **v1.1.5** 起取消操作自动归档：节点、用户、出站、路由、导入、格式化、IP 优先级及核心/面板更新不再累计历史备份。事务仅保留临时回滚文件，成功或恢复成功后清理；恢复失败时保留临时文件并报告位置。手动 `xray backup` 和菜单 13 的备份/恢复保留。TCP BBR 仅保留启用前恢复基线；已有历史备份不会自动删除。

添加模板节点的最终确认显示 `[Y/n]`，直接回车即添加并按需启动/重启；输入 `n` 取消。其他确认操作仍默认否。

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
