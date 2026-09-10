#!/usr/bin/env bash
# omocrisismonitor installer for Omarchy / Arch. Re-runnable, no sudo.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin"
CONFDIR="${XDG_CONFIG_HOME:-$HOME/.config}/omocrisismonitor"
VENV="$HOME/.local/share/omocrisismonitor/venv"
HOOKDIR="$HOME/.config/omarchy/hooks/theme-set.d"
mkdir -p "$BIN" "$CONFDIR"

if command -v uv >/dev/null 2>&1; then
  uv venv "$VENV" >/dev/null
  uv pip install --python "$VENV/bin/python" -e "$REPO" >/dev/null
else
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
  "$VENV/bin/pip" install -q -e "$REPO"
fi

cat > "$BIN/omocrisismonitor" <<LAUNCH
#!/usr/bin/env bash
exec "$VENV/bin/omocrisismonitor" "\$@"
LAUNCH
chmod +x "$BIN/omocrisismonitor"
ln -sf "$REPO/scripts/omarchy-omocrisismonitor-theme" "$BIN/omarchy-omocrisismonitor-theme"

mkdir -p "$HOOKDIR"
install -m 0755 "$REPO/scripts/omocrisismonitor-theme.hook" "$HOOKDIR/omocrisismonitor-theme.hook"
"$BIN/omarchy-omocrisismonitor-theme" 2>/dev/null || echo "(theme.css will use the bundled fallback until an omarchy theme set)"

# app-drawer entry
APPS="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
mkdir -p "$APPS"
install -m 0644 "$REPO/share/omocrisismonitor.desktop" "$APPS/omocrisismonitor.desktop"

[ -f "$CONFDIR/config.toml" ] || { [ -f "$REPO/config.example.toml" ] && install -m 0600 "$REPO/config.example.toml" "$CONFDIR/config.toml"; }
chmod 600 "$CONFDIR/config.toml" 2>/dev/null || true

echo "omocrisismonitor installed."
echo "  · put your MapTiler + aisstream keys in $CONFDIR/config.toml"
echo "  · for the window to float / land on DP-2, append share/hyprland-windowrule.conf to your hypr config"
