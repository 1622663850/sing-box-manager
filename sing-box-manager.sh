#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# sing-box-manager: a self-contained Chinese terminal manager.
# The UI command and the upstream core deliberately use different paths.

SB_MANAGER_VERSION="1.26.3"
SB_ROOT_PREFIX="${SB_ROOT_PREFIX:-}" # Source-only test seam; rejected by the CLI.
LIB_DIR="${SB_ROOT_PREFIX}/usr/local/lib/sing-box-manager"
CORE_BIN="$LIB_DIR/sing-box"
WRAPPER="${SB_ROOT_PREFIX}/usr/local/bin/sing-box"
CONFIG_DIR="${SB_ROOT_PREFIX}/etc/sing-box"
CONFIG_FILE="$CONFIG_DIR/config.json"
STATE_DIR="${SB_ROOT_PREFIX}/var/lib/sing-box-manager"
BACKUP_DIR="$STATE_DIR/backups"
CLIENT_DIR="$STATE_DIR/clients"
DEFAULT_SERVER_FILE="$STATE_DIR/default-server"
BBR_SYSCTL_DIR="${SB_ROOT_PREFIX}/etc/sysctl.d"
BBR_SYSCTL_FILE="$BBR_SYSCTL_DIR/99-sing-box-manager-bbr.conf"
BBR_RESTORE_FILE="$STATE_DIR/bbr-active"
ACME_MENU_DIR="${SB_ROOT_PREFIX}/root/acme-script"
ACME_MENU_SCRIPT="$ACME_MENU_DIR/acme_cf_token_menu_v1.sh"
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
    if ! have systemctl || [[ ! -d /run/systemd/system ]]; then
        fail "需要正在运行的 systemd；不支持 OpenRC/Alpine、OpenWrt 和未启用 systemd 的容器。"
        return 1
    fi
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
        "$CORE_BIN" "$CONFIG_FILE" "$WRAPPER" "$UNIT_FILE" "$MANAGED_MARKER" "$DEFAULT_SERVER_FILE"; do
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
    for cmd in curl jq awk python3 tar gzip sha256sum flock ss openssl sysctl; do
        have "$cmd" || need=1
    done
    [[ -r /etc/ssl/certs/ca-certificates.crt || -r /etc/pki/tls/certs/ca-bundle.crt ]] || need=1
    [[ $need -eq 1 ]] || return 0
    info "安装 curl、jq、OpenSSL 等必要依赖。"
    if have apt-get; then
        apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y \
            ca-certificates curl jq gawk python3 tar gzip coreutils util-linux iproute2 openssl procps || return
    elif have dnf; then
        dnf install -y ca-certificates curl jq gawk python3 tar gzip coreutils util-linux iproute openssl procps-ng || return
    elif have yum; then
        yum install -y ca-certificates curl jq gawk python3 tar gzip coreutils util-linux iproute openssl procps-ng || return
    elif have zypper; then
        zypper --non-interactive install ca-certificates curl jq gawk python3 tar gzip coreutils util-linux iproute2 openssl procps || return
    elif have pacman; then
        pacman -S --needed --noconfirm ca-certificates curl jq gawk python tar gzip coreutils util-linux iproute2 openssl procps-ng || return
    else
        fail "无法自动安装依赖。需要 Bash 4+、curl、jq、awk、python3、tar、gzip、sha256sum、flock、ss、openssl、sysctl。"
        return 1
    fi
    for cmd in curl jq awk python3 tar gzip sha256sum flock ss openssl sysctl; do
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
  "log": {
    "level": "info",
    "timestamp": true
  },
  "inbounds": [],
  "outbounds": [
    {
      "type": "direct",
      "tag": "direct"
    }
  ],
  "route": {
    "final": "direct"
  }
}
JSON
}

