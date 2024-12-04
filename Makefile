.PHONY: build clean

build: dance.py
	pyinstaller --onefile --add-data "static:static" --icon="assets/favicon.ico" dance.py -n JoyDance

clean:
	rm -rf build dist
