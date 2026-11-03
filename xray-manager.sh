#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# shellcheck disable=SC2030,SC2031 # Subshell handlers declare independent locals; shared state is committed through files.
# shellcheck disable=SC2016 # json_query/config_jq filters intentionally contain literal jq variables.
# xray-manager: a self-contained Chinese terminal manager.
# This script owns its wrapper, core, service, configuration and state.
XR_MANAGER_VERSION="1.0.0"
XR_ROOT_PREFIX="${XR_ROOT_PREFIX:-}"
LIB_DIR="${XR_ROOT_PREFIX}/usr/local/lib/xray-manager"
CORE_BIN="$LIB_DIR/xray"
WRAPPER="${XR_ROOT_PREFIX}/usr/local/bin/xray"
CONFIG_DIR="${XR_ROOT_PREFIX}/etc/xray"
CONFIG_FILE="$CONFIG_DIR/config.json"
STATE_DIR="${XR_ROOT_PREFIX}/var/lib/xray-manager"
BACKUP_DIR="$STATE_DIR/backups"
CLIENT_DIR="$STATE_DIR/clients"
DEFAULT_SERVER_FILE="$STATE_DIR/default-server"
UNIT_FILE="${XR_ROOT_PREFIX}/etc/systemd/system/xray.service"
MANAGED_MARKER="$LIB_DIR/.managed"
SERVICE="xray.service"
ACME_MENU_DIR="${XR_ROOT_PREFIX}/root/acme-script"
ACME_MENU_SCRIPT="$ACME_MENU_DIR/acme_cf_token_menu_v1.sh"
BBR_SYSCTL_DIR="${XR_ROOT_PREFIX}/etc/sysctl.d"
BBR_SYSCTL_FILE="$BBR_SYSCTL_DIR/99-xray-manager-bbr.conf"
BBR_RESTORE_FILE="$STATE_DIR/bbr-active"
SCRIPT_FILE="${BASH_SOURCE[0]}"