valid_port() {
    [[ ${1:-} =~ ^[0-9]{1,5}$ ]] && (( 10#$1 >= 1 && 10#$1 <= 65535 ))
}

valid_tag() { [[ ${1:-} =~ ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$ ]]; }
valid_host() {
    local host=${1:-} part count=0 compressed=0 tail
    local -a parts
    [[ -n $host && ${#host} -le 253 ]] || return 1
    if [[ $host == *:* ]]; then
        # IPv4-mapped addresses consume two IPv6 groups.
        if [[ $host == *.* ]]; then
            tail=${host##*:}
            [[ $tail =~ ^[0-9.]+$ ]] && valid_host "$tail" || return 1
            host="${host%:*}:0:0"
        fi
        [[ $host =~ ^[a-fA-F0-9:]+$ && $host != *:::* ]] || return 1
        [[ $host != :* || $host == ::* ]] && [[ $host != *: || $host == *:: ]] || return 1
        if [[ $host == *::* ]]; then
            tail=${host#*::}
            [[ $tail != *::* ]] || return 1
            compressed=1
        else
            [[ $host != :* && $host != *: ]] || return 1
        fi
        IFS=: read -r -a parts <<< "$host"
        for part in "${parts[@]}"; do
            [[ -n $part ]] || continue
            [[ ${#part} -le 4 ]] || return 1
            count=$((count + 1))
        done
        if [[ $compressed -eq 1 ]]; then ((count < 8)); else ((count == 8)); fi
    elif [[ $host =~ ^[0-9.]+$ ]]; then
        [[ $host =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
        IFS=. read -r -a parts <<< "$host"
        for part in "${parts[@]}"; do
            [[ ${#part} -le 3 && ( $part == 0 || $part != 0* ) ]] && ((10#$part <= 255)) || return 1
        done
    else
        [[ $host != .* && $host != *. && $host != *..* ]] || return 1
        IFS=. read -r -a parts <<< "$host"
        for part in "${parts[@]}"; do
            [[ ${#part} -le 63 && $part =~ ^[a-zA-Z0-9]([a-zA-Z0-9-]*[a-zA-Z0-9])?$ ]] || return 1
        done
    fi
}

json_query() (
    # Strip comments only outside strings; never treat https:// or escaped quotes as comments.
    # Keep line breaks and separate adjacent tokens so malformed JSON still fails in jq.
    set -o pipefail
    local file=$1
    shift
    awk '
      {
        for (i=1; i<=length($0); i++) {
          c=substr($0,i,1); next_c=substr($0,i+1,1)
          if (block) {
            if (c=="*" && next_c=="/") { block=0; i++; printf " " }
          } else if (quoted) {
            printf "%s", c
            if (escaped) escaped=0
            else if (c=="\\") escaped=1
            else if (c=="\"") quoted=0
          } else if (c=="\"") { quoted=1; printf "%s", c }
          else if (c=="/" && next_c=="/") { printf " "; break }
          else if (c=="/" && next_c=="*") { block=1; i++; printf " " }
          else printf "%s", c
        }
        printf "\n"
      }
      END { if (block) { print "[错误] 配置的 /* 注释未闭合。" > "/dev/stderr"; exit 1 } }
    ' "$file" | jq "$@"
)

config_jq() { json_query "$CONFIG_FILE" "$@"; }

preserve_config_source() {
    have python3 || { fail "保留配置注释需要 Python 3，请重新运行 setup 安装依赖。"; return 1; }
    python3 - "$@" <<'PY_JSONC_EDITOR'
"""Update JSONC values without rewriting untouched source text (stdlib only)."""
import difflib
import json
from pathlib import Path
import re
import sys


TOKEN = re.compile(
    r'\s+|//[^\r\n]*|/\*[\s\S]*?\*/|'
    r'"(?:[^"\\\x00-\x1f]|\\(?:["\\/bfnrt]|u[0-9a-fA-F]{4}))*"|'
    r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?|'
    r'true|false|null|[{}\[\]:,]'
)


def encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"))


def signature(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False,
                      sort_keys=True, separators=(",", ":"))


class Node:
    def __init__(self, start, end, value, items=None):
        self.start, self.end, self.value = start, end, value
        # Each item is (object key or array index, member start, value, comma).
        self.items = items


class Parser:
    def __init__(self, text):
        self.tokens, offset = [], 0
        while offset < len(text):
            match = TOKEN.match(text, offset)
            if match is None:
                raise ValueError("invalid JSONC")
            token = match.group()
            if not (token.isspace() or token.startswith(("//", "/*"))):
                self.tokens.append((token, offset, match.end()))
            offset = match.end()
        self.position = 0

    def peek(self):
        if self.position == len(self.tokens):
            return ""
        return self.tokens[self.position][0]

    def take(self, expected=None):
        if self.position == len(self.tokens):
            raise ValueError("unexpected end")
        token = self.tokens[self.position]
        self.position += 1
        if expected is not None and token[0] != expected:
            raise ValueError("unexpected token")
        return token

    def value(self):
        token, start, end = self.take()
        if token not in ("{", "["):
            value = json.loads(token)
            encode(value)  # Reject overflowing numbers, NaN and Infinity.
            return Node(start, end, value)
        is_object = token == "{"
        close = "}" if is_object else "]"
        result, items = ({} if is_object else []), []
        while self.peek() != close:
            member_start = self.tokens[self.position][1] if self.peek() else end
            if is_object:
                key_token = self.take()[0]
                if not key_token.startswith('"'):
                    raise ValueError("invalid object key")
                key = json.loads(key_token)
                if key in result:
                    raise ValueError("duplicate object key")
                self.take(":")
            else:
                key = len(items)
            child = self.value()
            if is_object:
                result[key] = child.value
            else:
                result.append(child.value)
            comma = self.take(",")[1] if self.peek() == "," else None
            if comma is not None and self.peek() == close:
                raise ValueError("trailing comma")
            items.append((key, member_start, child, comma))
            if comma is None:
                break
        end = self.take(close)[2]
        return Node(start, end, result, items)

    def parse(self):
        root = self.value()
        if self.position != len(self.tokens):
            raise ValueError("multiple documents")
        return root


def identity(value):
    if isinstance(value, dict) and isinstance(value.get("tag"), str):
        return ("tag", value["tag"])
    return ("value", signature(value))


def array_pairs(old, new):
    old_tags = [value.get("tag") for value in old if isinstance(value, dict)]
    new_tags = [value.get("tag") for value in new if isinstance(value, dict)]
    if (len(old_tags) == len(old) and len(new_tags) == len(new)
            and all(isinstance(tag, str) for tag in old_tags + new_tags)):
        common = set(old_tags) & set(new_tags)
        if ([tag for tag in old_tags if tag in common]
                != [tag for tag in new_tags if tag in common]):
            raise ValueError("tag reordering is not supported")
    if len(old) == len(new):
        return list(range(len(old)))
    pairs = [None] * len(new)
    matcher = difflib.SequenceMatcher(None, list(map(identity, old)),
                                     list(map(identity, new)), autojunk=False)
    for operation, a, b, c, d in matcher.get_opcodes():
        if operation == "equal" or (operation == "replace" and b - a == d - c):
            pairs[c:d] = range(a, b)
    return pairs


def format_source(source):
    original = Parser(source).parse().value
    tokens = [(m.group(), m.start(), m.end()) for m in TOKEN.finditer(source) if not m.group().isspace()]
    newline = "\r\n" if "\r\n" in source else "\n"
    lines, line, depth = [], "", 0

    def write(text):
        nonlocal line
        if not line:
            line = "  " * depth
        line += text

    def endline(keep_trailing=False):
        nonlocal line
        if line:
            lines.append(line if keep_trailing else line.rstrip(" \t"))
        line = ""

    def space():
        nonlocal line
        if line and not line.endswith((" ", "\t")):
            line += " "

    index, last_end, after_comma = 0, 0, False
    while index < len(tokens):
        token, start, end = tokens[index]
        comment = token.startswith(("//", "/*"))
        gap = source[last_end:start]
        same_line = "\n" not in gap and "\r" not in gap
        if after_comma and not (comment and same_line):
            endline()
            after_comma = False
        if comment:
            inline = bool(line.strip()) and same_line
            if inline:
                space()
            else:
                endline()
            write(token)
            if token.startswith("//"):
                endline(keep_trailing=True)
                after_comma = False
            elif not inline:
                endline()
            else:
                space()
        elif token in ("{", "["):
            close = "}" if token == "{" else "]"
            if index + 1 < len(tokens) and tokens[index + 1][0] == close:
                write(token + close)
                last_end = tokens[index + 1][2]
                index += 2
                continue
            write(token)
            depth += 1
            endline()
        elif token in ("}", "]"):
            depth -= 1
            endline()
            write(token)
        elif token == ",":
            line = line.rstrip(" \t")
            write(",")
            after_comma = True
        elif token == ":":
            line = line.rstrip(" \t")
            write(": ")
        else:
            write(token)
        last_end = end
        index += 1
    endline()
    result = newline.join(lines) + newline
    result_tokens = [m.group() for m in TOKEN.finditer(result) if not m.group().isspace()]
    if result_tokens != [t[0] for t in tokens] or signature(Parser(result).parse().value) != signature(original):
        raise ValueError("formatting changed tokens or configuration values")
    return result


def update(source, candidate):
    root = Parser(source).parse()
    signature(candidate)
    edits = []
    newline = "\r\n" if "\r\n" in source else "\n"

    def indent(position):
        line = source[source.rfind("\n", 0, position) + 1:position]
        return re.match(r"[ \t]*", line).group()

    def pretty(value, spacing):
        return json.dumps(value, ensure_ascii=False, allow_nan=False, indent=2).replace("\n", newline + spacing)

    def change(node, value):
        if signature(node.value) == signature(value):
            return
        if type(node.value) is not type(value) or node.items is None:
            edits.append((node.start, node.end, pretty(value, indent(node.start))))
            return
        is_object = isinstance(value, dict)
        if is_object:
            old_keys = {item[0]: index for index, item in enumerate(node.items)}
            # Preserve original order for surviving keys; append new keys.
            keys = [item[0] for item in node.items if item[0] in value]
            keys += [key for key in value if key not in old_keys]
            pairs = [old_keys.get(key) for key in keys]
            values = [value[key] for key in keys]
        else:
            keys, values = list(range(len(value))), value
            pairs = array_pairs(node.value, value)
        if pairs == list(range(len(node.items))):
            for position, item in enumerate(node.items):
                change(item[2], values[position])
            return
        retained = set(index for index in pairs if index is not None)
        for index, item in enumerate(node.items):
            _, member_start, child, comma = item
            if index not in retained:
                edits.append((member_start, child.end, ""))
            # Existing commas belong to their preceding surviving item.
            needs_comma = index in retained and pairs.index(index) < len(pairs) - 1
            if comma is not None and not needs_comma:
                edits.append((comma, comma + 1, ""))
            elif comma is None and needs_comma:
                edits.append((child.end, child.end, ","))
        for position, index in enumerate(pairs):
            if index is not None:
                change(node.items[index][2], values[position])
        position = 0
        while position < len(pairs):
            if pairs[position] is not None:
                position += 1
                continue
            first = position
            while position < len(pairs) and pairs[position] is None:
                position += 1
            if position < len(pairs):
                start = node.items[pairs[position]][1]
                spacing = indent(start)
            else:
                start = node.end - 1
                closing_indent = indent(start)
                spacing = indent(node.items[0][1]) if node.items else closing_indent + "  "
                if spacing == closing_indent:
                    spacing += "  "
            fresh = [(encode(keys[i]) + ": " if is_object else "") + pretty(values[i], spacing)
                     for i in range(first, position)]
            if position < len(pairs):
                text = ("," + newline + spacing).join(fresh) + "," + newline + spacing
            else:
                text = newline + spacing + ("," + newline + spacing).join(fresh)
                text += newline + closing_indent
            edits.append((start, start, text))

    change(root, candidate)
    # An appended value and its preceding comma can share one source offset.
    insertions, replacements = {}, []
    for start, end, text in edits:
        if start == end:
            insertions[start] = insertions.get(start, "") + text
        else:
            replacements.append((start, end, text))
    edits = replacements + [(position, position, text) for position, text in insertions.items()]
    boundary = len(source)
    for start, end, text in sorted(edits, reverse=True):
        if not (0 <= start <= end <= boundary):
            raise ValueError("overlapping changes")
        source = source[:start] + text + source[end:]
        boundary = start
    if signature(Parser(source).parse().value) != signature(candidate):
        raise ValueError("result differs from candidate")
    return source


def main():
    try:
        if len(sys.argv) != 3:
            raise ValueError("arguments")
        if sys.argv[1] == "--format":
            result = format_source(Path(sys.argv[2]).read_bytes().decode("utf-8")).encode("utf-8")
        else:
            original = Path(sys.argv[1]).read_bytes().decode("utf-8")
            candidate = json.loads(Path(sys.argv[2]).read_bytes().decode("utf-8"))
            result = update(original, candidate).encode("utf-8")
        sys.stdout.buffer.write(result)
    except Exception:
        print("JSONC 更新失败；原文件未修改。", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
PY_JSONC_EDITOR
}

format_config_source() { preserve_config_source --format "$1"; }

validate_config() {
    local file=$1
    [[ -f "$file" ]] || { fail "找不到配置文件：$file"; return 1; }
    [[ -x "$CORE_BIN" ]] || { fail "核心不存在，无法校验配置。"; return 1; }
    json_query "$file" -e 'type == "object" and ((.inbounds // []) | type == "array")
        and ((.outbounds // []) | type == "array")' >/dev/null || {
        fail "配置 JSON 或注释语法无效，inbounds/outbounds 必须为数组。"; return 1;
    }
    # The core still validates the original bytes, including its own parser constraints.
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
    local candidate=$1 policy=${2:-preserve} activation=${3:-preserve} active=0 old_exists=0 snapshot backup="" transaction=0 start_attempted=0
    case "$policy" in preserve|replace) ;; *) fail "未知的配置保存方式。"; exit 1;; esac
    case "$activation" in preserve|start) ;; *) fail "未知的服务启动策略。"; exit 1;; esac
    safe_paths || exit 1
    install -d -m 700 "$CONFIG_DIR" "$STATE_DIR" "$BACKUP_DIR" || exit 1
    snapshot=$(mktemp "$CONFIG_DIR/.candidate.XXXXXX") || exit 1
    # shellcheck disable=SC2317,SC2329 # Called by the EXIT trap.
    rollback_config() {
        local result=$? restored=1
        trap - EXIT INT TERM
        if [[ $transaction -eq 1 ]]; then
            warn "配置提交未完成，正在回滚。"
            if [[ $old_exists -eq 1 ]]; then
                atomic_copy "$backup" "$CONFIG_FILE" || restored=0
                if [[ $active -eq 1 && $restored -eq 1 ]]; then restart_checked || restored=0; fi
                if [[ $active -eq 0 && $start_attempted -eq 1 ]]; then systemctl stop "$SERVICE" || restored=0; fi
            else
                rm -f -- "$CONFIG_FILE" || restored=0
                systemctl stop "$SERVICE" || restored=0
            fi
            if [[ $restored -eq 1 ]]; then warn "已恢复原配置。"
            else fail "自动恢复失败，请检查备份：$backup"; fi
            result=1
        fi
        rm -f -- "$snapshot"
        exit "$result"
    }
    trap rollback_config EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if [[ $policy == preserve && -f $CONFIG_FILE ]]; then
        preserve_config_source "$CONFIG_FILE" "$candidate" >"$snapshot" || exit 1
        [[ $(json_query "$snapshot" -Sc .) == "$(json_query "$candidate" -Sc .)" ]] || {
            fail "保留注释后的配置与目标不一致，已取消保存。"; exit 1;
        }
    else
        cp -- "$candidate" "$snapshot" || exit 1
    fi
    validate_config "$snapshot" || { fail "配置检查未通过，现有配置未修改。"; exit 1; }
    service_is_active && active=1
    if [[ -f "$CONFIG_FILE" ]]; then
        old_exists=1
        backup=$(mktemp "$BACKUP_DIR/config-$(date +%Y%m%d-%H%M%S)-XXXXXX.json") || exit 1
        install -m 600 "$CONFIG_FILE" "$backup" || exit 1
    fi
    transaction=1
    chmod 600 "$snapshot" && mv -f -- "$snapshot" "$CONFIG_FILE" || exit 1
    if [[ $active -eq 1 ]]; then restart_checked || exit 1
    elif [[ $activation == start ]]; then
        start_attempted=1
        if ! systemctl start "$SERVICE" || ! wait_healthy; then
            fail "新增配置后服务未正常启动，正在恢复原配置和停止状态。"; exit 1
        fi
    fi
    transaction=0
    [[ $old_exists -eq 0 ]] || info "旧配置已备份：$backup"
    if [[ $active -eq 1 ]]; then
        info "配置已保存，服务已重新启动。"
    elif [[ $activation == start ]]; then
        info "配置已保存，原先未运行的服务已自动启动。"
    else
        info "配置已保存；服务仍处于停止状态，可选择 3.启动。"
    fi
)

# Check listening transports, not the TCP/UDP traffic carried inside a proxy.
# Listen addresses remain conservative: overlapping transports share no port.
# shellcheck disable=SC2016 # json_query filters use literal jq variables.
check_inbound_conflicts() {
    local file=$1 excluded=${2:-} config=${3:-$CONFIG_FILE} result port network flag
    result=$(json_query "$config" --slurpfile node "$file" --arg excluded "$excluded" '
      def networks:
        if .type == "hysteria2" or .type == "tuic" or .type == "hysteria" then ["udp"]
        elif .type == "vless" or .type == "vmess" or .type == "trojan" then
          if .transport.type == "quic" then ["udp"] else ["tcp"] end
        elif .type == "snell" or .type == "anytls" or .type == "shadowtls" or .type == "http" or
             .type == "socks" or .type == "mixed" then ["tcp"]
        elif .type == "shadowsocks" or .type == "direct" or .type == "naive" then
          if .network == "tcp" then ["tcp"] elif .network == "udp" then ["udp"]
          else ["tcp","udp"] end
        else ["tcp","udp"] end;
      ($node[0] | networks) as $wanted |
      {port:$node[0].listen_port,
       conflict:any(.inbounds[]?; .tag != $excluded and
         (.tag == $node[0].tag or (.listen_port == $node[0].listen_port and
          ((networks) - $wanted | length) < (networks | length)))),
       networks:$wanted,
       owned:[.inbounds[]? | select(.tag == $excluded and .listen_port == $node[0].listen_port) |
         networks[]]}') || return 1
    if [[ $(jq -r .conflict <<<"$result") == true ]]; then
        fail "现有配置已使用该名称，或相同 TCP/UDP 协议的端口。"; return 1
    fi
    have ss || return 0
    port=$(jq -r .port <<<"$result") || return 1
    for network in $(jq -r '.networks - .owned | .[]' <<<"$result"); do
        case "$network" in tcp) flag=-lnt ;; udp) flag=-lnu ;; esac
        if [[ -n $(ss -H "$flag" "sport = :$port" 2>/dev/null) ]]; then
            fail "端口 $port/$network 正被其他服务占用。"; return 1
        fi
    done
    return 0
}

# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
add_inbound_file() (
    umask 077
    local file=$1 tag port work candidate
    require_core || exit 1
    work=$(mktemp -d "$STATE_DIR/import-inbound.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    json_query "$file" . >"$work/inbound.json" || exit 1
    file="$work/inbound.json"
    jq -e 'type == "object" and (.type | type == "string")
        and (.tag | type == "string") and (.listen_port | type == "number")' "$file" >/dev/null || {
        fail "请提供单个 inbound JSON 对象，包含 type、tag、listen_port。"; exit 1;
    }
    tag=$(jq -r '.tag' "$file") && port=$(jq -r '.listen_port' "$file") || exit 1
    valid_tag "$tag" || { fail "tag 只能包含字母、数字、短横线、下划线，长度 1–64。"; exit 1; }
    valid_port "$port" || { fail "端口必须为 1–65535 的整数。"; exit 1; }
    check_inbound_conflicts "$file" || exit 1
    candidate="$work/candidate.json"
    config_jq --slurpfile node "$file" '.inbounds = ((.inbounds // []) + [$node[0]])' \
        >"$candidate" || exit 1
    commit_config "$candidate" preserve start
)

import_config() {
    require_core || return
    commit_config "$1" replace
}

format_config_locked() (
    umask 077
    local work candidate="" backup="" mode published_inode="" transaction=0
    safe_paths && require_core || exit 1
    work=$(mktemp -d "$STATE_DIR/config-format.XXXXXX") || exit 1
    # shellcheck disable=SC2317,SC2329 # Invoked by EXIT/INT/TERM traps below.
    cleanup_config_format() {
        local result=$? restore_candidate=""
        trap - EXIT INT TERM
        if [[ $transaction -eq 1 ]]; then
            if [[ -f $CONFIG_FILE && ! -L $CONFIG_FILE &&
                  $(stat -c '%d:%i' "$CONFIG_FILE") == "$published_inode" ]] &&
               cmp -s "$work/formatted.json" "$CONFIG_FILE"; then
                restore_candidate=$(mktemp "$CONFIG_DIR/.format-restore.XXXXXX") || true
                if [[ -n $restore_candidate ]]; then
                    if install -m "$mode" "$backup" "$restore_candidate"; then
                        mv -fT -- "$restore_candidate" "$CONFIG_FILE" || true
                    fi
                    rm -f -- "$restore_candidate"
                fi
            fi
            if [[ -f $CONFIG_FILE && ! -L $CONFIG_FILE ]] && cmp -s "$backup" "$CONFIG_FILE" &&
               [[ $(stat -c '%a' "$CONFIG_FILE") == "$mode" ]]; then
                warn "格式化未完成，已恢复原配置；服务未重启。"
            else
                warn "格式化恢复未完全成功，请使用备份核对：$backup"
            fi
            result=1
        fi
        [[ -z $candidate ]] || rm -f -- "$candidate"
        rm -rf -- "$work"
        exit "$result"
    }
    trap cleanup_config_format EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    mode=$(stat -c '%a' "$CONFIG_FILE") || exit 1
    install -m 600 "$CONFIG_FILE" "$work/original.json" || exit 1
    format_config_source "$work/original.json" >"$work/formatted.json" || exit 1
    if ! validate_config "$work/formatted.json" >"$work/check.log" 2>&1; then
        fail "格式化后的配置未通过核心校验，原文件未修改。"; exit 1
    fi
    if cmp -s "$work/original.json" "$work/formatted.json"; then
        info "配置已采用标准缩进，无需格式化。"; exit 0
    fi
    confirm "格式化配置文件？（保留注释和所有配置值，先备份，不重启服务）" || exit 0
    safe_paths || exit 1
    if [[ ! -f $CONFIG_FILE ]] || ! cmp -s "$work/original.json" "$CONFIG_FILE" ||
       [[ $(stat -c '%a' "$CONFIG_FILE") != "$mode" ]]; then
        fail "配置在确认期间被其他操作修改，已取消格式化。"; exit 1
    fi
    install -d -m 700 "$BACKUP_DIR" || exit 1
    backup=$(mktemp "$BACKUP_DIR/format-$(date +%Y%m%d-%H%M%S)-XXXXXX.json") || exit 1
    install -m 600 "$work/original.json" "$backup" || exit 1
    candidate=$(mktemp "$CONFIG_DIR/.format-candidate.XXXXXX") || exit 1
    install -m "$mode" "$work/formatted.json" "$candidate" || exit 1
    published_inode=$(stat -c '%d:%i' "$candidate") || exit 1
    transaction=1
    mv -fT -- "$candidate" "$CONFIG_FILE" || exit 1
    cmp -s "$work/formatted.json" "$CONFIG_FILE" &&
        [[ $(stat -c '%a' "$CONFIG_FILE") == "$mode" ]] || exit 1
    transaction=0
    info "配置文件已按 2 空格缩进换行，注释和配置值均保留；服务未重启。"
    info "原文件备份：$backup"
)

format_config() {
    [[ $# -eq 0 ]] || { fail "用法：sing-box format-config"; return 1; }
    with_lock format_config_locked
}

discover_acme_certificates() {
    python3 - "$SB_ROOT_PREFIX" <<'PY_ACME_CERTIFICATES'
from pathlib import Path
import json
import sys

root = Path(sys.argv[1] + "/root")
home = root / ".acme.sh"
entries, seen = [], set()

def add(label, cert, key):
    pair = (str(cert), str(key))
    if (pair not in seen and cert.is_file() and key.is_file() and
        all(ord(c) >= 32 and ord(c) != 127 for c in "".join(pair))):
        entries.append({"label": label, "certificate": pair[0], "key": pair[1]})
        seen.add(pair)

try:
    add("证书菜单默认部署路径", root / "cert.crt", root / "private.key")
    if home.is_dir():
        for directory in sorted(home.iterdir()):
            if directory.is_dir() and not directory.is_symlink():
                ecc = directory.name.endswith("_ecc")
                domain = directory.name[:-4] if ecc else directory.name
                add("ACME ECC" if ecc else "ACME 标准/RSA", directory / "fullchain.cer", directory / (domain + ".key"))
    print(json.dumps(entries, ensure_ascii=True))
except OSError:
    print("无法读取 ACME 证书目录，请检查目录权限或文件系统。", file=sys.stderr)
    sys.exit(1)
PY_ACME_CERTIFICATES
}

prompt_tls_certificate_files() {
    local name=$1 current_cert=${2:-} current_key=${3:-} entries entry cert_path key_path tls_choice
    local -a matches=()
    valid_host "$name" || { fail "TLS 证书域名/IP 无效。"; return 1; }
    entries=$(discover_acme_certificates) || return 1
    while IFS= read -r entry; do
        cert_path=$(jq -r '.certificate' <<<"$entry") && key_path=$(jq -r '.key' <<<"$entry") || return 1
        if validate_tls_files "$name" "$cert_path" "$key_path" >/dev/null 2>&1; then matches+=("$entry"); fi
    done < <(jq -c '.[]' <<<"$entries")
    if [[ ${#matches[@]} -gt 0 ]]; then
        printf '\n可用 ACME 证书（匹配 SNI：%s，已检查有效期及密钥配对）\n' "$name"
        local i=0
        for entry in "${matches[@]}"; do
            i=$((i + 1))
            printf '  %s. %s\n     证书：%s\n     私钥：%s\n' "$i" \
                "$(jq -r '.label' <<<"$entry")" "$(jq -r '.certificate' <<<"$entry")" "$(jq -r '.key' <<<"$entry")"
        done
        printf '  M. 手动输入证书和私钥路径\n'
        if [[ -n $current_cert && -n $current_key ]]; then
            printf '  0. 保留当前证书和私钥路径\n'
            tls_choice=0
        else
            printf '  0. 取消本次配置\n'
            tls_choice=M
        fi
        local default_choice=$tls_choice
        while true; do
            prompt tls_choice "请选择证书编号（M 手动）" "$default_choice" || return 1
            case "$tls_choice" in
                0)
                    [[ -n $current_cert && -n $current_key ]] || return 1
                    printf -v certificate '%s' "$current_cert"
                    printf -v tls_key '%s' "$current_key"
                    return 0 ;;
                m|M) break ;;
                *)
                    if [[ $tls_choice =~ ^[1-9][0-9]{0,5}$ ]] && (( tls_choice <= ${#matches[@]} )); then
                        entry=${matches[tls_choice - 1]}
                        cert_path=$(jq -r '.certificate' <<<"$entry") && key_path=$(jq -r '.key' <<<"$entry") || return 1
                        validate_tls_files "$name" "$cert_path" "$key_path" || return 1
                        # Follow prompt's dynamic scope: both wizards own these two variables.
                        printf -v certificate '%s' "$cert_path"
                        printf -v tls_key '%s' "$key_path"
                        info "已自动填写证书及私钥路径。"
                        return 0
                    fi
                    warn "请输入有效的证书编号、M 或 0。" ;;
            esac
        done
    else
        info "未发现匹配 SNI 且有效的 ACME 证书，请手动填写路径。"
    fi
    prompt certificate "证书/完整证书链路径（.pem/.cer/.crt，内容须为 PEM；建议 fullchain）" "$current_cert" &&
        prompt tls_key "私钥路径（.key，内容须为 PEM 且无口令）" "$current_key"
}

validate_tls_files() {
    local name=$1 certificate=$2 key=$3 cert_public key_public start identity_match match_option=-checkhost
    valid_host "$name" || { fail "TLS 证书域名/IP 无效。"; return 1; }
    [[ $certificate == /* && -f $certificate && -r $certificate &&
       $key == /* && -f $key && -r $key ]] || {
        fail "请提供可读的证书和私钥文件绝对路径。"; return 1;
    }
    [[ $name != *:* && ! $name =~ ^[0-9.]+$ ]] || match_option=-checkip
    # Older OpenSSL returns zero even for a negative -checkhost/-checkip result.
    if ! openssl x509 -in "$certificate" -noout -checkend 0 >/dev/null 2>&1 ||
       ! identity_match=$(LC_ALL=C openssl x509 -in "$certificate" -noout "$match_option" "$name" 2>/dev/null) ||
       [[ $identity_match != *' does match certificate' ]]; then
        fail "证书无效、已过期或不匹配 TLS 域名/IP。"; return 1
    fi
    start=$(openssl x509 -in "$certificate" -noout -startdate 2>/dev/null) || return 1
    start=$(date -d "${start#notBefore=}" +%s 2>/dev/null) || return 1
    [[ $start -le $(date +%s) ]] || { fail "证书尚未生效，请检查证书起始日期和服务器时间。"; return 1; }
    cert_public=$(openssl x509 -in "$certificate" -pubkey -noout 2>/dev/null) || return 1
    key_public=$(openssl pkey -in "$key" -passin pass: -pubout 2>/dev/null) || {
        fail "私钥无效或需要口令；服务需要可直接读取的私钥。"; return 1;
    }
    [[ -n $cert_public && $cert_public == "$key_public" ]] || {
        fail "证书与私钥不匹配。"; return 1;
    }
}

valid_salamander_password() {
    local LC_ALL=C
    [[ ${#1} -ge 4 ]]
}

validate_obfs_template() {
    jq -e '
      .obfs == null or
      (.type == "hysteria2" and (.obfs | type == "object") and
       (.obfs | keys - ["type","password"] | length == 0) and
       .obfs.type == "salamander" and
       (.obfs.password | type == "string" and utf8bytelength >= 4))' "$1" >/dev/null 2>&1 || {
        fail "仅支持 Hysteria 2 的 Salamander 混淆，密码须至少 4 字节；不支持的混淆字段不会被忽略。"; return 1;
    }
}

generate_snell_shadowtls() {
    local group_tag=$1 port=$2 psk=$3 shadow_password=$4 handshake=$5 handshake_port=${6:-443} sni=${7:-$5}
    if ! valid_tag "$group_tag" || [[ ${#group_tag} -gt 59 ]] || ! valid_port "$port" ||
       ! valid_port "$handshake_port" || ! valid_host "$handshake" || ! valid_host "$sni" || [[ -z $shadow_password ]]; then
        fail "Snell/ShadowTLS 名称、端口、握手地址、SNI 或密码无效。"; return 1
    fi
    jq -n --arg tag "$group_tag" --argjson port "$((10#$port))" --arg psk "$psk" --arg password "$shadow_password" \
      --arg hs "$handshake" --argjson hp "$((10#$handshake_port))" --arg sni "$sni" '
      {format:"sing-box-manager-snell-shadowtls-v1",sni:$sni,
       backend:{type:"snell",tag:$tag,listen:"127.0.0.1",listen_port:0,version:5,psk:$psk,obfs_mode:"none"},
       front:{type:"shadowtls",tag:($tag+"-stls"),listen:"::",listen_port:$port,version:3,
         users:[{name:"default",password:$password}],handshake:{server:$hs,server_port:$hp},strict_mode:true,detour:$tag}}' |
      { local generated_group; generated_group=$(cat) || return 1
        validate_snell_shadowtls_group <(printf '%s' "$generated_group") || return 1
        printf '%s\n' "$generated_group"; }
}

validate_snell_shadowtls_group() {
    local group_json
    group_json=$(cat -- "$1") || return 1
    jq -e '
      .format=="sing-box-manager-snell-shadowtls-v1" and
      (keys==["backend","format","front","sni"]) and
      (.backend|keys==["listen","listen_port","obfs_mode","psk","tag","type","version"]) and
      .backend.type=="snell" and .backend.version==5 and .backend.listen=="127.0.0.1" and
      .backend.listen_port==0 and .backend.obfs_mode=="none" and
      (.backend.tag|type=="string" and test("^[a-zA-Z0-9][a-zA-Z0-9_-]{0,58}$")) and
      (.backend.psk|type=="string" and utf8bytelength>0 and utf8bytelength<=255 and (contains("\u0000")|not)) and
      (.front|keys==["detour","handshake","listen","listen_port","strict_mode","tag","type","users","version"]) and
      .front.type=="shadowtls" and .front.version==3 and .front.strict_mode==true and
      (.front.listen=="::" or .front.listen=="127.0.0.1") and
      (.front.listen_port|type=="number" and .==floor and .>0 and .<=65535) and
      .front.tag==(.backend.tag+"-stls") and .front.detour==.backend.tag and
      (.front.users|type=="array" and length==1) and
      (.front.users[0]|keys==["name","password"] and .name=="default" and
         (.password|type=="string" and utf8bytelength>0 and (contains("\u0000")|not))) and
      (.front.handshake|keys==["server","server_port"] and (.server|type=="string") and
        (.server_port|type=="number" and .==floor and .>0 and .<=65535)) and
      (.sni|type=="string")' <<<"$group_json" >/dev/null 2>&1 || { fail "Snell + ShadowTLS 组合模板无效，未保存。"; return 1; }
    valid_host "$(jq -r .front.handshake.server <<<"$group_json")" && valid_host "$(jq -r .sni <<<"$group_json")"
}

read_snell_shadowtls_group() (
    umask 077
    local group_tag=$1 work front_tag
    valid_tag "$group_tag" || exit 1
    [[ -f $CLIENT_DIR/$group_tag.json && ! -L $CLIENT_DIR/$group_tag.json ]] || { fail "组合节点连接信息缺失。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/shadowtls-read.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    jq -e '.shadowtls_group' "$CLIENT_DIR/$group_tag.json" >"$work/group.json" || exit 1
    validate_snell_shadowtls_group "$work/group.json" || exit 1
    front_tag=$(jq -r .front.tag "$work/group.json") || exit 1
    [[ ! -e $CLIENT_DIR/$front_tag.json && ! -L $CLIENT_DIR/$front_tag.json ]] || { fail "组合组件存在独立连接信息，请先核对。"; exit 1; }
    config_jq . >"$work/config.json" || exit 1
    jq -e --arg t "$group_tag" --slurpfile g "$work/group.json" --slurpfile meta "$CLIENT_DIR/$group_tag.json" '
      $g[0].backend.tag==$t and $meta[0].inbound==$g[0].backend and
      ([.inbounds[]? | select(.tag==$t)]==[$g[0].backend]) and
      ([.inbounds[]? | select(.tag==$g[0].front.tag)]==[$g[0].front])' "$work/config.json" >/dev/null || {
        fail "Snell/ShadowTLS 组件或元数据已被外部修改，已取消操作。"; exit 1;
    }
    cat -- "$work/group.json"
)

is_snell_shadowtls() {
    [[ -f $CLIENT_DIR/$1.json && ! -L $CLIENT_DIR/$1.json ]] && jq -e 'has("shadowtls_group")' "$CLIENT_DIR/$1.json" >/dev/null 2>&1
}

check_shadowtls_component() {
    local component_tag=$1 file
    for file in "$CLIENT_DIR"/*.json; do
        [[ -f $file && ! -L $file ]] || continue
        if jq -e --arg t "$component_tag" '.shadowtls_group.front.tag==$t' "$file" >/dev/null 2>&1; then
            fail "此入站是受管 ShadowTLS 组件，请操作对应的 Snell 逻辑节点。"; return 1
        fi
    done
    return 0
}

logical_inbounds() {
    config_jq . | python3 -c '
import json, pathlib, sys
cfg=json.load(sys.stdin); nodes=cfg.get("inbounds") or []; views={}; hidden=set()
if not isinstance(nodes,list): sys.exit("入站列表格式无效。")
for p in pathlib.Path(sys.argv[1]).glob("*.json"):
    if p.is_symlink(): continue
    try:
        m=json.loads(p.read_text()); g=m.get("shadowtls_group")
        if not isinstance(g,dict) or g.get("format")!="sing-box-manager-snell-shadowtls-v1": continue
        backend=g["backend"]; front=g["front"]
        if m.get("inbound")!=backend or [n for n in nodes if n.get("tag")==backend["tag"]]!=[backend] or [n for n in nodes if n.get("tag")==front["tag"]]!=[front]: continue
        views[backend["tag"]]=dict(backend,listen=front["listen"],listen_port=front["listen_port"],_manager_type="snell+shadowtls-v3")
        hidden.add(front["tag"])
    except (OSError,ValueError,KeyError,TypeError,AttributeError): continue
json.dump([views.get(n.get("tag"),n) for n in nodes if n.get("tag") not in hidden],sys.stdout)
' "$CLIENT_DIR"
}

# shellcheck disable=SC2016 # jq bundle filters use literal variables.
change_snell_shadowtls_locked() (
    umask 077
    local action=$1 group_tag=$2 group_file=${3:-} server=${4:-} work front_tag activation=preserve
    require_core && safe_paths || exit 1
    if ! valid_tag "$group_tag" || { [[ -n $server ]] && ! valid_host "$server"; }; then fail "节点名称或连接地址无效。"; exit 1; fi
    work=$(mktemp -d "$STATE_DIR/shadowtls-save.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before.json" || exit 1
    if [[ $action == add ]]; then
        cp -- "$group_file" "$work/new.json" || exit 1
        validate_snell_shadowtls_group "$work/new.json" || exit 1
        front_tag=$(jq -r .front.tag "$work/new.json")
        [[ $(jq -r .backend.tag "$work/new.json") == "$group_tag" && ! -e $CLIENT_DIR/$group_tag.json && ! -L $CLIENT_DIR/$group_tag.json && ! -e $CLIENT_DIR/$front_tag.json && ! -L $CLIENT_DIR/$front_tag.json ]] || { fail "组合名称或连接信息已存在。"; exit 1; }
        if jq -e --arg t "$group_tag" --arg f "$front_tag" 'any(.config.inbounds[]?; .tag==$t or .tag==$f)' "$work/before.json" >/dev/null; then fail "组件名称已存在。"; exit 1; fi
        jq .front "$work/new.json" >"$work/front.json" || exit 1
        check_inbound_conflicts "$work/front.json" || exit 1
        jq --slurpfile g "$work/new.json" --arg server "$server" --arg t "$group_tag" '
          .config.inbounds+=[$g[0].backend,$g[0].front] |
          .clients[$t]={server:$server,public_key:"",inbound:$g[0].backend,shadowtls_group:$g[0]}' "$work/before.json" >"$work/after.json" || exit 1
        activation=start
    else
        read_snell_shadowtls_group "$group_tag" >"$work/old.json" || exit 1
        front_tag=$(jq -r .front.tag "$work/old.json")
        if [[ $action == delete ]]; then
            jq --arg t "$group_tag" --arg f "$front_tag" '.config.inbounds|=map(select(.tag!=$t and .tag!=$f))' "$work/before.json" >"$work/after.json" || exit 1
            if jq -e --arg t "$group_tag" --arg f "$front_tag" '.config|any(..|strings; .==$t or .==$f)' "$work/after.json" >/dev/null; then
                fail "组合节点被路由或其他配置引用，请先取消绑定/引用。"; exit 1
            fi
            jq --arg t "$group_tag" 'del(.clients[$t])' "$work/after.json" >"$work/next.json" && mv "$work/next.json" "$work/after.json" || exit 1
        elif [[ $action == edit ]]; then
            cp -- "$group_file" "$work/new.json" || exit 1
            validate_snell_shadowtls_group "$work/new.json" || exit 1
            [[ $(jq -r .backend.tag "$work/new.json") == "$group_tag" && $(jq -r .front.tag "$work/new.json") == "$front_tag" ]] || { fail "组合节点请保留名称；如需改名，请新增后迁移。"; exit 1; }
            jq .front "$work/new.json" >"$work/front.json" || exit 1
            check_inbound_conflicts "$work/front.json" "$front_tag" || exit 1
            jq --arg t "$group_tag" --arg f "$front_tag" --arg server "$server" --slurpfile g "$work/new.json" '
              .config.inbounds|=map(if .tag==$t then $g[0].backend elif .tag==$f then $g[0].front else . end) |
              .clients[$t].inbound=$g[0].backend | .clients[$t].shadowtls_group=$g[0] | .clients[$t].server=$server' "$work/before.json" >"$work/after.json" || exit 1
        else exit 1; fi
    fi
    if [[ $(jq -Sc '{config,clients}' "$work/before.json") == "$(jq -Sc '{config,clients}' "$work/after.json")" ]]; then info "组合配置未变化，无需保存或重启。"; exit 0; fi
    jq .config "$work/after.json" >"$work/candidate.json" || exit 1
    validate_config "$work/candidate.json" >"$work/check.log" 2>&1 || { fail "Snell/ShadowTLS 组合未通过核心校验，未保存。"; exit 1; }
    info "将同时${action} Snell 和 ShadowTLS 两层；现有其他节点、自启和默认出站保留。"
    confirm "保存组合节点操作？（成套备份，运行中的服务短暂重启）" || exit 1
    snapshot_bundle >"$work/current.json" || exit 1
    [[ $(jq -Sc . "$work/before.json") == "$(jq -Sc . "$work/current.json")" ]] || { fail "配置或元数据已变化，请重试。"; exit 1; }
    apply_bundle "$work/after.json" preserve "$activation" >"$work/save.log" 2>&1 || { fail "组合保存失败，已执行成套回滚，请核对服务状态和备份。"; exit 1; }
    info "Snell + ShadowTLS v3 组合已保存。"
)

add_snell_shadowtls_locked() {
    local group_file=$1 server=${2:-} group_tag
    group_tag=$(jq -er .backend.tag "$group_file") || return 1
    change_snell_shadowtls_locked add "$group_tag" "$group_file" "$server"
}
save_snell_shadowtls_locked() { change_snell_shadowtls_locked edit "$@"; }
delete_snell_shadowtls_locked() { change_snell_shadowtls_locked delete "$1"; }

generate_snell_shadowtls_clients() {
    local group_file=$1 server=$2 label=$3
    if ! validate_snell_shadowtls_group "$group_file" || ! valid_host "$server" || ! valid_tag "$label"; then return 1; fi
    jq --arg server "$server" --arg label "$label" '
      {outbounds:[{type:"snell",tag:$label,version:4,server:$server,server_port:.front.listen_port,psk:.backend.psk,
        obfs_mode:"none",detour:($label+":shadowtls")},
        {type:"shadowtls",tag:($label+":shadowtls"),server:$server,server_port:.front.listen_port,version:3,
         password:.front.users[0].password,tls:{enabled:true,server_name:.sni,utls:{enabled:true,fingerprint:"chrome"}}}],route:{final:$label}}' "$group_file"
}

snell_shadowtls_surge() {
    local group_file=$1 server=$2 label=$3
    if ! validate_snell_shadowtls_group "$group_file" || ! valid_host "$server" || ! valid_tag "$label"; then return 1; fi
    jq -e '(.backend.psk|test("^[A-Za-z0-9_+/@.=-]+$")) and (.front.users[0].password|test("^[A-Za-z0-9_+/@.=-]+$"))' "$group_file" >/dev/null || {
        fail "密码含无法安全表示为 Surge 行的字符，请导出完整双层 JSON。"; return 1;
    }
    jq -r --arg server "$server" --arg label "$label" '"\($label) = snell, \($server), \(.front.listen_port), psk=\(.backend.psk), version=4, shadow-tls-password=\(.front.users[0].password), shadow-tls-version=3, shadow-tls-sni=\(.sni)"' "$group_file"
}

show_snell_shadowtls_client() (
    umask 077
    local group_tag=$1 server=${2:-} mode=${3:-full} work
    work=$(mktemp -d "$STATE_DIR/shadowtls-client.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    read_snell_shadowtls_group "$group_tag" >"$work/group.json" || exit 1
    [[ -n $server ]] || server=$(jq -r '.server // ""' "$CLIENT_DIR/$group_tag.json")
    [[ -n $server ]] || server=$(read_default_server) || exit 1
    if [[ -z $server ]]; then
        [[ $mode == full ]] || { fail "缺少组合节点公网地址，请设置默认连接地址。"; exit 1; }
        prompt server "服务器公网 IP/域名（不是握手站点）" || exit 1
    fi
    generate_snell_shadowtls_clients "$work/group.json" "$server" "$group_tag" >"$work/client.json" || exit 1
    if [[ $mode == json-only ]]; then jq '.outbounds[]' "$work/client.json"; exit; fi
    if [[ $mode == full ]]; then
        info "客户端配置片段（包含双层凭据；须完整保留两个 outbound 和 detour）："
        cat "$work/client.json"
    fi
    info "Surge [Proxy] 配置行（非通用 URI）："
    snell_shadowtls_surge "$work/group.json" "$server" "$group_tag"
)

snell_shadowtls_wizard_locked() (
    umask 077
    local group_tag=${1:-} work port=443 psk shadow_password hs="" hp=443 sni="" server="" action=add
    require_core && safe_paths || exit 1
    work=$(mktemp -d "$STATE_DIR/shadowtls-wizard.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    if [[ -n $group_tag ]]; then
        action=edit
        read_snell_shadowtls_group "$group_tag" >"$work/original.json" || exit 1
        port=$(jq -r .front.listen_port "$work/original.json")
        psk=$(jq -jr '.backend.psk+"."' "$work/original.json"); psk=${psk%.}
        shadow_password=$(jq -jr '.front.users[0].password+"."' "$work/original.json"); shadow_password=${shadow_password%.}
        hs=$(jq -r .front.handshake.server "$work/original.json"); hp=$(jq -r .front.handshake.server_port "$work/original.json"); sni=$(jq -r .sni "$work/original.json")
        server=$(jq -r '.server // ""' "$CLIENT_DIR/$group_tag.json")
        info "修改组合 $group_tag（保留名称，回车保留两层密码，@new 重新生成）。"
    else
        prompt group_tag "组合节点名称（字母/数字/短横线/下划线，最多 59 位）" "snell-stls-$(date +%H%M%S)" || exit 1
        psk=$("$CORE_BIN" generate rand --base64 16) || exit 1
        shadow_password=$("$CORE_BIN" generate rand --base64 16) || exit 1
    fi
    prompt port "ShadowTLS 公网 TCP 端口" "$port" || exit 1
    prompt_secret psk "Snell PSK（回车保留/自动生成，@new 重新生成）" "$psk" || exit 1
    [[ $psk != @new ]] || psk=$("$CORE_BIN" generate rand --base64 16) || exit 1
    prompt_secret shadow_password "ShadowTLS v3 密码（回车保留/自动生成，@new 重新生成）" "$shadow_password" || exit 1
    [[ $shadow_password != @new ]] || shadow_password=$("$CORE_BIN" generate rand --base64 16) || exit 1
    info "握手站点须可达、兼容 ShadowTLS v3 并支持 TLS 1.3，证书须匹配 SNI；不使用本地证书。"
    prompt hs "TLS 握手站点 IP/域名" "$hs" || exit 1
    prompt hp "TLS 握手站点端口" "$hp" || exit 1
    prompt sni "TLS SNI（必须匹配握手站点证书）" "${sni:-$hs}" || exit 1
    [[ -n $server ]] || server=$(read_default_server) || exit 1
    prompt server "服务器公网 IP/域名（客户端连接地址；- 清除独立地址）" "$server" || exit 1
    [[ $server != - ]] || server=""
    generate_snell_shadowtls "$group_tag" "$port" "$psk" "$shadow_password" "$hs" "$hp" "$sni" >"$work/new.json" || exit 1
    if [[ $action == edit ]]; then
        jq --slurpfile old "$work/original.json" '.front.listen=$old[0].front.listen' "$work/new.json" >"$work/retained.json" &&
            mv "$work/retained.json" "$work/new.json" || exit 1
    fi
    if [[ $action == add ]]; then add_snell_shadowtls_locked "$work/new.json" "$server"
    else save_snell_shadowtls_locked "$group_tag" "$work/new.json" "$server"; fi
)

validate_snell_template() {
    jq -e '
      .type=="snell" and (.version==5 or .version==6) and
      (keys-["type","tag","listen","listen_port","version","psk","users","obfs_mode","mode"]|length==0) and
      (.users==null or .users==[]) and
      (.psk|type=="string" and utf8bytelength>0 and utf8bytelength<=255 and (contains("\u0000")|not)) and
      (if .version==5 then .mode==null and ((.obfs_mode // "none")=="none" or .obfs_mode=="http")
       else (.mode // "default") as $mode | .obfs_mode==null and (.psk|utf8bytelength>=12) and
         (["default","unshaped","unsafe-raw"]|index($mode))!=null end)
    ' "$1" >/dev/null 2>&1 || {
        fail "Snell 模板无效：仅支持 v5/v6 单 PSK，v6 PSK 须为 12–255 字节；多用户或未支持字段请通过完整 JSON 管理。"; return 1;
    }
}

prompt_snell_options() {
    local destination=$1 protocol=$2 current=${3:-} snell_selection snell_default=1
    if [[ $protocol == snell ]]; then
        [[ $current != http ]] || snell_default=2
        prompt snell_selection "Snell v5 混淆：1 不启用，2 HTTP，0 取消" "$snell_default" || return 1
        case "$snell_selection" in 1) printf -v "$destination" '%s' none;; 2) printf -v "$destination" '%s' http;; 0) return 1;; *) fail "无效的 Snell 混淆选项。"; return 1;; esac
    else
        case "$current" in unshaped) snell_default=2;; unsafe-raw) snell_default=3;; esac
        prompt snell_selection "Snell v6 模式：1 default，2 unshaped，3 unsafe-raw（明文，仅调试），0 取消" "$snell_default" || return 1
        case "$snell_selection" in 1) printf -v "$destination" '%s' default;; 2) printf -v "$destination" '%s' unshaped;; 3) printf -v "$destination" '%s' unsafe-raw;; 0) return 1;; *) fail "无效的 Snell 模式。"; return 1;; esac
    fi
}

generate_inbound() {
    local protocol=$1 tag=$2 port=$3 credential=${4:-} extra=${5:-} private_key=${6:-} short_id=${7:-}
    local tls_name=${8:-} certificate=${9:-} tls_key=${10:-} password=${11:-} obfs_password=${12:-} type transport="" security=""
    if ! valid_tag "$tag" || ! valid_port "$port"; then fail "无效的 tag 或端口。"; return 1; fi
    port=$((10#$port))
    case "$protocol" in
        vless-tcp) type=vless ;;
        vless-tcp-tls) type=vless; security=tls ;;
        vless-ws) type=vless; transport=ws ;;
        vless-ws-tls) type=vless; transport=ws; security=tls ;;
        vless-reality) type=vless; security=reality; tls_name=$extra ;;
        vless-grpc-reality) type=vless; transport=grpc; security=reality ;;
        vless-grpc-tls) type=vless; transport=grpc; security=tls ;;
        vmess-tcp) type=vmess ;;
        vmess-tcp-tls) type=vmess; security=tls ;;
        vmess-ws) type=vmess; transport=ws ;;
        vmess-ws-tls) type=vmess; transport=ws; security=tls ;;
        vmess-grpc-tls) type=vmess; transport=grpc; security=tls ;;
        hysteria2|tuic|anytls) type=$protocol; security=tls ;;
        shadowsocks) type=shadowsocks ;;
        snell|snell-v6) type=snell ;;
        vless-grpc-reality-vision)
            fail "Vision 不支持 gRPC 传输，请使用 TCP + REALITY + Vision。"; return 1 ;;
        *) fail "不支持的生成模板：$protocol"; return 1 ;;
    esac
    if [[ $type == snell ]]; then
        local snell_generated
        if [[ $protocol == snell ]]; then
            snell_generated=$(jq -n --arg tag "$tag" --argjson port "$port" --arg psk "$credential" --arg mode "${extra:-none}" \
                '{type:"snell",tag:$tag,listen:"::",listen_port:$port,version:5,psk:$psk,obfs_mode:$mode}') || return 1
        else
            snell_generated=$(jq -n --arg tag "$tag" --argjson port "$port" --arg psk "$credential" --arg mode "${extra:-default}" \
                '{type:"snell",tag:$tag,listen:"::",listen_port:$port,version:6,psk:$psk,mode:$mode}') || return 1
        fi
        validate_snell_template <(printf '%s' "$snell_generated") || return 1
        printf '%s\n' "$snell_generated"
        return 0
    fi
    case "$type" in
        vless|vmess|tuic)
            [[ $credential =~ ^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$ ]] || {
                fail "UUID 格式错误。"; return 1;
            } ;;
        *) [[ -n $credential ]] || { fail "密码不能为空。"; return 1; } ;;
    esac
    [[ $type != tuic || -n $password ]] || { fail "TUIC 密码不能为空。"; return 1; }
    if [[ -n $obfs_password ]] && { [[ $type != hysteria2 ]] || ! valid_salamander_password "$obfs_password"; }; then
        fail "Salamander 仅用于 Hysteria 2，混淆密码须至少 4 字节。"; return 1
    fi
    if [[ $transport == ws && $extra != /* ]]; then
        fail "WebSocket 路径必须以 / 开头。"; return 1
    fi
    if [[ $transport == grpc && ! $extra =~ ^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$ ]]; then
        fail "gRPC 服务名须为 1–128 位字母、数字、点、短横线或下划线，且以字母或数字开头。"; return 1
    fi
    if [[ $security == reality ]]; then
        if ! valid_host "$tls_name" || [[ ! $private_key =~ ^[a-zA-Z0-9_-]{43}$ ||
            ! $short_id =~ ^([[:xdigit:]]{2}){1,8}$ ]]; then
            fail "REALITY 的域名、私钥或 short_id 无效。"; return 1
        fi
    elif [[ $security == tls ]]; then
        validate_tls_files "$tls_name" "$certificate" "$tls_key" || return
    fi
    jq -n --arg type "$type" --arg tag "$tag" --argjson port "$port" --arg credential "$credential" \
        --arg transport "$transport" --arg extra "$extra" --arg security "$security" --arg name "$tls_name" \
        --arg key "$private_key" --arg sid "$short_id" --arg cert "$certificate" --arg tlskey "$tls_key" \
        --arg password "$password" --arg obfs "$obfs_password" '
        {type:$type,tag:$tag,listen:"::",listen_port:$port}
        | if $type == "vless" or $type == "vmess" then .users=[{uuid:$credential}]
          elif $type == "tuic" then .users=[{uuid:$credential,password:$password}] | .congestion_control="cubic"
          elif $type == "shadowsocks" then .method="2022-blake3-aes-128-gcm" | .password=$credential
          else .users=[{password:$credential}] end
        | if $type == "vmess" then .users[0].alterId=0 else . end
        | if $type == "hysteria2" and $obfs != "" then .obfs={type:"salamander",password:$obfs} else . end
        | if $transport == "ws" then .transport={type:"ws",path:$extra}
          elif $transport == "grpc" then .transport={type:"grpc",service_name:$extra} else . end
        | if $security == "reality" then
            (if $transport == "" then .users[0].flow="xtls-rprx-vision" else . end)
            | .tls={enabled:true,server_name:$name,reality:{enabled:true,
                handshake:{server:$name,server_port:443},private_key:$key,short_id:[$sid]}}
          elif $security == "tls" then
            .tls={enabled:true,server_name:$name,certificate_path:$cert,key_path:$tlskey}
          else . end
        | if $transport == "grpc" then .tls.alpn=["h2"]
          elif $type == "hysteria2" or $type == "tuic" then .tls.alpn=["h3"] else . end'
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
        disable-autostart)
            systemctl disable "$SERVICE" || return
            info "已关闭开机自启；当前运行状态不变。"
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

autostart_menu() {
    local choice
    while true; do
        printf '\n开机自启设置\n  1. 开启开机自启（不立即启动）\n  2. 关闭开机自启（不停止服务）\n  0. 返回\n'
        prompt choice "请选择自启操作" 0 || return 0
        case "$choice" in
            1) with_lock service_action enable ;;
            2) with_lock service_action disable-autostart ;;
            0) return 0 ;;
            *) fail "请输入 0–2。" ;;
        esac || true
        read -r -p "按回车键返回开机自启菜单..." choice || return 0
    done
}

download_https() {
    curl --fail --show-error --silent --location --proto '=https' --proto-redir '=https' \
        --connect-timeout 20 --max-time "${3:-600}" --retry 3 --retry-delay 2 "$1" -o "$2"
}

decode_github_file() {
    python3 - "$1" "$2" "$3" <<'PY_GITHUB_FILE'
import base64
import hashlib
import json
from pathlib import Path
import re
import sys

def require(condition):
    if not condition:
        raise ValueError("invalid GitHub response")

try:
    entry = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    require(entry["type"] == "file" and entry["path"] == sys.argv[3])
    require(entry["encoding"] == "base64" and type(entry["size"]) is int)
    require(0 < entry["size"] <= 1048576 and re.fullmatch(r"[a-f0-9]{40}", entry["sha"]))
    content = base64.b64decode("".join(entry["content"].split()), validate=True)
    require(len(content) == entry["size"] and b"\0" not in content)
    digest = hashlib.sha1(b"blob " + str(len(content)).encode() + b"\0" + content).hexdigest()
    require(digest == entry["sha"])
    Path(sys.argv[2]).write_bytes(content)
except Exception:
    print("GitHub 响应、文件大小或摘要检查失败。", file=sys.stderr)
    sys.exit(1)
PY_GITHUB_FILE
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
    arch=$(detect_arch "$(uname -m)") || return
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

update_panel_locked() (
    umask 077
    local work backup="" version transaction=0
    if ! safe_paths || ! is_managed; then
        fail "请先通过 setup 安装本项目面板。"; exit 1
    fi
    [[ -f $WRAPPER && ! -L $WRAPPER ]] || { fail "面板入口不存在或不是普通文件，请重新运行 setup。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/panel-update.XXXXXX") || exit 1
    # shellcheck disable=SC2317,SC2329 # Invoked by EXIT/INT/TERM traps below.
    rollback_panel_update() {
        local result=$?
        trap - EXIT INT TERM
        if [[ $transaction -eq 1 ]]; then
            if ! cmp -s "$backup" "$WRAPPER"; then
                atomic_copy "$backup" "$WRAPPER" 755 || true
            fi
            if cmp -s "$backup" "$WRAPPER"; then
                warn "面板更新未完成，已保留或恢复旧版本。"
            else
                warn "面板自动恢复失败，请使用备份恢复：$backup"
            fi
            result=1
        fi
        rm -rf -- "$work"
        exit "$result"
    }
    trap rollback_panel_update EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    info "正在从 GitHub 获取最新面板脚本。"
    download_https 'https://api.github.com/repos/1622663850/sing-box-manager/contents/sing-box-manager.sh?ref=main' \
        "$work/source.json" 60 || { fail "面板下载失败，旧版本保留。"; exit 1; }
    decode_github_file "$work/source.json" "$work/panel.sh" sing-box-manager.sh || exit 1
    version=$(python3 - "$work/panel.sh" <<'PY_PANEL_DOWNLOAD'
from pathlib import Path
import re
import sys

try:
    content = Path(sys.argv[1]).read_bytes()
    versions = re.findall(rb'^SB_MANAGER_VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$', content, re.M)
    if (not content.startswith(b"#!/usr/bin/env bash\n") or
        b"# sing-box-manager: a self-contained Chinese terminal manager." not in content.splitlines() or
        len(versions) != 1):
        raise ValueError("invalid panel identity")
    print(versions[0].decode("ascii"))
except Exception:
    print("面板脚本标识或版本号检查失败。", file=sys.stderr)
    sys.exit(1)
PY_PANEL_DOWNLOAD
    ) || { fail "下载内容未通过检查，旧版本保留。"; exit 1; }
    bash -n "$work/panel.sh" 9>&- || { fail "新面板语法检查失败，旧版本保留。"; exit 1; }
    if cmp -s "$work/panel.sh" "$WRAPPER"; then
        info "面板已是 GitHub 最新版本 v$version，无需更新。"
        exit 0
    fi
    install -d -m 700 "$BACKUP_DIR" || exit 1
    backup=$(mktemp "$BACKUP_DIR/panel-$(date +%Y%m%d-%H%M%S)-XXXXXX.sh") || exit 1
    install -m 600 "$WRAPPER" "$backup" || exit 1
    if ! SB_ROOT_PREFIX="" timeout --kill-after=2s 15s bash "$work/panel.sh" help 9>&- </dev/null >"$work/help.log" 2>&1 ||
       ! grep -Fq 'sing-box 中文终端管理面板' "$work/help.log"; then
        fail "新面板入口检查失败，旧版本保留。备份：$backup"; exit 1
    fi
    transaction=1
    atomic_copy "$work/panel.sh" "$WRAPPER" 755 || exit 1
    cmp -s "$work/panel.sh" "$WRAPPER" && bash -n "$WRAPPER" 9>&- || exit 1
    transaction=0
    info "面板已更新至 v$version，旧面板备份：$backup"
    info "现有配置、连接信息、核心和服务状态均保留。"
)

update_panel() {
    [[ $# -eq 0 ]] || { fail "用法：sing-box update-panel"; return 1; }
    is_managed || { fail "请先通过 setup 安装本项目面板。"; return 1; }
    require_systemd && check_conflicts && install_dependencies || return
    with_lock update_panel_locked
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
    # shellcheck disable=SC2317,SC2329 # Invoked by EXIT/INT/TERM traps below.
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
    local snapshot="" mode=${1:-retain}
    case "$mode" in retain|purge) ;; *) return 1;; esac
    is_managed || { fail "没有本脚本管理的安装。"; return 1; }
    if [[ $mode == retain ]]; then
        install -d -m 700 "$BACKUP_DIR" || return 1
        snapshot=$(mktemp -d "$BACKUP_DIR/uninstall-$(date +%Y%m%d-%H%M%S)-XXXXXX") || return
        [[ ! -f "$CONFIG_FILE" ]] || install -m 600 "$CONFIG_FILE" "$snapshot/config.json" || return
    fi
    if [[ -e "$UNIT_FILE" ]] || service_is_active || systemctl is-enabled --quiet "$SERVICE"; then
        systemctl disable --now "$SERVICE" || { fail "无法停止/禁用服务，已取消卸载。"; return 1; }
        if service_is_active; then fail "服务仍在运行，已取消卸载。"; return 1; fi
    fi
    rm -f -- "$CORE_BIN" "$UNIT_FILE" || return
    systemctl daemon-reload || return
    systemctl reset-failed "$SERVICE" 2>/dev/null || true
    if [[ $mode == retain ]]; then
        info "已卸载核心和服务；面板保留，仍可使用 sing-box 重新安装。"
        info "配置及连接信息保留在 $CONFIG_DIR 和 $STATE_DIR；本次备份：$snapshot"
    fi
}

uninstall_core() {
    is_managed || { fail "没有本脚本管理的安装。"; return 1; }
    confirm "卸载核心并停止服务？（配置、备份和面板保留）" || return
    with_lock uninstall_core_locked
}

validate_purge_paths() {
    python3 - "$SB_ROOT_PREFIX" "$LIB_DIR" "$CONFIG_DIR" "$STATE_DIR" "$WRAPPER" "$UNIT_FILE" \
        "$MANAGED_MARKER" "$CORE_BIN" "$BACKUP_DIR" "$CLIENT_DIR" "$DEFAULT_SERVER_FILE" \
        "$BBR_SYSCTL_DIR" "$BBR_SYSCTL_FILE" "$BBR_RESTORE_FILE" "$SERVICE" "${1:-}" "$CONFIG_FILE" <<'PY_PURGE_PATHS'
from pathlib import Path
import os, re, stat, sys

try:
    prefix = sys.argv[1]
    if prefix and (not prefix.startswith('/') or prefix.startswith('//') or prefix == '/' or os.path.normpath(prefix) != prefix):
        raise ValueError()
    expected = ['/usr/local/lib/sing-box-manager', '/etc/sing-box', '/var/lib/sing-box-manager',
                '/usr/local/bin/sing-box', '/etc/systemd/system/sing-box.service',
                '/usr/local/lib/sing-box-manager/.managed', '/usr/local/lib/sing-box-manager/sing-box',
                '/var/lib/sing-box-manager/backups', '/var/lib/sing-box-manager/clients',
                '/var/lib/sing-box-manager/default-server', '/etc/sysctl.d',
                '/etc/sysctl.d/99-sing-box-manager-bbr.conf', '/var/lib/sing-box-manager/bbr-active']
    if (sys.argv[2:15] != [prefix + p for p in expected] or sys.argv[15] != 'sing-box.service' or
        sys.argv[17] != prefix + '/etc/sing-box/config.json'):
        raise ValueError()
    paths = [Path(p) for p in sys.argv[2:15]] + [Path(sys.argv[17])]
    source = Path(sys.argv[16]) if sys.argv[16] else None
    if source:
        if (not source.is_absolute() or str(source).startswith('//') or os.path.normpath(str(source)) != str(source) or
            any(ord(c) < 32 or ord(c) == 127 for c in str(source))): raise ValueError()
        protected = ['/usr/local/lib/xray-manager', '/usr/local/bin/xray', '/etc/xray',
                     '/var/lib/xray-manager', '/etc/systemd/system/xray.service',
                     '/root/xray-manager', '/root/.acme.sh', '/root/acme-script']
        if any(source == Path(prefix + p) or Path(prefix + p) in source.parents for p in protected): raise ValueError()
        paths.append(source)
    for p in paths:
        if any(parent.is_symlink() for parent in [p, *p.parents]): raise ValueError()
        if p.exists() and os.path.ismount(p): raise ValueError()
    for p in paths[:3] + paths[7:9] + [paths[10]]:
        if p.exists() and not p.is_dir(): raise ValueError()
    # A bind mount can share st_dev with its parent; --one-file-system alone cannot protect it.
    for line in Path('/proc/self/mountinfo').read_text().splitlines():
        mount = Path(re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), line.split()[4]))
        if any(mount == p or p in mount.parents for p in paths[:3]): raise ValueError()
    for p in [paths[i] for i in [3, 4, 5, 6, 9, 11, 12, 13]]:
        if p.exists() and not stat.S_ISREG(p.stat().st_mode): raise ValueError()
    if paths[5].stat().st_size > 256 or paths[5].read_text().strip() != 'sing-box-manager-v1': raise ValueError()
    def owned_script(p):
        if not p.is_file() or p.stat().st_size > 1048576: return False
        data = p.read_bytes()
        return (data.startswith(b'#!/usr/bin/env bash\n') and
                b'# sing-box-manager: a self-contained Chinese terminal manager.' in data.splitlines() and
                re.search(rb'^SB_MANAGER_VERSION="[0-9]+\.[0-9]+\.[0-9]+"$', data, re.M) is not None)
    if paths[3].exists() and not owned_script(paths[3]): raise ValueError()
    if source and not owned_script(source): raise ValueError()
    if paths[4].exists():
        if paths[4].stat().st_size > 1048576: raise ValueError()
        unit = paths[4].read_text()
        if (not unit.startswith('# Managed by sing-box-manager.') or
            ('ExecStart=' + str(paths[6]) + ' run -D ' + str(paths[2]) + ' -c ' + str(paths[1] / 'config.json')) not in unit.splitlines()):
            raise ValueError()
except (ValueError, OSError, UnicodeError):
    print('[错误] 彻底卸载路径或文件归属检查失败；拒绝删除符号链接、挂载点或非本面板文件。', file=sys.stderr)
    raise SystemExit(1)
PY_PURGE_PATHS
}

archive_bbr_recovery() (
    umask 077
    local bbr_backup=$1 bbr_archive=""
    # Copy only validated recovery material, never node credentials or unrelated backups.
    bbr_archive=$(mktemp -d "${STATE_DIR%/*}/sing-box-manager-bbr-recovery-$(date +%Y%m%d-%H%M%S)-XXXXXX") || return 1
    install -d -m 700 "$bbr_archive/backups" "$bbr_archive/backups/${bbr_backup##*/}" &&
        install -m 600 "$BBR_RESTORE_FILE" "$bbr_archive/bbr-active" &&
        install -m 600 /dev/null "$bbr_archive/backups/${bbr_backup##*/}/runtime.json" &&
        jq '{tcp_congestion_control,default_qdisc,config_existed,config_mode}' "$bbr_backup/runtime.json" \
            >"$bbr_archive/backups/${bbr_backup##*/}/runtime.json" || return 1
    if [[ $(jq -r .config_existed "$bbr_archive/backups/${bbr_backup##*/}/runtime.json") == true ]]; then
        [[ ! -L $bbr_backup/sysctl.conf ]] &&
            install -m 600 "$bbr_backup/sysctl.conf" "$bbr_archive/backups/${bbr_backup##*/}/sysctl.conf" || return 1
    fi
    printf '仅保存共享 TCP BBR 的开启前恢复资料，不包含节点配置。\n如需恢复，重新安装面板后，将 bbr-active 和 backups 中的该项备份复制回 %s，再执行 sing-box bbr-disable。\n' "$STATE_DIR" >"$bbr_archive/README.txt" || return 1
    chmod 600 "$bbr_archive/README.txt" || return 1
    printf '%s\n' "$bbr_archive"
)

purge_bbr_settings() (
    local bbr_backup="" bbr_archive=""
    [[ -e $BBR_RESTORE_FILE || -e $BBR_SYSCTL_FILE ]] || return 0
    if bbr_backup=$(bbr_restore_backup 2>/dev/null); then
        # The complete purge has already been confirmed with DELETE.
        confirm() { return 0; }
        if disable_bbr_locked >/dev/null 2>&1; then
            info "已恢复开启 TCP BBR 前的算法、默认队列和原持久化文件状态。"
            return 0
        fi
        warn "未能安全恢复开启前的 TCP BBR 设置；继续卸载，不猜测或强制修改当前运行参数。"
        if bbr_backup=$(bbr_restore_backup 2>/dev/null) && bbr_archive=$(archive_bbr_recovery "$bbr_backup" 2>/dev/null); then
            warn "有效 BBR 恢复资料另存于：$bbr_archive"
        else
            warn "BBR 恢复资料另存失败；继续卸载，恢复资料可能丢失。"
        fi
    else
        warn "BBR 恢复记录缺失或无效；无法准确还原原算法和默认队列，保留当前运行参数并继续卸载。"
    fi
    if [[ -e $BBR_SYSCTL_FILE ]]; then
        if bbr_owned_config "$BBR_SYSCTL_FILE"; then
            if rm -f -- "$BBR_SYSCTL_FILE" && [[ ! -e $BBR_SYSCTL_FILE && ! -L $BBR_SYSCTL_FILE ]]; then
                info "已删除本面板的 TCP BBR 持久化文件：$BBR_SYSCTL_FILE"
            else
                warn "未能删除 BBR 持久化文件，请检查权限并手动清理：$BBR_SYSCTL_FILE"
            fi
        else
            warn "BBR 文件不属于本面板或含有其他设置，已保留，请人工核对：$BBR_SYSCTL_FILE"
        fi
    fi
)

purge_install_locked() {
    local source=${1:-}
    [[ $# -le 1 ]] || return 1
    is_managed && validate_purge_paths "$source" && check_conflicts || return 1
    uninstall_core_locked purge || return 1
    validate_purge_paths "$source" || return 1
    purge_bbr_settings
    validate_purge_paths "$source" || return 1
    if ! rm -rf --one-file-system -- "$CONFIG_DIR" "$STATE_DIR" "$LIB_DIR" ||
       ! rm -f -- "$WRAPPER"; then
        fail "彻底卸载未完成，请核对上方删除清单和剩余文件。"; return 1
    fi
    [[ -z $source ]] || rm -f -- "$source" || return 1
    info "已彻底卸载 sing-box 核心、服务、面板命令、节点配置、连接信息和面板备份。"
    [[ -z $source ]] || info "已删除指定的下载脚本：$source"
    info "外部 ACME 证书、ACME 菜单、其他程序及系统日志保留。"
}

purge_uninstall() {
    local source="" answer
    is_managed || { fail "没有本脚本管理的安装。"; return 1; }
    prompt source "额外删除的原下载脚本绝对路径（留空保留源码副本，0 取消）" || return 1
    [[ $source != 0 ]] || return 0
    validate_purge_paths "$source" && check_conflicts || return 1
    if [[ -e $BBR_RESTORE_FILE || -e $BBR_SYSCTL_FILE ]]; then
        info "将撤销本面板的 TCP BBR 设置，恢复原算法、默认队列和原文件状态；这会影响整机默认 TCP 设置，也可能影响 Xray 等服务。"
        info "恢复资料缺失或恢复失败不阻止卸载；仅清理本面板的 BBR 配置，不猜测原算法，不修改其他 sysctl 文件。"
        info "BBR 持久化文件：$BBR_SYSCTL_FILE（原来无此文件则删除；原来已有则恢复；非本面板文件保留）。"
    fi
    printf '\n将永久删除以下路径（含全部节点及面板备份）：\n  %s\n  %s\n  %s\n  %s\n  %s\n' "$CORE_BIN" "$LIB_DIR" "$WRAPPER" "$CONFIG_DIR" "$STATE_DIR"
    printf '  %s\n' "$UNIT_FILE"
    [[ -z $source ]] || printf '  %s\n' "$source"
    info "不会递归删除整个源码仓库；外部证书、ACME 和 Xray 保留。"
    prompt answer "此操作不可撤销；输入 DELETE 确认，其他输入取消" 0 || return 1
    [[ $answer == DELETE ]] || { info "已取消彻底卸载。"; return 0; }
    with_lock purge_install_locked "$source"
}

uninstall_menu() {
    local uninstall_choice
    printf '\n卸载 sing-box\n  1. 仅卸载核心和服务（保留面板、配置和备份）\n  2. 彻底卸载（清除面板、核心、配置和备份，撤销本面板 BBR 设置）\n  0. 返回\n'
    prompt uninstall_choice "请选择卸载方式" 1 || return 1
    case "$uninstall_choice" in 0) return 0;;1) uninstall_core;;2) purge_uninstall;;*) fail "无效选项。";;esac
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
        read -e -r -p "$message [$default]: " input || return 1
    else
        read -e -r -p "$message: " input || return 1
    fi
    printf -v "$variable" '%s' "${input:-$default}"
}

select_inbound() {
    local destination=$1 operation=${2:-操作} snapshot count inbound_selection candidate_tag current
    # Caller owns tag/old_tag/selected_tag, as with prompt's dynamic assignment.
    printf -v "$destination" '%s' ""
    snapshot=$(logical_inbounds | jq -ce 'if type=="array" then . else error("inbounds must be an array") end') || return 1
    count=$(jq 'length' <<<"$snapshot") || return 1
    [[ $count -gt 0 ]] || { info "暂无入站配置。"; return 0; }
    jq -r '["序号","名称","协议","端口"],
        (to_entries[] | [.key+1,.value.tag,(.value._manager_type // .value.type),(.value.listen_port // "-")])
        | map(if type=="string" then gsub("[[:cntrl:]]"; "?") else . end) | @tsv' <<<"$snapshot" || return 1
    info "可输入序号或配置名；纯数字配置名请使用 name:名称。"
    while true; do
        prompt inbound_selection "选择要${operation}的配置（0 返回）" 0 || return 1
        case "$inbound_selection" in
            0) return 0 ;;
            name:*) candidate_tag=${inbound_selection#name:} ;;
            *)
                if [[ $inbound_selection =~ ^[0-9]+$ ]]; then
                    if [[ ! $inbound_selection =~ ^[1-9][0-9]{0,5}$ ]] || (( inbound_selection > count )); then
                        warn "序号无效，请选择 1–$count；纯数字名称请使用 name:名称。"
                        continue
                    fi
                    candidate_tag=$(jq -r --argjson index "$((inbound_selection - 1))" '.[$index].tag' <<<"$snapshot") || return 1
                else
                    candidate_tag=$inbound_selection
                fi ;;
        esac
        if ! valid_tag "$candidate_tag" ||
           ! jq -e --arg tag "$candidate_tag" '[.[] | select(.tag==$tag)] | length==1' <<<"$snapshot" >/dev/null; then
            warn "配置不存在、名称不支持或名称重复，请重新选择。"
            continue
        fi
        current=$(logical_inbounds | jq -Sc .) || return 1
        if [[ $current != "$(jq -Sc . <<<"$snapshot")" ]]; then
            fail "配置列表已被其他操作修改，请重新进入选择，避免序号对应错误。"; return 1
        fi
        printf -v "$destination" '%s' "$candidate_tag"
        info "已选择配置：$candidate_tag"
        return 0
    done
}

read_default_server() {
    local server
    [[ -e $DEFAULT_SERVER_FILE || -L $DEFAULT_SERVER_FILE ]] || return 0
    [[ -f $DEFAULT_SERVER_FILE && ! -L $DEFAULT_SERVER_FILE ]] || {
        fail "默认连接地址文件无效或为符号链接。"; return 1;
    }
    server=$(cat "$DEFAULT_SERVER_FILE") || return
    valid_host "$server" || { fail "默认连接地址格式无效，请重新设置。"; return 1; }
    printf '%s\n' "$server"
}

prompt_secret() {
    local variable=$1 message=$2 default=${3:-} input
    [[ -t 0 ]] || { fail "修改认证信息需要交互终端。"; return 1; }
    IFS= read -e -r -s -p "$message: " input || { printf '\n' >&2; return 1; }
    printf '\n' >&2
    printf -v "$variable" '%s' "${input:-$default}"
}

prompt_hysteria2_obfs() {
    local destination=$1 current=${2:-} obfs_choice obfs_default=1 selected_obfs message
    [[ -z $current ]] || obfs_default=2
    prompt obfs_choice "Hysteria 2 混淆：1 不启用，2 Salamander，0 取消" "$obfs_default" || return 1
    case "$obfs_choice" in
        0) info "已取消混淆设置。"; return 1 ;;
        1) printf -v "$destination" '%s' ""; return 0 ;;
        2)
            if [[ -n $current ]]; then message="Salamander 混淆密码（至少 4 字节；回车保留，@new 重新生成）"
            else message="Salamander 混淆密码（至少 4 字节；回车或 @new 自动生成）"; fi
            prompt_secret selected_obfs "$message" "$current" || return 1
            if [[ -z $selected_obfs || $selected_obfs == @new ]]; then
                selected_obfs=$("$CORE_BIN" generate rand --base64 16) || return 1
            fi
            valid_salamander_password "$selected_obfs" || { fail "Salamander 混淆密码须至少 4 字节。"; return 1; }
            printf -v "$destination" '%s' "$selected_obfs"
            ;;
        *) fail "请输入 0–2。"; return 1 ;;
    esac
}

prompt_quic_bbr() {
    prompt "$1" "QUIC BBR：1 开启（回车默认），2 关闭/恢复原设置，0 取消" 1 || return 1
    case "${!1}" in
        1|2) return 0 ;;
        0) info "已取消 QUIC BBR 设置。"; return 1 ;;
        *) fail "请输入 0–2。"; return 1 ;;
    esac
}

prepare_quic_bbr_wizard() {
    local file=$1 choice=$2 record_source=$3 record_output=$4 enabled
    : >"$record_output" || return 1
    if [[ $record_source != - ]]; then
        generate_quic_bbr_restore "$file" "$record_source" >/dev/null || return 1
    fi
    case "$choice" in
        1)
            enabled=$(generate_quic_bbr_inbound "$file") || return 1
            if [[ $record_source == - && $(jq -Sc . "$file") != "$(jq -Sc . <<<"$enabled")" ]]; then
                make_quic_bbr_record "$file" >"$record_output" || return 1
            fi
            printf '%s\n' "$enabled"
            ;;
        2)
            if [[ $record_source != - ]]; then
                generate_quic_bbr_restore "$file" "$record_source" || return 1
                printf 'null\n' >"$record_output" || return 1
            elif jq -e 'if .type=="tuic" then .congestion_control=="bbr" else
              (.up_mbps // 0)==0 and (.down_mbps // 0)==0 and .ignore_client_bandwidth==true end' "$file" >/dev/null; then
                fail "没有开启前的恢复记录，不能猜测关闭后的原值；请先核对旧备份。"; return 1
            else cat -- "$file"; fi
            ;;
        *) fail "无效的 QUIC BBR 选项。"; return 1 ;;
    esac
}

validate_inbound_users() {
    if [[ $(jq -r .type "$1") == snell ]]; then validate_snell_template "$1"; return; fi
    jq -e '
      def uuid_ok: type=="string" and test("^[[:xdigit:]]{8}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{4}-[[:xdigit:]]{12}$");
      if .type=="shadowsocks" then .users==null else . as $node |
        (["vless","vmess","hysteria2","tuic","anytls"]|index($node.type))!=null and
        (.users|type=="array" and length>0) and
        all(.users[];
          type=="object" and (.name==null or (.name|type=="string" and (test("[[:cntrl:]]")|not))) and
          if $node.type=="vless" or $node.type=="vmess" then
            (keys-["name","uuid","flow","alterId"]|length==0) and (.uuid|uuid_ok) and
            (if $node.type=="vmess" then (.alterId // 0)==0 and .flow==null
             else (.flow // "")==($node.users[0].flow // "") end)
          else (keys-["name","uuid","password"]|length==0) and
            (.password|type=="string" and length>0 and (contains("\u0000")|not)) and
            (if $node.type=="tuic" then (.uuid|uuid_ok) else .uuid==null end) end) and
        ([.users[] | .name // "" | select(.!="")] | length as $n | (unique|length)==$n) and
        ([.users[] | if $node.type=="vless" or $node.type=="vmess" or $node.type=="tuic"
                    then .uuid|ascii_downcase else .password end] | length as $n | (unique|length)==$n)
      end' "$1" >/dev/null 2>&1 || {
        fail "用户列表无效：不支持的字段、认证或名称重复、空认证或不一致的 VLESS flow；现有用户不会被忽略。"; return 1;
    }
}

inbound_user_count() {
    jq -er 'if (.type=="shadowsocks" and .users==null) or (.type=="snell" and (.users==null or .users==[])) then 1
      elif (.users|type)=="array" and (.users|length)>0 then .users|length
      else error("invalid users") end' "$1"
}

select_inbound_user() {
    local destination=$1 file=$2 count inbound_user_selection
    printf -v "$destination" '%s' ""
    validate_inbound_users "$file" || return 1
    count=$(inbound_user_count "$file") || return 1
    case "$(jq -r .type "$file")" in shadowsocks|snell) fail "此模板仅支持单密码/PSK，请使用修改配置。"; return 1;; esac
    jq -r '["用户序号","用户名"], (.users|to_entries[]|[.key+1,(.value.name // "未命名")]) | @tsv' "$file" || return 1
    while true; do
        prompt inbound_user_selection "选择用户序号（0 返回）" 0 || return 1
        [[ $inbound_user_selection != 0 ]] || return 0
        if [[ $inbound_user_selection =~ ^[1-9][0-9]{0,5}$ ]] && (( inbound_user_selection <= count )); then
            printf -v "$destination" '%s' "$inbound_user_selection"; return 0
        fi
        warn "用户序号无效，请选择 1–$count。"
    done
}

detect_inbound_template() {
    validate_obfs_template "$1" || return
    validate_inbound_users "$1" || return
    if [[ $(jq -r .type "$1") == snell ]]; then
        validate_snell_template "$1" || return
        jq -r 'if .version==5 then "snell" else "snell-v6" end' "$1"; return
    fi
    jq -er '
      if .type == "shadowsocks" and .method == "2022-blake3-aes-128-gcm" and .users == null then "shadowsocks"
      elif (.users | length) < 1 then error("empty user list")
      elif .type == "hysteria2" or .type == "tuic" or .type == "anytls" then
        if .tls.enabled == true then .type else error("TLS required") end
      elif .type == "vless" then
        if .tls.reality.enabled == true then
          if .transport == null and .users[0].flow == "xtls-rprx-vision" then "vless-reality"
          elif .transport.type == "grpc" and (.users[0].flow // "") == "" then "vless-grpc-reality"
          else error("unsupported REALITY transport/flow") end
        elif (.users[0].flow // "") != "" then error("unsupported flow")
        elif .transport.type == "grpc" and .tls.enabled == true then "vless-grpc-tls"
        elif .transport.type == "ws" and .tls.enabled == true then "vless-ws-tls"
        elif .transport == null and .tls.enabled == true then "vless-tcp-tls"
        elif .tls.enabled != true and .transport.type == "ws" then "vless-ws"
        elif .tls.enabled != true and .transport == null then "vless-tcp"
        else error("unsupported VLESS template") end
      elif .type == "vmess" and (.users[0].alterId // 0) == 0 then
        (if .transport == null then "vmess-tcp"
         elif .transport.type == "ws" then "vmess-ws"
         elif .transport.type == "grpc" and .tls.enabled == true then "vmess-grpc"
         else error("unsupported VMess transport") end)
        + (if .tls.enabled == true then "-tls" else "" end)
      else error("unsupported template") end' "$1" 2>/dev/null || {
        fail "该配置不属于当前支持的向导模板，请通过完整 JSON 导入修改。"; return 1;
    }
}

# shellcheck disable=SC2016 # json_query filters use literal jq variables.
replace_inbound_file() (
    umask 077
    local old_tag=$1 file=$2 server=${3:-} public_key=${4:-} expected=${5:-}
    local quic_record=${6:-} expected_metadata=${7:-}
    local activation=${8:-preserve} start_requested=0
    local tag port old_port work snapshot old_metadata new_metadata effective backup=""
    local transaction=0 old_metadata_exists=0 active=0 changed=0
    require_core && safe_paths || exit 1
    case "$activation" in preserve|start) ;; *) fail "未知的服务启动策略。"; exit 1;; esac
    valid_tag "$old_tag" || { fail "原配置名称无效。"; exit 1; }
    jq -e 'type == "object" and (.tag|type == "string") and (.listen_port|type == "number")' "$file" >/dev/null || exit 1
    tag=$(jq -r .tag "$file") && port=$(jq -r .listen_port "$file") || exit 1
    if ! valid_tag "$tag" || ! valid_port "$port"; then fail "名称或端口无效。"; exit 1; fi
    [[ -z $server ]] || valid_host "$server" || { fail "客户端连接地址无效。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/edit-save.XXXXXX") || exit 1
    # The outer command holds manager.lock across config and metadata changes.
    # shellcheck disable=SC2317,SC2329 # Invoked by EXIT/INT/TERM traps below.
    rollback_edit() {
        local result=$? rollback_ok=1
        trap - EXIT INT TERM
        if [[ $transaction -eq 1 ]]; then
            if ! cmp -s "$backup/config.json" "$CONFIG_FILE"; then
                atomic_copy "$backup/config.json" "$CONFIG_FILE" 600 || rollback_ok=0
                if [[ $active -eq 1 ]]; then restart_checked || rollback_ok=0
                else systemctl stop "$SERVICE" || rollback_ok=0; fi
            elif [[ $active -eq 1 ]] && ! service_is_active; then
                restart_checked || rollback_ok=0
            fi
            if [[ $active -eq 0 && $start_requested -eq 1 ]]; then systemctl stop "$SERVICE" || rollback_ok=0; fi
            if [[ $old_metadata_exists -eq 1 ]]; then
                if ! cmp -s "$backup/metadata.json" "$old_metadata"; then
                    atomic_copy "$backup/metadata.json" "$old_metadata" 600 || rollback_ok=0
                fi
            else
                rm -f -- "$old_metadata" || rollback_ok=0
            fi
            [[ $tag == "$old_tag" ]] || rm -f -- "$new_metadata" || rollback_ok=0
            if [[ $rollback_ok -eq 1 ]]; then warn "修改失败，已恢复原配置及连接信息。"
            else warn "自动恢复未完全成功，请使用备份检查恢复：$backup"; fi
            result=1
        fi
        rm -rf -- "$work"
        exit "$result"
    }
    trap rollback_edit EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    snapshot="$work/original.json"
    cp -- "$CONFIG_FILE" "$snapshot" || exit 1
    json_query "$snapshot" -e --arg tag "$old_tag" '[.inbounds[]? | select(.tag==$tag)] | if length==1 then .[0] else error("配置不存在或名称重复") end' \
        >"$work/old-inbound.json" || exit 1
    if [[ -n $expected && $(jq -Sc . "$expected") != "$(jq -Sc . "$work/old-inbound.json")" ]]; then
        fail "选中的配置在编辑期间已被修改，请重新打开编辑。"; exit 1
    fi
    [[ $(detect_inbound_template "$file") == "$(detect_inbound_template "$work/old-inbound.json")" ]] || {
        fail "修改配置不切换协议类型，请分别添加所需协议。"; exit 1;
    }
    detect_inbound_template "$file" >/dev/null || exit 1
    old_port=$(jq -r .listen_port "$work/old-inbound.json") || exit 1
    check_inbound_conflicts "$file" "$old_tag" "$snapshot" || exit 1
    if [[ $tag != "$old_tag" ]] && json_query "$snapshot" -e --arg old "$old_tag" \
        'del(.inbounds[] | select(.tag==$old)) | any(.. | strings; .==$old)' >/dev/null; then
        fail "原名称被路由或其他配置引用，请保留名称或通过完整 JSON 同步修改引用。"; exit 1
    fi
    old_metadata="$CLIENT_DIR/$old_tag.json"
    new_metadata="$CLIENT_DIR/$tag.json"
    [[ ! -L $old_metadata && ! -L $new_metadata ]] || { fail "拒绝操作连接信息符号链接。"; exit 1; }
    [[ ! -e $old_metadata || -f $old_metadata ]] || { fail "原连接信息不是普通文件。"; exit 1; }
    [[ $tag == "$old_tag" || ! -e $new_metadata ]] || { fail "新名称已有连接信息文件，请使用其他名称。"; exit 1; }
    if [[ $expected_metadata == - ]]; then
        [[ ! -e $old_metadata ]] || { fail "连接信息已在确认期间创建，请重新操作。"; exit 1; }
    elif [[ -n $expected_metadata ]]; then
        cmp -s "$expected_metadata" "$old_metadata" || { fail "连接信息已在确认期间修改，请重新操作。"; exit 1; }
    fi
    if [[ -n $quic_record ]]; then
        case "$(jq -r .type "$file")" in hysteria2|tuic) ;; *) fail "此协议不支持 QUIC 恢复记录。"; exit 1;; esac
        if [[ $quic_record != null ]]; then
            printf '%s\n' "$quic_record" >"$work/quic-record.json" || exit 1
            validate_quic_bbr_record "$work/quic-record.json" "$(jq -r .type "$file")" || exit 1
        fi
    fi
    effective=$server
    [[ -n $effective ]] || effective=$(read_default_server) || exit 1
    if [[ -z $effective && $(jq -r .type "$file") == hysteria2 ]]; then effective=$(jq -r '.tls.server_name // ""' "$file"); fi
    generate_client "$file" "${effective:-example.invalid}" "$public_key" 1 >"$work/client-check.json" || exit 1
    json_query "$snapshot" --arg old "$old_tag" --slurpfile node "$file" \
        '.inbounds |= map(if .tag==$old then $node[0] else . end)' >"$work/candidate.json" || exit 1
    validate_config "$work/candidate.json" || exit 1
    if [[ -f $old_metadata ]]; then
        old_metadata_exists=1
        jq -e 'type == "object"' "$old_metadata" >/dev/null || exit 1
        cp -- "$old_metadata" "$work/metadata-before.json" || exit 1
    else printf '{}\n' >"$work/metadata-before.json"; fi
    jq --arg server "$server" --arg key "$public_key" --slurpfile node "$file" --arg record "$quic_record" \
        '.server=$server | .public_key=$key | .inbound=$node[0]
         | if has("user_outbound_routes") then .user_outbound_routes.inbound=$node[0] else . end
         | if $record=="" then . elif $record=="null" then del(.quic_bbr_restore)
           else .quic_bbr_restore=($record|fromjson) end' "$work/metadata-before.json" >"$work/metadata.json" || exit 1
    if jq -e 'has("user_outbound_routes")' "$work/metadata-before.json" >/dev/null; then
        snapshot_bundle >"$work/user-routes-before.json" || exit 1
        user_outbound_bundle validate "$work/user-routes-before.json" "$old_tag" >/dev/null || exit 1
        jq --slurpfile config "$work/candidate.json" --slurpfile metadata "$work/metadata.json" --arg t "$tag" \
            '.config=$config[0] | .clients[$t]=$metadata[0]' "$work/user-routes-before.json" >"$work/user-routes-after.json" || exit 1
        user_outbound_bundle validate "$work/user-routes-after.json" "$tag" >/dev/null || {
            fail "不能删除或更名已绑定出站的用户；请先解除其绑定。"; exit 1;
        }
    fi
    [[ $(json_query "$snapshot" -Sc .) == "$(jq -Sc . "$work/candidate.json")" ]] || changed=1
    if [[ $changed -eq 0 && $old_metadata_exists -eq 1 &&
          $(jq -Sc . "$work/metadata-before.json") == "$(jq -Sc . "$work/metadata.json")" ]]; then
        info "配置没有变化。"; exit 0
    fi
    cmp -s "$CONFIG_FILE" "$snapshot" || { fail "配置已被其他程序修改，已取消保存。"; exit 1; }
    [[ $old_metadata_exists -eq 0 ]] || cmp -s "$old_metadata" "$work/metadata-before.json" || {
        fail "连接信息已被其他程序修改，已取消保存。"; exit 1;
    }
    backup=$(mktemp -d "$BACKUP_DIR/edit-$(date +%Y%m%d-%H%M%S)-XXXXXX") || exit 1
    install -m 600 "$snapshot" "$backup/config.json" || exit 1
    [[ $old_metadata_exists -eq 0 ]] || install -m 600 "$old_metadata" "$backup/metadata.json" || exit 1
    service_is_active && active=1
    install -d -m 700 "$CLIENT_DIR" || exit 1
    transaction=1
    atomic_copy "$work/metadata.json" "$new_metadata" 600 || exit 1
    if [[ $changed -ne 0 ]]; then
        [[ $activation != start || $active -ne 0 ]] || start_requested=1
        commit_config "$work/candidate.json" preserve "$activation" || exit 1
    fi
    [[ $tag == "$old_tag" ]] || rm -f -- "$old_metadata" || exit 1
    transaction=0
    info "已修改 $tag；连接信息已同步。备份：$backup"
    [[ $port == "$old_port" ]] || info "监听端口已变更，请同步云平台和服务器防火墙规则，并重新导入客户端链接。"
    [[ $changed -ne 0 ]] || info "仅更新客户端连接信息，未重启服务。"
)

# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
edit_wizard_locked() (
    umask 077
    require_core || exit 1
    local old_tag=${1:-} tag port protocol work metadata server="" public_key="" credential extra=""
    local private_key="" short_id="" tls_name="" certificate="" tls_key="" password="" obfs_password="" rotation pair
    local quic_bbr_choice="" quic_record="" record_source=- expected_metadata=""
    local selected_user_index=1 user_count
    [[ $# -le 1 ]] || { fail "用法：sing-box edit [配置名称]"; exit 1; }
    if [[ -z $old_tag ]]; then
        select_inbound old_tag "修改" || exit 1
        [[ -n $old_tag ]] || exit 0
    fi
    valid_tag "$old_tag" || { fail "配置名称无效。"; exit 1; }
    check_shadowtls_component "$old_tag" || exit 1
    if is_snell_shadowtls "$old_tag"; then snell_shadowtls_wizard_locked "$old_tag"; exit $?; fi
    work=$(mktemp -d "$STATE_DIR/edit-wizard.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    config_jq -e --arg tag "$old_tag" '[.inbounds[]? | select(.tag==$tag)] | if length==1 then .[0] else error("配置不存在或名称重复") end' \
        >"$work/original.json" || exit 1
    protocol=$(detect_inbound_template "$work/original.json") || exit 1
    user_count=$(inbound_user_count "$work/original.json") || exit 1
    if [[ $user_count -gt 1 ]]; then
        info "此入站有 $user_count 个用户：认证信息只修改所选用户；端口、TLS、REALITY、混淆和 BBR 是共享设置。"
        select_inbound_user selected_user_index "$work/original.json" || exit 1
        [[ -n $selected_user_index ]] || exit 0
    fi
    tag=$old_tag
    port=$(jq -r .listen_port "$work/original.json")
    credential=$(jq -jr --argjson i "$((selected_user_index - 1))" '(.users[$i].uuid // .users[$i].password // .password // .psk // "") + "."' "$work/original.json") || exit 1
    credential=${credential%.}
    password=$(jq -jr --argjson i "$((selected_user_index - 1))" '(.users[$i].password // "") + "."' "$work/original.json") || exit 1
    password=${password%.}
    extra=$(jq -r '.transport.path // .transport.service_name // ""' "$work/original.json")
    if [[ $protocol == snell ]]; then extra=$(jq -r '.obfs_mode // "none"' "$work/original.json")
    elif [[ $protocol == snell-v6 ]]; then extra=$(jq -r '.mode // "default"' "$work/original.json"); fi
    tls_name=$(jq -r '.tls.server_name // .tls.reality.handshake.server // ""' "$work/original.json")
    private_key=$(jq -r '.tls.reality.private_key // ""' "$work/original.json")
    short_id=$(jq -r '.tls.reality.short_id[0] // ""' "$work/original.json")
    certificate=$(jq -r '.tls.certificate_path // ""' "$work/original.json")
    tls_key=$(jq -r '.tls.key_path // ""' "$work/original.json")
    if [[ $protocol == hysteria2 ]]; then
        obfs_password=$(jq -jr '(.obfs.password // "") + "."' "$work/original.json") || exit 1
        obfs_password=${obfs_password%.}
    fi
    metadata="$CLIENT_DIR/$old_tag.json"
    [[ ! -L $metadata ]] || { fail "拒绝读取连接信息符号链接。"; exit 1; }
    if [[ -f $metadata && $(jq -Sc .inbound "$metadata") == "$(jq -Sc . "$work/original.json")" ]]; then
        server=$(jq -r '.server // ""' "$metadata")
        public_key=$(jq -r '.public_key // ""' "$metadata")
    fi
    if [[ $protocol == hysteria2 || $protocol == tuic ]]; then
        [[ ! -e $metadata || -f $metadata ]] || { fail "连接信息文件无效。"; exit 1; }
        expected_metadata=-
        if [[ -f $metadata ]]; then
            cp -- "$metadata" "$work/metadata-before.json" || exit 1
            expected_metadata="$work/metadata-before.json"
            jq -e 'type=="object"' "$expected_metadata" >/dev/null || exit 1
            if jq -e 'has("quic_bbr_restore")' "$expected_metadata" >/dev/null; then
                [[ $(jq -Sc .inbound "$expected_metadata") == "$(jq -Sc . "$work/original.json")" ]] || {
                    fail "节点与恢复记录不一致，已取消编辑。"; exit 1;
                }
                record_source="$work/quic-record-before.json"
                jq '.quic_bbr_restore' "$expected_metadata" >"$record_source" || exit 1
                generate_quic_bbr_restore "$work/original.json" "$record_source" >/dev/null || exit 1
            fi
        fi
    fi
    info "修改 $old_tag（$protocol）：回车保留原值；认证信息不回显，输入 @new 可重新生成。"
    prompt tag "配置名称" "$tag" && prompt port "监听端口" "$port" || exit 1
    if ! valid_tag "$tag" || ! valid_port "$port"; then fail "名称或端口无效。"; exit 1; fi
    case "$protocol" in
        vless-*|vmess-*|tuic)
            prompt_secret credential "UUID（回车保留，@new 重新生成）" "$credential" || exit 1
            [[ $credential != @new ]] || credential=$("$CORE_BIN" generate uuid) || exit 1 ;;
        *)
            prompt_secret credential "密码（回车保留，@new 重新生成）" "$credential" || exit 1
            [[ $credential != @new ]] || credential=$("$CORE_BIN" generate rand --base64 16) || exit 1 ;;
    esac
    if [[ $protocol == tuic ]]; then
        prompt_secret password "TUIC 密码（回车保留，@new 重新生成）" "$password" || exit 1
        [[ $password != @new ]] || password=$("$CORE_BIN" generate rand --base64 16) || exit 1
    fi
    [[ $protocol != hysteria2 ]] || prompt_hysteria2_obfs obfs_password "$obfs_password" || exit 1
    if [[ $protocol == snell || $protocol == snell-v6 ]]; then prompt_snell_options extra "$protocol" "$extra" || exit 1; fi
    if [[ $protocol == hysteria2 || $protocol == tuic ]]; then prompt_quic_bbr quic_bbr_choice || exit 1; fi
    case "$protocol" in
        *-ws|*-ws-tls) prompt extra "WebSocket 路径" "$extra" || exit 1 ;;
        *-grpc-*) prompt extra "gRPC 服务名" "$extra" || exit 1 ;;
    esac
    case "$protocol" in
        vless-reality|vless-grpc-reality)
            prompt tls_name "REALITY 握手域名" "$tls_name" || exit 1
            [[ $protocol != vless-reality ]] || extra=$tls_name
            prompt rotation "REALITY 密钥：1 保留，2 重新生成" 1 || exit 1
            case "$rotation" in
                1)
                    [[ -n $public_key ]] || prompt_secret public_key "缺少当前密钥对应公钥，请填写原公钥（或退出后选择重新生成）" || exit 1 ;;
                2)
                    pair=$("$CORE_BIN" generate reality-keypair) || exit 1
                    private_key=$(printf '%s\n' "$pair" | sed -n 's/^PrivateKey: *//p')
                    public_key=$(printf '%s\n' "$pair" | sed -n 's/^PublicKey: *//p') ;;
                *) fail "无效的密钥操作。"; exit 1 ;;
            esac
            prompt_secret short_id "REALITY short ID（回车保留，@new 重新生成）" "$short_id" || exit 1
            [[ $short_id != @new ]] || short_id=$(openssl rand -hex 8) || exit 1
            ;;
        *-tls|hysteria2|tuic|anytls)
            prompt tls_name "TLS 证书域名/IP（客户端 SNI）" "$tls_name" || exit 1
            prompt_tls_certificate_files "$tls_name" "$certificate" "$tls_key" || exit 1
            ;;
    esac
    prompt server "客户端连接地址（回车保留，- 清除独立地址并使用默认）" "$server" || exit 1
    [[ $server != - ]] || server=""
    generate_inbound "$protocol" "$tag" "$port" "$credential" "$extra" "$private_key" "$short_id" \
        "$tls_name" "$certificate" "$tls_key" "$password" "$obfs_password" >"$work/generated.json" || exit 1
    # Overlay only template fields; preserve listen options, user names, TLS ALPN,
    # and a non-default REALITY handshake port from an imported configuration.
    jq --slurpfile updated "$work/generated.json" --argjson i "$((selected_user_index - 1))" '
        . as $old | . * $updated[0]
        | .listen = $old.listen
        | if $old.users != null and $old.type!="snell" then .users=$old.users | .users[$i]=($old.users[$i] * $updated[0].users[0]) else . end
        | if $old.tls.alpn != null then .tls.alpn=$old.tls.alpn else . end
        | if $old.tls.reality.handshake.server_port != null then
            .tls.reality.handshake.server_port=$old.tls.reality.handshake.server_port else . end
        | if $old.type == "tuic" then
            if ($old|has("congestion_control")) then .congestion_control=$old.congestion_control else del(.congestion_control) end
          else . end
        | if $old.type == "hysteria2" then
            if $updated[0].obfs == null then del(.obfs) else .obfs=$updated[0].obfs end
          else . end
        | if $old.listen == null then del(.listen) else . end' "$work/original.json" >"$work/updated.json" || exit 1
    if [[ -n $quic_bbr_choice ]]; then
        prepare_quic_bbr_wizard "$work/updated.json" "$quic_bbr_choice" "$record_source" "$work/quic-record-update.json" >"$work/quic-updated.json" || exit 1
        mv -- "$work/quic-updated.json" "$work/updated.json" || exit 1
        quic_record=$(cat "$work/quic-record-update.json") || exit 1
    fi
    info "待保存：$old_tag → $tag，端口 $port。"
    if ! confirm "保存修改？运行中的服务将按需重启，失败时回滚；修改认证或端口后需重新导入客户端链接。"; then
        info "已取消，配置未修改。"; exit 0
    fi
    replace_inbound_file "$old_tag" "$work/updated.json" "$server" "$public_key" "$work/original.json" "$quic_record" "$expected_metadata"
)

set_default_server() (
    umask 077
    local server=${1:-} current work
    [[ $# -le 1 ]] || { fail "用法：sing-box server [公网 IP/域名|-]"; exit 1; }
    safe_paths || exit 1
    if [[ $# -eq 0 ]]; then
        current=$(read_default_server) || exit 1
        prompt server "默认连接地址（公网 IP/域名；输入 - 清除）" "$current" || exit 1
    fi
    if [[ $server == - ]]; then
        rm -f -- "$DEFAULT_SERVER_FILE" || exit 1
        info "已清除默认连接地址。Hysteria 2 未指定地址时仍使用证书域名。"
        exit 0
    fi
    valid_host "$server" || { fail "请输入有效的公网 IP 或域名，不含协议和端口。"; exit 1; }
    work=$(mktemp "$STATE_DIR/.default-server.XXXXXX") || exit 1
    trap 'rm -f -- "$work"' EXIT
    printf '%s\n' "$server" >"$work" || exit 1
    atomic_copy "$work" "$DEFAULT_SERVER_FILE" 600 || exit 1
    info "默认连接地址已设置：$server"
    info "未指定连接地址的配置将使用此地址，已有独立地址优先；无需重启服务。"
)

generate_client() {
    local inbound=$1 server=$2 public_key=${3:-} user_index=${4:-} count
    valid_host "$server" || { fail "服务器地址格式无效。"; return 1; }
    validate_obfs_template "$inbound" || return
    validate_inbound_users "$inbound" || return
    count=$(inbound_user_count "$inbound") || return
    if [[ -z $user_index ]]; then
        [[ $count -eq 1 ]] || { fail "此入站有多个用户，请明确选择用户序号后导出。"; return 1; }
        user_index=1
    fi
    if [[ ! $user_index =~ ^[1-9][0-9]{0,5}$ ]] || (( user_index > count )); then
        fail "用户序号无效。"; return 1
    fi
    if [[ $(jq -r .type "$inbound") == snell ]]; then
        validate_snell_template "$inbound" || return
        valid_port "$(jq -r .listen_port "$inbound")" || { fail "内部 Snell 不可单独导出，请从逻辑节点导出完整组合。"; return 1; }
        jq --arg server "$server" '. as $node | {type:"snell",tag:"proxy",server:$server,server_port:.listen_port,psk:.psk,
          version:(if .version==5 then 4 else 6 end)} +
          (if $node.version==5 then {obfs_mode:($node.obfs_mode // "none")} else {mode:($node.mode // "default")} end)' "$inbound"
        return
    fi
    # Import supports all core schemas, but export must never silently drop security options.
    jq -e '
      def transport_ok:
        .transport == null or
        (.transport.type == "ws" and (.transport | keys - ["type","path"] | length == 0)) or
        (.transport.type == "grpc" and .tls.enabled == true and
          (.transport.service_name | type == "string" and length > 0) and
          (.transport | keys - ["type","service_name"] | length == 0));
      def tls_ok:
        if .tls.enabled == true then
          (.tls.server_name | type == "string" and length > 0) and
          ((.tls.alpn // []) | type == "array" and all(.[]; type == "string")) and
          (if .tls.reality.enabled == true then
             .type == "vless" and
             (.tls | keys - ["enabled","server_name","reality","alpn"] | length == 0) and
             (.tls.reality | keys - ["enabled","handshake","private_key","short_id"] | length == 0) and
             (.tls.reality.short_id | length == 1) and
             ((.transport == null and .users[0].flow == "xtls-rprx-vision") or
              (.transport.type == "grpc" and (.users[0].flow // "") == ""))
           else
             (.tls | keys - ["enabled","server_name","certificate_path","key_path","alpn"] | length == 0)
           end)
        else .tls == null or .tls == {enabled:false} end;
      (.multiplex == null or .multiplex == {enabled:false}) and transport_ok and tls_ok and
      (if .type == "vless" or .type == "vmess" then
        (.users | length >= 1) and
        (.users[0] | keys - ["name","uuid","flow","alterId"] | length == 0) and
        (.users[0].uuid | type == "string" and length > 0) and
        (if .type == "vmess" then (.users[0].alterId // 0) == 0 and .users[0].flow == null
         elif .tls.reality.enabled != true then (.users[0].flow // "") == "" else true end)
      elif .type == "shadowsocks" then
        .method == "2022-blake3-aes-128-gcm" and .users == null and .destinations == null and
        .tls == null and .transport == null
      elif .type == "hysteria2" or .type == "tuic" or .type == "anytls" then
        (.users | length >= 1) and
        (.users[0] | keys - ["name","uuid","password"] | length == 0) and
        (.users[0].password | type == "string" and length > 0) and
        .tls.enabled == true and .transport == null and
        .realm == null and .masquerade == null and .padding_scheme == null and
        (if .type == "tuic" then (.users[0].uuid | type == "string" and length > 0) else true end)
      else false end)' "$inbound" >/dev/null || {
        fail "该导入配置超出客户端导出模板范围，请按原始配置设置客户端；不会忽略 TLS 或其他安全字段。"
        return 1
    }
    if [[ $(jq -r '.tls.reality.enabled // false' "$inbound") == true && ! $public_key =~ ^[a-zA-Z0-9_-]{43}$ ]]; then
        fail "缺少有效的 REALITY 公钥。"; return 1
    fi
    if [[ $(jq -r '.tls.enabled // false' "$inbound") == true &&
          $(jq -r '.tls.reality.enabled // false' "$inbound") != true ]]; then
        validate_tls_files "$(jq -r '.tls.server_name' "$inbound")" \
            "$(jq -r '.tls.certificate_path // ""' "$inbound")" \
            "$(jq -r '.tls.key_path // ""' "$inbound")" || return
    fi
    jq --arg server "$server" --arg key "$public_key" --argjson i "$((user_index - 1))" '
        . as $in | .users[$i] as $user | {tag:"proxy",type:.type,server:$server,server_port:.listen_port}
        | if .type == "vless" or .type == "vmess" then
            .uuid = $user.uuid
            | if $in.transport != null then .transport = $in.transport else . end
            | if .type == "vmess" then .security="auto" | .alter_id=0
              elif ($user.flow // "") != "" then .flow=$user.flow else . end
          elif .type == "shadowsocks" then .method=$in.method | .password=$in.password
          elif .type == "tuic" then
            .uuid=$user.uuid | .password=$user.password
            | .congestion_control=($in.congestion_control // "cubic")
          else .password=$user.password end
        | if $in.type == "hysteria2" and $in.obfs != null then .obfs=$in.obfs else . end
        | if $in.tls.enabled == true then
            .tls={enabled:true,server_name:$in.tls.server_name}
            | if $in.tls.alpn != null then .tls.alpn=$in.tls.alpn else . end
            | if $in.tls.reality.enabled == true then
                .tls.utls={enabled:true,fingerprint:"chrome"}
                | .tls.reality={enabled:true,public_key:$key,short_id:$in.tls.reality.short_id[0]}
              else . end
          else . end' "$inbound"
}

uri_encode() { jq -rn --arg value "$1" '$value|@uri'; }

client_link() {
    local file=$1 tag=${2:-sing-box} type server port credential transport query sni key sid path method alpn flow
    validate_obfs_template "$file" || return
    type=$(jq -r '.type' "$file") && server=$(jq -r '.server' "$file") &&
        port=$(jq -r '.server_port' "$file") || return
    [[ $server != *:* ]] || server="[$server]"
    case "$type" in
        snell)
            valid_tag "$tag" || { fail "Surge 配置名称无效。"; return 1; }
            jq -e '(keys-["type","tag","server","server_port","version","psk","obfs_mode","mode"]|length==0) and
              (.psk|type=="string" and test("^[A-Za-z0-9_+/@.=-]+$")) and
              ((.version==4 and .mode==null and ((.obfs_mode // "none")=="none" or .obfs_mode=="http")) or
               (.version==6 and .obfs_mode==null and ((.mode // "default")=="default" or .mode=="unshaped" or .mode=="unsafe-raw")))' "$file" >/dev/null || {
                fail "Snell 无统一分享 URI；此 PSK/参数无法安全生成 Surge 配置行，请使用客户端 JSON。"; return 1;
            }
            # Host and PSK are validated before emitting an intentional credential export.
            if ! valid_host "$(jq -r .server "$file")" || ! valid_port "$port"; then return 1; fi
            credential=$(jq -r .psk "$file") || return
            query="version=$(jq -r .version "$file")"
            if [[ $(jq -r .version "$file") == 4 && $(jq -r '.obfs_mode // "none"' "$file") == http ]]; then query+=", obfs=http"; fi
            if [[ $(jq -r .version "$file") == 6 && $(jq -r '.mode // "default"' "$file") != default ]]; then query+=", mode=$(jq -r .mode "$file")"; fi
            printf '%s = snell, %s, %s, psk=%s, %s\n' "$tag" "$server" "$port" "$credential" "$query"
            ;;
        vless)
            credential=$(jq -r '.uuid' "$file")
            transport=$(jq -r '.transport.type // "tcp"' "$file")
            query="encryption=none&type=$transport"
            if [[ $(jq -r '.tls.reality.enabled // false' "$file") == true ]]; then
                sni=$(jq -r '.tls.server_name' "$file")
                key=$(jq -r '.tls.reality.public_key' "$file")
                sid=$(jq -r '.tls.reality.short_id' "$file")
                query+="&security=reality&fp=chrome&sni=$(uri_encode "$sni")&pbk=$(uri_encode "$key")&sid=$sid"
                flow=$(jq -r '.flow // ""' "$file")
                [[ -z $flow ]] || query+="&flow=$(uri_encode "$flow")"
            elif [[ $(jq -r '.tls.enabled // false' "$file") == true ]]; then
                sni=$(jq -r '.tls.server_name' "$file")
                query+="&security=tls&sni=$(uri_encode "$sni")"
            else
                query+='&security=none'
            fi
            alpn=$(jq -r '(.tls.alpn // []) | join(",")' "$file")
            [[ -z $alpn ]] || query+="&alpn=$(uri_encode "$alpn")"
            if [[ $transport == ws ]]; then
                path=$(jq -r '.transport.path' "$file")
                query+="&path=$(uri_encode "$path")"
            elif [[ $transport == grpc ]]; then
                path=$(jq -r '.transport.service_name' "$file")
                query+="&serviceName=$(uri_encode "$path")&mode=gun"
            fi
            printf 'vless://%s@%s:%s?%s#%s\n' "$credential" "$server" "$port" "$query" "$(uri_encode "$tag")"
            ;;
        vmess)
            printf 'vmess://%s\n' "$(jq -r --arg tag "$tag" '
                {v:"2",ps:$tag,add:.server,port:(.server_port|tostring),id:.uuid,aid:"0",scy:(.security // "auto"),
                 net:(.transport.type // "tcp"),type:(if .transport.type == "grpc" then "gun" else "none" end),
                 host:"",path:(.transport.path // .transport.service_name // ""),
                 tls:(if .tls.enabled then "tls" else "" end),sni:(.tls.server_name // ""),
                 alpn:((.tls.alpn // [])|join(","))} | tojson | @base64' "$file")"
            ;;
        hysteria2|tuic|anytls)
            credential=$(jq -r '.password' "$file")
            sni=$(jq -r '.tls.server_name' "$file")
            alpn=$(jq -r '(.tls.alpn // []) | join(",")' "$file")
            query="sni=$(uri_encode "$sni")"
            [[ -z $alpn ]] || query+="&alpn=$(uri_encode "$alpn")"
            if [[ $type == hysteria2 && $(jq -r '.obfs.type // ""' "$file") == salamander ]]; then
                query+="&obfs=salamander&obfs-password=$(jq -r '.obfs.password | @uri' "$file")"
            fi
            credential=$(uri_encode "$credential")
            if [[ $type == tuic ]]; then
                credential="$(jq -r '.uuid' "$file"):$credential"
                query+="&congestion_control=$(uri_encode "$(jq -r '.congestion_control // "cubic"' "$file")")&udp_relay_mode=native"
            fi
            printf '%s://%s@%s:%s?%s#%s\n' "$type" "$credential" "$server" "$port" "$query" "$(uri_encode "$tag")"
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
    local tls_name="" certificate="" tls_key="" password="" obfs_password="" default_server=""
    local quic_bbr_choice="" quic_record=""
    printf '\n添加配置\n  1. VLESS + TCP + REALITY + Vision\n  2. VLESS + WebSocket（无 TLS）\n  3. VLESS TCP（无 TLS）\n  4. Shadowsocks 2022\n  5. 导入单个 inbound JSON 文件\n  6. 导入完整配置 JSON（替换当前配置）\n  7. Hysteria 2\n  8. TUIC\n  9. AnyTLS\n 10. VLESS + gRPC + TLS\n 11. VMess + TCP\n 12. VMess + TCP + TLS\n 13. VMess + WebSocket\n 14. VMess + WebSocket + TLS\n 15. VMess + gRPC + TLS\n 16. VLESS + gRPC + REALITY（不使用 Vision）\n 17. VLESS + WebSocket + TLS\n 18. VLESS + TCP + TLS\n 19. Snell v5（客户端 v4）\n 20. Snell v6\n 21. Snell + ShadowTLS v3（客户端 v4）\n  0. 返回\n'
    prompt choice "请选择" 1 || exit 1
    case "$choice" in
        0) exit 0 ;;
        1) protocol=vless-reality ;;
        2) protocol=vless-ws ;;
        3) protocol=vless-tcp ;;
        4) protocol=shadowsocks ;;
        7) protocol=hysteria2 ;;
        8) protocol=tuic ;;
        9) protocol=anytls ;;
        10) protocol=vless-grpc-tls ;;
        11) protocol=vmess-tcp ;;
        12) protocol=vmess-tcp-tls ;;
        13) protocol=vmess-ws ;;
        14) protocol=vmess-ws-tls ;;
        15) protocol=vmess-grpc-tls ;;
        16) protocol=vless-grpc-reality ;;
        17) protocol=vless-ws-tls ;;
        18) protocol=vless-tcp-tls ;;
        19) protocol=snell ;;
        20) protocol=snell-v6 ;;
        21) snell_shadowtls_wizard_locked; exit $? ;;
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
        vless-*|vmess-*|tuic) credential=$("$CORE_BIN" generate uuid) || exit 1 ;;
        shadowsocks|hysteria2|anytls|snell|snell-v6) credential=$("$CORE_BIN" generate rand --base64 16) || exit 1 ;;
    esac
    [[ $protocol != tuic ]] || password=$("$CORE_BIN" generate rand --base64 16) || exit 1
    if [[ $protocol == snell || $protocol == snell-v6 ]]; then
        info "Snell 需核心 1.14+；当前模板为单 PSK，无需 TLS 证书。v5 不支持 Surge QUIC Proxy Mode，客户端请选 v4。"
        prompt_secret credential "Snell PSK（回车自动生成，输入不回显）" "$credential" || exit 1
        prompt_snell_options extra "$protocol" || exit 1
    fi
    [[ $protocol != hysteria2 ]] || prompt_hysteria2_obfs obfs_password || exit 1
    if [[ $protocol == hysteria2 || $protocol == tuic ]]; then prompt_quic_bbr quic_bbr_choice || exit 1; fi
    case "$protocol" in
        vless-ws|vless-ws-tls|vmess-ws|vmess-ws-tls)
            [[ $protocol == *-tls ]] || warn "此模板未启用 TLS；可配合 TLS 反向代理使用。"
            prompt extra "WebSocket 路径" "/${protocol%%-*}" || exit 1
            ;;
        vless-tcp) warn "此模板没有 TLS 加密；公网直连可选择 REALITY 模板。" ;;
        *-grpc-*) prompt extra "gRPC 服务名（两端须一致）" TunService || exit 1 ;;
    esac
    case "$protocol" in
        vless-reality|vless-grpc-reality)
            prompt tls_name "REALITY 握手域名（服务器可访问的 TLS 1.3 站点，不含 https://）" || exit 1
            valid_host "$tls_name" || { fail "域名格式无效。"; exit 1; }
            if [[ $protocol == vless-reality ]]; then extra=$tls_name; else
                info "gRPC + REALITY 不启用 Vision；握手目标还须支持 HTTP/2（h2）。"
            fi
            pair=$("$CORE_BIN" generate reality-keypair) || exit 1
            private_key=$(printf '%s\n' "$pair" | sed -n 's/^PrivateKey: *//p')
            public_key=$(printf '%s\n' "$pair" | sed -n 's/^PublicKey: *//p')
            [[ $public_key =~ ^[a-zA-Z0-9_-]{43}$ ]] || { fail "无法解析 REALITY 密钥。"; exit 1; }
            short_id=$(openssl rand -hex 8) || exit 1
            ;;
        *-tls|hysteria2|tuic|anytls)
            info "请准备有效的 TLS 证书及私钥；客户端默认校验证书，不跳过验证。"
            info "自签证书或私有 CA 需要在客户端另行配置信任；分享链接不包含证书。"
            prompt tls_name "TLS 证书域名/IP（客户端 SNI）" || exit 1
            prompt_tls_certificate_files "$tls_name" || exit 1
            ;;
    esac
    default_server=$(read_default_server) || exit 1
    if [[ -z $default_server && $protocol == hysteria2 ]]; then default_server=$tls_name; fi
    if [[ -n $default_server ]]; then
        prompt server "客户端连接用的 IP/域名（回车使用默认地址）" "$default_server" || exit 1
    else
        prompt server "客户端连接用的服务器公网 IP/域名（留空则只创建服务端配置）" || exit 1
    fi
    [[ -z $server ]] || valid_host "$server" || { fail "服务器地址格式无效。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/add.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    generate_inbound "$protocol" "$tag" "$port" "$credential" "$extra" "$private_key" "$short_id" \
        "$tls_name" "$certificate" "$tls_key" "$password" "$obfs_password" >"$work/inbound.json" || exit 1
    if [[ -n $quic_bbr_choice ]]; then
        prepare_quic_bbr_wizard "$work/inbound.json" "$quic_bbr_choice" - "$work/quic-record-update.json" >"$work/quic-inbound.json" || exit 1
        mv -- "$work/quic-inbound.json" "$work/inbound.json" || exit 1
        quic_record=$(cat "$work/quic-record-update.json") || exit 1
    fi
    [[ -z $server ]] || generate_client "$work/inbound.json" "$server" "$public_key" >/dev/null || exit 1
    add_managed_inbound "$work/inbound.json" "$server" "$public_key" "$quic_record" || exit 1
    info "已添加 $tag，端口 $((10#$port))。请在服务器防火墙和云安全组放行对应端口。"
    case "$protocol" in
        hysteria2|tuic) info "该模板监听 UDP，请放行 UDP 端口。" ;;
        shadowsocks) info "Shadowsocks 同时使用 TCP 和 UDP，客户端/服务器需保持时钟同步。" ;;
        *) info "该模板监听 TCP。" ;;
    esac
    [[ -z $server ]] || show_client "$tag" "$server"
)

# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
list_inbound_users() (
    umask 077
    local tag=${1:-} work
    [[ $# -le 1 ]] || { fail "用法：sing-box users [配置名称]"; exit 1; }
    require_core && safe_paths || exit 1
    if [[ -z $tag ]]; then select_inbound tag "查看用户" || exit 1; [[ -n $tag ]] || exit 0; fi
    valid_tag "$tag" || { fail "配置名称无效。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/users-view.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    config_jq -e --arg tag "$tag" '[.inbounds[]? | select(.tag==$tag)] | if length==1 then .[0] else error("配置不存在或名称重复") end' >"$work/node.json" || exit 1
    detect_inbound_template "$work/node.json" >/dev/null || exit 1
    case "$(jq -r .type "$work/node.json")" in shadowsocks|snell) fail "此模板仅支持单密码/PSK，请使用修改配置。"; exit 1;; esac
    info "$tag 的用户（不显示连接凭据）："
    jq -r '["用户序号","用户名"], (.users|to_entries[]|[.key+1,(.value.name // "未命名")]) | @tsv' "$work/node.json"
)

# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
change_inbound_user() (
    umask 077
    local action=$1; shift
    local tag=${1:-} work type user_name credential password="" selected_user_index="" count
    local metadata expected_metadata=- server="" public_key="" activation=preserve
    [[ $# -le 1 ]] || { fail "用户操作仅接受一个入站配置名称。"; exit 1; }
    case "$action" in add|delete) ;; *) exit 1;; esac
    require_core && safe_paths || exit 1
    if [[ -z $tag ]]; then select_inbound tag "管理用户" || exit 1; [[ -n $tag ]] || exit 0; fi
    valid_tag "$tag" || { fail "配置名称无效。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/users-change.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    config_jq -e --arg tag "$tag" '[.inbounds[]? | select(.tag==$tag)] | if length==1 then .[0] else error("配置不存在或名称重复") end' >"$work/original.json" || exit 1
    detect_inbound_template "$work/original.json" >/dev/null || exit 1
    type=$(jq -r .type "$work/original.json") || exit 1
    case "$type" in shadowsocks|snell) fail "此模板仅支持单密码/PSK，请使用修改配置。"; exit 1;; esac
    count=$(inbound_user_count "$work/original.json") || exit 1
    metadata="$CLIENT_DIR/$tag.json"
    [[ ! -L $metadata && ( ! -e $metadata || -f $metadata ) ]] || { fail "连接信息文件无效。"; exit 1; }
    if [[ -f $metadata ]]; then
        cp -- "$metadata" "$work/metadata-before.json" || exit 1
        expected_metadata="$work/metadata-before.json"
        jq -e 'type=="object"' "$expected_metadata" >/dev/null || exit 1
        if [[ $(jq -Sc .inbound "$expected_metadata") == "$(jq -Sc . "$work/original.json")" ]]; then
            server=$(jq -r '.server // ""' "$expected_metadata") || exit 1
            public_key=$(jq -r '.public_key // ""' "$expected_metadata") || exit 1
        elif jq -e 'has("quic_bbr_restore")' "$expected_metadata" >/dev/null; then
            fail "节点与恢复记录不一致，已取消用户操作。"; exit 1
        fi
    fi
    if [[ $action == add ]]; then
        prompt user_name "新用户名（字母/数字/短横线/下划线）" "user-$(date +%H%M%S)" || exit 1
        valid_tag "$user_name" || { fail "用户名格式无效。"; exit 1; }
        case "$type" in
            vless|vmess|tuic)
                credential=$("$CORE_BIN" generate uuid) || exit 1
                prompt_secret credential "新用户 UUID（回车自动生成，@new 重新生成）" "$credential" || exit 1
                [[ $credential != @new ]] || credential=$("$CORE_BIN" generate uuid) || exit 1 ;;
            *)
                credential=$("$CORE_BIN" generate rand --base64 16) || exit 1
                prompt_secret credential "新用户密码（回车自动生成，@new 重新生成）" "$credential" || exit 1
                [[ $credential != @new ]] || credential=$("$CORE_BIN" generate rand --base64 16) || exit 1 ;;
        esac
        if [[ $type == tuic ]]; then
            password=$("$CORE_BIN" generate rand --base64 16) || exit 1
            prompt_secret password "新用户 TUIC 密码（回车自动生成，@new 重新生成）" "$password" || exit 1
            [[ $password != @new ]] || password=$("$CORE_BIN" generate rand --base64 16) || exit 1
        fi
        jq --arg name "$user_name" --arg credential "$credential" --arg password "$password" '
          .type as $type | .users += [(.users[0] | .name=$name |
            if $type=="vless" or $type=="vmess" or $type=="tuic" then .uuid=$credential else .password=$credential end
            | if $type=="tuic" then .password=$password else . end)]' "$work/original.json" >"$work/updated.json" || exit 1
        activation=start
        validate_inbound_users "$work/updated.json" || exit 1
        confirm "确认给 $tag 添加用户 $user_name？共享原端口/TLS，原用户保留；服务将启动或短暂重启，失败回滚。" || { info "已取消。"; exit 0; }
    else
        [[ $count -gt 1 ]] || { fail "不能删除最后一个用户；如需移除此入站，请使用删除配置。"; exit 1; }
        select_inbound_user selected_user_index "$work/original.json" || exit 1
        [[ -n $selected_user_index ]] || exit 0
        if [[ -f $expected_metadata ]] && jq -e --argjson i "$((selected_user_index - 1))" --slurpfile metadata "$expected_metadata" \
            '.users[$i].name as $n | any($metadata[0].user_outbound_routes.bindings[]?; .name==$n)' "$work/original.json" >/dev/null; then
            fail "此用户已绑定出站，请先解除单用户绑定再删除。"; exit 1
        fi
        jq --argjson i "$((selected_user_index - 1))" 'del(.users[$i])' "$work/original.json" >"$work/updated.json" || exit 1
        confirm "确认删除 $tag 的第 $selected_user_index 个用户？其凭据将失效，其他用户保留；运行中的服务会短暂重启。" || { info "已取消。"; exit 0; }
    fi
    replace_inbound_file "$tag" "$work/updated.json" "$server" "$public_key" "$work/original.json" "" "$expected_metadata" "$activation" || exit 1
    info "入站用户已更新。可从用户管理菜单按用户导出独立分享链接。"
)

add_inbound_user_locked() { change_inbound_user add "$@"; }
delete_inbound_user_locked() { change_inbound_user delete "$@"; }
add_inbound_user() { with_lock add_inbound_user_locked "$@"; }
delete_inbound_user() { with_lock delete_inbound_user_locked "$@"; }

users_menu() {
    local tag=${1:-} choice selected_tag
    [[ $# -le 1 ]] || { fail "用法：sing-box users [配置名称]"; return 1; }
    [[ -z $tag ]] || valid_tag "$tag" || { fail "配置名称无效。"; return 1; }
    [[ -t 0 ]] || { list_inbound_users "$@"; return; }
    while true; do
        printf '\n入站多用户管理\n  1. 用户列表（不显示凭据）\n  2. 添加用户\n  3. 删除用户\n  4. 按用户导出客户端和分享链接\n  5. 修改入站/所选用户认证\n  0. 返回\n'
        prompt choice "请选择用户操作" 0 || return 0
        case "$choice" in
            0) return 0 ;;
            1) list_inbound_users ${tag:+"$tag"} ;;
            2) add_inbound_user ${tag:+"$tag"} ;;
            3) delete_inbound_user ${tag:+"$tag"} ;;
            4)
                selected_tag=$tag
                if [[ -z $selected_tag ]]; then select_inbound selected_tag "导出用户"; fi
                [[ -z $selected_tag ]] || show_client "$selected_tag"
                ;;
            5) with_lock edit_wizard_locked ${tag:+"$tag"} ;;
            *) fail "请输入 0–5。" ;;
        esac || true
        read -r -p "按回车键返回用户管理菜单..." choice || return 0
    done
}

# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
show_client() (
    umask 077
    local tag=$1 server=${2:-} mode=${3:-full} user_index=${4:-} inbound public_key="" metadata work count index label name
    local -a indices
    case "$mode" in full|json-only|link-only) ;; *) fail "无效的客户端导出方式。"; exit 1;; esac
    require_core || exit 1
    valid_tag "$tag" || { fail "配置名称无效。"; exit 1; }
    check_shadowtls_component "$tag" || exit 1
    if is_snell_shadowtls "$tag"; then
        [[ -z $user_index || $user_index == 1 || $user_index == all ]] || { fail "组合模板仅支持单 PSK。"; exit 1; }
        show_snell_shadowtls_client "$tag" "$server" "$mode"; exit $?
    fi
    work=$(mktemp -d "$STATE_DIR/view.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    config_jq -e --arg tag "$tag" '.inbounds[]? | select(.tag==$tag)' >"$work/inbound.json" || {
        fail "配置不存在：$tag"; exit 1;
    }
    inbound="$work/inbound.json"
    metadata="$CLIENT_DIR/$tag.json"
    [[ ! -L $metadata ]] || { fail "拒绝读取符号链接连接信息。"; exit 1; }
    if [[ -f "$metadata" ]]; then
        # Prevent stale exports after manually importing/changing configuration.
        if [[ $(jq -Sc . "$inbound") == "$(jq -Sc .inbound "$metadata")" ]]; then
            [[ -n $server ]] || server=$(jq -r '.server // ""' "$metadata")
            public_key=$(jq -r '.public_key // ""' "$metadata")
        fi
    fi
    [[ -n $server ]] || server=$(read_default_server) || exit 1
    if [[ -z $server && $(jq -r '.type' "$inbound") == hysteria2 ]]; then
        server=$(jq -r '.tls.server_name // ""' "$inbound") || exit 1
    fi
    if [[ -z $server && $mode != full ]]; then
        fail "缺少当前配置的公网 IP/域名，请使用选项 5 设置默认地址，或选项 3 输入地址导出链接。"
        exit 1
    fi
    [[ -n $server ]] || prompt server "服务器公网 IP 或域名" || exit 1
    if [[ $(jq -r '.tls.reality.enabled // false' "$inbound") == true && -z $public_key ]]; then
        fail "缺少此 REALITY 入站对应的公钥。请使用原始公钥；不会输出不完整的客户端配置。"
        exit 1
    fi
    validate_inbound_users "$inbound" || exit 1
    count=$(inbound_user_count "$inbound") || exit 1
    if [[ $user_index == all ]]; then
        for ((index=1; index<=count; index++)); do indices+=("$index"); done
    else
        if [[ -z $user_index ]]; then
            if [[ $count -gt 1 ]]; then
                [[ $mode == full && -t 0 ]] || { fail "此入站有多个用户，请指定用户序号或导出全部用户。"; exit 1; }
                select_inbound_user user_index "$inbound" || exit 1
                [[ -n $user_index ]] || exit 0
            else user_index=1; fi
        fi
        indices=("$user_index")
    fi
    for index in "${indices[@]}"; do
        generate_client "$inbound" "$server" "$public_key" "$index" >"$work/outbound.json" || exit 1
        label=$tag
        if [[ $count -gt 1 ]]; then
            name=$(jq -r --argjson i "$((index - 1))" '.users[$i].name // ""' "$inbound") || exit 1
            label="$tag:user$index${name:+:$name}"
        fi
        if [[ $mode == json-only ]]; then
            jq --arg tag "$label" '.tag=$tag' "$work/outbound.json" || exit 1
            continue
        fi
        if [[ $mode != link-only ]]; then
            info "$label 客户端 outbound JSON（包含连接凭据）："
            cat "$work/outbound.json"
            if [[ $(jq -r .type "$inbound") == snell ]]; then info "Surge [Proxy] 配置行（非通用分享 URI）："
            else info "分享链接："; fi
        fi
        if [[ $mode == link-only && $(jq -r .type "$inbound") == snell ]]; then
            info "Snell 无通用分享 URI，以下为 Surge [Proxy] 配置行："
        fi
        client_link "$work/outbound.json" "$label" || exit 1
    done
)

