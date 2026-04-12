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
RPC_LABEL="com.magi.rpc"
RPC_PLIST="$HOME/Library/LaunchAgents/$RPC_LABEL.plist"

_check() {
    local name="$1" pattern="$2"
    local pid
    pid=$(pgrep -f "$pattern" 2>/dev/null | head -1 || true)
    if [ -n "$pid" ]; then
        printf "  ${GREEN}●${NC} %-18s PID %-6s\n" "$name" "$pid"
    else
        printf "  ${RED}○${NC} %-18s ${RED}DOWN${NC}\n" "$name"
    fi
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

cmd_status() {
    echo "═══ MAGI System Status ═══"
    echo ""
    echo "Core Services:"
    _check "Daemon"       "daemon.py"
    _check "Server"       "api/server.py"
    _check "Discord Bot"  "api/discord_bot.py"
    _check "Tools API"    "api/tools_api.py"
    _check_port "RPC Worker"        50052
    echo ""
    echo "UI:"
    _check "Status Bar"   "gui/magi_menubar.py"
    echo ""
    echo "Sidecars:"
    _check_port "LINE Desktop MCP"  3012
    _check_port "Website Admin"     8088
    echo ""
    echo "oMLX Inference:"
    _check_port "Text (Gemma-4)" 8080
    _check_port "Embed (BERT)"   8081
    echo ""

    echo ""

    # NAS mounts
    echo "NAS Mounts:"
    for vol in /Volumes/homes /Volumes/lumi; do
        if mount | grep -q "$vol"; then
            local usage
            usage=$(df -h "$vol" 2>/dev/null | tail -1 | awk '{print $3"/"$2" ("$5")"}')
            printf "  ${GREEN}●${NC} %-18s %s\n" "$(basename $vol)" "$usage"
        else
            printf "  ${RED}○${NC} %-18s ${RED}NOT MOUNTED${NC}\n" "$(basename $vol)"
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
    zombies=$(ps aux | awk '$8=="Z"' | wc -l | tr -d ' ')
    if [ "$zombies" -gt 0 ]; then
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
    mem_used=$(ps -eo rss,comm 2>/dev/null | grep -E "omlx|daemon.py|server.py|discord_bot|tools_api" | awk '{sum+=$1} END {printf "%.1f", sum/1024/1024}' 2>/dev/null || echo "?")
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
    fi
    sleep 3
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
    pkill -f "gui/magi_menubar.py" 2>/dev/null || true
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
    # Use launchctl kickstart -k to force restart
    launchctl kickstart -k gui/$(id -u)/$MENUBAR_LABEL 2>/dev/null || {
        # Fallback: kill and let KeepAlive restart it
        pkill -f "gui/magi_menubar.py" 2>/dev/null || true
        sleep 2
        launchctl bootstrap gui/$(id -u) "$MENUBAR_PLIST" 2>/dev/null || launchctl load "$MENUBAR_PLIST" 2>/dev/null || true
    }
    sleep 2
    _check "Status Bar" "gui/magi_menubar.py"
}

cmd_zombie() {
    local zombie_info
    zombie_info=$(ps -eo pid=,ppid=,stat=,command= | awk '$3=="Z" || $3=="Z+"')
    if [ -z "$zombie_info" ]; then
        printf "${GREEN}No zombie processes.${NC}\n"
        return
    fi

    echo "Zombie processes found:"
    echo "$zombie_info"
    echo ""

    local parent_pids
    parent_pids=$(echo "$zombie_info" | awk '{print $2}' | sort -u)

    echo "Sending SIGCHLD to parent processes..."
    for ppid in $parent_pids; do
        if [ "$ppid" = "1" ]; then
            continue
        fi
        kill -SIGCHLD "$ppid" 2>/dev/null || true
    done

    sleep 1

    local remaining
    remaining=$(ps -eo stat= | grep -c '^Z' 2>/dev/null || echo "0")
    if [ "$remaining" -eq 0 ]; then
        printf "${GREEN}All zombies reaped successfully.${NC}\n"
        return
    fi

    printf "${YELLOW}%s zombie(s) remain — killing unresponsive parents...${NC}\n" "$remaining"
    zombie_info=$(ps -eo pid=,ppid=,stat=,command= | awk '$3=="Z" || $3=="Z+"')
    parent_pids=$(echo "$zombie_info" | awk '{print $2}' | sort -u)
    for ppid in $parent_pids; do
        if [ "$ppid" = "1" ]; then
            continue
        fi
        local pname
        pname=$(ps -p "$ppid" -o command= 2>/dev/null || echo "unknown")
        printf "  Killing parent PID %s (%s)\n" "$ppid" "$pname"
        kill -TERM "$ppid" 2>/dev/null || true
    done
    sleep 2

    remaining=$(ps -eo stat= | grep -c '^Z' 2>/dev/null || echo "0")
    if [ "$remaining" -eq 0 ]; then
        printf "${GREEN}All zombies cleaned (parents terminated).${NC}\n"
    else
        printf "${RED}%s zombie(s) still remain (parent may be launchd/system).${NC}\n" "$remaining"
    fi
}

case "${1:-status}" in
    status|s)    cmd_status ;;
    start)       cmd_start ;;
    stop)        cmd_stop ;;
    restart|r)   cmd_restart ;;
    menubar|bar) cmd_menubar ;;
    zombie|z)    cmd_zombie ;;
    *)
        echo "Usage: magi [status|start|stop|restart|menubar|zombie]"
        echo ""
        echo "  status   Show all MAGI service status (default)"
        echo "  start    Start MAGI daemon + status bar"
        echo "  stop     Stop MAGI daemon + all services + status bar"
        echo "  restart  Stop then start (includes status bar)"
        echo "  menubar  Restart only the status bar"
        echo "  zombie   Check and clean zombie processes"
        ;;
esac
