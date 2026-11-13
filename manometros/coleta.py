"""Coleta de dados: temperatura e uso da CPU e da GPU, RAM e ventoinhas.

Tudo aqui é leitura simples de arquivos ou chamadas baratas:
- CPU e ventoinhas: arquivos do kernel em /sys/class/hwmon e /proc/stat (sem abrir programas)
- GPU: biblioteca NVML que vem com o driver NVIDIA (via ctypes, sem pip)
- RAM: /proc/meminfo
- Ventoinhas (preferencial): API local do CoolerControl
"""
import ctypes
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


class UsoCPU:
    """Uso total da CPU em %, pela diferença entre duas leituras de /proc/stat."""

    def __init__(self):
        self.anterior = self._ler()

    @staticmethod
    def _ler():
        try:
            with open("/proc/stat") as fh:
                campos = [int(x) for x in fh.readline().split()[1:9]]
        except (OSError, ValueError):
            return None
        ocioso = campos[3] + campos[4]  # idle + iowait
        return sum(campos), ocioso

    def ler(self):
        atual, anterior = self._ler(), self.anterior
        self.anterior = atual
        if not atual or not anterior:
            return None
        dt, docioso = atual[0] - anterior[0], atual[1] - anterior[1]
        if dt <= 0:
            return None
        return max(0.0, min(100.0, (dt - docioso) / dt * 100.0))


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
            self.uso_cpu = UsoCPU()
            self.gpu = SensorGPU()
            self.fans = Ventoinhas(cfg)
        self._fans = {"lista": [], "fonte": None, "aviso": None}
        self._t_fans = -1e9

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
        return {
            "t": agora,
            "cpu": {"temp": temp, "pico": pico, "uso": self.uso_cpu.ler()},
            "gpu": {"temp": g_temp, "potencia": g_pot, "uso": g_uso, "existe": self.gpu.nome is not None},
            "ram": ler_ram(),
            "ventoinhas": self._fans,
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
        "cpu": {"temp": cpu, "pico": cpu + 3, "uso": max(0.0, min(100.0, 35 + 30 * math.sin(s * 1.7) + 15 * math.sin(s * 4.3)))},
        "gpu": {"temp": gpu, "potencia": 140 + 120 * math.sin(s * 0.6 + 1), "uso": 50 + 45 * math.sin(s * 0.6 + 1),
                "existe": True},
        "ram": {"pct": ram, "usado": ram / 100 * 32 * 2**30, "total": 32 * 2**30},
        "ventoinhas": {"lista": [{"id": n, "nome": n, "rpm": int(r), "frac": r / (3000 if n == "GPU" else 2000)}
                                 for n, r in fans], "fonte": "demo", "aviso": None},
    }