snapshot_bundle() (
    umask 077
    safe_paths && require_core || exit 1
    local work file tag server
    work=$(mktemp -d "$STATE_DIR/snapshot.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    printf '{}\n' >"$work/clients.json"
    if [[ -d $CLIENT_DIR ]]; then
        for file in "$CLIENT_DIR"/* "$CLIENT_DIR"/.[!.]* "$CLIENT_DIR"/..?*; do
            [[ -e $file || -L $file ]] || continue
            tag=${file##*/}; tag=${tag%.json}
            if [[ $file != *.json || ! -f $file || -L $file ]] || ! valid_tag "$tag"; then
                fail "连接信息目录包含不支持的文件或符号链接。"; exit 1;
            fi
            jq -e 'type == "object"' "$file" >/dev/null || exit 1
            jq --arg tag "$tag" --slurpfile meta "$file" '.[$tag]=$meta[0]' "$work/clients.json" >"$work/next.json" || exit 1
            mv -- "$work/next.json" "$work/clients.json" || exit 1
        done
    fi
    server=$(read_default_server) || exit 1
    config_jq . >"$work/config.json" || exit 1
    jq -n --slurpfile config "$work/config.json" --rawfile source "$CONFIG_FILE" \
        --slurpfile clients "$work/clients.json" --arg server "$server" \
        '{format:"sing-box-manager-backup-v1",config:$config[0],clients:$clients[0],
          config_source:$source,default_server:(if $server=="" then null else $server end)}'
)

