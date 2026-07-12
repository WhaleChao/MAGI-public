#!/bin/bash
# MAGI 快速管理工具
# 用法: magi [status|start|stop|restart|zombie]
# 安裝: cp scripts/magi_cli.sh /opt/homebrew/bin/magi && chmod +x /opt/homebrew/bin/magi

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
CYAN='\033[0;36m'
NC='\033[0m'

LABEL="com.magi.daemon"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
MENUBAR_LABEL="com.magi.menubar"
MENUBAR_PLIST="$HOME/Library/LaunchAgents/$MENUBAR_LABEL.plist"
MENUBAR_WRAPPER_PATTERN="run_menubar_no_site.py"
MENUBAR_GUI_PATTERN="gui/magi_menubar.py"
RPC_LABEL="com.magi.rpc"
RPC_PLIST="$HOME/Library/LaunchAgents/$RPC_LABEL.plist"

_check() {
    local name="$1" pattern="$2"
    local pid
    pid=$(_find_process_by_pattern "$pattern")
    if [ -n "$pid" ]; then
        printf "  ${GREEN}●${NC} %-18s PID %-6s\n" "$name" "$pid"
    else
        printf "  ${RED}○${NC} %-18s ${RED}DOWN${NC}\n" "$name"
    fi
}

_check_daemon() {
    local pid
    pid=$(_find_process_by_pattern "run_daemon_no_site.py")
    if [ -z "$pid" ]; then
        pid=$(_find_process_by_pattern "/daemon.py")
    fi
    if [ -n "$pid" ]; then
        printf "  ${GREEN}●${NC} %-18s PID %-6s\n" "Daemon" "$pid"
    else
        printf "  ${RED}○${NC} %-18s ${RED}DOWN${NC}\n" "Daemon"
    fi
}

_find_menubar_process() {
    local pid
    pid=$(_find_process_by_pattern "$MENUBAR_WRAPPER_PATTERN")
    if [ -z "$pid" ]; then
        pid=$(_find_process_by_pattern "$MENUBAR_GUI_PATTERN")
    fi
    echo "$pid"
}

_check_menubar() {
    local pid
    pid=$(_find_menubar_process)
    if [ -n "$pid" ]; then
        printf "  ${GREEN}●${NC} %-18s PID %-6s\n" "Status Bar" "$pid"
    else
        printf "  ${RED}○${NC} %-18s ${RED}DOWN${NC}\n" "Status Bar"
    fi
}

_kill_menubar_processes() {
    pkill -f "$MENUBAR_WRAPPER_PATTERN" 2>/dev/null || true
    pkill -f "$MENUBAR_GUI_PATTERN" 2>/dev/null || true
}

