"""Desenho dos mostradores com Cairo + Pango, no visual minimalista do esboço.

Tudo é desenhado num "espaço de projeto" fixo (1920x1080 para o monitor inteiro,
1920x280 para a faixa) e depois escalado para o tamanho real da tela.

Para gastar pouco processamento, o desenho é dividido em duas camadas:
- estática: trilhos, marcas, títulos, rótulos (desenhada uma vez e guardada);
- dinâmica: arcos de valor, números e barras (redesenhada só onde mudou).
"""
import math
import time

import cairo
import gi

gi.require_version("Pango", "1.0")
gi.require_version("PangoCairo", "1.0")
from gi.repository import Pango, PangoCairo  # noqa: E402

FONTE = "Inter"

BG = (0x0B, 0x0B, 0x0C)
INK = (0xED, 0xED, 0xED)
MUTED = (0x8A, 0x8A, 0x8E)
FAINT = (0x3E, 0x3F, 0x44)
TRACK = (0x1C, 0x1D, 0x20)
LINE = (0x1F, 0x20, 0x23)
BORDA = (0x2C, 0x2D, 0x31)
AMBER = (0xE8, 0xA3, 0x3D)
RED = (0xE5, 0x48, 0x4D)

INICIO, VARREDURA = 135.0, 270.0


def hex_rgb(s, padrao=INK):
    try:
        s = s.strip().lstrip("#")
        return (int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16))
    except (AttributeError, ValueError, IndexError):
        return padrao


def cor(cr, rgb, alfa=1.0):
    cr.set_source_rgba(rgb[0] / 255, rgb[1] / 255, rgb[2] / 255, alfa)


# ---------------------------------------------------------------- texto
_fo = cairo.FontOptions()
_fo.set_hint_metrics(cairo.HINT_METRICS_OFF)
_fo.set_hint_style(cairo.HINT_STYLE_NONE)
_fo.set_antialias(cairo.ANTIALIAS_GRAY)


def texto(cr, s, x, y, tam, peso=400, rgb=INK, alinhar="esq", base=True, espaco=0.0, tnum=False, alfa=1.0):
    """Escreve s com a linha de base (ou o topo) em y. Devolve a largura."""
    lay = PangoCairo.create_layout(cr)
    PangoCairo.context_set_font_options(lay.get_context(), _fo)
    fd = Pango.FontDescription.from_string(FONTE)
    fd.set_absolute_size(tam * Pango.SCALE)
    fd.set_weight(Pango.Weight(peso))
    lay.set_font_description(fd)
    attrs = Pango.AttrList()
    if espaco:
        attrs.insert(Pango.attr_letter_spacing_new(int(espaco * Pango.SCALE)))
    if tnum:
        attrs.insert(Pango.attr_font_features_new("tnum 1"))
    lay.set_attributes(attrs)
    lay.set_text(s, -1)
    _ink, log = lay.get_extents()
    larg = log.width / Pango.SCALE - (espaco if espaco else 0)
    if alinhar == "centro":
        x -= larg / 2
    elif alinhar == "dir":
        x -= larg
    if base:
        y -= lay.get_baseline() / Pango.SCALE
    cr.move_to(x, y)
    cor(cr, rgb, alfa)
    PangoCairo.show_layout(cr, lay)
    return larg


def largura(cr, s, tam, peso=400, tnum=False):
    lay = PangoCairo.create_layout(cr)
    PangoCairo.context_set_font_options(lay.get_context(), _fo)
    fd = Pango.FontDescription.from_string(FONTE)
    fd.set_absolute_size(tam * Pango.SCALE)
    fd.set_weight(Pango.Weight(peso))
    lay.set_font_description(fd)
    if tnum:
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_font_features_new("tnum 1"))
        lay.set_attributes(attrs)
    lay.set_text(s, -1)
    return lay.get_extents()[1].width / Pango.SCALE


# ---------------------------------------------------------------- formatação
def fmt_milhar(n):
    return f"{int(round(n)):,}".replace(",", ".")


