"""アプリアイコンの描画。ウィンドウ用アイコンと EXE 用 .ico の両方で共有する。"""

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (
    QBrush,
    QColor,
    QIcon,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPixmap,
)

ICON_SIZES = (16, 24, 32, 48, 64, 128, 256)


def draw_icon(size: int) -> QImage:
    image = QImage(size, size, QImage.Format.Format_ARGB32)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    s = float(size)

    background = QLinearGradient(QPointF(0, 0), QPointF(0, s))
    background.setColorAt(0.0, QColor("#3b82f6"))
    background.setColorAt(1.0, QColor("#1e40af"))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QBrush(background))
    painter.drawRoundedRect(QRectF(s * 0.04, s * 0.04, s * 0.92, s * 0.92), s * 0.2, s * 0.2)

    # 文書（白い紙）
    page = QPainterPath()
    page.addRoundedRect(QRectF(s * 0.22, s * 0.16, s * 0.56, s * 0.68), s * 0.06, s * 0.06)
    painter.setBrush(QColor("#ffffff"))
    painter.drawPath(page)

    # ハイライトされた行（オレンジ）と通常の行（グレー）
    painter.setBrush(QColor("#fdba74"))
    painter.drawRoundedRect(QRectF(s * 0.29, s * 0.27, s * 0.42, s * 0.09), s * 0.03, s * 0.03)
    painter.setBrush(QColor("#93c5fd"))
    painter.drawRoundedRect(QRectF(s * 0.29, s * 0.42, s * 0.42, s * 0.07), s * 0.03, s * 0.03)
    painter.setBrush(QColor("#cbd5e1"))
    painter.drawRoundedRect(QRectF(s * 0.29, s * 0.55, s * 0.30, s * 0.07), s * 0.03, s * 0.03)

    # 鉛筆（右下、crafter を表現）
    painter.save()
    painter.translate(s * 0.70, s * 0.72)
    painter.rotate(-45)
    body = QRectF(-s * 0.05, -s * 0.30, s * 0.10, s * 0.30)
    painter.setBrush(QColor("#facc15"))
    painter.drawRect(body)
    tip = QPainterPath()
    tip.moveTo(-s * 0.05, 0)
    tip.lineTo(s * 0.05, 0)
    tip.lineTo(0, s * 0.10)
    tip.closeSubpath()
    painter.setBrush(QColor("#fde68a"))
    painter.drawPath(tip)
    painter.setBrush(QColor("#0f172a"))
    lead = QPainterPath()
    lead.moveTo(-s * 0.017, s * 0.066)
    lead.lineTo(s * 0.017, s * 0.066)
    lead.lineTo(0, s * 0.10)
    lead.closeSubpath()
    painter.drawPath(lead)
    painter.setBrush(QColor("#f87171"))
    painter.drawRect(QRectF(-s * 0.05, -s * 0.34, s * 0.10, s * 0.05))
    painter.restore()

    painter.end()
    return image


def make_icon() -> QIcon:
    icon = QIcon()
    for size in ICON_SIZES:
        icon.addPixmap(QPixmap.fromImage(draw_icon(size)))
    return icon
