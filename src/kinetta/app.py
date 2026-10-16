"""Kinetta: grabador de pantalla para Linux (GTK4 + libadwaita).

La grabación real usa el portal ScreenCast (xdg-desktop-portal), PipeWire y
GStreamer; el motor vive en `recorder.py` y el selector de región en
`region.py`.

Barra flotante: usa gtk4-layer-shell (opcional) para quedar encima de las demás
ventanas en KDE Plasma, Sway, Hyprland, COSMIC, etc. En GNOME ese protocolo no
existe y la barra se muestra como una ventana normal.
"""
import os
import shutil
import subprocess
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Pango", "1.0")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango

from .recorder import (
    AUDIO_BITRATES,
    Recorder,
    RecorderCancelled,
    RecorderError,
    RecordingConfig,
    video_bitrate_kbps,
)
from .region import match_monitor, select_region

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell
except (ValueError, ImportError):
    LayerShell = None

APP_ID = "org.kinetta.Kinetta"
APP_NAME = "Kinetta"
APP_VERSION = "0.3.0"
APP_WEBSITE = "https://github.com/22460329-hue/kinetta"
BAR_TITLE = "Kinetta Bar"  # título único: Hyprland lo usa para sus reglas
BAR_RADIUS = 28  # radio de la píldora; Hyprland lo reutiliza para el hueco negro

MODES = [
    ("pantalla", "view-fullscreen-symbolic", "Pantalla", "Se grabará todo el monitor que elijas."),
    ("ventana", "focus-windows-symbolic", "Ventana", "Elegirás la ventana al empezar a grabar."),
    ("region", "edit-select-all-symbolic", "Región", "Dibujarás el área al empezar a grabar."),
]
FORMATS = [("MP4 (H.264)", "mp4"), ("WebM (VP9)", "webm"), ("GIF animado", "gif")]
FPS_OPTIONS = ["15", "24", "30", "60"]
QUALITY_OPTIONS = [
    "Baja (archivo pequeño)",
    "Media",
    "Alta (recomendada)",
    "Máxima (mejor calidad)",
]
SHORTCUTS = [
    ("<Ctrl>R", "Empezar o detener la grabación"),
    ("<Ctrl>Q", "Cerrar Kinetta"),
]

CSS = """
/* Botones propios: algunos temas generados desde el fondo de pantalla
   (HyDE/Wallbash) dejan los botones sin contraste en gtk.css, así que Kinetta
   define sus botones aquí. */
.pill { background-color: alpha(@window_fg_color, 0.10); color: @window_fg_color; box-shadow: none; }
.pill:hover { background-color: alpha(@window_fg_color, 0.16); }
.circular { background-color: alpha(@window_fg_color, 0.10); color: @window_fg_color; box-shadow: none; }
.circular:hover { background-color: alpha(@window_fg_color, 0.16); }
button.flat { background-color: transparent; background-image: none; box-shadow: none; color: @window_fg_color; }
button.flat:hover { background-color: alpha(@window_fg_color, 0.08); }
toast button { background-color: transparent; background-image: none; box-shadow: none; color: @accent_color; }
toast button:hover { background-color: alpha(@accent_bg_color, 0.15); }
window label { background-color: transparent; background-image: none; }
button.app-version { background-color: transparent; background-image: none; box-shadow: none; color: @accent_color; }
button.app-version:hover { background-color: alpha(@accent_bg_color, 0.15); }
/* Entradas y controles numéricos (temas rotos los pintan blancos con texto blanco). */
entry, spinbutton { background-color: alpha(@window_fg_color, 0.08); background-image: none;
    box-shadow: none; color: @window_fg_color; }
entry > text, spinbutton > text { background-color: transparent; background-image: none;
    box-shadow: none; color: @window_fg_color; }
spinbutton > button { background-color: alpha(@window_fg_color, 0.10); background-image: none;
    box-shadow: none; color: @window_fg_color; border-radius: 9999px; }
spinbutton > button:hover { background-color: alpha(@window_fg_color, 0.16); }
/* Deslizadores (mismo problema: pista blanca por el tema roto). */
scale trough { background-color: alpha(@window_fg_color, 0.18); border-radius: 9999px;
    min-height: 6px; }
scale highlight { background-color: @accent_bg_color; background-image: none;
    border-radius: 9999px; }
scale fill { background-color: @accent_bg_color; }
scale slider { background-color: @window_fg_color; background-image: none; color: @window_fg_color;
    border: none; box-shadow: none; }
/* La capa oscura tras los diálogos también sale clara con esos temas. */
dimming { background-color: rgba(0, 0, 0, 0.4); }
.setup-card { padding: 10px; }
.setup-card .mode-button { padding: 14px 6px; border-radius: 12px;
    background-color: transparent; background-image: none; box-shadow: none;
    border: 1px solid transparent; }
.setup-card .mode-button:hover { background-color: alpha(@window_fg_color, 0.07); }
.setup-card .mode-button:checked { background-color: alpha(@accent_bg_color, 0.30);
    color: @accent_color; border-color: alpha(@accent_color, 0.55); }
.setup-card .mode-button label { font-weight: 500; }
.record-button {
    background: #e01b24; color: #ffffff; font-weight: 700;
    min-height: 54px; border-radius: 27px; padding: 0 28px;
}
.record-button:hover { background: #ed333b; }
.stop-button {
    background: #e01b24; color: #ffffff;
    min-width: 76px; min-height: 76px; border-radius: 38px;
}
.stop-button:hover { background: #ed333b; }
.timer { font-size: 68px; font-weight: 300; font-feature-settings: "tnum"; }
.countdown { font-size: 140px; font-weight: 800; color: @accent_color; }
.rec-dot { color: #e01b24; font-size: 16px; }
.rec-dot.live { animation: pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.2; } 100% { opacity: 1; } }

window.floating-bar {
    background: transparent;
    color: @window_fg_color;
    border: none;
    box-shadow: none;
}
/* El fondo va en un contenedor con un marco transparente alrededor: si el
   compositor recorta 1 px al ocultar la barra, no se filtra nada. */
window.floating-bar .bar-pill {
    background: alpha(@window_bg_color, 0.97);
    border-radius: @BAR_RADIUS;
    padding: 8px 10px 8px 18px;
    box-shadow: inset 0 0 0 1px alpha(@window_fg_color, 0.18),
        0 3px 12px rgba(0, 0, 0, 0.45);
}
.bar-timer { font-size: 18px; font-weight: 500; font-feature-settings: "tnum"; min-width: 64px; }
.bar-size { font-size: 12px; color: alpha(@window_fg_color, 0.65); font-feature-settings: "tnum"; }
.bar-stop {
    background: #e01b24; color: #ffffff;
    min-width: 40px; min-height: 40px; border-radius: 20px; padding: 0;
}
.bar-stop:hover { background: #ed333b; }

window.region-overlay { background: transparent; }
.region-hint {
    background: alpha(@window_bg_color, 0.92);
    color: @window_fg_color;
    padding: 10px 20px;
    border-radius: 999px;
    box-shadow: 0 2px 10px rgba(0, 0, 0, 0.45);
}
"""
CSS = CSS.replace("@BAR_RADIUS", f"{BAR_RADIUS}px")


