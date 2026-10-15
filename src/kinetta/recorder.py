"""Motor de grabación de Kinetta.

Flujo completo:

1. Se abre una sesión en el portal `org.freedesktop.portal.ScreenCast`
   (xdg-desktop-portal). El usuario elige la pantalla o la ventana.
2. `OpenPipeWireRemote` devuelve un descriptor de PipeWire y el nodo del
   stream elegido.
3. Se construye un pipeline de GStreamer: `pipewiresrc` -> codificador ->
   muxer -> archivo.
4. Audio: `pulsesrc` captura el monitor del altavoz por defecto (audio del
   sistema) y el micrófono; se mezclan con `audiomixer`.
5. Pausa: las válvulas descartan los buffers mientras está en pausa y el
   salto de timestamps se compensa con `pad.set_offset()` sobre elementos
   `identity` (el `ts-offset` de identity solo afecta al reloj, no a los
   buffers), para que el archivo quede continuo.
6. Parar: EOS a las fuentes, se espera a que el muxer cierre el archivo y se
   cierra la sesión del portal. Para GIF se graba un MP4 temporal y se
   convierte con ffmpeg (paleta de colores incluida).
"""

import os
import shutil
import tempfile
import time
from dataclasses import dataclass

import gi

gi.require_version("Gst", "1.0")
from gi.repository import Gio, GLib, Gst

Gst.init(None)

PORTAL_BUS_NAME = "org.freedesktop.portal.Desktop"
PORTAL_OBJECT = "/org/freedesktop/portal/desktop"
IFACE_SCREENCAST = "org.freedesktop.portal.ScreenCast"
IFACE_SESSION = "org.freedesktop.portal.Session"
IFACE_REQUEST = "org.freedesktop.portal.Request"

# Tipos de fuente del portal ScreenCast
SOURCE_MONITOR = 1
SOURCE_WINDOW = 2

# Modos de cursor del portal ScreenCast
CURSOR_HIDDEN = 1
CURSOR_EMBEDDED = 2

# Calidad -> kbit/s a 1080p (se escala según el tamaño de la pantalla)
VIDEO_BITRATES = {0: 2500, 1: 5000, 2: 10000}
AUDIO_BITRATE = 192000

# Límites del GIF para que no pese demasiado
GIF_MAX_WIDTH = 960
GIF_MAX_FPS = 15


class RecorderError(Exception):
    """Fallo al preparar o ejecutar la grabación."""


class RecorderCancelled(RecorderError):
    """El usuario canceló en el diálogo del portal o al elegir la región."""


@dataclass
class RecordingConfig:
    mode: str = "pantalla"
    system_audio: bool = True
    microphone: bool = False
    show_cursor: bool = True
    format_ext: str = "mp4"
    fps: int = 30
    quality: int = 1
    countdown: int = 3
    output_dir: str = ""
    floating_bar: bool = True
    bar_monitor: object = None  # Gdk.Monitor donde mostrar la barra (None = el predeterminado)
    crop: tuple = None  # (left, top, width, height) en píxeles del stream