create_backup() (
    umask 077
    safe_paths || exit 1
    install -d -m 700 "$BACKUP_DIR" || exit 1
    local file
    file=$(mktemp "$BACKUP_DIR/bundle-$(date +%Y%m%d-%H%M%S)-XXXXXX.json") || exit 1
    if ! snapshot_bundle >"$file"; then rm -f -- "$file"; exit 1; fi
    printf '%s\n' "$file"
)

validate_bundle() {
    local file=$1 tag server
    [[ -f $file && ! -L $file ]] || { fail "备份必须为普通 JSON 文件，不能是符号链接。"; return 1; }
    jq -e '.format=="sing-box-manager-backup-v1" and (.config|type=="object") and
      (.clients|type=="object") and (.default_server==null or (.default_server|type=="string")) and
      (.config_source==null or (.config_source|type=="string")) and
      all(.clients[]; type=="object") and
      all(.clients|keys[]; test("^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$") and (contains("\n")|not))' "$file" >/dev/null || { fail "备份格式无效。"; return 1; }
    while IFS= read -r tag; do
        valid_tag "$tag" || { fail "备份中的节点名称无效。"; return 1; }
    done < <(jq -r '.clients|keys[]' "$file")
    server=$(jq -r '.default_server // ""' "$file") || return
    [[ -z $server ]] || valid_host "$server" || { fail "备份中的默认地址无效。"; return 1; }
}

apply_bundle() (
    umask 077
    local file=$1 policy=${2:-preserve} activation=${3:-preserve} work backup="" tag server active=0 transaction=0 had_clients=0 start_requested=0
    case "$policy" in preserve|replace) ;; *) fail "未知的配置恢复方式。"; exit 1;; esac
    case "$activation" in preserve|start) ;; *) fail "未知的服务启动策略。"; exit 1;; esac
    require_core && safe_paths && validate_bundle "$file" || exit 1
    work=$(mktemp -d "$STATE_DIR/restore.XXXXXX") || exit 1
    # shellcheck disable=SC2317,SC2329 # Called by the EXIT trap.
    rollback_bundle() {
        local result=$? restored=1
        trap - EXIT INT TERM
        if [[ $transaction -eq 1 ]]; then
            if ! cmp -s "$work/config-before.json" "$CONFIG_FILE"; then
                atomic_copy "$work/config-before.json" "$CONFIG_FILE" || restored=0
                if [[ $active -eq 1 && $restored -eq 1 ]]; then restart_checked || restored=0; fi
            elif [[ $active -eq 1 ]] && ! service_is_active; then restart_checked || restored=0; fi
            if [[ $active -eq 0 && $start_requested -eq 1 ]]; then systemctl stop "$SERVICE" || restored=0; fi
            # Inspect the actual rename result: a signal may arrive before a flag assignment.
            if [[ -d $work/clients-before ]]; then
                rm -rf -- "$CLIENT_DIR" && mv -- "$work/clients-before" "$CLIENT_DIR" || restored=0
            elif [[ $had_clients -eq 0 && ! -d $work/clients ]]; then
                rm -rf -- "$CLIENT_DIR" || restored=0
            fi
            if [[ -f $work/default-before ]]; then atomic_copy "$work/default-before" "$DEFAULT_SERVER_FILE" || restored=0
            else rm -f -- "$DEFAULT_SERVER_FILE" || restored=0; fi
            if [[ $restored -eq 1 ]]; then warn "操作未完成，已恢复原配置和连接信息。"
            else warn "恢复未完全成功，请使用备份：$backup；临时文件：$work"; fi
            result=1
        fi
        [[ $restored -eq 0 ]] || rm -rf -- "$work"
        exit "$result"
    }
    trap rollback_bundle EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    cp -- "$file" "$work/bundle.json" || exit 1
    validate_bundle "$work/bundle.json" || exit 1
    jq .config "$work/bundle.json" >"$work/config.json" || exit 1
    if [[ $policy == replace ]] && jq -e '.config_source|type=="string"' "$work/bundle.json" >/dev/null; then
        jq -j .config_source "$work/bundle.json" >"$work/source.json" || exit 1
        if [[ $(json_query "$work/source.json" -Sc .) != "$(jq -Sc . "$work/config.json")" ]]; then
            fail "备份的注释原文与配置数据不一致，已取消恢复。"; exit 1
        fi
        cp -- "$work/source.json" "$work/config.json" || exit 1
    fi
    validate_config "$work/config.json" || exit 1
    mkdir -m 700 "$work/clients" || exit 1
    while IFS= read -r tag; do
        jq --arg tag "$tag" '.clients[$tag]' "$work/bundle.json" >"$work/clients/$tag.json" || exit 1
    done < <(jq -r '.clients|keys[]' "$work/bundle.json")
    server=$(jq -r '.default_server // ""' "$work/bundle.json") || exit 1
    printf '%s\n' "$server" >"$work/default-server"
    backup=$(create_backup) || exit 1
    cp -- "$CONFIG_FILE" "$work/config-before.json" || exit 1
    [[ ! -e $DEFAULT_SERVER_FILE ]] || cp -- "$DEFAULT_SERVER_FILE" "$work/default-before" || exit 1
    [[ ! -d $CLIENT_DIR ]] || had_clients=1
    service_is_active && active=1
    transaction=1
    if [[ $had_clients -eq 1 ]]; then mv -- "$CLIENT_DIR" "$work/clients-before" || exit 1; fi
    mv -- "$work/clients" "$CLIENT_DIR" || exit 1
    if [[ -n $server ]]; then atomic_copy "$work/default-server" "$DEFAULT_SERVER_FILE" || exit 1
    else rm -f -- "$DEFAULT_SERVER_FILE" || exit 1; fi
    if { [[ $policy == replace ]] && ! cmp -s "$CONFIG_FILE" "$work/config.json"; } ||
       [[ $(config_jq -Sc .) != "$(json_query "$work/config.json" -Sc .)" ]]; then
        [[ $activation != start || $active -ne 0 ]] || start_requested=1
        commit_config "$work/config.json" "$policy" "$activation" || exit 1
    fi
    transaction=0
    info "配置及连接信息已保存。操作前备份：$backup"
)

