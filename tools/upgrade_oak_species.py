from pathlib import Path
p=Path('/mnt/data/hf99_work/Pokebot3DS-CFW_v0p43EJ_HF99_ORASProfessorOakChallenge_ORASChecklist_Unified3DS/qt_ui/oak_challenge_page.py')
s=p.read_text()
s=s.replace('from PySide6.QtWidgets import (\n    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QCheckBox,\n    QPushButton, QScrollArea, QFrame, QComboBox, QProgressBar, QLineEdit\n)', 'from PySide6.QtWidgets import (\n    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QCheckBox,\n    QPushButton, QScrollArea, QFrame, QComboBox, QProgressBar, QLineEdit,\n    QTabWidget, QGroupBox\n)')
marker='\n\nclass OakChallengePage(QWidget):\n'
insert=r'''

def _species_entries_for_version(version):
    """Build a species-level view from the researched route checklist.

    The guide contains grouped tasks, while the tracker is more useful when
    each Pokémon can be checked individually. We derive names from the bot's
    Gen 1-6 species table and retain the earliest challenge gate plus all guide
    sources. A few guide rows intentionally say "one starter" or "fossil";
    those are represented by explicit choice/fossil entries below.
    """
    from pokebot.common.species_names import SPECIES_NAMES
    segments = OAK_SEGMENTS_BY_VERSION[version]
    # Longest names first avoids accidental partial matches.
    names = [(int(i), n) for i, n in SPECIES_NAMES.items() if int(i) > 0]
    names.sort(key=lambda x: len(x[1]), reverse=True)
    found = {}
    for segment, (_target, tasks) in segments.items():
        for idx, task in enumerate(tasks):
            low = task.lower()
            for dex, name in names:
                if name.lower() in low:
                    rec = found.setdefault(name, {
                        "dex": dex, "sources": [], "gate": segment,
                    })
                    source = {"gate": segment, "task": task, "task_index": idx}
                    if source not in rec["sources"]:
                        rec["sources"].append(source)

    # The route guide deliberately uses shorthand for these choice groups.
    # Add them as selectable entries rather than silently treating every starter
    # as required in a single-game run.
    starter_groups = {
        "Hoenn starter": ["Treecko", "Grovyle", "Sceptile", "Torchic", "Combusken", "Blaziken", "Mudkip", "Marshtomp", "Swampert"],
        "Johto starter": ["Chikorita", "Bayleef", "Meganium", "Cyndaquil", "Quilava", "Typhlosion", "Totodile", "Croconaw", "Feraligatr"],
        "Unova starter": ["Snivy", "Servine", "Serperior", "Tepig", "Pignite", "Emboar", "Oshawott", "Dewott", "Samurott"],
        "Sinnoh starter": ["Turtwig", "Grotle", "Torterra", "Chimchar", "Monferno", "Infernape", "Piplup", "Prinplup", "Empoleon"],
    }
    # Keep only one selected starter line per generation; defaults are the first
    # line and can be changed in the UI without altering the guide data.
    for group, choices in starter_groups.items():
        chosen = choices[0]
        for name in choices:
            if name in SPECIES_NAMES.values():
                found.setdefault(name, {
                    "dex": next(i for i, n in SPECIES_NAMES.items() if n == name),
                    "sources": [{"gate": next((g for g in segments if group.split()[0] in g or group == "Hoenn starter"), list(segments)[0]),
                                  "task": f"Choice group: {group}", "task_index": -1}],
                    "gate": next((g for g in segments if group.split()[0] in g), list(segments)[0]),
                })

    fossils = ["Lileep", "Cradily", "Anorith", "Armaldo"]
    fossil_gate = "Badge 3 → Badge 5 — Norman"
    for name in fossils:
        if name in SPECIES_NAMES.values():
            found.setdefault(name, {"dex": next(i for i,n in SPECIES_NAMES.items() if n==name),
                                     "sources": [{"gate": fossil_gate, "task": "Fossil choice: Root or Claw Fossil + evolution", "task_index": -2}],
                                     "gate": fossil_gate})

    # Version-specific substitutions are already present in the OR task text.
    # Remove the opposite-version exclusives from the derived list when they
    # slipped in via a generic task label.
    opposite = {
        "Omega Ruby checklist": {"Lotad", "Lombre", "Ludicolo", "Sableye", "Seviper", "Lunatone", "Latias", "Kyogre", "Zekrom", "Dialga", "Thundurus", "Lugia"},
        "Alpha Sapphire checklist": {"Seedot", "Nuzleaf", "Shiftry", "Mawile", "Zangoose", "Solrock", "Latios", "Groudon", "Reshiram", "Palkia", "Tornadus", "Ho-Oh"},
    }
    for name in list(found):
        if name in opposite.get(version, set()):
            del found[name]
    return sorted(found.items(), key=lambda kv: (kv[1]["dex"], kv[0]))
'''
s=s.replace(marker,insert+marker)
# Add tab setup replacing filters/scroll block
old='''        filters = QHBoxLayout()\n        filters.addWidget(QLabel("Find"))\n        self.search = QLineEdit()\n        self.search.setPlaceholderText("Search route, Pokémon, evolution, fishing, DexNav…")\n        self.search.textChanged.connect(self._apply_filter)\n        filters.addWidget(self.search, 1)\n        self.incomplete_only = QCheckBox("Incomplete only")\n        self.incomplete_only.toggled.connect(self._apply_filter)\n        filters.addWidget(self.incomplete_only)\n        outer.addLayout(filters)\n\n        scroll = QScrollArea()\n        scroll.setWidgetResizable(True)\n        content = QWidget()\n        self.sections = QVBoxLayout(content)\n        self.sections.setContentsMargins(0, 0, 0, 0)\n        self.sections.setSpacing(7)\n        scroll.setWidget(content)\n        outer.addWidget(scroll, 1)\n\n        self._build_sections()\n'''
new='''        self.tabs = QTabWidget()\n        outer.addWidget(self.tabs, 1)\n\n        # Pokémon-level tracker: the useful day-to-day Oak view.\n        species_page = QWidget()\n        species_outer = QVBoxLayout(species_page)\n        species_filters = QHBoxLayout()\n        species_filters.addWidget(QLabel("Find"))\n        self.species_search = QLineEdit()\n        self.species_search.setPlaceholderText("Search Pokémon, gate or source…")\n        self.species_search.textChanged.connect(self._apply_species_filter)\n        species_filters.addWidget(self.species_search, 1)\n        self.species_incomplete = QCheckBox("Incomplete only")\n        self.species_incomplete.toggled.connect(self._apply_species_filter)\n        species_filters.addWidget(self.species_incomplete)\n        self.species_reset_btn = QPushButton("RESET POKÉMON")\n        self.species_reset_btn.clicked.connect(self._reset_species)\n        species_filters.addWidget(self.species_reset_btn)\n        self.copy_missing_btn = QPushButton("COPY MISSING")\n        self.copy_missing_btn.clicked.connect(self._copy_missing_species)\n        species_filters.addWidget(self.copy_missing_btn)\n        species_outer.addLayout(species_filters)\n        self.species_scroll = QScrollArea()\n        self.species_scroll.setWidgetResizable(True)\n        species_content = QWidget()\n        self.species_layout = QVBoxLayout(species_content)\n        self.species_layout.setContentsMargins(0, 0, 0, 0)\n        self.species_layout.setSpacing(4)\n        self.species_scroll.setWidget(species_content)\n        species_outer.addWidget(self.species_scroll, 1)\n        self.tabs.addTab(species_page, "POKÉMON")\n\n        # Keep the researched grouped route as a reference/guide.\n        guide_page = QWidget()\n        guide_outer = QVBoxLayout(guide_page)\n        guide_filters = QHBoxLayout()\n        guide_filters.addWidget(QLabel("Find"))\n        self.search = QLineEdit()\n        self.search.setPlaceholderText("Search route, Pokémon, evolution, fishing, DexNav…")\n        self.search.textChanged.connect(self._apply_filter)\n        guide_filters.addWidget(self.search, 1)\n        self.incomplete_only = QCheckBox("Incomplete only")\n        self.incomplete_only.toggled.connect(self._apply_filter)\n        guide_filters.addWidget(self.incomplete_only)\n        guide_outer.addLayout(guide_filters)\n        scroll = QScrollArea()\n        scroll.setWidgetResizable(True)\n        content = QWidget()\n        self.sections = QVBoxLayout(content)\n        self.sections.setContentsMargins(0, 0, 0, 0)\n        self.sections.setSpacing(7)\n        scroll.setWidget(content)\n        guide_outer.addWidget(scroll, 1)\n        self.tabs.addTab(guide_page, "GUIDE / GATES")\n\n        self._build_sections()\n        self._build_species()\n'''
if old not in s: raise SystemExit('old block not found')
s=s.replace(old,new)
# Insert methods before _load
marker='    def _load(self):\n'
methods=r'''    def _build_species(self):
        while self.species_layout.count():
            item = self.species_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._species_checks = {}
        self._species_meta = {}
        completed = self.state.setdefault("species_completed_by_version", {}).setdefault(self.current_version, {})
        entries = _species_entries_for_version(self.current_version)
        groups = {}
        for name, meta in entries:
            groups.setdefault(meta["gate"], []).append((name, meta))
        for gate, items in groups.items():
            box = QGroupBox(gate)
            layout = QVBoxLayout(box)
            target = OAK_SEGMENTS_BY_VERSION[self.current_version][gate][0]
            head = QLabel(f"Gate target: {target} • {len(items)} Pokémon tracked here")
            head.setObjectName("Muted")
            layout.addWidget(head)
            for name, meta in items:
                row = QHBoxLayout()
                key = f"{meta['dex']}|{name}"
                cb = QCheckBox(f"#{meta['dex']:03d} {name}")
                cb.setChecked(bool(completed.get(key, False)))
                sources = " • ".join(dict.fromkeys(src["task"] for src in meta["sources"]))
                cb.setToolTip(f"Gate: {gate}\nSource: {sources}\nManual Oak completion check.")
                cb.toggled.connect(lambda checked, k=key: self._species_changed(k, checked))
                row.addWidget(cb, 1)
                source_label = QLabel(meta["sources"][0]["task"])
                source_label.setObjectName("Muted")
                source_label.setWordWrap(True)
                row.addWidget(source_label, 2)
                layout.addLayout(row)
                self._species_checks[key] = cb
                self._species_meta[key] = meta
            self.species_layout.addWidget(box)
        self.species_layout.addStretch(1)
        self._apply_species_filter()
        self._refresh_progress()

    def _species_changed(self, key, checked):
        completed = self.state.setdefault("species_completed_by_version", {}).setdefault(self.current_version, {})
        completed[key] = bool(checked)
        self._save()
        self._refresh_progress()
        self._apply_species_filter()

    def _apply_species_filter(self):
        query = self.species_search.text().strip().lower() if hasattr(self, "species_search") else ""
        incomplete = self.species_incomplete.isChecked() if hasattr(self, "species_incomplete") else False
        for key, cb in getattr(self, "_species_checks", {}).items():
            meta = self._species_meta[key]
            hay = (cb.text() + " " + meta["gate"] + " " + " ".join(src["task"] for src in meta["sources"])).lower()
            cb.setVisible((not query or query in hay) and (not incomplete or not cb.isChecked()))
        for i in range(self.species_layout.count()):
            box = self.species_layout.itemAt(i).widget()
            if box is None or not isinstance(box, QGroupBox):
                continue
            visible = any(cb.isVisible() for cb in box.findChildren(QCheckBox))
            box.setVisible(visible or (not query and not incomplete))

    def _reset_species(self):
        self.state.setdefault("species_completed_by_version", {})[self.current_version] = {}
        self._save()
        self._build_species()

    def _copy_missing_species(self):
        missing = [cb.text().replace("✓ ", "") for cb in self._species_checks.values() if not cb.isChecked()]
        from PySide6.QtWidgets import QApplication
        QApplication.clipboard().setText("\n".join(missing))

'''
s=s.replace(marker,methods+marker)
# modify load defaults
s=s.replace('''                    data.setdefault("active", False)\n                    data.setdefault("version", "Alpha Sapphire checklist")\n                    return data\n''','''                    data.setdefault("active", False)\n                    data.setdefault("version", "Alpha Sapphire checklist")\n                    data.setdefault("species_completed_by_version", {\n                        "Alpha Sapphire checklist": {},\n                        "Omega Ruby checklist": {},\n                    })\n                    data["species_completed_by_version"].setdefault("Alpha Sapphire checklist", {})\n                    data["species_completed_by_version"].setdefault("Omega Ruby checklist", {})\n                    return data\n''')
s=s.replace('''            "completed_by_version": {\n                "Alpha Sapphire checklist": {},\n                "Omega Ruby checklist": {},\n            },\n''','''            "completed_by_version": {\n                "Alpha Sapphire checklist": {},\n                "Omega Ruby checklist": {},\n            },\n            "species_completed_by_version": {\n                "Alpha Sapphire checklist": {},\n                "Omega Ruby checklist": {},\n            },\n''')
# version changed rebuild species
s=s.replace('''        self._save()\n        self._build_sections()\n\n    def _reset(self):''','''        self._save()\n        self._build_sections()\n        self._build_species()\n\n    def _reset(self):''')
# reset guide rebuild species too? current reset method occurrence
s=s.replace('''        self._save()\n        self._build_sections()\n\n    def _current_segments(self):''','''        self._save()\n        self._build_sections()\n        self._build_species()\n\n    def _current_segments(self):''')
# refresh progress add species line
needle='''        self.bar.setValue(pct)\n\n        for segment, (_, tasks) in segments.items():'''
repl='''        self.bar.setValue(pct)\n\n        species_checks = getattr(self, "_species_checks", {})\n        species_done = sum(1 for cb in species_checks.values() if cb.isChecked())\n        species_total = len(species_checks)\n        if hasattr(self, "overall"):\n            self.overall.setText(\n                f"{self.current_version}: {species_done}/{species_total} Pokémon tracked complete  •  {int(round(species_done * 100 / species_total)) if species_total else 0}%  •  {done}/{total} guide milestones"\n            )\n\n        for segment, (_, tasks) in segments.items():'''
s=s.replace(needle,repl)
p.write_text(s)
