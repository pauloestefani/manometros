"""Desenho dos mostradores com Cairo + Pango, no visual minimalista do esboço.

Tudo é desenhado num "espaço de projeto" fixo (1920x1080 para o monitor inteiro,
1920x280 para a faixa) e depois escalado para o tamanho real da tela.

Para gastar pouco processamento, o desenho é dividido em duas camadas:
- estática: trilhos, marcas, títulos, rótulos (desenhada uma vez e guardada);
- dinâmica: arcos de valor, números e barras (redesenhada só onde mudou).
"""
import math

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
    """Um manômetro de 270°. O arco vai de 0 a 100 % e o número do meio é outra
    medida (temperatura na CPU/GPU, GB usados na RAM). Coordenadas internas iguais
    às do SVG do esboço (viewBox 500x500)."""

    VB = (10, 24, 480, 420)
    # No modo compacto (janelinha) traços e textos ficam proporcionalmente maiores.
    NORMAL = {"trilho": 8, "faixa": 2, "marca": 2, "botao": (13.5, 8.5), "titulo": (22, 186, 500, 4),
              "num": (120, 292), "unid": (30, 226, 8), "sub": (21, 342), "estado": (18, 392), "pontas": True}
    COMPACTO = {"trilho": 16, "faixa": 5, "marca": 4, "botao": (22, 14), "titulo": (46, 176, 600, 5),
                "num": (150, 318), "unid": (48, 248, 10), "sub": None, "estado": (40, 404), "pontas": False}
    LARG_MAX_NUMERO = 220  # o número encolhe se ficar mais largo que isso (ex.: "101,3")

    def __init__(self, titulo, unidade, alerta=None, critico=None):
        self.titulo, self.unidade = titulo, unidade
        self.alerta, self.critico = alerta, critico  # marcas coloridas no arco (opcionais)

    def _entrar(self, cr, x, y, larg):
        cr.save()
        k = larg / self.VB[2]
        cr.translate(x, y)
        cr.scale(k, k)
        cr.translate(-self.VB[0], -self.VB[1])

    def altura(self, larg):
        return larg * self.VB[3] / self.VB[2]

    def _dim(self, larg):
        return self.COMPACTO if larg < 220 else self.NORMAL

    def estatico(self, cr, x, y, larg):
        d = self._dim(larg)
        self._entrar(cr, x, y, larg)
        cr.set_line_cap(cairo.LINE_CAP_BUTT)
        cr.set_line_width(d["trilho"])
        cor(cr, TRACK)
        _arco(cr, 196, INICIO, INICIO + VARREDURA)
        cr.stroke()
        if self.alerta is not None and self.critico is not None:
            cr.set_line_width(d["faixa"])
            cor(cr, AMBER, 0.75)
            _arco(cr, 216, _ang(self.alerta, 0, 100), _ang(self.critico, 0, 100))
            cr.stroke()
            cor(cr, RED, 0.75)
            _arco(cr, 216, _ang(self.critico, 0, 100), INICIO + VARREDURA)
            cr.stroke()
        cr.set_line_width(d["marca"])
        cor(cr, FAINT)
        for v in range(0, 101, 10):
            _traco(cr, v, 0, 100, 178 - d["trilho"] / 2, 188 - d["trilho"] / 2)
        cr.stroke()
        if d["pontas"]:
            for v in (0, 100):
                px, py = _pt(196, _ang(v, 0, 100))
                texto(cr, f"{v}%" if v else "0", px, py + 34, 20, 400, MUTED, "centro")
        tam, yb, peso, esp = d["titulo"]
        texto(cr, self.titulo, 250, yb, tam, peso, MUTED, "centro", espaco=esp)
        cr.restore()

    def dinamico(self, cr, x, y, larg, arco, numero, estado="normal", sub="", destaque=INK, pulso=1.0):
        """arco: 0–100 (ou None); numero: texto do meio (ou None)."""
        d = self._dim(larg)
        self._entrar(cr, x, y, larg)
        c_arco = {"normal": destaque, "alerta": AMBER, "critico": RED}[estado]
        c_num = {"normal": INK, "alerta": AMBER, "critico": RED}[estado]
        alfa = pulso if estado == "critico" else 1.0
        if arco is not None:
            a = _ang(arco, 0, 100)
            if a > INICIO + 0.3:
                cr.set_line_cap(cairo.LINE_CAP_BUTT)
                cr.set_line_width(d["trilho"])
                cor(cr, c_arco, alfa)
                _arco(cr, 196, INICIO, a)
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
        if numero is not None:
            w = largura(cr, numero, tam, 300, tnum=True)
            if w > self.LARG_MAX_NUMERO:
                tam *= self.LARG_MAX_NUMERO / w
            w = texto(cr, numero, 250, yb, tam, 300, c_num, "centro", tnum=True)
            tu, yu, dx = d["unid"]
            texto(cr, self.unidade, 250 + w / 2 + dx, yu, tu, 400, MUTED)
        else:
            texto(cr, "—", 250, yb - 6, tam * 0.75, 300, MUTED, "centro")
        if sub and d["sub"]:
            ts, ys = d["sub"]
            texto(cr, sub, 250, ys, ts, 400, MUTED, "centro")
        if estado != "normal":
            te, ye = d["estado"]
            texto(cr, "ALERTA" if estado == "alerta" else "CRÍTICO", 250, ye, te, 600, c_num, "centro", espaco=4)
        cr.restore()


