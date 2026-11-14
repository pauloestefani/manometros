"""Configuração do Manômetros: valores padrão + arquivo TOML do usuário."""
import copy
import json
import os
import re
import sys
import tempfile

try:
    import tomllib  # Python 3.11+
except ImportError:  # pragma: no cover
    tomllib = None

HOME = os.path.expanduser("~")
CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "manometros")
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "manometros")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.toml")

PADRAO = {
    "layout": "janela",             # "janela", "faixa" ou "inteiro"
    "monitor": "auto",              # "auto", índice ("1") ou parte do nome do modelo
    "canto": "superior-direito",    # só no modo janela
    "margem": 16,                   # distância da borda, em pixels (modo janela)
    "escala": 1.0,                  # tamanho da janelinha
    "camada": "acima",              # "acima" ou "abaixo" das outras janelas (modo janela)
    "excluir_monitores": ["ULTRAWIDE"],
    "mostrar_sem_segundo_monitor": False,
    "animacao": True,
    "fps": 24,
    "cor_destaque": "#EDEDED",
    "cpu": {"alerta": 80, "critico": 90,    # °C: muda a cor do mostrador
            "clock": "maximo"},             # clock exibido: "maximo" (núcleo mais rápido) ou "media"
    "gpu": {"alerta": 78, "critico": 85},   # °C
    "ram": {"alerta": 80, "critico": 90},   # % em uso
    "atualizacao": {
        "verificar": True,           # procura versão nova no GitHub uma vez por dia
        "repositorio": "",           # vazio = o repositório oficial do programa
    },
}


def _mesclar(base, extra):
    for k, v in extra.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _mesclar(base[k], v)
        else:
            base[k] = v
    return base


def carregar(caminho=None):
    cfg = copy.deepcopy(PADRAO)
    caminho = caminho or CONFIG_FILE
    if os.path.exists(caminho):
        if tomllib is None:
            print("Aviso: Python sem tomllib; usando configuração padrão.", file=sys.stderr)
            return cfg
        try:
            with open(caminho, "rb") as fh:
                _mesclar(cfg, tomllib.load(fh))
        except Exception as exc:  # arquivo com erro de digitação, por exemplo
            print(f"Aviso: não consegui ler {caminho}: {exc}. Usando o padrão.", file=sys.stderr)
    return cfg


def _valor_toml(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return json.dumps(str(v), ensure_ascii=False)


def salvar(alteracoes, caminho=None):
    """Grava chaves do topo do config.toml sem apagar comentários nem o resto do arquivo."""
    caminho = caminho or CONFIG_FILE
    try:
        with open(caminho, encoding="utf-8") as fh:
            txt = fh.read()
    except FileNotFoundError:
        txt = "# Configuração do Manômetros (criada pelo menu do painel)\n"
    m = re.search(r"(?m)^\s*\[", txt)
    corte = m.start() if m else len(txt)
    topo, resto = txt[:corte], txt[corte:]
    for chave, valor in alteracoes.items():
        linha = f"{chave} = {_valor_toml(valor)}"
        padrao = re.compile(rf"(?m)^{re.escape(chave)}\s*=.*$")
        if padrao.search(topo):
            topo = padrao.sub(lambda _m: linha, topo, count=1)
        else:
            topo = topo.rstrip("\n") + "\n" + linha + "\n"
    if resto and not topo.endswith("\n\n"):
        topo = topo.rstrip("\n") + "\n\n"
    os.makedirs(os.path.dirname(caminho), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(caminho), prefix=".config-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(topo + resto)
    if os.path.exists(caminho):
        os.chmod(tmp, os.stat(caminho).st_mode & 0o777)
    os.replace(tmp, caminho)