restore_backup() {
    [[ $# -eq 1 ]] || { fail "用法：sing-box restore /path/bundle.json"; return 1; }
    validate_bundle "$1" || return
    info "恢复配置、全部连接信息和默认地址；证书文件须已在原路径，运行中的服务可能短暂重启。"
    confirm "使用此备份替换当前配置？（恢复前自动备份）" || return
    apply_bundle "$1" replace
}

# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
delete_inbound() (
    local tag=${1:-} work
    require_core && safe_paths || exit 1
    if [[ -z $tag ]]; then
        select_inbound tag "删除" || exit 1
        [[ -n $tag ]] || exit 0
    fi
    valid_tag "$tag" || { fail "配置名称无效。"; exit 1; }
    check_shadowtls_component "$tag" || exit 1
    if is_snell_shadowtls "$tag"; then delete_snell_shadowtls_locked "$tag"; exit $?; fi
    config_jq -e --arg tag "$tag" '[.inbounds[]? | select(.tag==$tag)] | length==1' >/dev/null || {
        fail "配置不存在或名称重复。"; exit 1;
    }
    if config_jq -e --arg tag "$tag" 'del(.inbounds[] | select(.tag==$tag)) | any(..|strings; .==$tag)' >/dev/null; then
        fail "节点被路由或其他配置引用，请先通过完整 JSON 导入调整引用。"; exit 1
    fi
    confirm "删除节点 $tag 及其连接信息？（自动备份，其他节点保留）" || exit 1
    umask 077
    work=$(mktemp -d "$STATE_DIR/delete.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before.json" || exit 1
    jq --arg tag "$tag" '.config.inbounds |= map(select(.tag!=$tag)) | del(.clients[$tag])' \
        "$work/before.json" >"$work/after.json" || exit 1
    apply_bundle "$work/after.json" || exit 1
    info "已删除 $tag。客户端中的旧节点需要自行移除。"
)

# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
add_managed_inbound() (
    umask 077
    local file=$1 server=${2:-} public_key=${3:-} quic_record=${4:-} work tag port
    require_core && safe_paths || exit 1
    tag=$(jq -er .tag "$file") && port=$(jq -er .listen_port "$file") || exit 1
    valid_tag "$tag" && valid_port "$port" || exit 1
    [[ -z $server ]] || valid_host "$server" || exit 1
    check_inbound_conflicts "$file" || exit 1
    work=$(mktemp -d "$STATE_DIR/add-save.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    if [[ -n $quic_record ]]; then
        printf '%s\n' "$quic_record" >"$work/quic-record.json" || exit 1
        generate_quic_bbr_restore "$file" "$work/quic-record.json" >/dev/null || exit 1
    fi
    snapshot_bundle >"$work/before.json" || exit 1
    jq --arg tag "$tag" --arg server "$server" --arg key "$public_key" --slurpfile node "$file" --arg record "$quic_record" \
        '.config.inbounds += [$node[0]] | .clients[$tag]={server:$server,public_key:$key,inbound:$node[0]}
         | if $record!="" then .clients[$tag].quic_bbr_restore=($record|fromjson) else . end' \
        "$work/before.json" >"$work/after.json" || exit 1
    apply_bundle "$work/after.json" preserve start
)

list_backups() {
    local file
    info "成套备份（配置、连接信息、默认地址；不包含证书/私钥文件、核心或服务文件）："
    for file in "$BACKUP_DIR"/bundle-*.json; do
        [[ -f $file && ! -L $file ]] || continue
        printf '%s\n' "$file"
    done
}

export_clients() (
    umask 077
    local format=${1:-} target=${2:-} work tag tmp=""
    require_core && safe_paths || exit 1
    case "$format" in links|base64|json) ;; *) fail "格式须为 links、base64 或 json。"; exit 1;; esac
    if [[ $format != json ]] && config_jq -e 'any(.inbounds[]?; .type=="snell")' >/dev/null; then
        fail "Snell 无统一分享 URI，批量 links/base64 不混入 Surge 配置行；请用 json 导出或单独查看 Snell 客户端配置。"; exit 1
    fi
    [[ $target == /* && ! -e $target && ! -L $target && -d $(dirname -- "$target") ]] || {
        fail "请提供尚不存在的绝对文件路径；不会覆盖已有文件。"; exit 1;
    }
    work=$(mktemp -d "$STATE_DIR/export.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"; [[ -z $tmp ]] || rm -f -- "$tmp"' EXIT
    : >"$work/nodes"
    config_jq -e '.inbounds|length>0' >/dev/null || { fail "没有可导出的节点。"; exit 1; }
    while IFS= read -r tag; do
        if [[ $format == json ]]; then
            show_client "$tag" "" json-only all >>"$work/nodes" || exit 1
        else show_client "$tag" "" link-only all >>"$work/nodes" || exit 1; fi
    done < <(logical_inbounds | jq -r '.[].tag')
    case "$format" in
        links) cp -- "$work/nodes" "$work/result" ;;
        base64) base64 -w 0 "$work/nodes" >"$work/result" ;;
        json) jq -s '{outbounds:.}' "$work/nodes" >"$work/result" ;;
    esac || exit 1
    tmp=$(mktemp "${target}.tmp.XXXXXX") || exit 1
    install -m 600 "$work/result" "$tmp" && ln -- "$tmp" "$target" || exit 1
    info "已导出至 $target（含连接凭据，权限 600）。JSON 是 outbound 集合，需合并到客户端配置。"
)

diagnose() (
    local work node tag type port proto cert key name days issue=0
    require_core && safe_paths || exit 1
    umask 077
    work=$(mktemp -d "$STATE_DIR/diagnose.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    printf '面板版本：%s\n' "$SB_MANAGER_VERSION"
    show_status
    if validate_config "$CONFIG_FILE" >"$work/check.log" 2>&1; then info "核心配置校验通过。"
    else warn "核心配置校验失败；详情请在本机运行 sing-box validate（错误可能含敏感字段）。"; issue=1; fi
    if ! service_is_active; then warn "服务未运行。"; issue=1; fi
    while IFS= read -r node; do
        printf '%s\n' "$node" >"$work/inbound.json"
        tag=$(jq -r '.tag' "$work/inbound.json"); type=$(jq -r '.type' "$work/inbound.json")
        port=$(jq -r '.listen_port // empty' "$work/inbound.json")
        printf '\n节点：%s  协议：%s  端口：%s\n' "$tag" "$type" "${port:-无}"
        case "$type" in hysteria2|tuic) proto=u;; shadowsocks) proto=tu;; vless|vmess|anytls) proto=t;; *) proto="";; esac
        if [[ -n $proto ]] && valid_port "$port"; then
            for name in t u; do
                [[ $proto == *"$name"* ]] || continue
                if ss -H -ln"$name" "sport = :$port" >"$work/listen" 2>/dev/null && [[ -s $work/listen ]]; then
                    info "$([[ $name == t ]] && printf TCP || printf UDP) $port 存在本机监听（不等于公网可达）。"
                else warn "缺少 $([[ $name == t ]] && printf TCP || printf UDP) $port 本机监听。"; issue=1; fi
            done
        fi
        if jq -e '.tls.enabled==true and .tls.reality.enabled!=true' "$work/inbound.json" >/dev/null; then
            cert=$(jq -r '.tls.certificate_path // ""' "$work/inbound.json")
            key=$(jq -r '.tls.key_path // ""' "$work/inbound.json")
            name=$(jq -r '.tls.server_name // ""' "$work/inbound.json")
            if validate_tls_files "$name" "$cert" "$key" >"$work/tls.log" 2>&1; then
                info "TLS 域名、有效期、证书与私钥配对检查通过。"
                days=$(openssl x509 -in "$cert" -noout -enddate 2>/dev/null)
                printf '%s\n' "$days"
                if ! openssl x509 -in "$cert" -noout -checkend 1209600 >/dev/null 2>&1; then warn "证书将在 14 天内到期，请检查续期任务。"; issue=1; fi
                if ! openssl verify -purpose sslserver -untrusted "$cert" "$cert" >/dev/null 2>&1; then
                    warn "系统 CA 无法验证证书链：可能缺少中间证书，或使用自签/私有 CA；请使用 fullchain 并检查客户端信任。"; issue=1
                fi
            else warn "TLS 文件、有效期、域名或密钥配对检查失败；请检查节点证书设置。"; issue=1; fi
        fi
        if show_client "$tag" "" link-only all >"$work/client" 2>"$work/client-error"; then info "客户端链接生成检查通过（不显示凭据）。"
        else warn "客户端链接不能生成，请检查连接地址、公钥元数据或模板支持范围。"; issue=1; fi
    done < <(logical_inbounds | jq -c '.[]')
    info "此检查不探测公网，不修改防火墙；云平台入站规则、DNS 解析及实际客户端连接需分别验证。"
    exit "$issue"
)

management_menu() {
    local choice file format=""
    while true; do
        printf '\n配置维护\n  1. 创建成套备份\n  2. 查看成套备份\n  3. 恢复成套备份\n  4. 批量导出客户端\n  5. 只读诊断\n  0. 返回\n'
        prompt choice "请选择" 1 || return 0
        case "$choice" in
            0) return 0;;
            1) with_lock create_backup;;
            2) list_backups;;
            3) list_backups; prompt file "成套备份 JSON 绝对路径" && with_lock restore_backup "$file";;
            4) prompt format "导出格式（links/base64/json）" links && prompt file "输出文件绝对路径（不能已存在）" && with_lock export_clients "$format" "$file";;
            5) with_lock diagnose;;
            *) fail "无效选项。";;
        esac || true
        read -r -p "按回车键返回配置维护菜单..." choice || return 0
    done
}

show_config_list() {
    local tag
    logical_inbounds | jq -r '["序号","名称","协议","监听地址","端口"],
        (to_entries[] | [.key+1,.value.tag,(.value._manager_type // .value.type),(.value.listen // "-"),(.value.listen_port // "-")])
        | map(if type=="string" then gsub("[[:cntrl:]]"; "?") else . end) | @tsv' || return
    if ! config_jq -e '.inbounds | length > 0' >/dev/null; then
        info "暂无入站配置。"
        return 0
    fi
    info "以下分享链接包含连接凭据，可复制整行导入客户端："
    while IFS= read -r tag; do
        printf '\n[%s]\n' "$(jq -rn --arg tag "$tag" '$tag | gsub("[[:cntrl:]]"; "?")')"
        show_client "$tag" "" link-only all || warn "此配置暂时无法生成分享链接，其他配置仍会继续显示。"
    done < <(logical_inbounds | jq -r '.[].tag')
}

show_config() {
    require_core || return
    local choice selected_tag=""
    while true; do
        printf '\n查看配置\n  1. 配置列表和完整分享链接\n  2. 完整服务端 JSON（包含密码/私钥）\n  3. 客户端 outbound JSON 和分享链接\n  4. 检查当前配置\n  5. 设置默认公网 IP/域名\n  6. 修改配置\n  7. 删除配置\n  8. 备份、恢复、批量导出和诊断\n  9. 格式化配置文件（保留注释）\n  0. 返回\n'
        prompt choice "请选择" 1 || return 0
        case "$choice" in
            0) return 0 ;;
            1) show_config_list ;;
            2) config_jq . ;;
            3) select_inbound selected_tag "导出客户端" && { [[ -z $selected_tag ]] || show_client "$selected_tag"; } ;;
            4) validate_config "$CONFIG_FILE" ;;
            5) with_lock set_default_server ;;
            6) with_lock edit_wizard_locked ;;
            7) with_lock delete_inbound ;;
            8) management_menu || true; continue ;;
            9) format_config ;;
            *) fail "无效选项。" ;;
        esac || true
        read -r -p "按回车键返回查看配置菜单..." choice || return 0
    done
}

generate_quic_bbr_inbound() {
    jq -e '
      if .type == "hysteria2" then del(.up_mbps,.down_mbps) | .ignore_client_bandwidth=true
      elif .type == "tuic" then .congestion_control="bbr"
      else error("QUIC BBR only supports Hysteria 2 and TUIC") end' "$1" || {
        fail "UDP/QUIC BBR 仅支持 Hysteria 2 和 TUIC 节点。"; return 1;
    }
}

quic_bbr_settings() {
    jq -e 'if .type=="hysteria2" then with_entries(select(.key=="up_mbps" or .key=="down_mbps" or .key=="ignore_client_bandwidth"))
      elif .type=="tuic" then with_entries(select(.key=="congestion_control"))
      else error("unsupported QUIC protocol") end' "$1"
}

validate_quic_bbr_record() {
    jq -se --arg type "$2" '
      length==1 and (.[0] | type=="object" and
        (keys==["format","settings","type"]) and .format=="sing-box-manager-quic-bbr-v1" and .type==$type and
        (.settings | type=="object" and
          if $type=="hysteria2" then
            (keys-["up_mbps","down_mbps","ignore_client_bandwidth"]|length==0) and
            all(to_entries[]; if .key=="ignore_client_bandwidth" then (.value==null or (.value|type)=="boolean")
                else (.value==null or (.value|type)=="number" and (.value|isfinite) and .value>=0) end)
          elif $type=="tuic" then (keys-["congestion_control"]|length==0) and
            all(.[]; .==null or .=="" or .=="cubic" or .=="new_reno" or .=="bbr")
          else false end))' "$1" >/dev/null 2>&1 || {
        fail "QUIC BBR 恢复记录无效；不会猜测开启前的设置。"; return 1;
    }
}

make_quic_bbr_record() {
    local settings
    settings=$(quic_bbr_settings "$1") || return 1
    jq -n --arg type "$(jq -r .type "$1")" --argjson settings "$settings" \
        '{format:"sing-box-manager-quic-bbr-v1",type:$type,settings:$settings}'
}

generate_quic_bbr_restore() {
    local current target
    validate_quic_bbr_record "$2" "$(jq -r .type "$1")" || return 1
    current=$(quic_bbr_settings "$1" | jq -Sc .) || return 1
    target=$(generate_quic_bbr_inbound "$1" | quic_bbr_settings /dev/stdin | jq -Sc .) || return 1
    [[ $current == "$target" ]] || { fail "当前 QUIC 拥塞控制已被其他操作修改，拒绝覆盖。"; return 1; }
    jq --slurpfile record "$2" '
      (if .type=="hysteria2" then del(.up_mbps,.down_mbps,.ignore_client_bandwidth)
       else del(.congestion_control) end) + $record[0].settings' "$1"
}

# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
show_quic_bbr_status() {
    [[ $# -eq 0 ]] || { fail "用法：sing-box quic-bbr-status"; return 1; }
    require_core || return
    printf '\nUDP/QUIC 拥塞控制（独立于系统 TCP BBR）\n序号\t名称\t协议\t状态\n'
    config_jq -r '
      def clean: tostring | gsub("[\u0000-\u001f\u007f]"; "?");
      (.inbounds // []) | to_entries[] | select(.value.type == "hysteria2" or .value.type == "tuic")
      | .key as $index | .value
      | [($index+1|tostring),(.tag|clean),.type,
         (if .type == "tuic" then (.congestion_control // "cubic" | clean)
          elif (.up_mbps // 0) != 0 or (.down_mbps // 0) != 0 then "带宽限制/Brutal（未强制 BBR）"
          elif .ignore_client_bandwidth == true then "BBR（强制）"
          else "自动协商（客户端未设带宽时使用 BBR）" end)] | @tsv'
}

# shellcheck disable=SC2016 # config_jq filters use literal jq variables.
configure_quic_bbr_change() (
    umask 077
    local action=$1; shift
    local tag=${1:-} bundle=${2:-} work metadata server="" public_key="" backup record="" expected_meta=- had_record=0
    if [[ $action == record ]]; then
        [[ $# -eq 2 ]] || { fail "用法：sing-box quic-bbr-record 配置名称 开启前成套备份"; exit 1; }
    else [[ $# -le 1 ]] || { fail "QUIC BBR 操作仅接受一个配置名称。"; exit 1; }; fi
    require_core && safe_paths || exit 1
    if [[ -z $tag ]]; then
        if [[ $action == disable ]]; then select_inbound tag "关闭 UDP/QUIC BBR" || exit 1
        else select_inbound tag "开启 UDP/QUIC BBR" || exit 1; fi
        [[ -n $tag ]] || exit 0
    fi
    valid_tag "$tag" || { fail "配置名称无效。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/quic-bbr.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    config_jq -e --arg tag "$tag" '[.inbounds[]? | select(.tag==$tag)] | if length==1 then .[0] else error("配置不存在或名称重复") end' >"$work/original.json" || exit 1
    generate_quic_bbr_inbound "$work/original.json" >"$work/updated.json" || exit 1
    detect_inbound_template "$work/original.json" >/dev/null || exit 1
    metadata="$CLIENT_DIR/$tag.json"
    [[ ! -L $metadata && ( ! -e $metadata || -f $metadata ) ]] || { fail "连接信息文件无效。"; exit 1; }
    if [[ -f $metadata ]]; then
        cp -- "$metadata" "$work/metadata-before.json" || exit 1
        expected_meta="$work/metadata-before.json"
        jq -e 'type=="object"' "$expected_meta" >/dev/null || exit 1
        if jq -e 'has("quic_bbr_restore")' "$expected_meta" >/dev/null; then had_record=1; fi
        if [[ $(jq -Sc .inbound "$expected_meta") == "$(jq -Sc . "$work/original.json")" ]]; then
            server=$(jq -r '.server // ""' "$expected_meta") || exit 1
            public_key=$(jq -r '.public_key // ""' "$expected_meta") || exit 1
        elif [[ $had_record -eq 1 || $action != enable ]]; then
            fail "节点与连接信息不一致，拒绝使用或覆盖恢复记录。"; exit 1
        fi
        [[ $had_record -eq 0 ]] || jq '.quic_bbr_restore' "$expected_meta" >"$work/record.json" || exit 1
    fi
    case "$action" in
        enable)
            if [[ $had_record -eq 1 ]]; then generate_quic_bbr_restore "$work/original.json" "$work/record.json" >/dev/null || exit 1; fi
            if [[ $(jq -Sc . "$work/original.json") == "$(jq -Sc . "$work/updated.json")" ]]; then
                info "$tag 的 UDP/QUIC BBR 已启用，无需修改。"; exit 0
            fi
            make_quic_bbr_record "$work/original.json" >"$work/record.json" || exit 1
            record=$(jq -c . "$work/record.json") || exit 1
            info "将为 $tag 开启 QUIC BBR，并记录原带宽/拥塞控制字段；系统 TCP 参数保持不变。"
            info "Hy2 会移除 up_mbps/down_mbps 并要求 BBR；TUIC 会设置 congestion_control=bbr。"
            ;;
        disable)
            [[ $had_record -eq 1 ]] || { fail "没有该节点开启前的恢复记录；请核对原备份，不会猜测原值。"; exit 1; }
            generate_quic_bbr_restore "$work/original.json" "$work/record.json" >"$work/updated.json" || exit 1
            record=null
            info "将恢复 $tag 开启 QUIC BBR 前的带宽/拥塞控制字段，不改动其他节点或认证信息。"
            if jq -e 'if .type=="tuic" then .congestion_control=="bbr" else
              (.up_mbps // 0)==0 and (.down_mbps // 0)==0 and .ignore_client_bandwidth==true end' "$work/updated.json" >/dev/null; then
                warn "开启前已经使用 BBR，恢复后仍为 BBR；本次撤销的是面板开启操作。"
            fi
            ;;
        record)
            [[ -f $bundle && ! -L $bundle ]] && validate_bundle "$bundle" || exit 1
            jq -e --arg tag "$tag" '[.config.inbounds[]? | select(.tag==$tag)] | if length==1 then .[0] else error("备份节点不存在或名称重复") end' "$bundle" >"$work/before.json" || exit 1
            generate_quic_bbr_inbound "$work/before.json" >"$work/enabled.json" || exit 1
            [[ $(jq -Sc . "$work/before.json") != "$(jq -Sc . "$work/enabled.json")" ]] || {
                fail "指定备份已是开启后的 BBR 目标设置，不能作为开启前的依据。"; exit 1;
            }
            [[ $(jq -Sc . "$work/enabled.json") == "$(jq -Sc . "$work/original.json")" ]] || {
                fail "指定备份与当前节点不匹配，拒绝补造恢复记录。"; exit 1;
            }
            make_quic_bbr_record "$work/before.json" >"$work/import-record.json" || exit 1
            if [[ $had_record -eq 1 ]]; then
                [[ $(jq -Sc . "$work/record.json") == "$(jq -Sc . "$work/import-record.json")" ]] || {
                    fail "已有恢复记录，不会覆盖首次记录。"; exit 1;
                }
                info "恢复记录已存在，无需修改。"; exit 0
            fi
            record=$(jq -c . "$work/import-record.json") || exit 1
            cp -- "$work/original.json" "$work/updated.json" || exit 1
            info "将按指定成套备份为 $tag 补齐恢复记录，不更改节点配置或重启服务。"
            ;;
        *) exit 1 ;;
    esac
    if [[ $action == record ]]; then confirm "确认登记此备份中的原设置？（先备份，失败回滚）" || { info "已取消。"; exit 0; }
    elif [[ $action == disable ]]; then confirm "确认关闭并恢复 QUIC BBR 开启前的设置？先备份；运行中的 sing-box 会短暂重启，失败回滚。" || { info "已取消。"; exit 0; }
    else confirm "确认开启 QUIC BBR？先备份；运行中的 sing-box 会短暂重启，失败回滚。" || { info "已取消。"; exit 0; }; fi
    backup=$(create_backup) || exit 1
    replace_inbound_file "$tag" "$work/updated.json" "$server" "$public_key" "$work/original.json" "$record" "$expected_meta" || exit 1
    info "UDP/QUIC BBR 操作已完成。成套备份：$backup（可从主菜单 13 恢复）。"
)

configure_quic_bbr_locked() { configure_quic_bbr_change enable "$@"; }
disable_quic_bbr_locked() { configure_quic_bbr_change disable "$@"; }
record_quic_bbr_locked() { configure_quic_bbr_change record "$@"; }

enable_quic_bbr() {
    [[ $# -le 1 ]] || { fail "用法：sing-box quic-bbr [配置名称]"; return 1; }
    with_lock configure_quic_bbr_locked "$@"
}

disable_quic_bbr() {
    [[ $# -le 1 ]] || { fail "用法：sing-box quic-bbr-disable [配置名称]"; return 1; }
    with_lock disable_quic_bbr_locked "$@"
}

record_quic_bbr() {
    [[ $# -eq 2 ]] || { fail "用法：sing-box quic-bbr-record 配置名称 开启前成套备份"; return 1; }
    with_lock record_quic_bbr_locked "$@"
}

bbr_menu() {
    local choice
    while true; do
        printf '\nBBR 设置\n  1. 开启系统 TCP BBR\n  2. 开启 UDP/QUIC BBR（选择 Hy2/TUIC 节点）\n  3. 查看 UDP/QUIC 拥塞控制状态\n  4. 关闭 UDP/QUIC BBR（恢复开启前设置）\n  0. 返回\n'
        prompt choice "请选择 BBR 操作" 0 || return 0
        case "$choice" in
            1) enable_bbr ;;
            2) enable_quic_bbr ;;
            3) show_quic_bbr_status ;;
            4) disable_quic_bbr ;;
            0) return 0 ;;
            *) fail "请输入 0–4。" ;;
        esac || true
        read -r -p "按回车键返回 BBR 菜单..." choice || return 0
    done
}

show_bbr_status() {
    [[ $# -eq 0 ]] || { fail "用法：sing-box bbr-status"; return 1; }
    have sysctl || { fail "缺少 sysctl，请重新运行 setup 安装依赖。"; return 1; }
    local cc qdisc available
    if ! cc=$(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null) ||
       ! qdisc=$(sysctl -n net.core.default_qdisc 2>/dev/null) ||
       ! available=$(sysctl -n net.ipv4.tcp_available_congestion_control 2>/dev/null); then
        fail "无法读取 TCP BBR 相关内核参数；当前内核或容器可能不支持。"; return 1
    fi
    printf '\n内核：%s\nTCP 拥塞控制：%s\n默认队列规则：%s\n已注册的 TCP 算法：%s\n' \
        "$(uname -r)" "$cc" "$qdisc" "$available"
}

check_bbr_persistence() {
    # Simulate sysctl.d precedence with our future file inserted; never edit foreign files.
    python3 - "$SB_ROOT_PREFIX" "$BBR_SYSCTL_FILE" <<'PY_BBR_PERSISTENCE'
from pathlib import Path
import re
import sys

prefix, target = sys.argv[1], Path(sys.argv[2])
desired = {"net.core.default_qdisc": "fq", "net.ipv4.tcp_congestion_control": "bbr"}
paths = {key.replace(".", "/"): key for key in desired}
chosen, effective = {}, {}
try:
    for directory in ("/etc/sysctl.d", "/run/sysctl.d", "/usr/local/lib/sysctl.d", "/usr/lib/sysctl.d", "/lib/sysctl.d"):
        for path in (Path(prefix + directory)).glob("*.conf"):
            if path.is_file() or path.is_symlink():
                chosen.setdefault(path.name, path)
    chosen[target.name] = None
    def read(path):
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith(("#", ";")) or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            key = key[1:] if key.startswith("-") else key
            dot, slash = key.find("."), key.find("/")
            if slash < 0 or (dot >= 0 and dot < slash):
                key = key.translate(str.maketrans({".": "/", "/": "."}))
            if key in paths:
                effective[paths[key]] = (value, path)
    for name in sorted(chosen):
        path = chosen[name]
        if path is None:
            effective.update({key: (value, target) for key, value in desired.items()})
        elif path.is_file():
            read(path)
    conflicts = [(key, path) for key, (value, path) in effective.items() if value != desired[key]]
    # procps sysctl --system also reads this file last; check it conservatively.
    global_conf = Path(prefix + "/etc/sysctl.conf")
    if global_conf.is_file() and global_conf.resolve() != target.resolve():
        read(global_conf)
    conflicts.extend((key, path) for key, (value, path) in effective.items() if value != desired[key])
    for key, path in sorted(set(conflicts), key=lambda item: (item[0], str(item[1]))):
        print("[错误] 后续 sysctl 配置可能覆盖 " + key + "：" + str(path) + "；请先核对冲突。", file=sys.stderr)
    sys.exit(1 if conflicts else 0)
except (OSError, UnicodeError):
    print("[错误] 无法完整读取现有 sysctl 配置，已取消 BBR 设置。", file=sys.stderr)
    sys.exit(1)
PY_BBR_PERSISTENCE
}

bbr_owned_config() {
    [[ -f $1 && ! -L $1 && $(head -n 1 "$1") == '# Managed by sing-box-manager: TCP BBR' ]] &&
        ! grep -Ev '^[[:space:]]*($|[#;]|-?net[./](core[./]default_qdisc|ipv4[./]tcp_congestion_control)[[:space:]]*=)' "$1" >/dev/null
}

bbr_restore_backup() {
    local name backup
    [[ -f $BBR_RESTORE_FILE && ! -L $BBR_RESTORE_FILE ]] || {
        fail "没有本面板开启 BBR 前的恢复记录；不会猜测原算法或覆盖外部设置。"; return 1;
    }
    name=$(cat "$BBR_RESTORE_FILE") || return 1
    [[ $name =~ ^bbr-[0-9]{8}-[0-9]{6}-[a-zA-Z0-9]{6}$ ]] || {
        fail "BBR 恢复记录格式无效。"; return 1;
    }
    backup="$BACKUP_DIR/$name"
    if [[ ! -d $backup || -L $backup || ! -f $backup/runtime.json || -L $backup/runtime.json ]]; then
        fail "BBR 开启前备份缺失或无效，已取消恢复。"; return 1
    fi
    if ! jq -se 'length == 1 and (.[0] | type == "object" and
            (.tcp_congestion_control | type == "string" and test("^[a-zA-Z0-9_-]+$")) and
            (.default_qdisc | type == "string" and test("^[a-zA-Z0-9_-]+$")) and
            (.config_existed | type == "boolean") and
            (.config_mode | type == "string" and test("^[0-7]{3,4}$")))' "$backup/runtime.json" >/dev/null 2>&1; then
        fail "BBR 开启前备份缺失或无效，已取消恢复。"; return 1
    fi
    if [[ $(jq -r '.config_existed' "$backup/runtime.json") == true ]]; then
        bbr_owned_config "$backup/sysctl.conf" || {
            fail "BBR 开启前的配置文件备份缺失或无效。"; return 1;
        }
    fi
    printf '%s\n' "$backup"
}

configure_bbr_locked() (
    umask 077
    local old_cc old_qdisc available work backup="" had_file=0 old_mode=644 transaction=0
    local action=${1:-enable} target_cc=bbr target_qdisc=fq target_file=1 target_mode=644
    local restore_backup="" had_restore=0
    [[ $action == enable || $action == disable ]] || exit 1
    safe_paths || exit 1
    [[ ! -L $BBR_SYSCTL_DIR && ! -L $BBR_SYSCTL_FILE &&
       ! -L $BBR_RESTORE_FILE && ( ! -e $BBR_RESTORE_FILE || -f $BBR_RESTORE_FILE ) &&
       ( ! -e $BBR_SYSCTL_DIR || -d $BBR_SYSCTL_DIR ) &&
       ( ! -e $BBR_SYSCTL_FILE || -f $BBR_SYSCTL_FILE ) ]] || {
        fail "BBR 配置目录或文件无效，拒绝操作符号链接。"; exit 1;
    }
    show_bbr_status || exit 1
    old_cc=$(sysctl -n net.ipv4.tcp_congestion_control) &&
        old_qdisc=$(sysctl -n net.core.default_qdisc) &&
        available=$(sysctl -n net.ipv4.tcp_available_congestion_control) || exit 1
    [[ $old_cc =~ ^[a-zA-Z0-9_-]+$ && $old_qdisc =~ ^[a-zA-Z0-9_-]+$ ]] || {
        fail "当前内核参数格式异常，已取消。"; exit 1;
    }
    if [[ -f $BBR_SYSCTL_FILE ]]; then
        had_file=1
        bbr_owned_config "$BBR_SYSCTL_FILE" || {
            fail "BBR 文件不属于本项目或含有其他设置，不会覆盖，请先核对。"; exit 1;
        }
        old_mode=$(stat -c '%a' "$BBR_SYSCTL_FILE") || exit 1
    fi
    if [[ -f $BBR_RESTORE_FILE ]]; then
        had_restore=1
        restore_backup=$(bbr_restore_backup) || exit 1
    fi
    if [[ $action == disable ]]; then
        [[ $had_restore -eq 1 ]] || { bbr_restore_backup >/dev/null; exit 1; }
    else
        check_bbr_persistence || exit 1
    fi
    work=$(mktemp -d "$STATE_DIR/bbr-enable.XXXXXX") || exit 1
    # shellcheck disable=SC2317,SC2329 # Invoked by EXIT/INT/TERM traps below.
    rollback_bbr() {
        local result=$? restored=1
        trap - EXIT INT TERM
        if [[ $transaction -eq 1 ]]; then
            if [[ $(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null) != "$old_cc" ]]; then
                sysctl -w "net.ipv4.tcp_congestion_control=$old_cc" >/dev/null 2>&1 || true
            fi
            if [[ $(sysctl -n net.core.default_qdisc 2>/dev/null) != "$old_qdisc" ]]; then
                sysctl -w "net.core.default_qdisc=$old_qdisc" >/dev/null 2>&1 || true
            fi
            if [[ $had_file -eq 1 ]]; then
                if ! cmp -s "$backup/sysctl.conf" "$BBR_SYSCTL_FILE" ||
                   [[ $(stat -c '%a' "$BBR_SYSCTL_FILE" 2>/dev/null) != "$old_mode" ]]; then
                    atomic_copy "$backup/sysctl.conf" "$BBR_SYSCTL_FILE" "$old_mode" || true
                fi
                cmp -s "$backup/sysctl.conf" "$BBR_SYSCTL_FILE" || restored=0
                [[ $(stat -c '%a' "$BBR_SYSCTL_FILE" 2>/dev/null) == "$old_mode" ]] || restored=0
            else
                rm -f -- "$BBR_SYSCTL_FILE" || restored=0
            fi
            if [[ $had_restore -eq 1 ]]; then
                atomic_copy "$backup/active-before" "$BBR_RESTORE_FILE" 600 || restored=0
                cmp -s "$backup/active-before" "$BBR_RESTORE_FILE" || restored=0
            else
                rm -f -- "$BBR_RESTORE_FILE" || restored=0
            fi
            [[ $(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null) == "$old_cc" &&
               $(sysctl -n net.core.default_qdisc 2>/dev/null) == "$old_qdisc" ]] || restored=0
            if [[ $restored -eq 1 ]]; then warn "BBR 设置未完成，已恢复原参数及配置文件。"
            else warn "BBR 自动恢复未完全成功，请核对备份：$backup"; fi
            result=1
        fi
        rm -rf -- "$work"
        exit "$result"
    }
    trap rollback_bbr EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    printf '# Managed by sing-box-manager: TCP BBR\nnet.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n' >"$work/sysctl.conf"
    [[ $had_file -eq 0 ]] || install -m 600 "$BBR_SYSCTL_FILE" "$work/original.conf" || exit 1
    if [[ $had_restore -eq 1 ]]; then
        install -m 600 "$BBR_RESTORE_FILE" "$work/active-before" &&
            install -m 600 "$restore_backup/runtime.json" "$work/restore-runtime.json" || exit 1
        if [[ $(jq -r '.config_existed' "$work/restore-runtime.json") == true ]]; then
            install -m 600 "$restore_backup/sysctl.conf" "$work/restore.conf" || exit 1
        fi
    fi
    if [[ $action == disable ]]; then
        target_cc=$(jq -r '.tcp_congestion_control' "$work/restore-runtime.json") &&
            target_qdisc=$(jq -r '.default_qdisc' "$work/restore-runtime.json") &&
            target_mode=$(jq -r '.config_mode' "$work/restore-runtime.json") || exit 1
        [[ $(jq -r '.config_existed' "$work/restore-runtime.json") == true ]] || target_file=0
        if [[ $old_cc != bbr || $old_qdisc != fq ]] || ! cmp -s "$work/sysctl.conf" "$BBR_SYSCTL_FILE"; then
            fail "当前参数或本面板 BBR 配置已被其他操作修改，拒绝覆盖；请先核对。"; exit 1
        fi
        [[ $target_file -eq 0 ]] || cp "$work/restore.conf" "$work/sysctl.conf" || exit 1
    fi
    bbr_inputs_unchanged() {
        [[ ! -L $BBR_SYSCTL_DIR && ! -L $BBR_SYSCTL_FILE &&
           ( ! -e $BBR_SYSCTL_DIR || -d $BBR_SYSCTL_DIR ) ]] || return 1
        [[ $(sysctl -n net.ipv4.tcp_congestion_control 2>/dev/null) == "$old_cc" &&
           $(sysctl -n net.core.default_qdisc 2>/dev/null) == "$old_qdisc" ]] || return 1
        if [[ $had_file -eq 1 ]]; then
            [[ -f $BBR_SYSCTL_FILE ]] && cmp -s "$work/original.conf" "$BBR_SYSCTL_FILE" &&
                [[ $(stat -c '%a' "$BBR_SYSCTL_FILE" 2>/dev/null) == "$old_mode" ]] || return 1
        else
            [[ ! -e $BBR_SYSCTL_FILE && ! -L $BBR_SYSCTL_FILE ]] || return 1
        fi
        [[ ! -L $BBR_RESTORE_FILE ]] || return 1
        if [[ $had_restore -eq 1 ]]; then
            cmp -s "$work/active-before" "$BBR_RESTORE_FILE" &&
                [[ $(bbr_restore_backup) == "$restore_backup" ]] &&
                cmp -s "$work/restore-runtime.json" "$restore_backup/runtime.json" || return 1
            if [[ -f $work/restore.conf ]]; then
                cmp -s "$work/restore.conf" "$restore_backup/sysctl.conf" || return 1
            fi
        else
            [[ ! -e $BBR_RESTORE_FILE ]] || return 1
        fi
        [[ $action == disable ]] || check_bbr_persistence
    }
    if [[ $action == enable && $old_cc == bbr && $old_qdisc == fq ]] && cmp -s "$work/sysctl.conf" "$BBR_SYSCTL_FILE"; then
        info "TCP BBR 已启用，持久化配置也已就绪，无需修改。"; exit 0
    fi
    if [[ $action == disable ]]; then
        info "将恢复开启前的 TCP 算法 $target_cc、默认队列规则 $target_qdisc，以及原持久化文件状态。"
        info "开启前备份：$restore_backup；不会把原算法强制改为 cubic。"
        [[ $target_cc != bbr ]] || warn "开启前已经使用 bbr，恢复后仍为 bbr；本次撤销的是本面板的设置。"
    else
        info "将设置整台 VPS 的 TCP 默认拥塞控制为 bbr，默认队列规则为 fq，并写入 $BBR_SYSCTL_FILE。"
    fi
    info "只影响 TCP；Hy2/TUIC 的 UDP/QUIC 拥塞控制不受此设置直接控制。"
    info "已有连接和监听可能沿用旧算法；本操作不重启服务、不重建现有网卡队列。"
    if [[ $action == disable ]]; then
        confirm "确认关闭并恢复开启前的设置？（先备份，失败尝试恢复）" || exit 0
    else
        confirm "确认启用 TCP BBR？（先备份，失败尝试恢复）" || exit 0
    fi
    bbr_inputs_unchanged || { fail "设置在确认期间发生变化或存在冲突，请重新打开 BBR 选项。"; exit 1; }
    if [[ ! " $available " =~ [[:space:]]${target_cc}[[:space:]] ]]; then
        if have modprobe; then modprobe "tcp_$target_cc" >/dev/null 2>&1 || true; fi
        available=$(sysctl -n net.ipv4.tcp_available_congestion_control) || exit 1
    fi
    [[ " $available " =~ [[:space:]]${target_cc}[[:space:]] ]] || {
        fail "内核未提供可用的 $target_cc；模块不存在或加载受限。不会安装内核或修改参数。"; exit 1;
    }
    if [[ $old_qdisc != "$target_qdisc" ]] && have modprobe; then modprobe "sch_$target_qdisc" >/dev/null 2>&1 || true; fi
    bbr_inputs_unchanged || { fail "设置已被其他操作修改，已取消写入。"; exit 1; }
    install -d -m 700 "$BACKUP_DIR" || exit 1
    backup=$(mktemp -d "$BACKUP_DIR/bbr-$(date +%Y%m%d-%H%M%S)-XXXXXX") || exit 1
    jq -n --arg cc "$old_cc" --arg qdisc "$old_qdisc" --arg mode "$old_mode" \
        --argjson existed "$([[ $had_file -eq 1 ]] && printf true || printf false)" \
        '{tcp_congestion_control:$cc,default_qdisc:$qdisc,config_existed:$existed,config_mode:$mode}' >"$backup/runtime.json" || exit 1
    [[ $had_file -eq 0 ]] || install -m 600 "$work/original.conf" "$backup/sysctl.conf" || exit 1
    [[ $had_restore -eq 0 ]] || install -m 600 "$work/active-before" "$backup/active-before" || exit 1
    transaction=1
    sysctl -w "net.core.default_qdisc=$target_qdisc" >/dev/null || exit 1
    sysctl -w "net.ipv4.tcp_congestion_control=$target_cc" >/dev/null || exit 1
    if [[ $target_file -eq 1 ]]; then
        [[ -d $BBR_SYSCTL_DIR ]] || install -d -m 755 "$BBR_SYSCTL_DIR" || exit 1
        atomic_copy "$work/sysctl.conf" "$BBR_SYSCTL_FILE" "$target_mode" || exit 1
        cmp -s "$work/sysctl.conf" "$BBR_SYSCTL_FILE" &&
            [[ $(stat -c '%a' "$BBR_SYSCTL_FILE") == "$target_mode" ]] || exit 1
    else
        rm -f -- "$BBR_SYSCTL_FILE" || exit 1
        [[ ! -e $BBR_SYSCTL_FILE && ! -L $BBR_SYSCTL_FILE ]] || exit 1
    fi
    [[ $(sysctl -n net.ipv4.tcp_congestion_control) == "$target_cc" &&
       $(sysctl -n net.core.default_qdisc) == "$target_qdisc" ]] || { fail "内核参数读回校验失败。"; exit 1; }
    if [[ $action == disable ]]; then
        rm -f -- "$BBR_RESTORE_FILE" || exit 1
        [[ ! -e $BBR_RESTORE_FILE ]] || exit 1
    elif [[ $had_restore -eq 0 ]]; then
        printf '%s\n' "${backup##*/}" >"$work/active"
        atomic_copy "$work/active" "$BBR_RESTORE_FILE" 600 || exit 1
        cmp -s "$work/active" "$BBR_RESTORE_FILE" || exit 1
    fi
    transaction=0
    if [[ $action == disable ]]; then
        info "已恢复开启前的设置：TCP=$target_cc，默认队列=$target_qdisc；操作前备份：$backup"
    else
        info "TCP BBR 已启用并写入持久化配置；操作前备份：$backup"
    fi
    info "如需已有入站监听采用新算法，可自行选择 5.重新启动；重启会短暂中断现有节点连接。"
)

enable_bbr_locked() { configure_bbr_locked enable; }
disable_bbr_locked() { configure_bbr_locked disable; }

enable_bbr() {
    [[ $# -eq 0 ]] || { fail "用法：sing-box bbr"; return 1; }
    if ! have sysctl; then install_dependencies || return; fi
    with_lock enable_bbr_locked
}

disable_bbr() {
    [[ $# -eq 0 ]] || { fail "用法：sing-box bbr-disable"; return 1; }
    have sysctl || { fail "缺少 sysctl，请重新运行 setup 安装依赖。"; return 1; }
    with_lock disable_bbr_locked
}

validate_acme_menu() {
    [[ -f $1 && ! -L $1 && -s $1 ]] &&
        [[ $(stat -c '%s' "$1") -le 1048576 ]] &&
        grep -Fq 'https://github.com/1622663850/acme-script' "$1" &&
        grep -Fxq '# acme-script: persistent-menu-v1' "$1" &&
        grep -Eq '^menu[[:space:]]*\([[:space:]]*\)[[:space:]]*\{' "$1" &&
        bash -n "$1" 9>&-
}

ensure_acme_menu_locked() (
    umask 077
    local script work staged="" transaction=0
    safe_paths || exit 1
    [[ ! -L $ACME_MENU_DIR && ( ! -e $ACME_MENU_DIR || -d $ACME_MENU_DIR ) ]] || {
        fail "ACME 菜单目录无效，拒绝操作符号链接。"; exit 1;
    }
    for script in "$ACME_MENU_SCRIPT" "${SB_ROOT_PREFIX}/root/acme_cf_token.sh"; do
        if [[ -e $script || -L $script ]]; then
            if validate_acme_menu "$script"; then
                printf '%s\n' "$script"
                exit 0
            fi
            [[ $script != "$ACME_MENU_SCRIPT" ]] || {
                fail "现有 ACME 菜单未通过检查，不会覆盖：$script"; exit 1;
            }
            warn "发现其他或无效的同名证书脚本，将保留原文件，另行安装指定项目的菜单。"
        fi
    done
    info "正在获取支持取消及失败返回的 ACME 菜单组件（不重装 acme.sh）。" >&2
    info "此步骤只安装菜单；申请证书或安装 acme.sh 请在证书菜单中选择。" >&2
    work=$(mktemp -d "$STATE_DIR/acme-menu.XXXXXX") || exit 1
    # shellcheck disable=SC2317,SC2329 # Invoked by EXIT/INT/TERM traps below.
    cleanup_acme_menu() {
        local result=$?
        trap - EXIT INT TERM
        if [[ $transaction -eq 1 && -n $staged && ! -L $ACME_MENU_SCRIPT && $staged -ef $ACME_MENU_SCRIPT ]]; then
            rm -f -- "$ACME_MENU_SCRIPT" || result=1
        fi
        [[ -z $staged ]] || rm -f -- "$staged" || result=1
        rm -rf -- "$work"
        exit "$result"
    }
    trap cleanup_acme_menu EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    download_https 'https://api.github.com/repos/1622663850/acme-script/contents/acme_cf_token.sh?ref=main' \
        "$work/source.json" 60 || { fail "ACME 菜单下载失败，请稍后重试。"; exit 1; }
    if ! decode_github_file "$work/source.json" "$work/acme.sh" acme_cf_token.sh ||
       ! validate_acme_menu "$work/acme.sh"; then
        fail "ACME 菜单下载内容未通过检查。"; exit 1
    fi
    [[ ! -e $ACME_MENU_SCRIPT && ! -L $ACME_MENU_SCRIPT && ! -L $ACME_MENU_DIR ]] || {
        fail "ACME 菜单路径已被其他操作修改，请重试。"; exit 1;
    }
    [[ -d $ACME_MENU_DIR ]] || install -d -m 700 "$ACME_MENU_DIR" || exit 1
    staged=$(mktemp "$ACME_MENU_SCRIPT.tmp.XXXXXX") || exit 1
    install -m 700 "$work/acme.sh" "$staged" || exit 1
    transaction=1
    # Publish without replacing a file created by another process during download.
    ln -T -- "$staged" "$ACME_MENU_SCRIPT" || exit 1
    cmp -s "$work/acme.sh" "$ACME_MENU_SCRIPT" || exit 1
    transaction=0
    printf '%s\n' "$ACME_MENU_SCRIPT"
)

open_acme_menu() {
    [[ $# -eq 0 ]] || { fail "用法：sing-box acme"; return 1; }
    local script
    script=$(with_lock ensure_acme_menu_locked) || return
    info "正在打开 ACME 证书菜单，退出后返回 sing-box 面板。"
    # persistent-menu-v1 returns only on main-menu exit or terminal closure.
    # Actions run in child shells so their errors cannot exit the ACME menu.
    bash "$script" 9>&-
}

parse_outbound_links() {
    local input=${1:-}
    [[ $# -eq 1 && -f $input && ! -L $input ]] || {
        printf '%s\n' '分享链接解析失败。' >&2
        return 1
    }
    python3 - "$input" <<'PY_OUTBOUND_LINKS'
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
from pathlib import Path
import re
import sys
from urllib.parse import parse_qsl, unquote, urlsplit


class ParseError(Exception):
    pass


UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
HOST_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
FINGERPRINT_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def fail() -> None:
    raise ParseError


def decode_b64(value: str) -> bytes:
    if not value or any(char.isspace() for char in value):
        fail()
    raw = value.encode("ascii", "strict")
    if not re.fullmatch(rb"[A-Za-z0-9+/_-]+={0,2}", raw) or len(raw.rstrip(b"=")) % 4 == 1:
        fail()
    padded = raw + b"=" * ((4 - len(raw) % 4) % 4)
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, UnicodeError, base64.binascii.Error):
        fail()
    if not decoded:
        fail()
    return decoded


def decode_b64_text(value: str) -> str:
    try:
        return decode_b64(value).decode("utf-8")
    except (UnicodeDecodeError, ParseError):
        fail()


def valid_host(host: str) -> bool:
    if not host or any(ord(char) < 0x21 or ord(char) == 0x7F for char in host):
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return bool(HOST_RE.fullmatch(host)) and ".." not in host


def endpoint(parts) -> tuple[str, int]:
    if not parts.hostname or not valid_host(parts.hostname):
        fail()
    try:
        port = parts.port
    except ValueError:
        fail()
    if port is None or not 1 <= port <= 65535:
        fail()
    return parts.hostname, port


def query_values(parts, allowed: set[str]) -> dict[str, str]:
    if parts.query and any(char in parts.query for char in "\r\n"):
        fail()
    values = parse_qsl(parts.query, keep_blank_values=True, strict_parsing=True)
    result: dict[str, str] = {}
    for key, value in values:
        if key not in allowed or key in result:
            fail()
        result[key] = value
    return result


def alpn(value: str | None) -> list[str] | None:
    if value is None:
        return None
    values = value.split(",")
    if not values or any(not item or len(item) > 64 for item in values):
        fail()
    return values


def uuid(value: str) -> str:
    if not UUID_RE.fullmatch(value):
        fail()
    return value


def tag(raw: str, preferred: str) -> str:
    value = preferred if preferred else ""
    if TAG_RE.fullmatch(value):
        return value
    return "import-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def tls_fields(result: dict, query: dict[str, str], default_name: str, reality: bool = False) -> None:
    name = query.get("sni", default_name)
    if name and not valid_host(name):
        fail()
    tls: dict = {"enabled": True}
    if name:
        tls["server_name"] = name
    names = alpn(query.get("alpn"))
    if names is not None:
        tls["alpn"] = names
    if reality:
        public_key = query.get("pbk")
        short_id = query.get("sid")
        if not public_key or not re.fullmatch(r"[A-Za-z0-9_-]{43}", public_key):
            fail()
        if not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", short_id or ""):
            fail()
        fingerprint = query.get("fp", "chrome")
        if not FINGERPRINT_RE.fullmatch(fingerprint):
            fail()
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
        tls["reality"] = {"enabled": True, "public_key": public_key, "short_id": short_id}
    elif "fp" in query:
        fingerprint = query["fp"]
        if not FINGERPRINT_RE.fullmatch(fingerprint):
            fail()
        tls["utls"] = {"enabled": True, "fingerprint": fingerprint}
    result["tls"] = tls


def userinfo(parts) -> tuple[str, str]:
    if "@" not in parts.netloc:
        fail()
    return unquote(parts.username or ""), unquote(parts.password or "")


def parse_vless(raw: str, parts, name: str) -> dict:
    user, password = userinfo(parts)
    if password or not user:
        fail()
    host, port = endpoint(parts)
    query = query_values(
        parts,
        {"encryption", "type", "security", "flow", "fp", "sni", "pbk", "sid", "alpn",
         "path", "host", "serviceName", "mode"},
    )
    if query.get("encryption", "none") != "none":
        fail()
    transport = query.get("type", "tcp")
    if transport not in {"tcp", "ws", "grpc"}:
        fail()
    security = query.get("security", "none")
    if security not in {"none", "tls", "reality"}:
        fail()
    result = {"type": "vless", "tag": name, "server": host, "server_port": port, "uuid": uuid(user)}
    if transport == "ws":
        path = query.get("path")
        if not path or not path.startswith("/"):
            fail()
        result["transport"] = {"type": "ws", "path": path}
        if "host" in query:
            if not valid_host(query["host"]):
                fail()
            result["transport"]["headers"] = {"Host": query["host"]}
    elif transport == "grpc":
        service = query.get("serviceName")
        if not service or query.get("mode", "gun") != "gun":
            fail()
        result["transport"] = {"type": "grpc", "service_name": service}
    for key in ("path", "host", "serviceName", "mode"):
        if key in query and ((transport == "ws" and key not in {"path", "host"}) or
                             (transport == "grpc" and key not in {"serviceName", "mode"}) or
                             transport == "tcp"):
            fail()
    if security == "reality":
        if transport not in {"tcp", "grpc"}:
            fail()
        tls_fields(result, query, host, reality=True)
        if "flow" in query:
            if query["flow"] != "xtls-rprx-vision":
                fail()
            if transport == "grpc":
                fail()
            result["flow"] = query["flow"]
    elif security == "tls":
        if any(key in query for key in ("flow", "pbk", "sid")):
            fail()
        tls_fields(result, query, host)
    elif any(key in query for key in ("flow", "fp", "pbk", "sid", "sni", "alpn")):
        fail()
    return result


def parse_vmess(raw: str, parts) -> dict:
    if parts.query:
        fail()
    encoded = raw.split("://", 1)[1].split("#", 1)[0]
    value = json.loads(decode_b64_text(encoded))
    if not isinstance(value, dict):
        fail()
    allowed = {"v", "ps", "add", "port", "id", "aid", "scy", "net", "type", "host",
               "path", "tls", "sni", "alpn", "fp"}
    if set(value) - allowed:
        fail()
    host = value.get("add")
    if not isinstance(host, str) or not valid_host(host):
        fail()
    try:
        if not re.fullmatch(r"[0-9]{1,5}", str(value["port"])) or not re.fullmatch(r"[0-9]+", str(value.get("aid", 0))):
            fail()
        port = int(value["port"])
        aid = int(value.get("aid", 0))
    except (KeyError, TypeError, ValueError):
        fail()
    if not 1 <= port <= 65535 or aid < 0:
        fail()
    security = str(value.get("scy", "auto"))
    if security not in {"auto", "none", "aes-128-gcm", "chacha20-poly1305", "zero"}:
        fail()
    result = {
        "type": "vmess", "tag": tag(raw, str(value.get("ps", ""))),
        "server": host, "server_port": port, "uuid": uuid(str(value.get("id", ""))),
        "security": security, "alter_id": aid,
    }
    network = value.get("net", "tcp")
    if network not in {"tcp", "ws", "grpc"}:
        fail()
    if network == "ws":
        path = str(value.get("path", ""))
        if not path.startswith("/"):
            fail()
        result["transport"] = {"type": "ws", "path": path}
        if value.get("host"):
            if not valid_host(str(value["host"])):
                fail()
            result["transport"]["headers"] = {"Host": str(value["host"])}
    elif network == "grpc":
        if value.get("type", "gun") != "gun" or not value.get("path"):
            fail()
        result["transport"] = {"type": "grpc", "service_name": str(value["path"])}
    elif value.get("path") or value.get("host") or value.get("type") not in {None, "none"}:
        fail()
    tls_mode = str(value.get("tls", ""))
    if tls_mode not in {"", "tls"}:
        fail()
    if tls_mode:
        query = {"sni": str(value.get("sni", host))}
        if value.get("alpn"):
            query["alpn"] = str(value["alpn"])
        if value.get("fp"):
            query["fp"] = str(value["fp"])
        tls_fields(result, query, host)
    elif any(value.get(key) for key in ("sni", "alpn", "fp")):
        fail()
    return result


def parse_hysteria2(raw: str, parts, name: str) -> dict:
    user, password = userinfo(parts)
    credential = unquote(parts.netloc.rsplit("@", 1)[0])
    if not credential:
        fail()
    host, port = endpoint(parts)
    query = query_values(parts, {"sni", "alpn", "obfs", "obfs-password", "insecure"})
    if query.get("insecure", "0").lower() not in {"0", "false", ""}:
        fail()
    result = {"type": "hysteria2", "tag": name, "server": host, "server_port": port, "password": credential}
    tls_fields(result, query, host)
    if "obfs-password" in query and "obfs" not in query:
        fail()
    if "obfs" in query:
        if query["obfs"] != "salamander" or len(query.get("obfs-password", "").encode("utf-8")) < 4:
            fail()
        result["obfs"] = {"type": "salamander", "password": query["obfs-password"]}
    return result


def parse_tuic(raw: str, parts, name: str) -> dict:
    user, password = userinfo(parts)
    host, port = endpoint(parts)
    query = query_values(parts, {"sni", "alpn", "congestion_control", "udp_relay_mode"})
    if query.get("congestion_control", "cubic") not in {"cubic", "new_reno", "bbr"}:
        fail()
    if query.get("udp_relay_mode", "native") != "native":
        fail()
    if not password:
        fail()
    result = {
        "type": "tuic", "tag": name, "server": host, "server_port": port,
        "uuid": uuid(user), "password": password,
        "congestion_control": query.get("congestion_control", "cubic"),
    }
    tls_fields(result, query, host)
    return result


def parse_anytls(raw: str, parts, name: str) -> dict:
    user, password = userinfo(parts)
    credential = unquote(parts.netloc.rsplit("@", 1)[0])
    if not credential:
        fail()
    host, port = endpoint(parts)
    query = query_values(parts, {"sni", "alpn"})
    result = {"type": "anytls", "tag": name, "server": host, "server_port": port, "password": credential}
    tls_fields(result, query, host)
    return result


def parse_ss(raw: str, parts, name: str) -> dict:
    if parts.query:
        fail()
    if "@" in parts.netloc:
        method, password = userinfo(parts)
        host, port = endpoint(parts)
        if parts.path not in {"", "/"}:
            fail()
        if not password:
            decoded = decode_b64_text(method)
            if ":" not in decoded:
                fail()
            method, password = decoded.split(":", 1)
        if not method or not password:
            fail()
    elif "@" not in raw:
        decoded = decode_b64_text(raw.split("://", 1)[1].split("#", 1)[0])
        if "@" not in decoded:
            fail()
        credentials, address = decoded.rsplit("@", 1)
        if ":" not in credentials:
            fail()
        method, password = credentials.split(":", 1)
        host, port = endpoint(urlsplit("ss://" + address))
    else:
        decoded = decode_b64_text(parts.path.lstrip("/"))
        if "@" not in decoded:
            fail()
        credentials, address = decoded.rsplit("@", 1)
        if ":" not in credentials:
            fail()
        method, password = credentials.split(":", 1)
        fake = urlsplit("ss://" + address)
        host, port = endpoint(fake)
    if not method or not password:
        fail()
    return {"type": "shadowsocks", "tag": name, "server": host, "server_port": port,
            "method": method, "password": password}


def parse_trojan(raw: str, parts, name: str) -> dict:
    user, password = userinfo(parts)
    credential = unquote(parts.netloc.rsplit("@", 1)[0])
    if not credential:
        fail()
    host, port = endpoint(parts)
    query = query_values(parts, {"security", "sni", "alpn", "fp", "type", "path", "host", "serviceName", "mode", "insecure"})
    if query.get("security", "tls") not in {"tls", ""} or query.get("insecure", "0").lower() not in {"0", "false", ""}:
        fail()
    result = {"type": "trojan", "tag": name, "server": host, "server_port": port, "password": credential}
    transport = query.get("type", "tcp")
    if transport == "ws":
        path = query.get("path", "")
        if not path.startswith("/"):
            fail()
        result["transport"] = {"type": "ws", "path": path}
        if query.get("host"):
            if not valid_host(query["host"]):
                fail()
            result["transport"]["headers"] = {"Host": query["host"]}
    elif transport == "grpc":
        if not query.get("serviceName") or query.get("mode", "gun") != "gun":
            fail()
        result["transport"] = {"type": "grpc", "service_name": query["serviceName"]}
    elif transport != "tcp":
        fail()
    for key in ("path", "host", "serviceName", "mode"):
        if key in query and ((transport == "ws" and key not in {"path", "host"}) or
                             (transport == "grpc" and key not in {"serviceName", "mode"}) or transport == "tcp"):
            fail()
    tls_fields(result, query, host)
    return result


def parse_socks(raw: str, parts, name: str) -> dict:
    user = password = ""
    if "@" in parts.netloc:
        user, password = userinfo(parts)
    host, port = endpoint(parts)
    if parts.query:
        fail()
    result = {"type": "socks", "tag": name, "server": host, "server_port": port}
    if user:
        result["username"] = user
    if password:
        result["password"] = password
    return result


def parse_http(raw: str, parts, name: str) -> dict:
    user = password = ""
    if "@" in parts.netloc:
        user, password = userinfo(parts)
    host, port = endpoint(parts)
    if parts.query:
        fail()
    result = {"type": "http", "tag": name, "server": host, "server_port": port}
    if user:
        result["username"] = user
    if password:
        result["password"] = password
    if parts.scheme == "https":
        tls_fields(result, {}, host)
    return result


def parse_line(raw: str) -> dict:
    try:
        parts = urlsplit(raw)
    except ValueError:
        fail()
    scheme = parts.scheme.lower()
    preferred = unquote(parts.fragment)
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        fail()
    if scheme not in {"vmess", "ss"} and parts.path not in {"", "/"}:
        fail()
    if scheme == "vmess":
        result = parse_vmess(raw, parts)
        result["tag"] = tag(raw, preferred or result["tag"])
        return result
    name = tag(raw, preferred)
    if scheme == "vless":
        return parse_vless(raw, parts, name)
    if scheme in {"hysteria2", "hy2"}:
        return parse_hysteria2(raw, parts, name)
    if scheme == "tuic":
        return parse_tuic(raw, parts, name)
    if scheme == "anytls":
        return parse_anytls(raw, parts, name)
    if scheme == "ss":
        return parse_ss(raw, parts, name)
    if scheme == "trojan":
        return parse_trojan(raw, parts, name)
    if scheme in {"socks5", "socks5h"}:
        return parse_socks(raw, parts, name)
    if scheme in {"http", "https"}:
        return parse_http(raw, parts, name)
    fail()


def main() -> int:
    try:
        path = Path(sys.argv[1])
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        if not lines:
            fail()
        print(json.dumps([parse_line(line) for line in lines], ensure_ascii=False, separators=(",", ":")))
        return 0
    except (OSError, ParseError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        print("分享链接解析失败。", file=sys.stderr)
        return 1


raise SystemExit(main())
PY_OUTBOUND_LINKS
}


normalize_outbound_import() (
    umask 077
    local file=$1 work
    [[ -f $file && ! -L $file && $(stat -c '%s' "$file") -le 1048576 ]] || {
        fail "导入文件须为不超过 1 MiB 的普通文件。"; exit 1;
    }
    work=$(mktemp -d "$STATE_DIR/outbound-import.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    cp -- "$file" "$work/source" || exit 1
    if json_query "$work/source" . >"$work/json" 2>/dev/null; then
        jq -e 'if type=="array" then .
          elif type=="object" and has("type") then [.]
          elif type=="object" and keys==["outbounds"] then .outbounds
          else error("not outbound JSON") end | if type=="array" and length>0 then . else error("empty import") end' \
          "$work/json" 2>/dev/null || { fail "请导入单个 outbound、数组或仅含 outbounds 的 JSON。"; exit 1; }
    else parse_outbound_links "$work/source"; fi
)

# shellcheck disable=SC2016 # json_query filters use literal jq variables.
add_outbounds_file_locked() (
    umask 077
    local file=$1 work tag backup
    require_core && safe_paths || exit 1
    work=$(mktemp -d "$STATE_DIR/outbound-add.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    cp -- "$CONFIG_FILE" "$work/before.json" || exit 1
    normalize_outbound_import "$file" >"$work/nodes.json" || exit 1
    jq -e 'type=="array" and length>0 and all(.[]; type=="object" and
      (.type|type=="string" and length>0) and (.tag|type=="string")) and
      ([.[].tag]|length== (unique|length))' "$work/nodes.json" >/dev/null || { fail "出站格式无效或名称重复。"; exit 1; }
    while IFS= read -r tag; do
        valid_tag "$tag" || { fail "出站名称须为 1–64 位字母、数字、短横线或下划线。"; exit 1; }
    done < <(jq -r '.[].tag' "$work/nodes.json")
    if json_query "$work/before.json" -e --slurpfile nodes "$work/nodes.json" '
      [(.outbounds[]?.tag),(.endpoints[]?.tag)] as $old | any($nodes[0][]; .tag as $t | $old|index($t)!=null)' >/dev/null; then
        fail "已有相同名称的出站或 endpoint，导入已取消。"; exit 1
    fi
    json_query "$work/before.json" --slurpfile nodes "$work/nodes.json" '.outbounds=((.outbounds // [])+$nodes[0])' >"$work/after.json" || exit 1
    validate_config "$work/after.json" >"$work/check.log" 2>&1 || { fail "出站未通过核心校验，未保存；请核对协议、认证、TLS 和 DNS 引用。"; exit 1; }
    jq -r '["名称","协议"],(.[]|[.tag,.type]) | @tsv' "$work/nodes.json"
    if jq -e 'any(.[]; .tls.insecure==true)' "$work/nodes.json" >/dev/null; then
        warn "导入内容显式关闭了 TLS 证书校验，请确认远端配置可信。"
    fi
    info "仅新增出站，不改变入站绑定；运行中的服务会短暂重启，停止状态与自启保留。"
    confirm "导入以上出站？（自动备份）" || exit 1
    cmp -s "$work/before.json" "$CONFIG_FILE" || { fail "配置已变化，请重新导入。"; exit 1; }
    backup=$(create_backup) || exit 1
    commit_config "$work/after.json" >"$work/save.log" 2>&1 || { fail "保存出站失败，已执行原有回滚；请检查服务状态和备份。"; exit 1; }
    info "出站已添加，成套备份：$backup"
)

user_outbound_bundle() {
    python3 - "$@" <<'PY_USER_OUTBOUND'
import json, re, secrets, sys
from pathlib import Path

class BindingError(Exception): pass
def require(condition, message):
    if not condition: raise BindingError(message)
def name_ok(value):
    return isinstance(value, str) and bool(value) and not any(ord(c) < 32 or ord(c) == 127 for c in value)
def auth_names(obj):
    result = set()
    if isinstance(obj, dict):
        value = obj.get('auth_user')
        if isinstance(value, str): result.add(value)
        elif isinstance(value, list): result.update(n for n in value if isinstance(n, str))
        for value in obj.values(): result.update(auth_names(value))
    elif isinstance(obj, list):
        for value in obj: result.update(auth_names(value))
    return result

try:
    mode, file, tag = sys.argv[1:4]
    bundle = json.loads(Path(file).read_text(encoding='utf-8'))
    config, clients = bundle['config'], bundle['clients']
    found = [n for n in config.get('inbounds', []) if n.get('tag') == tag]
    require(len(found) == 1, '入站不存在或名称重复。')
    node = found[0]
    meta = clients.get(tag, {})
    require(isinstance(meta, dict), '节点连接信息无效。')
    route = config.get('route') or {}
    require(isinstance(route, dict), '现有路由无效。')
    rules = route.get('rules')
    if rules is None: rules = []
    require(isinstance(rules, list), '现有规则列表无效。')
    collection = meta.get('user_outbound_routes')
    records = []
    if 'user_outbound_routes' in meta:
        require(isinstance(collection, dict) and set(collection) == {'format', 'created_metadata', 'bindings', 'inbound'} and
                collection.get('format') == 'sing-box-manager-user-outbound-route-v1' and
                isinstance(collection.get('created_metadata'), bool) and
                isinstance(collection.get('bindings'), list) and bool(collection['bindings']), '用户绑定记录格式无效。')
        require('inbound' not in meta or meta['inbound'] == node, '节点与用户绑定记录不一致，请先核对外部修改。')
        require(collection.get('inbound') == node, '入站用户与绑定快照不一致，请先核对外部修改。')
        records = collection['bindings']
    users = node.get('users')
    if mode == 'bind' or records:
        require(node.get('type') in {'vless', 'vmess', 'hysteria2', 'tuic', 'anytls'} and
                isinstance(users, list) and bool(users) and all(isinstance(u, dict) for u in users), '此模板不支持按认证用户绑定出站。')
        names = [u.get('name') for u in users if u.get('name')]
        require(all(name_ok(n) for n in names) and len(names) == len(set(names)), '用户名重复或无效，不能区分用户。')
        key = 'uuid' if node['type'] in {'vless', 'vmess', 'tuic'} else 'password'
        credentials = [u.get(key) for u in users]
        require(all(isinstance(c, str) and c for c in credentials), '用户认证无效。')
        if key == 'uuid': credentials = [c.lower() for c in credentials]
        require(len(set(credentials)) == len(credentials), '认证凭据重复，不能区分用户。')
    seen = set()
    for record in records:
        require(isinstance(record, dict) and set(record) == {'name', 'name_before_present', 'name_before', 'rule'}, '用户绑定记录无效。')
        name, rule = record['name'], record['rule']
        require(name_ok(name) and name not in seen and sum(u.get('name') == name for u in users) == 1, '绑定用户缺失或名称重复。')
        seen.add(name)
        require(isinstance(record['name_before_present'], bool) and record['name_before'] in (None, '', name) and
                (record['name_before_present'] or record['name_before'] is None), '用户原名称恢复记录无效。')
        require((record['name_before_present'] and record['name_before'] == name) or
                (not record['name_before'] and re.fullmatch(r'user-route-[0-9a-f]{16}', name)), '原用户名与自动名称恢复记录不一致。')
        require(isinstance(rule, dict) and name_ok(rule.get('outbound')) and
                rule == {'inbound': [tag], 'auth_user': [name], 'action': 'route', 'outbound': rule.get('outbound')} and
                sum(r == rule for r in rules) == 1, '用户绑定规则已被外部修改、删除或重复，未保存。')
    bulk = meta.get('outbound_route')
    if bulk is not None and (records or mode == 'bind'):
        require(isinstance(bulk, dict) and bulk.get('format') == 'sing-box-manager-outbound-route-v1' and
                isinstance(bulk.get('rule'), dict) and name_ok(bulk['rule'].get('outbound')) and
                bulk['rule'] == {'inbound': [tag], 'action': 'route', 'outbound': bulk['rule']['outbound']} and
                sum(r == bulk['rule'] for r in rules) == 1, '入站默认绑定记录已被外部修改，未保存。')
        require(all(rules.index(e['rule']) < rules.index(bulk['rule']) for e in records), '用户规则被入站默认规则遮蔽，请先核对外部调整的优先级。')
    if mode == 'validate':
        print(json.dumps({'rules': [r for r in rules if any(r == e['rule'] for e in records)],
                          'names': [e['name'] for e in records]}, ensure_ascii=False))
        raise SystemExit(0)
    require(mode == 'bind' and len(sys.argv) == 6, '用户绑定参数无效。')
    index, target = sys.argv[4:6]
    require(re.fullmatch(r'[1-9][0-9]{0,5}', index) is not None and int(index) <= len(users), '用户序号无效。')
    user = users[int(index) - 1]
    record = next((r for r in records if r['name'] == user.get('name')), None)
    if target == '-' and record is None:
        # Metadata may have been lost while the actual per-user rule survived.
        # Only a single, unambiguous rule scoped to this inbound and user is safe to remove.
        name = user.get('name')
        candidates = [r for r in rules if isinstance(r, dict) and name and
                      set(r) <= {'type', 'inbound', 'auth_user', 'action', 'outbound'} and
                      r.get('type', 'default') == 'default' and r.get('action', 'route') == 'route' and
                      r.get('inbound') in (tag, [tag]) and r.get('auth_user') in (name, [name]) and
                      name_ok(r.get('outbound'))]
        referenced = [r for r in rules if name and name in auth_names(r) and
                      (not isinstance(r, dict) or 'inbound' not in r or r.get('invert') is True or
                       r.get('inbound') == tag or isinstance(r.get('inbound'), list) and tag in r['inbound'])]
        require(len(candidates) <= 1, '此用户存在多条出站规则，无法确定要解除哪一条；请先核对 route.rules。')
        if candidates:
            config['route']['rules'] = [r for r in rules if r is not candidates[0]]
            print('[提示] 未找到该用户的面板绑定记录；将仅移除配置中明确匹配此入站和此用户的出站规则，保留用户名及其他路由。', file=sys.stderr)
        else:
            require(not referenced, '此用户仍有自定义或共用路由，无法安全自动解除；请核对 route.rules，其他用户的规则未修改。')
        print(json.dumps(bundle, ensure_ascii=False)); raise SystemExit(0)
    if target != '-':
        require(sum(o.get('tag') == target for o in config.get('outbounds', [])) == 1, '出站不存在或名称重复。')
        require('inbound' not in meta or meta['inbound'] == node, '节点与连接信息不一致，请先核对。')
        if record is not None and record['rule']['outbound'] == target:
            print(json.dumps(bundle, ensure_ascii=False)); raise SystemExit(0)
    if record is not None:
        rules = [r for r in rules if r != record['rule']]
    if target == '-':
        if not record['name_before']:
            require(record['name'] not in (auth_names(rules) | auth_names(config.get('dns'))), '自动用户名已被其他规则引用，请先处理该引用再解除绑定。')
        if record['name_before_present']: user['name'] = record['name_before']
        else: user.pop('name', None)
        records = [r for r in records if r is not record]
    else:
        if record is None:
            before_present, before_name = 'name' in user, user.get('name')
            if not before_name:
                occupied = set(names) | auth_names(rules) | auth_names(config.get('dns'))
                while True:
                    generated = 'user-route-' + secrets.token_hex(8)
                    if generated not in occupied: break
                user['name'] = generated
            record = {'name': user['name'], 'name_before_present': before_present, 'name_before': before_name, 'rule': {}}
            if collection is None:
                created = tag not in clients or (set(meta) == {'outbound_route'} and isinstance(bulk, dict) and bulk.get('created_metadata') is True)
                collection = {'format': 'sing-box-manager-user-outbound-route-v1', 'created_metadata': created, 'bindings': records, 'inbound': node}
            records.append(record)
        rule = {'inbound': [tag], 'auth_user': [user['name']], 'action': 'route', 'outbound': target}
        require(rule not in rules, '相同用户路由已存在且未登记为本面板所有，不会接管。')
        record['rule'] = rule
        rules = [rule] + rules
    config.setdefault('route', {})
    if config['route'] is None: config['route'] = {}
    config['route']['rules'] = rules
    if 'inbound' in meta: meta['inbound'] = node
    if records:
        collection['bindings'] = records
        collection['inbound'] = node
        meta['user_outbound_routes'] = collection
        clients[tag] = meta
    else:
        meta.pop('user_outbound_routes', None)
        if collection['created_metadata'] and not meta: clients.pop(tag, None)
        else:
            if collection['created_metadata'] and set(meta) == {'outbound_route'}: meta['outbound_route']['created_metadata'] = True
            clients[tag] = meta
    print(json.dumps(bundle, ensure_ascii=False))
except BindingError as error:
    print('[错误] ' + str(error), file=sys.stderr); raise SystemExit(1)
except (OSError, ValueError, TypeError, KeyError, IndexError):
    print('[错误] 用户绑定或现有配置格式无效，未保存。', file=sys.stderr); raise SystemExit(1)
PY_USER_OUTBOUND
}

# shellcheck disable=SC2016 # jq filters contain literal variables.
bind_user_outbound_locked() (
    umask 077
    local bind_inbound=${1:-} user_index=${2:-} bind_target=${3:-} expected=${4:-} work
    [[ $# -ge 3 && $# -le 4 ]] || { fail "用法：sing-box outbound-user-bind 入站名称 用户序号 出站名称|-"; exit 1; }
    require_core && safe_paths || exit 1
    if ! valid_tag "$bind_inbound" || { [[ $bind_target != - ]] && ! valid_tag "$bind_target"; }; then fail "入站或出站名称无效。";exit 1;fi
    check_shadowtls_component "$bind_inbound" || exit 1
    work=$(mktemp -d "$STATE_DIR/user-outbound.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before.json" || exit 1
    if [[ -n $expected ]]; then
        [[ $(jq -Sc . "$expected") == "$(jq -Sc . "$work/before.json")" ]] || { fail "选择期间配置或用户已变化，请重新选择。"; exit 1; }
    fi
    jq -e --arg i "$bind_inbound" '[.config.inbounds[]?|select(.tag==$i)]|if length==1 then .[0] else error("inbound") end' "$work/before.json" >"$work/node.json" || exit 1
    validate_inbound_users "$work/node.json" || exit 1
    user_outbound_bundle bind "$work/before.json" "$bind_inbound" "$user_index" "$bind_target" >"$work/after.json" || exit 1
    if [[ $(jq -Sc '{config,clients}' "$work/before.json") == "$(jq -Sc '{config,clients}' "$work/after.json")" ]]; then
        if [[ $bind_target == - ]]; then info "此用户没有可解除的单独出站绑定；仍按入站默认出站及其他路由规则处理，未修改配置。"
        else info "此用户已绑定到所选出站，无需保存或重启。"; fi
        exit 0
    fi
    jq .config "$work/after.json" >"$work/config.json" || exit 1
    validate_config "$work/config.json" >"$work/check.log" 2>&1 || { fail "单用户绑定未通过核心校验，未保存。";exit 1; }
    if [[ $bind_target == - ]]; then info "解除 $bind_inbound 第 $user_index 个用户的绑定，恢复其余路由及入站默认出站。"
    else info "仅 $bind_inbound 第 $user_index 个用户优先使用出站 $bind_target；其他用户保持原路由。"; fi
    confirm "保存单用户出站绑定？（自动备份，运行中的服务短暂重启）" || exit 1
    snapshot_bundle >"$work/current.json" || exit 1
    [[ $(jq -Sc . "$work/current.json") == "$(jq -Sc . "$work/before.json")" ]] || { fail "配置、用户或连接信息已变化，请重新选择。"; exit 1; }
    apply_bundle "$work/after.json" >"$work/save.log" 2>&1 || { fail "单用户绑定保存失败，已执行配置和连接信息回滚。"; exit 1; }
    info "单用户出站绑定已保存。"
)

# shellcheck disable=SC2016 # jq filters contain literal variables.
user_outbound_wizard_locked() (
    umask 077
    local action=${1:-bind} user_route_inbound="" user_route_index="" user_route_target=- work
    [[ $action == bind || $action == unbind ]] || exit 1
    require_core && safe_paths || exit 1
    select_inbound user_route_inbound "设置单用户出站" || exit 1
    [[ -n $user_route_inbound ]] || exit 0
    work=$(mktemp -d "$STATE_DIR/user-outbound-wizard.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before.json" || exit 1
    jq -e --arg i "$user_route_inbound" '[.config.inbounds[]?|select(.tag==$i)]|if length==1 then .[0] else error("inbound") end' "$work/before.json" >"$work/node.json" || exit 1
    select_inbound_user user_route_index "$work/node.json" || exit 1
    [[ -n $user_route_index ]] || exit 0
    if [[ $action == bind ]]; then select_outbound user_route_target || exit 1; [[ -n $user_route_target ]] || exit 0; fi
    bind_user_outbound_locked "$user_route_inbound" "$user_route_index" "$user_route_target" "$work/before.json"
)

# shellcheck disable=SC2016 # jq filters contain literal variables.
list_user_outbound_bindings() (
    umask 077
    local work tag
    require_core && safe_paths || exit 1
    work=$(mktemp -d "$STATE_DIR/user-outbound-list.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/bundle.json" || exit 1
    printf '入站\t用户\t出站\n'
    while IFS= read -r tag; do
        user_outbound_bundle validate "$work/bundle.json" "$tag" >"$work/bindings.json" || exit 1
        jq -r --arg i "$tag" '.rules[]|[$i,.auth_user[0],.outbound]|map(gsub("[[:cntrl:]]";"?"))|@tsv' "$work/bindings.json" || exit 1
    done < <(jq -r '.config.inbounds[]?.tag' "$work/bundle.json")
)

# shellcheck disable=SC2016 # jq bundle filters use literal variables.
bind_outbound_locked() (
    umask 077
    local bind_inbound=$1 bind_target=$2 work
    require_core && safe_paths || exit 1
    if ! valid_tag "$bind_inbound" || { [[ $bind_target != - ]] && ! valid_tag "$bind_target"; }; then
        fail "入站或出站名称无效。"; exit 1
    fi
    check_shadowtls_component "$bind_inbound" || exit 1
    if is_snell_shadowtls "$bind_inbound"; then read_snell_shadowtls_group "$bind_inbound" >/dev/null || exit 1; fi
    work=$(mktemp -d "$STATE_DIR/outbound-bind.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before.json" || exit 1
    user_outbound_bundle validate "$work/before.json" "$bind_inbound" >"$work/user-rules.json" || exit 1
    jq --slurpfile user_rules "$work/user-rules.json" --arg i "$bind_inbound" --arg o "$bind_target" '
      if ([.config.inbounds[]? | select(.tag==$i)]|length)!=1 then error("inbound not unique") else . end |
      if $o!="-" and ([.config.outbounds[]? | select(.tag==$o)]|length)!=1 then error("outbound not unique") else . end |
      (.clients[$i].outbound_route // null) as $record |
      if $record!=null then
        if $record.format!="sing-box-manager-outbound-route-v1" or
           $record.rule != {inbound:[$i],action:"route",outbound:$record.rule.outbound} or
           ([.config.route.rules[]? | select(.==$record.rule)]|length)!=1
        then error("managed rule changed") else . end
      else . end |
      if $record==null and $o!="-" and any(.config.route.rules[]?; .=={inbound:[$i],action:"route",outbound:$o})
      then error("identical unowned rule") else . end |
      if $o=="-" and $record==null then .
      else
        .config.route.rules=((.config.route.rules // []) | map(select($record==null or .!=$record.rule))) |
        if $o=="-" then
          del(.clients[$i].outbound_route) |
          if $record.created_metadata==true and .clients[$i]=={} then del(.clients[$i]) else . end
        else
          {inbound:[$i],action:"route",outbound:$o} as $rule |
          .config.route.rules=($user_rules[0].rules+[$rule]+[.config.route.rules[] | select(. as $r | $user_rules[0].rules | index($r) | not)]) |
          (.clients|has($i)|not) as $created |
          .clients[$i].outbound_route={format:"sing-box-manager-outbound-route-v1",rule:$rule,
            created_metadata:(if $record!=null then ($record.created_metadata // false) else $created end)}
        end
      end' "$work/before.json" >"$work/after.json" 2>/dev/null || {
        fail "绑定不存在、名称重复，或原面板绑定规则已被外部修改；未保存。"; exit 1;
    }
    if [[ $(jq -Sc '{config,clients}' "$work/before.json") == "$(jq -Sc '{config,clients}' "$work/after.json")" ]]; then
        info "绑定设置未变化，无需保存或重启。"; exit 0
    fi
    jq .config "$work/after.json" >"$work/config.json" || exit 1
    validate_config "$work/config.json" >"$work/check.log" 2>&1 || { fail "绑定未通过核心校验，请检查出站和现有路由。"; exit 1; }
    if [[ $bind_target == - ]]; then info "取消 $bind_inbound 的面板出站绑定，恢复由其余路由规则及默认出站决定。"
    else info "$bind_inbound 未单独绑定的用户将优先使用出站 $bind_target；已有单用户绑定优先。"; fi
    confirm "保存入站出站绑定？（自动备份，运行中的服务短暂重启）" || exit 1
    snapshot_bundle >"$work/current.json" || exit 1
    [[ $(jq -Sc . "$work/current.json") == "$(jq -Sc . "$work/before.json")" ]] || { fail "配置或连接信息已变化，请重试。"; exit 1; }
    apply_bundle "$work/after.json" >"$work/save.log" 2>&1 || { fail "保存绑定失败，已执行原有配置及连接信息回滚；请检查服务状态和备份。"; exit 1; }
    info "入站出站绑定已保存。"
)

# shellcheck disable=SC2016 # json_query filters use literal jq variables.
delete_outbound_locked() (
    umask 077
    local delete_outbound_tag=$1 work backup
    require_core && safe_paths || exit 1
    valid_tag "$delete_outbound_tag" || { fail "出站名称无效。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/outbound-delete.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    cp -- "$CONFIG_FILE" "$work/before.json" || exit 1
    json_query "$work/before.json" -e --arg t "$delete_outbound_tag" '(.outbounds|length)>1 and ([.outbounds[]|select(.tag==$t)]|length)==1' >/dev/null || { fail "出站不存在、名称重复或是最后一个出站，不能删除。"; exit 1; }
    if json_query "$work/before.json" -e --arg t "$delete_outbound_tag" 'del(.outbounds[]|select(.tag==$t)) | any(..|strings; .==$t)' >/dev/null; then
        fail "出站仍被路由、detour 或选择器等引用，请先解除引用。"; exit 1
    fi
    json_query "$work/before.json" --arg t "$delete_outbound_tag" '.outbounds|=map(select(.tag!=$t))' >"$work/after.json" || exit 1
    validate_config "$work/after.json" >"$work/check.log" 2>&1 || { fail "删除后配置未通过校验，未保存。"; exit 1; }
    confirm "删除出站 $delete_outbound_tag？（自动备份）" || exit 1
    cmp -s "$work/before.json" "$CONFIG_FILE" || { fail "配置已变化，请重试。"; exit 1; }
    backup=$(create_backup) || exit 1
    commit_config "$work/after.json" >"$work/save.log" 2>&1 || { fail "删除失败，已执行原有回滚；请检查服务状态和备份。"; exit 1; }
    info "出站已删除，成套备份：$backup"
)

list_outbounds() {
    require_core || return
    config_jq -r '["序号","出站名称","协议","服务器","端口"],
      ((.outbounds // [])|to_entries[]|[.key+1,.value.tag,.value.type,(.value.server // "-"),(.value.server_port // "-")]) |
      map(if type=="string" then gsub("[[:cntrl:]]"; "?") else . end)|@tsv' || return
    info "面板绑定（其余节点沿用原有路由）："
    local name file
    for file in "$CLIENT_DIR"/*.json; do
        [[ -f $file && ! -L $file ]] || continue
        name=${file##*/}; name=${name%.json}
        jq -r --arg i "$name" 'select(.outbound_route!=null)|[$i,.outbound_route.rule.outbound]|map(gsub("[[:cntrl:]]";"?"))|@tsv' "$file" || return
    done
}

select_outbound() {
    local destination=$1 outbounds_snapshot outbounds_choice outbounds_tag outbounds_count
    printf -v "$destination" '%s' ""
    outbounds_snapshot=$(config_jq -c '.outbounds // []') || return
    outbounds_count=$(jq length <<<"$outbounds_snapshot") || return
    [[ $outbounds_count -gt 0 ]] || { info "暂无出站。"; return 0; }
    jq -r '["序号","名称","协议"],(to_entries[]|[.key+1,.value.tag,.value.type])|
      map(if type=="string" then gsub("[[:cntrl:]]";"?") else . end)|@tsv' <<<"$outbounds_snapshot"
    prompt outbounds_choice "选择出站（序号或名称；纯数字名称用 name:名称；0 返回）" 0 || return
    case "$outbounds_choice" in
        0) return 0 ;;
        name:*) outbounds_tag=${outbounds_choice#name:} ;;
        *)
            if [[ $outbounds_choice =~ ^[0-9]+$ ]]; then
                if [[ ! $outbounds_choice =~ ^[1-9][0-9]{0,5}$ ]] || (( outbounds_choice>outbounds_count )); then fail "出站序号无效。"; return 1; fi
                outbounds_tag=$(jq -r --argjson i "$((outbounds_choice-1))" '.[$i].tag' <<<"$outbounds_snapshot") || return
            else outbounds_tag=$outbounds_choice; fi ;;
    esac
    if ! valid_tag "$outbounds_tag" || ! jq -e --arg t "$outbounds_tag" '[.[]|select(.tag==$t)]|length==1' <<<"$outbounds_snapshot" >/dev/null; then fail "出站名称无效或重复。"; return 1; fi
    [[ $(config_jq -c '.outbounds // []') == "$outbounds_snapshot" ]] || { fail "出站列表已变化，请重新选择。"; return 1; }
    printf -v "$destination" '%s' "$outbounds_tag"
}

import_outbound_wizard_locked() (
    umask 077
    local mode=$1 file work value line
    work=$(mktemp -d "$STATE_DIR/outbound-paste.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    case "$mode" in
        file) prompt file "出站 JSON/链接文本文件绝对路径" || exit 1 ;;
        link)
            prompt_secret value "粘贴分享链接（输入不回显）" || exit 1
            printf '%s\n' "$value" >"$work/import"; file="$work/import" ;;
        json)
            [[ -t 0 ]] || exit 1
            info "粘贴 outbound JSON（输入不回显），单独一行 END 结束；Ctrl+D 取消。"
            file="$work/import"; : >"$file"
            while true; do
                IFS= read -r -s line || exit 1
                [[ $line != END ]] || break
                printf '%s\n' "$line" >>"$file"
                [[ $(stat -c '%s' "$file") -le 1048576 ]] || { fail "内容超过 1 MiB。"; exit 1; }
            done ;;
        *) exit 1 ;;
    esac
    add_outbounds_file_locked "$file"
)

add_outbound_wizard_locked() (
    umask 077
    local choice kind outbound_name server="" port=443 credential="" password="" user="" method="" security=none transport=tcp
    local tls_name="" key="" sid="" flow="" path="" host="" cc=cubic obfs="" work
    require_core && safe_paths || exit 1
    printf '\n添加出站\n  1. VLESS\n  2. VMess\n  3. Hysteria 2\n  4. TUIC\n  5. AnyTLS\n  6. Shadowsocks\n  7. Trojan\n  8. SOCKS5\n  9. HTTP / HTTPS\n 10. 直连\n  0. 返回\n'
    prompt choice "选择出站协议" 1 || exit 1
    case "$choice" in 0) exit 0;; 1) kind=vless;; 2) kind=vmess;; 3) kind=hysteria2;; 4) kind=tuic;; 5) kind=anytls;; 6) kind=shadowsocks;; 7) kind=trojan;; 8) kind=socks;; 9) kind=http;; 10) kind=direct;; *) fail "无效协议。"; exit 1;; esac
    prompt outbound_name "出站名称（字母/数字/短横线/下划线）" "out-$kind-$(date +%H%M%S)" || exit 1
    valid_tag "$outbound_name" || { fail "出站名称无效。"; exit 1; }
    if [[ $kind != direct ]]; then
        prompt server "远端服务器 IP/域名（不含协议前缀）" || exit 1
        prompt port "远端端口" 443 || exit 1
        if ! valid_host "$server" || ! valid_port "$port"; then fail "远端地址或端口无效。"; exit 1; fi
        port=$((10#$port))
        case "$kind" in
            vless|vmess|tuic) prompt_secret credential "远端已有用户 UUID" || exit 1 ;;
            socks|http)
                prompt user "用户名（无认证则回车）" || exit 1
                [[ -z $user ]] || prompt_secret password "远端密码" || exit 1 ;;
            shadowsocks)
                prompt method "加密方法" 2022-blake3-aes-128-gcm || exit 1
                prompt_secret password "远端已有密码/密钥" || exit 1 ;;
            *) prompt_secret password "远端已有密码" || exit 1 ;;
        esac
        [[ $kind != tuic ]] || prompt_secret password "TUIC 远端已有密码" || exit 1
        case "$kind" in
            vless|vmess|trojan)
                prompt choice "传输：1 TCP，2 WebSocket，3 gRPC，0 取消" 1 || exit 1
                case "$choice" in 1) transport=tcp;; 2) transport=ws;; 3) transport=grpc;; 0) exit 0;; *) fail "无效传输。"; exit 1;; esac
                if [[ $transport == ws ]]; then
                    prompt path "WebSocket 路径" / || exit 1
                    prompt host "WebSocket Host（可留空）" || exit 1
                elif [[ $transport == grpc ]]; then prompt path "gRPC 服务名" TunService || exit 1; fi ;;
        esac
        case "$kind" in
            hysteria2|tuic|anytls|trojan) security=tls ;;
            vless)
                prompt choice "安全：1 无 TLS，2 TLS，3 REALITY，0 取消" 2 || exit 1
                case "$choice" in 1) security=none;; 2) security=tls;; 3) security=reality;; 0) exit 0;; *) exit 1;; esac ;;
            vmess|http)
                prompt choice "安全：1 无 TLS，2 TLS，0 取消" 2 || exit 1
                case "$choice" in 1) security=none;; 2) security=tls;; 0) exit 0;; *) exit 1;; esac ;;
        esac
        if [[ $security != none ]]; then
            prompt tls_name "远端 TLS SNI/服务器名称" "$server" || exit 1
            valid_host "$tls_name" || { fail "TLS SNI 无效。"; exit 1; }
            if [[ $security == reality ]]; then
                prompt key "REALITY 远端公钥" || exit 1
                prompt sid "REALITY short ID（可留空）" || exit 1
                [[ $key =~ ^[a-zA-Z0-9_-]{43}$ && $sid =~ ^([a-fA-F0-9]{2}){0,8}$ ]] || { fail "REALITY 公钥或 short ID 无效。"; exit 1; }
                if [[ $transport == tcp ]]; then
                    prompt choice "Vision：1 开启，2 不开启，0 取消" 1 || exit 1
                    case "$choice" in 1) flow=xtls-rprx-vision;; 2) ;; 0) exit 0;; *) exit 1;; esac
                fi
            fi
        elif [[ $transport == grpc ]]; then fail "gRPC 模板需要 TLS 或 REALITY。"; exit 1; fi
        if [[ $kind == hysteria2 ]]; then prompt_hysteria2_obfs obfs || exit 1; fi
        if [[ $kind == tuic ]]; then
            prompt choice "QUIC 拥塞控制：1 BBR，2 CUBIC，3 NEW_RENO，0 取消" 1 || exit 1
            case "$choice" in 1) cc=bbr;; 2) cc=cubic;; 3) cc=new_reno;; 0) exit 0;; *) exit 1;; esac
        fi
    fi
    work=$(mktemp -d "$STATE_DIR/outbound-wizard.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    jq -n --arg type "$kind" --arg tag "$outbound_name" --arg server "$server" --argjson port "$port" \
      --arg id "$credential" --arg password "$password" --arg user "$user" --arg method "$method" \
      --arg security "$security" --arg transport "$transport" --arg sni "$tls_name" --arg key "$key" \
      --arg sid "$sid" --arg flow "$flow" --arg path "$path" --arg host "$host" --arg cc "$cc" --arg obfs "$obfs" '
        {type:$type,tag:$tag} |
        if $type!="direct" then .server=$server|.server_port=$port else . end |
        if $type=="vless" or $type=="vmess" or $type=="tuic" then .uuid=$id else . end |
        if $type=="vmess" then .security="auto"|.alter_id=0 else . end |
        if $type=="hysteria2" or $type=="tuic" or $type=="anytls" or $type=="trojan" or $type=="shadowsocks" then .password=$password else . end |
        if $type=="socks" then .version="5" else . end |
        if ($type=="socks" or $type=="http") and $user!="" then .username=$user|.password=$password else . end |
        if $type=="shadowsocks" then .method=$method else . end |
        if $type=="tuic" then .congestion_control=$cc else . end |
        if $obfs!="" then .obfs={type:"salamander",password:$obfs} else . end |
        if $transport=="ws" then .transport={type:"ws",path:$path} | if $host!="" then .transport.headers={Host:$host} else . end
        elif $transport=="grpc" then .transport={type:"grpc",service_name:$path} else . end |
        if $security!="none" then .tls={enabled:true,server_name:$sni} |
          if $transport=="grpc" then .tls.alpn=["h2"] else . end |
          if $security=="reality" then .tls.reality={enabled:true,public_key:$key,short_id:$sid}|.tls.utls={enabled:true,fingerprint:"chrome"} else . end
        else . end |
        if $flow!="" then .flow=$flow else . end' >"$work/node.json" || exit 1
    add_outbounds_file_locked "$work/node.json"
)

