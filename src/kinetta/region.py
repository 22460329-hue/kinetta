"""Selector de región de Kinetta.

Overlay a pantalla completa sobre el monitor capturado donde el usuario
arrastra un rectángulo. Con gtk4-layer-shell el overlay se coloca exactamente
en el monitor elegido en el portal; sin él se usa una ventana a pantalla
completa (mejor en un solo monitor).

Devuelve el recorte en píxeles del stream (left, top, width, height),
ajustado a dimensiones pares porque H.264/VP9 lo necesitan.
"""

import cairo
import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gdk, GLib, Gtk

try:
    gi.require_version("Gtk4LayerShell", "1.0")
    from gi.repository import Gtk4LayerShell as LayerShell
except (ValueError, ImportError):
    LayerShell = None

MIN_SIZE = 16  # arrastre mínimo para aceptar la región


def match_monitor(monitors, position, size):
    """Monitor de GDK que corresponde al stream del portal.

    Compara posiciones escalando por la relación entre el tamaño del stream
    (píxeles físicos) y el tamaño lógico del monitor (HiDPI).
    """
    if not monitors:
        return None
    if not position or not size or len(position) < 2 or len(size) < 2 or not size[0]:
        return monitors[0]
    best, best_distance = monitors[0], None
    for monitor in monitors:
        geometry = monitor.get_geometry()
        if geometry.width <= 0:
            continue
        scale = size[0] / geometry.width
        distance = abs(geometry.x * scale - position[0]) + abs(geometry.y * scale - position[1])
        if best_distance is None or distance < best_distance:
            best, best_distance = monitor, distance
    return best


def crop_from_rect(rect, overlay_size, stream_size):
    """Rectángulo del overlay (logical) -> recorte en píxeles del stream."""
    x, y, width, height = rect
    overlay_w, overlay_h = overlay_size
    stream_w, stream_h = stream_size
    left = round(x / overlay_w * stream_w)
    top = round(y / overlay_h * stream_h)
    right = round((x + width) / overlay_w * stream_w)
    bottom = round((y + height) / overlay_h * stream_h)
    left = max(0, min(left, stream_w - 2))
    top = max(0, min(top, stream_h - 2))
    width = max(2, min(right - left, stream_w - left))
    height = max(2, min(bottom - top, stream_h - top))
    width -= width % 2
    height -= height % 2
    return (left, top, width, height)


class RegionSelector(Gtk.Window):
    """Ventana a pantalla completa para dibujar la región."""

    def __init__(self, app, monitor, stream_size, on_done):
        super().__init__(application=app, decorated=False, title="Selecciona la región")
        self.add_css_class("region-overlay")
        self._stream_size = stream_size
        self._on_done = on_done
        self._finished = False
        self._start = (0, 0)
        self._rect = None

        if LayerShell is not None and LayerShell.is_supported():
            LayerShell.init_for_window(self)
            LayerShell.set_namespace(self, "kinetta-region")
            LayerShell.set_layer(self, LayerShell.Layer.OVERLAY)
            for edge in (
                LayerShell.Edge.TOP,
                LayerShell.Edge.BOTTOM,
                LayerShell.Edge.LEFT,
                LayerShell.Edge.RIGHT,
            ):
                LayerShell.set_anchor(self, edge, True)
            LayerShell.set_keyboard_mode(self, LayerShell.KeyboardMode.EXCLUSIVE)
            if monitor is not None:
                LayerShell.set_monitor(self, monitor)

        self.area = Gtk.DrawingArea()
        self.area.set_draw_func(self._draw)

        hint = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        hint.add_css_class("region-hint")
        hint.set_halign(Gtk.Align.CENTER)
        hint.set_valign(Gtk.Align.START)
        hint.set_margin_top(28)
        hint.set_can_target(False)
        title = Gtk.Label(label="Arrastra para elegir la región")
        title.add_css_class("heading")
        subtitle = Gtk.Label(label="Esc para cancelar")
        subtitle.add_css_class("caption")
        subtitle.add_css_class("dim-label")
        hint.append(title)
        hint.append(subtitle)

        overlay = Gtk.Overlay()
        overlay.set_child(self.area)
        overlay.add_overlay(hint)
        self.set_child(overlay)

        gesture = Gtk.GestureDrag()
        gesture.connect("drag-begin", self._on_drag_begin)
        gesture.connect("drag-update", self._on_drag_update)
        gesture.connect("drag-end", self._on_drag_end)
        self.area.add_controller(gesture)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key_pressed)
        self.add_controller(keys)

    # ------------------------------------------------------------ interfaz
    def _draw(self, _area, cr, _width, _height):
        cr.set_source_rgba(0.0, 0.0, 0.0, 0.45)
        cr.paint()
        if self._rect:
            x, y, width, height = self._rect
            cr.save()
            cr.set_operator(cairo.OPERATOR_CLEAR)
            cr.rectangle(x, y, width, height)
            cr.fill()
            cr.restore()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
            cr.set_line_width(2)
            cr.rectangle(x + 1, y + 1, max(0, width - 2), max(0, height - 2))
            cr.stroke()
            label = f"{int(width)} × {int(height)}"
            cr.set_font_size(14)
            extents = cr.text_extents(label)
            tx = x + 10
            ty = y - 12 if y > 32 else y + height + 24
            cr.set_source_rgba(0.0, 0.0, 0.0, 0.65)
            cr.rectangle(tx - 6, ty - 15, extents.width + 12, 21)
            cr.fill()
            cr.set_source_rgba(1.0, 1.0, 1.0, 0.95)
            cr.move_to(tx, ty)
            cr.show_text(label)
        return False

    # ------------------------------------------------------------- gestos
    def _on_drag_begin(self, _gesture, start_x, start_y):
        self._start = (start_x, start_y)
        self._rect = (start_x, start_y, 0, 0)

    def _on_drag_update(self, _gesture, offset_x, offset_y):
        self._rect = self._rect_from_offset(offset_x, offset_y)
        self.area.queue_draw()

    def _on_drag_end(self, _gesture, offset_x, offset_y):
        rect = self._rect_from_offset(offset_x, offset_y)
        if rect[2] >= MIN_SIZE and rect[3] >= MIN_SIZE:
            self._finish(rect)
        else:
            self._rect = None
            self.area.queue_draw()

    def _rect_from_offset(self, offset_x, offset_y):
        start_x, start_y = self._start
        x = min(start_x, start_x + offset_x)
        y = min(start_y, start_y + offset_y)
        return (x, y, abs(offset_x), abs(offset_y))

    def _on_key_pressed(self, _controller, keyval, _keycode, _state):
        if keyval == Gdk.KEY_Escape:
            self._finish(None)
            return True
        return False

    # ------------------------------------------------------------ resultado
    def _finish(self, rect):
        if self._finished:
            return
        self._finished = True
        if rect is None:
            result = None
        else:
            overlay_size = (max(1, self.get_width()), max(1, self.get_height()))
            result = crop_from_rect(rect, overlay_size, self._stream_size)
        self.close()
        self._on_done(result)


def select_region(app, monitor, stream_size):
    """Muestra el selector y devuelve el recorte o None si se cancela."""
    outcome = {}
    loop = GLib.MainLoop()

    def finish(result):
        outcome["value"] = result
        loop.quit()

    selector = RegionSelector(app, monitor, stream_size, finish)
    selector.present()
    if LayerShell is None or not LayerShell.is_supported():
        selector.fullscreen()
    loop.run()
    selector.destroy()
    return outcome.get("value")