def format_time(seconds: int) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_bytes(size: int) -> str:
    mb = size / (1024 * 1024)
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB".replace(".", ",")
    if mb >= 100:
        return f"{mb:.0f} MB"
    return f"{mb:.1f} MB".replace(".", ",")


def format_mbps(kbps: int) -> str:
    text = f"{kbps / 1000:.1f}".rstrip("0").rstrip(".")
    return text.replace(".", ",") + " Mb/s"


def layer_shell_available() -> bool:
    return bool(LayerShell and LayerShell.is_supported())


def hyprland_available() -> bool:
    """¿Corremos bajo Hyprland con hyprctl a mano?"""
    return bool(os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")) and shutil.which("hyprctl") is not None


_hypr_rules_applied = False


def apply_hyprland_rules() -> None:
    """Pide a Hyprland (una vez por ejecución) que la barra flote, se vea en
    todos los escritorios y no salga en las grabaciones: Hyprland dibuja un
    rectángulo negro en su lugar. Las reglas viven solo en la sesión."""
    global _hypr_rules_applied
    if _hypr_rules_applied or not hyprland_available():
        return
    _hypr_rules_applied = True
    # `rounding` da forma de cápsula al rectángulo negro que Hyprland deja en
    # la grabación, para que copie la silueta redondeada de la barra.
    window_rule = (
        f"decorate off, float on, pin on, no_initial_focus on, no_screen_share on, "
        f"rounding {BAR_RADIUS}, match:title ^({BAR_TITLE})$"
    )
    layer_rule = "no_screen_share on, match:namespace ^(kinetta-bar)$"
    for keyword, rule in (("windowrule", window_rule), ("layerrule", layer_rule)):
        try:
            subprocess.run(
                ["hyprctl", "keyword", keyword, rule],
                capture_output=True,
                timeout=3,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            pass


def patch_spin_icons(spin: Adw.SpinRow) -> None:
    """Adw.SpinRow pinta sus botones con los iconos value-increase/decrease
    -symbolic. Algunos temas de iconos (p. ej. Tela) los definen con
    currentColor y GTK no los dibuja, dejando círculos vacíos: los cambiamos
    por etiquetas de texto, que siempre se ven."""

    def find_spin_button(widget):
        child = widget.get_first_child()
        while child:
            if isinstance(child, Gtk.SpinButton):
                return child
            found = find_spin_button(child)
            if found is not None:
                return found
            child = child.get_next_sibling()
        return None

    spin_button = find_spin_button(spin)
    if spin_button is None:
        return
    child = spin_button.get_first_child()
    while child:
        if isinstance(child, Gtk.Button) and isinstance(child.get_child(), Gtk.Image):
            icon = child.get_child().get_icon_name() or ""
            child.set_child(Gtk.Label(label="−" if "decrease" in icon else "+"))
        child = child.get_next_sibling()


class FloatingBar(Gtk.Window):
    """Barra pequeña con temporizador, pausa y detener.

    Con layer-shell queda siempre encima y centrada abajo. Sin él, es una
    ventana sin bordes que se puede arrastrar.
    """

    def __init__(self, app, on_pause, on_stop, monitor=None, opacity=1.0):
        super().__init__(application=app, title=BAR_TITLE, decorated=False, resizable=False)
        self.add_css_class("floating-bar")
        self.set_opacity(max(0.2, min(1.0, opacity)))

        if layer_shell_available():
            LayerShell.init_for_window(self)
            LayerShell.set_namespace(self, "kinetta-bar")
            LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
            LayerShell.set_anchor(self, LayerShell.Edge.BOTTOM, True)
            LayerShell.set_margin(self, LayerShell.Edge.BOTTOM, 28)
            LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)
            if monitor is not None:
                LayerShell.set_monitor(self, monitor)

        box = Gtk.Box(spacing=10, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)
        box.add_css_class("bar-pill")

        self.dot = Gtk.Label(label="●")
        self.dot.add_css_class("rec-dot")
        self.dot.add_css_class("live")
        box.append(self.dot)

        self.timer = Gtk.Label(label="00:00")
        self.timer.add_css_class("bar-timer")
        box.append(self.timer)

        self.size_label = Gtk.Label(label="")
        self.size_label.add_css_class("bar-size")
        box.append(self.size_label)

        self.pause_button = Gtk.Button(icon_name="media-playback-pause-symbolic", valign=Gtk.Align.CENTER)
        self.pause_button.add_css_class("circular")
        self.pause_button.set_tooltip_text("Pausar")
        self.pause_button.connect("clicked", lambda *_: on_pause())
        box.append(self.pause_button)

        self.stop_button = Gtk.Button(icon_name="media-playback-stop-symbolic", valign=Gtk.Align.CENTER)
        self.stop_button.add_css_class("bar-stop")
        self.stop_button.set_tooltip_text("Detener y guardar")
        self.stop_button.connect("clicked", lambda *_: on_stop())
        box.append(self.stop_button)

        handle = Gtk.WindowHandle()
        handle.set_child(box)
        self.set_child(handle)

    def set_time(self, text: str):
        self.timer.set_label(text)

    def set_size(self, text: str):
        self.size_label.set_label(text)

    def set_paused(self, paused: bool):
        if paused:
            self.dot.remove_css_class("live")
            self.pause_button.set_icon_name("media-playback-start-symbolic")
            self.pause_button.set_tooltip_text("Continuar")
        else:
            self.dot.add_css_class("live")
            self.pause_button.set_icon_name("media-playback-pause-symbolic")
            self.pause_button.set_tooltip_text("Pausar")

    def set_saving(self):
        """Mientras se finaliza el archivo (o se convierte el GIF)."""
        self.timer.set_label("Guardando…")
        self.pause_button.set_sensitive(False)
        self.stop_button.set_sensitive(False)
        self.dot.remove_css_class("live")


class MainWindow(Adw.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME, default_width=460, default_height=760)
        self.recorder = Recorder(on_error=self._on_recorder_error)
        self.mode = "pantalla"
        videos = GLib.get_user_special_dir(GLib.UserDirectory.DIRECTORY_VIDEOS)
        self.output_dir = videos or os.path.expanduser("~")
        self.monitors = list(Gdk.Display.get_default().get_monitors())
        self.bar = None

        self._elapsed_before_pause = 0.0
        self._segment_start = 0.0
        self._tick_id = 0
        self._countdown_id = 0
        self._countdown_left = 0
        self._last_file = None
        self._busy = False
        self._stopping = False

        self._build_actions()

        self.toasts = Adw.ToastOverlay()
        toolbar = Adw.ToolbarView()
        self.header = Adw.HeaderBar()
        self.title_widget = Adw.WindowTitle(title=APP_NAME, subtitle="Listo para grabar")
        self.header.set_title_widget(self.title_widget)
        self.header.pack_end(self._build_menu())
        toolbar.add_top_bar(self.header)

        self.stack = Gtk.Stack(transition_type=Gtk.StackTransitionType.CROSSFADE)
        self.stack.add_named(self._build_setup_page(), "setup")
        self.stack.add_named(self._build_countdown_page(), "countdown")
        self.stack.add_named(self._build_recording_page(), "recording")
        toolbar.set_content(self.stack)

        self.toasts.set_child(toolbar)
        self.set_content(self.toasts)

    # ------------------------------------------------------------------ acciones
    def _build_actions(self):
        toggle = Gio.SimpleAction.new("toggle-record", None)
        toggle.connect("activate", lambda *_: self._toggle_record())
        self.add_action(toggle)

        open_folder = Gio.SimpleAction.new("open-folder", None)
        open_folder.connect("activate", lambda *_: self._open_folder())
        self.add_action(open_folder)

    def _build_menu(self):
        menu = Gio.Menu()
        menu.append("Abrir carpeta de grabaciones", "win.open-folder")
        menu.append("Atajos de teclado", "app.shortcuts")
        menu.append("Acerca de " + APP_NAME, "app.about")
        button = Gtk.MenuButton(icon_name="open-menu-symbolic", menu_model=menu, primary=True)
        button.set_tooltip_text("Menú principal")
        return button

    # ------------------------------------------------------------ página: ajustes
    def _build_setup_page(self):
        page = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        scroller = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        clamp = Adw.Clamp(maximum_size=520, margin_top=12, margin_bottom=12, margin_start=16, margin_end=16)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)

        # --- Qué grabar
        mode_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        caption = Gtk.Label(label="¿Qué quieres grabar?", xalign=0)
        caption.add_css_class("heading")
        mode_box.append(caption)
        card = Gtk.Box(margin_top=2)
        card.add_css_class("card")
        card.add_css_class("setup-card")
        row = Gtk.Box(spacing=6, homogeneous=True, hexpand=True)
        first = None
        self.mode_buttons = {}
        for key, icon, label, _hint in MODES:
            btn = Gtk.ToggleButton()
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            img = Gtk.Image.new_from_icon_name(icon)
            img.set_pixel_size(28)
            inner.append(img)
            inner.append(Gtk.Label(label=label))
            btn.set_child(inner)
            btn.add_css_class("mode-button")
            if first is None:
                first = btn
                btn.set_active(True)
            else:
                btn.set_group(first)
            btn.connect("toggled", self._on_mode_toggled, key)
            self.mode_buttons[key] = btn
            row.append(btn)
        card.append(row)
        mode_box.append(card)
        self.mode_hint = Gtk.Label(label=MODES[0][3], xalign=0)
        self.mode_hint.add_css_class("dim-label")
        self.mode_hint.add_css_class("caption")
        self.mode_hint.set_margin_start(4)
        mode_box.append(self.mode_hint)
        content.append(mode_box)

        # --- Audio y cursor
        audio = Adw.PreferencesGroup(title="Sonido y cursor")
        self.row_system = Adw.SwitchRow(title="Audio del sistema", subtitle="Sonidos de las aplicaciones")
        self.row_system.set_active(True)
        self.row_mic = Adw.SwitchRow(title="Micrófono", subtitle="Tu voz mientras grabas")
        self.row_cursor = Adw.SwitchRow(title="Mostrar el cursor")
        self.row_cursor.set_active(True)
        for r in (self.row_system, self.row_mic, self.row_cursor):
            audio.add(r)
        self.row_system.connect("notify::active", lambda *_: self._update_quality_hint())
        self.row_mic.connect("notify::active", lambda *_: self._update_quality_hint())
        content.append(audio)

        # --- Calidad
        quality = Adw.PreferencesGroup(title="Calidad")
        self.row_format = Adw.ComboRow(title="Formato", model=Gtk.StringList.new([f[0] for f in FORMATS]))
        self.row_format.connect("notify::selected", self._on_format_changed)
        self.row_fps = Adw.ComboRow(title="Fotogramas por segundo", model=Gtk.StringList.new(FPS_OPTIONS))
        self.row_fps.set_selected(2)
        self.row_quality = Adw.ComboRow(title="Calidad de video", model=Gtk.StringList.new(QUALITY_OPTIONS))
        self.row_quality.set_selected(2)
        self.row_quality.connect("notify::selected", lambda *_: self._update_quality_hint())
        self.row_resolution = Adw.ActionRow(title="Resolución de captura")
        self.row_resolution.set_subtitle(self._resolution_summary())
        for r in (self.row_format, self.row_fps, self.row_quality, self.row_resolution):
            quality.add(r)
        content.append(quality)

        # --- Controles flotantes
        self.group_controls = Adw.PreferencesGroup(title="Controles")
        self.row_bar = Adw.SwitchRow(
            title="Barra flotante", subtitle="Pausa y detener siempre a la vista mientras grabas"
        )
        self.row_bar.set_active(True)
        self.row_bar.connect("notify::active", lambda *_: self._update_bar_hint())
        self.group_controls.add(self.row_bar)

        self.bar_opacity = 100
        self.row_opacity = Adw.ActionRow(title="Opacidad de la barra")
        self.scale_opacity = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 30, 100, 5)
        self.scale_opacity.set_value(100)
        self.scale_opacity.set_size_request(180, -1)
        self.scale_opacity.set_valign(Gtk.Align.CENTER)
        self.scale_opacity.set_draw_value(False)
        self.scale_opacity.set_tooltip_text("Transparencia de la barra flotante")
        self.scale_opacity.connect("value-changed", self._on_opacity_changed)
        self.opacity_label = Gtk.Label(label="100 %", valign=Gtk.Align.CENTER)
        self.opacity_label.add_css_class("dim-label")
        self.opacity_label.set_width_chars(5)
        self.row_opacity.add_suffix(self.scale_opacity)
        self.row_opacity.add_suffix(self.opacity_label)
        self.group_controls.add(self.row_opacity)

        names = []
        for i, m in enumerate(self.monitors):
            connector = m.get_connector() or f"Monitor {i + 1}"
            model = m.get_model()
            names.append(f"{connector} ({model})" if model else connector)
        self.row_bar_monitor = Adw.ComboRow(
            title="Mostrar la barra en",
            subtitle="Elige un monitor distinto al que grabas",
            model=Gtk.StringList.new(names or ["Predeterminado"]),
        )
        self.row_bar_monitor.set_visible(layer_shell_available() and len(self.monitors) > 1)
        self.group_controls.add(self.row_bar_monitor)
        content.append(self.group_controls)

        # --- Salida
        output = Adw.PreferencesGroup(title="Guardado")
        self.row_folder = Adw.ActionRow(title="Guardar en", subtitle=self.output_dir)
        self.row_folder.set_subtitle_lines(1)
        choose = Gtk.Button(icon_name="folder-open-symbolic", valign=Gtk.Align.CENTER)
        choose.add_css_class("flat")
        choose.set_tooltip_text("Elegir carpeta")
        choose.connect("clicked", self._choose_folder)
        self.row_folder.add_suffix(choose)
        self.row_folder.set_activatable_widget(choose)
        self.row_countdown = Adw.SpinRow.new_with_range(0, 10, 1)
        self.row_countdown.set_title("Cuenta regresiva")
        self.row_countdown.set_subtitle("Segundos antes de empezar")
        self.row_countdown.set_value(3)
        patch_spin_icons(self.row_countdown)
        self.row_name = Adw.EntryRow(title="Nombre del archivo")
        self.row_name.set_text("Grabacion")
        self.row_name.set_show_apply_button(False)
        self.row_open = Adw.SwitchRow(
            title="Abrir el video al guardar", subtitle="Se abre con tu reproductor al terminar"
        )
        output.add(self.row_folder)
        output.add(self.row_name)
        output.add(self.row_countdown)
        output.add(self.row_open)
        content.append(output)

        clamp.set_child(content)
        scroller.set_child(clamp)
        page.append(scroller)

        # --- Botón principal fijo abajo
        bar = Adw.Clamp(maximum_size=520, margin_top=8, margin_bottom=16, margin_start=16, margin_end=16)
        self.record_button = Gtk.Button()
        inner = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        inner.append(Gtk.Image.new_from_icon_name("media-record-symbolic"))
        self.record_label = Gtk.Label(label="Empezar a grabar")
        inner.append(self.record_label)
        self.record_button.set_child(inner)
        self.record_button.add_css_class("record-button")
        self.record_button.add_css_class("pill")
        self.record_button.set_tooltip_text("Empezar o detener la grabación  (Ctrl+R)")
        self.record_button.connect("clicked", lambda *_: self._toggle_record())
        bar.set_child(self.record_button)
        page.append(bar)

        self._update_bar_hint()
        self._update_quality_hint()
        return page

    def _estimated_capture_size(self):
        """Resolución aproximada de la captura: el monitor más grande, en píxeles físicos."""
        best = None
        for monitor in self.monitors:
            geometry = monitor.get_geometry()
            pixels = geometry.width * geometry.height
            if best is None or pixels > best[0]:
                best = (pixels, geometry.width, geometry.height, monitor.get_scale())
        if best is None:
            return (1920, 1080)
        scale = best[3] or 1.0
        return (int(best[1] * scale + 0.5), int(best[2] * scale + 0.5))

    def _resolution_summary(self):
        width, height = self._estimated_capture_size()
        if len(self.monitors) > 1:
            return f"hasta {width}×{height} · la del monitor que elijas"
        return f"{width}×{height} · la del monitor elegido"

    def _update_quality_hint(self):
        """Subraya la calidad estimada (bitrate y tamaño por minuto)."""
        if FORMATS[self.row_format.get_selected()][1] == "gif":
            self.row_quality.set_subtitle("El GIF se convierte al final: máx. 960 px de ancho y 15 fps")
            return
        quality = self.row_quality.get_selected()
        kbps = video_bitrate_kbps(self._estimated_capture_size(), quality)
        if self.row_system.get_active() or self.row_mic.get_active():
            kbps += AUDIO_BITRATES.get(quality, 192000) // 1000
        per_minute = kbps * 60 / 8 / 1000
        self.row_quality.set_subtitle(
            f"≈ {format_mbps(kbps)} · unos {per_minute:.0f} MB por minuto"
        )

    def _set_state(self, text):
        self.title_widget.set_subtitle(text)

    def _on_opacity_changed(self, scale):
        self.bar_opacity = int(scale.get_value())
        self.opacity_label.set_label(f"{self.bar_opacity} %")

    def _update_bar_hint(self):
        """Explica en lenguaje simple si los controles saldrán en el video."""
        self.row_opacity.set_sensitive(self.row_bar.get_active())
        if not self.row_bar.get_active():
            text = "Sin barra, usa la ventana de Kinetta para detener la grabación."
        elif self.mode == "ventana":
            text = "Al grabar una ventana, los controles no salen en el video."
        elif hyprland_available():
            text = "La barra no sale en el video y sigue visible en todos los escritorios."
        elif layer_shell_available() and len(self.monitors) > 1:
            text = "Para que la barra no salga en el video, muéstrala en un monitor que no grabes."
        else:
            text = (
                "Con un solo monitor, la barra saldrá en el video si grabas pantalla o región. "
                "Usa el modo Ventana para evitarlo."
            )
        if not layer_shell_available() and not hyprland_available():
            text += " Tu escritorio no permite fijar la barra encima de otras ventanas."
        self.group_controls.set_description(text)

    # --------------------------------------------------------- página: cuenta atrás
    def _build_countdown_page(self):
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=12, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER
        )
        self.countdown_label = Gtk.Label(label="3")
        self.countdown_label.add_css_class("countdown")
        box.append(self.countdown_label)
        box.append(Gtk.Label(label="La grabación empieza en un momento"))
        self.countdown_hint = Gtk.Label(label="")
        self.countdown_hint.add_css_class("dim-label")
        self.countdown_hint.add_css_class("caption")
        box.append(self.countdown_hint)
        cancel = Gtk.Button(label="Cancelar", halign=Gtk.Align.CENTER, margin_top=12)
        cancel.add_css_class("pill")
        cancel.connect("clicked", lambda *_: self._cancel_countdown())
        box.append(cancel)
        return box

    # --------------------------------------------------------- página: grabando
    # Solo se usa cuando la barra flotante está desactivada.
    def _build_recording_page(self):
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=8, valign=Gtk.Align.CENTER, halign=Gtk.Align.CENTER
        )

        status = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        self.rec_dot = Gtk.Label(label="●")
        self.rec_dot.add_css_class("rec-dot")
        self.rec_status = Gtk.Label(label="Grabando")
        self.rec_status.add_css_class("heading")
        status.append(self.rec_dot)
        status.append(self.rec_status)
        box.append(status)

        self.timer_label = Gtk.Label(label="00:00")
        self.timer_label.add_css_class("timer")
        box.append(self.timer_label)

        self.size_label = Gtk.Label(label="")
        self.size_label.add_css_class("dim-label")
        box.append(self.size_label)

        self.rec_info = Gtk.Label(label="")
        self.rec_info.add_css_class("dim-label")
        box.append(self.rec_info)

        self.file_label = Gtk.Label(label="")
        self.file_label.add_css_class("caption")
        self.file_label.add_css_class("dim-label")
        self.file_label.set_ellipsize(Pango.EllipsizeMode.END)
        self.file_label.set_max_width_chars(44)
        box.append(self.file_label)

        controls = Gtk.Box(spacing=24, halign=Gtk.Align.CENTER, margin_top=36)
        self.pause_button = Gtk.Button(icon_name="media-playback-pause-symbolic", valign=Gtk.Align.CENTER)
        self.pause_button.add_css_class("circular")
        self.pause_button.set_size_request(56, 56)
        self.pause_button.set_tooltip_text("Pausar")
        self.pause_button.connect("clicked", lambda *_: self._toggle_pause())
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic")
        stop.add_css_class("stop-button")
        stop.set_tooltip_text("Detener y guardar  (Ctrl+R)")
        stop.connect("clicked", lambda *_: self._stop_recording())
        controls.append(self.pause_button)
        controls.append(stop)
        box.append(controls)
        return box

    # ------------------------------------------------------------------ eventos
    def _on_mode_toggled(self, button, key):
        if button.get_active():
            self.mode = key
            self.mode_hint.set_label(next(m[3] for m in MODES if m[0] == key))
            if hasattr(self, "group_controls"):
                self._update_bar_hint()

    def _on_format_changed(self, *_):
        is_gif = FORMATS[self.row_format.get_selected()][1] == "gif"
        for r in (self.row_system, self.row_mic):
            r.set_sensitive(not is_gif)
            if is_gif:
                r.set_active(False)
        self._update_quality_hint()

    def _choose_folder(self, *_):
        dialog = Gtk.FileDialog(title="Elegir carpeta de guardado")
        dialog.select_folder(self, None, self._on_folder_chosen)

    def _on_folder_chosen(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # el usuario canceló
        self.output_dir = folder.get_path()
        self.row_folder.set_subtitle(self.output_dir)

    def _read_config(self) -> RecordingConfig:
        monitor = None
        if self.row_bar_monitor.get_visible() and self.monitors:
            monitor = self.monitors[self.row_bar_monitor.get_selected()]
        return RecordingConfig(
            mode=self.mode,
            system_audio=self.row_system.get_active(),
            microphone=self.row_mic.get_active(),
            show_cursor=self.row_cursor.get_active(),
            format_ext=FORMATS[self.row_format.get_selected()][1],
            fps=int(FPS_OPTIONS[self.row_fps.get_selected()]),
            quality=self.row_quality.get_selected(),
            countdown=int(self.row_countdown.get_value()),
            output_dir=self.output_dir,
            name_prefix=self.row_name.get_text().strip() or "Grabacion",
            floating_bar=self.row_bar.get_active(),
            bar_monitor=monitor,
        )

    # --------------------------------------------------------- flujo de grabación
    def _toggle_record(self):
        page = self.stack.get_visible_child_name()
        if page == "setup":
            self._begin()
        elif page == "recording":
            self._stop_recording()
        elif page == "countdown":
            self._cancel_countdown()

    def _set_setup_busy(self, busy, label=None):
        if busy:
            self.record_label.set_label(label or "Preparando…")
            self.record_button.set_sensitive(False)
            self.stack.set_sensitive(False)
        else:
            self.record_label.set_label("Empezar a grabar")
            self.record_button.set_sensitive(True)
            self.stack.set_sensitive(True)

    def _begin(self):
        """Elige la fuente en el portal (y la región) antes de la cuenta atrás."""
        if self._busy:
            return
        self._busy = True
        self._set_setup_busy(True, "Eligiendo qué grabar…")
        self._set_state("Eligiendo qué grabar…")
        try:
            self.config = self._read_config()
            try:
                info = self.recorder.prepare(self.config)
            except RecorderCancelled:
                return
            except RecorderError as exc:
                self._error_toast(str(exc))
                return

            if self.config.mode == "region":
                size = info.get("size") or ()
                if not size:
                    self.recorder.cancel()
                    self._error_toast(
                        "El portal no informó el tamaño de la pantalla; no se puede recortar la región"
                    )
                    return
                self._set_setup_busy(True, "Elige la región…")
                monitor = match_monitor(self.monitors, info.get("position") or (), size)
                crop = select_region(self.get_application(), monitor, size)
                if crop is None:
                    self.recorder.cancel()
                    return
                self.config.crop = crop
        finally:
            self._busy = False
            self._set_setup_busy(False)
            self._set_state("Listo para grabar")

        if self.config.countdown > 0:
            self._countdown_left = self.config.countdown
            self.countdown_label.set_label(str(self._countdown_left))
            mode_name = next(m[2] for m in MODES if m[0] == self.config.mode)
            self.countdown_hint.set_label(
                f"{mode_name} · {self.config.format_ext.upper()} · {self.config.fps} fps"
            )
            self.stack.set_visible_child_name("countdown")
            self._set_state("Cuenta atrás")
            self._countdown_id = GLib.timeout_add_seconds(1, self._countdown_tick)
        else:
            self._start_recording()

    def _countdown_tick(self):
        self._countdown_left -= 1
        if self._countdown_left <= 0:
            self._countdown_id = 0
            self._start_recording()
            return GLib.SOURCE_REMOVE
        self.countdown_label.set_label(str(self._countdown_left))
        return GLib.SOURCE_CONTINUE

    def _cancel_countdown(self):
        if self._countdown_id:
            GLib.source_remove(self._countdown_id)
            self._countdown_id = 0
        self.recorder.cancel()
        self.stack.set_visible_child_name("setup")
        self._set_state("Listo para grabar")

    def _start_recording(self):
        try:
            if self.config.format_ext == "gif" and shutil.which("ffmpeg") is None:
                raise RecorderError("Para exportar GIF se necesita ffmpeg instalado")
            path = self.recorder.start(self.config)
        except RecorderError as exc:
            self.recorder.cancel()
            self.stack.set_visible_child_name("setup")
            self._error_toast(str(exc))
            return

        self._elapsed_before_pause = 0.0
        self._segment_start = time.monotonic()

        # Reiniciar la pantalla de grabación integrada
        self.timer_label.set_label("00:00")
        self.size_label.set_label("")
        self.file_label.set_label(os.path.basename(path))
        self.rec_status.set_label("Grabando")
        self.rec_dot.add_css_class("live")
        self.pause_button.set_icon_name("media-playback-pause-symbolic")
        self.pause_button.set_tooltip_text("Pausar")
        mode_name = next(m[2] for m in MODES if m[0] == self.config.mode)
        self.rec_info.set_label(f"{mode_name} · {self.config.format_ext.upper()} · {self.config.fps} fps")
        self._set_state("Grabando")

        if self.config.floating_bar:
            # La ventana principal se oculta y solo queda la barra pequeña.
            apply_hyprland_rules()  # antes de crearla: Hyprland aplica reglas al mapear
            self.bar = FloatingBar(
                self.get_application(),
                on_pause=self._toggle_pause,
                on_stop=self._stop_recording,
                monitor=self.config.bar_monitor,
                opacity=self.bar_opacity / 100,
            )
            self.bar.present()
            self.set_visible(False)
        else:
            self.stack.set_visible_child_name("recording")

        self._tick_id = GLib.timeout_add(250, self._tick)

    def _elapsed(self) -> float:
        if self.recorder.paused:
            return self._elapsed_before_pause
        return self._elapsed_before_pause + (time.monotonic() - self._segment_start)

    def _tick(self):
        text = format_time(self._elapsed())
        self.timer_label.set_label(text)
        size = self.recorder.current_size()
        size_text = format_bytes(size) if size > 0 else ""
        self.size_label.set_label(size_text)
        if self.bar:
            self.bar.set_time(text)
            self.bar.set_size(size_text)
        return GLib.SOURCE_CONTINUE

    def _toggle_pause(self):
        if self.bar is None and self.stack.get_visible_child_name() != "recording":
            return
        if self.recorder.paused:
            self.recorder.resume()
            self._segment_start = time.monotonic()
            self.rec_status.set_label("Grabando")
            self.rec_dot.add_css_class("live")
            self.pause_button.set_icon_name("media-playback-pause-symbolic")
            self.pause_button.set_tooltip_text("Pausar")
            self._set_state("Grabando")
            if self.bar:
                self.bar.set_paused(False)
        else:
            self._elapsed_before_pause = self._elapsed()
            self.recorder.pause()
            self.rec_status.set_label("En pausa")
            self.rec_dot.remove_css_class("live")
            self.pause_button.set_icon_name("media-playback-start-symbolic")
            self.pause_button.set_tooltip_text("Continuar")
            self._set_state("En pausa")
            if self.bar:
                self.bar.set_paused(True)

    def _on_recorder_error(self, message):
        """El pipeline falló mientras grababa (bus de GStreamer)."""
        if self._stopping:
            return
        self._stop_recording(error=message)

    def _stop_recording(self, error=None):
        if self._stopping:
            return
        self._stopping = True
        self._set_state("Guardando…")
        try:
            if self._tick_id:
                GLib.source_remove(self._tick_id)
                self._tick_id = 0
            if self.bar is None:
                self.rec_status.set_label("Guardando…")
            else:
                self.bar.set_saving()
            self.rec_dot.remove_css_class("live")

            try:
                path = self.recorder.stop()
                failure = self.recorder.error_message
            except RecorderError as exc:
                path = None
                failure = str(exc)

            if self.bar:
                self.bar.destroy()
                self.bar = None
            self.set_visible(True)
            self.present()
            self.stack.set_visible_child_name("setup")
            self._set_state("Listo para grabar")
            self._last_file = path

            if path:
                label = "GIF guardado" if path.endswith(".gif") else "Grabación guardada"
                toast = Adw.Toast(title=label, button_label="Abrir carpeta", action_name="win.open-folder")
                self.toasts.add_toast(toast)
                if self.row_open.get_active():
                    Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(path).get_uri(), None)
            else:
                reason = failure or error or "error desconocido"
                self.toasts.add_toast(Adw.Toast(title=f"La grabación falló: {reason}"))
        finally:
            self._stopping = False

    def _error_toast(self, message):
        self.toasts.add_toast(Adw.Toast(title=message))

    def _open_folder(self):
        folder = os.path.dirname(self._last_file) if self._last_file else self.output_dir
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(folder).get_uri(), None)


class KinettaApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self._window = None
        self.connect("activate", self._on_activate)
        self.connect("shutdown", self._on_shutdown)

        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda *_: self.quit())
        self.add_action(quit_action)
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", lambda *_: self._show_about())
        self.add_action(about_action)

        shortcuts_action = Gio.SimpleAction.new("shortcuts", None)
        shortcuts_action.connect("activate", lambda *_: self._show_shortcuts())
        self.add_action(shortcuts_action)

        self.set_accels_for_action("app.quit", ["<Ctrl>Q"])
        self.set_accels_for_action("app.shortcuts", ["<Ctrl>question"])
        self.set_accels_for_action("win.toggle-record", ["<Ctrl>R"])

    def _on_activate(self, app):
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):  # GTK >= 4.12
            provider.load_from_string(CSS)
        else:
            provider.load_from_data(CSS.encode())
        # Prioridad por encima del CSS de usuario (~/.config/gtk-4.0/gtk.css): con
        # temas generados desde el fondo de pantalla los botones quedan ilegibles
        # y la interfaz debe verse como está diseñada.
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_USER + 1
        )
        window = self.props.active_window or MainWindow(app)
        self._window = window
        window.present()

    def _on_shutdown(self, _app):
        if self._window is not None:
            self._window.recorder.cancel()

    def _show_about(self, *_):
        if hasattr(Adw, "AboutDialog"):  # libadwaita >= 1.5
            about = Adw.AboutDialog(
                application_name=APP_NAME,
                application_icon=APP_ID,
                developer_name="Proyecto Kinetta",
                version=APP_VERSION,
                website=APP_WEBSITE,
                comments="Grabador de pantalla sencillo para Linux.",
                license_type=Gtk.License.GPL_3_0,
            )
            about.present(self.props.active_window)
        else:
            about = Adw.AboutWindow(
                transient_for=self.props.active_window,
                application_name=APP_NAME,
                application_icon=APP_ID,
                developer_name="Proyecto Kinetta",
                version=APP_VERSION,
                website=APP_WEBSITE,
                comments="Grabador de pantalla sencillo para Linux.",
                license_type=Gtk.License.GPL_3_0,
            )
            about.present()

    def _show_shortcuts(self, *_):
        window = self.props.active_window
        if hasattr(Adw, "ShortcutsItem"):
            dialog = Adw.ShortcutsDialog()
            if hasattr(dialog, "set_follows_content_size"):
                dialog.set_follows_content_size(True)  # sin espacio de sobra debajo
            section = Adw.ShortcutsSection(title="General")
            for accelerator, title in SHORTCUTS:
                section.add(Adw.ShortcutsItem(accelerator=accelerator, title=title))
            dialog.add(section)
            dialog.present(window)
            return
        dialog = Adw.MessageDialog(
            transient_for=window,
            heading="Atajos de teclado",
            body="\n".join(
                f"{title}: {accelerator.replace('<', '').replace('>', '+')}"
                for accelerator, title in SHORTCUTS
            ),
        )
        dialog.add_response("close", "Cerrar")
        dialog.present()


def main():
    return KinettaApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
