"""Atualização pelo próprio app: puxa a versão mais nova do GitHub e instala sem sudo.

Como funciona:
1. Pergunta à API pública do GitHub qual versão está no ramo principal do git
   (o número em manometros/__init__.py) e, se houver, qual é a última release.
2. Se a mais nova das duas for maior que a instalada, baixa o código dela.
3. Confere o pacote (versão certa e código que compila) antes de mexer em qualquer coisa.
4. Troca a pasta do programa de uma vez. Se algo falhar, a versão antiga continua lá.
Nada roda como administrador: tudo fica em ~/.local/share/manometros.
"""
import base64
import glob
import json
import os
import py_compile
import re
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request

from . import REPOSITORIO, __version__

API = "https://api.github.com"
LIMITE_DOWNLOAD = 20 * 1024 * 1024  # 20 MB: o pacote real tem bem menos


def versao_tupla(texto):
    return tuple(int(n) for n in re.findall(r"\d+", texto or "")[:3]) or (0,)


def repositorio(cfg=None):
    rep = ((cfg or {}).get("atualizacao") or {}).get("repositorio") or REPOSITORIO
    return rep.strip().strip("/")


def _abrir(url, timeout):
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": f"manometros/{__version__}",
    })
    return urllib.request.urlopen(req, timeout=timeout)


def _json(url, timeout=10):
    with _abrir(url, timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def _versao_no_git(rep, timeout):
    """Versão que está no ramo principal do repositório (o que foi enviado por último)."""
    d = _json(f"{API}/repos/{rep}/contents/manometros/__init__.py", timeout)
    texto = base64.b64decode(d.get("content") or "").decode("utf-8", "replace")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', texto)
    if not m:
        return None
    notas = ""
    try:
        c = _json(f"{API}/repos/{rep}/commits/HEAD", timeout)
        notas = ((c.get("commit") or {}).get("message") or "").strip()
        notas = "\n".join(l for l in notas.splitlines() if not l.startswith(("Co-Authored-By:", "Claude-Session:")))
    except (urllib.error.URLError, ValueError, OSError):
        pass
    return {"versao": m.group(1), "tag": "main", "notas": notas.strip(), "origem": "git",
            "tarball": f"{API}/repos/{rep}/tarball", "pagina": f"https://github.com/{rep}", "repositorio": rep}


def _ultima_release(rep, timeout):
    d = _json(f"{API}/repos/{rep}/releases/latest", timeout)
    tag = d.get("tag_name") or ""
    return {"versao": tag.lstrip("vV"), "tag": tag, "notas": (d.get("body") or "").strip(), "origem": "release",
            "tarball": d.get("tarball_url"), "pagina": d.get("html_url"), "repositorio": rep}


def verificar(cfg=None, timeout=10):
    """Devolve a versão mais nova disponível no GitHub (do git ou de uma release)."""
    rep = repositorio(cfg)
    candidatos = []
    for buscar in (_versao_no_git, _ultima_release):
        try:
            info = buscar(rep, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # sem release, ou repositório ainda vazio
                continue
            raise
        if info and info.get("tarball"):
            candidatos.append(info)
    if not candidatos:
        raise RuntimeError(f"não encontrei o programa em https://github.com/{rep} (o repositório existe e é público?)")
    # a mais nova; se empatar, prefere a release (que costuma ter notas melhores)
    return max(candidatos, key=lambda c: (versao_tupla(c["versao"]), c["origem"] == "release"))


def tem_novidade(info):
    return bool(info) and versao_tupla(info["versao"]) > versao_tupla(__version__)


def pasta_instalacao():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def pode_atualizar():
    base = pasta_instalacao()
    if os.path.isdir(os.path.join(base, ".git")):
        return False, f"o programa está rodando de um clone git ({base}); atualize com 'git pull'"
    if not os.access(base, os.W_OK):
        return False, f"sem permissão para gravar em {base}"
    return True, ""


def instalar(info, avisar=print):
    """Baixa e instala a versão de `info`. Devolve a versão instalada."""
    ok, motivo = pode_atualizar()
    if not ok:
        raise RuntimeError(motivo)
    if not info.get("tarball", "").startswith("https://api.github.com/"):
        raise RuntimeError("endereço de download inesperado")
    base = pasta_instalacao()
    destino = os.path.join(base, "manometros")
    with tempfile.TemporaryDirectory(prefix="manometros-atualizacao-") as tmp:
        arquivo = os.path.join(tmp, "pacote.tar.gz")
        avisar(f"Baixando a versão {info['versao']}…")
        with _abrir(info["tarball"], 60) as r, open(arquivo, "wb") as fh:
            total = 0
            while True:
                bloco = r.read(65536)
                if not bloco:
                    break
                total += len(bloco)
                if total > LIMITE_DOWNLOAD:
                    raise RuntimeError("pacote grande demais; atualização cancelada")
                fh.write(bloco)
        avisar("Conferindo o pacote…")
        extraido = os.path.join(tmp, "x")
        with tarfile.open(arquivo, "r:gz") as tf:
            tf.extractall(extraido, filter="data")  # bloqueia caminhos perigosos dentro do pacote
        achados = glob.glob(os.path.join(extraido, "*", "manometros", "__init__.py"))
        if len(achados) != 1:
            raise RuntimeError("o pacote baixado não tem o formato esperado")
        novo_pkg = os.path.dirname(achados[0])
        with open(achados[0], encoding="utf-8") as fh:
            m = re.search(r'__version__\s*=\s*"([^"]+)"', fh.read())
        if not m or versao_tupla(m.group(1)) != versao_tupla(info["versao"]):
            raise RuntimeError("a versão dentro do pacote não confere com a anunciada")
        for py in glob.glob(os.path.join(novo_pkg, "*.py")):
            py_compile.compile(py, cfile=os.path.join(tmp, "teste.pyc"), doraise=True)
        avisar("Instalando…")
        temp_novo = destino + ".novo"
        temp_antigo = destino + ".antigo"
        for p in (temp_novo, temp_antigo):
            shutil.rmtree(p, ignore_errors=True)
        shutil.copytree(novo_pkg, temp_novo, ignore=shutil.ignore_patterns("__pycache__"))
        os.chmod(os.path.join(temp_novo, "statusline.py"), 0o755)
        os.rename(destino, temp_antigo)
        try:
            os.rename(temp_novo, destino)
        except OSError:
            os.rename(temp_antigo, destino)  # devolve a versão antiga
            raise
        shutil.rmtree(temp_antigo, ignore_errors=True)
        raiz = os.path.dirname(novo_pkg)  # guarda o exemplo de configuração e o guia da versão nova
        for extra in ("config.exemplo.toml", "README.md"):
            if os.path.exists(os.path.join(raiz, extra)):
                shutil.copy2(os.path.join(raiz, extra), os.path.join(base, extra))
    return m.group(1)