def fmt_compacto(n):
    """8412907 -> ('8,41', 'mi')"""
    if n is None:
        return "—", ""
    n = float(n)
    for div, suf in ((1e9, "bi"), (1e6, "mi"), (1e3, "mil")):
        if n >= div:
            v = n / div
            casas = 2 if v < 10 else 1 if v < 100 else 0
            return f"{v:.{casas}f}".replace(".", ","), suf
    return f"{int(n)}", ""


def fmt_curto(n):
    v, suf = fmt_compacto(n)
    return f"{v} {suf}".strip()


def fmt_duracao(seg):
    seg = max(0, int(seg))
    d, r = divmod(seg, 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d} d {h} h"
    if h:
        return f"{h} h {m} min"
    return f"{m} min"


def fmt_idade(seg):
    if seg < 60:
        return "atualizado agora"
    if seg < 3600:
        return f"atualizado há {int(seg // 60)} min"
    if seg < 86400:
        return f"atualizado há {int(seg // 3600)} h"
    return f"atualizado há {int(seg // 86400)} d"


# ---------------------------------------------------------------- geometria do mostrador
def _ang(v, lo, hi):
    v = min(max(v, lo), hi)
    return INICIO + VARREDURA * (v - lo) / (hi - lo)


def _pt(r, a):
    t = math.radians(a)
    return 250 + r * math.cos(t), 250 + r * math.sin(t)


def _arco(cr, r, a1, a2):
    cr.new_sub_path()
    cr.arc(250, 250, r, math.radians(a1), math.radians(a2))


def _traco(cr, v, lo, hi, r1, r2):
    a = _ang(v, lo, hi)
    cr.move_to(*_pt(r1, a))
    cr.line_to(*_pt(r2, a))


