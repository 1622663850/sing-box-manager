#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# sing-box-manager: a self-contained Chinese terminal manager.
# The UI command and the upstream core deliberately use different paths.

SB_MANAGER_VERSION="1.0.1"
SB_ROOT_PREFIX="${SB_ROOT_PREFIX:-}" # Source-only test seam; rejected by the CLI.
LIB_DIR="${SB_ROOT_PREFIX}/usr/local/lib/sing-box-manager"
CORE_BIN="$LIB_DIR/sing-box"
WRAPPER="${SB_ROOT_PREFIX}/usr/local/bin/sing-box"
CONFIG_DIR="${SB_ROOT_PREFIX}/etc/sing-box"
CONFIG_FILE="$CONFIG_DIR/config.json"
STATE_DIR="${SB_ROOT_PREFIX}/var/lib/sing-box-manager"
BACKUP_DIR="$STATE_DIR/backups"
CLIENT_DIR="$STATE_DIR/clients"
UNIT_FILE="${SB_ROOT_PREFIX}/etc/systemd/system/sing-box.service"
MANAGED_MARKER="$LIB_DIR/.managed"
SERVICE="sing-box.service"
SCRIPT_FILE="${BASH_SOURCE[0]}"

info() { printf '[信息] %s\n' "$*"; }
warn() { printf '[提示] %s\n' "$*" >&2; }
fail() { printf '[错误] %s\n' "$*" >&2; return 1; }
have() { command -v "$1" >/dev/null 2>&1; }

require_root() {
    [[ $EUID -eq 0 ]] || fail "请使用 sudo bash $SCRIPT_FILE，或 sudo sing-box。"
}

require_systemd() {
    [[ $(uname -s) == Linux ]] || { fail "仅支持 Linux。"; return 1; }
    have systemctl && [[ -d /run/systemd/system ]] || {
        fail "需要正在运行的 systemd；不支持 OpenRC/Alpine、OpenWrt 和未启用 systemd 的容器。"
        return 1
    }
}

is_managed() {
    [[ -f "$MANAGED_MARKER" && ! -L "$MANAGED_MARKER" ]] &&
        [[ $(cat "$MANAGED_MARKER") == sing-box-manager-v1 ]]
}

require_core() {
    [[ -x "$CORE_BIN" ]] || { fail "尚未安装核心，请先选择 1.安装 sing-box。"; return 1; }
    [[ -f "$CONFIG_FILE" ]] || { fail "主配置不存在：$CONFIG_FILE"; return 1; }
}

safe_paths() {
    local path
    for path in "$LIB_DIR" "$STATE_DIR" "$CONFIG_DIR" "$CLIENT_DIR" "$BACKUP_DIR" \
        "$CORE_BIN" "$CONFIG_FILE" "$WRAPPER" "$UNIT_FILE" "$MANAGED_MARKER"; do
        [[ ! -L "$path" ]] || { fail "拒绝操作符号链接：$path"; return 1; }
    done
}

check_conflicts() {
    safe_paths || return
    local path fragment
    if ! is_managed; then
        for path in "$LIB_DIR" "$STATE_DIR" "$CONFIG_DIR" "$WRAPPER" "$UNIT_FILE"; do
            [[ ! -e "$path" ]] || {
                fail "发现现有安装或文件：$path。请先备份并通过原安装方式卸载，再运行本脚本。"
                return 1
            }
        done
    fi
    for path in /usr/bin/sing-box /usr/sbin/sing-box /lib/systemd/system/sing-box.service \
        /usr/lib/systemd/system/sing-box.service /etc/systemd/system/sing-box.service.d; do
        [[ ! -e "$path" ]] || {
            fail "发现其他安装方式或服务覆盖配置：$path；不会自动接管。"
            return 1
        }
    done
    fragment=$(systemctl show "$SERVICE" --property=FragmentPath --value 2>/dev/null) || fragment=""
    [[ -z "$fragment" || "$fragment" == "$UNIT_FILE" ]] || {
        fail "发现外部服务文件：$fragment"; return 1;
    }
}

install_dependencies() {
    local need=0 cmd
    for cmd in curl jq tar gzip sha256sum flock ss openssl; do
        have "$cmd" || need=1
    done
    [[ -r /etc/ssl/certs/ca-certificates.crt || -r /etc/pki/tls/certs/ca-bundle.crt ]] || need=1
    [[ $need -eq 1 ]] || return 0
    info "安装 curl、jq、OpenSSL 等必要依赖。"
    if have apt-get; then
        apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
            ca-certificates curl jq tar gzip coreutils util-linux iproute2 openssl || return
    elif have dnf; then
        dnf install -y ca-certificates curl jq tar gzip coreutils util-linux iproute openssl || return
    elif have yum; then
        yum install -y ca-certificates curl jq tar gzip coreutils util-linux iproute openssl || return
    elif have zypper; then
        zypper --non-interactive install ca-certificates curl jq tar gzip coreutils util-linux iproute2 openssl || return
    elif have pacman; then
        pacman -S --needed --noconfirm ca-certificates curl jq tar gzip coreutils util-linux iproute2 openssl || return
    else
        fail "无法自动安装依赖。需要 Bash 4+、curl、jq、tar、gzip、sha256sum、flock、ss、openssl。"
        return 1
    fi
    for cmd in curl jq tar gzip sha256sum flock ss openssl; do
        have "$cmd" || { fail "依赖安装后仍缺少：$cmd"; return 1; }
    done
}

