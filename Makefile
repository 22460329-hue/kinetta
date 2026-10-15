APP_ID := org.kinetta.Kinetta
PREFIX ?= $(HOME)/.local
RUFF ?= ruff

.PHONY: run lint check install uninstall

run:
	PYTHONPATH=src python3 -m kinetta

lint:
	$(RUFF) check src

check:
	python3 -m compileall -q src

install:
	install -d $(PREFIX)/bin $(PREFIX)/share/kinetta
	rm -rf $(PREFIX)/share/kinetta/kinetta
	cp -r src/kinetta $(PREFIX)/share/kinetta/
	find $(PREFIX)/share/kinetta -name __pycache__ -type d -exec rm -rf {} +
	install -Dm755 packaging/kinetta $(PREFIX)/bin/kinetta
	install -Dm644 data/$(APP_ID).desktop $(PREFIX)/share/applications/$(APP_ID).desktop
	install -Dm644 data/$(APP_ID).metainfo.xml $(PREFIX)/share/metainfo/$(APP_ID).metainfo.xml
	install -Dm644 data/icons/$(APP_ID).svg $(PREFIX)/share/icons/hicolor/scalable/apps/$(APP_ID).svg
	-update-desktop-database $(PREFIX)/share/applications
	-gtk-update-icon-cache -qtf $(PREFIX)/share/icons/hicolor
	@echo "Instalado en $(PREFIX). Ejecuta: kinetta"

uninstall:
	rm -f $(PREFIX)/bin/kinetta
	rm -rf $(PREFIX)/share/kinetta
	rm -f $(PREFIX)/share/applications/$(APP_ID).desktop
	rm -f $(PREFIX)/share/metainfo/$(APP_ID).metainfo.xml
	rm -f $(PREFIX)/share/icons/hicolor/scalable/apps/$(APP_ID).svg
	-update-desktop-database $(PREFIX)/share/applications
	-gtk-update-icon-cache -qtf $(PREFIX)/share/icons/hicolor
	@echo "Desinstalado de $(PREFIX)"