class Mostrador:
    """Um manômetro de 270°. Coordenadas internas iguais às do SVG do esboço."""

    VB = (10, 24, 480, 420)

    def __init__(self, titulo, unidade, lo, hi, passo, alerta, critico):
        self.titulo, self.unidade = titulo, unidade
        self.lo, self.hi, self.passo = lo, hi, passo
        self.alerta, self.critico = alerta, critico

    def _entrar(self, cr, x, y, larg):
        cr.save()
        k = larg / self.VB[2]
        cr.translate(x, y)
        cr.scale(k, k)
        cr.translate(-self.VB[0], -self.VB[1])

    def altura(self, larg):
        return larg * self.VB[3] / self.VB[2]

    def estado(self, v):
        if v is None:
            return "normal"
        if v >= self.critico:
            return "critico"
        if v >= self.alerta:
            return "alerta"
        return "normal"

    # Tamanhos internos (em unidades do viewBox 500x500). No modo compacto, usado
    # na janelinha, traços e textos ficam proporcionalmente maiores para continuar legíveis.
    NORMAL = {"trilho": 8, "faixa": 2, "marca": 2, "botao": (13.5, 8.5), "titulo": (22, 186, 500, 4),
              "num": (120, 292), "unid": (30, 226, 8), "sub": (21, 342), "estado": (18, 392), "pontas": True}
    COMPACTO = {"trilho": 16, "faixa": 5, "marca": 4, "botao": (22, 14), "titulo": (46, 176, 600, 5),
                "num": (150, 318), "unid": (48, 248, 10), "sub": None, "estado": (40, 404), "pontas": False}

    def _dim(self, larg):
        return self.COMPACTO if larg < 220 else self.NORMAL

    def estatico(self, cr, x, y, larg):
        d = self._dim(larg)
        self._entrar(cr, x, y, larg)
        lo, hi = self.lo, self.hi
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        cr.set_line_width(d["trilho"])
        cor(cr, TRACK)
        _arco(cr, 196, INICIO, INICIO + VARREDURA)
        cr.stroke()
        cr.set_line_width(d["faixa"])
        cor(cr, AMBER, 0.75)
        _arco(cr, 216, _ang(self.alerta, lo, hi), _ang(self.critico, lo, hi))
        cr.stroke()
        cor(cr, RED, 0.75)
        _arco(cr, 216, _ang(self.critico, lo, hi), INICIO + VARREDURA)
        cr.stroke()
        cr.set_line_width(d["marca"])
        cor(cr, FAINT)
        v = lo
        while v <= hi + 1e-9:
            _traco(cr, v, lo, hi, 178 - d["trilho"] / 2, 188 - d["trilho"] / 2)
            v += self.passo
        cr.stroke()
        if d["pontas"]:
            for v in (lo, hi):
                px, py = _pt(196, _ang(v, lo, hi))
                texto(cr, f"{int(v)}", px, py + 34, 20, 400, MUTED, "centro")
        tam, yb, peso, esp = d["titulo"]
        texto(cr, self.titulo, 250, yb, tam, peso, MUTED, "centro", espaco=esp)
        cr.restore()

    def dinamico(self, cr, x, y, larg, valor, sub="", pico=None, destaque=INK, pulso=1.0):
        d = self._dim(larg)
        self._entrar(cr, x, y, larg)
        lo, hi = self.lo, self.hi
        est = self.estado(valor)
        c_arco = {"normal": destaque, "alerta": AMBER, "critico": RED}[est]
        c_num = {"normal": INK, "alerta": AMBER, "critico": RED}[est]
        alfa = pulso if est == "critico" else 1.0
        if valor is not None:
            a = _ang(valor, lo, hi)
            if a > INICIO + 0.3:
                cr.set_line_cap(cairo.LINE_CAP_BUTT)
                cr.set_line_width(d["trilho"])
                cor(cr, c_arco, alfa)
                _arco(cr, 196, INICIO, a)
                cr.stroke()
            if pico is not None and pico > valor + 0.5 and d["pontas"]:  # na janelinha fica pequeno demais
                cr.set_line_width(d["marca"])
                cor(cr, MUTED)
                _traco(cr, pico, lo, hi, 196 - d["trilho"] / 2 - 6, 196 + d["trilho"] / 2 + 6)
                cr.stroke()
            kx, ky = _pt(196, a)
            fora, dentro = d["botao"]
            cor(cr, BG)
            cr.arc(kx, ky, fora, 0, 2 * math.pi)
            cr.fill()
            cor(cr, c_arco, alfa)
            cr.arc(kx, ky, dentro, 0, 2 * math.pi)
            cr.fill()
            tam, yb = d["num"]
            w = texto(cr, f"{round(valor):d}", 250, yb, tam, 300, c_num, "centro", tnum=True)
            tu, yu, dx = d["unid"]
            texto(cr, self.unidade, 250 + w / 2 + dx, yu, tu, 400, MUTED)
        else:
            tam, yb = d["num"]
            texto(cr, "—", 250, yb - 6, tam * 0.75, 300, MUTED, "centro")
        if sub and d["sub"]:
            tam, yb = d["sub"]
            texto(cr, sub, 250, yb, tam, 400, MUTED, "centro")
        if est != "normal":
            tam, yb = d["estado"]
            texto(cr, "ALERTA" if est == "alerta" else "CRÍTICO", 250, yb, tam, 600, c_num, "centro", espaco=4)
        cr.restore()


