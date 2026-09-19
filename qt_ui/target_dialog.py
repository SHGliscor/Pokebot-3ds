from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QGridLayout,
    QHBoxLayout, QLabel, QSpinBox, QVBoxLayout, QWidget, QLineEdit,
    QListWidget, QListWidgetItem, QAbstractItemView, QTabWidget,
)

from pokebot.common.pk6 import NATURE_NAMES
from pokebot.common.target_filter import HP_TYPES, IV_STATS, normalize_target
from pokebot.common.species_names import SPECIES_NAMES


class TargetDialog(QDialog):
    def __init__(self, criteria, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Look for Target")
        self.setModal(True)
        self.setMinimumWidth(520)
        self._criteria = normalize_target(criteria)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        intro = QLabel(
            "Set only the traits you care about. IVs use inclusive ranges; set Min = Max "
            "for an exact IV. Hidden Power is always base power 60 in Gen VI."
        )
        intro.setWordWrap(True)
        intro.setObjectName("Muted")
        root.addWidget(intro)

        tabs = QTabWidget()
        basics = QWidget()
        basics_layout = QVBoxLayout(basics)
        basics_layout.setContentsMargins(8, 8, 8, 8)
        basics_layout.setSpacing(9)

        species_title = QLabel("Species")
        species_title.setObjectName("FieldLabel")
        basics_layout.addWidget(species_title)
        species_note = QLabel("Optional. Select one or more Pokémon; an empty selection means any species. Real shinies still use the normal absolute shiny HOLD regardless of this filter.")
        species_note.setWordWrap(True)
        species_note.setObjectName("Muted")
        basics_layout.addWidget(species_note)
        self.species_search = QLineEdit()
        self.species_search.setPlaceholderText("Search species…")
        basics_layout.addWidget(self.species_search)
        self.species_list = QListWidget()
        self.species_list.setSelectionMode(QAbstractItemView.MultiSelection)
        self.species_list.setMinimumHeight(170)
        selected_species = {int(x) for x in self._criteria.get("species", [])}
        for sid, name in sorted(SPECIES_NAMES.items(), key=lambda item: item[1].lower()):
            if not (1 <= int(sid) <= 721):
                continue
            item = QListWidgetItem(f"{name}  #{int(sid):03d}")
            item.setData(Qt.UserRole, int(sid))
            self.species_list.addItem(item)
            if int(sid) in selected_species:
                item.setSelected(True)
        self.species_search.textChanged.connect(self._filter_species)
        basics_layout.addWidget(self.species_list)

        top = QGridLayout()
        top.setHorizontalSpacing(10)
        top.setVerticalSpacing(7)

        self.nature = QComboBox()
        self.nature.addItem("Any")
        self.nature.addItems(list(NATURE_NAMES))
        self._set_combo(self.nature, self._criteria["nature"])

        self.shiny = QComboBox()
        self.shiny.addItems(["Any", "Shiny", "Non-shiny"])
        self._set_combo(self.shiny, self._criteria["shiny"])

        self.gender = QComboBox()
        self.gender.addItems(["Any", "Male", "Female", "Genderless"])
        self._set_combo(self.gender, self._criteria["gender"])

        self.hp_type = QComboBox()
        self.hp_type.addItem("Any")
        self.hp_type.addItems(list(HP_TYPES))
        self._set_combo(self.hp_type, self._criteria["hidden_power_type"])

        top.addWidget(QLabel("Nature"), 0, 0)
        top.addWidget(self.nature, 0, 1)
        top.addWidget(QLabel("Shininess"), 0, 2)
        top.addWidget(self.shiny, 0, 3)
        top.addWidget(QLabel("Gender"), 1, 0)
        top.addWidget(self.gender, 1, 1)
        top.addWidget(QLabel("Hidden Power"), 1, 2)
        top.addWidget(self.hp_type, 1, 3)
        hp_power = QLabel("Power 60 (fixed in Gen VI)")
        hp_power.setObjectName("Muted")
        top.addWidget(hp_power, 2, 2, 1, 2)
        basics_layout.addLayout(top)
        basics_layout.addStretch(1)
        tabs.addTab(basics, "Species & traits")

        iv_page = QWidget()
        iv_page_layout = QVBoxLayout(iv_page)
        iv_page_layout.setContentsMargins(8, 8, 8, 8)
        iv_page_layout.addWidget(QLabel("IV ranges"))
        iv_grid = QGridLayout()
        iv_grid.setHorizontalSpacing(8)
        iv_grid.setVerticalSpacing(5)
        iv_grid.addWidget(QLabel("Stat"), 0, 0)
        iv_grid.addWidget(QLabel("Min"), 0, 1)
        iv_grid.addWidget(QLabel("Max"), 0, 2)

        labels = {
            "hp": "HP", "attack": "Attack", "defense": "Defense",
            "sp_attack": "Sp. Atk", "sp_defense": "Sp. Def", "speed": "Speed",
        }
        self.iv_min = {}
        self.iv_max = {}
        for row, stat in enumerate(IV_STATS, start=1):
            lo = QSpinBox()
            hi = QSpinBox()
            for box in (lo, hi):
                box.setRange(0, 31)
            lo.setValue(int(self._criteria["ivs"][stat]["min"]))
            hi.setValue(int(self._criteria["ivs"][stat]["max"]))
            lo.valueChanged.connect(lambda value, s=stat: self._keep_order(s, "min", value))
            hi.valueChanged.connect(lambda value, s=stat: self._keep_order(s, "max", value))
            self.iv_min[stat] = lo
            self.iv_max[stat] = hi
            iv_grid.addWidget(QLabel(labels[stat]), row, 0)
            iv_grid.addWidget(lo, row, 1)
            iv_grid.addWidget(hi, row, 2)
        iv_wrap = QWidget()
        iv_wrap.setLayout(iv_grid)
        iv_page_layout.addWidget(iv_wrap)
        iv_page_layout.addStretch(1)
        tabs.addTab(iv_page, "IV ranges")
        root.addWidget(tabs, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)

    @staticmethod
    def _set_combo(combo, value):
        idx = combo.findText(str(value))
        combo.setCurrentIndex(idx if idx >= 0 else 0)

    def _keep_order(self, stat, which, value):
        if which == "min" and value > self.iv_max[stat].value():
            self.iv_max[stat].setValue(value)
        elif which == "max" and value < self.iv_min[stat].value():
            self.iv_min[stat].setValue(value)

    def _filter_species(self, text):
        query = str(text or "").strip().lower()
        for row in range(self.species_list.count()):
            item = self.species_list.item(row)
            item.setHidden(bool(query and query not in item.text().lower()))

    def criteria(self):
        data = deepcopy(self._criteria)
        data["species"] = [
            int(item.data(Qt.UserRole))
            for item in self.species_list.selectedItems()
        ]
        data["nature"] = self.nature.currentText()
        data["shiny"] = self.shiny.currentText()
        data["gender"] = self.gender.currentText()
        data["hidden_power_type"] = self.hp_type.currentText()
        data["hidden_power_power"] = 60
        data["ivs"] = {
            stat: {
                "min": self.iv_min[stat].value(),
                "max": self.iv_max[stat].value(),
            }
            for stat in IV_STATS
        }
        return normalize_target(data)
