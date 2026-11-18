#!/usr/bin/env bash
# Starts vault-ingest's background programs when you log in. macOS and Linux.
#   1. "Vault ingest watcher" - processes transcripts dropped into 00_Inbox
#   2. "Meeting companion"    - detects meetings, shows the client picker, collects screenshots
#   3. "LM Studio server"     - `lms server start`, if LM Studio's `lms` command is installed
#   4. "Obsidian MCP server"  - only if bin/obsidian-mcp (or ~/.cargo/bin/obsidian-mcp) exists
# macOS: LaunchAgents in ~/Library/LaunchAgents. Linux: autostart entries in ~/.config/autostart (they start
# with your desktop session, so the companion can open windows).
# Run once:            bash unix/register-startup.sh
# Remove later with:   bash unix/register-startup.sh --remove
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
py="$root/.venv/bin/python"
logs="$root/state/logs"
remove="${1:-}"

if [ "$remove" != "--remove" ] && [ ! -x "$py" ]; then
  echo "Run unix/install.sh first."; exit 1
fi
mkdir -p "$logs"

lms_bin="$(command -v lms || true)"
[ -z "$lms_bin" ] && [ -x "$HOME/.lmstudio/bin/lms" ] && lms_bin="$HOME/.lmstudio/bin/lms"
mcp_bin=""
for c in "$root/bin/obsidian-mcp" "$HOME/.cargo/bin/obsidian-mcp"; do [ -x "$c" ] && mcp_bin="$c" && break; done

case "$(uname -s)" in
Darwin)
  dir="$HOME/Library/LaunchAgents"
  mkdir -p "$dir"
  agent() {  # label, then the program and its arguments
    local label="$1"; shift
    local plist="$dir/$label.plist"
    launchctl bootout "gui/$(id -u)" "$plist" 2>/dev/null || true
    rm -f "$plist"
    [ "$remove" = "--remove" ] && { echo "Removed $label"; return; }
    {
      echo '<?xml version="1.0" encoding="UTF-8"?>'
      echo '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">'
      echo '<plist version="1.0"><dict>'
      echo "  <key>Label</key><string>$label</string>"
      echo '  <key>ProgramArguments</key><array>'
      for a in "$@"; do echo "    <string>$a</string>"; done
      echo '  </array>'
      echo "  <key>WorkingDirectory</key><string>$root</string>"
      echo '  <key>RunAtLoad</key><true/>'
      echo '  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>'
      echo '  <key>ProcessType</key><string>Interactive</string>'
      echo "  <key>StandardOutPath</key><string>$logs/$label.out.log</string>"
      echo "  <key>StandardErrorPath</key><string>$logs/$label.err.log</string>"
      echo '</dict></plist>'
    } > "$plist"
    launchctl bootstrap "gui/$(id -u)" "$plist"
    echo "Registered $label"
  }
  [ -n "$mcp_bin" ] && agent com.vault-ingest.obsidian-mcp /bin/bash "$root/unix/start-obsidian-mcp.sh"
  [ -n "$lms_bin" ] && agent com.vault-ingest.lm-studio "$lms_bin" server start
  agent com.vault-ingest.watcher "$py" -m vault_ingest watch
  agent com.vault-ingest.companion "$py" -m vault_ingest companion
  if [ "$remove" != "--remove" ]; then
    echo
    echo "macOS will ask for permissions the first time. Allow them for 'python' (or your terminal):"
    echo "  - Screen Recording: meeting window titles and participant screenshots"
    echo "  - Accessibility / Input Monitoring: the global hotkeys (Ctrl+Alt+C, Ctrl+Alt+S, Ctrl+Alt+G)"
    echo "Then run this script again (or log out and in) so the companion restarts with them."
  fi
  ;;
*)
  dir="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"
  mkdir -p "$dir"
  entry() {  # file name, display name, command line
    local file="$dir/$1.desktop"
    rm -f "$file"
    [ "$remove" = "--remove" ] && { echo "Removed $2"; return; }
    cat > "$file" <<EOF
[Desktop Entry]
Type=Application
Name=$2
Exec=/bin/sh -c 'cd "$root" && exec $3 >>"$logs/$1.log" 2>&1'
X-GNOME-Autostart-enabled=true
NoDisplay=true
Terminal=false
EOF
    echo "Registered $2 ($file)"
  }
  [ -n "$mcp_bin" ] && entry vault-ingest-obsidian-mcp "Obsidian MCP server" "/bin/bash \"$root/unix/start-obsidian-mcp.sh\""
  [ -n "$lms_bin" ] && entry vault-ingest-lm-studio "LM Studio server" "\"$lms_bin\" server start"
  entry vault-ingest-watcher "Vault ingest watcher" "\"$py\" -m vault_ingest watch"
  entry vault-ingest-companion "Meeting companion" "\"$py\" -m vault_ingest companion"
  if [ "$remove" != "--remove" ]; then
    echo
    echo "They start at your next login. To start them now without logging out:"
    echo "  (cd \"$root\" && nohup \"$py\" -m vault_ingest watch >/dev/null 2>&1 &)"
    echo "  (cd \"$root\" && nohup \"$py\" -m vault_ingest companion >/dev/null 2>&1 &)"
    if [ "${XDG_SESSION_TYPE:-}" = "wayland" ]; then
      echo
      echo "Wayland session: window titles are only visible for apps running under XWayland, so most calls"
      echo "are found through the microphone (pactl). Global hotkeys may need an X11 session."
    fi
  fi
  ;;
esac
