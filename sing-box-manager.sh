#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# sing-box-manager: a self-contained Chinese terminal manager.
# The UI command and the upstream core deliberately use different paths.

SB_MANAGER_VERSION="1.20.0"
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
    if config_jq -e --arg tag "$tag" --argjson port "$port" \
        'any(.inbounds[]?; .tag == $tag or .listen_port == $port)' >/dev/null; then
        fail "现有配置已使用该 tag 或端口。"; exit 1
    fi
    if have ss && [[ -n $(ss -H -lntu "sport = :$port" 2>/dev/null) ]]; then
        fail "端口 $port 正被其他服务占用。"; exit 1
    fi
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
        vless-grpc-reality-vision)
            fail "Vision 不支持 gRPC 传输，请使用 TCP + REALITY + Vision。"; return 1 ;;
        *) fail "不支持的生成模板：$protocol"; return 1 ;;
    esac
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
    snapshot=$(config_jq -ce '(.inbounds // []) | if type=="array" then . else error("inbounds must be an array") end') || return 1
    count=$(jq 'length' <<<"$snapshot") || return 1
    [[ $count -gt 0 ]] || { info "暂无入站配置。"; return 0; }
    jq -r '["序号","名称","协议","端口"],
        (to_entries[] | [.key+1,.value.tag,.value.type,(.value.listen_port // "-")])
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
        current=$(config_jq -Sc '.inbounds // []') || return 1
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
    jq -er 'if .type=="shadowsocks" and .users==null then 1
      elif (.users|type)=="array" and (.users|length)>0 then .users|length
      else error("invalid users") end' "$1"
}

select_inbound_user() {
    local destination=$1 file=$2 count inbound_user_selection
    printf -v "$destination" '%s' ""
    validate_inbound_users "$file" || return 1
    count=$(inbound_user_count "$file") || return 1
    [[ $(jq -r .type "$file") != shadowsocks ]] || { fail "此 Shadowsocks 模板仅支持单密码。"; return 1; }
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
    if json_query "$snapshot" -e --arg old "$old_tag" --arg tag "$tag" --argjson port "$port" \
        'any(.inbounds[]?; .tag != $old and (.tag==$tag or .listen_port==$port))' >/dev/null; then
        fail "其他配置已使用该名称或端口。"; exit 1
    fi
    if [[ $tag != "$old_tag" ]] && json_query "$snapshot" -e --arg old "$old_tag" \
        'del(.inbounds[] | select(.tag==$old)) | any(.. | strings; .==$old)' >/dev/null; then
        fail "原名称被路由或其他配置引用，请保留名称或通过完整 JSON 同步修改引用。"; exit 1
    fi
    old_port=$(jq -r .listen_port "$work/old-inbound.json") || exit 1
    if [[ $port != "$old_port" ]] && have ss && [[ -n $(ss -H -lntu "sport = :$port" 2>/dev/null) ]]; then
        fail "端口 $port 正被其他服务占用。"; exit 1
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
         | if $record=="" then . elif $record=="null" then del(.quic_bbr_restore)
           else .quic_bbr_restore=($record|fromjson) end' "$work/metadata-before.json" >"$work/metadata.json" || exit 1
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
    credential=$(jq -jr --argjson i "$((selected_user_index - 1))" '(.users[$i].uuid // .users[$i].password // .password // "") + "."' "$work/original.json") || exit 1
    credential=${credential%.}
    password=$(jq -jr --argjson i "$((selected_user_index - 1))" '(.users[$i].password // "") + "."' "$work/original.json") || exit 1
    password=${password%.}
    extra=$(jq -r '.transport.path // .transport.service_name // ""' "$work/original.json")
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
        | if $old.users != null then .users=$old.users | .users[$i]=($old.users[$i] * $updated[0].users[0]) else . end
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
    printf '\n添加配置\n  1. VLESS + TCP + REALITY + Vision\n  2. VLESS + WebSocket（无 TLS）\n  3. VLESS TCP（无 TLS）\n  4. Shadowsocks 2022\n  5. 导入单个 inbound JSON 文件\n  6. 导入完整配置 JSON（替换当前配置）\n  7. Hysteria 2\n  8. TUIC\n  9. AnyTLS\n 10. VLESS + gRPC + TLS\n 11. VMess + TCP\n 12. VMess + TCP + TLS\n 13. VMess + WebSocket\n 14. VMess + WebSocket + TLS\n 15. VMess + gRPC + TLS\n 16. VLESS + gRPC + REALITY（不使用 Vision）\n 17. VLESS + WebSocket + TLS\n 18. VLESS + TCP + TLS\n  0. 返回\n'
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
        shadowsocks|hysteria2|anytls) credential=$("$CORE_BIN" generate rand --base64 16) || exit 1 ;;
    esac
    [[ $protocol != tuic ]] || password=$("$CORE_BIN" generate rand --base64 16) || exit 1
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
    [[ $(jq -r .type "$work/node.json") != shadowsocks ]] || { fail "此 Shadowsocks 模板仅支持单密码。"; exit 1; }
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
    [[ $type != shadowsocks ]] || { fail "此 Shadowsocks 模板仅支持单密码。"; exit 1; }
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
            info "分享链接："
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
    if config_jq -e --arg tag "$tag" --argjson port "$port" 'any(.inbounds[]?; .tag==$tag or .listen_port==$port)' >/dev/null; then
        fail "现有配置已使用该 tag 或端口。"; exit 1
    fi
    if have ss && [[ -n $(ss -H -lntu "sport = :$port" 2>/dev/null) ]]; then fail "端口已被占用。"; exit 1; fi
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
    done < <(config_jq -r '.inbounds[].tag')
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
    done < <(config_jq -c '.inbounds[]?')
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
    config_jq -r '["序号","名称","协议","监听地址","端口"],
        ((.inbounds // []) | to_entries[] | [.key+1,.value.tag,.value.type,(.value.listen // "-"),(.value.listen_port // "-")])
        | map(if type=="string" then gsub("[[:cntrl:]]"; "?") else . end) | @tsv' || return
    if ! config_jq -e '.inbounds | length > 0' >/dev/null; then
        info "暂无入站配置。"
        return 0
    fi
    info "以下分享链接包含连接凭据，可复制整行导入客户端："
    while IFS= read -r tag; do
        printf '\n[%s]\n' "$(jq -rn --arg tag "$tag" '$tag | gsub("[[:cntrl:]]"; "?")')"
        show_client "$tag" "" link-only all || warn "此配置暂时无法生成分享链接，其他配置仍会继续显示。"
    done < <(config_jq -r '.inbounds[]?.tag')
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
        printf '\n  1. 安装 sing-box\n  2. 卸载 sing-box\n  3. 启动\n  4. 开机自启（开启/关闭）\n  5. 重新启动\n  6. 禁用（停止 + 关闭开机自启）\n  7. 查看日志\n  8. 添加配置\n  9. 查看配置\n 10. 更新 sing-box 核心\n 11. 修改配置\n 12. 删除配置\n 13. 备份、恢复、批量导出和诊断\n 14. 更新面板脚本\n 15. BBR 设置（TCP / UDP QUIC）\n 16. 关闭 TCP BBR（恢复开启前设置）\n 17. 安装/打开 ACME 证书菜单\n 18. 入站多用户管理\n  0. 退出\n\n'
        read -r -p "请输入选项 [0-18]: " choice || return 0
        case "$choice" in
            1) install_core || true ;;
            2) uninstall_core || true ;;
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
            0) return 0 ;;
            *) warn "请输入 0–18。" ;;
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
  sing-box acme                   安装/打开 acme-script 证书管理菜单
  sing-box uninstall              卸载核心及服务，保留配置、备份和面板
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
        menu|setup|install|update|update-panel|bbr|bbr-disable|bbr-status|quic-bbr|quic-bbr-status|quic-bbr-disable|quic-bbr-record|acme|uninstall|start|restart|enable|disable-autostart|disable|stop|status|log|logs|add|edit|delete|backup|backups|restore|export|diagnose|add-file|import|info|client|users|user-add|user-delete|server|validate|format-config|remove-panel) ;;
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
        menu) menu ;;
        uninstall) uninstall_core ;;
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