info(){ printf '[信息] %s\n' "$*"; }
warn(){ printf '[提示] %s\n' "$*" >&2; }
fail(){ printf '[错误] %s\n' "$*" >&2; return 1; }
have(){ command -v "$1" >/dev/null 2>&1; }
require_root(){ [[ $EUID -eq 0 ]] || fail "请使用 sudo xray 或 sudo bash $SCRIPT_FILE。"; }
require_systemd(){
    if [[ $(uname -s) != Linux || ! -d /run/systemd/system ]] || ! have systemctl; then
        fail "需要 Linux、Bash 4+ 和正在运行的 systemd。"; return 1
    fi
}
is_managed(){ [[ -f $MANAGED_MARKER && ! -L $MANAGED_MARKER && $(cat "$MANAGED_MARKER") == xray-manager-v1 ]]; }
require_core(){ [[ -x $CORE_BIN && -f $CONFIG_FILE ]] || { fail "尚未安装 Xray 核心，请先选择安装。"; return 1; }; }
safe_paths(){
    local path
    for path in "$LIB_DIR" "$CORE_BIN" "$WRAPPER" "$CONFIG_DIR" "$CONFIG_FILE" "$STATE_DIR" "$BACKUP_DIR" "$CLIENT_DIR" "$DEFAULT_SERVER_FILE" "$UNIT_FILE" "$MANAGED_MARKER"; do
        [[ ! -L $path ]] || { fail "拒绝操作符号链接：$path"; return 1; }
    done
}
check_conflicts(){
    local path fragment
    safe_paths || return
    if ! is_managed; then
        for path in "$LIB_DIR" "$CONFIG_DIR" "$WRAPPER" "$UNIT_FILE"; do
            [[ ! -e $path ]] || { fail "发现非本面板管理的 Xray 路径：$path；请先通过原安装方式处理。"; return 1; }
        done
        if [[ -d $STATE_DIR ]]; then
            [[ -f $STATE_DIR/manager.lock && -z $(find "$STATE_DIR" -mindepth 1 -maxdepth 1 ! -name manager.lock -print -quit) ]] || { fail "发现已有 Xray 状态目录，拒绝自动接管。"; return 1; }
        fi
    fi
    for path in "${XR_ROOT_PREFIX}/usr/bin/xray" "${XR_ROOT_PREFIX}/usr/sbin/xray" "${XR_ROOT_PREFIX}/usr/local/etc/xray" "${XR_ROOT_PREFIX}/lib/systemd/system/xray.service" "${XR_ROOT_PREFIX}/usr/lib/systemd/system/xray.service" "${UNIT_FILE}.d"; do
        [[ ! -e $path ]] || { fail "发现外部 Xray 安装或服务覆盖：$path；不会覆盖。"; return 1; }
    done
    fragment=$(systemctl show "$SERVICE" --property=FragmentPath --value 2>/dev/null) || fragment=""
    [[ -z $fragment || $fragment == "$UNIT_FILE" ]] || { fail "发现外部 Xray 服务文件，拒绝自动接管。"; return 1; }
}
base_config(){
    printf '%s\n' '{"log":{"loglevel":"warning"},"inbounds":[],"outbounds":[{"protocol":"freedom","tag":"direct","settings":{}}],"routing":{"rules":[]}}'
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
          else if (c=="#") { printf " "; break }
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
    r'\s+|//[^\r\n]*|\#[^\r\n]*|/\*[\s\S]*?\*/|'
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
            if not (token.isspace() or token.startswith(("//", "/*", "#"))):
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
        comment = token.startswith(("//", "/*", "#"))
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
            if token.startswith(("//", "#")):
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

validate_config(){
    local file=$1 log result=0
    [[ -f $file && -x $CORE_BIN ]] || { fail "配置文件或 Xray 核心不存在。"; return 1; }
    json_query "$file" -e 'type=="object" and ((.inbounds // [])|type=="array") and ((.outbounds // [])|type=="array")' >/dev/null || { fail "配置须为合法 JSON/JSONC 对象，inbounds/outbounds 须为数组。"; return 1; }
    log=$(mktemp "$STATE_DIR/check.XXXXXX") || return 1
    XRAY_LOCATION_ASSET="$LIB_DIR" "$CORE_BIN" run -test -config "$file" >"$log" 2>&1 || result=$?
    rm -f -- "$log"
    [[ $result -eq 0 ]] || { fail "Xray 配置校验未通过（诊断可能包含凭据，未直接输出）；请核对协议字段、TLS 和引用。"; return 1; }
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
        '{format:"xray-manager-backup-v1",config:$config[0],clients:$clients[0],
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
    jq -e '.format=="xray-manager-backup-v1" and (.config|type=="object") and
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


discover_acme_certificates() {
    python3 - "$XR_ROOT_PREFIX" <<'PY_ACME_CERTIFICATES'
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


prompt_secret() {
    local variable=$1 message=$2 default=${3:-} input
    [[ -t 0 ]] || { fail "修改认证信息需要交互终端。"; return 1; }
    IFS= read -e -r -s -p "$message: " input || { printf '\n' >&2; return 1; }
    printf '\n' >&2
    printf -v "$variable" '%s' "${input:-$default}"
}


confirm() {
    local answer
    [[ -t 0 ]] || { fail "此操作需要交互确认，请在终端运行。"; return 1; }
    read -r -p "$1 [y/N]: " answer || return 1
    [[ $answer == y || $answer == Y ]]
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
    download_https 'https://api.github.com/repos/1622663850/sing-box-manager/contents/xray-manager.sh?ref=main' \
        "$work/source.json" 60 || { fail "面板下载失败，旧版本保留。"; exit 1; }
    decode_github_file "$work/source.json" "$work/panel.sh" xray-manager.sh || exit 1
    version=$(python3 - "$work/panel.sh" <<'PY_PANEL_DOWNLOAD'
from pathlib import Path
import re
import sys

try:
    content = Path(sys.argv[1]).read_bytes()
    versions = re.findall(rb'^XR_MANAGER_VERSION="([0-9]+\.[0-9]+\.[0-9]+)"$', content, re.M)
    if (not content.startswith(b"#!/usr/bin/env bash\n") or
        b"# xray-manager: a self-contained Chinese terminal manager." not in content.splitlines() or
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
    if ! XR_ROOT_PREFIX="" timeout --kill-after=2s 15s bash "$work/panel.sh" help 9>&- </dev/null >"$work/help.log" 2>&1 ||
       ! grep -Fq 'Xray 中文终端管理面板' "$work/help.log"; then
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
    [[ $# -eq 0 ]] || { fail "用法：xray update-panel"; return 1; }
    is_managed || { fail "请先通过 setup 安装本项目面板。"; return 1; }
    require_systemd && check_conflicts && install_dependencies || return
    with_lock update_panel_locked
}


show_bbr_status() {
    [[ $# -eq 0 ]] || { fail "用法：xray bbr-status"; return 1; }
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
    python3 - "$XR_ROOT_PREFIX" "$BBR_SYSCTL_FILE" <<'PY_BBR_PERSISTENCE'
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
    [[ -f $1 && ! -L $1 && $(head -n 1 "$1") == '# Managed by xray-manager: TCP BBR' ]] &&
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
    printf '# Managed by xray-manager: TCP BBR\nnet.core.default_qdisc=fq\nnet.ipv4.tcp_congestion_control=bbr\n' >"$work/sysctl.conf"
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
    info "只影响 TCP；Hy2 的 UDP/QUIC 拥塞控制不受此设置直接控制。"
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
    [[ $# -eq 0 ]] || { fail "用法：xray bbr"; return 1; }
    if ! have sysctl; then install_dependencies || return; fi
    with_lock enable_bbr_locked
}

disable_bbr() {
    local other
    for other in "$BBR_SYSCTL_DIR"/*.conf "${XR_ROOT_PREFIX}/etc/sysctl.conf"; do
        [[ -f $other && $other != "$BBR_SYSCTL_FILE" ]] || continue
        if awk '/^[[:space:]]*net[.]ipv4[.]tcp_congestion_control[[:space:]]*=[[:space:]]*bbr([[:space:]#]|$)/ {found=1} END{exit !found}' "$other";then
            fail "其他持久化配置仍管理系统 BBR，请先通过对应管理方式恢复；未修改内核。";return 1
        fi
    done
    [[ $# -eq 0 ]] || { fail "用法：xray bbr-disable"; return 1; }
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
    for script in "$ACME_MENU_SCRIPT" "${XR_ROOT_PREFIX}/root/acme_cf_token.sh"; do
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
    [[ $# -eq 0 ]] || { fail "用法：xray acme"; return 1; }
    local script
    script=$(with_lock ensure_acme_menu_locked) || return
    info "正在打开 ACME 证书菜单，退出后返回 xray 面板。"
    # persistent-menu-v1 returns only on main-menu exit or terminal closure.
    # Actions run in child shells so their errors cannot exit the ACME menu.
    bash "$script" 9>&-
}


read_default_server(){
    [[ -e $DEFAULT_SERVER_FILE ]] || return 0
    [[ -f $DEFAULT_SERVER_FILE && ! -L $DEFAULT_SERVER_FILE ]] || { fail "默认地址文件无效。"; return 1; }
    local value; value=$(cat "$DEFAULT_SERVER_FILE") || return
    [[ -z $value ]] || valid_host "$value" || { fail "默认地址格式无效。"; return 1; }
    printf '%s' "$value"
}
set_default_server(){
    local server=${1:-} current work
    require_core && safe_paths || return
    [[ $# -le 1 ]] || return 1
    if [[ $# -eq 0 ]]; then current=$(read_default_server) || return; prompt server "默认公网 IP/域名（- 清除）" "$current" || return; fi
    if [[ $server == - ]]; then rm -f -- "$DEFAULT_SERVER_FILE"; info "默认连接地址已清除。"; return; fi
    valid_host "$server" || { fail "地址无效。"; return 1; }
    work=$(mktemp "$STATE_DIR/.default.XXXXXX") || return
    printf '%s\n' "$server" >"$work" && atomic_copy "$work" "$DEFAULT_SERVER_FILE" 600
    local result=$?; rm -f -- "$work"; return "$result"
}

write_unit(){
    cat <<EOF
[Unit]
Description=Xray managed by xray-manager
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=XRAY_LOCATION_ASSET=$LIB_DIR
WorkingDirectory=$STATE_DIR
ExecStartPre=$CORE_BIN run -test -config $CONFIG_FILE
ExecStart=$CORE_BIN run -config $CONFIG_FILE
Restart=on-failure
RestartSec=5s
LimitNOFILE=1048576
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
}

setup_panel_locked(){
    safe_paths && check_conflicts || return
    install -d -m 755 "$LIB_DIR" "$(dirname "$WRAPPER")" || return
    install -d -m 700 "$CONFIG_DIR" "$STATE_DIR" "$CLIENT_DIR" "$BACKUP_DIR" || return
    printf 'xray-manager-v1\n' >"$MANAGED_MARKER" && chmod 600 "$MANAGED_MARKER" || return
    [[ -f $CONFIG_FILE ]] || { base_config >"$CONFIG_FILE" && chmod 600 "$CONFIG_FILE"; } || return
    atomic_copy "$SCRIPT_FILE" "$WRAPPER" 755 || return
    info "独立 Xray 面板已安装，使用 sudo xray 打开。"
}
setup_panel(){ require_root && require_systemd && check_conflicts && install_dependencies && with_lock setup_panel_locked; }

download_core(){
    local requested=${1:-latest} destination=$2 arch asset api tag digest url
    case "$(uname -m)" in x86_64|amd64) arch=64;; aarch64|arm64) arch=arm64-v8a;; *) fail "只支持 amd64/arm64。"; return 1;; esac
    asset="Xray-linux-$arch.zip"
    if [[ $requested == latest ]]; then api=https://api.github.com/repos/XTLS/Xray-core/releases/latest
    else
        requested=${requested#v}
        [[ $requested =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || { fail "版本号无效。"; return 1; }
        api="https://api.github.com/repos/XTLS/Xray-core/releases/tags/v$requested"
    fi
    download_https "$api" "$destination/release.json" 60 || return
    jq -e '.draft==false and .prerelease==false' "$destination/release.json" >/dev/null || { fail "仅自动安装官方稳定发布。"; return 1; }
    tag=$(jq -er .tag_name "$destination/release.json") || return
    [[ $tag =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
    url=$(jq -er --arg name "$asset" '.assets[]|select(.name==$name)|.browser_download_url' "$destination/release.json") || return
    [[ $url == "https://github.com/XTLS/Xray-core/releases/download/$tag/$asset" ]] || { fail "核心下载地址不符合官方发布路径。"; return 1; }
    digest=$(jq -r --arg name "$asset" '.assets[]|select(.name==$name)|.digest // ""' "$destination/release.json") || return
    if [[ $digest =~ ^sha256:([a-fA-F0-9]{64})$ ]]; then digest=${BASH_REMATCH[1]}
    else
        download_https "$url.dgst" "$destination/core.dgst" 60 || return
        digest=$(python3 - "$destination/core.dgst" <<'PY_XR_DIGEST'
from pathlib import Path
import re,sys
text=Path(sys.argv[1]).read_text()
m=re.findall(r'^SHA(?:2-)?256=\s*([a-fA-F0-9]{64})\s*$',text,re.M)
if len(m)!=1: raise SystemExit(1)
print(m[0])
PY_XR_DIGEST
        ) || { fail "官方 SHA256 校验文件无效。"; return 1; }
    fi
    download_https "$url" "$destination/core.zip" || return
    printf '%s  %s\n' "$digest" "$destination/core.zip" | sha256sum --check --status || { fail "核心 SHA256 校验失败。"; return 1; }
    python3 - "$destination" <<'PY_XR_ZIP'
from pathlib import Path
import stat,sys,zipfile
root=Path(sys.argv[1])
with zipfile.ZipFile(root/'core.zip') as archive:
    for name in ['xray','geoip.dat','geosite.dat']:
        if name not in archive.namelist():
            if name=='xray':raise SystemExit(1)
            continue
        if archive.namelist().count(name)!=1:raise SystemExit(1)
        item=archive.getinfo(name)
        if stat.S_ISLNK(item.external_attr>>16) or item.file_size>200*1024*1024:raise SystemExit(1)
        content=archive.read(name)
        if name=='xray' and not content.startswith(b'\x7fELF'):raise SystemExit(1)
        path=root/name;path.write_bytes(content);path.chmod(0o755 if name=='xray' else 0o644)
PY_XR_ZIP
    [[ ${PIPESTATUS[0]} -eq 0 ]] || { fail "核心 ZIP 无效。"; return 1; }
    "$destination/xray" version >"$destination/version.txt" 2>&1 || return
    grep -q '^Xray ' "$destination/version.txt" || return 1
    printf '%s\n' "$tag" >"$destination/release-tag"
}

install_core_locked() (
    umask 077
    local requested=${1:-latest} operation=${2:-install} work backup active=0 had_core=0 had_unit=0 transaction=0 name target
    safe_paths && check_conflicts && is_managed || exit 1
    [[ $operation == update || ! -e $CORE_BIN ]] || { fail "核心已存在，请使用 update。"; exit 1; }
    [[ $operation != update || -x $CORE_BIN ]] || { fail "请先安装核心。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/core-stage.XXXXXX") || exit 1
    backup=$(mktemp -d "$BACKUP_DIR/core-$(date +%Y%m%d-%H%M%S)-XXXXXX") || exit 1
    # shellcheck disable=SC2317,SC2329 # EXIT/INT/TERM transaction cleanup.
    rollback_core(){
        local result=$? restored=1
        trap - EXIT INT TERM
        if [[ $transaction -eq 1 ]]; then
            systemctl stop "$SERVICE" || restored=0
            for name in xray geoip.dat geosite.dat; do
                target="$LIB_DIR/$name"
                if [[ -f $backup/$name ]]; then atomic_copy "$backup/$name" "$target" "$(stat -c '%a' "$backup/$name")" || restored=0
                elif [[ -f $work/$name ]]; then rm -f -- "$target" || restored=0; fi
            done
            if [[ $had_unit -eq 1 ]]; then atomic_copy "$backup/unit" "$UNIT_FILE" 644 || restored=0
            else rm -f -- "$UNIT_FILE" || restored=0; fi
            systemctl daemon-reload || restored=0
            if [[ $active -eq 1 && $had_core -eq 1 ]]; then restart_checked || restored=0; fi
            warn "核心安装/更新失败，已尝试恢复；备份：$backup（恢复结果 $restored）。"
            result=1
        fi
        rm -rf -- "$work"; exit "$result"
    }
    trap rollback_core EXIT; trap 'exit 130' INT; trap 'exit 143' TERM
    download_core "$requested" "$work" || exit 1
    XRAY_LOCATION_ASSET="$LIB_DIR" "$work/xray" run -test -config "$CONFIG_FILE" >"$work/check.log" 2>&1 || { fail "新核心不接受当前配置，未替换旧核心。"; exit 1; }
    service_is_active && active=1
    [[ ! -e $UNIT_FILE || -f $UNIT_FILE ]] || exit 1
    if [[ -f $CORE_BIN ]]; then had_core=1; fi
    for name in xray geoip.dat geosite.dat; do
        target="$LIB_DIR/$name"
        [[ ! -L $target ]] || exit 1
        [[ ! -f $target ]] || install -m "$(stat -c '%a' "$target")" "$target" "$backup/$name" || exit 1
    done
    if [[ -f $UNIT_FILE ]]; then had_unit=1; install -m 644 "$UNIT_FILE" "$backup/unit" || exit 1; fi
    transaction=1
    for name in xray geoip.dat geosite.dat; do
        [[ ! -f $work/$name ]] || atomic_copy "$work/$name" "$LIB_DIR/$name" "$(stat -c '%a' "$work/$name")" || exit 1
    done
    write_unit >"$work/unit" && atomic_copy "$work/unit" "$UNIT_FILE" 644 || exit 1
    systemctl daemon-reload || exit 1
    if [[ $active -eq 1 ]]; then restart_checked || exit 1
    elif [[ $operation == install ]]; then systemctl start "$SERVICE" && wait_healthy || exit 1; fi
    transaction=0
    info "Xray 核心已安装/更新；旧文件备份：$backup；开机自启设置保留。"
)
install_core(){
    require_root && require_systemd || return
    if ! is_managed; then setup_panel || return; fi
    install_dependencies && with_lock install_core_locked "${1:-latest}" install
}
update_core(){ install_dependencies && with_lock install_core_locked "${1:-latest}" update; }
uninstall_core() {
    is_managed && require_core || return
    confirm "停止并卸载 Xray 核心/服务？配置和面板保留，其他服务不变。" || return 1
    with_lock uninstall_core_locked
}
uninstall_core_locked(){
    local backup
    safe_paths && is_managed || return
    backup=$(mktemp -d "$BACKUP_DIR/uninstall-XXXXXX") || return
    cp -- "$CORE_BIN" "$backup/xray" && cp -- "$UNIT_FILE" "$backup/unit" || return
    systemctl disable --now "$SERVICE" || return
    rm -f -- "$CORE_BIN" "$UNIT_FILE" || return
    systemctl daemon-reload || return
    info "Xray 核心已卸载，配置、证书、备份和菜单保留。"
}
service_action(){
    local action=$1
    require_core || return
    case "$action" in
        start|restart) validate_config "$CONFIG_FILE" && systemctl "$action" "$SERVICE" && wait_healthy ;;
        enable) systemctl enable "$SERVICE" ;;
        disable-autostart) systemctl disable "$SERVICE" ;;
        stop) systemctl stop "$SERVICE" ;;
        disable) systemctl disable --now "$SERVICE" ;;
        *) return 1 ;;
    esac
}

# shellcheck disable=SC2016 # jq variables are literal filter inputs.
check_inbound_conflicts(){
    local file=$1 excluded=${2:-} config=${3:-$CONFIG_FILE} result port network flag
    result=$(json_query "$config" --slurpfile n "$file" --arg old "$excluded" '
      def nets:
        if .protocol=="hysteria" or .protocol=="hysteria2" or (.streamSettings.method // .streamSettings.network)=="hysteria" or (.streamSettings.method // .streamSettings.network)=="kcp" then ["udp"]
        elif .protocol=="shadowsocks" then if .settings.network=="tcp" then ["tcp"] elif .settings.network=="udp" then ["udp"] else ["tcp","udp"] end
        elif (.protocol as $p | (["vless","vmess","trojan","socks","http"]|index($p))!=null) then ["tcp"] else ["tcp","udp"] end;
      ($n[0]|nets) as $wanted |
      {port:$n[0].port, conflict:any(.inbounds[]?; .tag!=$old and (.tag==$n[0].tag or (.port==$n[0].port and ((nets)-$wanted|length)<(nets|length)))),
      wanted:$wanted,owned:[.inbounds[]?|select(.tag==$old and .port==$n[0].port)|nets[]]}') || return 1
    [[ $(jq -r .conflict <<<"$result") != true ]] || { fail "名称或同一 TCP/UDP 端口已使用。"; return 1; }
    have ss || return 0
    port=$(jq -r .port <<<"$result") || return 1
    for network in $(jq -r '.wanted-.owned|.[]' <<<"$result"); do
        case "$network" in tcp) flag=-lnt;; udp) flag=-lnu;; esac
        [[ -z $(ss -H "$flag" "sport = :$port" 2>/dev/null) ]] || { fail "端口 $port/$network 已被服务占用。"; return 1; }
    done
}

# shellcheck disable=SC2016 # jq bundle filters use literal variables.
add_managed_inbound() (
    umask 077
    local file=$1 server=${2:-} public_key=${3:-} work tag port
    require_core && safe_paths || exit 1
    tag=$(jq -er .tag "$file") && port=$(jq -er .port "$file") || exit 1
    if ! valid_tag "$tag" || ! valid_port "$port" || { [[ -n $server ]] && ! valid_host "$server"; }; then fail "名称、端口或地址无效。"; exit 1; fi
    check_inbound_conflicts "$file" || exit 1
    [[ ! -e $CLIENT_DIR/$tag.json && ! -L $CLIENT_DIR/$tag.json ]] || { fail "已有同名连接信息。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/add.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before.json" || exit 1
    jq --slurpfile n "$file" --arg t "$tag" --arg s "$server" --arg k "$public_key" '
      .config.inbounds+=[$n[0]] | .clients[$t]={inbound:$n[0],server:$s,public_key:$k}' "$work/before.json" >"$work/after.json" || exit 1
    apply_bundle "$work/after.json" preserve start
)
add_inbound_file() (
    umask 077
    local file=$1 work
    work=$(mktemp -d "$STATE_DIR/import-node.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    json_query "$file" . >"$work/node" || exit 1
    add_managed_inbound "$work/node" "" ""
)

# shellcheck disable=SC2016 # jq variables are literal.
replace_inbound_file() (
    umask 077
    local old=$1 file=$2 server=${3:-} public_key=${4:-} expected=${5:-} work tag port
    require_core && safe_paths || exit 1
    tag=$(jq -er .tag "$file") && port=$(jq -er .port "$file") || exit 1
    if ! valid_tag "$old" || ! valid_tag "$tag" || ! valid_port "$port"; then fail "节点名称或端口无效。"; exit 1; fi
    [[ -z $server ]] || valid_host "$server" || exit 1
    work=$(mktemp -d "$STATE_DIR/edit.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before" || exit 1
    jq -e --arg t "$old" '[.config.inbounds[]?|select(.tag==$t)]|if length==1 then .[0] else error("node missing") end' "$work/before" >"$work/original" || exit 1
    [[ -z $expected || $(jq -Sc . "$expected") == "$(jq -Sc . "$work/original")" ]] || { fail "节点已被修改，请重新编辑。"; exit 1; }
    [[ $(jq -r .protocol "$file") == "$(jq -r .protocol "$work/original")" ]] || { fail "修改不能切换协议。"; exit 1; }
    json_query "$work/before" .config >"$work/config" || exit 1
    check_inbound_conflicts "$file" "$old" "$work/config" || exit 1
    if [[ $tag != "$old" ]]; then
        [[ ! -e $CLIENT_DIR/$tag.json && ! -L $CLIENT_DIR/$tag.json ]] || { fail "新名称已有连接信息。"; exit 1; }
        if jq -e --arg t "$old" '.config|del(.inbounds[]|select(.tag==$t))|any(..|strings;.==$t)' "$work/before" >/dev/null; then fail "节点被路由引用，请保留名称。"; exit 1; fi
    fi
    jq --arg old "$old" --arg t "$tag" --arg s "$server" --arg k "$public_key" --slurpfile n "$file" '
      .config.inbounds|=map(if .tag==$old then $n[0] else . end) |
      (.clients[$old] // {}) as $m | .clients[$t]=($m+{server:$s,public_key:$k,inbound:$n[0]}) |
      if $old!=$t then del(.clients[$old]) else . end' "$work/before" >"$work/after" || exit 1
    apply_bundle "$work/after" preserve preserve
)

# shellcheck disable=SC2016 # jq variables are literal.
delete_inbound() (
    umask 077
    local tag=${1:-} work
    require_core && safe_paths || exit 1
    if [[ -z $tag ]]; then select_inbound tag "删除" || exit 1; [[ -n $tag ]] || exit 0; fi
    valid_tag "$tag" || exit 1
    config_jq -e --arg t "$tag" '[.inbounds[]?|select(.tag==$t)]|length==1' >/dev/null || { fail "节点不存在或名称重复。"; exit 1; }
    if config_jq -e --arg t "$tag" 'del(.inbounds[]|select(.tag==$t))|any(..|strings;.==$t)' >/dev/null; then fail "节点仍被路由引用，请先解除。"; exit 1; fi
    confirm "删除节点 $tag？（成套备份）" || exit 1
    work=$(mktemp -d "$STATE_DIR/delete.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before" || exit 1
    jq --arg t "$tag" '.config.inbounds|=map(select(.tag!=$t))|del(.clients[$t])' "$work/before" >"$work/after" || exit 1
    apply_bundle "$work/after"
)

# shellcheck disable=SC2016 # jq variables are literal.
bind_outbound_locked() (
    umask 077
    local selected_in=$1 selected_out=$2 work
    require_core && safe_paths || exit 1
    if ! valid_tag "$selected_in" || { [[ $selected_out != - ]] && ! valid_tag "$selected_out"; }; then fail "名称无效。"; exit 1; fi
    work=$(mktemp -d "$STATE_DIR/bind.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before" || exit 1
    jq --arg i "$selected_in" --arg o "$selected_out" '
      if ([.config.inbounds[]?|select(.tag==$i)]|length)!=1 then error("inbound missing") else . end |
      if $o!="-" and ([.config.outbounds[]?|select(.tag==$o)]|length)!=1 then error("outbound missing") else . end |
      (.clients[$i].outbound_route // null) as $r |
      if $r!=null and ($r.format!="xray-manager-outbound-route-v1" or
         $r.rule!={type:"field",inboundTag:[$i],outboundTag:$r.rule.outboundTag} or
         ([.config.routing.rules[]?|select(.==$r.rule)]|length)!=1) then error("owned rule changed") else . end |
      if $r==null and $o!="-" and any(.config.routing.rules[]?;.=={type:"field",inboundTag:[$i],outboundTag:$o}) then error("unowned duplicate") else . end |
      if $o=="-" and $r==null then . else
        .config.routing.rules=((.config.routing.rules // [])|map(select($r==null or .!=$r.rule))) |
        if $o=="-" then del(.clients[$i].outbound_route) |
          if $r.created_metadata==true and .clients[$i]=={} then del(.clients[$i]) else . end
        else {type:"field",inboundTag:[$i],outboundTag:$o} as $rule | (.clients|has($i)|not) as $created |
          .config.routing.rules=([$rule]+.config.routing.rules) |
          .clients[$i].outbound_route={format:"xray-manager-outbound-route-v1",rule:$rule,created_metadata:(if $r!=null then ($r.created_metadata // false) else $created end)}
        end end' "$work/before" >"$work/after" 2>/dev/null || { fail "节点缺失、重复或受管规则已外部修改。"; exit 1; }
    if [[ $(jq -Sc '{config,clients}' "$work/before") == "$(jq -Sc '{config,clients}' "$work/after")" ]]; then info "绑定未变化。"; exit 0; fi
    confirm "保存入站出站绑定？（优先于已有路由，自动备份并按需重启）" || exit 1
    snapshot_bundle >"$work/current" || exit 1
    [[ $(jq -Sc . "$work/current") == "$(jq -Sc . "$work/before")" ]] || { fail "配置或元数据已变化。"; exit 1; }
    apply_bundle "$work/after"
)

select_inbound(){ select_node "$1" inbounds "${2:-操作}"; }
select_outbound(){ select_node "$1" outbounds "选择出站"; }
select_node(){
    local destination=$1 collection=$2 operation=$3 snapshot selection chosen count current
    printf -v "$destination" '%s' ''
    snapshot=$(config_jq -ce --arg k "$collection" '.[$k] // []') || return
    count=$(jq length <<<"$snapshot") || return
    [[ $count -gt 0 ]] || { info "暂无可选节点。"; return 0; }
    jq -r '["序号","名称","协议","端口"],(to_entries[]|[.key+1,.value.tag,.value.protocol,(.value.port // .value.settings.port // "-")])|map(if type=="string" then gsub("[[:cntrl:]]";"?") else . end)|@tsv' <<<"$snapshot"
    prompt selection "$operation（序号/名称，纯数字名称用 name:名称；0 返回）" 0 || return
    case "$selection" in 0) return 0;; name:*) chosen=${selection#name:};; *)
        if [[ $selection =~ ^[0-9]+$ ]]; then
            if [[ ! $selection =~ ^[1-9][0-9]{0,5}$ ]] || ((selection>count)); then fail "序号无效。"; return 1; fi
            chosen=$(jq -r --argjson i "$((selection-1))" '.[$i].tag' <<<"$snapshot") || return
        else chosen=$selection; fi;; esac
    if ! valid_tag "$chosen" || ! jq -e --arg t "$chosen" '[.[]|select(.tag==$t)]|length==1' <<<"$snapshot" >/dev/null; then fail "名称无效或重复。"; return 1; fi
    current=$(config_jq -Sc --arg k "$collection" '.[$k] // []') || return
    [[ $current == "$(jq -Sc . <<<"$snapshot")" ]] || { fail "列表已变化，请重新选择。"; return 1; }
    printf -v "$destination" '%s' "$chosen"
}

list_backups(){ local file; for file in "$BACKUP_DIR"/bundle-*.json; do [[ ! -f $file || -L $file ]] || printf '%s\n' "$file"; done; }
restore_backup(){ validate_bundle "$1" && confirm "恢复完整 Xray 配置、连接信息和默认地址？（先备份）" && apply_bundle "$1" replace; }
import_config(){ validate_config "$1" && commit_config "$1" replace; }
format_config_locked() (
    umask 077
    local work backup
    require_core && safe_paths || exit 1
    work=$(mktemp -d "$STATE_DIR/format.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    cp -- "$CONFIG_FILE" "$work/before" || exit 1
    format_config_source "$CONFIG_FILE" >"$work/after" || exit 1
    if cmp -s "$work/before" "$work/after"; then info "格式已规范。"; exit 0; fi
    validate_config "$work/after" || exit 1
    backup=$(create_backup) || exit 1
    cmp -s "$work/before" "$CONFIG_FILE" && atomic_copy "$work/after" "$CONFIG_FILE" 600 || exit 1
    info "已格式化（不重启），备份：$backup"
)
format_config(){ with_lock format_config_locked; }

generate_inbound() {
    while (($# < 12)); do set -- "$@" ""; done
    if ! valid_tag "$2" || ! valid_port "$3"; then fail "名称或端口无效。";return 1;fi
    case ${1:-} in
        *-tls|trojan|hysteria)
            if declare -F validate_tls_files >/dev/null 2>&1; then
                validate_tls_files "${8:-}" "${9:-}" "${10:-}" || return
            fi
            ;;
    esac
    python3 - "$@" 2>/dev/null <<'PY_XRAY_INBOUND'
import base64, json, re, sys

p, tag, port, credential = sys.argv[1:5]
extra, private_key, short_id = sys.argv[5:8]
tls_name, certificate, tls_key = sys.argv[8:11]
password = sys.argv[11] if len(sys.argv) > 11 else ""
obfs = sys.argv[12] if len(sys.argv) > 12 else ""
uuid_re = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", tag) or not 1 <= int(port) <= 65535:
    raise SystemExit(1)
supported = {
    "vless-tcp", "vless-tcp-tls", "vless-reality", "vless-ws", "vless-ws-tls",
    "vless-grpc-tls", "vless-grpc-reality", "vless-xhttp-tls", "vless-xhttp-reality",
    "vmess-tcp", "vmess-tcp-tls", "vmess-ws", "vmess-ws-tls", "vmess-grpc-tls",
    "trojan", "shadowsocks", "hysteria",
}
if p not in supported:
    raise SystemExit(1)
def user_uuid(value):
    if not uuid_re.fullmatch(value): raise SystemExit(1)
    return value
def stream(network, security="none"):
    value = {"method": network, "security": security}
    if network == "websocket":
        if not extra.startswith("/"): raise SystemExit(1)
        value["wsSettings"] = {"path": extra, "headers": {}}
    elif network == "grpc":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", extra): raise SystemExit(1)
        value["grpcSettings"] = {"serviceName": extra}
    elif network == "xhttp":
        if not extra.startswith("/"): raise SystemExit(1)
        value["xhttpSettings"] = {"path": extra, "mode": "auto"}
    if security == "tls":
        if not tls_name or not certificate or not tls_key: raise SystemExit(1)
        alpn = ["h2"] if network == "grpc" else (["http/1.1"] if network == "websocket" else ["h2", "http/1.1"])
        value["tlsSettings"] = {"serverName": tls_name, "alpn": alpn, "certificates": [{"certificateFile": certificate, "keyFile": tls_key}]}
    elif security == "reality":
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", private_key) or not re.fullmatch(r"(?:[0-9a-fA-F]{2}){0,8}", short_id): raise SystemExit(1)
        if not tls_name: raise SystemExit(1)
        value["realitySettings"] = {
            "show": False, "target": f"{tls_name}:443", "xver": 0,
            "serverNames": [tls_name], "privateKey": private_key, "shortIds": [short_id],
        }
    return value
if p == "vless-grpc-reality-vision":
    raise SystemExit(1)
if p.startswith("vless-"):
    network = "raw"
    if "-ws" in p: network = "websocket"
    elif "-grpc" in p: network = "grpc"
    elif "-xhttp" in p: network = "xhttp"
    security = "none"
    if p.endswith("-tls"): security = "tls"
    elif "reality" in p: security = "reality"
    client = {"id": user_uuid(credential)}
    if p == "vless-reality": client["flow"] = "xtls-rprx-vision"
    value = {"protocol": "vless", "tag": tag, "listen": "::", "port": int(port),
             "settings": {"users": [client], "decryption": "none"},
             "streamSettings": stream(network, security)}
elif p.startswith("vmess-"):
    network = "raw"
    if "-ws" in p: network = "websocket"
    elif "-grpc" in p: network = "grpc"
    security = "tls" if p.endswith("-tls") else "none"
    client = {"id": user_uuid(credential), "alterId": 0}
    value = {"protocol": "vmess", "tag": tag, "listen": "::", "port": int(port),
             "settings": {"users": [client]}, "streamSettings": stream(network, security)}
elif p == "trojan":
    value = {"protocol": "trojan", "tag": tag, "listen": "::", "port": int(port),
             "settings": {"users": [{"password": credential}]},
             "streamSettings": stream("raw", "tls")}
elif p == "shadowsocks":
    try:
        if len(base64.b64decode(credential, validate=True)) != 16: raise SystemExit(1)
    except Exception:
        raise SystemExit(1)
    value = {"protocol": "shadowsocks", "tag": tag, "listen": "::", "port": int(port),
             "settings": {"method": "2022-blake3-aes-128-gcm", "password": credential, "network": "tcp,udp"}}
elif p == "hysteria":
    if not credential or not tls_name: raise SystemExit(1)
    value = {"protocol": "hysteria", "tag": tag, "listen": "::", "port": int(port),
             "settings": {"version": 2, "users": [{"auth": credential}]},
             "streamSettings": {"method": "hysteria", "security": "tls", "hysteriaSettings": {"version": 2},
                                "tlsSettings": {"serverName": tls_name, "certificates": [{"certificateFile": certificate, "keyFile": tls_key}]}}}
else:
    raise SystemExit(1)
if obfs:
    if p != "hysteria" or len(obfs.encode("utf-8")) < 4:raise SystemExit(1)
    value["streamSettings"]["finalmask"]={"udp":[{"type":"salamander","settings":{"password":obfs}}]}
print(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
PY_XRAY_INBOUND
}

generate_client() {
    validate_inbound_users "$1" >/dev/null 2>&1 || return 1
    python3 - "$@" 2>/dev/null <<'PY_XRAY_CLIENT'
import json, re, sys
from pathlib import Path

path, server = sys.argv[1:3]
public_key = sys.argv[3] if len(sys.argv) > 3 else ""
index = int(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else 1
value = json.loads(Path(path).read_text(encoding="utf-8"))
protocol = value.get("protocol")
port = value.get("port")
settings = value.get("settings", {})
allowed_settings = {
    "vless": {"users", "clients", "decryption"}, "vmess": {"users", "clients"},
    "trojan": {"users", "clients"}, "hysteria": {"version", "users"},
    "shadowsocks": {"method", "password", "network"},
}
if protocol not in allowed_settings or set(settings) - allowed_settings[protocol]: raise SystemExit(1)
users = settings.get("users") or settings.get("clients", [])
if not isinstance(port, int) or not 1 <= port <= 65535:
    raise SystemExit(1)
if protocol in {"vless", "vmess"}:
    ids = [u.get("id") for u in users]
    if not users or any(not isinstance(x, str) or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", x) for x in ids) or len(set(ids)) != len(ids): raise SystemExit(1)
    if protocol == "vmess" and any(int(u.get("alterId", 0)) != 0 for u in users): raise SystemExit(1)
    if any(u.get("flow") not in (None, "", "xtls-rprx-vision") for u in users): raise SystemExit(1)
elif protocol == "trojan":
    if not users or any(not isinstance(u.get("password"), str) or not u["password"] for u in users): raise SystemExit(1)
elif protocol == "hysteria":
    users = value.get("settings", {}).get("users", [])
    if not users or any(not isinstance(u.get("auth"), str) or not u["auth"] for u in users): raise SystemExit(1)
elif protocol == "shadowsocks":
    if value.get("settings", {}).get("users") is not None: raise SystemExit(1)
    password = value.get("settings", {}).get("password", "")
    try:
        decoded = __import__("base64").b64decode(password, validate=True)
    except Exception:
        raise SystemExit(1)
    if len(decoded) != 16: raise SystemExit(1)
else:
    raise SystemExit(1)
if protocol in {"vless", "vmess"}:
    if not users or not 1 <= index <= len(users): raise SystemExit(1)
    user = users[index - 1]
    if protocol == "vless":
        out = {"tag": "proxy", "protocol": "vless", "settings": {"address": server, "port": port, "id": user["id"], "encryption": "none"}}
    else:
        out = {"tag": "proxy", "protocol": "vmess", "settings": {"address": server, "port": port, "id": user["id"], "alterId": user.get("alterId", 0), "security": "auto"}}
    if user.get("flow"): out["settings"]["flow"] = user["flow"]
elif protocol == "trojan":
    users = value.get("settings", {}).get("users") or value.get("settings", {}).get("clients", [])
    if not users or not 1 <= index <= len(users): raise SystemExit(1)
    out = {"tag": "proxy", "protocol": "trojan", "settings": {"address": server, "port": port, "password": users[index-1]["password"]}}
elif protocol == "shadowsocks":
    settings = value.get("settings", {})
    out = {"tag": "proxy", "protocol": "shadowsocks", "settings": {"address": server, "port": port, "method": settings["method"], "password": settings["password"]}}
elif protocol == "hysteria":
    user = value.get("settings", {}).get("users", [])[index - 1]
    out = {"tag": "proxy", "protocol": "hysteria", "settings": {"version": 2, "address": server, "port": port}}
else:
    raise SystemExit(1)
stream = dict(value.get("streamSettings", {}))
if stream:
    method = stream.get("method", stream.get("network", "raw"))
    if method not in {"raw", "websocket", "grpc", "xhttp", "hysteria"}: raise SystemExit(1)
    if set(stream) - {"method", "network", "security", "tlsSettings", "realitySettings", "wsSettings", "grpcSettings", "xhttpSettings", "hysteriaSettings", "finalmask"}: raise SystemExit(1)
    stream["method"] = method
    stream.pop("network", None)
    if "tlsSettings" in stream:
        tls = dict(stream["tlsSettings"])
        tls.pop("certificates", None)
        if tls.get("allowInsecure", False): raise SystemExit(1)
        tls["allowInsecure"] = False
        out["streamSettings"] = {k: v for k, v in stream.items() if k not in {"tlsSettings", "realitySettings"}}
        out["streamSettings"]["tlsSettings"] = tls
    else:
        out["streamSettings"] = stream
    if stream.get("security") == "reality":
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", public_key): raise SystemExit(1)
        reality = dict(stream["realitySettings"])
        for field in ("target","dest","privateKey","serverNames","shortIds","xver","minClientVer","maxClientVer","maxTimeDiff"):
            reality.pop(field,None)
        reality["serverName"] = (stream.get("realitySettings", {}).get("serverNames") or [""])[0]
        reality["fingerprint"] = "chrome"
        reality["password"] = public_key
        reality["shortId"] = (stream.get("realitySettings", {}).get("shortIds") or [""])[0]
        out["streamSettings"]["realitySettings"] = reality
    if protocol == "hysteria":
        hs = dict(out["streamSettings"].get("hysteriaSettings", {}))
        hs["version"] = 2
        hs["auth"] = value.get("settings", {}).get("users", [])[index - 1]["auth"]
        out["streamSettings"]["hysteriaSettings"] = hs
print(json.dumps(out, ensure_ascii=False, separators=(",", ":")))
PY_XRAY_CLIENT
}

client_link() {
    python3 - "$@" 2>/dev/null <<'PY_XRAY_LINK'
import base64, json, sys
from urllib.parse import quote
from pathlib import Path
o = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
tag = quote(sys.argv[2] if len(sys.argv)>2 else o.get("tag","proxy"), safe="")
protocol = o["protocol"]; settings = o.get("settings", {}); stream = o.get("streamSettings", {})
method = stream.get("method", stream.get("network", "raw")); security = stream.get("security", "none")
def address(value): return "[" + value + "]" if ":" in value and not value.startswith("[") else value
if method not in {"raw", "websocket", "grpc", "xhttp", "hysteria"} or security not in {"none", "tls", "reality"}:
    raise SystemExit(1)
def common(q):
    if security == "reality":
        reality = stream.get("realitySettings", {})
        q += "&sni=" + quote(reality.get("serverName", ""), safe="")
        q += "&fp=" + quote(reality.get("fingerprint", "chrome"), safe="")
        q += "&pbk=" + quote(reality.get("password", reality.get("publicKey", "")), safe="")
        q += "&sid=" + quote(reality.get("shortId", ""), safe="")
        if settings.get("flow"): q += "&flow=" + quote(settings["flow"], safe="")
    elif security == "tls":
        tls = stream.get("tlsSettings", {})
        q += "&sni=" + quote(tls.get("serverName", ""), safe="")
        if tls.get("alpn"): q += "&alpn=" + quote(",".join(tls["alpn"]), safe="")
        if tls.get("fingerprint"): q += "&fp=" + quote(tls["fingerprint"], safe="")
    return q
def transport(q):
    if method == "websocket":
        ws = stream.get("wsSettings", {})
        q += "&path=" + quote(ws.get("path", ""), safe="")
        host = ws.get("headers", {}).get("Host", "")
        if host: q += "&host=" + quote(host, safe="")
    elif method == "grpc":
        q += "&serviceName=" + quote(stream.get("grpcSettings", {}).get("serviceName", ""), safe="") + "&mode=gun"
    elif method == "xhttp":
        x = stream.get("xhttpSettings", {})
        q += "&path=" + quote(x.get("path", ""), safe="") + "&mode=" + quote(x.get("mode", "auto"), safe="")
    return q
if protocol == "vless":
    q = common("encryption=none&type=" + {"raw":"tcp","websocket":"ws","grpc":"grpc","xhttp":"xhttp"}.get(method, method) + "&security=" + security)
    print(f"vless://{settings['id']}@{address(settings['address'])}:{settings['port']}?{transport(q)}#{tag}")
elif protocol == "vmess":
    payload = {"v":"2","ps":sys.argv[2],"add":settings["address"],"port":str(settings["port"]),"id":settings["id"],"aid":str(settings.get("alterId",0)),"net":{"raw":"tcp","websocket":"ws","grpc":"grpc","xhttp":"xhttp"}.get(method,method),"type":"none"}
    payload["tls"] = "tls" if security == "tls" else ""
    if security == "tls":
        payload["sni"] = stream.get("tlsSettings", {}).get("serverName", "")
        payload["alpn"] = ",".join(stream.get("tlsSettings", {}).get("alpn", []))
    if method == "websocket":
        payload["path"] = stream.get("wsSettings", {}).get("path", "")
        payload["host"] = stream.get("wsSettings", {}).get("headers", {}).get("Host", "")
    elif method == "grpc":
        payload["type"] = "gun"; payload["path"] = stream.get("grpcSettings", {}).get("serviceName", "")
    print("vmess://" + base64.b64encode(json.dumps(payload,separators=(",",":")).encode()).decode())
elif protocol == "trojan":
    q = transport(common("security=tls"))
    print(f"trojan://{quote(settings['password'], safe='')}@{address(settings['address'])}:{settings['port']}?{q}#{tag}")
elif protocol == "shadowsocks":
    print(f"ss://{quote(settings['method'], safe='')}:{quote(settings['password'], safe='')}@{address(settings['address'])}:{settings['port']}#{tag}")
elif protocol == "hysteria":
    hs = stream.get("hysteriaSettings", {})
    masks = stream.get("finalmask", {}).get("udp", [])
    if masks:
        if len(masks)!=1 or masks[0].get("type")!="salamander" or not masks[0].get("settings",{}).get("password"): raise SystemExit(1)
    q = "sni=" + quote(stream.get("tlsSettings", {}).get("serverName", ""), safe="")
    if stream.get("tlsSettings", {}).get("alpn"): q += "&alpn=" + quote(",".join(stream["tlsSettings"]["alpn"]), safe="")
    if masks: q += "&obfs=salamander&obfs-password=" + quote(masks[0]["settings"]["password"], safe="")
    print(f"hysteria2://{quote(hs.get('auth', settings.get('auth','')), safe='')}@{address(settings['address'])}:{settings['port']}?{q}#{tag}")
else:
    raise SystemExit(1)
PY_XRAY_LINK
}

detect_inbound_template() {
    python3 - "$1" 2>/dev/null <<'PY_XRAY_DETECT'
import json, sys
from pathlib import Path
v=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); p=v.get("protocol",""); s=v.get("streamSettings",{})
network=s.get("network", s.get("method","raw")); security=s.get("security","none")
label = {"raw": "tcp", "websocket": "ws", "grpc": "grpc", "xhttp": "xhttp", "hysteria": "hysteria"}.get(network, network)
if p=="vless":
    if security=="reality" and label=="tcp": print("vless-reality"); raise SystemExit
    print("vless-"+label+"-"+security if security!="none" else "vless-"+label); raise SystemExit
if p=="vmess": print("vmess-"+label+"-"+security if security!="none" else "vmess-"+label); raise SystemExit
if p=="trojan": print("trojan"); raise SystemExit
if p=="shadowsocks": print("shadowsocks"); raise SystemExit
if p=="hysteria": print("hysteria"); raise SystemExit
raise SystemExit(1)
PY_XRAY_DETECT
}

validate_inbound_users() {
    python3 - "$@" 2>/dev/null <<'PY_XRAY_USERS'
import base64, json, re, sys
from pathlib import Path
v=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); p=v.get("protocol"); settings=v.get("settings",{})
allowed={"vless":{"users","clients","decryption"},"vmess":{"users","clients"},"trojan":{"users","clients"},"hysteria":{"version","users"},"shadowsocks":{"method","password","network"}}
if p not in allowed or set(settings)-allowed[p]: raise SystemExit(1)
users=settings.get("users") if settings.get("users") is not None else settings.get("clients")
if p == "shadowsocks":
    if users is not None: raise SystemExit(1)
    try: valid = len(base64.b64decode(settings.get("password", ""), validate=True)) == 16
    except Exception: valid = False
    raise SystemExit(0 if valid else 1)
if p in {"vless","vmess"}:
    if not isinstance(users,list) or not users: raise SystemExit(1)
    ids=[u.get("id") for u in users]
    if any(not isinstance(x,str) or not re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", x) for x in ids): raise SystemExit(1)
    if len(ids) != len(set(ids)): raise SystemExit(1)
    if p == "vmess" and any(int(u.get("alterId", 0)) != 0 for u in users): raise SystemExit(1)
    if any(u.get("flow") not in (None, "", "xtls-rprx-vision") for u in users): raise SystemExit(1)
elif p == "trojan":
    if not isinstance(users,list) or not users or any(not u.get("password") for u in users): raise SystemExit(1)
    if len({u["password"] for u in users}) != len(users): raise SystemExit(1)
elif p == "hysteria":
    users=settings.get("users", [])
    if not isinstance(users,list) or not users or any(not u.get("auth") for u in users): raise SystemExit(1)
else:
    raise SystemExit(1)
raise SystemExit(0)
PY_XRAY_USERS
}

inbound_user_count() { python3 - "$1" 2>/dev/null <<'PY_XRAY_COUNT'
import json,sys
from pathlib import Path
v=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8")); p=v.get("protocol"); settings=v.get("settings",{})
if p == "shadowsocks" and settings.get("users") is None:
    try:
        import base64
        print(1 if len(base64.b64decode(settings.get("password", ""), validate=True)) == 16 else 0)
    except Exception: print(0)
else:
    u=settings.get("users") if settings.get("users") is not None else settings.get("clients",[])
    print(len(u) if isinstance(u,list) else 0)
PY_XRAY_COUNT
}

make_reality_pair(){
    local value private public
    if [[ -n ${1:-} ]]; then value=$("$CORE_BIN" x25519 -i "$1") || return
    else value=$("$CORE_BIN" x25519) || return; fi
    private=$(sed -n 's/^PrivateKey: *//p' <<<"$value")
    public=$(sed -n 's/^Password (PublicKey): *//p' <<<"$value")
    [[ -n $public ]] || public=$(sed -n 's/^PublicKey: *//p' <<<"$value")
    [[ $private =~ ^[a-zA-Z0-9_-]{43}$ && $public =~ ^[a-zA-Z0-9_-]{43}$ ]] || { fail "无法解析 Xray X25519 密钥（Hash32 不是公钥）。"; return 1; }
    jq -n --arg private "$private" --arg public "$public" '{private:$private,public:$public}'
}
choose_template(){
    local destination=$1 protocol_choice
    printf '\n添加 Xray 入站\n  1. VLESS RAW + REALITY + Vision\n  2. VLESS RAW\n  3. VLESS RAW + TLS\n  4. VLESS WebSocket\n  5. VLESS WebSocket + TLS\n  6. VLESS gRPC + TLS\n  7. VLESS gRPC + REALITY（无 Vision）\n  8. VLESS XHTTP + TLS\n  9. VLESS XHTTP + REALITY\n 10. VMess RAW\n 11. VMess RAW + TLS\n 12. VMess WebSocket\n 13. VMess WebSocket + TLS\n 14. VMess gRPC + TLS\n 15. Trojan + TLS\n 16. Shadowsocks 2022\n 17. Hysteria 2\n 18. 导入单个 inbound JSON\n 19. 导入完整 Xray JSON（替换）\n  0. 返回\n'
    prompt protocol_choice "请选择协议" 1 || return
    case "$protocol_choice" in
      0) printf -v "$destination" '%s' '';; 1) printf -v "$destination" vless-reality;; 2) printf -v "$destination" vless-tcp;; 3) printf -v "$destination" vless-tcp-tls;;
      4) printf -v "$destination" vless-ws;; 5) printf -v "$destination" vless-ws-tls;; 6) printf -v "$destination" vless-grpc-tls;; 7) printf -v "$destination" vless-grpc-reality;;
      8) printf -v "$destination" vless-xhttp-tls;; 9) printf -v "$destination" vless-xhttp-reality;; 10) printf -v "$destination" vmess-tcp;; 11) printf -v "$destination" vmess-tcp-tls;;
      12) printf -v "$destination" vmess-ws;; 13) printf -v "$destination" vmess-ws-tls;; 14) printf -v "$destination" vmess-grpc-tls;; 15) printf -v "$destination" trojan;;
      16) printf -v "$destination" shadowsocks;; 17) printf -v "$destination" hysteria;; 18) printf -v "$destination" import-node;; 19) printf -v "$destination" import-full;; *) fail "无效协议。"; return 1;; esac
}
select_inbound_user(){
    local destination=$1 file=$2 choice count
    printf -v "$destination" ''
    validate_inbound_users "$file" || return
    count=$(inbound_user_count "$file") || return
    [[ $(jq -r .protocol "$file") != shadowsocks ]] || { fail "当前 Shadowsocks 模板是单密码。"; return 1; }
    jq -r '["用户序号","名称"], ((.settings.users // .settings.clients)|to_entries[]|[.key+1,(.value.email // "未命名")])|map(if type=="string" then gsub("[[:cntrl:]]";"?") else . end)|@tsv' "$file"
    prompt choice "选择用户（0 返回）" 0 || return
    [[ $choice != 0 ]] || return 0
    if [[ ! $choice =~ ^[1-9][0-9]{0,5}$ ]] || ((choice>count)); then fail "用户序号无效。";return 1;fi
    printf -v "$destination" '%s' "$choice"
}

