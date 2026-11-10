#!/usr/bin/env bash
# Install Mino Scout next to config.json and register a daemon.
#
# 分层安装：zip 里带哪几层就装哪几层，没带的层原地不动。
#
#   layers.txt + runtime/         → 替换 bin/mino-scout 与 bin/_internal
#   layers.txt + app/             → 替换 bin/app
#   layers.txt + browser/         → 替换 bin/ms-playwright
#   mino_scout/ + pyproject.toml  → 源码包（dev）：venv + pip，需要 Python 3.12+
#
# **刻意不再整体 rm -rf bin**：浏览器层实测 781 MB、占安装体积 78%，而它几乎从不变。
# 分层前每次更新都要连它一起重下重铺，这是包体问题的一半。
#
# **安装根刻意仍是 $PREFIX/bin**：Studio 的 scoutBinCandidates() 和 launchd plist
# 都硬编码了 $PREFIX/bin/mino-scout 与 $PREFIX/bin/ms-playwright，换根等于跨仓破坏。
# 层与目录的映射见 scripts/layers.py 的 PAYLOAD_DIRS —— 改那里要同步改这里。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"

case "$(uname -s)" in
  Darwin|Linux) ;;
  *) echo "此脚本仅支持 macOS / Linux；Windows 请使用 install.ps1" >&2; exit 1 ;;
esac

PREFIX="${MINO_SCOUT_HOME:-$HOME/.mino/scout}"
LOG_DIR="$PREFIX/logs"

# One explicit installation root for installer children and CLI commands.
export MINO_SCOUT_HOME="$PREFIX"

DEST="$PREFIX/bin"
BIN=""
LAYER_NAMES="runtime app browser"
ROLLBACK_SNAPSHOT=""
ROLLBACK_PATHS=()

# macOS /bin/bash 是 3.2。set -u 下空数组 "${arr[@]}" 会报 unbound variable，
# 且行号指向函数定义而不是 for 本身。先看长度再展开。
rollback_restore() {
  local p
  if [[ ${#ROLLBACK_PATHS[@]} -gt 0 ]]; then
    for p in "${ROLLBACK_PATHS[@]}"; do
      [[ -n "$p" ]] || continue
      if [[ -e "${p}.old" ]]; then
        rm -rf "$p"
        mv "${p}.old" "$p"
      fi
    done
  fi
  if [[ -n "$ROLLBACK_SNAPSHOT" && -f "$ROLLBACK_SNAPSHOT" ]]; then
    cp "$ROLLBACK_SNAPSHOT" "$DEST/layers.txt" 2>/dev/null || true
  fi
  echo "安装失败，已从 .old 快照回滚" >&2
}

install_failed() {
  local code=$?
  rollback_restore
  exit "$code"
}

echo "Mino Scout → $PREFIX"
mkdir -p "$PREFIX" "$LOG_DIR"

# ---------------- 层清单读取 ----------------

# zip 根的 layers.txt：`<层> <指纹> [k=v ...]`，# 开头是注释。
zip_layer_key() {
  [[ -f "$ROOT/layers.txt" ]] || return 0
  awk -v l="$1" '/^#/ {next} $1==l {print $2; exit}' "$ROOT/layers.txt"
}

zip_layer_field() {
  [[ -f "$ROOT/layers.txt" ]] || return 0
  awk -v l="$1" -v k="$2" '
    /^#/ {next}
    $1==l { for (i=3; i<=NF; i++) { split($i, kv, "="); if (kv[1]==k) { print kv[2]; exit } } }
  ' "$ROOT/layers.txt"
}

# 安装后的 bin/layers.txt：只有 `<层> <指纹>` 两列，是已装状态。
installed_layer_key() {
  [[ -f "$DEST/layers.txt" ]] || return 0
  awk -v l="$1" '/^#/ {next} $1==l {print $2; exit}' "$DEST/layers.txt"
}

record_layer() {
  local layer="$1" key="$2" f="$DEST/layers.txt" tmp
  tmp="$(mktemp)"
  if [[ -f "$f" ]]; then
    grep -v -E "^${layer}[[:space:]]" "$f" > "$tmp" || true
  fi
  printf '%s %s\n' "$layer" "$key" >> "$tmp"
  LC_ALL=C sort "$tmp" -o "$tmp"
  mv "$tmp" "$f"
}

layer_target() {
  # 层 → bin/ 下的相对路径。runtime 平铺到 bin/ 根，所以是空串。
  case "$1" in
    runtime) printf '%s' "" ;;
    app)     printf '%s' "app" ;;
    browser) printf '%s' "ms-playwright" ;;
    *) echo "unknown layer: $1" >&2; return 1 ;;
  esac
}

