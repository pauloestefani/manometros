"""Janela do painel: GTK3 + gtk-layer-shell (Wayland) para fixar o painel num monitor."""
import argparse
import fcntl
import math
import os
import signal
import subprocess
import sys
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gio, GLib, Gtk  # noqa: E402

try:
    gi.require_version("GtkLayerShell", "0.1")
    from gi.repository import GtkLayerShell  # noqa: E402
except (ValueError, ImportError):
    GtkLayerShell = None

from . import __version__, atualizacao, coleta, desenho  # noqa: E402
from . import config as C  # noqa: E402

DURACAO_ANIM = 0.30  # segundos

CANTOS = {
    "superior-direito": ("TOP", "RIGHT", "Superior direito"),
    "superior-esquerdo": ("TOP", "LEFT", "Superior esquerdo"),
    "inferior-direito": ("BOTTOM", "RIGHT", "Inferior direito"),
    "inferior-esquerdo": ("BOTTOM", "LEFT", "Inferior esquerdo"),
}
TAMANHOS = [("Pequeno", 0.8), ("Médio", 1.0), ("Grande", 1.25), ("Muito grande", 1.5)]
MODOS = [("Janelinha no canto", "janela"), ("Faixa na parte de baixo", "faixa"), ("Monitor inteiro", "inteiro")]


# ---------------------------------------------------------------- monitores
def listar_monitores(display=None):
    display = display or Gdk.Display.get_default()
    saida = []
    if display is None:
        return saida
    for i in range(display.get_n_monitors()):
        m = display.get_monitor(i)
        g = m.get_geometry()
        fab, mod = (m.get_manufacturer() or "").strip(), (m.get_model() or "").strip()
        nome = " ".join(p for p in (fab, mod) if p and p.lower() != "unknown")
        saida.append({
            "indice": i, "monitor": m, "fabricante": fab, "modelo": mod, "nome": nome,
            "largura": g.width, "altura": g.height, "escala": m.get_scale_factor(),
        })
    return saida


def id_monitor(m, monitores):
    """Como guardar a escolha: pelo nome do modelo (estável) ou, se não der, pelo número."""
    if m["nome"] and sum(1 for x in monitores if x["nome"] == m["nome"]) == 1:
        return m["nome"]
    return str(m["indice"])


def _casar(pedido, monitores):
    if pedido.isdigit():
        return next((m for m in monitores if m["indice"] == int(pedido)), None)
    p = pedido.lower()
    return next((m for m in monitores if p in f"{m['fabricante']} {m['modelo']}".lower()), None)


def escolher_monitor(cfg, monitores, layout=None):
    """Escolhe o monitor do painel. Devolve (dicionário, motivo) ou (None, motivo)."""
    layout = layout or cfg.get("layout", "janela")
    if not monitores:
        return None, "nenhum monitor"
    pedido = str(cfg.get("monitor", "auto")).strip()
    if pedido and pedido.lower() != "auto":
        m = _casar(pedido, monitores)
        if m is not None:
            return m, f"escolhido: '{pedido}'"
        if layout != "janela":
            return None, f"monitor '{pedido}' não está conectado; aguardando"
        aviso = f"monitor '{pedido}' não está conectado; usando o automático"
    else:
        aviso = "automático"
    if len(monitores) == 1:
        if layout == "janela" or cfg.get("mostrar_sem_segundo_monitor"):
            return monitores[0], aviso + " (único monitor)"
        return None, "só um monitor conectado; aguardando o segundo"
    excluir = [e.lower() for e in cfg.get("excluir_monitores") or []]

    def excluido(m):
        nome = f"{m['fabricante']} {m['modelo']}".lower()
        return any(e and e in nome for e in excluir) or m["largura"] / max(m["altura"], 1) >= 2.0

    candidatos = [m for m in monitores if not excluido(m)]
    if candidatos:
        return min(candidatos, key=lambda m: (m["largura"] / max(m["altura"], 1), m["indice"])), aviso
    maior = max(monitores, key=lambda m: m["largura"] * m["altura"])
    resto = [m for m in monitores if m is not maior]
    return (resto[0], aviso + " (não é o maior)") if resto else (None, "nenhum candidato")