with_lock() (
    umask 077
    safe_paths || exit 1
    install -d -m 700 "$STATE_DIR" || exit 1
    [[ ! -L "$STATE_DIR/manager.lock" ]] || exit 1
    exec 9>"$STATE_DIR/manager.lock" || exit 1
    flock -n 9 || { fail "另一个管理操作正在执行，请稍后重试。"; exit 1; }
    "$@"
)

base_config() {
    cat <<'JSON'
{
  "log": {"level": "info", "timestamp": true},
  "inbounds": [],
  "outbounds": [{"type": "direct", "tag": "direct"}],
  "route": {"final": "direct"}
}
JSON
}

valid_port() {
    [[ ${1:-} =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 ))
}

valid_tag() { [[ ${1:-} =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]]; }
valid_host() {
    # Hosts only, never host:port or a URL. IPv6 is supplied without brackets.
    [[ -n ${1:-} && ${#1} -le 253 ]] || return 1
    if [[ $1 == *:* ]]; then
        [[ $1 =~ ^[a-fA-F0-9:]+$ && $1 == *:*:* ]]
    else
        [[ $1 =~ ^[a-zA-Z0-9]([a-zA-Z0-9.-]*[a-zA-Z0-9])?$ && $1 != *..* ]]
    fi
}

validate_config() {
    local file=$1
    [[ -f "$file" ]] || { fail "找不到配置文件：$file"; return 1; }
    # Strict JSON keeps jq merging predictable; no comments / JSONC.
    jq -e 'type == "object" and ((.inbounds // []) | type == "array")
        and ((.outbounds // []) | type == "array")' "$file" >/dev/null || {
        fail "配置必须是一个合法的 JSON 对象，inbounds/outbounds 必须为数组。"; return 1;
    }
    [[ -x "$CORE_BIN" ]] || { fail "核心不存在，无法校验配置。"; return 1; }
    "$CORE_BIN" check -D "$STATE_DIR" -c "$file"
}

service_is_active() { systemctl is-active --quiet "$SERVICE"; }

wait_healthy() {
    local attempt
    # Check over several seconds; a successful systemctl invocation alone is insufficient.
    for ((attempt = 0; attempt < 3; attempt++)); do
        sleep 1
        service_is_active || return 1
    done
}

restart_checked() {
    systemctl restart "$SERVICE" && wait_healthy
}

atomic_copy() {
    local source=$1 target=$2 mode=${3:-600} tmp
    tmp=$(mktemp "${target}.tmp.XXXXXX") || return
    if install -m "$mode" "$source" "$tmp" && mv -f -- "$tmp" "$target"; then
        return 0
    fi
    rm -f -- "$tmp"
    return 1
}

commit_config() (
    umask 077
    local candidate=$1 active=0 old_exists=0 snapshot backup
    safe_paths || exit 1
    install -d -m 700 "$CONFIG_DIR" "$STATE_DIR" "$BACKUP_DIR" || exit 1
    snapshot=$(mktemp "$CONFIG_DIR/.candidate.XXXXXX") || exit 1
    trap 'rm -f -- "$snapshot"' EXIT
    cp -- "$candidate" "$snapshot" || exit 1
    validate_config "$snapshot" || { fail "配置检查未通过，现有配置未修改。"; exit 1; }
    service_is_active && active=1
    if [[ -f "$CONFIG_FILE" ]]; then
        old_exists=1
        backup=$(mktemp "$BACKUP_DIR/config-$(date +%Y%m%d-%H%M%S)-XXXXXX.json") || exit 1
        install -m 600 "$CONFIG_FILE" "$backup" || exit 1
    fi
    chmod 600 "$snapshot" && mv -f -- "$snapshot" "$CONFIG_FILE" || exit 1
    if [[ $active -eq 1 ]] && ! restart_checked; then
        warn "新配置启动失败，正在回滚。"
        if [[ $old_exists -eq 1 ]]; then
            atomic_copy "$backup" "$CONFIG_FILE" || { fail "回滚文件失败，备份位于 $backup"; exit 1; }
            if ! restart_checked; then
                fail "已恢复旧配置，但服务未恢复。请执行 sing-box log 并检查。"
                exit 1
            fi
        else
            rm -f -- "$CONFIG_FILE"
            systemctl stop "$SERVICE" || true
        fi
        fail "新配置未生效，已恢复原配置。"
        exit 1
    fi
    [[ $old_exists -eq 0 ]] || info "旧配置已备份：$backup"
    if [[ $active -eq 1 ]]; then
        info "配置已保存，服务已重新启动。"
    else
        info "配置已保存；服务仍处于停止状态，可选择 3.启动。"
    fi
)

add_inbound_file() (
    umask 077
    local file=$1 tag port candidate
    require_core || exit 1
    jq -e 'type == "object" and (.type | type == "string")
        and (.tag | type == "string") and (.listen_port | type == "number")' "$file" >/dev/null || {
        fail "请提供单个 inbound JSON 对象，包含 type、tag、listen_port。"; exit 1;
    }
    tag=$(jq -r '.tag' "$file") && port=$(jq -r '.listen_port' "$file") || exit 1
    valid_tag "$tag" || { fail "tag 只能包含字母、数字、短横线、下划线，长度 1–64。"; exit 1; }
    valid_port "$port" || { fail "端口必须为 1–65535 的整数。"; exit 1; }
    if jq -e --arg tag "$tag" --argjson port "$port" \
        'any(.inbounds[]?; .tag == $tag or .listen_port == $port)' "$CONFIG_FILE" >/dev/null; then
        fail "现有配置已使用该 tag 或端口。"; exit 1
    fi
    if have ss && [[ -n $(ss -H -lntu "sport = :$port" 2>/dev/null) ]]; then
        fail "端口 $port 正被其他服务占用。"; exit 1
    fi
    candidate=$(mktemp "$CONFIG_DIR/.merge.XXXXXX") || exit 1
    trap 'rm -f -- "$candidate"' EXIT
    jq --slurpfile node "$file" '.inbounds = ((.inbounds // []) + [$node[0]])' \
        "$CONFIG_FILE" >"$candidate" || exit 1
    commit_config "$candidate"
)

import_config() {
    require_core || return
    commit_config "$1"
}

generate_inbound() {
    local protocol=$1 tag=$2 port=$3 credential=${4:-} extra=${5:-} private_key=${6:-} short_id=${7:-}
    if ! valid_tag "$tag" || ! valid_port "$port"; then fail "无效的 tag 或端口。"; return 1; fi
    port=$((10#$port))
    case "$protocol" in
        vless-tcp|vless-ws|vless-reality)
            [[ $credential =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] || {
                fail "UUID 格式错误。"; return 1;
            }
            if [[ $protocol == vless-ws && $extra != /* ]]; then
                fail "WebSocket 路径必须以 / 开头。"; return 1
            fi
            if [[ $protocol == vless-reality ]]; then
                valid_host "$extra" && [[ $private_key =~ ^[a-zA-Z0-9_-]{43}$ ]] && \
                    [[ $short_id =~ ^([[:xdigit:]]{2}){1,8}$ ]] || {
                    fail "REALITY 的域名、私钥或 short_id 无效。"; return 1;
                }
            fi
            jq -n --arg protocol "$protocol" --arg tag "$tag" --argjson port "$port" \
                --arg uuid "$credential" --arg extra "$extra" --arg key "$private_key" --arg sid "$short_id" '
                {type:"vless",tag:$tag,listen:"::",listen_port:$port,users:[{uuid:$uuid}]}
                | if $protocol == "vless-ws" then .transport = {type:"ws",path:$extra}
                  elif $protocol == "vless-reality" then
                    .users[0].flow = "xtls-rprx-vision"
                    | .tls = {enabled:true,server_name:$extra,reality:{enabled:true,
                        handshake:{server:$extra,server_port:443},private_key:$key,short_id:[$sid]}}
                  else . end'
            ;;
        shadowsocks)
            [[ -n $credential ]] || { fail "密码不能为空。"; return 1; }
            jq -n --arg tag "$tag" --argjson port "$port" --arg password "$credential" \
                '{type:"shadowsocks",tag:$tag,listen:"::",listen_port:$port,
                  method:"2022-blake3-aes-128-gcm",password:$password}'
            ;;
        *) fail "不支持的生成模板：$protocol"; return 1 ;;
    esac
}

service_action() {
    local action=$1
    case "$action" in
        start|restart)
            require_core || return
            validate_config "$CONFIG_FILE" || return
            if ! systemctl "$action" "$SERVICE" || ! wait_healthy; then
                fail "服务未正常运行，请执行 sing-box log 查看日志。"; return 1;
            fi
            if [[ $action == start ]]; then info "服务已启动。"; else info "服务已重新启动。"; fi
            ;;
        enable)
            require_core || return
            validate_config "$CONFIG_FILE" || return
            systemctl enable "$SERVICE" || return
            info "已设置开机自启；当前运行状态不变。"
            ;;
        disable)
            systemctl disable --now "$SERVICE" || return
            info "已停止服务并关闭开机自启。"
            ;;
        stop)
            systemctl stop "$SERVICE" || return
            info "已停止服务，开机自启设置保持不变。"
            ;;
        *) fail "未知服务操作：$action"; return 1 ;;
    esac
}

download_https() {
    curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 20 --max-time 600 --retry 3 --retry-delay 2 "$1" -o "$2"
}

detect_arch() {
    case "${1:-$(uname -m)}" in
        x86_64|amd64) printf 'amd64\n' ;;
        aarch64|arm64) printf 'arm64\n' ;;
        *) fail "目前支持 x86_64/amd64 和 aarch64/arm64。"; return 1 ;;
    esac
}

download_core() {
    local destination=$1 requested=${2:-latest} arch release tag asset url digest version
    arch=$(detect_arch) || return
    if [[ $requested == latest ]]; then
        release=https://api.github.com/repos/SagerNet/sing-box/releases/latest
    else
        requested=${requested#v}
        [[ $requested =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || {
            fail "仅支持正式版本号，例如 1.14.3。"; return 1;
        }
        release="https://api.github.com/repos/SagerNet/sing-box/releases/tags/v$requested"
    fi
    download_https "$release" "$destination/release.json" || return
    tag=$(jq -er '.tag_name | select(test("^v[0-9]+\\.[0-9]+\\.[0-9]+$"))' "$destination/release.json") || {
        fail "无法读取正式发行版本。"; return 1;
    }
    jq -e '.draft == false and .prerelease == false' "$destination/release.json" >/dev/null || return 1
    version=${tag#v}
    asset="sing-box-$version-linux-$arch.tar.gz"
    url=$(jq -er --arg name "$asset" '.assets[] | select(.name == $name) | .browser_download_url' "$destination/release.json") || return
    digest=$(jq -er --arg name "$asset" '.assets[] | select(.name == $name) | .digest' "$destination/release.json") || return
    [[ $url == "https://github.com/SagerNet/sing-box/releases/download/$tag/$asset" ]] || {
        fail "下载地址不是预期的 SagerNet 官方地址。"; return 1;
    }
    [[ $digest =~ ^sha256:[a-fA-F0-9]{64}$ ]] || {
        fail "此发行文件没有可用的 SHA256 摘要，已拒绝安装。请使用较新的正式版本。"; return 1;
    }
    info "下载官方核心 $tag ($arch)，并校验 SHA256。"
    download_https "$url" "$destination/core.tar.gz" || return
    printf '%s  %s\n' "${digest#sha256:}" "$destination/core.tar.gz" | sha256sum --check --status || {
        fail "SHA256 不匹配，文件不会安装。"; return 1;
    }
    # Extract exactly one file to stdout: archive paths cannot overwrite system files.
    tar -xzOf "$destination/core.tar.gz" "sing-box-$version-linux-$arch/sing-box" >"$destination/sing-box" || return
    chmod 755 "$destination/sing-box" && "$destination/sing-box" version || return
}

write_unit() {
    cat <<EOF
# Managed by sing-box-manager. Do not point ExecStart at the menu wrapper.
[Unit]
Description=sing-box (sing-box-manager)
Documentation=https://sing-box.sagernet.org/
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$STATE_DIR
ExecStartPre=$CORE_BIN check -D $STATE_DIR -c $CONFIG_FILE
ExecStart=$CORE_BIN run -D $STATE_DIR -c $CONFIG_FILE
Restart=on-failure
RestartSec=5s
LimitNOFILE=1048576
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

setup_panel() {
    require_root && require_systemd && check_conflicts && install_dependencies || return
    install -d -m 755 "$LIB_DIR" "$(dirname "$WRAPPER")" || return
    install -d -m 700 "$STATE_DIR" "$CLIENT_DIR" "$BACKUP_DIR" || return
    printf 'sing-box-manager-v1\n' >"$MANAGED_MARKER" || return
    chmod 600 "$MANAGED_MARKER" || return
    atomic_copy "$SCRIPT_FILE" "$WRAPPER" 755 || return
    info "面板已安装。输入 sudo sing-box 即可打开。"
    info "下一步选择 1 安装核心；首次安装后选择 8 添加配置。"
}

install_core_locked() (
    umask 077
    local requested=${1:-latest} operation=${2:-} work backup="" existed=0 unit_existed=0 config_existed=0 active=0
    # Recheck after taking the lock: another operation may have installed/uninstalled the core.
    case "$operation" in
        install)
            [[ ! -e "$CORE_BIN" && ! -L "$CORE_BIN" ]] || {
                fail "核心已安装，请选择 10.更新 sing-box，或执行 sudo sing-box update [版本号]。"
                exit 1
            }
            ;;
        update)
            is_managed || { fail "尚未通过本脚本安装 sing-box，请先执行 sudo sing-box install。"; exit 1; }
            require_core || exit 1
            ;;
    esac
    work=$(mktemp -d "$STATE_DIR/install.XXXXXX") || exit 1
    local transaction=0
    # One cleanup path handles command errors, SIGINT/SIGTERM and failed health checks.
    # shellcheck disable=SC2329 # Invoked by EXIT/INT/TERM traps below.
    rollback_install() {
        local result=$? rollback_ok=1
        trap - EXIT INT TERM
        if [[ $transaction -eq 1 ]]; then
            warn "安装未完成，正在恢复旧核心与服务文件。"
            if [[ $existed -eq 1 ]]; then
                atomic_copy "$backup/core" "$CORE_BIN" 755 || rollback_ok=0
            else
                rm -f -- "$CORE_BIN" || rollback_ok=0
            fi
            if [[ $unit_existed -eq 1 ]]; then
                atomic_copy "$backup/sing-box.service" "$UNIT_FILE" 644 || rollback_ok=0
            else
                rm -f -- "$UNIT_FILE" || rollback_ok=0
            fi
            if [[ $config_existed -eq 0 ]]; then rm -f -- "$CONFIG_FILE" || rollback_ok=0; fi
            systemctl daemon-reload || rollback_ok=0
            if [[ $active -eq 1 ]]; then
                restart_checked || rollback_ok=0
            else
                systemctl stop "$SERVICE" 2>/dev/null || true
            fi
            [[ $rollback_ok -eq 1 ]] || warn "自动恢复未完全成功，请查看日志；备份位于 $backup。"
            result=1
        fi
        rm -rf -- "$work"
        exit "$result"
    }
    trap rollback_install EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    download_core "$work" "$requested" || exit 1
    install -d -m 700 "$CONFIG_DIR" || exit 1
    write_unit >"$work/sing-box.service" || exit 1
    if [[ -f "$CONFIG_FILE" ]]; then
        config_existed=1
        "$work/sing-box" check -D "$STATE_DIR" -c "$CONFIG_FILE" || {
            fail "新版本不能读取当前配置，旧核心保持不变。"; exit 1;
        }
    else
        base_config >"$work/config.json" || exit 1
        "$work/sing-box" check -D "$STATE_DIR" -c "$work/config.json" || exit 1
    fi
    service_is_active && active=1
    backup=$(mktemp -d "$BACKUP_DIR/install-$(date +%Y%m%d-%H%M%S)-XXXXXX") || exit 1
    if [[ -f "$CORE_BIN" ]]; then
        existed=1
        install -m 700 "$CORE_BIN" "$backup/core" || exit 1
    fi
    if [[ -f "$UNIT_FILE" ]]; then
        unit_existed=1
        install -m 600 "$UNIT_FILE" "$backup/sing-box.service" || exit 1
    fi
    transaction=1
    [[ $config_existed -eq 1 ]] || atomic_copy "$work/config.json" "$CONFIG_FILE" || exit 1
    atomic_copy "$work/sing-box" "$CORE_BIN" 755 || exit 1
    atomic_copy "$work/sing-box.service" "$UNIT_FILE" 644 || exit 1
    systemctl daemon-reload || exit 1
    [[ $active -eq 0 ]] || restart_checked || exit 1
    transaction=0
    if [[ $operation == update ]]; then
        info "核心更新完成：$CORE_BIN"
    else
        info "核心安装完成：$CORE_BIN"
    fi
    [[ $existed -eq 0 ]] || info "原核心与服务文件备份：$backup"
    if [[ $active -eq 0 ]]; then
        if [[ $operation == update ]]; then
            info "更新后服务保持停止状态，可按需选择 3 启动。"
        else
            info "当前未启动。请先添加配置，再选择 3 启动、4 设置开机自启。"
        fi
    fi
)

install_core() {
    local version=${1:-latest}
    [[ ! -e "$CORE_BIN" && ! -L "$CORE_BIN" ]] || {
        fail "核心已安装，请选择 10.更新 sing-box，或执行 sudo sing-box update [版本号]。"
        return 1
    }
    if ! is_managed; then
        setup_panel || return
    else
        require_systemd && check_conflicts && install_dependencies || return
    fi
    with_lock install_core_locked "$version" install
}

update_core() {
    local version=${1:-latest}
    is_managed || {
        fail "尚未通过本脚本安装 sing-box，请先执行 sudo bash sing-box-manager.sh setup，再执行 sudo sing-box install。"
        return 1
    }
    # Updating must not bootstrap an installation or replace a missing configuration with defaults.
    require_core || return
    require_systemd && check_conflicts && install_dependencies || return
    with_lock install_core_locked "$version" update
}

confirm() {
    local answer
    [[ -t 0 ]] || { fail "此操作需要交互确认，请在终端运行。"; return 1; }
    read -r -p "$1 [y/N]: " answer || return 1
    [[ $answer == y || $answer == Y ]]
}

uninstall_core_locked() {
    local snapshot
    is_managed || { fail "没有本脚本管理的安装。"; return 1; }
    snapshot=$(mktemp -d "$BACKUP_DIR/uninstall-$(date +%Y%m%d-%H%M%S)-XXXXXX") || return
    [[ ! -f "$CONFIG_FILE" ]] || install -m 600 "$CONFIG_FILE" "$snapshot/config.json" || return
    if [[ -e "$UNIT_FILE" ]] || service_is_active; then
        systemctl disable --now "$SERVICE" || { fail "无法停止/禁用服务，已取消卸载。"; return 1; }
    fi
    rm -f -- "$CORE_BIN" "$UNIT_FILE" || return
    systemctl daemon-reload || return
    systemctl reset-failed "$SERVICE" 2>/dev/null || true
    info "已卸载核心和服务；面板保留，仍可使用 sing-box 重新安装。"
    info "配置及连接信息保留在 $CONFIG_DIR 和 $STATE_DIR；本次备份：$snapshot"
}

uninstall_core() {
    is_managed || { fail "没有本脚本管理的安装。"; return 1; }
    confirm "卸载核心并停止服务？（配置、备份和面板保留）" || return
    with_lock uninstall_core_locked
}

remove_panel() {
    [[ ! -e "$CORE_BIN" ]] || { fail "请先卸载核心，再移除面板。"; return 1; }
    is_managed || { fail "面板不是本脚本管理的。"; return 1; }
    confirm "移除 sing-box 面板命令？（配置、标记和备份保留供以后恢复）" || return
    rm -f -- "$WRAPPER"
}

prompt() {
    local variable=$1 message=$2 default=${3:-} input
    [[ -t 0 ]] || { fail "交互添加需要终端；批量操作可用 add-file/import 命令。"; return 1; }
    if [[ -n $default ]]; then
        read -r -p "$message [$default]: " input || return 1
    else
        read -r -p "$message: " input || return 1
    fi
    printf -v "$variable" '%s' "${input:-$default}"
}

generate_client() {
    local inbound=$1 server=$2 public_key=${3:-}
    valid_host "$server" || { fail "服务器地址格式无效。"; return 1; }
    # Import supports all core schemas, but export must never silently drop security options.
    jq -e '
      if .type == "vless" then
        (.users | length == 1) and
        ((.transport // {}) | keys - ["type","path"] | length == 0) and
        (.transport == null or .transport.type == "ws") and
        (.multiplex == null or .multiplex.enabled == false) and
        (if .tls.enabled == true then
            .tls.reality.enabled == true and .transport == null and
            .users[0].flow == "xtls-rprx-vision" and
            (.tls.reality.short_id | length == 1)
         else (.users[0].flow // "") == "" end)
      elif .type == "shadowsocks" then
        .method == "2022-blake3-aes-128-gcm" and .users == null and .destinations == null and
        (.multiplex == null or .multiplex.enabled == false)
      else false end' "$inbound" >/dev/null || {
        fail "该导入配置超出客户端导出模板范围，请按原始配置设置客户端；不会忽略 TLS 或其他安全字段。"
        return 1
    }
    if [[ $(jq -r '.tls.reality.enabled // false' "$inbound") == true && ! $public_key =~ ^[a-zA-Z0-9_-]{43}$ ]]; then
        fail "缺少有效的 REALITY 公钥。"; return 1
    fi
    jq --arg server "$server" --arg key "$public_key" '
        . as $in | {tag:"proxy",type:.type,server:$server,server_port:.listen_port}
        | if .type == "vless" then
            .uuid = $in.users[0].uuid
            | if $in.transport != null then .transport = $in.transport else . end
            | if $in.tls.reality.enabled == true then
                .flow = $in.users[0].flow
                | .tls = {enabled:true,server_name:$in.tls.server_name,
                    utls:{enabled:true,fingerprint:"chrome"},
                    reality:{enabled:true,public_key:$key,short_id:$in.tls.reality.short_id[0]}}
              else . end
          elif .type == "shadowsocks" then .method=$in.method | .password=$in.password
          else error("no client template for this protocol") end' "$inbound"
}

uri_encode() { jq -rn --arg value "$1" '$value|@uri'; }

client_link() {
    local file=$1 tag=${2:-sing-box} type server port credential transport query sni key sid path method
    type=$(jq -r '.type' "$file") && server=$(jq -r '.server' "$file") &&
        port=$(jq -r '.server_port' "$file") || return
    [[ $server != *:* ]] || server="[$server]"
    case "$type" in
        vless)
            credential=$(jq -r '.uuid' "$file")
            transport=$(jq -r '.transport.type // "tcp"' "$file")
            query="encryption=none&type=$transport"
            if [[ $(jq -r '.tls.reality.enabled // false' "$file") == true ]]; then
                sni=$(jq -r '.tls.server_name' "$file")
                key=$(jq -r '.tls.reality.public_key' "$file")
                sid=$(jq -r '.tls.reality.short_id' "$file")
                query+="&security=reality&flow=xtls-rprx-vision&fp=chrome&sni=$(uri_encode "$sni")&pbk=$(uri_encode "$key")&sid=$sid"
            else
                query+='&security=none'
                if [[ $transport == ws ]]; then
                    path=$(jq -r '.transport.path' "$file")
                    query+="&path=$(uri_encode "$path")"
                fi
            fi
            printf 'vless://%s@%s:%s?%s#%s\n' "$credential" "$server" "$port" "$query" "$(uri_encode "$tag")"
            ;;
        shadowsocks)
            method=$(jq -r '.method' "$file") && credential=$(jq -r '.password' "$file") || return
            # SIP002: 2022 ciphers use URI-encoded userinfo, not legacy Base64(method:password).
            printf 'ss://%s:%s@%s:%s#%s\n' "$(uri_encode "$method")" "$(uri_encode "$credential")" "$server" "$port" "$(uri_encode "$tag")"
            ;;
        *) return 1 ;;
    esac
}

add_wizard_locked() (
    umask 077
    require_core || exit 1
    local choice protocol tag port credential extra="" private_key="" public_key="" short_id="" server="" pair work file
    printf '\n添加配置\n  1. VLESS + REALITY + Vision\n  2. VLESS + WebSocket（无 TLS）\n  3. VLESS TCP（无 TLS）\n  4. Shadowsocks 2022\n  5. 导入单个 inbound JSON 文件\n  6. 导入完整配置 JSON（替换当前配置）\n  0. 返回\n'
    prompt choice "请选择" 1 || exit 1
    case "$choice" in
        0) exit 0 ;;
        1) protocol=vless-reality ;;
        2) protocol=vless-ws ;;
        3) protocol=vless-tcp ;;
        4) protocol=shadowsocks ;;
        5|6)
            prompt file "请输入服务器上的 JSON 文件绝对路径" || exit 1
            if [[ $choice == 5 ]]; then add_inbound_file "$file"; else
                validate_config "$file" || exit 1
                confirm "完整导入会替换全部配置，是否继续？（自动备份旧配置）" || exit 1
                import_config "$file"
            fi
            exit $?
            ;;
        *) fail "无效选项。"; exit 1 ;;
    esac
    prompt tag "配置名称（字母/数字/短横线/下划线）" "$protocol-$(date +%H%M%S)" || exit 1
    prompt port "监听端口" 443 || exit 1
    if ! valid_tag "$tag" || ! valid_port "$port"; then fail "名称或端口无效。"; exit 1; fi
    case "$protocol" in
        vless-*) credential=$("$CORE_BIN" generate uuid) || exit 1 ;;
        shadowsocks) credential=$("$CORE_BIN" generate rand --base64 16) || exit 1 ;;
    esac
    case "$protocol" in
        vless-ws)
            warn "此模板没有 TLS 加密，适用于已有安全隧道或 TLS 反向代理的场景。"
            prompt extra "WebSocket 路径" /vless || exit 1
            ;;
        vless-tcp) warn "此模板没有 TLS 加密；公网直连可选择 REALITY 模板。" ;;
        vless-reality)
            prompt extra "REALITY 握手域名（服务器可访问的 TLS 1.3 站点，不含 https://）" || exit 1
            valid_host "$extra" || { fail "域名格式无效。"; exit 1; }
            pair=$("$CORE_BIN" generate reality-keypair) || exit 1
            private_key=$(printf '%s\n' "$pair" | sed -n 's/^PrivateKey: *//p')
            public_key=$(printf '%s\n' "$pair" | sed -n 's/^PublicKey: *//p')
            [[ $public_key =~ ^[a-zA-Z0-9_-]{43}$ ]] || { fail "无法解析 REALITY 密钥。"; exit 1; }
            short_id=$(openssl rand -hex 8) || exit 1
            ;;
    esac
    prompt server "客户端连接用的服务器公网 IP/域名（留空则只创建服务端配置）" || exit 1
    [[ -z $server ]] || valid_host "$server" || { fail "服务器地址格式无效。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/add.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    generate_inbound "$protocol" "$tag" "$port" "$credential" "$extra" "$private_key" "$short_id" >"$work/inbound.json" || exit 1
    # Client metadata is separate from server configuration and never loaded by the core.
    jq -n --arg key "$public_key" --arg server "$server" --slurpfile inbound "$work/inbound.json" \
        '{server:$server,public_key:$key,inbound:$inbound[0]}' >"$work/metadata.json" || exit 1
    add_inbound_file "$work/inbound.json" || exit 1
    install -d -m 700 "$CLIENT_DIR" || exit 1
    atomic_copy "$work/metadata.json" "$CLIENT_DIR/$tag.json" || {
        warn "服务端配置已成功保存，但连接信息保存失败；请在查看配置中检查。"; exit 1;
    }
    info "已添加 $tag，端口 $((10#$port))。请在服务器防火墙和云安全组放行对应端口。"
    [[ $protocol != shadowsocks ]] || info "Shadowsocks 同时使用 TCP 和 UDP，客户端/服务器需保持时钟同步。"
    [[ $protocol == shadowsocks ]] || info "该模板监听 TCP。"
    [[ -z $server ]] || show_client "$tag" "$server"
)

show_client() (
    umask 077
    local tag=$1 server=${2:-} inbound public_key="" metadata work
    require_core || exit 1
    valid_tag "$tag" || { fail "配置名称无效。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/view.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    jq -e --arg tag "$tag" '.inbounds[]? | select(.tag==$tag)' "$CONFIG_FILE" >"$work/inbound.json" || {
        fail "配置不存在：$tag"; exit 1;
    }
    inbound="$work/inbound.json"
    metadata="$CLIENT_DIR/$tag.json"
    if [[ -f "$metadata" ]]; then
        # Prevent stale exports after manually importing/changing configuration.
        if [[ $(jq -Sc . "$inbound") == "$(jq -Sc .inbound "$metadata")" ]]; then
            [[ -n $server ]] || server=$(jq -r '.server // ""' "$metadata")
            public_key=$(jq -r '.public_key // ""' "$metadata")
        fi
    fi
    [[ -n $server ]] || prompt server "服务器公网 IP 或域名" || exit 1
    if [[ $(jq -r '.tls.reality.enabled // false' "$inbound") == true && -z $public_key ]]; then
        fail "缺少此 REALITY 入站对应的公钥。请使用原始公钥；不会输出不完整的客户端配置。"
        exit 1
    fi
    generate_client "$inbound" "$server" "$public_key" >"$work/outbound.json" || exit 1
    info "客户端 outbound JSON（包含连接凭据）："
    cat "$work/outbound.json"
    info "分享链接："
    client_link "$work/outbound.json" "$tag"
)

show_config() {
    require_core || return
    local choice selected_tag=""
    printf '\n查看配置\n  1. 配置列表\n  2. 完整服务端 JSON（包含密码/私钥）\n  3. 客户端 outbound JSON 和分享链接\n  4. 检查当前配置\n  0. 返回\n'
    prompt choice "请选择" 1 || return
    case "$choice" in
        0) return 0 ;;
        1) jq -r '["名称","协议","监听地址","端口"],
               (.inbounds[]? | [.tag,.type,(.listen // "-"),(.listen_port // "-")]) | @tsv' "$CONFIG_FILE" ;;
        2) jq . "$CONFIG_FILE" ;;
        3) prompt selected_tag "配置名称" && show_client "$selected_tag" ;;
        4) validate_config "$CONFIG_FILE" ;;
        *) fail "无效选项。" ;;
    esac
}

show_status() {
    local active enabled version="未安装"
    [[ ! -x "$CORE_BIN" ]] || version=$("$CORE_BIN" version 2>/dev/null | head -n 1)
    active=$(systemctl is-active "$SERVICE" 2>/dev/null) || active=${active:-inactive}
    enabled=$(systemctl is-enabled "$SERVICE" 2>/dev/null) || enabled=${enabled:-disabled}
    printf '\n核心：%s\n状态：%s    开机自启：%s\n配置：%s\n' "$version" "$active" "$enabled" "$CONFIG_FILE"
}

show_logs() {
    local follow=${1:-}
    if [[ $follow == --follow ]]; then
        journalctl -u "$SERVICE" -n 100 -f --no-pager -o cat
    else
        journalctl -u "$SERVICE" -n 100 --no-pager -o cat
    fi
}

menu() {
    local choice
    [[ -t 0 ]] || { usage; return 0; }
    while true; do
        printf '\n========== sing-box 管理面板 v%s ==========\n' "$SB_MANAGER_VERSION"
        show_status
        printf '\n  1. 安装 sing-box\n  2. 卸载 sing-box\n  3. 启动\n  4. 设置开机自启\n  5. 重新启动\n  6. 禁用（停止 + 关闭开机自启）\n  7. 查看日志\n  8. 添加配置\n  9. 查看配置\n 10. 更新 sing-box\n  0. 退出\n\n'
        read -r -p "请输入选项 [0-10]: " choice || return 0
        case "$choice" in
            1) install_core || true ;;
            2) uninstall_core || true ;;
            3) with_lock service_action start || true ;;
            4) with_lock service_action enable || true ;;
            5) with_lock service_action restart || true ;;
            6) with_lock service_action disable || true ;;
            7) show_logs || true ;;
            8) with_lock add_wizard_locked || true ;;
            9) show_config || true ;;
            10) update_core || true ;;
            0) return 0 ;;
            *) warn "请输入 0–10。" ;;
        esac
        read -r -p "按回车键返回菜单..." choice || return 0
    done
}

