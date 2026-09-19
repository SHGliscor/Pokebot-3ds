from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QComboBox,
    QLineEdit, QListWidget, QListWidgetItem, QScrollArea, QFrame,
    QSizePolicy, QPushButton, QCheckBox,
)

from .widgets import Panel, SpriteLoader
from .appdata_store import (
    ProfilePaths,
    load_shiny_blocklist,
    set_species_shiny_block,
)


GAME_NAMES = {
    "alpha_sapphire": "Alpha Sapphire",
    "omega_ruby": "Omega Ruby",
    "pokemon_x": "Pokémon X",
    "pokemon_y": "Pokémon Y",
}


class EncounterCard(QFrame):
    hunt_requested = Signal(dict)
    shiny_block_changed = Signal(int, str, bool)

    def __init__(
        self,
        record,
        shiny_total=0,
        hunt_selection=None,
        shiny_blocked=False,
        blocklist_available=True,
        parent=None,
    ):
        super().__init__(parent)
        self.record = dict(record)
        self.setObjectName("EncounterCard")
        self.setMinimumWidth(285)
        self.setMinimumHeight(170)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 7, 8, 7)
        outer.setSpacing(4)

        title = QLabel(self.record.get("species_name", "Unknown"))
        title.setObjectName("EncounterName")
        outer.addWidget(title)

        images = QHBoxLayout()
        images.setSpacing(8)

        self.normal_image = self._image_box("NORMAL")
        self.shiny_image = self._image_box("SHINY")
        images.addWidget(self.normal_image[0])
        images.addWidget(self.shiny_image[0])
        images.addStretch(1)
        outer.addLayout(images)

        min_level = int(self.record.get("min_level", 0))
        max_level = int(self.record.get("max_level", 0))
        levels = f"Lv. {min_level}" if min_level == max_level else f"Lv. {min_level}–{max_level}"
        form = int(self.record.get("form", 0))
        detail_bits = [levels]
        if self.record.get("gift"):
            detail_bits.append("Gift / starter choice")
        else:
            detail_bits.append(f"Slots {int(self.record.get('slot_count', 0))}")
        if form:
            detail_bits.append(f"Form {form}")
        if self.record.get("gift"):
            detail_bits.append(
                "Automation ready"
                if self.record.get("automation_ready")
                else "Not yet wired"
            )
        details = QLabel("  •  ".join(detail_bits))
        details.setObjectName("Muted")
        outer.addWidget(details)

        found = QLabel(f"Shinies Found: {int(shiny_total)}")
        found.setObjectName("ShinyFoundCount")
        outer.addWidget(found)

        self.block_checkbox = None
        if bool(blocklist_available) and not bool(self.record.get("gift")):
            self.block_checkbox = QCheckBox("Run from shiny")
            self.block_checkbox.setChecked(bool(shiny_blocked))
            self.block_checkbox.setToolTip(
                "Shiny Blocklist: ON = if RAM confirms a shiny of this species, "
                "Pokebot still records/counts it, then automatically Runs instead "
                "of entering SHINY HOLD. Changes apply to an active Wild hunt "
                "without restarting Pokebot."
            )
            self.block_checkbox.toggled.connect(
                lambda checked: self.shiny_block_changed.emit(
                    int(self.record.get("species", 0)),
                    str(
                        self.record.get("species_name")
                        or f"Species #{int(self.record.get('species', 0))}"
                    ),
                    bool(checked),
                )
            )
            outer.addWidget(self.block_checkbox)

        self.hunt_selection = dict(hunt_selection or {})
        if self.hunt_selection:
            self.hunt_btn = QPushButton("HUNT THIS POKÉMON")
            self.hunt_btn.setObjectName("SmallAction")
            self.hunt_btn.setToolTip(
                "Send this standard land target to the Dashboard. "
                "The live game/location/terrain are still RAM-validated before movement."
            )
            self.hunt_btn.clicked.connect(
                lambda: self.hunt_requested.emit(dict(self.hunt_selection))
            )
            outer.addWidget(self.hunt_btn)

    @staticmethod
    def _image_box(caption):
        frame = QFrame()
        frame.setObjectName("EncounterImageFrame")
        box = QVBoxLayout(frame)
        box.setContentsMargins(3, 3, 3, 2)
        box.setSpacing(1)
        image = QLabel("—")
        image.setObjectName("EncounterImage")
        image.setAlignment(Qt.AlignCenter)
        image.setFixedSize(62, 62)
        label = QLabel(caption)
        label.setObjectName("EncounterImageCaption")
        label.setAlignment(Qt.AlignCenter)
        box.addWidget(image)
        box.addWidget(label)
        return frame, image

    def set_normal_pixmap(self, pixmap):
        self._set_pixmap(self.normal_image[1], pixmap)

    def set_shiny_pixmap(self, pixmap):
        self._set_pixmap(self.shiny_image[1], pixmap)

    @staticmethod
    def _set_pixmap(label, pixmap):
        if isinstance(pixmap, QPixmap) and not pixmap.isNull():
            label.setPixmap(
                pixmap.scaled(
                    58, 58, Qt.KeepAspectRatio, Qt.SmoothTransformation
                )
            )
            label.setText("")
        else:
            label.setPixmap(QPixmap())
            label.setText("—")