def reiniciar_processo(argv):
    """Troca o processo atual por um novo (usado depois de mudar o modo ou de atualizar)."""
    limpos, pular = [], False
    for a in argv:
        if pular:
            pular = False
            continue
        if a in ("--layout", "--monitor"):
            pular = True
            continue
        if a.startswith(("--layout=", "--monitor=")) or a == "--reiniciar":
            continue
        limpos.append(a)
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable, "-m", "manometros"] + limpos)


# ---------------------------------------------------------------- janela
class Janela(Gtk.Window):
    def __init__(self, cfg, args, argv):
        super().__init__(title="Manômetros")
        self.cfg = cfg
        self.args = args
        self.argv = argv
        self.painel = desenho.Painel(cfg, args.layout or cfg.get("layout"))
        self.layout = self.painel.layout
        self.escala = min(max(float(cfg.get("escala", 1.0) or 1.0), 0.5), 3.0)
        self.foto = None
        self.exib = {"cpu": None, "gpu": None, "ram": None}
        self.anim = {}
        self.assinaturas = {}
        self.ticker = None
        self.estatico = None
        self.estatico_chave = None
        self.monitor_atual = None
        self.encerrando = False
        self.novidade = None      # informações da versão nova, quando houver
        self.ocupado = False      # atualização em andamento
        self._menu = None
        self.usar_layer = (not args.janela) and GtkLayerShell is not None and GtkLayerShell.is_supported()

        if self.layout == "janela":  # fundo transparente para os cantos arredondados
            visual = self.get_screen().get_rgba_visual()
            if visual is not None:
                self.set_visual(visual)
            self.set_app_paintable(True)

        self.area = Gtk.DrawingArea()
        self.area.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.area.connect("draw", self.on_draw)
        self.area.connect("button-press-event", self.on_clique)
        self.area.connect("size-allocate", lambda *_: self._invalidar_estatico())
        self.area.set_tooltip_text("Clique para escolher monitor, posição, tamanho e mais")
        self.add(self.area)
        self.connect("delete-event", self.on_delete)

        if self.usar_layer:
            self._configurar_layer()
        else:
            if not args.janela:
                print("Aviso: gtk-layer-shell indisponível; abrindo como janela comum.", file=sys.stderr)
            if self.layout == "janela":
                self.set_default_size(*self._tamanho_janela())
            elif self.layout == "faixa":
                self.set_default_size(1600, int(1600 * self.painel.H / self.painel.W))
            else:
                self.set_default_size(1280, 720)

        disp = Gdk.Display.get_default()
        disp.connect("monitor-added", lambda *_: GLib.timeout_add(800, self._reavaliar_monitor))
        disp.connect("monitor-removed", lambda *_: GLib.timeout_add(800, self._reavaliar_monitor))

        if (cfg.get("atualizacao") or {}).get("verificar", True):
            GLib.timeout_add_seconds(45, self._verificar_auto)

    # --- layer shell
    def _tamanho_janela(self):
        return int(round(self.painel.W * self.escala)), int(round(self.painel.H * self.escala))

    def _configurar_layer(self):
        L = GtkLayerShell
        L.init_for_window(self)
        L.set_namespace(self, "manometros")
        if hasattr(L, "set_keyboard_mode"):  # teclado só quando você clica (para o menu)
            L.set_keyboard_mode(self, L.KeyboardMode.ON_DEMAND)
        if self.layout == "janela":
            self._aplicar_camada()
            self._aplicar_canto()
            L.set_exclusive_zone(self, 0)  # respeita o painel do COSMIC
            self.set_size_request(*self._tamanho_janela())
        elif self.layout == "faixa":
            L.set_layer(self, L.Layer.TOP)
            for borda in (L.Edge.LEFT, L.Edge.RIGHT, L.Edge.BOTTOM):
                L.set_anchor(self, borda, True)
            L.auto_exclusive_zone_enable(self)
        else:
            L.set_layer(self, L.Layer.BOTTOM)
            for borda in (L.Edge.LEFT, L.Edge.RIGHT, L.Edge.TOP, L.Edge.BOTTOM):
                L.set_anchor(self, borda, True)
            L.set_exclusive_zone(self, 0)

    def _aplicar_camada(self):
        L = GtkLayerShell
        L.set_layer(self, L.Layer.BOTTOM if self.cfg.get("camada") == "abaixo" else L.Layer.TOP)

    def _aplicar_canto(self):
        L = GtkLayerShell
        v, h, _ = CANTOS.get(self.cfg.get("canto"), CANTOS["superior-direito"])
        margem = int(self.cfg.get("margem", 16))
        for nome in ("TOP", "BOTTOM", "LEFT", "RIGHT"):
            borda = getattr(L.Edge, nome)
            L.set_anchor(self, borda, nome in (v, h))
            L.set_margin(self, borda, margem)

    def _altura_faixa(self, mon):
        return int(round(mon["largura"] * self.painel.H / self.painel.W))

    def iniciar(self):
        if self.usar_layer:
            self._reavaliar_monitor()
        else:
            self.show_all()
            if not self.args.janela and self.layout == "inteiro":
                m, _ = escolher_monitor(self.cfg, listar_monitores(), self.layout)
                if m is not None:
                    self.fullscreen_on_monitor(self.get_screen(), m["indice"])

    def _reavaliar_monitor(self, forcar=False):
        if not self.usar_layer or self.encerrando:
            return False
        mons = listar_monitores()
        m, motivo = escolher_monitor(self.cfg, mons, self.layout)
        alvo = m["monitor"] if m else None
        if not forcar and alvo == self.monitor_atual and (alvo is None or self.get_visible()):
            return False
        nome = f"{m['nome'] or 'monitor ' + str(m['indice'])} ({m['largura']}x{m['altura']})" if m else "-"
        print(f"Monitor: {nome} — {motivo}", file=sys.stderr)
        self.hide()
        self.monitor_atual = alvo
        if alvo is not None:
            GtkLayerShell.set_monitor(self, alvo)
            if self.layout == "faixa":
                self.set_size_request(-1, self._altura_faixa(m))
            self.show_all()
        return False

    def on_delete(self, *_):
        # Em layer-shell o compositor "fecha" a janela quando o monitor some.
        # Nesse caso só escondemos e esperamos o monitor voltar.
        if self.usar_layer and not self.encerrando:
            self.hide()
            self.monitor_atual = None
            GLib.timeout_add(1500, self._reavaliar_monitor)
            return True
        Gtk.main_quit()
        return False

    # --- menu
    def on_clique(self, _widget, ev):
        if ev.type != Gdk.EventType.BUTTON_PRESS:
            return False
        self._menu = self._montar_menu()
        self._menu.show_all()
        self._menu.popup_at_pointer(ev)
        return True

    def _submenu(self, menu, titulo, opcoes, atual, escolher):
        item = Gtk.MenuItem(label=titulo)
        sub = Gtk.Menu()
        grupo, itens = None, []
        for rotulo, valor in opcoes:
            r = Gtk.RadioMenuItem.new_with_label_from_widget(grupo, rotulo)
            grupo = grupo or r
            r.set_active(valor == atual)
            sub.append(r)
            itens.append((r, valor))
        for r, valor in itens:  # conecta só depois de marcar o atual, para não disparar à toa
            r.connect("toggled", lambda w, v=valor: w.get_active() and GLib.idle_add(escolher, v))
        item.set_submenu(sub)
        menu.append(item)

    def _montar_menu(self):
        menu = Gtk.Menu()
        mons = listar_monitores()
        pedido = str(self.cfg.get("monitor", "auto"))
        if pedido.lower() == "auto":
            atual = "auto"
        else:
            m = _casar(pedido, mons)
            atual = id_monitor(m, mons) if m else pedido
        opcoes = [("Automático (o que não é ultrawide)", "auto")]
        for m in mons:
            rotulo = f"{m['nome'] or 'Monitor ' + str(m['indice'] + 1)} — {m['largura']}×{m['altura']}"
            opcoes.append((rotulo, id_monitor(m, mons)))
        self._submenu(menu, "Monitor", opcoes, atual, self._mudar_monitor)
        if self.layout == "janela":
            self._submenu(menu, "Posição", [(r, k) for k, (_, _, r) in CANTOS.items()],
                          self.cfg.get("canto", "superior-direito"), self._mudar_canto)
            self._submenu(menu, "Tamanho", TAMANHOS,
                          min((v for _, v in TAMANHOS), key=lambda v: abs(v - self.escala)), self._mudar_escala)
            acima = Gtk.CheckMenuItem(label="Ficar por cima das janelas")
            acima.set_active(self.cfg.get("camada", "acima") != "abaixo")
            acima.connect("toggled", lambda w: GLib.idle_add(self._mudar_camada, w.get_active()))
            menu.append(acima)
        self._submenu(menu, "Modo", MODOS, self.layout, self._mudar_modo)
        menu.append(Gtk.SeparatorMenuItem())

        if self.novidade:
            rot = f"Atualizar para a versão {self.novidade['versao']}…"
        else:
            rot = "Verificar atualizações"
        item = Gtk.MenuItem(label=rot)
        item.set_sensitive(not self.ocupado)
        item.connect("activate", lambda *_: GLib.idle_add(self._acao_atualizar))
        menu.append(item)
        item = Gtk.MenuItem(label="Abrir configuração")
        item.connect("activate", lambda *_: GLib.idle_add(self._abrir_config))
        menu.append(item)
        sobre = Gtk.MenuItem(label=f"Manômetros {__version__}")
        sobre.set_sensitive(False)
        menu.append(sobre)
        menu.append(Gtk.SeparatorMenuItem())
        item = Gtk.MenuItem(label="Fechar o painel")
        item.connect("activate", lambda *_: GLib.idle_add(self._fechar))
        menu.append(item)
        return menu

    def _salvar(self, **chaves):
        try:
            C.salvar(chaves, self.args.config or None)
        except OSError as exc:
            self._dialogo("Não consegui salvar a escolha", str(exc), Gtk.MessageType.ERROR)
        self.cfg.update(chaves)

    def _mudar_monitor(self, valor):
        self._salvar(monitor=valor)
        self._reavaliar_monitor(forcar=True)
        return False

    def _mudar_canto(self, valor):
        self._salvar(canto=valor)
        if self.usar_layer:
            self.hide()
            self._aplicar_canto()
            self.show_all()
        return False

    def _mudar_escala(self, valor):
        self._salvar(escala=valor)
        self.escala = valor
        w, h = self._tamanho_janela()
        if self.usar_layer:
            self.set_size_request(w, h)
        else:
            self.resize(w, h)
        return False

    def _mudar_camada(self, acima):
        self._salvar(camada="acima" if acima else "abaixo")
        if self.usar_layer:
            self.hide()
            self._aplicar_camada()
            self.show_all()
        return False

    def _mudar_modo(self, valor):
        if valor == self.layout:
            return False
        self._salvar(layout=valor)
        reiniciar_processo(self.argv)
        return False

    def _abrir_config(self):
        caminho = self.args.config or C.CONFIG_FILE
        if not os.path.exists(caminho):
            C.salvar({}, caminho)
        try:
            Gio.AppInfo.launch_default_for_uri(GLib.filename_to_uri(caminho, None), None)
        except GLib.Error:
            self._dialogo("Arquivo de configuração", f"Abra este arquivo num editor de texto:\n{caminho}")
        return False

    def _fechar(self):
        self.encerrando = True
        Gtk.main_quit()
        return False

    def _dialogo(self, titulo, texto, tipo=Gtk.MessageType.INFO, botoes=None, ao_responder=None):
        d = Gtk.MessageDialog(message_type=tipo, buttons=Gtk.ButtonsType.NONE if botoes else Gtk.ButtonsType.OK,
                              text=titulo)
        d.set_title("Manômetros")
        d.format_secondary_text(texto)
        for rotulo, resp in botoes or []:
            d.add_button(rotulo, resp)

        def responder(dlg, resp):
            dlg.destroy()
            if ao_responder:
                ao_responder(resp)

        d.connect("response", responder)
        d.show_all()
        return d

    # --- atualização
    def _verificar_auto(self):
        self._verificar(silencioso=True)
        GLib.timeout_add_seconds(24 * 3600, self._verificar_auto)
        return False

    def _verificar(self, silencioso):
        def trabalho():
            try:
                info, erro = atualizacao.verificar(self.cfg), None
            except Exception as exc:  # sem internet, GitHub fora do ar...
                info, erro = None, exc
            GLib.idle_add(self._resultado_verificacao, info, erro, silencioso)

        threading.Thread(target=trabalho, daemon=True).start()

    def _resultado_verificacao(self, info, erro, silencioso):
        if erro is not None:
            print(f"Verificação de atualização falhou: {erro}", file=sys.stderr)
            if not silencioso:
                self._dialogo("Não consegui verificar atualizações", f"{erro}", Gtk.MessageType.WARNING)
        elif atualizacao.tem_novidade(info):
            self.novidade = info
            self._invalidar("aviso")
            if not silencioso:
                self._oferecer_atualizacao()
        elif not silencioso:
            self._dialogo("Você já está na versão mais recente", f"Versão instalada: {__version__}")
        return False

    def _acao_atualizar(self):
        if self.novidade:
            self._oferecer_atualizacao()
        else:
            self._verificar(silencioso=False)
        return False

    def _oferecer_atualizacao(self):
        info = self.novidade
        ok, motivo = atualizacao.pode_atualizar()
        if not ok:
            self._dialogo(f"Versão {info['versao']} disponível", f"Não dá para atualizar por aqui: {motivo}.",
                          Gtk.MessageType.WARNING)
            return
        notas = info.get("notas") or "Sem notas para esta versão."
        if len(notas) > 900:
            notas = notas[:900].rstrip() + "…"
        texto = (f"Você está na versão {__version__}.\n\nNovidades:\n{notas}\n\n"
                 "O painel baixa a versão nova do git (GitHub), instala na sua pasta (sem senha) e reinicia.")
        self._dialogo(f"Versão {info['versao']} disponível", texto, Gtk.MessageType.QUESTION,
                      [("Agora não", Gtk.ResponseType.CANCEL), ("Atualizar agora", Gtk.ResponseType.OK)],
                      lambda r: r == Gtk.ResponseType.OK and self._instalar(info))

    def _instalar(self, info):
        self.ocupado = True

        def trabalho():
            try:
                versao, erro = atualizacao.instalar(info, avisar=lambda t: print(t, file=sys.stderr)), None
            except Exception as exc:
                versao, erro = None, exc
            GLib.idle_add(self._fim_instalacao, versao, erro)

        threading.Thread(target=trabalho, daemon=True).start()

    def _fim_instalacao(self, versao, erro):
        self.ocupado = False
        if erro is not None:
            self._dialogo("A atualização não deu certo", f"{erro}\n\nA versão atual continua instalada.",
                          Gtk.MessageType.ERROR)
        else:
            print(f"Atualizado para {versao}; reiniciando.", file=sys.stderr)
            self._dialogo(f"Atualizado para a versão {versao}", "O painel vai reiniciar agora.",
                          ao_responder=lambda _r: reiniciar_processo(self.argv))
        return False

    # --- dados
    def receber(self, foto):
        """Chamado pelo coletor (via GLib.idle_add) com dados novos."""
        self.foto = foto
        agora = time.monotonic()
        alvos = desenho.valores_dos_arcos(foto)  # uso da CPU, uso da GPU, % de RAM
        animar = self.cfg.get("animacao", True)
        for k, alvo in alvos.items():
            atual = self.exib.get(k)
            if alvo is None or atual is None or not animar:
                if alvo != atual:
                    self.exib[k] = alvo
                    self.anim.pop(k, None)
                    self._invalidar(k)
            elif abs(alvo - atual) >= 0.05:
                self.anim[k] = (atual, alvo, agora)
            self._invalidar_se_mudou(k, self._sig_mostrador(k))
        self._invalidar_se_mudou("fans", self._sig_fans())
        self._ligar_ticker()
        return False

    def _sig_mostrador(self, k):
        # o número do meio e a cor não são animados: redesenha quando mudam
        num, sub = self.painel.numero_e_sub(k, self.foto)
        return (num, sub, self.painel.estado(k, self.foto))

    def _sig_fans(self):
        fans = (self.foto or {}).get("ventoinhas") or {}
        return (tuple((x["nome"], x["rpm"]) for x in fans.get("lista") or []), fans.get("fonte"), fans.get("aviso"))

    def _invalidar_se_mudou(self, nome, sig):
        if self.assinaturas.get(nome, object()) != sig:
            self.assinaturas[nome] = sig
            self._invalidar(nome)

    # --- animação
    def _ligar_ticker(self):
        precisa = bool(self.anim) or self._algum_critico()
        if precisa and self.ticker is None:
            fps = max(5, min(60, int(self.cfg.get("fps", 24))))
            self.ticker = GLib.timeout_add(int(1000 / fps), self._tick)

    def _algum_critico(self):
        return any(self.painel.estado(k, self.foto) == "critico" for k in self.exib)

    def _tick(self):
        agora = time.monotonic()
        for k, (v0, v1, t0) in list(self.anim.items()):
            p = min(1.0, (agora - t0) / DURACAO_ANIM)
            e = 1 - (1 - p) ** 3
            self.exib[k] = v0 + (v1 - v0) * e
            self._invalidar(k)
            if p >= 1.0:
                del self.anim[k]
        criticos = [k for k in self.exib if self.painel.estado(k, self.foto) == "critico"]
        for k in criticos:
            self._invalidar(k)
        if self.anim or criticos:
            return True
        self.ticker = None
        return False

    def _pulso(self):
        return 0.55 + 0.45 * (0.5 + 0.5 * math.cos(time.monotonic() * math.pi))  # ~2 s por ciclo

    # --- desenho
    def _invalidar(self, nome):
        a = self.area.get_allocation()
        if a.width > 1 and a.height > 1:
            self.area.queue_draw_area(*self.painel.regiao_na_tela(nome, a.width, a.height))

    def _invalidar_estatico(self):
        self.estatico = None

    def on_draw(self, widget, cr):
        larg, alt = widget.get_allocated_width(), widget.get_allocated_height()
        esc = widget.get_scale_factor()
        chave = (larg, alt, esc)
        cairo = desenho.cairo
        if self.estatico is None or self.estatico_chave != chave:
            formato = cairo.FORMAT_ARGB32 if self.layout == "janela" else cairo.FORMAT_RGB24
            sup = cr.get_target().create_similar_image(formato, larg * esc, alt * esc)
            sup.set_device_scale(esc, esc)
            self.painel.estatico(cairo.Context(sup), larg, alt)
            self.estatico, self.estatico_chave = sup, chave
        cr.save()
        cr.set_operator(cairo.OPERATOR_SOURCE)  # copia inclusive a transparência dos cantos
        cr.set_source_surface(self.estatico, 0, 0)
        cr.paint()
        cr.restore()
        self.painel.dinamico(cr, larg, alt, self.foto, self.exib, self._pulso(), clip=cr.clip_extents(),
                             aviso=bool(self.novidade))
        return True


