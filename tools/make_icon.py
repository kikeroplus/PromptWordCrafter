"""assets/icon.ico を生成する（EXE ビルド用）。使い方: python tools/make_icon.py"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PIL import Image
from PySide6.QtCore import QBuffer, QIODevice
from PySide6.QtGui import QGuiApplication

from promptwordcrafter.icon import ICON_SIZES, draw_icon


def qimage_to_pil(qimage) -> Image.Image:
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    qimage.save(buffer, "PNG")
    from io import BytesIO

    return Image.open(BytesIO(bytes(buffer.data()))).convert("RGBA")


def main():
    app = QGuiApplication(sys.argv)  # noqa: F841 (QPainter/QFont の初期化に必要)
    output = ROOT / "assets" / "icon.ico"
    output.parent.mkdir(exist_ok=True)

    largest = qimage_to_pil(draw_icon(max(ICON_SIZES)))
    largest.save(output, format="ICO", sizes=[(size, size) for size in ICON_SIZES])
    print(f"saved {output}")


if __name__ == "__main__":
    main()