_find_process_by_pattern() {
    local pattern="$1"
    { ps -axo pid=,command= | awk -v pat="$pattern" '
        index($0, pat) &&
        $0 ~ /[Pp]ython|Python\.app/ &&
        $0 !~ /^ *[0-9]+ +\/bin\/(zsh|bash)( |$)/ &&
        $0 !~ /^ *[0-9]+ +\/usr\/bin\/(zsh|bash)( |$)/ &&
        $0 !~ /awk -v pat=/ &&
        $0 !~ /[r]g / &&
        $0 !~ /[p]grep/ &&
        $0 !~ /magi_cli\.sh/ &&
        $0 !~ /\/bin\/(zsh|bash) -lc/ &&
        $0 !~ /\/bin\/bash -c/ &&
        $0 !~ /codex/ {
            print $1
            exit
        }
    '; } 2>/dev/null || true
}

_check_port() {
    local name="$1" port="$2"
    local pid
    pid=$(lsof -ti:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [ -n "$pid" ]; then
        printf "  ${GREEN}●${NC} %-18s port %-5s PID %-6s\n" "$name" "$port" "$pid"
    else
        printf "  ${RED}○${NC} %-18s port %-5s ${RED}DOWN${NC}\n" "$name" "$port"
    fi
}

_wait_for_pattern() {
    local pattern="$1" max_wait="${2:-20}" i
    for i in $(seq 1 "$max_wait"); do
        if [ -n "$(_find_process_by_pattern "$pattern")" ]; then
            return 0
        fi
        sleep 1
    done
    return 1
}

_wait_for_port() {
    local port="$1" max_wait="${2:-20}" i
    for i in $(seq 1 "$max_wait"); do
        if lsof -ti:"$port" -sTCP:LISTEN >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

_launchctl_present() {
    local label="$1"
    launchctl list "$label" >/dev/null 2>&1
}

_magi_root() {
    local root
    root=$(/usr/libexec/PlistBuddy -c 'Print :EnvironmentVariables:MAGI_ROOT' "$PLIST" 2>/dev/null || true)
    if [ -n "${root:-}" ] && [ -f "$root/daemon.py" ]; then
        echo "$root"
        return 0
    fi
    if [ -f "./daemon.py" ]; then
        pwd
        return 0
    fi
    echo "$HOME/Desktop/MAGI_v2"
}

_start_daemon_direct() {
    local root py
    root=$(_magi_root)
    py="$root/venv/bin/python3"
    if [ ! -x "$py" ]; then
        py="$root/.venv/bin/python"
    fi
    if [ ! -x "$py" ]; then
        py="python3"
    fi
    MAGI_ROOT_VALUE="$root" MAGI_PY_VALUE="$py" "$py" - <<'PY'
import os
import subprocess
from pathlib import Path

root = os.environ["MAGI_ROOT_VALUE"]
py = os.environ["MAGI_PY_VALUE"]
env = os.environ.copy()
env["MAGI_ROOT"] = root
env["MAGI_ROOT_DIR"] = root
stdout = open("/tmp/magi-daemon-stdout.log", "ab", buffering=0)
stderr = open("/tmp/magi-daemon.log", "ab", buffering=0)
proc = subprocess.Popen(
    [py, str(Path(root) / "daemon.py")],
    cwd=str(Path.home()),
    stdout=stdout,
    stderr=stderr,
    env=env,
    start_new_session=True,
    close_fds=True,
)
print(proc.pid)
PY
}

_start_menubar_direct() {
    local root py
    root=$(_magi_root)
    py="$root/venv/bin/python3"
    if [ ! -x "$py" ]; then
        py="$root/.venv/bin/python"
    fi
    if [ ! -x "$py" ]; then
        py="python3"
    fi
    MAGI_ROOT_VALUE="$root" MAGI_PY_VALUE="$py" "$py" - <<'PY'
import os
import subprocess
from pathlib import Path

root = os.environ["MAGI_ROOT_VALUE"]
py = os.environ["MAGI_PY_VALUE"]
env = os.environ.copy()
env["MAGI_ROOT"] = root
env["MAGI_ROOT_DIR"] = root
stdout = open("/tmp/magi-menubar.log", "ab", buffering=0)
proc = subprocess.Popen(
    [py, str(Path(root) / "gui" / "magi_menubar.py")],
    cwd=str(Path.home()),
    stdout=stdout,
    stderr=stdout,
    env=env,
    start_new_session=True,
    close_fds=True,
)
print(proc.pid)
PY
}

_check_port_with_label() {
    local name="$1" port="$2" label="$3"
    local pid
    pid=$(lsof -ti:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [ -n "$pid" ]; then
        if _launchctl_present "$label"; then
            printf "  ${GREEN}●${NC} %-18s port %-5s PID %-6s\n" "$name" "$port" "$pid"
        else
            printf "  ${YELLOW}▲${NC} %-18s port %-5s PID %-6s ${YELLOW}UNMANAGED${NC}\n" "$name" "$port" "$pid"
        fi
    else
        if _launchctl_present "$label"; then
            printf "  ${YELLOW}○${NC} %-18s port %-5s ${YELLOW}IDLE${NC}\n" "$name" "$port"
        else
            printf "  ${RED}○${NC} %-18s port %-5s ${RED}DOWN${NC}\n" "$name" "$port"
        fi
    fi
}

_omlx_active_profile() {
    local profile_file="${HOME}/.omlx/active_profile"
    if [ -f "$profile_file" ]; then
        tr -d '\r\n' < "$profile_file" 2>/dev/null || true
    fi
}

_check_omlx_day_sidecar() {
    local name="$1" port="$2" label="$3" profile="$4"
    local pid
    pid=$(lsof -ti:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    case "$profile" in
        night*)
            if [ -n "$pid" ]; then
                printf "  ${YELLOW}▲${NC} %-18s port %-5s PID %-6s ${YELLOW}UNEXPECTED_NIGHT${NC}\n" "$name" "$port" "$pid"
            else
                printf "  ${GREEN}●${NC} %-18s port %-5s ${GREEN}SLEEP (night profile)${NC}\n" "$name" "$port"
            fi
            ;;
        *)
            _check_port_with_label "$name" "$port" "$label"
            ;;
    esac
}

_estimate_magi_memory_gb() {
    # 估算 MAGI 核心 + oMLX 服務 RSS（GB）
    local pid_list
    pid_list=$(
        {
            pgrep -f "daemon.py|api/server.py|api/discord_bot.py|api/tools_api.py|omlx|mlx_lm" 2>/dev/null || true
            lsof -ti:8080 -sTCP:LISTEN 2>/dev/null || true
            lsof -ti:8081 -sTCP:LISTEN 2>/dev/null || true
            lsof -ti:8082 -sTCP:LISTEN 2>/dev/null || true
            lsof -ti:8083 -sTCP:LISTEN 2>/dev/null || true
        } | awk 'NF {print $1}' | sort -u
    )

    if [ -z "$pid_list" ]; then
        echo "?"
        return 0
    fi

    ps -o pid=,rss= -p $(echo "$pid_list" | tr '\n' ' ') 2>/dev/null | awk '
        NF >= 2 {sum += $2; found = 1}
        END {
            if (!found) {
                print "?"
            } else {
                printf "%.1f", sum / 1024 / 1024
            }
        }
    '
}

_process_hygiene_python() {
    local root py
    root=$(_magi_root)
    py="$root/venv/bin/python3"
    if [ ! -x "$py" ]; then
        py="$root/venv/bin/python"
    fi
    if [ ! -x "$py" ]; then
        py="$root/.venv/bin/python"
    fi
    if [ ! -x "$py" ]; then
        py="python3"
    fi
    echo "$py"
}

_osc_shell_nas_helper_port() {
    echo "${MAGI_OSC_SHELL_NAS_HELPER_PORT:-5016}"
}

_osc_shell_nas_helper_health() {
    local port
    port=$(_osc_shell_nas_helper_port)
    curl -fsS --max-time 2 "http://127.0.0.1:${port}/health" >/dev/null 2>&1
}

_osc_shell_nas_helper_pids() {
    ps -axo pid=,command= | awk '
        /[Pp]ython|Python\.app/ &&
        /scripts\/ops\/osc_shell_nas_helper\.py/ &&
        !/awk / {
            print $1
        }
    ' 2>/dev/null || true
}

_check_osc_shell_nas_helper() {
    local port pid
    port=$(_osc_shell_nas_helper_port)
    pid=$(lsof -ti:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [ -n "$pid" ] && _osc_shell_nas_helper_health; then
        printf "  ${GREEN}●${NC} %-18s port %-5s PID %-6s\n" "OSC NAS Helper" "$port" "$pid"
    elif [ -n "$pid" ]; then
        printf "  ${YELLOW}▲${NC} %-18s port %-5s PID %-6s ${YELLOW}UNHEALTHY${NC}\n" "OSC NAS Helper" "$port" "$pid"
    else
        printf "  ${RED}○${NC} %-18s port %-5s ${RED}DOWN${NC}\n" "OSC NAS Helper" "$port"
    fi
}

cmd_stop_osc_shell_nas_helper() {
    local port pids port_pid cmd pid
    port=$(_osc_shell_nas_helper_port)
    pids=$(_osc_shell_nas_helper_pids)
    port_pid=$(lsof -ti:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [ -n "$port_pid" ]; then
        cmd=$(ps -p "$port_pid" -o command= 2>/dev/null || true)
        if [[ "$cmd" == *"osc_shell_nas_helper.py"* ]]; then
            pids=$(printf "%s\n%s\n" "$pids" "$port_pid" | awk 'NF && !seen[$1]++')
        fi
    fi
    if [ -z "${pids//[[:space:]]/}" ]; then
        return 0
    fi
    for pid in $pids; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    for _ in $(seq 1 20); do
        local alive=""
        for pid in $pids; do
            if kill -0 "$pid" 2>/dev/null; then
                alive=1
                break
            fi
        done
        [ -z "$alive" ] && break
        sleep 0.2
    done
    for pid in $pids; do
        kill -KILL "$pid" 2>/dev/null || true
    done
}

cmd_start_osc_shell_nas_helper() {
    local root py script runtime_dir log_file pid_file port port_pid port_cmd
    root=$(_magi_root)
    py=$(_process_hygiene_python)
    script="$root/scripts/ops/osc_shell_nas_helper.py"
    runtime_dir="$root/.runtime"
    log_file="$runtime_dir/osc_shell_nas_helper.log"
    pid_file="$runtime_dir/osc_shell_nas_helper.shell.pid"
    port=$(_osc_shell_nas_helper_port)

    if _osc_shell_nas_helper_health; then
        return 0
    fi
    port_pid=$(lsof -ti:"$port" -sTCP:LISTEN 2>/dev/null | head -1 || true)
    if [ -n "$port_pid" ]; then
        port_cmd=$(ps -p "$port_pid" -o command= 2>/dev/null || true)
        if [[ "$port_cmd" == *"osc_shell_nas_helper.py"* ]]; then
            cmd_stop_osc_shell_nas_helper
        else
            echo "  OSC NAS helper port $port is occupied by PID $port_pid; not starting helper."
            return 1
        fi
    fi
    if [ ! -f "$script" ]; then
        echo "  OSC NAS helper script not found: $script"
        return 1
    fi
    mkdir -p "$runtime_dir"
    MAGI_OSC_HELPER_ROOT="$root" \
    MAGI_OSC_HELPER_PY="$py" \
    MAGI_OSC_HELPER_SCRIPT="$script" \
    MAGI_OSC_HELPER_LOG="$log_file" \
    MAGI_OSC_HELPER_PIDFILE="$pid_file" \
    "$py" - <<'PY'
import os
from pathlib import Path

root = Path(os.environ["MAGI_OSC_HELPER_ROOT"])
py = os.environ["MAGI_OSC_HELPER_PY"]
script = os.environ["MAGI_OSC_HELPER_SCRIPT"]
log_file = os.environ["MAGI_OSC_HELPER_LOG"]
pid_file = os.environ["MAGI_OSC_HELPER_PIDFILE"]
env = os.environ.copy()
env["MAGI_ROOT"] = str(root)
env["MAGI_ROOT_DIR"] = str(root)

pid = os.fork()
if pid:
    os.waitpid(pid, 0)
    raise SystemExit(0)
os.setsid()
pid2 = os.fork()
if pid2:
    Path(pid_file).write_text(str(pid2) + "\n", encoding="utf-8")
    os._exit(0)

os.chdir(str(root))
fd = os.open("/dev/null", os.O_RDONLY)
os.dup2(fd, 0)
os.close(fd)
Path(log_file).parent.mkdir(parents=True, exist_ok=True)
lfd = os.open(log_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
os.dup2(lfd, 1)
os.dup2(lfd, 2)
os.close(lfd)
os.execvpe(py, [py, script], env)
PY
    _wait_for_port "$port" 10 || return 1
    _osc_shell_nas_helper_health
}

cmd_restart_osc_shell_nas_helper() {
    cmd_stop_osc_shell_nas_helper
    sleep 1
    cmd_start_osc_shell_nas_helper
}

_magi_zombie_count() {
    local root py
    root=$(_magi_root)
    py=$(_process_hygiene_python)
    "$py" "$root/skills/process-hygiene/action.py" --task scan 2>/dev/null | "$py" -c 'import json,sys; print(json.load(sys.stdin).get("zombies",{}).get("count",0))' 2>/dev/null || echo "?"
}

_df_usage_with_timeout() {
    local path="$1"
    python3 - "$path" <<'PY' 2>/dev/null || echo "容量讀取失敗"
import subprocess
import sys

path = sys.argv[1]
try:
    proc = subprocess.run(["df", "-h", path], capture_output=True, text=True, timeout=2, check=False)
except subprocess.TimeoutExpired:
    print("容量讀取逾時")
    raise SystemExit(0)
lines = [line.split() for line in proc.stdout.splitlines() if line.split()]
if len(lines) >= 2 and len(lines[-1]) >= 5:
    row = lines[-1]
    print(f"{row[2]}/{row[1]} ({row[4]})")
else:
    print("容量讀取失敗")
PY
}

cmd_status() {
    echo "═══ MAGI System Status ═══"
    echo ""
    echo "Core Services:"
    _check_daemon
    _check "Server"       "api/server.py"
    _check "Discord Bot"  "api/discord_bot.py"
    _check "Tools API"    "api/tools_api.py"
    _check_port "RPC Worker"        50052
    echo ""
    echo "UI:"
    _check_menubar
    echo ""
    echo "Sidecars:"
    _check_osc_shell_nas_helper
    _check_port "Website Admin"     8088
    echo ""
    echo "oMLX Inference:"
    local omlx_profile
    omlx_profile=$(_omlx_active_profile)
    _check_port_with_label "Text (Gemma-4)" 8080 "com.magi.omlx"
    _check_port_with_label "Embed (BERT)"   8081 "com.magi.omlx-embed"
    _check_omlx_day_sidecar "Logic (Phi-4)"  8082 "com.magi.omlx-phi4" "$omlx_profile"
    _check_omlx_day_sidecar "Cross (SmolLM3)" 8083 "com.magi.omlx-smol" "$omlx_profile"
    echo ""

    echo ""

    # NAS mounts — 從 MAGI_NAS_SHARES env var 讀取（與 nas_mount_guard 同步）
    echo "NAS Mounts:"
    # 優先讀 env；沒有再從 .env 撈
    local magi_root_guess="${MAGI_ROOT:-/Users/ai/Desktop/MAGI_v2}"
    local nas_shares_env="${MAGI_NAS_SHARES:-}"
    if [ -z "$nas_shares_env" ] && [ -f "$magi_root_guess/.env" ]; then
        nas_shares_env=$(grep -E "^MAGI_NAS_SHARES=" "$magi_root_guess/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"' | tr -d "'")
    fi
    local vol_list
    if [ -n "$nas_shares_env" ]; then
        vol_list=""
        for s in ${nas_shares_env//,/ }; do
            vol_list="$vol_list /Volumes/$s"
        done
    else
        vol_list="/Volumes/homes /Volumes/lumi"
    fi
    for vol in $vol_list; do
        # 先顯示真正 SMB mount；Synology Drive 只列為 fallback，避免誤判成已掛載。
        local share_name
        share_name="$(basename "$vol")"
        local mounted_path=""
        for candidate in "$vol" "${vol}-1" "${vol}-2"; do
            if mount | grep -q "$candidate"; then
                mounted_path="$candidate"
                break
            fi
        done
        if [ -z "$mounted_path" ]; then
            local user_mount="$HOME/.magi_mounts/$share_name"
            if mount | grep -q "$user_mount"; then
                mounted_path="$user_mount"
            fi
        fi
        if [ -n "$mounted_path" ]; then
            local usage
            if [ "${MAGI_STATUS_SHOW_CAPACITY:-0}" = "1" ]; then
                usage=$(_df_usage_with_timeout "$mounted_path")
            else
                usage="MOUNTED"
            fi
            printf "  ${GREEN}●${NC} %-18s %s\n" "$share_name" "$usage"
        elif [ "$share_name" = "lumi" ] && { [ -d "$HOME/SynologyDrive/01_案件" ] || [ -d "$HOME/Library/CloudStorage/SynologyDrive-homes/01_案件" ]; }; then
            printf "  ${YELLOW}◐${NC} %-18s ${YELLOW}FALLBACK: Synology Drive sync; SMB NOT MOUNTED${NC}\n" "$share_name"
        elif [ -d "$HOME/Library/CloudStorage/SynologyDrive-$share_name" ]; then
            printf "  ${YELLOW}◐${NC} %-18s ${YELLOW}FALLBACK: Synology Drive sync; SMB NOT MOUNTED${NC}\n" "$share_name"
        else
            printf "  ${RED}○${NC} %-18s ${RED}NOT MOUNTED${NC}\n" "$share_name"
        fi
    done
    echo ""

    # DB
    echo "Database:"
    local db_local
    db_local=$(nc -z -w2 127.0.0.1 3306 2>/dev/null && echo "UP" || echo "DOWN")
    if [ "$db_local" = "UP" ]; then
        printf "  ${GREEN}●${NC} MariaDB (local)\n"
    else
        printf "  ${RED}○${NC} ${RED}MariaDB 離線${NC}\n"
    fi
    echo ""

    # Zombie check
    local zombies
    zombies=$(_magi_zombie_count)
    if [ "$zombies" = "?" ]; then
        printf "Zombies: ${YELLOW}?${NC} (process-hygiene unavailable)\n"
    elif [ "$zombies" -gt 0 ]; then
        printf "Zombies: ${RED}%s zombie process(es)${NC}\n" "$zombies"
    else
        printf "Zombies: ${GREEN}0${NC}\n"
    fi

    # FAISS Vector DB
    echo ""
    echo "Vector DB:"
    local magi_root faiss_meta faiss_vectors
    magi_root="$HOME/Desktop/MAGI_v2"
    faiss_meta="$magi_root/skills/memory/index_cache/meta.json"
    if [ -f "$faiss_meta" ]; then
        faiss_vectors=$(python3 -c "import json; d=json.load(open('$faiss_meta')); print(d.get('total',0))" 2>/dev/null || echo "")
        if [ -n "$faiss_vectors" ] && [ "$faiss_vectors" != "0" ]; then
            local faiss_fmt
            faiss_fmt=$(python3 -c "print(f'{int($faiss_vectors):,}')" 2>/dev/null || echo "$faiss_vectors")
            printf "  ${GREEN}●${NC} FAISS  %s vectors\n" "$faiss_fmt"
        else
            printf "  ${YELLOW}⚠${NC} FAISS  索引為空\n"
        fi
    else
        printf "  ${YELLOW}⚠${NC} FAISS  meta.json 不存在\n"
    fi

    # Memory
    local mem_used
    mem_used=$(_estimate_magi_memory_gb)
    echo "Memory:  ~${mem_used}GB (MAGI + oMLX)"
}

cmd_start() {
    echo "Starting MAGI..."
    # Start daemon
    echo "  Starting daemon..."
    launchctl bootstrap gui/$(id -u) "$PLIST" 2>/dev/null || launchctl load "$PLIST" 2>/dev/null || true
    if [ -f "$RPC_PLIST" ]; then
        echo "  Starting RPC worker..."
        launchctl bootstrap gui/$(id -u) "$RPC_PLIST" 2>/dev/null || launchctl load "$RPC_PLIST" 2>/dev/null || true
    fi
    # Start menubar
    if [ -f "$MENUBAR_PLIST" ]; then
        echo "  Starting status bar..."
        launchctl bootstrap gui/$(id -u) "$MENUBAR_PLIST" 2>/dev/null || launchctl load "$MENUBAR_PLIST" 2>/dev/null || true
        sleep 3
        if [ -z "$(_find_menubar_process)" ]; then
            echo "  LaunchAgent did not bring status bar up; starting status bar directly..."
            launchctl bootout gui/$(id -u)/$MENUBAR_LABEL 2>/dev/null || launchctl unload "$MENUBAR_PLIST" 2>/dev/null || true
            _kill_menubar_processes
            _start_menubar_direct >/dev/null
        fi
    fi
    echo "  Waiting for web services..."
    local need_direct=0
    _wait_for_pattern "api/server.py" 30 || need_direct=1
    _wait_for_port 5002 30 || need_direct=1
    _wait_for_pattern "api/tools_api.py" 20 || need_direct=1
    _wait_for_port 5003 20 || need_direct=1
    if [ "$need_direct" = "1" ]; then
        echo "  LaunchAgent did not bring web services up; starting daemon directly..."
        launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
        pkill -f "daemon.py" 2>/dev/null || true
        pkill -f "api/server.py" 2>/dev/null || true
        pkill -f "api/discord_bot.py" 2>/dev/null || true
        pkill -f "api/tools_api.py" 2>/dev/null || true
        sleep 2
        _start_daemon_direct
        _wait_for_pattern "api/server.py" 60 || true
        _wait_for_port 5002 60 || true
        _wait_for_pattern "api/tools_api.py" 30 || true
        _wait_for_port 5003 30 || true
    fi
    _wait_for_pattern "api/discord_bot.py" 20 || true
    echo "  Starting OSC NAS helper..."
    cmd_start_osc_shell_nas_helper || true
    sleep 1
    cmd_status
}

cmd_stop() {
    echo "Stopping MAGI..."
    # Stop daemon
    launchctl bootout gui/$(id -u)/$LABEL 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null || true
    sleep 1
    if [ -f "$RPC_PLIST" ]; then
        echo "  Stopping RPC worker..."
        launchctl bootout gui/$(id -u)/$RPC_LABEL 2>/dev/null || launchctl unload "$RPC_PLIST" 2>/dev/null || true
    fi
    sleep 1
    # Stop menubar
    if [ -f "$MENUBAR_PLIST" ]; then
        echo "  Stopping status bar..."
        launchctl bootout gui/$(id -u)/$MENUBAR_LABEL 2>/dev/null || launchctl unload "$MENUBAR_PLIST" 2>/dev/null || true
    fi
    sleep 1
    # Kill any remaining MAGI processes
    pkill -f "daemon.py" 2>/dev/null || true
    pkill -f "api/server.py" 2>/dev/null || true
    pkill -f "api/discord_bot.py" 2>/dev/null || true
    pkill -f "api/tools_api.py" 2>/dev/null || true
    pkill -f "skills/ops/file_review_auto_worker.py" 2>/dev/null || true
    pkill -f "skills/ops/heartbeat.py" 2>/dev/null || true
    pkill -f "whalechao.github.io/admin/admin_server.py" 2>/dev/null || true
    echo "  Stopping OSC NAS helper..."
    cmd_stop_osc_shell_nas_helper
    _kill_menubar_processes
    pkill -f "rpc-server" 2>/dev/null || true
    sleep 2
    echo "MAGI stopped."
}

cmd_restart() {
    cmd_stop
    sleep 2
    cmd_start
}

cmd_menubar() {
    echo "Restarting status bar..."
    # macOS 26 + Homebrew Python 3.14 can crash during launchd getpath.
    # Keep launchd from respawning the bash wrapper, then start the GUI process
    # directly in the current GUI user session.
    launchctl bootout gui/$(id -u)/$MENUBAR_LABEL 2>/dev/null || launchctl unload "$MENUBAR_PLIST" 2>/dev/null || true
    _kill_menubar_processes
    sleep 1
    _start_menubar_direct >/dev/null
    sleep 4
    _check_menubar
}

cmd_zombie() {
    local root py
    root=$(_magi_root)
    py=$(_process_hygiene_python)
    "$py" "$root/skills/process-hygiene/action.py" --task zombies
}

case "${1:-status}" in
    status|s)    cmd_status ;;
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart|r)   cmd_restart ;;
    menubar|bar) cmd_menubar ;;
    zombie|z)    cmd_zombie ;;
    helper-start)   cmd_start_osc_shell_nas_helper ;;
    helper-stop)    cmd_stop_osc_shell_nas_helper ;;
    helper-restart) cmd_restart_osc_shell_nas_helper ;;
    helper-status)  _check_osc_shell_nas_helper ;;
    *)
        echo "Usage: magi [status|start|stop|restart|menubar|zombie|helper-start|helper-stop|helper-restart|helper-status]"
        echo ""
        echo "  status   Show all MAGI service status (default)"
        echo "  start    Start MAGI daemon + status bar"
        echo "  stop     Stop MAGI daemon + all services + status bar"
        echo "  restart  Stop then start (includes status bar)"
        echo "  menubar  Restart only the status bar"
        echo "  zombie   Check and clean zombie processes"
        echo "  helper-* Manage OSC NAS helper on port 5016"
        ;;
esac
