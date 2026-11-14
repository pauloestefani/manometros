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


def tinta(cr, s, tam, peso=400, tnum=False):
    """(início, largura) do desenho real dos caracteres, sem as margens laterais da fonte."""
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
    ink = lay.get_extents()[0]
    return ink.x / Pango.SCALE, ink.width / Pango.SCALE


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
def fmt_ghz(mhz):
    """2610 -> '2,61 GHz'"""
    if not mhz:
        return ""
    return f"{mhz / 1000:.2f} GHz".replace(".", ",")


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
    # Textos em dois tamanhos: "num" (o valor grande) e "pequeno" (nome, unidade e clock).
    # Nome, valor e clock formam um bloco centralizado no meio do círculo.
    NORMAL = {"trilho": 8, "faixa": 2, "marca": 2, "passo": 10, "botao": (13.5, 8.5),
              "num": 112, "pequeno": 24, "sub": 18, "peso_titulo": 500, "espaco_titulo": 4,
              "espacos": (0.95, 0.85, 0.7), "centro": 246, "min_px": 8.5,
              "estado_escrito": True, "pontas": True}
    # janelinha
    COMPACTO = {"trilho": 14, "faixa": 5, "marca": 3, "passo": 25, "botao": (20, 12.5),
                "num": 124, "pequeno": 30, "sub": None, "peso_titulo": 600, "espaco_titulo": 4,
                "espacos": (0.95, 0.85, 0.7), "centro": 246, "min_px": 8.5,
                "estado_escrito": False, "pontas": False}
    # janelinha nos tamanhos mínimos: sem marcas no arco e clock com uma casa decimal
    MICRO = {"trilho": 20, "faixa": 6, "marca": 0, "passo": 0, "botao": (24, 15),
             "num": 118, "pequeno": 30, "sub": None, "peso_titulo": 600, "espaco_titulo": 3,
             "espacos": (0.6, 0.55, 0.5), "centro": 234, "min_px": 8.0,
             "estado_escrito": False, "pontas": False, "linha_curta": True,
             "unidade_curta": True, "larg_max": 270}
    # "espacos": espaço depois do nome, do valor e do clock (em proporção do texto pequeno)
    # "centro": altura do meio do bloco (um pouco acima do centro do círculo, por causa do vão de baixo)
    LARG_MAX_NUMERO = 220  # o número encolhe se ficar mais largo que isso (ex.: "101,3")
    RAIO_TEXTO = 176       # até onde o texto pode ir a partir do centro sem encostar no arco
    TEXTO_MIN_PX = 8.5     # textos pequenos nunca ficam menores que isso na tela
    ALTURA_MAIUSC = 0.727  # altura das maiúsculas/dígitos da Inter em relação ao tamanho

    def __init__(self, titulo, unidade, alerta=None, critico=None):
        self.titulo, self.unidade = titulo, unidade
        self.alerta, self.critico = alerta, critico  # marcas coloridas no arco (opcionais)

    @staticmethod
    def _px(cr):
        """Quantos pixels (lógicos, como o resto da área de trabalho) vale uma unidade do desenho."""
        try:
            escala_tela = cr.get_target().get_device_scale()[0] or 1
        except (AttributeError, cairo.Error):
            escala_tela = 1
        return (abs(cr.user_to_device_distance(1, 0)[0]) / escala_tela) or 1

    def _legivel(self, cr, tam, maximo=60, min_px=None):
        """Aumenta um texto pequeno (em unidades do desenho) para não ficar ilegível na tela."""
        return min(max(tam, (min_px or self.TEXTO_MIN_PX) / self._px(cr)), maximo)

    def _entrar(self, cr, x, y, larg):
        cr.save()
        k = larg / self.VB[2]
        cr.translate(x, y)
        cr.scale(k, k)
        cr.translate(-self.VB[0], -self.VB[1])

    def altura(self, larg):
        return larg * self.VB[3] / self.VB[2]

    def _dim(self, cr, larg):
        """Escolhe o arranjo pelo tamanho real na tela (chamar depois de _entrar)."""
        if larg >= 220:
            return self.NORMAL
        return self.MICRO if self._px(cr) < 0.2 else self.COMPACTO

    def _textos_centrais(self, cr, d, numero, estado, c_num, linha, sub):
        """Nome, valor (com unidade) e clock empilhados e centralizados no meio do círculo."""
        A = self.ALTURA_MAIUSC
        peq = self._legivel(cr, d["pequeno"], min_px=d["min_px"])  # nome, unidade e clock: o mesmo tamanho
        num = num_nominal = d["num"]
        if linha and d.get("linha_curta") and linha.endswith(" GHz"):  # "4,18 GHz" -> "4,2 GHz"
            try:
                linha = f"{float(linha.split()[0].replace(',', '.')):.1f} GHz".replace(".", ",")
            except ValueError:
                pass
        titulo = self.titulo
        if estado != "normal" and d["estado_escrito"]:
            titulo += " · " + ("ALERTA" if estado == "alerta" else "CRÍTICO")
        texto_num = numero if numero is not None else "—"
        # o número fica exatamente no eixo central; a unidade fica pendurada à direita dele.
        # Se não couber no círculo (ex.: "13,1" + "GB" na janelinha bem pequena), o número encolhe.
        unidade = self.unidade
        if d.get("unidade_curta"):  # janelinha mínima: "58°" e, na RAM, só o número
            unidade = "°" if self.unidade == "°C" else ""
        grau_colado = unidade == "°"
        dx = num * 0.02 if grau_colado else peq * 0.25
        tam_u = num if grau_colado else peq
        wu = largura(cr, unidade, tam_u, 300) if (numero is not None and unidade) else 0
        ix, iw = tinta(cr, texto_num, num, 300, tnum=True)
        meia_max = min(d.get("larg_max", self.LARG_MAX_NUMERO) / 2, self.RAIO_TEXTO - dx - wu)
        if iw / 2 > meia_max:
            num *= meia_max / (iw / 2)
            ix, iw = tinta(cr, texto_num, num, 300, tnum=True)
        # linhas do bloco: (altura das maiúsculas, espaço depois)
        e1, e2, e3 = d["espacos"]
        linhas = [("titulo", A * peq, e1 * peq), ("valor", A * num_nominal, e2 * peq)]
        if linha:
            linhas.append(("linha", A * peq, e3 * peq))
        sub_tam = d["sub"]
        if sub and sub_tam:
            linhas.append(("sub", A * sub_tam, 0))
        altura = sum(h for _, h, _ in linhas) + sum(g for _, _, g in linhas[:-1])
        y = d["centro"] - altura / 2
        cor_titulo = MUTED if estado == "normal" else c_num
        for nome, h, g in linhas:
            base = y + h
            if nome == "titulo":
                texto(cr, titulo, 250, base, peq, d["peso_titulo"], cor_titulo, "centro", espaco=d["espaco_titulo"])
            elif nome == "valor":
                x0 = 250 - (ix + iw / 2)  # centro do desenho dos dígitos no eixo
                texto(cr, texto_num, x0, base, num, 300, c_num if numero is not None else MUTED, tnum=True)
                if wu and grau_colado:  # "°" do mesmo tamanho do número, na mesma linha de base
                    texto(cr, unidade, 250 + iw / 2 + dx, base, num, 300, c_num if numero is not None else MUTED)
                elif wu:  # unidade pequena alinhada ao topo do número, logo depois dele
                    texto(cr, unidade, 250 + iw / 2 + dx, base - A * num + A * peq, peq, 400, MUTED)
            elif nome == "linha":
                texto(cr, linha, 250, base, peq, 400, MUTED, "centro", tnum=True)
            else:
                texto(cr, sub, 250, base, sub_tam, 400, MUTED, "centro")
            y = base + g

    def estatico(self, cr, x, y, larg):
        self._entrar(cr, x, y, larg)
        d = self._dim(cr, larg)
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
        if d["passo"]:
            cr.set_line_width(d["marca"])
            cor(cr, FAINT)
            for v in range(0, 101, d["passo"]):
                _traco(cr, v, 0, 100, 178 - d["trilho"] / 2, 188 - d["trilho"] / 2)
            cr.stroke()
        if d["pontas"]:
            for v in (0, 100):
                px, py = _pt(196, _ang(v, 0, 100))
                texto(cr, f"{v}%" if v else "0", px, py + 34, 20, 400, MUTED, "centro")
        cr.restore()

    def dinamico(self, cr, x, y, larg, arco, numero, estado="normal", sub="", destaque=INK, pulso=1.0, linha=""):
        """arco: 0–100 (ou None); numero: texto do meio (ou None); linha: texto pequeno embaixo dele."""
        self._entrar(cr, x, y, larg)
        d = self._dim(cr, larg)
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
        self._textos_centrais(cr, d, numero, estado, c_num, linha, sub)
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
            gl, gap, pad = 168, 28, 22
            self.W = 2 * pad + 3 * gl + 2 * gap
            self.H = 18 + self.m["cpu"].altura(gl) + 14
            self.g = {k: (pad + i * (gl + gap), 18, gl) for i, k in enumerate(("cpu", "gpu", "ram"))}
        elif self.layout == "inteiro":
            self.W, self.H = 1920.0, 1080.0
            gl, gap = 560, 60
            x0 = (1920 - (3 * gl + 2 * gap)) / 2
            y0 = (1080 - self.m["cpu"].altura(gl)) / 2
            self.g = {k: (x0 + i * (gl + gap), y0, gl) for i, k in enumerate(("cpu", "gpu", "ram"))}
        else:
            self.W, self.H = 1920.0, 280.0
            gl, gap = 250, 160
            x0 = (1920 - (3 * gl + 2 * gap)) / 2
            y0 = (280 - self.m["cpu"].altura(gl)) / 2
            self.g = {k: (x0 + i * (gl + gap), y0, gl) for i, k in enumerate(("cpu", "gpu", "ram"))}
        self.reg = {}
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

    def textos(self, k, foto):
        """(número do meio, linha pequena embaixo dele, linha extra dos modos grandes)."""
        if k == "ram":
            r = (foto or {}).get("ram")
            if not r:
                return None, "", ""
            gb = r["usado"] / 2**30
            num = (f"{gb:.0f}" if gb >= 100 else f"{gb:.1f}").replace(".", ",")
            return num, f"de {r['total'] / 2**30:.0f} GB", f"{r['pct']:.0f} % em uso"
        f = (foto or {}).get(k) or {}
        if k == "gpu" and not f.get("existe", True):
            return None, "", "GPU não encontrada"
        t = f.get("temp")
        linha = fmt_ghz(f.get("clock"))
        partes = []
        if f.get("uso") is not None:
            partes.append(f"uso {f['uso']:.0f} %")
        if k == "cpu" and f.get("pico") is not None:
            partes.append(f"pico {round(f['pico'])} °C")
        if k == "gpu" and f.get("potencia") is not None:
            partes.append(f"{f['potencia']:.0f} W")
        if t is None and k == "cpu":
            partes.append("sensor não encontrado")
        return (None if t is None else f"{round(t)}"), linha, " · ".join(partes)

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
        cr.restore()

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
                    num, linha, sub = self.textos(k, foto)
                    self.m[k].dinamico(cr, x, y, gl, exib.get(k), num, self.estado(k, foto), sub,
                                       self.destaque, pulso, linha)
        if aviso and visivel("aviso"):  # há versão nova: um ponto discreto no canto
            px, py, pr = self.ponto
            cor(cr, AMBER)
            cr.arc(px, py, pr, 0, 2 * math.pi)
            cr.fill()
        cr.restore()


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
