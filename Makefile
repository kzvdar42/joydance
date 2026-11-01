.PHONY: build clean

PYTHON ?= python

build: dance.py
	"$(PYTHON)" -m PyInstaller --onefile --add-data "static:static" --icon="assets/favicon.ico" --name JoyDance dance.py

clean:
	rm -rf build dist