# ---------------------------------------------------------------- utilidades de linha de comando
def _caminho_trava():
    pasta = os.environ.get("XDG_RUNTIME_DIR") or C.CACHE_DIR
    os.makedirs(pasta, exist_ok=True)
    return os.path.join(pasta, "manometros.lock")


def _trava():
    fh = open(_caminho_trava(), "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.seek(0)
        pid = fh.read().strip()
        fh.close()
        return None, pid
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh, None


def _pid_rodando():
    caminho = _caminho_trava()
    try:
        with open(caminho) as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return None  # ninguém segurando a trava
    except BlockingIOError:
        with open(caminho) as fh:
            p = fh.read().strip()
        return int(p) if p.isdigit() else None
    except OSError:
        return None


def parar_instancia(quieto=False):
    pid = _pid_rodando()
    if not pid:
        if not quieto:
            print("O painel não está rodando.")
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.1)
        if _pid_rodando() is None:
            break
    print(f"Painel parado (pid {pid}).")
    return True


def iniciar_em_segundo_plano(argv):
    subprocess.Popen([sys.executable, "-m", "manometros"] + argv, start_new_session=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def listar(cfg):
    print(f"Manômetros {__version__}\n")
    print("== Monitores ==")
    tem_tela = Gtk.init_check(sys.argv[:1])[0]
    mons = listar_monitores() if tem_tela else []
    escolhido, motivo = escolher_monitor(cfg, mons)
    if not mons:
        print("  (nenhum: rode este comando num terminal aberto dentro da sessão gráfica)")
    for m in mons:
        marca = "  <- painel" if escolhido is not None and m["indice"] == escolhido["indice"] else ""
        print(f"  [{m['indice']}] {m['nome'] or '(sem nome)'} — {m['largura']}x{m['altura']}"
              f" (escala {m['escala']}){marca}")
    print(f"  modo: {cfg.get('layout')}; escolha: {motivo}")
    sessao = "Wayland" if os.environ.get("WAYLAND_DISPLAY") else "X11" if os.environ.get("DISPLAY") else "?"
    print(f"  sessão: {sessao}; layer-shell: "
          f"{'sim' if GtkLayerShell is not None and GtkLayerShell.is_supported() else 'não'}")

    print("\n== CPU ==")
    cpu = coleta.SensorCPU()
    uso = coleta.UsoCPU()
    time.sleep(0.5)
    t, u = cpu.ler(), uso.ler()
    print(f"  sensor: {cpu.descricao}; agora: {'—' if t is None else f'{t:.0f} °C'}"
          f"; uso: {'—' if u is None else f'{u:.0f} %'}")

    print("\n== GPU ==")
    gpu = coleta.SensorGPU()
    gt, gp, gu = gpu.ler()
    if gpu.nome:
        print(f"  {gpu.nome}: {gt} °C, {gp if gp is None else round(gp)} W, uso {gu} %")
    else:
        print(f"  não encontrada ({gpu.erro})")

    print("\n== RAM ==")
    r = coleta.ler_ram()
    if r:
        print(f"  {r['usado'] / 2**30:.1f} de {r['total'] / 2**30:.1f} GB ({r['pct']:.0f} %)".replace(".", ","))

    print("\n== Ventoinhas ==")
    v = coleta.Ventoinhas(cfg)
    todas = v.todas()
    print(f"  fonte: {v.fonte}" + (f" ({v.aviso})" if v.aviso else ""))
    for ident, nome, rpm in todas:
        print(f"  {ident:<40} {nome:<18} {rpm:>6} rpm")
    if not todas:
        print("  nenhuma encontrada")
    print("  (para escolher/renomear, use os ids acima em [ventoinhas] no config.toml)")

    print(f"\nConfiguração: {C.CONFIG_FILE}{'' if os.path.exists(C.CONFIG_FILE) else ' (não existe; usando o padrão)'}")
    print(f"Atualizações: https://github.com/{atualizacao.repositorio(cfg)}")


def png(cfg, args):
    painel = desenho.Painel(cfg, args.layout or cfg.get("layout"))
    if args.demo:
        foto = coleta.foto_demo(time.time())
    else:
        foto = coleta.Coletor(cfg, lambda f: None).foto()
    desenho.renderizar_png(painel, foto, args.png, args.largura)
    print(f"Imagem salva em {args.png}")


def atualizar_terminal(cfg):
    print(f"Versão instalada: {__version__}")
    print(f"Procurando em https://github.com/{atualizacao.repositorio(cfg)} …")
    try:
        info = atualizacao.verificar(cfg)
    except Exception as exc:
        print(f"Não consegui verificar: {exc}")
        return 1
    if not atualizacao.tem_novidade(info):
        print("Você já está na versão mais recente.")
        return 0
    print(f"\nVersão nova: {info['versao']}\n")
    if info.get("notas"):
        print(info["notas"] + "\n")
    ok, motivo = atualizacao.pode_atualizar()
    if not ok:
        print(f"Não dá para atualizar por aqui: {motivo}.")
        return 1
    if input("Instalar agora? [s/N] ").strip().lower()[:1] not in ("s", "y"):
        print("Nada foi alterado.")
        return 0
    try:
        versao = atualizacao.instalar(info)
    except Exception as exc:
        print(f"A atualização não deu certo: {exc}\nA versão atual continua instalada.")
        return 1
    print(f"Pronto: versão {versao} instalada.")
    if parar_instancia(quieto=True):
        iniciar_em_segundo_plano([])
        print("Painel reiniciado.")
    return 0


def _log_em_arquivo():
    if sys.stderr.isatty():
        return
    os.makedirs(C.CACHE_DIR, exist_ok=True)
    caminho = os.path.join(C.CACHE_DIR, "manometros.log")
    try:
        if os.path.getsize(caminho) > 512 * 1024:
            os.replace(caminho, caminho + ".1")
    except OSError:
        pass
    fh = open(caminho, "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stderr = fh
    print(f"\n--- iniciado {time.strftime('%Y-%m-%d %H:%M:%S')} (versão {__version__}) ---")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="manometros", description="Painel de manômetros para Linux.")
    ap.add_argument("--janela", action="store_true", help="abre como janela comum (para testar)")
    ap.add_argument("--demo", action="store_true", help="usa dados inventados que variam (para ver o visual)")
    ap.add_argument("--layout", choices=["janela", "faixa", "inteiro"], help="sobrepõe o modo do config")
    ap.add_argument("--monitor", help="sobrepõe o monitor do config (número ou parte do nome)")
    ap.add_argument("--listar", action="store_true", help="mostra monitores, sensores e ventoinhas e sai")
    ap.add_argument("--png", metavar="ARQUIVO", help="salva uma imagem do painel e sai")
    ap.add_argument("--largura", type=int, help="largura da imagem do --png")
    ap.add_argument("--parar", action="store_true", help="encerra o painel que está rodando")
    ap.add_argument("--reiniciar", action="store_true", help="encerra e abre de novo em segundo plano")
    ap.add_argument("--atualizar", action="store_true", help="procura e instala uma versão nova")
    ap.add_argument("--versao", action="version", version=f"Manômetros {__version__}")
    ap.add_argument("--config", help="usa outro arquivo de configuração")
    args = ap.parse_args(argv)

    cfg = C.carregar(args.config)
    if args.monitor:
        cfg["monitor"] = args.monitor

    if args.listar:
        listar(cfg)
        return 0
    if args.png:
        png(cfg, args)
        return 0
    if args.atualizar:
        return atualizar_terminal(cfg)
    if args.parar or args.reiniciar:
        parar_instancia()
        if args.parar:
            return 0
        iniciar_em_segundo_plano([a for a in argv if a != "--reiniciar"])
        print("Painel reiniciado em segundo plano.")
        return 0

    if not Gtk.init_check(sys.argv[:1])[0]:
        print("Não encontrei a sessão gráfica. Rode o painel num terminal aberto dentro da sua área de trabalho.")
        return 1

    trava = None
    if not args.janela:
        trava, pid = _trava()
        if trava is None:
            print(f"O painel já está rodando (pid {pid}). Use 'manometros --reiniciar' para reabrir.")
            return 1
        _log_em_arquivo()

    janela = Janela(cfg, args, argv)
    coletor = coleta.Coletor(cfg, lambda f: GLib.idle_add(janela.receber, f), demo=args.demo)

    def sair(*_):
        janela.encerrando = True
        coletor.parar.set()
        Gtk.main_quit()
        return False

    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, sair)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, sair)
    janela.connect("destroy", sair)
    janela.iniciar()
    coletor.start()
    Gtk.main()
    if trava:
        trava.close()
    return 0