# ---------------------------------------------------------------- painel
class Painel:
    """Monta o painel em três modos: 'janela' (canto), 'faixa' e 'inteiro'."""

    def __init__(self, cfg, layout=None):
        self.cfg = cfg
        self.layout = layout or cfg.get("layout", "janela")
        if self.layout not in ("janela", "inteiro", "faixa"):
            self.layout = "janela"
        self.destaque = hex_rgb(cfg.get("cor_destaque", "#EDEDED"))
        r = cfg["ram"]
        self.m = {
            "cpu": Mostrador("CPU", "°C"),
            "gpu": Mostrador("GPU", "°C"),
            "ram": Mostrador("RAM", "GB", r["alerta"], r["critico"]),
        }
        if self.layout == "janela":
            self.W, self.H = 520.0, 262.0
            gl = 150
            self.g = {k: (x, 14, gl) for k, x in zip(("cpu", "gpu", "ram"), (20, 185, 350))}
            self.reg = {"fans": (16, 166, 488, 92)}
        elif self.layout == "inteiro":
            self.W, self.H = 1920.0, 1080.0
            colw = (1920 - 160 - 160) / 3
            gl = 460
            self.g = {k: (80 + i * (colw + 80) + (colw - gl) / 2, 72, gl) for i, k in enumerate(("cpu", "gpu", "ram"))}
            self.reg = {"fans": (76, 600, 1768, 440)}
        else:
            self.W, self.H = 1920.0, 280.0
            gl = 250
            gap = (1808 - (3 * 250 + 1 + 900)) / 4
            x, pos = 56, []
            for w in (250, 250, 250, 1, 900):
                pos.append(x)
                x += w + gap
            gy = (280 - self.m["cpu"].altura(gl)) / 2
            self.g = {"cpu": (pos[0], gy, gl), "gpu": (pos[1], gy, gl), "ram": (pos[2], gy, gl)}
            self.div = pos[3]
            self.fx = pos[4]
            self.reg = {"fans": (self.fx - 4, 20, 908, 250)}
        for k, (x, y, gl) in self.g.items():
            self.reg[k] = (x - 2, y - 2, gl + 4, self.m[k].altura(gl) + 4)
        r = 12 if self.layout == "janela" else 28
        self.ponto = (self.W - r, r, 3.5 if self.layout == "janela" else 6)
        self.reg["aviso"] = (self.W - 2 * r, 0, 2 * r, 2 * r)

    # --- cores de estado: temperatura na CPU/GPU, % de uso na RAM
    def estado(self, k, foto):
        if not foto:
            return "normal"
        if k == "ram":
            v = (foto.get("ram") or {}).get("pct")
        else:
            v = (foto.get(k) or {}).get("temp")
        lim = self.cfg[k]
        if v is None:
            return "normal"
        if v >= lim["critico"]:
            return "critico"
        if v >= lim["alerta"]:
            return "alerta"
        return "normal"

    def numero_e_sub(self, k, foto):
        """Texto do meio do mostrador e a linha de baixo (só aparece nos modos grandes)."""
        if k == "ram":
            r = (foto or {}).get("ram")
            if not r:
                return None, ""
            gb = r["usado"] / 2**30
            num = (f"{gb:.0f}" if gb >= 100 else f"{gb:.1f}").replace(".", ",")
            return num, f"{r['pct']:.0f} % de {r['total'] / 2**30:.0f} GB"
        f = (foto or {}).get(k) or {}
        if k == "gpu" and not f.get("existe", True):
            return None, "GPU não encontrada"
        t = f.get("temp")
        partes = []
        if f.get("uso") is not None:
            partes.append(f"uso {f['uso']:.0f} %")
        if k == "cpu" and f.get("pico") is not None:
            partes.append(f"pico {round(f['pico'])} °C")
        if k == "gpu" and f.get("potencia") is not None:
            partes.append(f"{f['potencia']:.0f} W")
        if t is None and k == "cpu":
            partes.append("sensor não encontrado")
        return (None if t is None else f"{round(t)}"), " · ".join(partes)

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
        cor(cr, LINE)
        if self.layout == "janela":
            cr.rectangle(20, 158, 480, 1)
            cr.fill()
            self._cabecalho(cr, 20, 480, 182, "Ventoinhas", "rpm", 11)
        elif self.layout == "inteiro":
            cr.rectangle(80, 584, 1760, 1)
            cr.fill()
            self._cabecalho(cr, 80, 1760, 646, "Ventoinhas", "rpm")
        else:
            cr.rectangle(self.div, 28, 1, 224)
            cr.fill()
            self._cabecalho(cr, self.fx, 900, 47, "Ventoinhas", "rpm", 14)
        cr.restore()

    def _cabecalho(self, cr, x, w, y, esq, dir_="", tam=16):
        texto(cr, esq.upper(), x, y, tam, 500, MUTED, espaco=3 if tam >= 14 else 2)
        if dir_:
            texto(cr, dir_, x + w, y, tam + (1 if tam >= 14 else 0), 400, MUTED, "dir")

    def _trilho(self, cr, x, y, w, h):
        cor(cr, TRACK)
        _retangulo_arredondado(cr, x, y, w, h, h / 2)
        cr.fill()

    def _barra(self, cr, x, y, w, h, frac):
        frac = min(max(frac, 0.0), 1.0)
        if frac <= 0:
            return
        rgb = RED if frac >= 0.9 else AMBER if frac >= 0.75 else self.destaque
        cor(cr, rgb)
        _retangulo_arredondado(cr, x, y, max(w * frac, h), h, h / 2)
        cr.fill()

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
        if foto:
            for k, (x, y, gl) in self.g.items():
                if visivel(k):
                    num, sub = self.numero_e_sub(k, foto)
                    self.m[k].dinamico(cr, x, y, gl, exib.get(k), num, self.estado(k, foto), sub,
                                       self.destaque, pulso)
            if visivel("fans"):
                f = foto.get("ventoinhas") or {}
                {"janela": self._fans_janela, "inteiro": self._fans_inteiro, "faixa": self._fans_faixa}[
                    self.layout](cr, f)
        if aviso and visivel("aviso"):  # há versão nova: um ponto discreto no canto
            px, py, pr = self.ponto
            cor(cr, AMBER)
            cr.arc(px, py, pr, 0, 2 * math.pi)
            cr.fill()
        cr.restore()

    # --- ventoinhas
    @staticmethod
    def _curto(nome, n):
        return nome if len(nome) <= n else nome[: n - 1] + "…"

    def _sem_fans(self, cr, f, x, y, tam):
        texto(cr, "nenhuma ventoinha encontrada", x, y, tam, 400, MUTED)
        if f.get("aviso"):
            texto(cr, f["aviso"], x, y + tam * 1.6, tam - 2, 400, MUTED)

    def _fans_janela(self, cr, f):
        lista = (f.get("lista") or [])[:4]
        if not lista:
            return self._sem_fans(cr, f, 20, 208, 13)
        for i, fan in enumerate(lista):  # grade 2 x 2
            x, yb = 20 + (i % 2) * 255, 208 + (i // 2) * 28
            texto(cr, self._curto(fan["nome"], 16), x, yb, 13, 400, INK)
            texto(cr, fmt_milhar(fan["rpm"]), x + 225, yb, 17, 300, INK, "dir", tnum=True)
            self._trilho(cr, x, yb + 5, 225, 2)
            self._barra(cr, x, yb + 5, 225, 2, fan["frac"])

    def _fans_inteiro(self, cr, f):
        lista = (f.get("lista") or [])[:4]
        if not lista:
            return self._sem_fans(cr, f, 80, 740, 22)
        colw = (1760 - 3 * 80) / 4
        for i, fan in enumerate(lista):  # até 4 colunas
            x = 80 + i * (colw + 80)
            texto(cr, self._curto(fan["nome"], 24), x, 740, 22, 400, INK)
            wn = texto(cr, fmt_milhar(fan["rpm"]), x, 860, 96, 300, INK, tnum=True)
            texto(cr, "rpm", x + wn + 12, 860, 30, 400, MUTED)
            self._trilho(cr, x, 896, colw, 4)
            self._barra(cr, x, 896, colw, 4, fan["frac"])
        if f.get("fonte") and f.get("fonte") != "demo":
            rodape = f"via {f['fonte']}" + (f" · {f['aviso']}" if f.get("aviso") else "")
            texto(cr, rodape, 80, 982, 16, 400, MUTED)

    def _fans_faixa(self, cr, f):
        lista = (f.get("lista") or [])[:4]
        if not lista:
            return self._sem_fans(cr, f, self.fx, 112, 16)
        for i, fan in enumerate(lista):  # grade 2 x 2
            x, yb = self.fx + (i % 2) * 470, 112 + (i // 2) * 80
            texto(cr, self._curto(fan["nome"], 20), x, yb, 18, 400, INK)
            texto(cr, fmt_milhar(fan["rpm"]), x + 430, yb + 2, 30, 300, INK, "dir", tnum=True)
            self._trilho(cr, x, yb + 14, 430, 3)
            self._barra(cr, x, yb + 14, 430, 3, fan["frac"])


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
    painel.dinamico(cr, larg, alt, foto, valores_dos_arcos(foto), aviso=aviso)
    sup.write_to_png(caminho)
    return caminho


def valores_dos_arcos(foto):
    """O que cada arco mostra: uso da CPU, uso da GPU e % de RAM em uso."""
    foto = foto or {}
    return {"cpu": (foto.get("cpu") or {}).get("uso"),
            "gpu": (foto.get("gpu") or {}).get("uso"),
            "ram": (foto.get("ram") or {}).get("pct")}
