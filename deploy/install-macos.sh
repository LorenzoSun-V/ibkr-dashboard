#!/bin/bash
# 用 launchd 让 dashboard 开机自启、退出后自动重启。可以重复运行（更新代码、移动目录后再跑一次）。
#
#   deploy/install-macos.sh            安装或重新加载
#   deploy/install-macos.sh uninstall  停止并取消自启
#
# 日志写在内置硬盘的 ~/Library/Logs/ibkr-dashboard/server.log：项目放在外接硬盘（/Volumes/...）上时，
# launchd 打不开硬盘上的日志文件，服务会启动失败（launchctl 里显示 78: EX_CONFIG）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.ibkr-dashboard.server"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
LOGS="$HOME/Library/Logs/ibkr-dashboard"
DOMAIN="gui/$(id -u)"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
if [ "${1:-}" = "uninstall" ]; then
  rm -f "$PLIST"
  echo "已停止并取消自启"
  exit 0
fi

PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || { echo "找不到 $PY：先按 README「二、安装」建好虚拟环境"; exit 1; }
mkdir -p "$LOGS" "$HOME/Library/LaunchAgents"

# 硬盘没挂载时不启动，挂上后再启动；退出后 30 秒再拉起，避免反复失败刷日志
cat > "$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>-m</string><string>app.server</string>
  </array>
  <key>WorkingDirectory</key><string>$ROOT</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>PathState</key><dict><key>$ROOT</key><true/></dict></dict>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>$LOGS/server.log</string>
  <key>StandardErrorPath</key><string>$LOGS/server.log</string>
</dict></plist>
PLIST
plutil -lint "$PLIST" >/dev/null
launchctl bootstrap "$DOMAIN" "$PLIST"

echo "已安装：$PLIST"
echo "日志：$LOGS/server.log"
case "$ROOT" in
  /Volumes/*|"$HOME/Documents"*|"$HOME/Desktop"*|"$HOME/Downloads"*)
    REAL="$("$PY" -c 'import os, sys; print(os.path.realpath(sys.executable))')"
    echo
    echo "项目在外接硬盘或受保护的目录里，后台服务要有「完全磁盘访问权限」才能读："
    echo "  系统设置 → 隐私与安全性 → 完全磁盘访问权限 → 点「＋」→ 按 ⌘⇧G 粘贴下面的路径 → 打开开关"
    echo "  $REAL"
    echo "然后再运行一次 deploy/install-macos.sh"
    ;;
esac
sleep 3
if launchctl print "$DOMAIN/$LABEL" 2>/dev/null | grep -q "state = running"; then
  echo "服务在运行"
else
  echo "服务还没起来，看日志：tail -20 $LOGS/server.log"
  echo "和状态：launchctl print $DOMAIN/$LABEL | grep -iE 'state|last exit'"
fi