outbounds_menu() {
    local choice route_inbound route_target
    [[ $# -eq 0 ]] || { fail "用法：sing-box outbounds"; return 1; }
    [[ -t 0 ]] || { list_outbounds; return; }
    while true; do
        printf '\n出站节点管理\n  1. 出站列表和入站绑定\n  2. 按协议添加出站\n  3. 粘贴分享链接导入\n  4. 从 JSON/链接文件导入\n  5. 粘贴 outbound JSON 导入\n  6. 选择入站并绑定默认出站\n  7. 取消入站默认出站绑定\n  8. 删除出站\n  9. 选择节点、用户并绑定出站\n 10. 解除单用户出站绑定\n 11. 查看单用户绑定\n  0. 返回\n'
        read -r -p "请选择出站操作 [0]: " choice || return 0
        case "$choice" in
            0|"") return 0 ;;
            1) list_outbounds || true ;;
            2) with_lock add_outbound_wizard_locked || true ;;
            3) with_lock import_outbound_wizard_locked link || true ;;
            4) with_lock import_outbound_wizard_locked file || true ;;
            5) with_lock import_outbound_wizard_locked json || true ;;
            6|7)
                if select_inbound route_inbound "设置出站" && [[ -n $route_inbound ]]; then
                    route_target=-
                    if [[ $choice == 7 ]] || { select_outbound route_target && [[ -n $route_target ]]; }; then
                        with_lock bind_outbound_locked "$route_inbound" "$route_target" || true
                    fi
                fi ;;
            8) if select_outbound route_target && [[ -n $route_target ]]; then with_lock delete_outbound_locked "$route_target" || true; fi ;;
            9) with_lock user_outbound_wizard_locked bind || true ;;
            10) with_lock user_outbound_wizard_locked unbind || true ;;
            11) list_user_outbound_bindings || true ;;
            *) warn "请输入 0–11。" ;;
        esac
        read -r -p "按回车键返回出站管理菜单..." choice || return 0
    done
}

