
from PySide6.QtWidgets import QWidget, QVBoxLayout, QLabel, QFrame

class PlaceholderPage(QWidget):
    def __init__(self, title, subtitle, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(9, 9, 9, 9)

        frame = QFrame()
        frame.setObjectName("Panel")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(16, 14, 16, 14)
        lay.setSpacing(9)

        t = QLabel(title)
        t.setStyleSheet("font-size:16pt;font-weight:900;color:#eef6fd;")
        s = QLabel(subtitle)
        s.setObjectName("Muted")
        s.setWordWrap(True)
        detail = QLabel(
            "The live starter backend is intentionally concentrated in the Dashboard first. "
            "These tabs will be populated without changing the locked starter modules."
        )
        detail.setWordWrap(True)
        lay.addWidget(t)
        lay.addWidget(s)
        lay.addWidget(detail)
        lay.addStretch(1)

        outer.addWidget(frame, 1)