node_wizard_locked() (
    umask 077
    local certificate="" tls_key=""
    local obfs="" obfs_choice
    local old=${1:-} protocol="" tag port=443 credential="" extra="" private_key="" public_key="" sid="" tls_name="" cert="" key="" server="" pair work index=1 count=1 old_tls="" choice file
    require_core && safe_paths || exit 1
    work=$(mktemp -d "$STATE_DIR/node-wizard.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    if [[ -n $old ]]; then
        snapshot_bundle >"$work/snapshot" || exit 1
        jq -e --arg t "$old" '[.config.inbounds[]?|select(.tag==$t)]|if length==1 then .[0] else error("missing") end' "$work/snapshot" >"$work/original" || exit 1
        protocol=$(detect_inbound_template "$work/original") || { fail "导入节点超出向导模板范围，请通过完整 JSON 管理。"; exit 1; }
        count=$(inbound_user_count "$work/original") || exit 1
        if [[ $count -gt 1 ]]; then select_inbound_user index "$work/original" || exit 1; [[ -n $index ]] || exit 0; fi
        tag=$old;port=$(jq -r .port "$work/original")
        credential=$(jq -jr --argjson i "$((index-1))" '((.settings.users // .settings.clients // [])[$i] // {}) as $u | ($u.id // $u.password // $u.auth // .settings.password // "")+"."' "$work/original");credential=${credential%.}
        extra=$(jq -r '.streamSettings.wsSettings.path // .streamSettings.grpcSettings.serviceName // .streamSettings.xhttpSettings.path // ""' "$work/original")
        tls_name=$(jq -r '.streamSettings.tlsSettings.serverName // .streamSettings.realitySettings.serverNames[0] // ""' "$work/original");old_tls=$tls_name
        cert=$(jq -r '.streamSettings.tlsSettings.certificates[0].certificateFile // ""' "$work/original");key=$(jq -r '.streamSettings.tlsSettings.certificates[0].keyFile // ""' "$work/original")
        private_key=$(jq -r '.streamSettings.realitySettings.privateKey // ""' "$work/original");sid=$(jq -r '.streamSettings.realitySettings.shortIds[0] // ""' "$work/original")
        if [[ -f $CLIENT_DIR/$old.json ]]; then
            server=$(jq -r '.server // ""' "$CLIENT_DIR/$old.json");public_key=$(jq -r '.public_key // ""' "$CLIENT_DIR/$old.json")
        fi
    else
        choose_template protocol || exit 1; [[ -n $protocol ]] || exit 0
        if [[ $protocol == import-* ]]; then
            prompt file "服务器 JSON 文件绝对路径" || exit 1
            if [[ $protocol == import-node ]]; then confirm "追加单个入站并按需启动/重启？" && add_inbound_file "$file"
            else confirm "替换完整 Xray 配置？（自动备份，连接元数据可能需重新设置）" && import_config "$file"; fi
            exit $?
        fi
        tag="$protocol-$(date +%H%M%S)"
        case "$protocol" in vless-*|vmess-*) credential=$("$CORE_BIN" uuid) || exit 1;; *) credential=$(openssl rand -base64 16) || exit 1;; esac
    fi
    prompt tag "配置名称（字母/数字/短横线/下划线）" "$tag" && prompt port "监听端口" "$port" || exit 1
    if ! valid_tag "$tag" || ! valid_port "$port"; then fail "名称或端口无效。"; exit 1; fi
    prompt_secret credential "UUID/密码（回车保留或自动生成，@new 重新生成）" "$credential" || exit 1
    if [[ $credential == @new ]]; then case "$protocol" in vless-*|vmess-*) credential=$("$CORE_BIN" uuid);; *) credential=$(openssl rand -base64 16);; esac || exit 1; fi
    case "$protocol" in *-ws|*-ws-tls|*-xhttp-*) prompt extra "传输路径" "${extra:-/xray}" || exit 1;; *-grpc-*) prompt extra "gRPC 服务名" "${extra:-TunService}" || exit 1;; esac
    case "$protocol" in
        *reality*)
            prompt tls_name "REALITY TLS 1.3 握手域名" "$tls_name" || exit 1
            valid_host "$tls_name" || exit 1
            if [[ -n $private_key ]]; then
                prompt choice "REALITY 密钥：1 保留，2 重新生成，0 取消" 1 || exit 1
                case "$choice" in 1) pair=$(make_reality_pair "$private_key") || exit 1;; 2) pair=$(make_reality_pair) || exit 1;; 0) exit 0;; *) exit 1;; esac
            else pair=$(make_reality_pair) || exit 1; fi
            private_key=$(jq -r .private <<<"$pair");public_key=$(jq -r .public <<<"$pair")
            [[ -n $sid ]] || sid=$(openssl rand -hex 8) || exit 1
            prompt_secret sid "REALITY short ID（回车保留）" "$sid" || exit 1
            ;;
        *-tls|trojan|hysteria)
            prompt tls_name "TLS SNI 域名/IP" "$tls_name" || exit 1
            prompt_tls_certificate_files "$tls_name" "$cert" "$key" || exit 1
            cert=$certificate; key=$tls_key
            ;;
    esac
    if [[ $protocol == hysteria ]];then
        if [[ -n $old ]];then obfs=$(jq -jr '(.streamSettings.finalmask.udp[0].settings.password // "")+"."' "$work/original");obfs=${obfs%.};fi
        if [[ -n $obfs ]];then obfs_choice=2;else obfs_choice=1;fi
        prompt obfs_choice "Hy2 混淆：1 无，2 Salamander，0 取消" "$obfs_choice" || exit 1
        case "$obfs_choice" in 0)exit 0;;1)obfs="";;2)prompt_secret obfs "Salamander 密码（至少 4 字节，回车保留/自动生成）" "${obfs:-$(openssl rand -base64 16)}" || exit 1;;*)exit 1;;esac
    fi
    [[ -n $server ]] || server=$(read_default_server) || exit 1
    prompt server "客户端服务器 IP/域名（- 清除独立地址）" "$server" || exit 1
    [[ $server != - ]] || server=""
    generate_inbound "$protocol" "$tag" "$port" "$credential" "$extra" "$private_key" "$sid" "$tls_name" "$cert" "$key" "" "$obfs" >"$work/generated" || exit 1
    if [[ -n $old ]]; then
        jq --slurpfile update "$work/generated" --argjson i "$((index-1))" '
          . as $old | (.settings.users // .settings.clients // null) as $users | . * $update[0] |
          .listen=$old.listen | if $users!=null then .settings.users=$users | .settings.users[$i]=($users[$i]*$update[0].settings.users[0]) | del(.settings.clients) else . end' "$work/original" >"$work/updated" || exit 1
        if [[ $protocol == *reality* && $tls_name == "$old_tls" ]]; then
            jq --slurpfile old "$work/original" 'if $old[0].streamSettings.realitySettings.target!=null then .streamSettings.realitySettings.target=$old[0].streamSettings.realitySettings.target else . end' "$work/updated" >"$work/retained" && mv "$work/retained" "$work/updated" || exit 1
        fi
        if [[ $protocol == hysteria && -z $obfs ]];then
            jq 'del(.streamSettings.finalmask.udp)' "$work/updated" >"$work/unmasked" && mv "$work/unmasked" "$work/updated" || exit 1
        fi
        confirm "保存修改？（其他用户保留，配置和连接信息成套备份）" || exit 0
        snapshot_bundle >"$work/current" || exit 1
        [[ $(jq -Sc . "$work/current") == "$(jq -Sc . "$work/snapshot")" ]] || { fail "编辑期间配置或连接信息已变化。"; exit 1; }
        replace_inbound_file "$old" "$work/updated" "$server" "$public_key" "$work/original" || exit 1
    else
        confirm "添加节点并按需启动/重启 Xray？（自动备份）" || exit 0
        add_managed_inbound "$work/generated" "$server" "$public_key" || exit 1
    fi
    info "配置已保存，请按实际传输放行云安全组和系统防火墙；面板不改防火墙。"
    [[ -z $server ]] || show_client "$tag" "$server" full all
)
add_wizard_locked(){ node_wizard_locked; }
edit_wizard_locked(){ local selected=${1:-}; if [[ -z $selected ]]; then select_inbound selected "修改" || return; [[ -n $selected ]] || return 0; fi; node_wizard_locked "$selected"; }

