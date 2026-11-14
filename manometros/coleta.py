"""Coleta de dados: temperatura, uso e clock da CPU e da GPU, e uso da RAM.

Tudo aqui é leitura simples de arquivos ou chamadas baratas:
- CPU: arquivos do kernel em /sys/class/hwmon, /sys/devices/system/cpu e /proc/stat (sem abrir programas)
- GPU: biblioteca NVML que vem com o driver NVIDIA (via ctypes, sem pip)
- RAM: /proc/meminfo
"""
import ctypes
import glob
import math
import os
import re
import shutil
import subprocess
import threading
import time
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


class ClockCPU:
    """Clock atual da CPU em MHz: o do núcleo mais rápido ("maximo") ou a média ("media")."""

    def __init__(self, modo="maximo"):
        self.modo = modo
        self.arquivos = sorted(glob.glob("/sys/devices/system/cpu/cpu[0-9]*/cpufreq/scaling_cur_freq"), key=_natural)

    def ler(self):
        if self.arquivos:
            vals = [v / 1000.0 for v in (_ler_int(a) for a in self.arquivos) if v]  # kHz -> MHz
        else:  # sem cpufreq: usa o /proc/cpuinfo
            vals = []
            try:
                with open("/proc/cpuinfo") as fh:
                    for linha in fh:
                        if linha.startswith("cpu MHz"):
                            vals.append(float(linha.split(":", 1)[1]))
            except (OSError, ValueError):
                pass
        if not vals:
            return None
        return sum(vals) / len(vals) if self.modo == "media" else max(vals)


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
        self._ult_smi = (0.0, (None, None, None, None))
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
        """Retorna (temperatura °C, potência W, uso %, clock MHz)."""
        if self.lib is not None:
            temp = self._temp_nvml()
            p = ctypes.c_uint()
            pot = p.value / 1000.0 if self.lib.nvmlDeviceGetPowerUsage(self.h, ctypes.byref(p)) == 0 else None
            u = _Utilizacao()
            uso = float(u.gpu) if self.lib.nvmlDeviceGetUtilizationRates(self.h, ctypes.byref(u)) == 0 else None
            c = ctypes.c_uint()  # NVML_CLOCK_GRAPHICS = 0
            clock = float(c.value) if self.lib.nvmlDeviceGetClockInfo(self.h, ctypes.c_int(0), ctypes.byref(c)) == 0 \
                else None
            return temp, pot, uso, clock
        if self.smi:
            agora = time.monotonic()
            if agora - self._ult_smi[0] < 2.0:  # nvidia-smi é mais caro: no máximo a cada 2 s
                return self._ult_smi[1]
            try:
                out = subprocess.run(
                    [self.smi, "--query-gpu=temperature.gpu,power.draw,utilization.gpu,clocks.gr",
                     "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout
                partes = [x.strip() for x in out.splitlines()[0].split(",")]
                vals = tuple(float(x) if x not in ("", "[N/A]") else None for x in partes[:4])
            except (OSError, IndexError, ValueError, subprocess.TimeoutExpired):
                vals = (None, None, None, None)
            self._ult_smi = (agora, vals)
            return vals
        return None, None, None, None


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
            self.clock_cpu = ClockCPU((cfg.get("cpu") or {}).get("clock", "maximo"))
            self.gpu = SensorGPU()

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
        g_temp, g_pot, g_uso, g_clock = self.gpu.ler()
        return {
            "t": agora,
            "cpu": {"temp": temp, "pico": pico, "uso": self.uso_cpu.ler(), "clock": self.clock_cpu.ler()},
            "gpu": {"temp": g_temp, "potencia": g_pot, "uso": g_uso, "clock": g_clock,
                    "existe": self.gpu.nome is not None},
            "ram": ler_ram(),
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
    uso_cpu = max(0.0, min(100.0, 35 + 30 * math.sin(s * 1.7) + 15 * math.sin(s * 4.3)))
    uso_gpu = 50 + 45 * math.sin(s * 0.6 + 1)
    return {
        "t": agora,
        "cpu": {"temp": cpu, "pico": cpu + 3, "uso": uso_cpu, "clock": 1200 + uso_cpu * 30},
        "gpu": {"temp": gpu, "potencia": 140 + 120 * math.sin(s * 0.6 + 1), "uso": uso_gpu,
                "clock": 210 + uso_gpu * 24, "existe": True},
        "ram": {"pct": ram, "usado": ram / 100 * 32 * 2**30, "total": 32 * 2**30},
    }
