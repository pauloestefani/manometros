#!/usr/bin/env python3
"""Statusline do Claude Code para o Manômetros.

O Claude Code chama este script a cada atualização e manda um JSON pela entrada
padrão. Quando esse JSON traz "rate_limits" (planos Pro/Max), guardamos uma cópia
em ~/.cache/manometros/limites.json para o painel ler.

Se você já tinha uma statusline, o instalador guardou o comando dela e este
script a executa em seguida, com os mesmos dados: o que aparece no Claude Code
continua igual ao de antes.
"""
import json
import os
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
CACHE = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.join(HOME, ".cache")), "manometros")
CONFIG = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.join(HOME, ".config")), "manometros")
ARQ_LIMITES = os.path.join(CACHE, "limites.json")
ARQ_ANTERIOR = os.path.join(CONFIG, "statusline_anterior.txt")


def salvar_limites(dados):
    rl = dados.get("rate_limits")
    if not isinstance(rl, dict) or not rl:
        return
    os.makedirs(CACHE, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=CACHE, prefix=".limites-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump({"recebido_em": time.time(), "rate_limits": rl}, fh)
    os.replace(tmp, ARQ_LIMITES)  # troca atômica: o painel nunca lê um arquivo pela metade


def linha_padrao(dados):
    partes = []
    modelo = (dados.get("model") or {}).get("display_name")
    if modelo:
        partes.append(modelo)
    ctx = (dados.get("context_window") or {}).get("used_percentage")
    if ctx is not None:
        partes.append(f"contexto {float(ctx):.0f}%")
    rl = dados.get("rate_limits") or {}
    for chave, rotulo in (("five_hour", "5h"), ("seven_day", "semana")):
        pct = (rl.get(chave) or {}).get("used_percentage")
        if pct is not None:
            partes.append(f"{rotulo} {float(pct):.0f}%")
    return " · ".join(partes)


def main():
    bruto = sys.stdin.read()
    try:
        dados = json.loads(bruto) if bruto.strip() else {}
    except ValueError:
        dados = {}
    try:
        salvar_limites(dados)
    except OSError:
        pass
    anterior = ""
    try:
        with open(ARQ_ANTERIOR, encoding="utf-8") as fh:
            anterior = fh.read().strip()
    except OSError:
        pass
    if anterior:
        try:
            r = subprocess.run(["bash", "-c", anterior], input=bruto, capture_output=True, text=True, timeout=10)
            sys.stdout.write(r.stdout)
            return r.returncode
        except (OSError, subprocess.TimeoutExpired):
            pass
    print(linha_padrao(dados))
    return 0


if __name__ == "__main__":
    sys.exit(main())
