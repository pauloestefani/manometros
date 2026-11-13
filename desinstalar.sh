#!/usr/bin/env bash
# Remove o Manômetros. Não precisa de sudo (os pacotes do sistema ficam instalados).
set -euo pipefail

DESTINO="$HOME/.local/share/manometros"
LANCADOR="$HOME/.local/bin/manometros"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/manometros"
AUTOSTART="${XDG_CONFIG_HOME:-$HOME/.config}/autostart/manometros.desktop"

"$LANCADOR" --parar >/dev/null 2>&1 || true

# devolve a statusline anterior (ou remove a nossa)
/usr/bin/python3 - "$CONF_DIR/statusline_anterior.txt" <<'PYEOF'
import json, os, shutil, sys, time
arq_anterior = sys.argv[1]
cfg_dir = os.environ.get("CLAUDE_CONFIG_DIR", "").split(",")[0].strip() or os.path.expanduser("~/.claude")
caminho = os.path.join(cfg_dir, "settings.json")
try:
    dados = json.load(open(caminho, encoding="utf-8"))
except (OSError, ValueError):
    sys.exit(0)
sl = dados.get("statusLine")
if not (isinstance(sl, dict) and "manometros" in str(sl.get("command", ""))):
    sys.exit(0)
shutil.copy2(caminho, f"{caminho}.bak-manometros-{time.strftime('%Y%m%d-%H%M%S')}")
anterior = open(arq_anterior, encoding="utf-8").read().strip() if os.path.exists(arq_anterior) else ""
if anterior:
    sl["command"] = anterior
    print("statusline anterior restaurada.")
else:
    del dados["statusLine"]
    print("statusline do Manômetros removida.")
with open(caminho, "w", encoding="utf-8") as fh:
    json.dump(dados, fh, indent=2, ensure_ascii=False)
    fh.write("\n")
PYEOF

rm -rf "$DESTINO" "$LANCADOR" "$AUTOSTART" "${XDG_CACHE_HOME:-$HOME/.cache}/manometros"
echo "Programa, lançador, início automático e cache removidos."

read -r -p "Apagar também a configuração em $CONF_DIR? [s/N] " r || true
if [[ "${r:-n}" =~ ^[sS] ]]; then
  rm -rf "$CONF_DIR"
  echo "Configuração apagada."
else
  echo "Configuração mantida."
fi
echo "Os pacotes do sistema (GTK, layer-shell, fonte Inter) continuam instalados; são usados por outros programas também."