usage() {
    cat <<'HELP'
sing-box 中文终端管理面板

首次使用：sudo bash sing-box-manager.sh setup
打开菜单：sudo sing-box

管理命令：
  sing-box install [版本号]       安装核心，默认官方最新稳定版；已安装时提示使用 update
  sing-box update [版本号]        更新已安装核心，保留配置；默认官方最新稳定版
  sing-box uninstall              卸载核心及服务，保留配置、备份和面板
  sing-box start | restart        启动 / 重新启动
  sing-box enable                 设置开机自启（不改变当前运行状态）
  sing-box disable                停止服务并关闭开机自启
  sing-box stop                   仅停止服务
  sing-box status                 查看状态
  sing-box log [--follow]         最近 100 行日志 / 持续查看
  sing-box add                    添加配置向导
  sing-box add-file /path/in.json 添加单个 inbound JSON 对象
  sing-box import /path/full.json 导入完整 JSON 配置（需要确认）
  sing-box info                   查看配置菜单
  sing-box client TAG [SERVER]    输出客户端 outbound JSON 与分享链接
  sing-box validate               检查实际服务配置
  sing-box remove-panel           核心卸载后移除面板命令
  sing-box help                   此帮助

原生命令透传：sing-box version / generate uuid / check -c file.json
显式调用核心：sing-box core <原生参数...>

系统要求：Bash 4+、systemd、x86_64 或 ARM64 Linux。
HELP
}