show_client() (
    umask 077
    local tag=$1 server=${2:-} mode=${3:-full} user_index=${4:-} work public_key="" count index label name
    local -a indices=()
    require_core && valid_tag "$tag" || exit 1
    case "$mode" in full|json-only|link-only) ;; *) exit 1;; esac
    work=$(mktemp -d "$STATE_DIR/client.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    config_jq -e --arg t "$tag" '[.inbounds[]?|select(.tag==$t)]|if length==1 then .[0] else error("missing") end' >"$work/node" || exit 1
    if [[ -f $CLIENT_DIR/$tag.json && ! -L $CLIENT_DIR/$tag.json && $(jq -Sc .inbound "$CLIENT_DIR/$tag.json") == "$(jq -Sc . "$work/node")" ]]; then
        [[ -n $server ]] || server=$(jq -r '.server // ""' "$CLIENT_DIR/$tag.json")
        public_key=$(jq -r '.public_key // ""' "$CLIENT_DIR/$tag.json")
    fi
    [[ -n $server ]] || server=$(read_default_server) || exit 1
    if [[ -z $server ]]; then
        if [[ $mode != full || ! -t 0 ]];then fail "请先设置默认公网地址或显式传入服务器地址。";exit 1;fi
        prompt server "服务器公网 IP/域名" || exit 1
    fi
    validate_inbound_users "$work/node" || { fail "节点超出安全导出模板范围。"; exit 1; }
    count=$(inbound_user_count "$work/node") || exit 1
    if [[ $user_index == all ]]; then for ((index=1;index<=count;index++)); do indices+=("$index"); done
    else
        if [[ -z $user_index ]]; then
            if [[ $count -gt 1 ]]; then [[ $mode == full && -t 0 ]] || { fail "多用户节点须指定用户序号或 all。"; exit 1; }; select_inbound_user user_index "$work/node" || exit 1; [[ -n $user_index ]] || exit 0
            else user_index=1; fi
        fi
        indices=("$user_index")
    fi
    for index in "${indices[@]}"; do
        generate_client "$work/node" "$server" "$public_key" "$index" >"$work/outbound" || { fail "客户端配置无法安全导出，请核对模板与 REALITY 公钥。"; exit 1; }
        label=$tag
        if [[ $count -gt 1 ]]; then
            name=$(jq -r --argjson i "$((index-1))" '(.settings.users // .settings.clients)[$i].email // ""' "$work/node") || exit 1
            label="$tag:user$index${name:+:$name}"
        fi
        if [[ $mode == json-only ]]; then jq --arg t "$label" '.tag=$t' "$work/outbound"; continue; fi
        if [[ $mode == full ]]; then info "客户端 outbound JSON（包含认证）："; cat "$work/outbound"; info "完整分享链接："; fi
        client_link "$work/outbound" "$label" || exit 1
    done
)
show_config_list(){
    local name
    config_jq -r '["序号","名称","协议","监听","端口"],((.inbounds // [])|to_entries[]|[.key+1,.value.tag,.value.protocol,(.value.listen // "-"),.value.port])|map(if type=="string" then gsub("[[:cntrl:]]";"?") else . end)|@tsv' || return
    info "以下链接包含节点凭据，可复制导入客户端："
    while IFS= read -r name; do printf '\n[%s]\n' "$name"; show_client "$name" "" link-only all || warn "此节点导出失败，继续显示其他节点。"; done < <(config_jq -r '.inbounds[]?.tag')
}
exports() (
    umask 077
    local format=$1 target=$2 work name
    [[ $target == /* && ! -e $target && ! -L $target && -d $(dirname -- "$target") ]] || { fail "请指定尚不存在的绝对输出路径。"; exit 1; }
    case "$format" in links|base64|json) ;; *) exit 1;; esac
    work=$(mktemp -d "$STATE_DIR/export.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    : >"$work/nodes"
    while IFS= read -r name; do
        if [[ $format == json ]]; then show_client "$name" "" json-only all >>"$work/nodes" || exit 1
        else show_client "$name" "" link-only all >>"$work/nodes" || exit 1; fi
    done < <(config_jq -r '.inbounds[]?.tag')
    [[ -s $work/nodes ]] || { fail "没有可导出节点。"; exit 1; }
    case "$format" in json) jq -s '{outbounds:.}' "$work/nodes" >"$work/result";; links) cp "$work/nodes" "$work/result";; base64) base64 -w0 "$work/nodes" >"$work/result";; esac || exit 1
    (set -o noclobber; cat "$work/result" >"$target") || exit 1; chmod 600 "$target"; info "已导出：$target"
)
export_clients(){ exports "$@"; }

list_inbound_users(){
    local selected=${1:-} work
    require_core || return
    if [[ -z $selected ]]; then select_inbound selected "查看用户" || return; [[ -n $selected ]] || return 0; fi
    work=$(config_jq -ce --arg t "$selected" '.inbounds[]|select(.tag==$t)') || return
    [[ $(jq -r .protocol <<<"$work") != shadowsocks ]] || { info "Shadowsocks 当前为单密码。"; return 0; }
    jq -r '["用户序号","名称"],((.settings.users // .settings.clients)|to_entries[]|[.key+1,(.value.email // "未命名")])|map(if type=="string" then gsub("[[:cntrl:]]";"?") else . end)|@tsv' <<<"$work"
}
change_inbound_user() (
    umask 077
    local action=$1 selected=${2:-} work protocol count index="" name credential key=""
    require_core && safe_paths || exit 1
    if [[ -z $selected ]]; then select_inbound selected "管理用户" || exit 1; [[ -n $selected ]] || exit 0; fi
    work=$(mktemp -d "$STATE_DIR/users.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before" || exit 1
    jq -e --arg t "$selected" '.config.inbounds[]|select(.tag==$t)' "$work/before" >"$work/node" || exit 1
    validate_inbound_users "$work/node" || exit 1
    protocol=$(jq -r .protocol "$work/node")
    [[ $protocol != shadowsocks ]] || { fail "当前 SS2022 为单密码模板。"; exit 1; }
    count=$(inbound_user_count "$work/node")
    case "$protocol" in vless|vmess) key=id;; trojan) key=password;; hysteria) key=auth;; *) exit 1;; esac
    if [[ $action == add ]]; then
        prompt name "新用户名（字母/数字/短横线/下划线）" "user-$(date +%H%M%S)" || exit 1; valid_tag "$name" || exit 1
        case "$key" in id) credential=$("$CORE_BIN" uuid);; *) credential=$(openssl rand -base64 16);; esac || exit 1
        prompt_secret credential "UUID/密码（回车自动生成）" "$credential" || exit 1
        jq --arg k "$key" --arg c "$credential" --arg name "$name" '.settings.users=(.settings.users // .settings.clients) | del(.settings.clients) | .settings.users+=[(.settings.users[0]|.[$k]=$c|.email=$name)]' "$work/node" >"$work/new" || exit 1
    else
        [[ $count -gt 1 ]] || { fail "不能删除最后一个用户。"; exit 1; }
        select_inbound_user index "$work/node" || exit 1; [[ -n $index ]] || exit 0
        jq --argjson i "$((index-1))" '.settings.users=(.settings.users // .settings.clients)|del(.settings.clients)|del(.settings.users[$i])' "$work/node" >"$work/new" || exit 1
    fi
    validate_inbound_users "$work/new" || { fail "认证重复或用户数据无效。"; exit 1; }
    confirm "保存用户变更（其他用户保留，自动备份）？" || exit 0
    snapshot_bundle >"$work/current" || exit 1
    [[ $(jq -Sc . "$work/current") == "$(jq -Sc . "$work/before")" ]] || { fail "配置已变化。"; exit 1; }
    jq --arg t "$selected" --slurpfile n "$work/new" '.config.inbounds|=map(if .tag==$t then $n[0] else . end)|.clients[$t]=((.clients[$t] // {})+{inbound:$n[0]})' "$work/before" >"$work/after" || exit 1
    if [[ $action == add ]]; then apply_bundle "$work/after" preserve start; else apply_bundle "$work/after"; fi
)
users_menu(){
    local choice selected=${1:-}
    if [[ ! -t 0 ]]; then list_inbound_users "$selected"; return; fi
    while true; do
        printf '\n入站用户管理\n  1. 查看用户\n  2. 添加用户\n  3. 删除用户\n  4. 用户客户端配置和链接\n  0. 返回\n'
        prompt choice "请选择用户操作" 0 || return
        case "$choice" in
            0) return 0;;
            1|2|3|4)
                if [[ -z $selected ]]; then select_inbound selected "管理用户" || continue; [[ -n $selected ]] || continue; fi
                case "$choice" in 1) list_inbound_users "$selected";; 2) with_lock change_inbound_user add "$selected";; 3) with_lock change_inbound_user delete "$selected";; 4) show_client "$selected";; esac || true
                selected="" ;;
            *) warn "无效选项。";;
        esac
        prompt choice "按回车返回用户管理" "" || return
    done
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
    local strategy=$1 work backup
    case "$strategy" in prefer_ipv6|prefer_ipv4) ;; *) return 1;; esac
    require_core && safe_paths || exit 1
    work=$(mktemp -d "$STATE_DIR/ip-preference.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    cp -- "$CONFIG_FILE" "$work/before" || exit 1
    config_jq -e 'any(.outbounds[]?;.protocol=="freedom")' >/dev/null || { fail "当前没有 freedom 直连出站，不会猜测或修改其他代理。"; exit 1; }
    json_query "$work/before" --arg s "$strategy" '
      .dns.queryStrategy="UseIP" | if (.dns.servers // [] | length)==0 then .dns.servers=["localhost"] else . end |
      .outbounds|=map(if .protocol=="freedom" then
        .streamSettings.sockopt.domainStrategy=(if $s=="prefer_ipv6" then "UseIPv6v4" else "UseIPv4v6" end) |
        del(.settings.domainStrategy) else . end)' >"$work/after" || exit 1
    [[ $(json_query "$work/before" -Sc .) != "$(jq -Sc . "$work/after")" ]] || { info "优先级已是当前设置。"; exit 0; }
    validate_config "$work/after" || exit 1
    info "只调整 freedom 出站的地址解析顺序，保留代理出站和独立 DNS 服务器；不保证 IPv6 连接失败后的网络重连回退。"
    confirm "保存出站 IP 优先级？（自动备份并按需重启）" || exit 1
    [[ $strategy != prefer_ipv6 ]] || check_ipv6_egress || exit 1
    cmp -s "$work/before" "$CONFIG_FILE" || { fail "配置已变化，请重试。"; exit 1; }
    backup=$(create_backup) || exit 1
    commit_config "$work/after" || exit 1
    info "已保存，备份：$backup"
)
show_outbound_preference(){
    config_jq -r '["直连出站","解析策略"],(.outbounds[]?|select(.protocol=="freedom")|[.tag,(.streamSettings.sockopt.domainStrategy // .settings.domainStrategy // "默认")])|@tsv'
}
set_outbound_preference(){
    case "${1:-}" in ipv6) with_lock configure_outbound_preference_locked prefer_ipv6;; ipv4) with_lock configure_outbound_preference_locked prefer_ipv4;; status) show_outbound_preference;; "") outbound_preference_menu;; *) fail "用法：xray ip-preference [ipv6|ipv4|status]";; esac
}
outbound_preference_menu(){
    local choice
    while true; do
        printf '\n出站 IP 优先级\n'; show_outbound_preference || true
        printf '  1. IPv6 优先（先自动检测公网）\n  2. IPv4 优先\n  3. 查看设置\n  0. 返回\n'
        prompt choice "请选择出站策略" 0 || return
        case "$choice" in 0) return 0;; 1) with_lock configure_outbound_preference_locked prefer_ipv6;; 2) with_lock configure_outbound_preference_locked prefer_ipv4;; 3) show_outbound_preference;; *) warn "无效选项。";; esac || true
        prompt choice "按回车返回出站设置" "" || return
    done
}

show_status(){
    local version="未安装" active enabled green="" red="" reset="" dim=""
    if [[ -t 1 && ${TERM:-dumb} != dumb && -z ${NO_COLOR+x} ]]; then green=$'\033[32m';red=$'\033[31m';dim=$'\033[90m';reset=$'\033[0m';fi
    [[ ! -x $CORE_BIN ]] || version=$("$CORE_BIN" version 2>/dev/null|head -n1)
    active=$(systemctl is-active "$SERVICE" 2>/dev/null) || active=${active:-unknown}
    enabled=$(systemctl is-enabled "$SERVICE" 2>/dev/null) || enabled=${enabled:-unknown}
    case "$active" in active) active="${green}●${reset} 运行中 (active)";; inactive) active="${dim}●${reset} 已停止 (inactive)";; *) active="${red}●${reset} $active";; esac
    case "$enabled" in enabled) enabled="${green}●${reset} 已启用 (enabled)";; disabled) enabled="${red}●${reset} 未启用 (disabled)";; *) enabled="${dim}●${reset} $enabled";; esac
    printf '\n核心：%s\n状态：%s    开机自启：%s\n配置：%s\n' "$version" "$active" "$enabled" "$CONFIG_FILE"
}
autostart_menu(){
    local choice
    while true; do
        printf '\n开机自启\n  1. 开启（运行状态不变）\n  2. 关闭（运行状态不变）\n  0. 返回\n'
        prompt choice "请选择自启设置" 0 || return
        case "$choice" in 0) return 0;; 1) with_lock service_action enable;; 2) with_lock service_action disable-autostart;; *) warn "无效选项。";; esac || true
        prompt choice "按回车返回自启菜单" "" || return
    done
}
show_logs(){ if [[ ${1:-} == --follow ]]; then journalctl -u "$SERVICE" -n100 -f --no-pager -o cat; else journalctl -u "$SERVICE" -n100 --no-pager -o cat; fi; }
diagnose(){
    local issue=0 n tag
    require_core && validate_config "$CONFIG_FILE" || return 1
    service_is_active || { warn "服务未运行。"; issue=1; }
    while IFS= read -r n; do
        tag=$(jq -r .tag <<<"$n")
        show_client "$tag" "" json-only all >/dev/null 2>&1 || { warn "$tag 客户端导出需要地址/公钥或原生 JSON。"; issue=1; }
    done < <(config_jq -c '.inbounds[]?')
    info "诊断不检测公网、不调整防火墙；生产 freedom 保留核心默认私网访问保护。"
    return "$issue"
}
show_config(){
    local choice selected
    while true; do
        printf '\n查看 Xray 配置\n  1. 配置列表和完整分享链接\n  2. 完整服务端 JSON（包含认证/私钥）\n  3. 客户端 JSON 和链接\n  4. 检查当前配置\n  5. 设置默认公网地址\n  6. 格式化配置（保留注释、不重启）\n  0. 返回\n'
        prompt choice "请选择" 1 || return
        case "$choice" in
            0) return 0;; 1) show_config_list;; 2) cat "$CONFIG_FILE";;
            3) if select_inbound selected "导出" && [[ -n $selected ]]; then show_client "$selected"; fi;;
            4) require_core && validate_config "$CONFIG_FILE";; 5) with_lock set_default_server;; 6) format_config;; *) warn "无效选项。";;
        esac || true
        prompt choice "按回车返回查看配置" "" || return
    done
}
management_menu(){
    local choice file format
    while true; do
        printf '\n备份、恢复、导出和诊断\n  1. 成套备份\n  2. 备份列表\n  3. 恢复备份\n  4. 批量导出客户端\n  5. 诊断\n  6. 单个 inbound JSON 导入\n  7. 完整 Xray JSON 导入\n  0. 返回\n'
        prompt choice "请选择维护操作" 0 || return
        case "$choice" in
            0) return 0;; 1) with_lock create_backup;; 2) list_backups;;
            3) list_backups; prompt file "成套备份文件绝对路径" && with_lock restore_backup "$file";;
            4) prompt format "导出格式 links/base64/json" links && prompt file "尚不存在的绝对输出路径" && with_lock exports "$format" "$file";;
            5) diagnose;; 6) prompt file "单个 inbound JSON 文件路径" && confirm "追加配置并按需启动？" && with_lock add_inbound_file "$file";;
            7) prompt file "完整 Xray JSON 文件路径" && confirm "替换完整配置（成套备份）？" && with_lock import_config "$file";;
            *) warn "无效选项。";;
        esac || true
        prompt choice "按回车返回维护菜单" "" || return
    done
}
bbr_menu(){
    local choice
    while true; do
        printf '\nBBR 设置（系统 TCP 为整机共享）\n  1. 开启系统 TCP BBR\n  2. 恢复本脚本开启前的 TCP 设置\n  3. 查看 TCP 状态\n  4. 为 Hy2 显式启用 QUIC BBR\n  5. 恢复 Hy2 QUIC 开启前设置\n  6. 查看 Hy2 QUIC 状态\n  0. 返回\n'
        prompt choice "请选择 BBR 操作" 0 || return
        case "$choice" in 0) return 0;; 1) enable_bbr;; 2) disable_bbr;; 3) show_bbr_status;;4) with_lock quic_bbr_change_locked enable;;5) with_lock quic_bbr_change_locked disable;;6)show_quic_bbr_status;; *) warn "无效选项。";; esac || true
        prompt choice "按回车返回 BBR" "" || return
    done
}

decode_standard_links() {
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
    if transport not in {"tcp", "ws", "grpc", "xhttp"}:
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
    elif transport == "xhttp":
        if not query.get("path", "/").startswith("/") or query.get("mode", "auto") not in {"auto","packet-up","stream-up","stream-one"}: fail()
        result["transport"] = {"type":"xhttp","path":query.get("path","/"),"mode":query.get("mode","auto")}
        if query.get("host"): result["transport"]["host"]=query["host"]
    elif transport == "grpc":
        service = query.get("serviceName")
        if not service or query.get("mode", "gun") != "gun":
            fail()
        result["transport"] = {"type": "grpc", "service_name": service}
    for key in ("path", "host", "serviceName", "mode"):
        if key in query and ((transport == "ws" and key not in {"path", "host"}) or
                             (transport == "grpc" and key not in {"serviceName", "mode"}) or
                             (transport == "xhttp" and key not in {"path", "host", "mode"}) or transport == "tcp"):
            fail()
    if security == "reality":
        if transport not in {"tcp", "grpc", "xhttp"}:
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



parse_outbound_links() (
    umask 077
    local work
    work=$(mktemp -d "$STATE_DIR/link-parse.XXXXXX") || exit 1; trap 'rm -rf -- "$work"' EXIT
    decode_standard_links "$1" >"$work/nodes" || exit 1
    python3 - "$work/nodes" <<'PY_XR_LINK_CONVERT'
import json,sys
from pathlib import Path
try:
    result=[]
    for n in json.loads(Path(sys.argv[1]).read_text()):
        kind=n['type']
        if kind not in {'vless','vmess','trojan','shadowsocks','hysteria2'}:raise ValueError()
        settings={'address':n['server'],'port':n['server_port']}
        if kind in {'vless','vmess'}:
            settings['id']=n['uuid']
            settings['encryption' if kind=='vless' else 'security']='none' if kind=='vless' else n.get('security','auto')
            if n.get('flow'):settings['flow']=n['flow']
        elif kind in {'trojan','shadowsocks'}:
            settings['password']=n['password']
            if kind=='shadowsocks':settings['method']=n['method']
        else:settings['version']=2
        stream={'method':'raw','security':'none'}
        t=n.get('transport',{})
        if t.get('type')=='ws':
            stream.update(method='websocket',wsSettings={'path':t['path']})
            if t.get('headers',{}).get('Host'):stream['wsSettings']['host']=t['headers']['Host']
        elif t.get('type')=='grpc':stream.update(method='grpc',grpcSettings={'serviceName':t['service_name']})
        elif t.get('type')=='xhttp':stream.update(method='xhttp',xhttpSettings={k:v for k,v in t.items() if k!='type'})
        if n.get('tls',{}).get('enabled'):
            tls=n['tls'];stream['security']='tls';stream['tlsSettings']={'serverName':tls.get('server_name',n['server']),'allowInsecure':False}
            if tls.get('alpn'):stream['tlsSettings']['alpn']=tls['alpn']
            if tls.get('utls',{}).get('enabled'):stream['tlsSettings']['fingerprint']=tls['utls']['fingerprint']
            if tls.get('reality',{}).get('enabled'):
                stream['security']='reality';stream.pop('tlsSettings')
                stream['realitySettings']={'serverName':tls['server_name'],'fingerprint':tls.get('utls',{}).get('fingerprint','chrome'),'password':tls['reality']['public_key'],'shortId':tls['reality']['short_id']}
        if kind=='hysteria2':
            stream['method']='hysteria';stream['hysteriaSettings']={'version':2,'auth':n['password']}
            if n.get('obfs'):stream['finalmask']={'udp':[{'type':'salamander','settings':{'password':n['obfs']['password']}}]}
        result.append({'tag':n['tag'],'protocol':'hysteria' if kind=='hysteria2' else kind,'settings':settings,'streamSettings':stream})
    print(json.dumps(result,ensure_ascii=False))
except (ValueError,KeyError,TypeError):
    print('分享链接超出当前 Xray 安全导入范围，请改用原生 outbound JSON。',file=sys.stderr)
    raise SystemExit(1)
PY_XR_LINK_CONVERT
)
normalize_outbound_import() (
    umask 077
    local file=$1 work
    [[ -f $file && ! -L $file && $(stat -c '%s' "$file") -le 1048576 ]] || { fail "文件须为不超过 1 MiB 的普通文件。"; exit 1; }
    work=$(mktemp -d "$STATE_DIR/out-import.XXXXXX") || exit 1;trap 'rm -rf -- "$work"' EXIT
    cp -- "$file" "$work/source" || exit 1
    if json_query "$work/source" . >"$work/json" 2>/dev/null; then
        jq -e 'if type=="array" then . elif type=="object" and has("protocol") then [.] elif type=="object" and keys==["outbounds"] then .outbounds else error("not outbound JSON") end | select(type=="array" and length>0)' "$work/json" 2>/dev/null || { fail "请提供原生 Xray outbound 对象、数组或仅包含 outbounds 的 JSON。"; exit 1; }
    else parse_outbound_links "$work/source"; fi
)
add_outbounds_file_locked() (
    umask 077
    local file=$1 work tag backup
    require_core && safe_paths || exit 1
    work=$(mktemp -d "$STATE_DIR/out-add.XXXXXX") || exit 1;trap 'rm -rf -- "$work"' EXIT
    cp -- "$CONFIG_FILE" "$work/before" || exit 1
    normalize_outbound_import "$file" >"$work/nodes" || exit 1
    jq -e 'type=="array" and length>0 and all(.[];type=="object" and (.protocol|type=="string") and (.tag|type=="string")) and ([.[].tag]|length==(unique|length))' "$work/nodes" >/dev/null || { fail "出站格式无效或名称重复。"; exit 1; }
    while IFS= read -r tag; do valid_tag "$tag" || { fail "出站名称须为字母/数字/短横线/下划线。"; exit 1; };done < <(jq -r '.[].tag' "$work/nodes")
    if json_query "$work/before" -e --slurpfile nodes "$work/nodes" '[.outbounds[]?.tag] as $tags|any($nodes[0][];.tag as $t|$tags|index($t)!=null)' >/dev/null;then fail "出站名称已存在。";exit 1;fi
    json_query "$work/before" --slurpfile n "$work/nodes" '.outbounds=((.outbounds // [])+$n[0])' >"$work/after" || exit 1
    validate_config "$work/after" || exit 1
    jq -r '["出站名称","协议"],(.[]|[.tag,.protocol])|@tsv' "$work/nodes"
    confirm "添加以上出站（不自动绑定入站，成套备份）？" || exit 0
    cmp -s "$work/before" "$CONFIG_FILE" || { fail "配置已变化。";exit 1; }
    backup=$(create_backup) || exit 1;commit_config "$work/after" || exit 1;info "出站已保存，备份：$backup"
)
delete_outbound_locked() (
    umask 077
    local selected=$1 work backup
    require_core && safe_paths && valid_tag "$selected" || exit 1
    work=$(mktemp -d "$STATE_DIR/out-delete.XXXXXX") || exit 1;trap 'rm -rf -- "$work"' EXIT
    cp -- "$CONFIG_FILE" "$work/before" || exit 1
    json_query "$work/before" -e --arg t "$selected" '(.outbounds|length)>1 and ([.outbounds[]|select(.tag==$t)]|length)==1' >/dev/null || { fail "出站不存在或为最后一个出站。";exit 1; }
    if json_query "$work/before" -e --arg t "$selected" 'del(.outbounds[]|select(.tag==$t))|any(..|strings;.==$t)' >/dev/null;then fail "出站被路由、proxySettings 等引用，请先解除。";exit 1;fi
    json_query "$work/before" --arg t "$selected" '.outbounds|=map(select(.tag!=$t))' >"$work/after" || exit 1
    validate_config "$work/after" && confirm "删除出站 $selected（成套备份）？" || exit 1
    cmp -s "$work/before" "$CONFIG_FILE" || exit 1
    backup=$(create_backup) || exit 1;commit_config "$work/after" || exit 1;info "已删除，备份：$backup"
)
list_outbounds(){ config_jq -r '["名称","协议","远端地址","远端端口"],(.outbounds[]?|[.tag,.protocol,(.settings.address // .settings.vnext[0].address // .settings.servers[0].address // "-"),(.settings.port // .settings.vnext[0].port // .settings.servers[0].port // "-")])|map(if type=="string" then gsub("[[:cntrl:]]";"?") else . end)|@tsv'; }
import_outbound_wizard_locked() (
    umask 077
    local mode=$1 file work value line
    work=$(mktemp -d "$STATE_DIR/out-paste.XXXXXX") || exit 1;trap 'rm -rf -- "$work"' EXIT
    case "$mode" in
      file) prompt file "JSON/分享链接文本文件绝对路径" || exit 1;;
      link) prompt_secret value "粘贴分享链接（不回显）" || exit 1;printf '%s\n' "$value" >"$work/input";file="$work/input";;
      json) [[ -t 0 ]] || exit 1;info "粘贴原生 Xray outbound JSON（不回显），单独一行 END 结束。";file="$work/input";: >"$file"
        while IFS= read -r -s line;do [[ $line != END ]] || break;printf '%s\n' "$line" >>"$file";[[ $(stat -c '%s' "$file") -le 1048576 ]] || exit 1;done;;
      *) exit 1;;
    esac
    add_outbounds_file_locked "$file"
)
add_outbound_wizard_locked() (
    umask 077
    local protocol tag server port credential="" method="" network=raw security=none sni="" pub="" sid="" flow="" path="" username="" password="" choice work
    require_core || exit 1
    printf '\n添加远端出站\n  1. VLESS\n  2. VMess\n  3. Trojan\n  4. Shadowsocks\n  5. Hysteria 2\n  6. SOCKS5\n  7. HTTP/HTTPS\n  8. 直连\n  0. 返回\n'
    prompt choice "选择协议" 1 || exit 1
    case "$choice" in 0) exit 0;;1) protocol=vless;;2) protocol=vmess;;3) protocol=trojan;;4) protocol=shadowsocks;;5) protocol=hysteria;;6) protocol=socks;;7) protocol=http;;8) protocol=freedom;;*) exit 1;;esac
    prompt tag "出站名称" "out-$protocol-$(date +%H%M%S)" || exit 1;valid_tag "$tag" || exit 1
    work=$(mktemp -d "$STATE_DIR/out-wizard.XXXXXX") || exit 1;trap 'rm -rf -- "$work"' EXIT
    if [[ $protocol == freedom ]]; then jq -n --arg t "$tag" '{protocol:"freedom",tag:$t,settings:{}}' >"$work/node";add_outbounds_file_locked "$work/node";exit $?;fi
    prompt server "远端 IP/域名" || exit 1;prompt port "远端端口" 443 || exit 1
    if ! valid_host "$server" || ! valid_port "$port";then fail "远端地址或端口无效。";exit 1;fi
    case "$protocol" in socks|http) prompt username "用户名（留空则无认证）" || exit 1;[[ -z $username ]] || prompt_secret password "远端密码" || exit 1;;*) prompt_secret credential "远端已有 UUID/密码" || exit 1;;esac
    if [[ $protocol == shadowsocks ]];then prompt method "加密方法" 2022-blake3-aes-128-gcm || exit 1;fi
    case "$protocol" in vless|vmess)
      prompt choice "传输：1 RAW，2 WS，3 gRPC，4 XHTTP，0 取消" 1 || exit 1
      case "$choice" in 1) network=raw;;2) network=websocket;;3) network=grpc;;4) network=xhttp;;0) exit 0;;*) exit 1;;esac
      if [[ $network != raw ]];then prompt path "路径或 gRPC 服务名" /xray || exit 1;fi;;esac
    case "$protocol" in trojan|hysteria) security=tls;; vless|vmess|http)
      prompt choice "安全：1 无 TLS，2 TLS，3 REALITY（仅 VLESS），0 取消" 2 || exit 1
      case "$choice" in 1)security=none;;2)security=tls;;3)[[ $protocol == vless && $network != websocket ]] || exit 1;security=reality;;0)exit 0;;*)exit 1;;esac;;esac
    if [[ $security != none ]];then prompt sni "远端 SNI" "$server" || exit 1;valid_host "$sni" || exit 1;fi
    if [[ $security == reality ]];then
      prompt pub "REALITY 公钥（Password/PublicKey，不是 Hash32）" || exit 1;prompt sid "REALITY short ID" || exit 1
      [[ $pub =~ ^[a-zA-Z0-9_-]{43}$ && $sid =~ ^([a-fA-F0-9]{2}){0,8}$ ]] || exit 1
      if [[ $network == raw ]];then prompt choice "Vision：1 开启，2 关闭" 1 || exit 1;[[ $choice != 1 ]] || flow=xtls-rprx-vision;fi
    fi
    jq -n --arg p "$protocol" --arg tag "$tag" --arg addr "$server" --argjson port "$((10#$port))" --arg c "$credential" --arg n "$network" --arg security "$security" --arg sni "$sni" --arg pub "$pub" --arg sid "$sid" --arg flow "$flow" --arg path "$path" --arg method "$method" --arg u "$username" --arg password "$password" '
      {protocol:$p,tag:$tag,settings:{address:$addr,port:$port},streamSettings:{method:$n,security:$security}} |
      if $p=="vless" or $p=="vmess" then .settings.id=$c | if $p=="vless" then .settings.encryption="none" else .settings.security="auto" end
      elif $p=="trojan" then .settings.password=$c
      elif $p=="shadowsocks" then .settings.method=$method|.settings.password=$c
      elif $p=="hysteria" then .settings.version=2|.streamSettings.method="hysteria"|.streamSettings.hysteriaSettings={version:2,auth:$c}
      elif $p=="socks" or $p=="http" then .settings={servers:[{address:$addr,port:$port}]} | if $u!="" then .settings.servers[0].users=[{user:$u,pass:$password}] else . end else . end |
      if $n=="websocket" then .streamSettings.wsSettings={path:$path} elif $n=="grpc" then .streamSettings.grpcSettings={serviceName:$path} elif $n=="xhttp" then .streamSettings.xhttpSettings={path:$path,mode:"auto"} else . end |
      if $security=="tls" then .streamSettings.tlsSettings={serverName:$sni,allowInsecure:false} | if $n=="grpc" then .streamSettings.tlsSettings.alpn=["h2"] elif $n=="websocket" then .streamSettings.tlsSettings.alpn=["http/1.1"] else . end
      elif $security=="reality" then .streamSettings.realitySettings={serverName:$sni,password:$pub,shortId:$sid,fingerprint:"chrome"} else . end |
      if $flow!="" then .settings.flow=$flow else . end' >"$work/node" || exit 1
    add_outbounds_file_locked "$work/node"
)
outbounds_menu(){
    local choice input_node output_node
    [[ -t 0 ]] || { list_outbounds;return; }
    while true;do
      printf '\nXray 出站管理\n  1. 出站列表\n  2. 按协议添加\n  3. 粘贴分享链接\n  4. 从 JSON/链接文件导入\n  5. 粘贴原生 outbound JSON\n  6. 绑定入站至出站\n  7. 取消绑定\n  8. 删除出站\n  0. 返回\n'
      prompt choice "请选择出站操作" 0 || return
      case "$choice" in
        0)return 0;;1)list_outbounds;;2)with_lock add_outbound_wizard_locked;;3)with_lock import_outbound_wizard_locked link;;4)with_lock import_outbound_wizard_locked file;;5)with_lock import_outbound_wizard_locked json;;
        6|7) if select_inbound input_node "设置出站" && [[ -n $input_node ]];then
          output_node=-
          if [[ $choice == 7 ]] || { select_outbound output_node && [[ -n $output_node ]]; };then with_lock bind_outbound_locked "$input_node" "$output_node";fi
        fi;;8)if select_outbound output_node && [[ -n $output_node ]];then with_lock delete_outbound_locked "$output_node";fi;;*)warn "无效选项。";;esac || true
      prompt choice "按回车返回出站管理" "" || return
    done
}

quic_bbr_change_locked() (
    umask 077
    local action=$1 selected=${2:-} work
    require_core && safe_paths || exit 1
    if [[ -z $selected ]];then select_inbound selected "设置 Hy2 QUIC BBR" || exit 1;[[ -n $selected ]] || exit 0;fi
    work=$(mktemp -d "$STATE_DIR/quic-bbr.XXXXXX") || exit 1;trap 'rm -rf -- "$work"' EXIT
    snapshot_bundle >"$work/before" || exit 1
    jq --arg t "$selected" --arg a "$action" '
      (.config.inbounds|map(select(.tag==$t))) as $nodes |
      if ($nodes|length)!=1 or $nodes[0].protocol!="hysteria" or $nodes[0].settings.version!=2 then error("Hy2 required") else . end |
      $nodes[0] as $node | (.clients[$t].quic_bbr_restore // null) as $old |
      if $old!=null and (.clients[$t].inbound!=$node or $old.format!="xray-manager-quic-bbr-v1") then error("stale record") else . end |
      if $a=="enable" then
        (if $old!=null then $old else
          {format:"xray-manager-quic-bbr-v1",settings:(($node.streamSettings.finalmask.quicParams // {})|with_entries(select(.key=="congestion" or .key=="bbrProfile"))),
           qp_present:($node.streamSettings.finalmask // {}|has("quicParams")),qp_null:($node.streamSettings.finalmask.quicParams==null),
           fm_present:($node.streamSettings|has("finalmask")),fm_null:($node.streamSettings.finalmask==null)} end) as $record |
        ($node|.streamSettings.finalmask.quicParams.congestion="bbr"|.streamSettings.finalmask.quicParams.bbrProfile="standard") as $new |
        .config.inbounds|=map(if .tag==$t then $new else . end) | .clients[$t]=((.clients[$t] // {})+{inbound:$new,quic_bbr_restore:$record})
      elif $a=="disable" then
        if $old==null or $node.streamSettings.finalmask.quicParams.congestion!="bbr" or $node.streamSettings.finalmask.quicParams.bbrProfile!="standard" then error("no matching record") else . end |
        ($node|del(.streamSettings.finalmask.quicParams.congestion,.streamSettings.finalmask.quicParams.bbrProfile)|
          .streamSettings.finalmask.quicParams+=($old.settings // {}) |
          if .streamSettings.finalmask.quicParams=={} then
            if $old.qp_present==false then del(.streamSettings.finalmask.quicParams) elif $old.qp_null==true then .streamSettings.finalmask.quicParams=null else . end
          else . end |
          if .streamSettings.finalmask=={} then
            if $old.fm_present==false then del(.streamSettings.finalmask) elif $old.fm_null==true then .streamSettings.finalmask=null else . end
          else . end) as $new |
        .config.inbounds|=map(if .tag==$t then $new else . end) | .clients[$t].inbound=$new|del(.clients[$t].quic_bbr_restore)
      else error("unknown operation") end' "$work/before" >"$work/after" 2>/dev/null || { fail "仅支持 Hy2；缺失、过期或被外部修改的恢复记录不会猜测恢复。";exit 1; }
    [[ $(jq -Sc '{config,clients}' "$work/before") != "$(jq -Sc '{config,clients}' "$work/after")" ]] || { info "QUIC 设置未变化。";exit 0; }
    info "恢复是撤销显式 BBR 设置；原来未指定时，核心仍可能默认使用 BBR。"
    confirm "保存 Hy2 QUIC 设置（备份、按需重启，其他认证和混淆不变）？" || exit 0
    snapshot_bundle >"$work/current" || exit 1
    [[ $(jq -Sc . "$work/current") == "$(jq -Sc . "$work/before")" ]] || { fail "配置已变化。";exit 1; }
    apply_bundle "$work/after"
)
show_quic_bbr_status(){ config_jq -r '["节点","拥塞控制","BBR profile"],(.inbounds[]?|select(.protocol=="hysteria")|[.tag,(.streamSettings.finalmask.quicParams.congestion // "核心默认"),(.streamSettings.finalmask.quicParams.bbrProfile // "默认")])|@tsv'; }

usage(){
cat <<'HELP'
Xray 中文终端管理面板
首次安装菜单：sudo bash xray-manager.sh setup
打开菜单：sudo xray
核心、服务、配置、状态与其他面板分别管理。

  xray install [VERSION] | update [VERSION]   官方稳定核心，校验 SHA256，失败回滚
  xray update-panel                        更新独立面板脚本并备份
  xray start | restart | stop | status      仅操作 xray.service
  xray enable | disable-autostart          开启/关闭自启，不改变运行状态
  xray disable                             停止并关闭自启
  xray log [--follow]                       日志
  xray add | edit [TAG] | delete [TAG]      入站向导或编号管理
  xray add-file FILE | import FILE          原生 Xray JSON/JSONC 导入
  xray info | validate | format-config      查看/检查/保留注释格式化
  xray client TAG [SERVER] [INDEX|all]       客户端 JSON 与完整链接
  xray users [TAG]                          多用户管理（非交互只读）
  xray user-add [TAG] | user-delete [TAG]    交互确认变更用户
  xray server [IP/DOMAIN|-]                 默认公网连接地址
  xray backup | backups | restore FILE      独立成套备份与恢复
  xray export links|base64|json FILE        批量导出，不覆盖已有文件
  xray diagnose                            只读诊断
  xray outbounds                           出站管理（非交互只读）
  xray outbound-add FILE                   原生 outbound JSON/分享链接导入
  xray outbound-bind IN OUT|-              入站绑定出站，- 恢复其余路由
  xray outbound-delete TAG                 删除未引用出站
  xray ip-preference [ipv6|ipv4|status]     freedom 地址解析优先级
  xray bbr | bbr-disable | bbr-status       系统 TCP BBR（内核为整机共享）
  xray quic-bbr [TAG] | quic-bbr-disable [TAG] | quic-bbr-status
                                            Hy2 显式 BBR 及开启前设置恢复
  xray acme                               安装/打开独立 ACME 项目菜单
  xray uninstall                          卸载此面板拥有的 Xray 核心和服务
  xray remove-panel                       核心卸载后移除菜单入口
  xray core ARGS...                       显式透传私有核心
  xray version | uuid | x25519 ...         原生命令透传
HELP
}
menu(){
    local choice selected
    [[ -t 0 ]] || { usage;return 0; }
    while true;do
      printf '\n========== Xray 管理面板 v%s ==========\n' "$XR_MANAGER_VERSION"
      show_status
      printf '\n  1. 安装 Xray 核心\n  2. 卸载 Xray 核心\n  3. 启动\n  4. 开机自启（开启/关闭）\n  5. 重新启动\n  6. 停止并关闭自启\n  7. 查看日志\n  8. 添加配置\n  9. 查看配置\n 10. 更新 Xray 核心\n 11. 修改配置\n 12. 删除配置\n 13. 备份、恢复、导出和诊断\n 14. 更新面板脚本\n 15. BBR 设置（TCP / Hy2 QUIC）\n 16. 恢复本脚本开启前 TCP BBR 设置\n 17. ACME 证书菜单\n 18. 入站用户管理\n 19. 出站 IP 优先级\n 20. 出站节点管理\n  0. 退出\n'
      read -r -p "请输入选项 [0-20]: " choice || return 0
      case "$choice" in
        0)return 0;;1)install_core;;2)uninstall_core;;3)with_lock service_action start;;4)autostart_menu;continue;;5)with_lock service_action restart;;6)with_lock service_action disable;;7)show_logs;;
        8)with_lock add_wizard_locked;;9)show_config;continue;;10)update_core;;11)with_lock edit_wizard_locked;;12)with_lock delete_inbound;;13)management_menu;continue;;
        14)if update_panel;then exec bash "$WRAPPER" menu;fi;;15)bbr_menu;continue;;16)disable_bbr;;17)open_acme_menu;;18)users_menu;continue;;19)outbound_preference_menu;continue;;20)outbounds_menu;continue;;*)warn "请输入 0–20。";;
      esac || true
      read -r -p "按回车键返回菜单..." selected || return 0
    done
}
core_passthrough(){ require_core || return; XRAY_LOCATION_ASSET="$LIB_DIR" exec "$CORE_BIN" "$@"; }
main(){
    set -o pipefail;umask 077
    [[ -z $XR_ROOT_PREFIX ]] || { fail "XR_ROOT_PREFIX 仅供 source 后隔离测试，CLI 禁止设置。";return 1; }
    (( BASH_VERSINFO[0]>=4 )) || { fail "需要 Bash 4+。";return 1; }
    local command=${1:-menu};[[ $# -eq 0 ]] || shift
    case "$command" in help|-h|--help)usage;return;;core|bin)core_passthrough "$@";return;;
      menu|setup|install|update|update-panel|uninstall|start|restart|stop|enable|disable-autostart|disable|status|log|logs|add|edit|delete|add-file|import|info|validate|format-config|client|users|user-add|user-delete|server|backup|backups|restore|export|diagnose|outbounds|outbound-add|outbound-bind|outbound-delete|ip-preference|bbr|bbr-disable|bbr-status|quic-bbr|quic-bbr-disable|quic-bbr-status|acme|remove-panel);;
      *)core_passthrough "$command" "$@";return;;esac
    require_root && require_systemd && safe_paths || return
    if ! is_managed;then case "$command" in menu|setup|install) ;;*)fail "请先 sudo bash xray-manager.sh setup 安装独立面板。";return 1;;esac;fi
    case "$command" in
      menu)if ! is_managed;then setup_panel || return;fi;menu;;setup)setup_panel;;
      install)[[ $# -le 1 ]] || return 1;install_core "${1:-latest}";;update)[[ $# -le 1 ]] || return 1;update_core "${1:-latest}";;
      update-panel)update_panel "$@";;uninstall)uninstall_core;;
      start|restart|stop|enable|disable-autostart|disable)with_lock service_action "$command";;status)show_status;;log|logs)show_logs "${1:-}";;
      add)with_lock add_wizard_locked;;edit)[[ $# -le 1 ]] && with_lock edit_wizard_locked "$@";;delete)[[ $# -le 1 ]] && with_lock delete_inbound "$@";;
      add-file)[[ $# -eq 1 ]] && with_lock add_inbound_file "$1";;import)[[ $# -eq 1 ]] && confirm "替换完整 Xray 配置？" && with_lock import_config "$1";;
      info)show_config;;validate)require_core && validate_config "$CONFIG_FILE";;format-config)format_config;;
      client)[[ $# -ge 1 && $# -le 3 ]] && show_client "$1" "${2:-}" full "${3:-}";;users)users_menu "$@";;user-add)with_lock change_inbound_user add "$@";;user-delete)with_lock change_inbound_user delete "$@";;
      server)with_lock set_default_server "$@";;backup)with_lock create_backup;;backups)list_backups;;restore)[[ $# -eq 1 ]] && with_lock restore_backup "$1";;
      export)[[ $# -eq 2 ]] && with_lock exports "$1" "$2";;diagnose)diagnose;;
      outbounds)outbounds_menu;;outbound-add)[[ $# -eq 1 ]] && with_lock add_outbounds_file_locked "$1";;outbound-bind)[[ $# -eq 2 ]] && with_lock bind_outbound_locked "$1" "$2";;outbound-delete)[[ $# -eq 1 ]] && with_lock delete_outbound_locked "$1";;
      ip-preference)[[ $# -le 1 ]] && set_outbound_preference "$@";;quic-bbr)with_lock quic_bbr_change_locked enable "$@";;quic-bbr-disable)with_lock quic_bbr_change_locked disable "$@";;quic-bbr-status)show_quic_bbr_status;;bbr)enable_bbr "$@";;bbr-disable)disable_bbr "$@";;bbr-status)show_bbr_status "$@";;acme)open_acme_menu "$@";;
      remove-panel)[[ ! -e $CORE_BIN ]] && confirm "删除独立 Xray 菜单入口？" && rm -f -- "$WRAPPER";;
    esac
}
if [[ ${BASH_SOURCE[0]} == "$0" ]];then main "$@";exit $?;fi
