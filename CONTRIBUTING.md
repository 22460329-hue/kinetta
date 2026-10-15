# Contributing to Kinetta / Contribuir a Kinetta

Thanks for your interest! / ¡Gracias por tu interés!

## Getting started / Primeros pasos

1. Fork the repository and clone it. / Haz un fork y clónalo.
2. Install the dependencies listed in the README. / Instala las dependencias del README.
3. Run the app with `make run`. / Ejecuta la app con `make run`.
4. Create a branch for your change. / Crea una rama para tu cambio.

## Before opening a pull request / Antes de abrir un pull request

```bash
pip install ruff
make lint
make check
```

- Keep each pull request focused on one change. / Un cambio por pull request.
- Describe what you changed and why; add a screenshot for interface changes. / Explica qué y por qué; añade una captura si cambia la interfaz.
- Test on the desktop you use and say which one it is (GNOME, KDE, Sway...). / Prueba en tu escritorio e indica cuál es.

## Where help is most useful / Dónde ayudas más

- Testing on different desktops and compositors (the engine uses the ScreenCast portal, PipeWire and GStreamer).
- Translations.
- Remembering the portal permission (restore tokens) and improvements to the region selector.
- Packaging (Flatpak, AUR, .deb).

Issues labeled `good first issue` are a good place to start. / Los issues con la etiqueta `good first issue` son un buen punto de partida.

## License

By contributing, you agree that your work is released under GPL-3.0-or-later. / Al contribuir aceptas que tu trabajo se publique bajo GPL-3.0-or-later.