# ---------------- 落地 ----------------

# 先拷到 .new 再 rename，把"目标已删、新的还没到位"的窗口压到一次 mv。
replace_path() {
  local src="$1" dst="$2"
  rm -rf "$dst.new"
  cp -R "$src" "$dst.new"
  if [[ -e "$dst" ]]; then
    rm -rf "$dst.old"
    mv "$dst" "$dst.old"
    ROLLBACK_PATHS+=("$dst")
  fi
  mv "$dst.new" "$dst"
}

harden() {
  # GitHub 的 zip 常把嵌套的 Playwright/adb 二进制的 +x 丢掉；macOS 还会加隔离属性。
  local path="$1"
  [[ -e "$path" ]] || return 0
  if [[ "$(uname -s)" == "Darwin" ]]; then
    xattr -cr "$path" >/dev/null 2>&1 || true
  fi
  if [[ -d "$path" ]]; then
    find "$path" -type f -exec chmod u+x {} + 2>/dev/null || true
  else
    chmod u+x "$path" 2>/dev/null || true
  fi
}

install_cli_link() {
  [[ -n "${BIN:-}" && -x "$BIN" ]] || return 0
  local link_dir="${MINO_SCOUT_BIN_LINK_DIR:-$HOME/.local/bin}"
  mkdir -p "$link_dir"
  ln -sf "$BIN" "$link_dir/mino-scout"
  echo "CLI 链接: $link_dir/mino-scout → $BIN"
}

