#!/usr/bin/env bash
# Instalador do Manômetros.
# Rode SEM sudo:   ./instalar.sh
# (serve tanto para instalar quanto para atualizar uma versão antiga)
# Ele pergunta antes de qualquer passo que precise de senha de administrador.
set -euo pipefail

AQUI="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESTINO="$HOME/.local/share/manometros"
BIN_DIR="$HOME/.local/bin"
LANCADOR="$BIN_DIR/manometros"
CONF_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/manometros"
AUTOSTART_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/autostart"

negrito() { printf '\n\033[1m%s\033[0m\n' "$*"; }
pergunta() {  # pergunta "texto" padrão(s/n)  -> retorna 0 para sim
  local resp=""
  read -r -p "$1 " resp || true
  resp="${resp:-$2}"
  [[ "$resp" =~ ^[sSyY] ]]
}

if [ "$(id -u)" -eq 0 ]; then
  echo "Não rode o instalador com sudo. Rode só ./instalar.sh — ele pede a senha quando precisar."
  exit 1
fi

# ------------------------------------------------------------------ 1. dependências
negrito "1/5 · Verificando o que já está instalado"
declare -A PARA_QUE=(
  [python3-gi]="liga o Python às bibliotecas gráficas do sistema"
  [python3-gi-cairo]="permite desenhar com Cairo a partir do Python"
  [gir1.2-gtk-3.0]="a biblioteca de janelas GTK 3"
  [gir1.2-gtklayershell-0.1]="fixa o painel no segundo monitor no Wayland (layer-shell)"
  [fonts-inter]="a fonte Inter, usada no visual"
)
FALTAM=()
for pacote in python3-gi python3-gi-cairo gir1.2-gtk-3.0 gir1.2-gtklayershell-0.1 fonts-inter; do
  if dpkg-query -W -f='${Status}' "$pacote" 2>/dev/null | grep -q "install ok installed"; then
    echo "  ok      $pacote"
  else
    echo "  falta   $pacote — ${PARA_QUE[$pacote]}"
    FALTAM+=("$pacote")
  fi
done

if [ "${#FALTAM[@]}" -gt 0 ]; then
  echo
  echo "Para instalar o que falta, preciso rodar este comando (ele pede a sua senha):"
  echo
  echo "    sudo apt install ${FALTAM[*]}"
  echo
  echo "São pacotes oficiais do repositório do Pop!_OS/Ubuntu. Nada mais é alterado no sistema."
  if pergunta "Posso rodar esse comando agora? [s/N]" n; then
    sudo apt install -y "${FALTAM[@]}"
  else
    echo "Sem problema. Rode o comando acima quando quiser e depois execute ./instalar.sh de novo."
    exit 1
  fi
fi

TESTE_GI="import gi; gi.require_version('Gtk','3.0'); gi.require_version('GtkLayerShell','0.1'); from gi.repository import Gtk, GtkLayerShell; import cairo"
PY=""
for candidato in /usr/bin/python3 /usr/bin/python3.12 /usr/bin/python3.13 /usr/bin/python3.11; do
  if [ -x "$candidato" ] && "$candidato" -c "$TESTE_GI" 2>/dev/null; then PY="$candidato"; break; fi
done
if [ -z "$PY" ]; then
  echo "As bibliotecas estão instaladas, mas nenhum Python do sistema consegue usá-las."
  echo "Rode:  /usr/bin/python3 -c \"$TESTE_GI\"  e me mande o erro."
  exit 1
fi
echo "  Python usado: $PY"

# ------------------------------------------------------------------ 2. arquivos
negrito "2/5 · Copiando o programa para $DESTINO"
"$LANCADOR" --parar >/dev/null 2>&1 || true
mkdir -p "$DESTINO" "$BIN_DIR" "$CONF_DIR"
rm -rf "$DESTINO/manometros"
cp -r "$AQUI/manometros" "$DESTINO/"
cp "$AQUI/config.exemplo.toml" "$DESTINO/" 2>/dev/null || true
cp "$AQUI/README.md" "$DESTINO/" 2>/dev/null || true
find "$DESTINO" -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
chmod +x "$DESTINO/manometros/statusline.py"

cat > "$LANCADOR" <<EOF
#!/usr/bin/env bash
# Lançador do Manômetros (criado pelo instalar.sh)
if [ -n "\${WAYLAND_DISPLAY:-}" ]; then export GDK_BACKEND=wayland; fi
export PYTHONPATH="$DESTINO\${PYTHONPATH:+:\$PYTHONPATH}"
exec $PY -m manometros "\$@"
EOF
chmod +x "$LANCADOR"
echo "  comando criado: $LANCADOR"

if [ ! -f "$CONF_DIR/config.toml" ]; then
  cp "$AQUI/config.exemplo.toml" "$CONF_DIR/config.toml"
  echo "  configuração criada: $CONF_DIR/config.toml"
else
  echo "  configuração existente mantida: $CONF_DIR/config.toml"
fi

# ------------------------------------------------------------------ 3. modo
negrito "3/5 · Como o painel deve aparecer"
echo "  1) Janelinha no canto de um monitor (padrão)"
echo "  2) Faixa na parte de baixo de um monitor"
echo "  3) Monitor inteiro"
read -r -p "Escolha [1]: " MODO || true
case "${MODO:-1}" in
  2) MODO=faixa ;;
  3) MODO=inteiro ;;
  *) MODO=janela ;;
esac
PYTHONPATH="$DESTINO" "$PY" -c "from manometros import config; config.salvar({'layout': '$MODO'})"
echo "  modo: $MODO. Depois dá para trocar o modo, o monitor, o canto e o tamanho clicando no painel."

# ------------------------------------------------------------------ 4. início automático
negrito "4/5 · Início automático"
echo "Cria um atalho em $AUTOSTART_DIR para o painel abrir quando você entra na sessão."
if pergunta "Ativar? [S/n]" s; then
  mkdir -p "$AUTOSTART_DIR"
  cat > "$AUTOSTART_DIR/manometros.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Manômetros
Comment=Painel de temperatura, uso e clock de CPU e GPU, e uso de RAM
Exec=$LANCADOR
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
  echo "  ativado."
else
  echo "  pulado."
fi

# ------------------------------------------------------------------ 5. teste
negrito "5/5 · Diagnóstico"
"$LANCADOR" --listar || true

echo
if pergunta "Abrir o painel agora? [S/n]" s; then
  "$LANCADOR" --reiniciar || true
fi

negrito "Pronto!"
cat <<EOF
Clique no painel para escolher monitor, canto, tamanho, modo e para atualizar.

Comandos úteis:
  manometros --listar      mostra monitores e sensores
  manometros --atualizar   procura e instala uma versão nova
  manometros --reiniciar   reabre o painel (use depois de editar a configuração)
  manometros --parar       fecha o painel
  manometros --janela --demo   abre uma janela comum com dados de exemplo

Configuração: $CONF_DIR/config.toml
Registro de erros: ~/.cache/manometros/manometros.log
EOF
case ":$PATH:" in
  *":$BIN_DIR:"*) ;;
  *) echo
     echo "Obs.: $BIN_DIR ainda não está no seu PATH nesta sessão."
     echo "Até você sair e entrar de novo, use o caminho completo: $LANCADOR" ;;
esac