core_passthrough() {
    [[ -x "$CORE_BIN" ]] || { fail "未安装核心，请先执行 sudo sing-box install。"; return 1; }
    exec "$CORE_BIN" "$@"
}

main() {
    set -o pipefail
    umask 077
    [[ -z $SB_ROOT_PREFIX ]] || { fail "SB_ROOT_PREFIX 仅供 source 后的离线测试使用，CLI 禁止设置。"; return 1; }
    (( BASH_VERSINFO[0] >= 4 )) || { fail "需要 Bash 4 或更新版本。"; return 1; }
    local command=${1:-menu}
    [[ $# -eq 0 ]] || shift
    case "$command" in
        help|-h|--help) usage; return ;;
        core|bin) core_passthrough "$@"; return ;;
        menu|setup|install|update|uninstall|start|restart|enable|disable|stop|status|log|logs|add|add-file|import|info|client|validate|remove-panel) ;;
        *) core_passthrough "$command" "$@"; return ;;
    esac
    require_root && require_systemd && safe_paths || return
    if ! is_managed; then
        case "$command" in
            menu) setup_panel || return ;;
            setup|install|update) ;;
            *) fail "请先执行 sudo bash sing-box-manager.sh setup 安装面板。"; return 1 ;;
        esac
    fi
    case "$command" in
        setup) setup_panel ;;
        install) install_core "${1:-latest}" ;;
        update)
            [[ $# -le 1 ]] || { fail "用法：sing-box update [版本号]"; return 1; }
            update_core "${1:-latest}"
            ;;
        menu) menu ;;
        uninstall) uninstall_core ;;
        remove-panel) remove_panel ;;
        status) show_status ;;
        start|restart|enable|disable|stop) with_lock service_action "$command" ;;
        log|logs) show_logs "${1:-}" ;;
        add) with_lock add_wizard_locked ;;
        add-file)
            [[ $# -eq 1 ]] || { fail "用法：sing-box add-file /path/inbound.json"; return 1; }
            with_lock add_inbound_file "$1"
            ;;
        import)
            [[ $# -eq 1 ]] || { fail "用法：sing-box import /path/full.json"; return 1; }
            validate_config "$1" && confirm "替换当前完整配置？（旧配置会自动备份）" && with_lock import_config "$1"
            ;;
        info) show_config ;;
        client)
            [[ $# -ge 1 && $# -le 2 ]] || { fail "用法：sing-box client TAG [SERVER]"; return 1; }
            show_client "$1" "${2:-}"
            ;;
        validate) require_core && validate_config "$CONFIG_FILE" ;;
    esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