# shellcheck disable=SC2016 # json_query filters use literal jq variables.
generate_outbound_preference() {
    [[ $# -eq 2 ]] || return 1
    case "$2" in prefer_ipv6|prefer_ipv4) ;; *) fail "仅支持 prefer_ipv6 或 prefer_ipv4。"; return 1;; esac
    json_query "$1" --arg strategy "$2" '
      if type != "object" or ((.dns // {}) | type) != "object" or
         ((.route // {}) | type) != "object" or ((.dns.servers // []) | type) != "array"
      then error("invalid DNS/route structure") else . end |
      .dns = (.dns // {}) | .route = (.route // {}) |
      if ((.dns.servers // []) | length) == 0 then
        .dns.servers = [{type:"local",tag:"local"}] else . end |
      (.route.default_domain_resolver // {}) as $old |
      (if ($old | type) == "string" then {server:$old}
       elif ($old | type) == "object" then $old
       else error("invalid default resolver") end) as $resolver |
      ($resolver.server // "") as $explicit |
      (if $explicit != "" then $explicit
       elif (.dns.final // "") != "" then .dns.final
       else .dns.servers[0].tag end) as $server |
      if ($server | type) != "string" or $server == "" or
         ([.dns.servers[] | select(.tag == $server)] | length) != 1
      then error("unresolved DNS server tag") else . end |
      .dns.strategy = $strategy |
      .route.default_domain_resolver = ($resolver + {server:$server,strategy:$strategy})
    ' 2>/dev/null || { fail "DNS/路由配置无效，或默认 DNS 引用不明确；请先为 DNS 服务器设置唯一 tag。"; return 1; }
}

outbound_preference_label() {
    case "$1" in
        prefer_ipv6) printf '优先 IPv6' ;;
        prefer_ipv4) printf '优先 IPv4' ;;
        ipv6_only) printf '仅 IPv6（自定义）' ;;
        ipv4_only) printf '仅 IPv4（自定义）' ;;
        "") printf '未设置（沿用默认）' ;;
        *) printf '自定义策略' ;;
    esac
}

show_outbound_preference() {
    require_core || return
    local dns_strategy route_strategy
    dns_strategy=$(config_jq -r '.dns.strategy // ""') || return
    route_strategy=$(config_jq -r '
      .route.default_domain_resolver | if type == "object" then .strategy // "" else "" end') || return
    printf 'DNS 默认解析：%s\n默认出站解析：%s\n' \
        "$(outbound_preference_label "$dns_strategy")" "$(outbound_preference_label "$route_strategy")"
    info "这是域名解析的默认优先级；独立规则、出站解析器及固定 IP 连接保持各自设置。"
}

check_ipv6_egress() {
    local url response address
    if ! have curl || ! have python3; then
        fail "检测 IPv6 公网出站需要 curl 和 python3，已保留原设置。"; return 1
    fi
    info "正在检测 IPv6 公网出站（HTTPS 直连，最多约 16 秒）..."
    for url in https://api64.ipify.org https://icanhazip.com; do
        if ! response=$(curl -q --noproxy '*' -6 --proto '=https' --fail --silent --show-error \
            --connect-timeout 4 --max-time 8 --max-filesize 128 "$url" 2>/dev/null); then
            continue
        fi
        if address=$(python3 - "$response" 2>/dev/null <<'PY_IPV6_EGRESS'
import ipaddress
import sys
try:
    source = sys.argv[1].strip()
    address = ipaddress.ip_address(source)
    if (address.version != 6 or not address.is_global or address.is_multicast or
            address.ipv4_mapped is not None or "%" in source):
        sys.exit(1)
    print(str(address))
except ValueError:
    sys.exit(1)
PY_IPV6_EGRESS
        ); then
            info "IPv6 公网出站检测通过：$address"; return 0
        fi
    done
    fail "未能确认 IPv6 公网出站可用（可能是网络、DNS 或检测站点不可达），已保留原设置。"
}

configure_outbound_preference_locked() (
    umask 077
    local strategy=${1:-} work backup
    [[ $# -eq 1 ]] || exit 1
    require_core && safe_paths || exit 1
    work=$(mktemp -d "$STATE_DIR/ip-preference.XXXXXX") || exit 1
    trap 'rm -rf -- "$work"' EXIT
    cp -- "$CONFIG_FILE" "$work/original.json" || exit 1
    generate_outbound_preference "$work/original.json" "$strategy" >"$work/candidate.json" || exit 1
    if [[ $(json_query "$work/original.json" -Sc .) == "$(json_query "$work/candidate.json" -Sc .)" ]]; then
        info "默认出站已是$(outbound_preference_label "$strategy")，无需修改或重启。"; exit 0
    fi
    validate_config "$work/candidate.json" >/dev/null || exit 1
    info "设置为$(outbound_preference_label "$strategy")；保留 DNS 服务器和路由规则，另一地址族仍可回退。"
    info "运行中的服务将短暂重启；停止状态和开机自启设置保留。IPv6 需要服务器具备可用的 IPv6 网络。"
    info "独立 DNS/路由规则、outbound.domain_resolver 等显式设置可能覆盖默认策略。"
    confirm "保存出站 IP 优先级？（自动备份）" || exit 1
    if [[ $strategy == prefer_ipv6 ]]; then check_ipv6_egress || exit 1; fi
    cmp -s "$work/original.json" "$CONFIG_FILE" || { fail "配置已被其他操作修改，请重新设置。"; exit 1; }
    backup=$(create_backup) || exit 1
    commit_config "$work/candidate.json" || exit 1
    info "出站 IP 优先级已保存。成套备份：$backup"
)

outbound_preference_menu() {
    local choice
    while true; do
        printf '\n出站 IP 优先级\n'
        show_outbound_preference || true
        printf '\n  1. 开启 IPv6 优先\n  2. 关闭 IPv6 优先（改为 IPv4 优先）\n  3. 查看当前设置\n  0. 返回\n'
        read -r -p "请选择出站策略 [0]: " choice || return 0
        case "$choice" in
            1) with_lock configure_outbound_preference_locked prefer_ipv6 || true ;;
            2) with_lock configure_outbound_preference_locked prefer_ipv4 || true ;;
            3) show_outbound_preference || true ;;
            0|"") return 0 ;;
            *) warn "请输入 0–3。" ;;
        esac
        read -r -p "按回车键返回出站设置菜单..." choice || return 0
    done
}

set_outbound_preference() {
    [[ $# -le 1 ]] || { fail "用法：sing-box ip-preference [ipv6|ipv4|status]"; return 1; }
    case "${1:-}" in
        "") outbound_preference_menu ;;
        ipv6) with_lock configure_outbound_preference_locked prefer_ipv6 ;;
        ipv4) with_lock configure_outbound_preference_locked prefer_ipv4 ;;
        status) show_outbound_preference ;;
        *) fail "用法：sing-box ip-preference [ipv6|ipv4|status]" ;;
    esac
}

show_status() {
    local active enabled version="未安装"
    local green="" red="" yellow="" dim="" reset=""
    if [[ -t 1 && ${TERM:-dumb} != dumb && -z ${NO_COLOR+x} ]]; then
        green=$'\033[32m' red=$'\033[31m' yellow=$'\033[33m' dim=$'\033[90m' reset=$'\033[0m'
    fi
    [[ ! -x "$CORE_BIN" ]] || version=$("$CORE_BIN" version 2>/dev/null | head -n 1)
    active=$(systemctl is-active "$SERVICE" 2>/dev/null) || active=${active:-unknown}
    enabled=$(systemctl is-enabled "$SERVICE" 2>/dev/null) || enabled=${enabled:-unknown}
    case "$active" in
        active) active="${green}●${reset} 运行中 (active)" ;;
        inactive) active="${dim}●${reset} 已停止 (inactive)" ;;
        failed) active="${red}●${reset} 运行失败 (failed)" ;;
        activating) active="${yellow}●${reset} 启动中 (activating)" ;;
        deactivating) active="${yellow}●${reset} 停止中 (deactivating)" ;;
        reloading) active="${yellow}●${reset} 重新加载中 (reloading)" ;;
        *) active="${yellow}●${reset} 未知状态 ($active)" ;;
    esac
    case "$enabled" in
        enabled) enabled="${green}●${reset} 已启用 (enabled)" ;;
        disabled) enabled="${red}●${reset} 未启用 (disabled)" ;;
        enabled-runtime) enabled="${yellow}●${reset} 临时启用 (enabled-runtime)" ;;
        masked|masked-runtime) enabled="${red}●${reset} 已屏蔽 ($enabled)" ;;
        static|indirect|alias|linked|linked-runtime|generated|transient) enabled="${dim}●${reset} $enabled" ;;
        *) enabled="${yellow}●${reset} 未知状态 ($enabled)" ;;
    esac
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
        printf '\n  1. 安装 sing-box\n  2. 卸载 sing-box\n  3. 启动\n  4. 开机自启（开启/关闭）\n  5. 重新启动\n  6. 禁用（停止 + 关闭开机自启）\n  7. 查看日志\n  8. 添加配置\n  9. 查看配置\n 10. 更新 sing-box 核心\n 11. 修改配置\n 12. 删除配置\n 13. 备份、恢复、批量导出和诊断\n 14. 更新面板脚本\n 15. BBR 设置（TCP / UDP QUIC）\n 16. 关闭 TCP BBR（恢复开启前设置）\n 17. 安装/打开 ACME 证书菜单\n 18. 入站多用户管理\n 19. 出站 IP 优先级（IPv6 / IPv4）\n 20. 出站节点管理（添加/导入/入站绑定）\n  0. 退出\n\n'
        read -r -p "请输入选项 [0-20]: " choice || return 0
        case "$choice" in
            1) install_core || true ;;
            2) uninstall_menu || true; is_managed || return 0 ;;
            3) with_lock service_action start || true ;;
            4) autostart_menu || true; continue ;;
            5) with_lock service_action restart || true ;;
            6) with_lock service_action disable || true ;;
            7) show_logs || true ;;
            8) with_lock add_wizard_locked || true ;;
            9) show_config || true; continue ;;
            10) update_core || true ;;
            11) with_lock edit_wizard_locked || true ;;
            12) with_lock delete_inbound || true ;;
            13) management_menu || true; continue ;;
            14)
                if update_panel; then
                    # update_panel returns after its with_lock subshell has released fd 9.
                    exec bash "$WRAPPER" menu
                fi
                ;;
            15) bbr_menu || true; continue ;;
            16) disable_bbr || true ;;
            17) open_acme_menu || true ;;
            18) users_menu || true; continue ;;
            19) outbound_preference_menu || true; continue ;;
            20) outbounds_menu || true; continue ;;
            0) return 0 ;;
            *) warn "请输入 0–20。" ;;
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
  sing-box update-panel           更新面板脚本到 GitHub 最新版，更新前自动备份
  sing-box bbr                    确认后开启系统 TCP BBR，备份并持久化
  sing-box bbr-disable            确认后恢复本面板开启 BBR 前的参数及配置
  sing-box bbr-status             只读查看 TCP 拥塞控制及默认队列规则
  sing-box quic-bbr [TAG]         为 Hy2/TUIC 节点启用 QUIC BBR，先备份再按需重启
  sing-box quic-bbr-status        只读查看 Hy2/TUIC 的 QUIC 拥塞控制状态
  sing-box quic-bbr-disable [TAG] 恢复节点开启 QUIC BBR 前的设置
  sing-box quic-bbr-record TAG FILE 核对指定旧成套备份并登记节点恢复记录
  sing-box outbounds              出站列表/管理菜单（非交互时只读列表）
  sing-box outbound-add FILE      从出站 JSON 或分享链接文本文件追加出站
  sing-box outbound-bind IN OUT|- 绑定入站至出站，- 取消面板绑定
  sing-box outbound-user-bind IN INDEX OUT|- 绑定指定入站的第 INDEX 个用户，- 解除绑定
  sing-box outbound-delete TAG    删除未引用的出站（自动备份）
  sing-box ip-preference [ipv6|ipv4|status] 设置或查看默认出站 IP 优先级
  sing-box acme                   安装/打开 acme-script 证书管理菜单
  sing-box uninstall              选择仅卸载核心或彻底卸载
  sing-box uninstall --purge      彻底卸载（列出清单，输入 DELETE 确认）
  sing-box start | restart        启动 / 重新启动
  sing-box enable                 设置开机自启（不改变当前运行状态）
  sing-box disable-autostart      关闭开机自启（不改变当前运行状态）
  sing-box disable                停止服务并关闭开机自启
  sing-box stop                   仅停止服务
  sing-box status                 查看状态
  sing-box log [--follow]         最近 100 行日志 / 持续查看
  sing-box add                    添加配置向导
  sing-box edit [TAG]             修改已有配置；回车保留原值，保存前确认
  sing-box delete [TAG]           删除节点（确认、备份、引用保护和失败回滚）
  sing-box backup                 创建配置、连接信息、默认地址成套备份
  sing-box backups                列出成套备份
  sing-box restore FILE           恢复成套备份（需要确认，证书文件须另行保留）
  sing-box export FORMAT FILE     批量导出 links/base64/json，绝对路径且不覆盖文件
  sing-box diagnose               只读检查服务、监听、证书和导出；异常返回非零
  sing-box add-file /path/in.json 添加单个 inbound JSON 对象
  sing-box import /path/full.json 导入完整 JSON 配置（需要确认）
  sing-box info                   查看配置菜单
  sing-box client TAG [SERVER] [USER_INDEX|all] 按用户输出客户端 JSON 与分享链接
  sing-box users [TAG]            入站多用户管理；非交互时只显示用户列表
  sing-box user-add [TAG]         为入站添加独立认证用户（交互确认）
  sing-box user-delete [TAG]      按序号删除入站用户（保留至少一个）
  sing-box server [IP/域名|-]     设置默认连接地址；无参数打开向导，- 清除
  sing-box validate               检查实际服务配置
  sing-box format-config          格式化配置文件，保留注释和值，备份且不重启服务
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
        menu|setup|install|update|update-panel|bbr|bbr-disable|bbr-status|quic-bbr|quic-bbr-status|quic-bbr-disable|quic-bbr-record|acme|ip-preference|outbounds|outbound-add|outbound-bind|outbound-user-bind|outbound-delete|uninstall|start|restart|enable|disable-autostart|disable|stop|status|log|logs|add|edit|delete|backup|backups|restore|export|diagnose|add-file|import|info|client|users|user-add|user-delete|server|validate|format-config|remove-panel) ;;
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
        update-panel) update_panel "$@" ;;
        bbr) enable_bbr "$@" ;;
        bbr-disable) disable_bbr "$@" ;;
        bbr-status) show_bbr_status "$@" ;;
        quic-bbr) enable_quic_bbr "$@" ;;
        quic-bbr-status) show_quic_bbr_status "$@" ;;
        quic-bbr-disable) disable_quic_bbr "$@" ;;
        quic-bbr-record) record_quic_bbr "$@" ;;
        acme) open_acme_menu "$@" ;;
        ip-preference) set_outbound_preference "$@" ;;
        outbounds) outbounds_menu "$@" ;;
        outbound-add)
            [[ $# -eq 1 ]] || { fail "用法：sing-box outbound-add FILE"; return 1; }
            with_lock add_outbounds_file_locked "$1" ;;
        outbound-bind)
            [[ $# -eq 2 ]] || { fail "用法：sing-box outbound-bind IN OUT|-"; return 1; }
            with_lock bind_outbound_locked "$1" "$2" ;;
        outbound-user-bind)
            [[ $# -eq 3 ]] || { fail "用法：sing-box outbound-user-bind 入站名称 用户序号 出站名称|-"; return 1; }
            with_lock bind_user_outbound_locked "$1" "$2" "$3" ;;
        outbound-delete)
            [[ $# -eq 1 ]] || { fail "用法：sing-box outbound-delete TAG"; return 1; }
            with_lock delete_outbound_locked "$1" ;;
        menu) menu ;;
        uninstall)
            if [[ $# -eq 0 ]]; then uninstall_menu
            elif [[ $# -eq 1 && $1 == --purge ]]; then purge_uninstall
            else fail "用法：sing-box uninstall [--purge]"; return 1; fi ;;
        remove-panel) remove_panel ;;
        status) show_status ;;
        disable-autostart)
            [[ $# -eq 0 ]] || { fail "用法：sing-box disable-autostart"; return 1; }
            with_lock service_action disable-autostart ;;
        start|restart|enable|disable|stop) with_lock service_action "$command" ;;
        log|logs) show_logs "${1:-}" ;;
        add) with_lock add_wizard_locked ;;
        edit) with_lock edit_wizard_locked "$@" ;;
        delete)
            [[ $# -le 1 ]] || { fail "用法：sing-box delete [TAG]"; return 1; }
            with_lock delete_inbound "$@" ;;
        backup) with_lock create_backup ;;
        backups) list_backups ;;
        restore) with_lock restore_backup "$@" ;;
        export)
            [[ $# -eq 2 ]] || { fail "用法：sing-box export links|base64|json /path/file"; return 1; }
            with_lock export_clients "$@" ;;
        diagnose) with_lock diagnose ;;
        add-file)
            [[ $# -eq 1 ]] || { fail "用法：sing-box add-file /path/inbound.json"; return 1; }
            with_lock add_inbound_file "$1"
            ;;
        import)
            [[ $# -eq 1 ]] || { fail "用法：sing-box import /path/full.json"; return 1; }
            validate_config "$1" && confirm "替换当前完整配置？（旧配置会自动备份）" && with_lock import_config "$1"
            ;;
        info) show_config ;;
        server) with_lock set_default_server "$@" ;;
        users) users_menu "$@" ;;
        user-add) add_inbound_user "$@" ;;
        user-delete) delete_inbound_user "$@" ;;
        client)
            [[ $# -ge 1 && $# -le 3 ]] || { fail "用法：sing-box client TAG [SERVER] [USER_INDEX|all]"; return 1; }
            show_client "$1" "${2:-}" full "${3:-}"
            ;;
        validate) require_core && validate_config "$CONFIG_FILE" ;;
        format-config) format_config "$@" ;;
    esac
}

if [[ ${BASH_SOURCE[0]} == "$0" ]]; then
    main "$@"
    exit $?
fi