class ScreenCastPortal:
    """Cliente mínimo del portal ScreenCast (D-Bus)."""

    def __init__(self):
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        sender = self.bus.get_unique_name()[1:].replace(".", "_")
        self.session_path = None
        self._sender = sender
        self._counter = 0

    # ------------------------------------------------------------------ utils
    def _token(self, prefix):
        self._counter += 1
        return f"kinetta_{prefix}_{self._counter}"

    def _request_path(self, token):
        return f"{PORTAL_OBJECT}/request/{self._sender}/{token}"

    def _call(self, method, params, iface=IFACE_SCREENCAST, path=PORTAL_OBJECT):
        try:
            return self.bus.call_sync(
                PORTAL_BUS_NAME, path, iface, method, params, None, Gio.DBusCallFlags.NONE, -1, None
            )
        except GLib.Error as exc:
            raise RecorderError(
                "No se pudo hablar con xdg-desktop-portal. "
                "¿Está instalado y en ejecución? (" + exc.message + ")"
            ) from exc

    def _call_with_response(self, method, make_params, timeout=600):
        """Llama a un método del portal y espera la señal `Response`."""
        token = self._token("req")
        path = self._request_path(token)
        outcome = {}
        loop = GLib.MainLoop()

        def on_response(_conn, _sender, _objpath, _iface, _signal, params):
            code, results = params.unpack()
            outcome["code"] = code
            outcome["results"] = results
            loop.quit()

        sub = self.bus.signal_subscribe(
            PORTAL_BUS_NAME, IFACE_REQUEST, "Response", path, None, Gio.DBusSignalFlags.NONE, on_response
        )
        timed_out = {"value": False}

        def on_timeout():
            timed_out["value"] = True
            loop.quit()
            return GLib.SOURCE_REMOVE

        timeout_id = GLib.timeout_add_seconds(timeout, on_timeout)
        try:
            self._call(method, make_params(token))
            loop.run()
        finally:
            GLib.source_remove(timeout_id)
            self.bus.signal_unsubscribe(sub)

        if timed_out["value"]:
            raise RecorderError("El portal de escritorio no respondió a tiempo")
        code = outcome.get("code", 2)
        if code == 1:
            raise RecorderCancelled("Selección cancelada en el diálogo del portal")
        if code != 0:
            raise RecorderError("El portal de escritorio rechazó la petición")
        return outcome.get("results", {})

    # -------------------------------------------------------------- operaciones
    def create_session(self):
        session_token = self._token("session")

        def make_params(token):
            return GLib.Variant(
                "(a{sv})",
                (
                    {
                        "handle_token": GLib.Variant("s", token),
                        "session_handle_token": GLib.Variant("s", session_token),
                    },
                ),
            )

        results = self._call_with_response("CreateSession", make_params)
        self.session_path = results.get("session_handle")
        if not self.session_path:
            # Algunos portales antiguos no lo devuelven: se deduce del token.
            self.session_path = f"{PORTAL_OBJECT}/session/{self._sender}/{session_token}"
        if not self.session_path:
            raise RecorderError("El portal no devolvió una sesión de grabación")

    def select_sources(self, source_types, cursor_mode):
        def make_params(token):
            options = {
                "handle_token": GLib.Variant("s", token),
                "types": GLib.Variant("u", source_types),
                "multiple": GLib.Variant("b", False),
                "cursor_mode": GLib.Variant("u", cursor_mode),
                "persist_mode": GLib.Variant("u", 0),
            }
            return GLib.Variant("(oa{sv})", (self.session_path, options))

        self._call_with_response("SelectSources", make_params)

    def start(self, parent_window=""):
        def make_params(token):
            options = {"handle_token": GLib.Variant("s", token)}
            return GLib.Variant("(osa{sv})", (self.session_path, parent_window, options))

        results = self._call_with_response("Start", make_params)
        streams = results.get("streams") or []
        if not streams:
            raise RecorderError("El portal no devolvió ningún stream de video")
        node_id, props = streams[0]
        return int(node_id), dict(props)

    def open_pipewire_remote(self):
        """Devuelve el fd de PipeWire de la sesión (PipeWire se encarga de cerrarlo)."""
        out, fd_list = self.bus.call_with_unix_fd_list_sync(
            PORTAL_BUS_NAME,
            PORTAL_OBJECT,
            IFACE_SCREENCAST,
            "OpenPipeWireRemote",
            GLib.Variant("(oa{sv})", (self.session_path, {})),
            None,
            Gio.DBusCallFlags.NONE,
            -1,
            None,
        )
        index = out.unpack()[0]
        return fd_list.get(index)

    def close(self):
        if self.session_path:
            try:
                self._call("Close", None, iface=IFACE_SESSION, path=self.session_path)
            except RecorderError:
                pass
            self.session_path = None


