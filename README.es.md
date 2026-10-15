# Kinetta

Grabador de pantalla sencillo para Linux, hecho con GTK4 y libadwaita.

*Read in English: [README.md](README.md)*

> **Estado: v0.3.** La grabación real funciona mediante el portal `ScreenCast`
> (xdg-desktop-portal), PipeWire y GStreamer. Faltan los atajos globales, las
> traducciones y la publicación en Flathub (ver hoja de ruta).

## Funciones

- Grabar una **pantalla**, una **ventana** o una **región** (selector propio)
- Audio del sistema y micrófono, mezclados en un solo archivo
- Salida en MP4 (H.264), WebM (VP9) y GIF (el GIF requiere ffmpeg)
- Ajustes de fotogramas por segundo y calidad (el bitrate se adapta al tamaño de la pantalla)
- Cuenta regresiva antes de empezar
- **Pausa real:** el tiempo en pausa no aparece en el video
- **Barra flotante de controles** con temporizador, pausa y detener
- Aspecto nativo con GTK4 y libadwaita

## Ejecutar

Instala las dependencias. Además de GTK y PyGObject necesitas GStreamer (con el
plugin de PipeWire) y un xdg-desktop-portal en ejecución con el backend de tu
escritorio:

```bash
# Debian / Ubuntu / Mint
sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 \
  gstreamer1.0-plugins-base gstreamer1.0-plugins-good gstreamer1.0-plugins-bad \
  gstreamer1.0-plugins-ugly gstreamer1.0-pipewire \
  xdg-desktop-portal xdg-desktop-portal-gnome   # -kde / -wlr / -hyprland según tu escritorio
# opcional, para exportar GIF:
sudo apt install ffmpeg

# Fedora
sudo dnf install python3-gobject gtk4 libadwaita \
  gstreamer1-plugins-base gstreamer1-plugins-good gstreamer1-plugins-bad-free \
  gstreamer1-plugin-pipewire xdg-desktop-portal-gnome
# H.264: sudo dnf install gstreamer1-plugins-ugly   (RPM Fusion)
# opcional: sudo dnf install ffmpeg

# Arch
sudo pacman -S python-gobject gtk4 libadwaita gst-plugin-pipewire \
  gst-plugins-base gst-plugins-good gst-plugins-bad gst-plugins-ugly \
  xdg-desktop-portal xdg-desktop-portal-hyprland  # backend según tu escritorio
# opcional: sudo pacman -S ffmpeg
```

Luego, desde la carpeta del proyecto:

```bash
make run
```

Requiere GTK 4.10+, libadwaita 1.4+ y GStreamer 1.22+.

### Instalar en tu usuario (recomendado)

Instala el comando `kinetta` y una entrada en el menú dentro de `~/.local`, sin
tocar el sistema:

```bash
make install     # PREFIX=~/.local por defecto
make uninstall
```

### Barra flotante siempre encima (opcional)

Para que la barra quede encima de las demás ventanas en KDE Plasma, Sway, Hyprland o COSMIC, instala `gtk4-layer-shell`. Sin él, la barra funciona como una ventana normal (y el selector de región usa una ventana a pantalla completa en lugar de un overlay exacto sobre el monitor elegido).

```bash
sudo apt install gir1.2-gtk4layershell-1.0   # Debian / Ubuntu
sudo dnf install gtk4-layer-shell            # Fedora
sudo pacman -S gtk4-layer-shell              # Arch
```

GNOME no soporta este protocolo, así que ahí la barra no puede forzarse encima de otras ventanas.

## Notas

- El portal pide permiso cada vez que empiezas a grabar (todavía no se recuerda
  la elección con restore tokens).
- En modo Región, dibuja el rectángulo sobre el monitor que elegiste en el
  diálogo del portal (con gtk4-layer-shell el overlay aparece exactamente sobre
  ese monitor).
- Las grabaciones GIF se convierten con ffmpeg, limitadas a 960 px de ancho y
  15 fps para que el archivo no se dispare.
- Mientras grabas, la app se oculta y solo queda la barra flotante; úsala para
  pausar o detener. Si desactivas la barra, la ventana principal mantiene los
  controles.

## ¿Los controles salen en el video?

- **Modo Ventana:** no. Solo se graba la ventana elegida.
- **Pantalla o región con varios monitores:** muestra la barra en un monitor que no grabes.
- **Pantalla o región con un monitor:** sí. Wayland no ofrece una forma estándar de excluir una ventana de la captura.

## Hoja de ruta

- [x] Interfaz: modo, audio, calidad, carpeta, cuenta regresiva, pausa
- [x] Barra flotante de controles (layer-shell)
- [x] Motor de grabación: portal `ScreenCast` + PipeWire + GStreamer
- [x] Captura de audio del sistema y micrófono
- [x] Exportar a GIF
- [x] Selector de región propio (recorte con GStreamer)
- [ ] Recordar el permiso del portal (restore token)
- [ ] Atajos globales (portal `GlobalShortcuts`)
- [ ] Traducciones (gettext)
- [ ] Publicación en Flathub

## Estructura del proyecto

```
src/kinetta/     código de la aplicación (app.py, recorder.py, region.py)
data/            archivo .desktop, metadatos AppStream, icono
flatpak/         manifiesto de Flatpak
packaging/       lanzador que instala `make install`
.github/         integración continua
```

## Contribuir

Las contribuciones son bienvenidas. Mira [CONTRIBUTING.md](CONTRIBUTING.md).

## Licencia

[GPL-3.0-or-later](LICENSE)
