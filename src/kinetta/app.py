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
import sys
import time

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .recorder import Recorder, RecorderCancelled, RecorderError, RecordingConfig
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

MODES = [
    ("pantalla", "view-fullscreen-symbolic", "Pantalla", "Se grabará todo el monitor que elijas."),
    ("ventana", "focus-windows-symbolic", "Ventana", "Elegirás la ventana al empezar a grabar."),
    ("region", "edit-select-all-symbolic", "Región", "Dibujarás el área al empezar a grabar."),
]
FORMATS = [("MP4 (H.264)", "mp4"), ("WebM (VP9)", "webm"), ("GIF animado", "gif")]
FPS_OPTIONS = ["15", "24", "30", "60"]
QUALITY_OPTIONS = ["Baja (archivo pequeño)", "Media", "Alta (archivo grande)"]

CSS = """
.mode-button { padding: 16px 8px; border-radius: 14px; }
.mode-button:checked { background: alpha(@accent_bg_color, 0.18); color: @accent_color; }
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
.countdown { font-size: 140px; font-weight: 800; }
.rec-dot { color: #e01b24; font-size: 16px; }
.rec-dot.live { animation: pulse 1.2s ease-in-out infinite; }
@keyframes pulse { 0% { opacity: 1; } 50% { opacity: 0.2; } 100% { opacity: 1; } }

window.floating-bar {
    background: alpha(@window_bg_color, 0.97);
    color: @window_fg_color;
    border-radius: 32px;
    border: 1px solid alpha(@window_fg_color, 0.18);
}
.bar-timer { font-size: 18px; font-weight: 500; font-feature-settings: "tnum"; min-width: 64px; }
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


def format_time(seconds: int) -> str:
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h:d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def layer_shell_available() -> bool:
    return bool(LayerShell and LayerShell.is_supported())


class FloatingBar(Gtk.Window):
    """Barra pequeña con temporizador, pausa y detener.

    Con layer-shell queda siempre encima y centrada abajo. Sin él, es una
    ventana sin bordes que se puede arrastrar.
    """

    def __init__(self, app, on_pause, on_stop, monitor=None):
        super().__init__(application=app, title=APP_NAME, decorated=False, resizable=False)
        self.add_css_class("floating-bar")

        if layer_shell_available():
            LayerShell.init_for_window(self)
            LayerShell.set_namespace(self, "kinetta-bar")
            LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
            LayerShell.set_anchor(self, LayerShell.Edge.BOTTOM, True)
            LayerShell.set_margin(self, LayerShell.Edge.BOTTOM, 28)
            LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.NONE)
            if monitor is not None:
                LayerShell.set_monitor(self, monitor)

        box = Gtk.Box(spacing=10, margin_top=8, margin_bottom=8, margin_start=18, margin_end=10)

        self.dot = Gtk.Label(label="●")
        self.dot.add_css_class("rec-dot")
        self.dot.add_css_class("live")
        box.append(self.dot)

        self.timer = Gtk.Label(label="00:00")
        self.timer.add_css_class("bar-timer")
        box.append(self.timer)

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
        self.header.set_title_widget(Adw.WindowTitle(title=APP_NAME, subtitle=""))
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
        row = Gtk.Box(spacing=10, homogeneous=True)
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
        mode_box.append(row)
        self.mode_hint = Gtk.Label(label=MODES[0][3], xalign=0)
        self.mode_hint.add_css_class("dim-label")
        self.mode_hint.add_css_class("caption")
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
        content.append(audio)

        # --- Calidad
        quality = Adw.PreferencesGroup(title="Calidad")
        self.row_format = Adw.ComboRow(title="Formato", model=Gtk.StringList.new([f[0] for f in FORMATS]))
        self.row_format.connect("notify::selected", self._on_format_changed)
        self.row_fps = Adw.ComboRow(title="Fotogramas por segundo", model=Gtk.StringList.new(FPS_OPTIONS))
        self.row_fps.set_selected(2)
        self.row_quality = Adw.ComboRow(title="Calidad de video", model=Gtk.StringList.new(QUALITY_OPTIONS))
        self.row_quality.set_selected(1)
        for r in (self.row_format, self.row_fps, self.row_quality):
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
        output.add(self.row_folder)
        output.add(self.row_countdown)
        content.append(output)

        clamp.set_child(content)
        scroller.set_child(clamp)
        page.append(scroller)

        # --- Botón principal fijo abajo
        bar = Adw.Clamp(maximum_size=520, margin_top=8, margin_bottom=16, margin_start=16, margin_end=16)
        self.record_button = Gtk.Button(label="Empezar a grabar  (Ctrl+R)")
        self.record_button.add_css_class("record-button")
        self.record_button.add_css_class("pill")
        self.record_button.connect("clicked", lambda *_: self._toggle_record())
        bar.set_child(self.record_button)
        page.append(bar)

        self._update_bar_hint()
        return page

    def _update_bar_hint(self):
        """Explica en lenguaje simple si los controles saldrán en el video."""
        if not self.row_bar.get_active():
            text = "Sin barra, usa la ventana de Kinetta para detener la grabación."
        elif self.mode == "ventana":
            text = "Al grabar una ventana, los controles no salen en el video."
        elif layer_shell_available() and len(self.monitors) > 1:
            text = "Para que la barra no salga en el video, muéstrala en un monitor que no grabes."
        else:
            text = (
                "Con un solo monitor, la barra saldrá en el video si grabas pantalla o región. "
                "Usa el modo Ventana para evitarlo."
            )
        if not layer_shell_available():
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

        self.rec_info = Gtk.Label(label="")
        self.rec_info.add_css_class("dim-label")
        box.append(self.rec_info)

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
            self.record_button.set_label(label or "Preparando…")
            self.record_button.set_sensitive(False)
            self.stack.set_sensitive(False)
        else:
            self.record_button.set_label("Empezar a grabar  (Ctrl+R)")
            self.record_button.set_sensitive(True)
            self.stack.set_sensitive(True)

    def _begin(self):
        """Elige la fuente en el portal (y la región) antes de la cuenta atrás."""
        if self._busy:
            return
        self._busy = True
        self._set_setup_busy(True, "Eligiendo qué grabar…")
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

        if self.config.countdown > 0:
            self._countdown_left = self.config.countdown
            self.countdown_label.set_label(str(self._countdown_left))
            self.stack.set_visible_child_name("countdown")
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

    def _start_recording(self):
        try:
            if self.config.format_ext == "gif" and shutil.which("ffmpeg") is None:
                raise RecorderError("Para exportar GIF se necesita ffmpeg instalado")
            self.recorder.start(self.config)
        except RecorderError as exc:
            self.recorder.cancel()
            self.stack.set_visible_child_name("setup")
            self._error_toast(str(exc))
            return

        self._elapsed_before_pause = 0.0
        self._segment_start = time.monotonic()

        # Reiniciar la pantalla de grabación integrada
        self.timer_label.set_label("00:00")
        self.rec_status.set_label("Grabando")
        self.rec_dot.add_css_class("live")
        self.pause_button.set_icon_name("media-playback-pause-symbolic")
        self.pause_button.set_tooltip_text("Pausar")
        mode_name = next(m[2] for m in MODES if m[0] == self.config.mode)
        self.rec_info.set_label(f"{mode_name} · {self.config.format_ext.upper()} · {self.config.fps} fps")

        if self.config.floating_bar:
            # La ventana principal se oculta y solo queda la barra pequeña.
            self.bar = FloatingBar(
                self.get_application(),
                on_pause=self._toggle_pause,
                on_stop=self._stop_recording,
                monitor=self.config.bar_monitor,
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
        if self.bar:
            self.bar.set_time(text)
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
            if self.bar:
                self.bar.set_paused(False)
        else:
            self._elapsed_before_pause = self._elapsed()
            self.recorder.pause()
            self.rec_status.set_label("En pausa")
            self.rec_dot.remove_css_class("live")
            self.pause_button.set_icon_name("media-playback-start-symbolic")
            self.pause_button.set_tooltip_text("Continuar")
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
            self._last_file = path

            if path:
                label = "GIF guardado" if path.endswith(".gif") else "Grabación guardada"
                toast = Adw.Toast(title=label, button_label="Abrir carpeta", action_name="win.open-folder")
                self.toasts.add_toast(toast)
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

        self.set_accels_for_action("app.quit", ["<Ctrl>Q"])
        self.set_accels_for_action("win.toggle-record", ["<Ctrl>R"])

    def _on_activate(self, app):
        provider = Gtk.CssProvider()
        if hasattr(provider, "load_from_string"):  # GTK >= 4.12
            provider.load_from_string(CSS)
        else:
            provider.load_from_data(CSS.encode())
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
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
                developer_name="Kinetta",
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
                developer_name="Kinetta",
                version=APP_VERSION,
                website=APP_WEBSITE,
                comments="Grabador de pantalla sencillo para Linux.",
                license_type=Gtk.License.GPL_3_0,
            )
            about.present()


def main():
    return KinettaApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
