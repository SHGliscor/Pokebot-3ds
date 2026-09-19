
from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtNetwork import QNetworkAccessManager, QNetworkRequest, QNetworkReply
from PySide6.QtWidgets import (
    QFrame, QVBoxLayout, QLabel, QHBoxLayout, QWidget, QPushButton, QGridLayout
)

class Panel(QFrame):
    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self.setObjectName("Panel")
        self.outer = QVBoxLayout(self)
        self.outer.setContentsMargins(9, 6, 9, 8)
        self.outer.setSpacing(5)
        self.title_label = QLabel(title)
        self.title_label.setObjectName("PanelTitle")
        self.outer.addWidget(self.title_label)

class DotLabel(QLabel):
    def __init__(self, color="#86e1ff", diameter=10, parent=None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self.setStyleSheet(
            f"background:{color};border:1px solid #c9f6ff;"
            f"border-radius:{diameter//2}px;"
        )

class DataPair(QWidget):
    def __init__(self, key, value="—", key_width=94, parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        self.k = QLabel(key)
        self.k.setObjectName("FieldLabel")
        self.k.setFixedWidth(key_width)
        self.v = QLabel(value)
        self.v.setObjectName("Value")
        row.addWidget(self.k)
        row.addWidget(self.v, 1)

class StarterRow(QWidget):
    def __init__(self, key, text, color="#86e1ff", parent=None):
        super().__init__(parent)
        self.key = key
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 1, 0, 1)
        row.setSpacing(7)
        self.dot = DotLabel(color=color, diameter=10)
        self.text = QLabel(text)
        self.text.setObjectName("Value")
        self.button = QPushButton("Select")
        self.button.setObjectName("SmallAction")
        self.button.setMinimumWidth(65)
        row.addWidget(self.dot)
        row.addWidget(self.text, 1)
        row.addWidget(self.button)


class SpriteLoader(QWidget):
    """Small async ORAS sprite cache for party cards.

    Images are presentation-only. Failure to download an image leaves the
    textual party data intact.
    """
    sprite_ready = Signal(int, object)

    def __init__(self, cache_dir, parent=None):
        super().__init__(parent)
        from pathlib import Path
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.manager = QNetworkAccessManager(self)
        self.manager.finished.connect(self._finished)
        self.pending = {}
        self.download_enabled = True

    def set_download_enabled(self, enabled):
        self.download_enabled = bool(enabled)

    def request_sprite(self, slot, species_id, shiny=False):
        if not species_id:
            self.sprite_ready.emit(slot, QPixmap())
            return

        suffix = "-shiny" if shiny else ""
        path = self.cache_dir / f"{species_id}{suffix}.png"
        if path.exists():
            pix = QPixmap(str(path))
            self.sprite_ready.emit(slot, pix)
            return

        if not self.download_enabled:
            self.sprite_ready.emit(slot, QPixmap())
            return

        # ORAS front sprites from PokeAPI's public sprite repository.
        base = (
            "https://raw.githubusercontent.com/PokeAPI/sprites/master/"
            "sprites/pokemon/versions/generation-vi/"
            "omegaruby-alphasapphire/"
        )
        filename = f"shiny/{species_id}.png" if shiny else f"{species_id}.png"
        reply = self.manager.get(QNetworkRequest(QUrl(base + filename)))
        self.pending[reply] = (slot, path)

    def _finished(self, reply):
        slot, path = self.pending.pop(reply, (-1, None))
        pix = QPixmap()

        if slot >= 0 and reply.error() == QNetworkReply.NoError:
            data = bytes(reply.readAll())
            if data:
                try:
                    path.write_bytes(data)
                except Exception:
                    pass
                pix.loadFromData(data)

        if slot >= 0:
            self.sprite_ready.emit(slot, pix)

        reply.deleteLater()


class PartyCard(QFrame):
    def __init__(self, slot, parent=None):
        super().__init__(parent)
        self.slot = slot
        self.setObjectName("PartyCard")
        self.setMinimumHeight(50)
        self.setMaximumHeight(54)
        self.setMouseTracking(True)

        row = QHBoxLayout(self)
        row.setContentsMargins(6, 4, 6, 4)
        row.setSpacing(6)

        self.image = QLabel("—")
        self.image.setObjectName("PartyImage")
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setFixedSize(40, 40)
        row.addWidget(self.image)

        text = QVBoxLayout()
        text.setSpacing(0)
        self.slot_label = QLabel(f"Slot {slot}")
        self.slot_label.setObjectName("PartySlot")
        self.name_label = QLabel("Empty")
        self.name_label.setObjectName("PartyName")
        self.gender_label = QLabel("—")
        self.gender_label.setObjectName("Muted")
        text.addWidget(self.slot_label)
        text.addWidget(self.name_label)
        text.addWidget(self.gender_label)
        text.addStretch(1)
        row.addLayout(text, 1)

        self.set_party({
            "slot": slot,
            "species": "Empty",
            "species_id": 0,
            "nature": "—",
            "gender": "—",
            "ivs": {},
            "evs": {},
            "hidden_power": "—",
            "pokerus": "—",
            "sv": None,
            "shiny": False,
        })

    @staticmethod
    def _spread(values):
        values = values or {}
        return (
            f"HP {values.get('hp','—')}  /  "
            f"Atk {values.get('attack','—')}  /  "
            f"Def {values.get('defense','—')}  /  "
            f"SpA {values.get('sp_attack','—')}  /  "
            f"SpD {values.get('sp_defense','—')}  /  "
            f"Spe {values.get('speed','—')}"
        )

    def set_party(self, mon):
        species = str(mon.get("species", "Empty"))
        gender = str(mon.get("gender", "—"))
        shiny = bool(mon.get("shiny"))
        evolution = str(mon.get("predicted_evolution") or "").strip()

        self.slot_label.setText(f"Slot {self.slot}")
        self.name_label.setText(("✨ " if shiny else "") + species)
        detail = gender if species != "Empty" else "—"
        if evolution and species != "Empty":
            detail = f"{gender} • {evolution}"
        self.gender_label.setText(detail)

        if species == "Empty":
            self.image.setPixmap(QPixmap())
            self.image.setText("—")
            self.setToolTip(f"<b>Slot {self.slot}</b><br>Empty")
            return

        sv = mon.get("sv")
        sv_text = "—" if sv is None else str(sv)
        tooltip = (
            f"<b>{species}</b> &nbsp; {gender}<br>"
            f"<b>Nature:</b> {mon.get('nature','—')}<br>"
            f"<b>Hidden Power:</b> {mon.get('hidden_power','—')}<br>"
            f"<b>IVs:</b> {self._spread(mon.get('ivs'))}<br>"
            f"<b>EVs:</b> {self._spread(mon.get('evs'))}<br>"
            f"<b>Pokérus:</b> {mon.get('pokerus','—')}<br>"
            + (f"<b>Evolution:</b> {evolution}<br>" if evolution else "")
            + f"<b>SV:</b> {sv_text}"
        )
        self.setToolTip(tooltip)

    def set_sprite(self, pixmap):
        if pixmap is not None and not pixmap.isNull():
            self.image.setText("")
            self.image.setPixmap(
                pixmap.scaled(
                    38, 38,
                    Qt.KeepAspectRatio,
                    Qt.SmoothTransformation,
                )
            )
        else:
            if self.name_label.text() not in ("Empty", ""):
                self.image.setText("—")