def video_bitrate_kbps(size, quality):
    """kbit/s del video según el tamaño de la pantalla y la calidad elegida."""
    base = VIDEO_BITRATES.get(int(quality), VIDEO_BITRATES[1])
    pixels = max(1, int(size[0])) * max(1, int(size[1]))
    scale = max(1.0, pixels / (1920 * 1080))
    return max(1000, int(base * scale / 250) * 250)


def pick_element(candidates, what):
    for name in candidates:
        if Gst.ElementFactory.find(name):
            return name
    raise RecorderError(
        f"No hay ningún codificador {what} instalado (se buscó: {', '.join(candidates)}). "
        "Instala los plugins de GStreamer que indica el README."
    )


class Recorder:
    """Grabación real: portal ScreenCast + PipeWire + GStreamer."""

    def __init__(self, on_error=None):
        self.on_error = on_error
        self.config = None
        self.paused = False
        self.error_message = None

        self.portal = None
        self.stream_info = {}
        self.pipeline = None
        self._fd = None
        self._node_id = None
        self._bus = None
        self._sources = []
        self._valves = []
        self._offsets = []
        self._ts_offset = 0
        self._pause_started = 0.0
        self._target_path = None
        self._temp_dir = None
        self._temp_recording = None

    # ------------------------------------------------------------------ fase 1
    def prepare(self, config, parent_window=""):
        """Abre la sesión del portal y elige la fuente.

        Bloquea mientras el usuario decide en el diálogo del portal, pero el
        bucle principal de GTK sigue procesando eventos.
        """
        self.cancel()
        self.config = config
        portal = ScreenCastPortal()
        try:
            portal.create_session()
            source_types = SOURCE_WINDOW if config.mode == "ventana" else SOURCE_MONITOR
            cursor_mode = CURSOR_EMBEDDED if config.show_cursor else CURSOR_HIDDEN
            portal.select_sources(source_types, cursor_mode)
            node_id, props = portal.start(parent_window)
            fd = portal.open_pipewire_remote()
        except BaseException:
            portal.close()
            raise
        self.portal = portal
        self._node_id = node_id
        self._fd = fd
        self.stream_info = {
            "size": tuple(props.get("size") or ()),
            "position": tuple(props.get("position") or ()),
        }
        return self.stream_info

    # ------------------------------------------------------------------ fase 2
    def start(self, config):
        """Construye el pipeline y empieza a grabar. Devuelve la ruta del archivo."""
        if self.portal is None:
            raise RecorderError("La sesión del portal no está preparada")
        self.config = config
        self.error_message = None
        self.paused = False

        output_dir = config.output_dir or os.path.expanduser("~")
        if not os.path.isdir(output_dir) or not os.access(output_dir, os.W_OK):
            raise RecorderError(f"No se puede escribir en la carpeta «{output_dir}»")

        name = time.strftime("Grabacion_%Y-%m-%d_%H-%M-%S")
        if config.format_ext == "gif":
            self._temp_dir = tempfile.mkdtemp(prefix="kinetta-")
            self._temp_recording = os.path.join(self._temp_dir, "captura.mp4")
            self._target_path = os.path.join(output_dir, name + ".gif")
            record_path = self._temp_recording
        else:
            self._target_path = os.path.join(output_dir, name + "." + config.format_ext)
            record_path = self._target_path

        try:
            self.pipeline = self._build_pipeline(config, record_path)
        except RecorderError:
            self._cleanup_temp()
            raise
        except GLib.Error as exc:
            self._cleanup_temp()
            raise RecorderError(f"No se pudo construir el pipeline: {exc.message}") from exc

        self._bus = self.pipeline.get_bus()
        self._bus.add_signal_watch()
        self._bus.connect("message::error", self._on_bus_error)

        state = self.pipeline.set_state(Gst.State.PLAYING)
        if state == Gst.StateChangeReturn.FAILURE:
            self._discard_pipeline()
            raise RecorderError("No se pudo iniciar la grabación (revisa los dispositivos de audio)")
        return self._target_path

    def _build_pipeline(self, config, record_path):
        size = self.stream_info.get("size") or (1920, 1080)
        is_gif = config.format_ext == "gif"
        container = "mp4" if is_gif else config.format_ext
        fps = min(config.fps, GIF_MAX_FPS) if is_gif else config.fps

        pipeline = Gst.Pipeline.new("kinetta")

        def make(factory, name=None, **props):
            element = Gst.ElementFactory.make(factory, name)
            if element is None:
                raise RecorderError(
                    f"Falta el elemento «{factory}» de GStreamer. "
                    "Instala los plugins que indica el README."
                )
            for prop, value in props.items():
                element.set_property(prop.replace("_", "-"), value)
            pipeline.add(element)
            return element

        def link(*elements):
            for index in range(len(elements) - 1):
                current, following = elements[index], elements[index + 1]
                if not current.link(following):
                    raise RecorderError(
                        f"No se pudieron enlazar «{current.get_name()}» y «{following.get_name()}»"
                    )

        # --- video ---------------------------------------------------------
        video_src = make(
            "pipewiresrc",
            fd=self._fd,
            path=self._node_id,
            do_timestamp=True,
            keepalive_time=1000,
            client_name="Kinetta",
        )
        valve = make("valve", "video-pause", drop=False)
        offset = make("identity", "video-offset")
        video = [
            video_src,
            make("queue", max_size_buffers=2),
            valve,
            offset,
            make("videoconvert"),
        ]
        self._sources.append(video_src)
        self._valves.append(valve)
        self._offsets.append(offset)

        if config.crop:
            left, top, width, height = (int(v) for v in config.crop)
            video.append(
                make(
                    "videocrop",
                    left=left,
                    top=top,
                    right=int(size[0]) - left - width,
                    bottom=int(size[1]) - top - height,
                )
            )
        video.append(make("videorate", drop_only=True))
        video.append(make("capsfilter", caps=Gst.Caps.from_string(f"video/x-raw,framerate={fps}/1")))

        bitrate = video_bitrate_kbps(size, config.quality)
        if container == "mp4":
            encoder_name = pick_element(("x264enc", "avenc_h264", "openh264enc"), "H.264")
            encoder = make(encoder_name)
            if encoder_name == "x264enc":
                encoder.set_property("bitrate", bitrate)
                encoder.set_property("speed-preset", "veryfast")
                encoder.set_property("tune", "zerolatency")
                encoder.set_property("key-int-max", fps * 2)
                video.append(encoder)
                video.append(make("h264parse"))
            else:
                encoder.set_property("bitrate", bitrate * 1000)
                video.append(encoder)
                video.append(make("h264parse"))
            muxer_name = "mp4mux"
        else:
            encoder_name = pick_element(("vp9enc", "vp8enc"), "VP8/VP9")
            encoder = make(encoder_name)
            encoder.set_property("target-bitrate", bitrate * 1000)
            encoder.set_property("deadline", 1)
            if encoder_name == "vp9enc":
                encoder.set_property("cpu-used", 6)
                encoder.set_property("lag-in-frames", 0)
            video.append(encoder)
            muxer_name = "webmmux"

        video_tail = make("queue")
        link(*video, video_tail)
        muxer = make(muxer_name)
        filesink = make("filesink", location=record_path)
        if not muxer.link(filesink):
            raise RecorderError("No se pudo enlazar el muxer con el archivo de salida")
        video_pad = video_tail.get_static_pad("src").link(muxer.request_pad_simple("video_%u"))
        if video_pad != Gst.PadLinkReturn.OK:
            raise RecorderError("No se pudo enlazar el video con el muxer")

        # --- audio ---------------------------------------------------------
        if not is_gif and (config.system_audio or config.microphone):
            if container == "mp4":
                audio_encoder_name = pick_element(("avenc_aac", "fdkaacenc"), "AAC (MP4)")
            else:
                audio_encoder_name = pick_element(("opusenc", "vorbisenc"), "Opus/Vorbis (WebM)")

            branches = []
            if config.system_audio:
                branches.append(({"device": "@DEFAULT_MONITOR@"}, "sistema"))
            if config.microphone:
                branches.append(({}, "microfono"))

            mixer = make("audiomixer", "audio-mix") if len(branches) > 1 else None
            last_of_branch = []
            for index, (props, label) in enumerate(branches):
                source = make("pulsesrc", client_name=f"Kinetta ({label})", **props)
                branch = [
                    source,
                    make("queue", max_size_buffers=10),
                    make("valve", f"audio-pause-{index}", drop=False),
                    make("identity", f"audio-offset-{index}"),
                    make("audioconvert"),
                    make("audioresample"),
                    make(
                        "capsfilter",
                        caps=Gst.Caps.from_string("audio/x-raw,rate=48000,channels=2"),
                    ),
                ]
                link(*branch)
                self._sources.append(source)
                self._valves.append(branch[2])
                self._offsets.append(branch[3])
                if mixer is None:
                    last_of_branch.append(branch[-1])
                else:
                    pad = mixer.request_pad_simple(f"sink_{index}")
                    if pad is None or branch[-1].get_static_pad("src").link(pad) != Gst.PadLinkReturn.OK:
                        raise RecorderError("No se pudo conectar el audio al mezclador")
            if mixer is not None:
                converter = make("audioconvert")
                link(mixer, converter)
                last_of_branch.append(converter)

            encoder = make(audio_encoder_name)
            if audio_encoder_name in ("avenc_aac", "fdkaacenc"):
                encoder.set_property("bitrate", AUDIO_BITRATE)
            elif audio_encoder_name == "opusenc":
                encoder.set_property("bitrate", 128000)
            else:
                encoder.set_property("bitrate", AUDIO_BITRATE)
            audio_queue = make("queue")
            tail = [encoder, audio_queue]
            link(*tail)
            if not last_of_branch[0].link(encoder):
                raise RecorderError("No se pudo enlazar el audio con el codificador")
            audio_pad = audio_queue.get_static_pad("src").link(muxer.request_pad_simple("audio_%u"))
            if audio_pad != Gst.PadLinkReturn.OK:
                raise RecorderError("No se pudo enlazar el audio con el muxer")

        # --- salida --------------------------------------------------------
        return pipeline

    # ------------------------------------------------------------------- pausa
    def pause(self):
        if self.pipeline is None or self.paused:
            return
        self.paused = True
        self._pause_started = time.monotonic()
        for valve in self._valves:
            valve.set_property("drop", True)

    def resume(self):
        if self.pipeline is None or not self.paused:
            return
        gap = time.monotonic() - self._pause_started
        self._ts_offset -= int(gap * Gst.SECOND)
        for element in self._offsets:
            element.get_static_pad("src").set_offset(self._ts_offset)
        for valve in self._valves:
            valve.set_property("drop", False)
        self.paused = False

    # ------------------------------------------------------------------- parar
    def stop(self):
        """Finaliza el archivo y devuelve su ruta (None si hubo error).

        Para GIF convierte el MP4 temporal con ffmpeg antes de devolver.
        """
        pipeline = self.pipeline
        if pipeline is None:
            self.cancel()
            return None
        self.pipeline = None
        finalize = self.error_message is None
        try:
            if finalize:
                for source in self._sources:
                    source.send_event(Gst.Event.new_eos())
                self._wait_for_eos()
            pipeline.set_state(Gst.State.NULL)
            pipeline.get_state(5 * Gst.SECOND)
        finally:
            try:
                bus = pipeline.get_bus()
                if bus is not None:
                    bus.remove_signal_watch()
            except GLib.Error:
                pass
            self.portal_close()
            self._reset_lists()

        if self.error_message:
            self._remove_file(self._temp_recording)
            self._remove_file(self._target_path)
            return None
        if self.config is not None and self.config.format_ext == "gif":
            try:
                self._convert_gif()
            finally:
                self._cleanup_temp()
        return self._target_path

    def _wait_for_eos(self, timeout=20):
        bus = self._bus
        if bus is None:
            return
        loop = GLib.MainLoop()

        def on_message(_bus, message):
            if message.type == Gst.MessageType.EOS:
                loop.quit()
            elif message.type == Gst.MessageType.ERROR:
                error, _debug = message.parse_error()
                self.error_message = self.error_message or error.message
                loop.quit()

        handler = bus.connect("message", on_message)

        def on_timeout():
            loop.quit()
            return GLib.SOURCE_REMOVE

        timeout_id = GLib.timeout_add_seconds(timeout, on_timeout)
        try:
            loop.run()
        finally:
            GLib.source_remove(timeout_id)
            bus.disconnect(handler)

    # -------------------------------------------------------------------- GIF
    def _convert_gif(self):
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg is None:
            raise RecorderError("Se necesita ffmpeg instalado para exportar GIF")
        filters = (
            f"scale=w='min({GIF_MAX_WIDTH},iw)':h=-2:flags=lanczos,"
            "split[s0][s1];[s0]palettegen=stats_mode=diff[p];[s1][p]paletteuse=dither=bayer:bayer_scale=3"
        )
        argv = [
            ffmpeg,
            "-y",
            "-loglevel",
            "error",
            "-i",
            self._temp_recording,
            "-vf",
            filters,
            "-an",
            "-loop",
            "0",
            self._target_path,
        ]
        success, stderr = self._run_process(argv)
        if not success or not os.path.exists(self._target_path):
            detail = stderr.strip()[:300] or "error desconocido"
            raise RecorderError("ffmpeg no pudo convertir el GIF: " + detail)

    def _run_process(self, argv):
        """Ejecuta un proceso sin congelar la interfaz (bucle anidado)."""
        process = Gio.Subprocess.new(
            argv, Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_PIPE
        )
        loop = GLib.MainLoop()
        outcome = {"ok": False, "stderr": ""}

        def on_done(proc, result):
            try:
                ok, _stdout, stderr = proc.communicate_utf8_finish(result)
                outcome["ok"] = bool(ok)
                outcome["stderr"] = stderr or ""
            except GLib.Error:
                outcome["ok"] = False
            loop.quit()

        process.communicate_utf8_async(None, None, on_done)
        loop.run()
        return outcome["ok"], outcome["stderr"]

    # -------------------------------------------------------------- limpieza
    def cancel(self):
        """Aborta sin finalizar: cierra sesión y pipeline, borra archivos parciales."""
        had_pipeline = self.pipeline is not None
        self._discard_pipeline()
        if not had_pipeline and self._fd is not None:
            try:  # nunca llegó a usarlo pipewiresrc: cerramos el fd nosotros
                os.close(self._fd)
            except OSError:
                pass
        self.portal_close()
        self._remove_file(self._temp_recording)
        self._cleanup_temp()
        self._target_path = None
        self.paused = False
        self._ts_offset = 0

    def _discard_pipeline(self):
        pipeline, self.pipeline = self.pipeline, None
        if pipeline is not None:
            try:
                pipeline.set_state(Gst.State.NULL)
                pipeline.get_state(5 * Gst.SECOND)
            except GLib.Error:
                pass
            try:
                bus = pipeline.get_bus()
                if bus is not None:
                    bus.remove_signal_watch()
            except GLib.Error:
                pass
            if self._target_path:
                self._remove_file(self._target_path)
        self._reset_lists()

    def portal_close(self):
        if self.portal is not None:
            self.portal.close()
            self.portal = None
            self._fd = None
            self._node_id = None

    def _reset_lists(self):
        self._sources = []
        self._valves = []
        self._offsets = []
        self._bus = None

    def _cleanup_temp(self):
        if self._temp_dir and os.path.isdir(self._temp_dir):
            shutil.rmtree(self._temp_dir, ignore_errors=True)
        self._temp_dir = None
        self._temp_recording = None

    @staticmethod
    def _remove_file(path):
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except OSError:
                pass

    def _on_bus_error(self, _bus, message):
        error, _debug = message.parse_error()
        self.error_message = error.message
        if self.on_error is not None:
            self.on_error(error.message)
