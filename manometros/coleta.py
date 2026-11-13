"""Coleta de dados: temperatura da CPU e da GPU, RAM, ventoinhas e uso do Claude.

Tudo aqui é leitura simples de arquivos ou chamadas baratas:
- CPU e ventoinhas: arquivos do kernel em /sys/class/hwmon (sem abrir programas)
- GPU: biblioteca NVML que vem com o driver NVIDIA (via ctypes, sem pip)
- RAM: /proc/meminfo
- Ventoinhas (preferencial): API local do CoolerControl
- Claude: registros JSONL do Claude Code + arquivo gravado pela statusline
"""
import ctypes
import datetime as dt
import glob
import json
import math
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from collections import deque

from . import config as C

HWMON = "/sys/class/hwmon"


def _ler(caminho):
    try:
        with open(caminho, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _ler_int(caminho):
    s = _ler(caminho)
    try:
        return int(s)
    except (TypeError, ValueError):
        return None


def _natural(s):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", s)]


def hwmon_dispositivos():
    saida = []
    for d in sorted(glob.glob(os.path.join(HWMON, "hwmon*")), key=_natural):
        saida.append((_ler(os.path.join(d, "name")) or os.path.basename(d), d))
    return saida


# ---------------------------------------------------------------- CPU
class SensorCPU:
    NOMES = ("coretemp", "k10temp", "zenpower")

    def __init__(self):
        self.arquivos = []
        self.descricao = "não encontrado"
        cands = []
        for nome, d in hwmon_dispositivos():
            if nome not in self.NOMES:
                continue
            for inp in sorted(glob.glob(os.path.join(d, "temp*_input")), key=_natural):
                rot = _ler(inp.replace("_input", "_label")) or ""
                if rot.startswith("Package id"):
                    prio = 3
                elif rot in ("Tctl", "Tdie"):
                    prio = 2
                elif rot.startswith("Core"):
                    prio = 0
                else:
                    prio = 1
                cands.append((prio, inp, f"{nome} · {rot or os.path.basename(inp)}"))
        if not cands:
            for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*"), key=_natural):
                if _ler(os.path.join(z, "type")) == "x86_pkg_temp":
                    cands.append((1, os.path.join(z, "temp"), "thermal · x86_pkg_temp"))
        if cands:
            topo = max(c[0] for c in cands)
            escolhidos = [c for c in cands if c[0] == topo]
            self.arquivos = [c[1] for c in escolhidos]
            self.descricao = escolhidos[0][2] + (f" (+{len(escolhidos) - 1})" if len(escolhidos) > 1 else "")

    def ler(self):
        vals = [v / 1000.0 for v in (_ler_int(a) for a in self.arquivos) if v is not None]
        return max(vals) if vals else None


# ---------------------------------------------------------------- GPU (NVIDIA)
class _Utilizacao(ctypes.Structure):
    _fields_ = [("gpu", ctypes.c_uint), ("memory", ctypes.c_uint)]


class _TemperaturaV(ctypes.Structure):
    _fields_ = [("version", ctypes.c_uint), ("sensorType", ctypes.c_int), ("temperature", ctypes.c_int)]


class SensorGPU:
    def __init__(self):
        self.lib = None
        self.h = None
        self.nome = None
        self.erro = None
        self.smi = None
        self._ult_smi = (0.0, (None, None, None))
        try:
            lib = ctypes.CDLL("libnvidia-ml.so.1")
            if lib.nvmlInit_v2() != 0:
                raise OSError("nvmlInit falhou")
            h = ctypes.c_void_p()
            if lib.nvmlDeviceGetHandleByIndex_v2(ctypes.c_uint(0), ctypes.byref(h)) != 0:
                raise OSError("nenhuma GPU NVIDIA encontrada")
            self.lib, self.h = lib, h
            buf = ctypes.create_string_buffer(96)
            if lib.nvmlDeviceGetName(h, buf, ctypes.c_uint(96)) == 0:
                self.nome = buf.value.decode(errors="replace")
        except (OSError, AttributeError) as exc:
            self.erro = str(exc)
            self.smi = shutil.which("nvidia-smi")
            if self.smi:
                self.nome = "NVIDIA (via nvidia-smi)"

    def _temp_nvml(self):
        t = ctypes.c_uint()
        try:
            if self.lib.nvmlDeviceGetTemperature(self.h, ctypes.c_int(0), ctypes.byref(t)) == 0:
                return float(t.value)
        except AttributeError:
            pass
        try:  # API nova (drivers recentes)
            tv = _TemperaturaV(ctypes.sizeof(_TemperaturaV) | (1 << 24), 0, 0)
            if self.lib.nvmlDeviceGetTemperatureV(self.h, ctypes.byref(tv)) == 0:
                return float(tv.temperature)
        except AttributeError:
            pass
        return None

    def ler(self):
        """Retorna (temperatura °C, potência W, uso %)."""
        if self.lib is not None:
            temp = self._temp_nvml()
            p = ctypes.c_uint()
            pot = p.value / 1000.0 if self.lib.nvmlDeviceGetPowerUsage(self.h, ctypes.byref(p)) == 0 else None
            u = _Utilizacao()
            uso = float(u.gpu) if self.lib.nvmlDeviceGetUtilizationRates(self.h, ctypes.byref(u)) == 0 else None
            return temp, pot, uso
        if self.smi:
            agora = time.monotonic()
            if agora - self._ult_smi[0] < 2.0:  # nvidia-smi é mais caro: no máximo a cada 2 s
                return self._ult_smi[1]
            try:
                out = subprocess.run(
                    [self.smi, "--query-gpu=temperature.gpu,power.draw,utilization.gpu",
                     "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
                partes = [x.strip() for x in out.splitlines()[0].split(",")]
                vals = tuple(float(x) if x not in ("", "[N/A]") else None for x in partes[:3])
            except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
                vals = (None, None, None)
            self._ult_smi = (agora, vals)
            return vals
        return None, None, None


# ---------------------------------------------------------------- RAM
def ler_ram():
    info = {}
    try:
        with open("/proc/meminfo") as fh:
            for linha in fh:
                k, v = linha.split(":", 1)
                info[k] = int(v.split()[0]) * 1024
    except (OSError, ValueError):
        return None
    total = info.get("MemTotal")
    livre = info.get("MemAvailable", info.get("MemFree"))
    if not total or livre is None:
        return None
    usado = total - livre
    return {"pct": usado / total * 100.0, "usado": usado, "total": total}


# ---------------------------------------------------------------- ventoinhas
def _nome_bonito(rotulo):
    m = re.fullmatch(r"fan(\d+)", rotulo or "")
    return f"Fan {m.group(1)}" if m else (rotulo or "?")


class Ventoinhas:
    def __init__(self, cfg):
        self.cfg = cfg["ventoinhas"]
        self.fonte = None
        self.aviso = None
        self._cc_nomes = {}
        self._cc_nomes_t = -1e9
        self._max = {}

    # --- CoolerControl
    def _req(self, caminho):
        url = self.cfg["coolercontrol_url"].rstrip("/") + caminho
        cab = {"Accept": "application/json"}
        if self.cfg.get("coolercontrol_token"):
            cab["Authorization"] = "Bearer " + self.cfg["coolercontrol_token"]
        with urllib.request.urlopen(urllib.request.Request(url, headers=cab), timeout=0.8) as r:
            return json.loads(r.read().decode("utf-8"))

    def _carregar_nomes_cc(self):
        self._cc_nomes_t = time.monotonic()
        try:
            dados = self._req("/devices")
        except (OSError, ValueError):
            return
        tipos = {}
        for dev in dados.get("devices", []) or []:
            uid = dev.get("uid")
            nome = dev.get("name") or dev.get("type") or "?"
            if (dev.get("type") or "").upper() == "GPU":
                tipos["GPU"] = tipos.get("GPU", 0) + 1
                nome_curto = "GPU" if tipos["GPU"] == 1 else f"GPU {tipos['GPU']}"
            else:
                nome_curto = None
            self._cc_nomes[uid] = (nome, nome_curto)
            canais = ((dev.get("info") or {}).get("channels") or {})
            if isinstance(canais, dict):
                for ch, info in canais.items():
                    if isinstance(info, dict) and info.get("label"):
                        self._cc_nomes[(uid, ch)] = info["label"]

    def _ler_cc(self):
        try:
            if time.monotonic() - self._cc_nomes_t > 300:
                self._carregar_nomes_cc()
            st = self._req("/status")
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                self.aviso = "CoolerControl pediu autenticação: crie um token só de leitura"
            else:
                self.aviso = f"CoolerControl respondeu HTTP {exc.code}"
            return None
        except (OSError, ValueError):
            self.aviso = "CoolerControl não respondeu"
            return None
        saida = []
        for dev in st.get("devices", []) or []:
            hist = dev.get("status_history") or []
            if not hist:
                continue
            uid = dev.get("uid")
            nome_dev, curto = self._cc_nomes.get(uid, (dev.get("type") or "?", None))
            for ch in hist[-1].get("channels", []) or []:
                rpm = ch.get("rpm")
                nome_ch = ch.get("name") or "?"
                if rpm is None:
                    continue
                rot = self._cc_nomes.get((uid, nome_ch))
                if rot:
                    exib = rot
                elif curto:
                    exib = curto
                else:
                    exib = _nome_bonito(nome_ch)
                saida.append((f"{nome_dev}/{nome_ch}", exib, int(rpm)))
        self.aviso = None
        return saida

    # --- hwmon (direto do kernel)
    def _ler_hwmon(self):
        saida = []
        for nome, d in hwmon_dispositivos():
            for inp in sorted(glob.glob(os.path.join(d, "fan*_input")), key=_natural):
                rpm = _ler_int(inp)
                if rpm is None:
                    continue
                ch = os.path.basename(inp)[: -len("_input")]
                rot = _ler(inp.replace("_input", "_label"))
                saida.append((f"{nome}/{ch}", rot or _nome_bonito(ch), rpm))
        return saida

    def todas(self):
        fonte = self.cfg.get("fonte", "auto")
        if fonte in ("auto", "coolercontrol"):
            r = self._ler_cc()
            if r is not None:
                self.fonte = "CoolerControl"
                return r
            if fonte == "coolercontrol":
                self.fonte = "CoolerControl"
                return []
        self.fonte = "hwmon"
        return self._ler_hwmon()

    def ler(self):
        lista = self.todas()
        nomes = self.cfg.get("nomes") or {}
        pedidas = self.cfg.get("mostrar") or []
        if pedidas:
            por_id = {i: (i, n, r) for i, n, r in lista}
            escolhidas = [por_id[i] for i in pedidas if i in por_id]
        else:
            escolhidas = [x for x in lista if x[2] > 0][: int(self.cfg.get("maximo", 4))]
        saida = []
        for ident, nome, rpm in escolhidas:
            mx = max(self._max.get(ident, 0), rpm, int(self.cfg.get("rpm_max", 2000)))
            self._max[ident] = mx
            saida.append({"id": ident, "nome": nomes.get(ident, nome), "rpm": rpm, "frac": rpm / mx if mx else 0})
        return saida


# ---------------------------------------------------------------- Claude
def _ts(s):
    if not isinstance(s, str):
        return None
    try:
        return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class UsoClaude:
    """Lê os registros do Claude Code de forma incremental e sem contar repetições.

    O Claude Code grava a mesma resposta em várias linhas (uma por bloco de conteúdo
    e às vezes repetidas). Aqui cada resposta é contada uma vez, pela chave
    message.id + requestId, guardando o maior valor visto de cada campo.
    """

    JANELA = 8 * 86400

    def __init__(self, cfg):
        self.cfg = cfg["claude"]
        self.arquivos = {}   # caminho -> [inode, posição já lida]
        self.msgs = {}       # chave -> [ts, entrada, saída, escrita_cache, leitura_cache]
        self.pastas = []

    def _pastas(self):
        cands = []
        env = os.environ.get("CLAUDE_CONFIG_DIR")
        if env:
            cands += [os.path.join(p.strip(), "projects") for p in env.split(",") if p.strip()]
        cands += [os.path.join(C.HOME, ".claude", "projects"), os.path.join(C.HOME, ".config", "claude", "projects")]
        cands += [os.path.expanduser(p) for p in self.cfg.get("pastas") or []]
        vistos, saida = set(), []
        for p in cands:
            r = os.path.realpath(p)
            if r not in vistos and os.path.isdir(r):
                vistos.add(r)
                saida.append(r)
        return saida

    def _registrar(self, obj):
        if obj.get("type") != "assistant":
            return
        msg = obj.get("message")
        if not isinstance(msg, dict):
            return
        u = msg.get("usage")
        if not isinstance(u, dict):
            return
        ts = _ts(obj.get("timestamp"))
        if ts is None:
            return
        chave = f"{msg.get('id') or obj.get('uuid') or ''}|{obj.get('requestId') or ''}"
        try:
            vals = [int(u.get(k) or 0) for k in ("input_tokens", "output_tokens",
                                                   "cache_creation_input_tokens", "cache_read_input_tokens")]
        except (TypeError, ValueError):
            return
        ant = self.msgs.get(chave)
        if ant is None:
            self.msgs[chave] = [ts] + vals
        else:
            ant[0] = min(ant[0], ts)
            for i in range(4):
                if vals[i] > ant[i + 1]:
                    ant[i + 1] = vals[i]

    def _ler_arquivo(self, caminho, inicio):
        try:
            with open(caminho, "rb") as fh:
                fh.seek(inicio)
                dados = fh.read()
        except OSError:
            return inicio
        fim = dados.rfind(b"\n")
        if fim < 0:
            return inicio  # linha ainda sendo escrita
        for linha in dados[: fim + 1].split(b"\n"):
            if b'"usage"' not in linha:
                continue
            try:
                obj = json.loads(linha)
            except ValueError:
                continue
            if isinstance(obj, dict):
                self._registrar(obj)
        return inicio + fim + 1

    def atualizar(self, agora=None):
        agora = agora or time.time()
        corte = agora - self.JANELA
        self.pastas = self._pastas()
        vistos = set()
        for pasta in self.pastas:
            for raiz, _dirs, arqs in os.walk(pasta):
                for nome in arqs:
                    if not nome.endswith(".jsonl"):
                        continue
                    p = os.path.join(raiz, nome)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    vistos.add(p)
                    reg = self.arquivos.get(p)
                    if reg is None:
                        if st.st_mtime < corte:  # arquivo antigo: só acompanha o que vier depois
                            self.arquivos[p] = [st.st_ino, st.st_size]
                            continue
                        reg = self.arquivos[p] = [st.st_ino, 0]
                    if reg[0] != st.st_ino or st.st_size < reg[1]:
                        reg[0], reg[1] = st.st_ino, 0
                    if st.st_size > reg[1]:
                        reg[1] = self._ler_arquivo(p, reg[1])
        for p in list(self.arquivos):
            if p not in vistos:
                del self.arquivos[p]
        for k in [k for k, v in self.msgs.items() if v[0] < corte]:
            del self.msgs[k]

    def totais(self, agora=None):
        agora = agora or time.time()
        hoje0 = dt.datetime.combine(dt.date.fromtimestamp(agora), dt.time())
        ini_dia = hoje0.timestamp()
        if self.cfg.get("semana") == "7dias":
            ini_sem = agora - 7 * 86400
        else:
            ini_sem = (hoje0 - dt.timedelta(days=hoje0.weekday())).timestamp()
        dia, sem = [0, 0, 0, 0], [0, 0, 0, 0]
        for ts, *v in self.msgs.values():
            if ts >= ini_sem:
                for i in range(4):
                    sem[i] += v[i]
            if ts >= ini_dia:
                for i in range(4):
                    dia[i] += v[i]
        cache_leitura = bool(self.cfg.get("incluir_leitura_cache", True))

        def total(v):
            return v[0] + v[1] + v[2] + (v[3] if cache_leitura else 0)

        return {"ok": bool(self.pastas), "hoje": total(dia), "hoje_saida": dia[1],
                "semana": total(sem), "semana_saida": sem[1], "respostas": len(self.msgs)}


def ler_limites(agora=None):
    agora = agora or time.time()
    try:
        st = os.stat(C.LIMITES_FILE)
        with open(C.LIMITES_FILE, "r", encoding="utf-8") as fh:
            dados = json.load(fh)
    except (OSError, ValueError):
        return None
    rl = dados.get("rate_limits") or {}
    saida = {"recebido_em": float(dados.get("recebido_em") or st.st_mtime), "janelas": {}}
    for k in ("seven_day", "five_hour"):
        w = rl.get(k)
        if isinstance(w, dict) and w.get("used_percentage") is not None:
            try:
                zera = float(w.get("resets_at")) if w.get("resets_at") is not None else None
                saida["janelas"][k] = {"pct": float(w["used_percentage"]), "zera": zera,
                                       "expirou": zera is not None and zera <= agora}
            except (TypeError, ValueError):
                continue
    return saida


# ---------------------------------------------------------------- coletor em segundo plano
class Coletor(threading.Thread):
    """Lê tudo em intervalos e entrega uma 'foto' dos dados para a interface."""

    def __init__(self, cfg, entregar, demo=False):
        super().__init__(daemon=True, name="coletor")
        self.cfg = cfg
        self.entregar = entregar
        self.demo = demo
        self.parar = threading.Event()
        self.picos = deque()
        if not demo:
            self.cpu = SensorCPU()
            self.gpu = SensorGPU()
            self.fans = Ventoinhas(cfg)
            self.claude = UsoClaude(cfg)
        self._fans = {"lista": [], "fonte": None, "aviso": None}
        self._claude = {"ok": False}
        self._lim = None
        self._t_fans = self._t_claude = self._t_lim = -1e9

    def foto(self):
        agora = time.time()
        mono = time.monotonic()
        if self.demo:
            return foto_demo(agora)
        temp = self.cpu.ler()
        if temp is not None:
            self.picos.append((mono, temp))
        while self.picos and self.picos[0][0] < mono - 300:
            self.picos.popleft()
        pico = max((t for _, t in self.picos), default=None)
        g_temp, g_pot, g_uso = self.gpu.ler()
        if mono - self._t_fans >= 2:
            self._t_fans = mono
            self._fans = {"lista": self.fans.ler(), "fonte": self.fans.fonte, "aviso": self.fans.aviso}
        if mono - self._t_claude >= float(self.cfg["claude"].get("intervalo_s", 30)):
            self._t_claude = mono
            try:
                self.claude.atualizar(agora)
            except Exception as exc:  # nunca derrubar o painel por causa de um registro estranho
                print(f"Aviso: erro lendo registros do Claude: {exc}")
        if mono - self._t_lim >= 10:
            self._t_lim = mono
            self._lim = ler_limites(agora)
        claude = self.claude.totais(agora)
        claude["limites"] = self._lim
        return {
            "t": agora,
            "cpu": {"temp": temp, "pico": pico},
            "gpu": {"temp": g_temp, "potencia": g_pot, "uso": g_uso, "existe": self.gpu.nome is not None},
            "ram": ler_ram(),
            "ventoinhas": self._fans,
            "claude": claude,
        }

    def run(self):
        while not self.parar.is_set():
            inicio = time.monotonic()
            try:
                self.entregar(self.foto())
            except Exception as exc:
                print(f"Aviso: erro na coleta: {exc}")
            self.parar.wait(max(0.2, 1.0 - (time.monotonic() - inicio)))


def foto_demo(agora):
    """Dados inventados que variam com o tempo, para testar o visual em qualquer máquina."""
    s = agora / 7.0
    cpu = 58 + 22 * math.sin(s) + 6 * math.sin(s * 3.1)
    gpu = 50 + 30 * math.sin(s * 0.6 + 1)
    ram = 41 + 8 * math.sin(s * 0.3)
    fans = [("CPU", 1240 + 500 * math.sin(s)), ("Frente", 860 + 200 * math.sin(s + 1)),
            ("Topo", 790 + 150 * math.sin(s + 2)), ("GPU", 1150 + 900 * math.sin(s * 0.6 + 1))]
    return {
        "t": agora,
        "cpu": {"temp": cpu, "pico": cpu + 3},
        "gpu": {"temp": gpu, "potencia": 140 + 120 * math.sin(s * 0.6 + 1), "uso": 50 + 45 * math.sin(s * 0.6 + 1),
                "existe": True},
        "ram": {"pct": ram, "usado": ram / 100 * 32 * 2**30, "total": 32 * 2**30},
        "ventoinhas": {"lista": [{"id": n, "nome": n, "rpm": int(r), "frac": r / (3000 if n == "GPU" else 2000)}
                                 for n, r in fans], "fonte": "demo", "aviso": None},
        "claude": {"ok": True, "hoje": 8_412_907, "hoje_saida": 182_000, "semana": 46_380_115,
                   "semana_saida": 1_100_000, "respostas": 0,
                   "limites": {"recebido_em": agora - 180, "janelas": {
                       "seven_day": {"pct": 41, "zera": agora + 2 * 86400 + 14 * 3600, "expirou": False},
                       "five_hour": {"pct": 23, "zera": agora + 3 * 3600 + 600, "expirou": False}}}},
    }