install_layer() {
  local layer="$1" src="$ROOT/$layer" rel target
  rel="$(layer_target "$layer")"
  if [[ -z "$rel" ]]; then
    # runtime：逐个顶层条目替换，这样将来多出别的顶层文件也不用改脚本。
    local entry
    for entry in "$src"/*; do
      [[ -e "$entry" ]] || continue
      target="$DEST/$(basename "$entry")"
      replace_path "$entry" "$target"
      harden "$target"
    done
  else
    target="$DEST/$rel"
    replace_path "$src" "$target"
    harden "$target"
  fi
  echo "  层 $layer → ${rel:-bin/}  ($(zip_layer_key "$layer"))"
}

# app 层单独更新时，必须确认本机 runtime 与它是同一套依赖 —— 否则新代码引用了
# 本机 runtime 里没有的依赖，换完就起不来，而症状是启动时一句 ImportError。
check_app_gate() {
  local need have
  need="$(zip_layer_field app requires_runtime)"
  [[ -n "$need" ]] || return 0
  have="$(installed_layer_key runtime)"
  if [[ -z "$have" ]]; then
    echo "app layer needs runtime $need but nothing is installed here." >&2
    echo "Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip) first." >&2
    exit 1
  fi
  if [[ "$have" != "$need" ]]; then
    echo "app layer needs runtime $need but $have is installed." >&2
    echo "Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip) instead." >&2
    exit 1
  fi
}

install_layers() {
  local layer present=0
  mkdir -p "$DEST"
  trap install_failed ERR

  if [[ -f "$DEST/layers.txt" ]]; then
    ROLLBACK_SNAPSHOT="$(mktemp)"
    cp "$DEST/layers.txt" "$ROLLBACK_SNAPSHOT"
  fi

  # app-only 增量：先过闸门，再动任何文件。
  if [[ -d "$ROOT/app" && ! -d "$ROOT/runtime" ]]; then
    check_app_gate
  fi

  for layer in $LAYER_NAMES; do
    [[ -d "$ROOT/$layer" ]] || continue
    install_layer "$layer"
    record_layer "$layer" "$(zip_layer_key "$layer")"
    present=$((present + 1))
  done

  if [[ "$present" -eq 0 ]]; then
    echo "layers.txt found but no payload directory (runtime/ app/ browser/)" >&2
    exit 1
  fi

  if [[ ! -f "$DEST/mino-scout" ]]; then
    echo "runtime layer missing: $DEST/mino-scout not present after install" >&2
    echo "Run a full install (the combined MinoScout-<ver>-<os>-<arch>.zip)." >&2
    exit 1
  fi
  chmod +x "$DEST/mino-scout" 2>/dev/null || true
  if [[ ! -f "$DEST/app/mino_scout/cli.py" ]]; then
    echo "app layer missing: $DEST/app/mino_scout/cli.py not present after install" >&2
    exit 1
  fi
  BIN="$DEST/mino-scout"

  local p
  if [[ ${#ROLLBACK_PATHS[@]} -gt 0 ]]; then
    for p in "${ROLLBACK_PATHS[@]}"; do
      [[ -n "$p" ]] || continue
      rm -rf "${p}.old"
    done
  fi
  ROLLBACK_PATHS=()
  [[ -n "$ROLLBACK_SNAPSHOT" ]] && rm -f "$ROLLBACK_SNAPSHOT"
  trap - ERR
}

install_source() {
  local app="$PREFIX/app"
  local venv="$PREFIX/venv"
  if ! python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)'; then
    echo "This zip is a source payload and needs Python 3.12+. Prefer the frozen GitHub Release." >&2
    exit 1
  fi
  mkdir -p "$app"
  rm -rf "$app/mino_scout"
  cp -R "$ROOT/mino_scout" "$app/mino_scout"
  cp "$ROOT/pyproject.toml" "$app/pyproject.toml"
  python3 -m venv "$venv"
  "$venv/bin/python" -m pip install -U pip
  if [[ "$(uname -s)" == "Darwin" ]]; then
    "$venv/bin/python" -m pip install "$app[desktop]"
  else
    "$venv/bin/python" -m pip install "$app"
  fi
  BIN="$venv/bin/mino-scout"
  if [[ ! -x "$BIN" ]]; then
    echo "install failed: $BIN not created" >&2
    exit 1
  fi
}

stop_running() {
  local candidate=""
  if [[ -x "$DEST/mino-scout" ]]; then
    candidate="$DEST/mino-scout"
  elif [[ -x "$PREFIX/venv/bin/mino-scout" ]]; then
    candidate="$PREFIX/venv/bin/mino-scout"
  fi
  if [[ -n "$candidate" ]]; then
    "$candidate" stop >/dev/null 2>&1 || true
    local pid_file="$PREFIX/scout.pid"
    if [[ -f "$pid_file" ]]; then
      local pid
      pid="$(tr -d '[:space:]' < "$pid_file" 2>/dev/null || true)"
      if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" >/dev/null 2>&1; then
        kill -TERM "$pid" >/dev/null 2>&1 || true
        sleep 0.5
        kill -0 "$pid" >/dev/null 2>&1 && kill -KILL "$pid" >/dev/null 2>&1 || true
      fi
    fi
  fi
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local uid target plist
    uid="$(id -u)"
    target="gui/${uid}/com.mino.scout"
    launchctl kill SIGTERM "$target" >/dev/null 2>&1 || true
    launchctl bootout "$target" >/dev/null 2>&1 || true
    for plist in "$HOME/Library/LaunchAgents/com.mino.scout.plist" \
                 "/Library/LaunchDaemons/com.mino.scout.plist"; do
      [[ -f "$plist" ]] || continue
      launchctl unload "$plist" >/dev/null 2>&1 || true
    done
  elif command -v systemctl >/dev/null 2>&1; then
    if [[ "$(id -u)" -eq 0 ]]; then
      systemctl stop mino-scout.service >/dev/null 2>&1 || true
    else
      systemctl --user stop mino-scout.service >/dev/null 2>&1 || true
    fi
  fi
}

if [[ -n "${MINO_SCOUT_SKIP_SERVICE:-}" ]]; then
  echo "MINO_SCOUT_SKIP_SERVICE — skipping all service stop/start operations"
elif [[ -z "${MINO_SCOUT_UPDATING:-}" ]]; then
  stop_running
else
  echo "MINO_SCOUT_UPDATING=1 — 跳过 stop（进程内热更，装完由 Scout reexec）"
fi

if [[ -f "$ROOT/layers.txt" ]]; then
  install_layers
elif [[ -d "$ROOT/mino_scout" && -f "$ROOT/pyproject.toml" ]]; then
  install_source
else
  echo "installer payload not found (need layers.txt with runtime/ app/ browser/, or mino_scout/)" >&2
  exit 1
fi

write_launchd() {
  local plist="$1"
  mkdir -p "$(dirname "$plist")"
  cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.mino.scout</string>
  <key>ProgramArguments</key>
  <array>
    <string>${BIN}</string>
    <string>run</string>
  </array>
  <key>WorkingDirectory</key>
  <string>${PREFIX}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/opt/homebrew/sbin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    <key>HOME</key>
    <string>${HOME}</string>
    <key>MINO_SCOUT_HOME</key>
    <string>${PREFIX}</string>
    <key>PLAYWRIGHT_BROWSERS_PATH</key>
    <string>${PREFIX}/bin/ms-playwright</string>
    <key>SSL_CERT_FILE</key>
    <string>${PREFIX}/bin/_internal/certifi/cacert.pem</string>
    <key>REQUESTS_CA_BUNDLE</key>
    <string>${PREFIX}/bin/_internal/certifi/cacert.pem</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <dict>
    <key>Crashed</key>
    <true/>
  </dict>
  <key>StandardOutPath</key>
  <string>${LOG_DIR}/scout.log</string>
  <key>StandardErrorPath</key>
  <string>${LOG_DIR}/scout.err.log</string>
</dict>
</plist>
EOF
}

# CI 与本地自测只想验证"层有没有正确落地"，不该在跑测试的机器上留一个常驻服务。
# 进程内热更也不重写 launchd / kickstart —— 由 schedule_reexec 拉起新二进制。
if [[ -n "${MINO_SCOUT_SKIP_SERVICE:-}" ]]; then
  echo "MINO_SCOUT_SKIP_SERVICE — not registering, restarting or linking a daemon"
elif [[ -n "${MINO_SCOUT_UPDATING:-}" ]]; then
  install_cli_link
  echo "In-place update finished (service control delegated to caller)."
elif [[ "$(uname -s)" == "Darwin" ]]; then
  if [[ "$(id -u)" -eq 0 ]]; then
    PLIST="/Library/LaunchDaemons/com.mino.scout.plist"
    write_launchd "$PLIST"
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    launchctl load "$PLIST"
    echo "Registered LaunchDaemon: $PLIST"
  else
    PLIST="$HOME/Library/LaunchAgents/com.mino.scout.plist"
    write_launchd "$PLIST"
    uid="$(id -u)"
    target="gui/${uid}/com.mino.scout"
    launchctl bootout "$target" >/dev/null 2>&1 || true
    launchctl unload "$PLIST" >/dev/null 2>&1 || true
    launchctl bootstrap "gui/${uid}" "$PLIST" >/dev/null 2>&1 \
      || launchctl load "$PLIST"
    launchctl kickstart "$target" >/dev/null 2>&1 \
      || launchctl load "$PLIST"
    echo "Registered LaunchAgent: $PLIST"
  fi
  install_cli_link
elif command -v systemctl >/dev/null 2>&1; then
  if [[ "$(id -u)" -eq 0 ]]; then
    UNIT="/etc/systemd/system/mino-scout.service"
  else
    UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
    mkdir -p "$UNIT_DIR"
    UNIT="$UNIT_DIR/mino-scout.service"
  fi
  cat > "$UNIT" <<EOF
[Unit]
Description=Mino Scout
After=network-online.target

[Service]
Type=simple
ExecStart=${BIN} run
WorkingDirectory=${PREFIX}
Environment="MINO_SCOUT_HOME=${PREFIX}"
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
  if [[ "$(id -u)" -eq 0 ]]; then
    systemctl daemon-reload
    systemctl enable --now mino-scout.service
    echo "Registered systemd unit: $UNIT"
  else
    systemctl --user daemon-reload
    systemctl --user enable --now mino-scout.service
    echo "Registered systemd user unit: $UNIT"
  fi
  install_cli_link
else
  echo "No launchd/systemd. Start manually: $BIN"
  install_cli_link
fi

if [[ -f "$DEST/layers.txt" ]]; then
  echo "Installed layers:"
  sed 's/^/  /' "$DEST/layers.txt"
fi
echo "Config file (existing identity and credentials preserved): $PREFIX/config.json"
echo "Binary: $BIN"
echo "Control: $BIN status | $BIN stop"
