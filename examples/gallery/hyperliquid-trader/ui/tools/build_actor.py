"""Build a self-contained UI actor with the static assets embedded as bytes."""

import pathlib

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
SOURCE = ROOT / "main.py"
DIST = ROOT / "dist"


def main():
    source = SOURCE.read_text()
    replacements = {
        'INDEX_HTML = b""': "INDEX_HTML = "
        + repr((ROOT / "hl-ui/index.html").read_bytes()),
        'STYLES_CSS = b""': "STYLES_CSS = "
        + repr((ROOT / "hl-ui/styles.css").read_bytes()),
        'APP_JS = b""': "APP_JS = " + repr((ROOT / "hl-ui/app.js").read_bytes()),
    }
    for marker, value in replacements.items():
        if marker not in source:
            raise RuntimeError("missing build marker: " + marker)
        source = source.replace(marker, value, 1)
    DIST.mkdir(exist_ok=True)
    output = DIST / "main.py"
    output.write_text(source)
    print("wrote %s (%d bytes)" % (output, output.stat().st_size))


if __name__ == "__main__":
    main()
