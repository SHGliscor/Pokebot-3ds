from __future__ import annotations

import json
import hashlib
from pathlib import Path
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QScrollArea, QFrame, QLineEdit, QComboBox, QCheckBox, QTableWidget,
    QTableWidgetItem, QHeaderView, QAbstractItemView, QTabWidget,
)
from .widgets import Panel


class ToolsPage(QWidget):
    connection_test_requested = Signal()
    controller_test_requested = Signal(str)
    touch_test_requested = Signal()
    support_export_requested = Signal()
    open_folder_requested = Signal(str)
    self_test_requested = Signal()
    patch_validate_requested = Signal()
    cache_clear_requested = Signal()

    def __init__(self, profile, base_dir, parent=None):
        super().__init__(parent)
        self.profile=profile; self.base_dir=Path(base_dir); self.last_probe={}
        outer=QVBoxLayout(self); outer.setContentsMargins(8,8,8,8); outer.setSpacing(7)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); content=QWidget(); lay=QVBoxLayout(content); lay.setSpacing(8)

        health=Panel("SYSTEM HEALTH")
        self.health_labels={}; g=QGridLayout()
        for i,k in enumerate(["3DS Bridge","Input Controller","Game","RAM Authority","World Database","Shiny Charm","code.ips","Overall"]):
            a=QLabel(k); a.setObjectName("FieldLabel"); b=QLabel("NOT TESTED"); b.setObjectName("AmberText"); g.addWidget(a,i//2,(i%2)*2); g.addWidget(b,i//2,(i%2)*2+1); self.health_labels[k]=b
        health.outer.addLayout(g)
        row=QHBoxLayout(); b=QPushButton("Run System Health / Self-Test"); b.setObjectName("StartButton"); b.clicked.connect(self.self_test_requested); row.addWidget(b); row.addStretch(1); health.outer.addLayout(row); lay.addWidget(health)

        diag=Panel("3DS DIAGNOSTICS")
        row=QHBoxLayout();
        for txt,cb in [("Connection Test",self.connection_test_requested),("Refresh RAM Inspector",self.connection_test_requested)]:
            b=QPushButton(txt); b.setObjectName("SmallAction"); b.clicked.connect(cb); row.addWidget(b)
        row.addStretch(1); diag.outer.addLayout(row)
        self.connection_text=QLabel("Bridge/game/controller details appear here after a test."); self.connection_text.setWordWrap(True); self.connection_text.setObjectName("Muted"); diag.outer.addWidget(self.connection_text)
        ctl=QHBoxLayout(); ctl.addWidget(QLabel("Controller Tester:"))
        for key in ("UP","DOWN","LEFT","RIGHT","A","B","X","Y","L","R","START","SELECT"):
            b=QPushButton(key); b.setObjectName("SmallAction"); b.setMaximumWidth(58); b.clicked.connect(lambda checked=False,k=key:self.controller_test_requested.emit(k)); ctl.addWidget(b)
        touch=QPushButton("Touch (160,220)"); touch.setObjectName("SmallAction"); touch.clicked.connect(self.touch_test_requested); ctl.addWidget(touch); diag.outer.addLayout(ctl)
        self.controller_result=QLabel("Acknowledged-input status: —"); self.controller_result.setObjectName("Muted"); diag.outer.addWidget(self.controller_result); lay.addWidget(diag)

        ram=Panel("RAM & GAME DATA")
        self.ram_text=QLabel("Run Connection Test / Self-Test to populate read-only RAM telemetry."); self.ram_text.setWordWrap(True); self.ram_text.setTextInteractionFlags(Qt.TextSelectableByMouse); ram.outer.addWidget(self.ram_text)
        self.pk6_table=QTableWidget(0,9); self.pk6_table.setHorizontalHeaderLabels(["Source","Species","PID","EC","XOR","Nature","Ability","IV Sum","Checksum"]); self.pk6_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.pk6_table.setEditTriggers(QAbstractItemView.NoEditTriggers); ram.outer.addWidget(self.pk6_table)
        self.terrain_text=QLabel("Terrain / Position Inspector: —"); self.terrain_text.setWordWrap(True); self.terrain_text.setObjectName("Muted"); ram.outer.addWidget(self.terrain_text)
        erow=QHBoxLayout(); erow.addWidget(QLabel("Encounter Lookup:")); self.encounter_game=QComboBox(); self.encounter_game.addItem("Alpha Sapphire","alpha_sapphire"); self.encounter_game.addItem("Omega Ruby","omega_ruby"); self.encounter_location=QComboBox(); erow.addWidget(self.encounter_game); erow.addWidget(self.encounter_location,1); ram.outer.addLayout(erow)
        self.encounter_table=QTableWidget(0,7); self.encounter_table.setHorizontalHeaderLabels(["Section","Species ID","Pokémon","Levels","Form","Slots","Automation"]); self.encounter_table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch); self.encounter_table.setEditTriggers(QAbstractItemView.NoEditTriggers); ram.outer.addWidget(self.encounter_table)
        self.encounter_game.currentIndexChanged.connect(self._rebuild_encounter_locations); self.encounter_location.currentIndexChanged.connect(self._render_encounter_lookup); self._rebuild_encounter_locations(); lay.addWidget(ram)

        maint=Panel("MAINTENANCE")
        mg=QGridLayout()
        buttons=[("Open Stats","stats"),("Open Logs","logs"),("Open Sprite Cache","cache"),("Open Support Exports","support"),("Open AppData","appdata")]
        for i,(txt,key) in enumerate(buttons):
            b=QPushButton(txt); b.setObjectName("SmallAction"); b.clicked.connect(lambda checked=False,k=key:self.open_folder_requested.emit(k)); mg.addWidget(b,i//3,i%3)
        maint.outer.addLayout(mg)
        prow=QHBoxLayout(); pv=QPushButton("Validate Local code.ips"); pv.setObjectName("SmallAction"); pv.clicked.connect(self.patch_validate_requested); prow.addWidget(pv); cc=QPushButton("Clear Sprite Cache / Rebuild on Demand"); cc.setObjectName("SmallAction"); cc.clicked.connect(self.cache_clear_requested); prow.addWidget(cc); prow.addStretch(1); maint.outer.addLayout(prow)
        self.patch_text=QLabel("Game Patch Validator: detects C400/C500 game identity; select a local code.ips to compare SHA256 against the included patch for the detected game. Direct 3DS SD-card inspection is not available."); self.patch_text.setWordWrap(True); maint.outer.addWidget(self.patch_text)
        self.cache_text=QLabel("Sprite Cache Manager: missing artwork is refreshed automatically when sprite download is enabled. Clearing the cache forces on-demand rebuilds."); self.cache_text.setWordWrap(True); self.cache_text.setObjectName("Muted"); maint.outer.addWidget(self.cache_text); lay.addWidget(maint)

        support=Panel("SUPPORT")
        row=QHBoxLayout(); exp=QPushButton("Export Full Support ZIP"); exp.setObjectName("StartButton"); exp.clicked.connect(self.support_export_requested); row.addWidget(exp); row.addStretch(1); support.outer.addLayout(row)
        self.build_info=QLabel(self._build_info()); self.build_info.setWordWrap(True); self.build_info.setTextInteractionFlags(Qt.TextSelectableByMouse); support.outer.addWidget(self.build_info)
        self.advanced=QCheckBox("Developer / Advanced Tools")
        self.advanced_text=QLabel("Raw address reader / chooser-state / battle-state / protocol diagnostics remain read-only and are intentionally hidden unless a dedicated validated probe is present in the build."); self.advanced_text.setWordWrap(True); self.advanced_text.setVisible(False); self.advanced.toggled.connect(self.advanced_text.setVisible); support.outer.addWidget(self.advanced); support.outer.addWidget(self.advanced_text); lay.addWidget(support)

        # Keep the tool surface readable as capabilities grow: everyday checks,
        # live RAM/game data, and maintenance/support are separate tabs.
        tabs = QTabWidget()
        for panel in (health, diag, ram, maint, support):
            lay.removeWidget(panel)
        overview = QWidget(); overview_lay = QVBoxLayout(overview); overview_lay.setContentsMargins(6,6,6,6); overview_lay.setSpacing(7)
        overview_lay.addWidget(health); overview_lay.addWidget(diag); overview_lay.addStretch(1)
        data_page = QWidget(); data_lay = QVBoxLayout(data_page); data_lay.setContentsMargins(6,6,6,6); data_lay.addWidget(ram); data_lay.addStretch(1)
        maintenance_page = QWidget(); maintenance_lay = QVBoxLayout(maintenance_page); maintenance_lay.setContentsMargins(6,6,6,6); maintenance_lay.setSpacing(7)
        maintenance_lay.addWidget(maint); maintenance_lay.addWidget(support); maintenance_lay.addStretch(1)
        tabs.addTab(overview, "Overview")
        tabs.addTab(data_page, "RAM & game data")
        tabs.addTab(maintenance_page, "Maintenance & support")
        lay.addWidget(tabs, 1)
        scroll.setWidget(content); outer.addWidget(scroll,1)

    def _sha(self, path):
        try: return hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except Exception: return "unavailable"

    def _build_info(self):
        manifests=[p.name for p in sorted(self.base_dir.glob("MANIFEST*.json"))]
        starter=[]
        for key in ("treecko","torchic","mudkip"):
            p=self.base_dir/"pokebot"/"starters"/f"{key}.py"; starter.append(f"{key} {self._sha(p)[:12]}")
        patches=[]
        for title in ("000400000011C400","000400000011C500"):
            p=self.base_dir/"3ds_sd"/"luma"/"titles"/title/"code.ips"; patches.append(f"{title[-4:]} {self._sha(p)[:12]}")
        world=self.base_dir/"pokebot"/"wild"/"world"/"alpha_sapphire_grass_runtime.sqlite"
        return ("Patch / Build Information • " + (manifests[-1] if manifests else "No manifest") +
                " • Starter module hashes: " + ", ".join(starter) +
                " • code.ips: " + ", ".join(patches) +
                f" • World DB SHA256 {self._sha(world)[:12]} • Source build; EXE status is determined by BUILD_EXE output • RAM writes disabled")

    def _load_encounters(self):
        try:
            data=json.loads((self.base_dir/"data"/"oras_encounters.json").read_text(encoding="utf-8")); return data if isinstance(data,dict) else {"games":{}}
        except Exception: return {"games":{}}

    def _rebuild_encounter_locations(self, *args):
        data=self._load_encounters(); key=self.encounter_game.currentData(); locs=list(((data.get("games") or {}).get(key) or {}).get("locations") or [])
        self.encounter_location.blockSignals(True); self.encounter_location.clear()
        for loc in locs:
            if loc.get("has_encounters", True): self.encounter_location.addItem(str(loc.get("name") or "Unknown"), loc)
        self.encounter_location.blockSignals(False); self._render_encounter_lookup()

    def _render_encounter_lookup(self, *args):
        loc=self.encounter_location.currentData(); rows=[]
        if isinstance(loc,dict):
            for sec in loc.get("sections") or []:
                for mon in sec.get("pokemon") or []:
                    rows.append([sec.get("title","—"),mon.get("species","—"),mon.get("species_name","—"),f"{mon.get('min_level','—')}-{mon.get('max_level','—')}",mon.get("form",0),mon.get("slot_count",1),"Ready" if mon.get("automation_ready") else "Browser only"])
        self.encounter_table.setRowCount(len(rows))
        for r,vals in enumerate(rows):
            for c,v in enumerate(vals): self.encounter_table.setItem(r,c,QTableWidgetItem(str(v)))

    def set_controller_result(self,text,ok=True):
        self.controller_result.setText(str(text)); self.controller_result.setObjectName("GreenText" if ok else "RedText"); self.controller_result.style().unpolish(self.controller_result); self.controller_result.style().polish(self.controller_result)

    def set_probe_result(self,p):
        self.last_probe=dict(p or {}); gp=self.last_probe.get("game_profile") or {}; gi=self.last_probe.get("game_info") or {}; ci=self.last_probe.get("controller_info") or {}; charm=self.last_probe.get("shiny_charm") or {}; world=self.last_probe.get("world_location") or {}
        ram=bool(self.last_probe.get("ram_ready")); ctl=bool(self.last_probe.get("controller_ready")); game=gp.get("name") or "Not detected"
        vals={"3DS Bridge":"PASS" if ram else "HOLD","Input Controller":"PASS" if ctl else "HOLD","Game":game,"RAM Authority":"PASS" if ram else "HOLD","World Database":"PASS" if world.get("resolved") else ("UNRESOLVED" if ram else "NOT TESTED"),"Shiny Charm":("Present" if charm.get("detected") is True else "Not Present" if charm.get("detected") is False else charm.get("status","UNVERIFIED")),"code.ips":"Not required (X/Y)" if gp.get("family")=="xy" else "C400 expected" if gp.get("key")=="omega_ruby" else "C500 expected" if gp.get("key")=="alpha_sapphire" else "Unknown","Overall":"READY" if ram and ctl else "HOLD"}
        for k,v in vals.items(): self.health_labels[k].setText(str(v))
        target_key=gp.get("key")
        if target_key:
            for i in range(self.encounter_game.count()):
                if self.encounter_game.itemData(i)==target_key:
                    self.encounter_game.blockSignals(True); self.encounter_game.setCurrentIndex(i); self.encounter_game.blockSignals(False); self._rebuild_encounter_locations(); break
        lat=self.last_probe.get("latency_ms") or {}; ids=self.last_probe.get("trainer_ids") or {}
        self.connection_text.setText(f"Game: {game} • Title ID: {gi.get('title_id',gi.get('title_id_hex','—'))} • Process: {gi.get('process_name',gi.get('process','—'))} • PID: {gi.get('pid','—')} • TID/SID: {ids.get('tid','—')}/{ids.get('sid','—')} • Bridge latency: {lat.get('game_info','—')} ms • Controller latency: {lat.get('controller_ping','—')} ms • Protocol: {ci.get('protocol','—')} • Caps: 0x{int(ci.get('capabilities',ci.get('capability_flags',0)) or 0):08X} • Runtime: 0x{int(ci.get('runtime_flags',0) or 0):08X} • Max RAM read: 512 bytes • Max input hold/settle: {ci.get('max_hold_ms','—')}/{ci.get('max_settle_ms','—')} ms")
        battle=self.last_probe.get("battle_state") or {}; self.ram_text.setText(f"Read-only RAM Inspector • Battle: {battle.get('hex','—')} • Shiny Charm: {vals['Shiny Charm']} • TID/SID and party data are read through bounded bridge snapshots. No RAM writes are available.")
        self.terrain_text.setText(f"Terrain / Position Inspector • {world.get('location_name','Unknown')} • zone {world.get('zone_id','—')} • grid {world.get('grid','—')} • encounter terrain={world.get('encounter_terrain','—')} • running={world.get('enable_running','—')} • cycling={world.get('enable_cycling','—')} • core2={world.get('core2_interior','—')}")
        rows=[]
        for mon in self.last_probe.get("party") or []:
            if int(mon.get("species_id",0) or 0)>0: rows.append((f"Party {mon.get('slot')}",mon))
        wild=self.last_probe.get("wild_opponent")
        if isinstance(wild,dict) and wild.get("valid"): rows.append(("Wild opponent",wild))
        self.pk6_table.setRowCount(len(rows))
        for r,(src,mon) in enumerate(rows):
            ivs=mon.get("ivs") or {}; ivsum=sum(int(v) for v in ivs.values()) if ivs else "—"; vals=[src,mon.get("species") if isinstance(mon.get("species"),str) else mon.get("species_name",mon.get("species","—")),mon.get("pid","—"),mon.get("ec","—"),mon.get("sv",mon.get("shiny_xor","—")),mon.get("nature",mon.get("nature_id","—")),mon.get("ability",mon.get("ability_id","—")),ivsum,"OK" if mon.get("checksum_valid",mon.get("valid",False)) else "INVALID"]
            for c,v in enumerate(vals): self.pk6_table.setItem(r,c,QTableWidgetItem(str(v)))

    def set_patch_result(self, text, ok=True):
        self.patch_text.setText(str(text)); self.patch_text.setObjectName("GreenText" if ok else "RedText"); self.patch_text.style().unpolish(self.patch_text); self.patch_text.style().polish(self.patch_text)

    def set_cache_result(self, text, ok=True):
        self.cache_text.setText(str(text)); self.cache_text.setObjectName("GreenText" if ok else "RedText"); self.cache_text.style().unpolish(self.cache_text); self.cache_text.style().polish(self.cache_text)