class HuntPage(QWidget):
    hunt_selected = Signal(dict)
    shiny_block_changed = Signal(dict)

    """Route/location encounter browser.

    The former statistics-management content has moved to Settings. HUNTS is
    now reserved for browsing actual Generation VI encounter tables by location
    and encounter method/environment. ORAS and X/Y datasets remain separate.
    """

    def __init__(
        self,
        profile_root,
        base_dir=None,
        download_sprites=True,
        parent=None,
    ):
        super().__init__(parent)
        self.profile_root = Path(profile_root)
        self.profile = ProfilePaths(self.profile_root)
        self.base_dir = Path(base_dir or Path(__file__).resolve().parents[1])
        self.data_path = self.base_dir / "data" / "oras_encounters.json"
        self.xy_data_path = self.base_dir / "data" / "xy_encounters.json"
        self.dataset = self._load_dataset()
        self.detected_game_key = None
        self._visible_locations = []
        self._sprite_targets = {}
        self._next_sprite_token = 1
        self._encounter_cards = []

        self.sprite_loader = SpriteLoader(
            self.profile_root / "cache" / "oras_sprites",
            self,
        )
        self.sprite_loader.set_download_enabled(bool(download_sprites))
        self.sprite_loader.sprite_ready.connect(self._sprite_ready)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(7)

        header = Panel("GEN 6 ENCOUNTER BROWSER")
        hrow = QHBoxLayout()
        hrow.setSpacing(8)

        hrow.addWidget(QLabel("Game"))
        self.game_combo = QComboBox()
        self.game_combo.addItem("Auto — waiting for GAME_INFO", "auto")
        self.game_combo.setEnabled(False)
        self.game_combo.setToolTip(
            "Hunts is locked to the game identified by GAME_INFO so ORAS and X/Y data can never be mixed."
        )
        hrow.addWidget(self.game_combo)

        hrow.addWidget(QLabel("Find Route / Pokémon"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Route 2, Santalune Forest, Bunnelby…")
        hrow.addWidget(self.search, 1)
        header.outer.addLayout(hrow)

        self.game_status = QLabel("")
        self.game_status.setObjectName("Muted")
        header.outer.addWidget(self.game_status)
        outer.addWidget(header)

        body = QHBoxLayout()
        body.setSpacing(7)

        left = Panel("LOCATIONS")
        self.location_list = QListWidget()
        self.location_list.setObjectName("EncounterLocationList")
        self.location_list.setMinimumWidth(220)
        self.location_list.setMaximumWidth(290)
        left.outer.addWidget(self.location_list, 1)
        body.addWidget(left, 0)

        self.detail_panel = Panel("AVAILABLE POKÉMON")
        self.detail_header = QLabel("Select a location")
        self.detail_header.setObjectName("EncounterLocationHeader")
        self.detail_panel.outer.addWidget(self.detail_header)

        self.blocklist_note = QLabel(
            "SHINY BLOCKLIST — check “Run from shiny” beside a species to "
            "record/count that RAM-confirmed shiny but automatically escape it "
            "instead of HOLD. Changes apply live during an active Wild hunt."
        )
        self.blocklist_note.setObjectName("Muted")
        self.blocklist_note.setWordWrap(True)
        self.detail_panel.outer.addWidget(self.blocklist_note)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll_content = QWidget()
        self.sections_layout = QVBoxLayout(self.scroll_content)
        self.sections_layout.setContentsMargins(0, 0, 0, 0)
        self.sections_layout.setSpacing(7)
        self.sections_layout.addStretch(1)
        self.scroll.setWidget(self.scroll_content)
        self.detail_panel.outer.addWidget(self.scroll, 1)
        body.addWidget(self.detail_panel, 1)

        outer.addLayout(body, 1)

        self.game_combo.currentIndexChanged.connect(self._game_changed)
        self.search.textChanged.connect(self._rebuild_location_list)
        self.location_list.currentItemChanged.connect(self._location_changed)

        self._rebuild_location_list()

    def _load_dataset(self):
        merged = {"games": {}}
        for path in (self.data_path, self.xy_data_path):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not isinstance(data, dict):
                continue
            games = data.get("games") or {}
            if isinstance(games, dict):
                merged["games"].update(games)
        return merged

    def set_detected_game(self, game_profile):
        key = None
        if isinstance(game_profile, dict):
            candidate = str(game_profile.get("key") or "")
            if candidate in GAME_NAMES:
                key = candidate

        changed = key != self.detected_game_key
        self.detected_game_key = key
        label = (
            f"Auto — {GAME_NAMES[key]}" if key in GAME_NAMES
            else "Auto — waiting for GAME_INFO"
        )
        self.game_combo.setItemText(0, label)

        # A game-family transition is authoritative. Never preserve a route or
        # encounter selection from the previous game, even when names happen to
        # overlap. This prevents ORAS/XY data from being mixed in Hunts.
        self._rebuild_location_list(
            preserve_name=None if changed else self.current_location_name(),
            force_clear=changed,
        )

    def set_download_sprites(self, enabled):
        self.sprite_loader.set_download_enabled(bool(enabled))

    def effective_game_key(self):
        # GAME_INFO is the sole game authority. Do not silently fall back to
        # Alpha Sapphire while disconnected; an empty browser is safer than
        # displaying encounter data for the wrong game.
        if self.detected_game_key in GAME_NAMES:
            return self.detected_game_key
        return None

    def current_location_name(self):
        item = self.location_list.currentItem()
        if item is None:
            return None
        rec = item.data(Qt.UserRole)
        return rec.get("name") if isinstance(rec, dict) else None

    def _update_game_status(self):
        effective = self.effective_game_key()
        if effective:
            family = "X/Y" if effective in {"pokemon_x", "pokemon_y"} else "ORAS"
            self.game_status.setText(
                f"GAME_INFO: {GAME_NAMES[effective]} • {family} encounter database only • stale cross-game selections cleared automatically."
            )
        else:
            self.game_status.setText(
                "Waiting for GAME_INFO • no encounter database is shown until the connected Gen 6 game is identified."
            )

    def _game_changed(self):
        self._rebuild_location_list()

    def _locations_for_game(self):
        key = self.effective_game_key()
        if not key:
            return []
        game = (self.dataset.get("games") or {}).get(key, {})
        return list(game.get("locations") or [])

    def _rebuild_location_list(self, preserve_name=None, force_clear=False):
        if preserve_name is None and not force_clear:
            preserve_name = self.current_location_name()
        query = self.search.text().strip().lower() if hasattr(self, "search") else ""
        locations = []
        for loc in self._locations_for_game():
            if not loc.get("has_encounters"):
                continue
            haystack = [str(loc.get("name") or "")]
            for section in loc.get("sections") or []:
                haystack.append(str(section.get("title") or ""))
                haystack.append(str(section.get("note") or ""))
                haystack.append(str(section.get("unlock") or ""))
                haystack.extend(
                    str(p.get("species_name") or "")
                    for p in section.get("pokemon") or []
                )
            if query and query not in " ".join(haystack).lower():
                continue
            locations.append(loc)

        self._visible_locations = locations
        self.location_list.blockSignals(True)
        self.location_list.clear()
        target_row = -1
        for row, loc in enumerate(locations):
            item = QListWidgetItem(loc.get("name") or "Unknown")
            item.setData(Qt.UserRole, loc)
            self.location_list.addItem(item)
            if preserve_name and loc.get("name") == preserve_name:
                target_row = row
        self.location_list.blockSignals(False)

        if locations:
            self.location_list.setCurrentRow(target_row if target_row >= 0 else 0)
            self._render_location(self.location_list.currentItem().data(Qt.UserRole))
        else:
            self._clear_sections()
            self.detail_header.setText("No matching encounter locations")
        self._update_game_status()

    def _location_changed(self, current, previous=None):
        if current is None:
            return
        rec = current.data(Qt.UserRole)
        if isinstance(rec, dict):
            self._render_location(rec)

    def _blocked_species(self):
        try:
            data = load_shiny_blocklist(self.profile)
            return {
                int(key)
                for key, value in (data.get("species") or {}).items()
                if isinstance(value, dict) and bool(value.get("enabled"))
            }
        except Exception:
            # UI fail-safe mirrors worker fail-safe: an unavailable blocklist
            # never silently changes normal shiny HOLD behaviour.
            return set()

    def _set_species_shiny_block(self, species, species_name, enabled):
        species = int(species)
        if species <= 0:
            return
        try:
            set_species_shiny_block(
                species,
                species_name,
                bool(enabled),
                self.profile,
            )
        except Exception:
            # Re-render from persisted state if the atomic write failed.
            item = self.location_list.currentItem()
            if item is not None:
                rec = item.data(Qt.UserRole)
                if isinstance(rec, dict):
                    self._render_location(rec)
            return

        # Synchronize duplicate cards for the same species in the currently
        # visible location without rebuilding the page or restarting a hunt.
        for card in list(self._encounter_cards):
            try:
                if int(card.record.get("species", 0)) != species:
                    continue
                checkbox = getattr(card, "block_checkbox", None)
                if checkbox is None:
                    continue
                checkbox.blockSignals(True)
                checkbox.setChecked(bool(enabled))
                checkbox.blockSignals(False)
            except Exception:
                continue

        self.shiny_block_changed.emit({
            "species": species,
            "species_name": str(species_name),
            "enabled": bool(enabled),
            "action": "RUN" if enabled else "HOLD",
        })

    def _load_shiny_totals(self):
        path = self.profile_root / "stats" / "species_shiny_totals.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def refresh_shiny_totals(self):
        item = self.location_list.currentItem()
        if item is not None:
            rec = item.data(Qt.UserRole)
            if isinstance(rec, dict):
                self._render_location(rec)

    def _clear_sections(self):
        self._sprite_targets.clear()
        self._encounter_cards.clear()
        while self.sections_layout.count():
            item = self.sections_layout.takeAt(0)
            widget = item.widget()
            layout = item.layout()
            if widget is not None:
                widget.deleteLater()
            elif layout is not None:
                self._delete_layout(layout)

    @staticmethod
    def _delete_layout(layout):
        while layout.count():
            item = layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
            elif item.layout() is not None:
                HuntPage._delete_layout(item.layout())

    def _render_location(self, loc):
        self._clear_sections()
        name = str(loc.get("name") or "Unknown")
        game_key = self.effective_game_key()
        game_name = GAME_NAMES.get(game_key, "No game detected")
        self.detail_header.setText(f"{name}  •  {game_name}")
        totals = self._load_shiny_totals()
        blocked_species = self._blocked_species()

        for section in loc.get("sections") or []:
            pokemon = list(section.get("pokemon") or [])
            if not pokemon:
                continue
            panel = Panel(section.get("title") or section.get("key") or "Encounters")
            note = str(section.get("note") or "").strip()
            summary = QLabel(
                note
                if note
                else f"{len(pokemon)} Pokémon available • kept separate from other hunt types on this location"
            )
            summary.setWordWrap(True)
            summary.setObjectName("Muted")
            panel.outer.addWidget(summary)

            grid = QGridLayout()
            grid.setHorizontalSpacing(7)
            grid.setVerticalSpacing(7)
            for i, rec in enumerate(pokemon):
                species = int(rec.get("species", 0))
                shiny_total = int((totals.get(str(species)) or {}).get("total", 0))

                # Grass, Cave and Surf pools are selectable. Surf/Ocean is
                # v0p40a hardware-test automation and requires the player to
                # already be Surfing before Start. DexNav, Horde encounter
                # tables, Rock Smash and rods remain browser-only here.
                section_key = str(section.get("key") or "")
                game_key_now = self.effective_game_key()
                oras_selectable = (
                    game_key_now in {"alpha_sapphire", "omega_ruby"}
                    and section_key in {"grass", "tall_grass", "surf"}
                )
                xy_selectable = (
                    game_key_now in {"pokemon_x", "pokemon_y"}
                    and section_key in {
                        "grass", "yellow_flowers", "purple_flowers",
                        "red_flowers", "rough_terrain",
                    }
                )
                selectable = bool(
                    (oras_selectable or xy_selectable)
                    and not bool(rec.get("gift"))
                )
                hunt_selection = None
                if selectable:
                    is_cave = str(section.get("title") or "").casefold() == "cave"
                    is_surf = section_key == "surf"
                    environment_hint = str(
                        loc.get("environment_hint") or "land"
                    ).casefold()
                    environment_name = (
                        "Ocean"
                        if is_surf and environment_hint == "ocean"
                        else ("Surf" if is_surf else str(section.get("title") or "Grass"))
                    )
                    hunt_selection = {
                        "hunt_type": "surf" if is_surf else ("cave" if is_cave else "wild"),
                        "game_key": self.effective_game_key(),
                        "game_name": GAME_NAMES[self.effective_game_key()],
                        "location_name": name,
                        "parent_map": loc.get("parent_map"),
                        "section_key": section_key,
                        "section_title": str(
                            section.get("title") or "Grass"
                        ),
                        "environment_hint": environment_hint,
                        "environment_name": environment_name,
                        "species": species,
                        "species_name": str(
                            rec.get("species_name")
                            or f"Species #{species}"
                        ),
                        "min_level": int(rec.get("min_level", 0)),
                        "max_level": int(rec.get("max_level", 0)),
                    }

                card = EncounterCard(
                    rec,
                    shiny_total=shiny_total,
                    hunt_selection=hunt_selection,
                    shiny_blocked=(species in blocked_species),
                    blocklist_available=(not bool(rec.get("gift"))),
                )
                card.shiny_block_changed.connect(
                    self._set_species_shiny_block
                )
                self._encounter_cards.append(card)
                if hunt_selection:
                    card.hunt_requested.connect(self.hunt_selected.emit)
                grid.addWidget(card, i // 2, i % 2)
                self._request_card_sprites(card, species)
            panel.outer.addLayout(grid)
            self.sections_layout.addWidget(panel)

        if not loc.get("sections"):
            empty = QLabel("No encounter data is present for this location.")
            empty.setObjectName("Muted")
            self.sections_layout.addWidget(empty)
        self.sections_layout.addStretch(1)
        self.scroll.verticalScrollBar().setValue(0)

    def _request_card_sprites(self, card, species):
        normal_token = self._next_sprite_token
        self._next_sprite_token += 1
        shiny_token = self._next_sprite_token
        self._next_sprite_token += 1
        self._sprite_targets[normal_token] = (card, False)
        self._sprite_targets[shiny_token] = (card, True)
        self.sprite_loader.request_sprite(normal_token, species, shiny=False)
        self.sprite_loader.request_sprite(shiny_token, species, shiny=True)

    def _sprite_ready(self, token, pixmap):
        target = self._sprite_targets.pop(int(token), None)
        if not target:
            return
        card, shiny = target
        try:
            if shiny:
                card.set_shiny_pixmap(pixmap)
            else:
                card.set_normal_pixmap(pixmap)
        except RuntimeError:
            # Card may have been deleted after changing location while a network
            # request was in flight.
            pass