# ---------------------------------------------------------------- painel
class Painel:
    """Monta os blocos do painel nos dois layouts: 'inteiro' e 'faixa'."""

    def __init__(self, cfg, layout=None):
        self.cfg = cfg
        self.layout = layout or cfg.get("layout", "inteiro")
        if self.layout not in ("janela", "inteiro", "faixa"):
            self.layout = "janela"
        self.destaque = hex_rgb(cfg.get("cor_destaque", "#EDEDED"))
        c, g, r = cfg["cpu"], cfg["gpu"], cfg["ram"]
        self.m = {
            "cpu": Mostrador("CPU", "°C", c["min"], c["max"], 10, c["alerta"], c["critico"]),
            "gpu": Mostrador("GPU", "°C", g["min"], g["max"], 10, g["alerta"], g["critico"]),
            "ram": Mostrador("RAM", "%", 0, 100, 10, r["alerta"], r["critico"]),
        }
        if self.layout == "janela":
            self.W, self.H = 520.0, 320.0
            gl = 150
            self.g = {k: (x, 14, gl) for k, x in zip(("cpu", "gpu", "ram"), (20, 185, 350))}
            self.reg = {k: (x - 2, y - 2, gl + 4, self.m[k].altura(gl) + 4) for k, (x, y, gl) in self.g.items()}
            self.reg["tokens"] = self.reg["limites"] = (16, 166, 233, 150)
            self.reg["fans"] = (271, 166, 233, 150)
        elif self.layout == "inteiro":
            self.W, self.H = 1920.0, 1080.0
            colw = (1920 - 160 - 160) / 3
            self.col = [80 + i * (colw + 80) for i in range(3)]
            self.colw = colw
            gl = 460
            self.g = {k: (self.col[i] + (colw - gl) / 2, 72, gl) for i, k in enumerate(("cpu", "gpu", "ram"))}
            self.reg = {k: (x - 4, y - 4, gl + 8, self.m[k].altura(gl) + 8) for k, (x, y, gl) in self.g.items()}
            for i, nome in enumerate(("tokens", "limites", "fans")):
                self.reg[nome] = (self.col[i] - 4, 600, colw + 8, 440)
        else:
            self.W, self.H = 1920.0, 280.0
            gl = 250
            gap = (1808 - 1592) / 7
            x = 56
            pos = []
            for w in (250, 250, 250, 1, 250, 260, 1, 330):
                pos.append(x)
                x += w + gap
            gy = (280 - self.m["cpu"].altura(gl)) / 2
            self.g = {"cpu": (pos[0], gy, gl), "gpu": (pos[1], gy, gl), "ram": (pos[2], gy, gl)}
            self.div = [pos[3], pos[6]]
            self.bx = {"tokens": (pos[4], 250), "limites": (pos[5], 260), "fans": (pos[7], 330)}
            self.reg = {k: (x - 4, y - 4, gl + 8, self.m[k].altura(gl) + 8) for k, (x, y, gl) in self.g.items()}
            for nome, (bx, bw) in self.bx.items():
                self.reg[nome] = (bx - 4, 20, bw + 8, 250)
        r = 12 if self.layout == "janela" else 28
        self.ponto = (self.W - r, r, 3.5 if self.layout == "janela" else 6)
        self.reg["aviso"] = (self.W - 2 * r, 0, 2 * r, 2 * r)

    # --- encaixe na tela
    def encaixe(self, larg, alt):
        s = min(larg / self.W, alt / self.H)
        return s, (larg - self.W * s) / 2, (alt - self.H * s) / 2

    def regiao_na_tela(self, nome, larg, alt):
        s, ox, oy = self.encaixe(larg, alt)
        x, y, w, h = self.reg[nome]
        return (int(math.floor(ox + x * s)) - 1, int(math.floor(oy + y * s)) - 1,
                int(math.ceil(w * s)) + 3, int(math.ceil(h * s)) + 3)

    # --- camada estática
    def estatico(self, cr, larg, alt):
        if self.layout == "janela":  # janelinha: cantos arredondados sobre fundo transparente
            cr.save()
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.paint()
            cr.restore()
        else:
            cor(cr, BG)
            cr.paint()
        s, ox, oy = self.encaixe(larg, alt)
        cr.save()
        cr.translate(ox, oy)
        cr.scale(s, s)
        if self.layout == "janela":
            _retangulo_arredondado(cr, 0.5, 0.5, self.W - 1, self.H - 1, 14)
            cor(cr, BG)
            cr.fill_preserve()
            cr.set_line_width(1)
            cor(cr, BORDA)
            cr.stroke()
        for k, (x, y, gl) in self.g.items():
            self.m[k].estatico(cr, x, y, gl)
        if self.layout == "inteiro":
            self._estatico_inteiro(cr)
        elif self.layout == "faixa":
            self._estatico_faixa(cr)
        else:
            self._estatico_janela(cr)
        cr.restore()

    def _cabecalho(self, cr, x, w, y, esq, dir_="", tam=16):
        texto(cr, esq.upper(), x, y, tam, 500, MUTED, espaco=3)
        if dir_:
            texto(cr, dir_, x + w, y, tam + 1, 400, MUTED, "dir")

    def _trilho(self, cr, x, y, w, h):
        cor(cr, TRACK)
        _retangulo_arredondado(cr, x, y, w, h, h / 2)
        cr.fill()

    def _estatico_inteiro(self, cr):
        cor(cr, LINE)
        cr.rectangle(80, 584, 1760, 1)
        cr.fill()
        x0, x1, x2, w = self.col[0], self.col[1], self.col[2], self.colw
        self._cabecalho(cr, x0, w, 646, "Tokens", "Claude Code neste PC")
        texto(cr, "Hoje", x0, 700, 20, 400, MUTED)
        texto(cr, "Semana", x0, 878, 20, 400, MUTED)
        self._cabecalho(cr, x1, w, 646, "Limites do plano", "inclui o navegador")
        texto(cr, "Semanal", x1, 734, 20, 400, MUTED)
        texto(cr, "Sessão de 5 h", x1, 876, 20, 400, MUTED)
        self._trilho(cr, x1, 754, w, 4)
        self._trilho(cr, x1, 896, w, 4)
        self._cabecalho(cr, x2, w, 646, "Ventoinhas", "rpm")

    def _estatico_janela(self, cr):
        cor(cr, LINE)
        cr.rectangle(20, 158, 480, 1)
        cr.fill()
        cx, cw, fx, fw = 20, 225, 275, 225
        texto(cr, "CLAUDE", cx, 182, 11, 500, MUTED, espaco=2)
        texto(cr, "Hoje", cx, 208, 13, 400, MUTED)
        texto(cr, "Semana", cx, 236, 13, 400, MUTED)
        texto(cr, "Semanal", cx, 264, 13, 400, MUTED)
        self._trilho(cr, cx, 269, cw, 3)
        texto(cr, "Sessão 5 h", cx, 292, 13, 400, MUTED)
        self._trilho(cr, cx, 297, cw, 3)
        texto(cr, "VENTOINHAS", fx, 182, 11, 500, MUTED, espaco=2)
        texto(cr, "rpm", fx + fw, 182, 11, 400, MUTED, "dir")

    def _estatico_faixa(self, cr):
        cor(cr, LINE)
        for dx in self.div:
            cr.rectangle(dx, 28, 1, 224)
        cr.fill()
        tx, tw = self.bx["tokens"]
        lx, lw = self.bx["limites"]
        fx, fw = self.bx["fans"]
        self._cabecalho(cr, tx, tw, 47, "Tokens", "", 14)
        texto(cr, "Hoje", tx, 78, 16, 400, MUTED)
        texto(cr, "Semana", tx, 166, 16, 400, MUTED)
        self._cabecalho(cr, lx, lw, 47, "Limites", "inclui navegador", 14)
        texto(cr, "Semanal", lx, 100, 17, 400, MUTED)
        texto(cr, "Sessão 5 h", lx, 168, 17, 400, MUTED)
        self._trilho(cr, lx, 116, lw, 4)
        self._trilho(cr, lx, 184, lw, 4)
        self._cabecalho(cr, fx, fw, 47, "Ventoinhas", "rpm", 14)

    # --- camada dinâmica
    def dinamico(self, cr, larg, alt, foto, exib, pulso=1.0, clip=None, aviso=False):
        s, ox, oy = self.encaixe(larg, alt)
        if clip is not None:  # converte o recorte da tela para o espaço de projeto
            cx1, cy1, cx2, cy2 = clip
            clip = ((cx1 - ox) / s, (cy1 - oy) / s, (cx2 - ox) / s, (cy2 - oy) / s)

        def visivel(nome):
            if clip is None:
                return True
            x, y, w, h = self.reg[nome]
            return not (x + w < clip[0] or x > clip[2] or y + h < clip[1] or y > clip[3])

        cr.save()
        cr.translate(ox, oy)
        cr.scale(s, s)
        agora = foto.get("t", time.time()) if foto else time.time()
        if foto:
            cpu, gpu, ram = foto.get("cpu") or {}, foto.get("gpu") or {}, foto.get("ram")
            subs = {
                "cpu": f"pico {round(cpu['pico'])} °C" if cpu.get("pico") is not None else "sensor não encontrado"
                if cpu.get("temp") is None else "",
                "gpu": self._sub_gpu(gpu),
                "ram": (f"{ram['usado'] / 2**30:.1f} de {ram['total'] / 2**30:.0f} GB".replace(".", ",")
                        if ram else ""),
            }
            picos = {"cpu": cpu.get("pico")}
            for k, (x, y, gl) in self.g.items():
                if visivel(k):
                    self.m[k].dinamico(cr, x, y, gl, exib.get(k), subs[k], picos.get(k), self.destaque, pulso)
            if self.layout == "janela":
                if visivel("tokens"):
                    self._claude_janela(cr, foto.get("claude") or {}, agora)
                if visivel("fans"):
                    self._fans_janela(cr, foto.get("ventoinhas") or {})
            elif self.layout == "inteiro":
                if visivel("tokens"):
                    self._tokens_inteiro(cr, foto.get("claude") or {})
                if visivel("limites"):
                    self._limites_inteiro(cr, (foto.get("claude") or {}).get("limites"), agora)
                if visivel("fans"):
                    self._fans_inteiro(cr, foto.get("ventoinhas") or {})
            else:
                if visivel("tokens"):
                    self._tokens_faixa(cr, foto.get("claude") or {})
                if visivel("limites"):
                    self._limites_faixa(cr, (foto.get("claude") or {}).get("limites"), agora)
                if visivel("fans"):
                    self._fans_faixa(cr, foto.get("ventoinhas") or {})
        if aviso and visivel("aviso"):  # há versão nova: um ponto discreto no canto
            px, py, pr = self.ponto
            cor(cr, AMBER)
            cr.arc(px, py, pr, 0, 2 * math.pi)
            cr.fill()
        cr.restore()

    @staticmethod
    def _sub_gpu(g):
        if not g.get("existe"):
            return "GPU não encontrada"
        partes = []
        if g.get("potencia") is not None:
            partes.append(f"{g['potencia']:.0f} W")
        if g.get("uso") is not None:
            partes.append(f"uso {g['uso']:.0f} %")
        return " · ".join(partes)

    # janelinha
    def _valor_dir(self, cr, xdir, yb, valor, unidade, tam=19, tam_u=11):
        wu = texto(cr, unidade, xdir, yb, tam_u, 400, MUTED, "dir") if unidade else 0
        texto(cr, valor, xdir - wu - (4 if unidade else 0), yb, tam, 300, INK, "dir", tnum=True)

    def _claude_janela(self, cr, c, agora):
        cx, cw = 20, 225
        xd = cx + cw
        for yb, k in ((208, "hoje"), (236, "semana")):
            if c.get("ok"):
                v, suf = fmt_compacto(c.get(k, 0))
                self._valor_dir(cr, xd, yb, v, suf)
            else:
                texto(cr, "—", xd, yb, 15, 300, MUTED, "dir")
        lim = c.get("limites")
        if lim is None:
            rot = "sem statusline"
        else:
            rot = "limites " + fmt_idade(agora - lim["recebido_em"]).replace("atualizado ", "")
        texto(cr, rot, xd, 182, 11, 400, MUTED, "dir")
        for (pct, _leg), (yb, ybar) in zip(self._janelas(lim, agora), ((264, 269), (292, 297))):
            if pct is None:
                texto(cr, "—", xd, yb, 15, 300, MUTED, "dir")
            else:
                self._valor_dir(cr, xd, yb - 1, f"{round(pct)}", "%", 17)
                self._barra(cr, cx, ybar, cw, 3, pct / 100)

    def _fans_janela(self, cr, f):
        fx, fw = 275, 225
        lista = (f.get("lista") or [])[:4]
        if not lista:
            texto(cr, "nenhuma encontrada", fx, 208, 13, 400, MUTED)
            return
        for i, fan in enumerate(lista):
            yb = 208 + i * 28
            nome = fan["nome"] if len(fan["nome"]) <= 16 else fan["nome"][:15] + "…"
            texto(cr, nome, fx, yb, 13, 400, INK)
            texto(cr, fmt_milhar(fan["rpm"]), fx + fw, yb, 17, 300, INK, "dir", tnum=True)
            self._trilho(cr, fx, yb + 5, fw, 2)
            self._barra(cr, fx, yb + 5, fw, 2, fan["frac"])

    # tokens
    def _numero(self, cr, x, y, valor, unidade, tam, alinhar="esq", rgb=INK):
        """Número grande leve + unidade menor ao lado. Alinha pela linha de base."""
        wu = largura(cr, unidade, round(tam * 0.33), 400) if unidade else 0
        wn = largura(cr, valor, tam, 300, tnum=True)
        if alinhar == "dir":
            x = x - wn - (10 + wu if unidade else 0)
        texto(cr, valor, x, y, tam, 300, rgb, tnum=True)
        if unidade:
            texto(cr, unidade, x + wn + 10, y, round(tam * 0.33), 400, MUTED)
        return wn + (10 + wu if unidade else 0)

    def _tokens_inteiro(self, cr, c):
        x = self.col[0]
        if not c.get("ok"):
            texto(cr, "—", x, 784, 64, 300, MUTED)
            texto(cr, "registros do Claude Code não encontrados", x, 822, 18, 400, MUTED)
            texto(cr, "—", x, 962, 64, 300, MUTED)
            texto(cr, "procurei em ~/.claude/projects", x, 1000, 18, 400, MUTED)
            return
        for y0, k in ((784, "hoje"), (962, "semana")):
            v, suf = fmt_compacto(c.get(k, 0))
            self._numero(cr, x, y0, v, f"{suf} tokens".strip(), 84)
            texto(cr, f"{fmt_milhar(c.get(k, 0))} · saída do Claude {fmt_curto(c.get(k + '_saida', 0))}",
                  x, y0 + 38, 18, 400, MUTED)

    def _tokens_faixa(self, cr, c):
        x, _w = self.bx["tokens"]
        for y0, k in ((128, "hoje"), (216, "semana")):
            if not c.get("ok"):
                texto(cr, "—", x, y0, 40, 300, MUTED)
                continue
            v, suf = fmt_compacto(c.get(k, 0))
            self._numero(cr, x, y0, v, suf, 48)

    # limites
    def _janelas(self, lim, agora):
        linhas = []
        for k in ("seven_day", "five_hour"):
            j = (lim or {}).get("janelas", {}).get(k)
            if not j:
                linhas.append((None, "sem dados ainda"))
            elif j.get("expirou"):
                linhas.append((None, "zerou · aguardando o Claude Code"))
            else:
                z = j.get("zera")
                linhas.append((j["pct"], f"zera em {fmt_duracao(z - agora)}" if z else ""))
        return linhas

    def _barra(self, cr, x, y, w, h, frac):
        frac = min(max(frac, 0.0), 1.0)
        if frac <= 0:
            return
        rgb = RED if frac >= 0.9 else AMBER if frac >= 0.75 else self.destaque
        cor(cr, rgb)
        _retangulo_arredondado(cr, x, y, max(w * frac, h), h, h / 2)
        cr.fill()

    def _limites_inteiro(self, cr, lim, agora):
        x, w = self.col[1], self.colw
        for (pct, legenda), (yb, yv, yl) in zip(self._janelas(lim, agora), ((754, 738, 790), (896, 880, 932))):
            if pct is None:
                self._numero(cr, x + w, yv, "—", "", 64, "dir")
            else:
                self._numero(cr, x + w, yv, f"{round(pct)}", "%", 64, "dir")
                self._barra(cr, x, yb, w, 4, pct / 100)
            texto(cr, legenda, x, yl, 18, 400, MUTED)
        if lim is None:
            rodape = "configure a statusline do Claude Code (veja o README)"
        else:
            rodape = fmt_idade(agora - lim["recebido_em"])
        texto(cr, rodape, x, 982, 16, 400, MUTED)

    def _limites_faixa(self, cr, lim, agora):
        x, w = self.bx["limites"]
        for (pct, _leg), (yb, yv) in zip(self._janelas(lim, agora), ((116, 102), (184, 170))):
            if pct is None:
                self._numero(cr, x + w, yv, "—", "", 34, "dir")
            else:
                self._numero(cr, x + w, yv, f"{round(pct)}", "%", 34, "dir")
                self._barra(cr, x, yb, w, 4, pct / 100)
        texto(cr, "sem statusline" if lim is None else fmt_idade(agora - lim["recebido_em"]), x, 240, 14, 400, MUTED)

    # ventoinhas
    def _fans_inteiro(self, cr, f):
        x, w = self.col[2], self.colw
        lista = f.get("lista") or []
        if not lista:
            texto(cr, "nenhuma ventoinha encontrada", x, 702, 20, 400, MUTED)
            if f.get("aviso"):
                texto(cr, f["aviso"], x, 734, 18, 400, MUTED)
            return
        passo = min(70.0, 300.0 / len(lista))
        for i, fan in enumerate(lista[:6]):
            y = 702 + i * passo
            texto(cr, fan["nome"], x, y, 20, 400, INK)
            texto(cr, fmt_milhar(fan["rpm"]), x + w, y + 2, 30, 300, INK, "dir", tnum=True)
            self._trilho(cr, x, y + 16, w, 3)
            self._barra(cr, x, y + 16, w, 3, fan["frac"])
        rodape = f"via {f['fonte']}" if f.get("fonte") and f.get("fonte") != "demo" else ""
        if f.get("aviso") and f.get("fonte") == "hwmon":
            rodape = f"via hwmon · {f['aviso']}"
        if rodape:
            texto(cr, rodape, x, 982, 16, 400, MUTED)

    def _fans_faixa(self, cr, f):
        x, w = self.bx["fans"]
        lista = (f.get("lista") or [])[:4]
        if not lista:
            texto(cr, "nenhuma ventoinha encontrada", x, 96, 16, 400, MUTED)
            return
        for i, fan in enumerate(lista):
            yc = 88 + i * 44
            texto(cr, fan["nome"], x, yc + 6, 16, 400, INK)
            bx0, bx1 = x + 84, x + w - 78
            self._trilho(cr, bx0, yc - 1.5, bx1 - bx0, 3)
            self._barra(cr, bx0, yc - 1.5, bx1 - bx0, 3, fan["frac"])
            texto(cr, fmt_milhar(fan["rpm"]), x + w, yc + 8, 22, 300, INK, "dir", tnum=True)


def _retangulo_arredondado(cr, x, y, w, h, r):
    r = min(r, w / 2, h / 2)
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def renderizar_png(painel, foto, caminho, larg=None, alt=None, aviso=False):
    """Desenha um quadro completo num arquivo PNG (usado em --png e nos testes)."""
    larg = int(larg or painel.W)
    alt = int(alt or round(larg * painel.H / painel.W))
    sup = cairo.ImageSurface(cairo.FORMAT_ARGB32, larg, alt)
    cr = cairo.Context(sup)
    painel.estatico(cr, larg, alt)
    exib = {k: (foto.get(k) or {}).get("temp") if k != "ram" else (foto.get("ram") or {}).get("pct")
            for k in ("cpu", "gpu", "ram")}
    painel.dinamico(cr, larg, alt, foto, exib, aviso=aviso)
    sup.write_to_png(caminho)
    return caminho
