from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt, QPointF
from PySide6.QtGui import QPainter, QPen
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QPushButton,
    QTabWidget, QTableWidget, QTableWidgetItem, QHeaderView, QScrollArea,
    QProgressBar, QAbstractItemView,
)

from .widgets import Panel, SpriteLoader
from pokebot.common.shiny_odds import (
    resolve_shiny_odds, cumulative_probability_from_log_miss,
    format_cumulative_percent, phase_progress_bar_value,
    additional_encounters_to_probability, phase_log_miss_for_constant,
)

STARTERS = {"treecko": "Treecko", "torchic": "Torchic", "mudkip": "Mudkip"}
METHOD_NAMES = {
    "walk": "Walk", "run": "Run", "acro_bunny": "Acro Bunny",
    "grass": "Grass", "cave": "Cave", "surf": "Surf", "ocean": "Ocean",
    "fishing": "Fishing", "rock_smash": "Rock Smash", "horde": "Horde",
    "dexnav": "DexNav Exclusive", "starter": "Starter", "static": "Static", "gift": "Gift",
}



class MiniChart(QWidget):
    """Tiny dependency-free Qt chart for low-overhead analytics."""
    def __init__(self, title="", mode="line", parent=None):
        super().__init__(parent)
        self.title = str(title)
        self.mode = str(mode)
        self.values = []
        self.setMinimumHeight(132)

    def set_values(self, values):
        self.values = [max(0.0, float(v or 0)) for v in values]
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        pal = self.palette()
        text_color = pal.text().color()
        axis_color = pal.mid().color()
        data_color = pal.highlight().color()
        r = self.rect().adjusted(10, 18, -10, -12)
        painter.setPen(QPen(text_color, 1))
        painter.drawText(10, 14, self.title)
        if r.width() <= 10 or r.height() <= 10:
            return
        painter.setPen(QPen(axis_color, 1))
        painter.drawLine(r.left(), r.bottom(), r.right(), r.bottom())
        painter.drawLine(r.left(), r.top(), r.left(), r.bottom())
        if not self.values or max(self.values, default=0.0) <= 0:
            painter.setPen(QPen(axis_color, 1))
            painter.drawText(r, Qt.AlignCenter, "No data yet")
            return
        vmax = max(self.values)
        if self.mode == "bar":
            n = max(1, len(self.values))
            slot = r.width() / n
            painter.setPen(QPen(data_color, 1))
            for i, value in enumerate(self.values):
                h = (value / vmax) * max(1, r.height()-2)
                left = r.left() + i * slot + max(1.0, slot * 0.12)
                width = max(1.0, slot * 0.76)
                painter.fillRect(int(left), int(r.bottom()-h), int(width), int(h), data_color)
        else:
            n = len(self.values)
            pts=[]
            for i, value in enumerate(self.values):
                x = r.left() if n == 1 else r.left() + (i/(n-1))*r.width()
                y = r.bottom() - (value/vmax)*max(1, r.height()-2)
                pts.append(QPointF(x,y))
            painter.setPen(QPen(data_color, 2))
            for a,b in zip(pts, pts[1:]):
                painter.drawLine(a,b)
            if len(pts) == 1:
                painter.drawPoint(pts[0])

def _read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return default


def _fmt_time(seconds):
    try: seconds = max(0, int(float(seconds)))
    except Exception: seconds = 0
    h, rem = divmod(seconds, 3600); m, _ = divmod(rem, 60)
    return f"{h}h {m:02d}m"


class StatsPage(QWidget):
    """Persistent analytics/history only. No hunt configuration lives here."""
    def __init__(self, profile, base_dir=None, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.base_dir = Path(base_dir or Path(__file__).resolve().parents[1])
        self.current_context = {}
        self._shiny_rows = []
        # Do not synchronously parse the full persistent encounter ledger while
        # MainWindow is being constructed. On long-running profiles this can be
        # tens/hundreds of thousands of JSONL rows and made RUN_BOT.bat appear
        # frozen on slower laptops. Stats are loaded on first tab visit instead.
        self._loaded_once = False
        self._last_data_signature = None
        self.sprite_loader = SpriteLoader(self.profile.root / "cache" / "oras_sprites", self)
        self.sprite_loader.sprite_ready.connect(self._sprite_ready)

        outer = QVBoxLayout(self); outer.setContentsMargins(8,8,8,8); outer.setSpacing(7)
        head = QHBoxLayout()
        title = QLabel("STATS — ORAS ANALYTICS & HISTORY")
        title.setStyleSheet("font-size:15pt;font-weight:900;")
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.setObjectName("SmallAction")
        self.refresh_btn.clicked.connect(self.refresh)
        head.addWidget(title); head.addStretch(1); head.addWidget(self.refresh_btn)
        outer.addLayout(head)

        self.summary = Panel("OVERALL SUMMARY")
        self.summary_labels = {}
        grid = QGridLayout(); grid.setHorizontalSpacing(18); grid.setVerticalSpacing(5)
        fields = [
            "Lifetime Encounters", "Shinies Found", "Total Hunt Time", "Overall Shiny Rate",
            "Current Phase", "Phase Encounters", "Current Shiny Streak", "Overall Encounters/hr",
            "Longest Phase", "Fastest Shiny", "Most Found", "Game Breakdown",
        ]
        for i, name in enumerate(fields):
            box = QWidget(); lay = QVBoxLayout(box); lay.setContentsMargins(2,2,2,2); lay.setSpacing(1)
            k=QLabel(name.upper()); k.setObjectName("Muted")
            v=QLabel("—"); v.setObjectName("Value"); v.setStyleSheet("font-size:13pt;font-weight:900;")
            lay.addWidget(k); lay.addWidget(v)
            grid.addWidget(box, i//4, i%4)
            self.summary_labels[name]=v
        self.summary.outer.addLayout(grid)
        outer.addWidget(self.summary)

        self.tabs = QTabWidget()
        self.overview = self._make_overview()
        self.species_table = self._make_table(["Pokémon","Seen","Shinies","Longest Phase","Shortest Shiny Phase","Total Time","Avg Enc/Shiny"])
        self.location_table = self._make_table(["Location","Encounters","Shinies","Time","Rate/hr"])
        self.method_table = self._make_table(["Method","Encounters","Shinies","Time","Avg Rate/hr"])
        self.shiny_history_widget = QWidget(); shlay=QVBoxLayout(self.shiny_history_widget); shlay.setContentsMargins(0,0,0,0)
        self.shiny_table = self._make_table(["Date / Time","Pokémon","Game","Location","Method","PID","XOR","Nature","IV Total"]); shlay.addWidget(self.shiny_table,1)
        prev=Panel("SELECTED SHINY SPRITES")
        prow=QHBoxLayout(); self.normal_sprite=QLabel("Normal —"); self.normal_sprite.setAlignment(Qt.AlignCenter); self.normal_sprite.setMinimumHeight(90); self.shiny_sprite=QLabel("Shiny —"); self.shiny_sprite.setAlignment(Qt.AlignCenter); self.shiny_sprite.setMinimumHeight(90); prow.addWidget(self.normal_sprite); prow.addWidget(self.shiny_sprite); prev.outer.addLayout(prow); shlay.addWidget(prev)
        self.shiny_table.currentCellChanged.connect(self._shiny_row_selected)
        self.records = self._make_records()
        self.graphs = self._make_graphs()
        for name, widget in [
            ("Overview", self.overview), ("Species", self.species_table),
            ("Locations", self.location_table), ("Methods", self.method_table),
            ("Shiny History", self.shiny_history_widget), ("Records", self.records),
            ("Graphs", self.graphs),
        ]: self.tabs.addTab(widget, name)
        outer.addWidget(self.tabs, 1)

    def _make_table(self, headers):
        t=QTableWidget(0,len(headers)); t.setHorizontalHeaderLabels(headers); t.setAlternatingRowColors(True)
        t.setEditTriggers(QAbstractItemView.NoEditTriggers); t.setSelectionBehavior(QAbstractItemView.SelectRows)
        t.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        return t

    def _make_overview(self):
        w=QWidget(); lay=QVBoxLayout(w); lay.setContentsMargins(5,5,5,5)
        current=Panel("CURRENT HUNT")
        self.current_labels={}
        g=QGridLayout()
        for i,k in enumerate(["Target","Game","Route / Location","Method","Phase","Phase Time","Current Rate/hr","Field Pace/hr","Field Avg","Battle/Return Avg","Cycle Avg","Est. Time to 63.2%","Cumulative Shiny Chance","Resets / Batches","Best / Worst SV","Highest / Lowest IV"]):
            lab=QLabel(k+":"); lab.setObjectName("FieldLabel"); val=QLabel("—"); val.setObjectName("Value")
            g.addWidget(lab,i//2,(i%2)*2); g.addWidget(val,i//2,(i%2)*2+1); self.current_labels[k]=val
        current.outer.addLayout(g); lay.addWidget(current)
        game=Panel("GAME BREAKDOWN")
        self.game_breakdown_label=QLabel("Omega Ruby — 0 • Alpha Sapphire — 0 • Combined ORAS — 0")
        self.game_breakdown_label.setObjectName("Value"); game.outer.addWidget(self.game_breakdown_label); lay.addWidget(game)
        odds=Panel("ODDS PROGRESS")
        self.odds_label=QLabel("No active phase selected"); self.odds_label.setObjectName("Value")
        self.odds_bar=QProgressBar(); self.odds_bar.setRange(0,10000); self.odds_bar.setValue(0); self.odds_bar.setTextVisible(False)
        odds.outer.addWidget(self.odds_label); odds.outer.addWidget(self.odds_bar); lay.addWidget(odds)
        graph=Panel("GRAPHS")
        self.graph_label=QLabel("Encounter-rate, cumulative encounter/shiny, species and method distributions are computed from the persistent ledger. Select the detailed tabs for exact values.")
        self.graph_label.setWordWrap(True); self.graph_label.setObjectName("Muted"); graph.outer.addWidget(self.graph_label); lay.addWidget(graph)
        lay.addStretch(1); return w

    def _make_graphs(self):
        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        content=QWidget(); lay=QVBoxLayout(content); lay.setContentsMargins(5,5,5,5); lay.setSpacing(7)
        self.hourly_chart=MiniChart("Encounters per hour", "line")
        self.cumulative_enc_chart=MiniChart("Cumulative encounters", "line")
        self.cumulative_shiny_chart=MiniChart("Cumulative shinies", "line")
        self.species_chart=MiniChart("Encounters by species — top 8", "bar")
        self.method_chart=MiniChart("Encounters by method — top 8", "bar")
        for chart in (self.hourly_chart,self.cumulative_enc_chart,self.cumulative_shiny_chart,self.species_chart,self.method_chart):
            lay.addWidget(chart)
        h=Panel("HOURLY DETAIL")
        self.hourly_table=self._make_table(["Hour","Encounters","Shinies"]); h.outer.addWidget(self.hourly_table); lay.addWidget(h)
        d=Panel("CUMULATIVE / DISTRIBUTION")
        self.cumulative_label=QLabel("Cumulative encounters: 0 • cumulative shinies: 0"); self.cumulative_label.setObjectName("Value")
        self.distribution_label=QLabel("Species / method distribution will populate from the encounter ledger."); self.distribution_label.setWordWrap(True); self.distribution_label.setObjectName("Muted")
        d.outer.addWidget(self.cumulative_label); d.outer.addWidget(self.distribution_label); lay.addWidget(d); lay.addStretch(1)
        scroll.setWidget(content); return scroll

    def _make_records(self):
        w=QWidget(); lay=QGridLayout(w); self.record_labels={}
        names=["Fastest Shiny","Longest Phase","Most Hunted Species","Most Shinies of One Species","Fastest Encounter Rate","Best IV Shiny","Lowest Shiny XOR"]
        for i,n in enumerate(names):
            p=Panel(n.upper()); v=QLabel("—"); v.setObjectName("Value"); v.setWordWrap(True); p.outer.addWidget(v); self.record_labels[n]=v; lay.addWidget(p,i//2,i%2)
        return w

    def set_current_context(self, data):
        if isinstance(data, dict): self.current_context.update(data)
        self._refresh_current()

    def _rows(self):
        rows=[]
        if self.profile.encounters_dir.exists():
            for path in sorted(self.profile.encounters_dir.glob("*.jsonl")):
                try:
                    # Stream line-by-line instead of read_text().splitlines(),
                    # avoiding a second full copy of large encounter ledgers.
                    with path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            if not line.strip(): continue
                            try: r=json.loads(line)
                            except Exception: continue
                            if isinstance(r,dict):
                                r.setdefault("_source",path.stem); rows.append(r)
                except Exception: pass
        return rows

    def _data_signature(self):
        """Cheap change detector so revisiting Stats does not rescan unchanged ledgers."""
        items=[]
        try:
            paths=list(self.profile.encounters_dir.glob("*.jsonl"))
            paths.extend(self.profile.stats_dir.glob("*.json"))
            for path in sorted(paths, key=lambda p: str(p)):
                st=path.stat()
                items.append((str(path), int(st.st_size), int(st.st_mtime_ns)))
            return tuple(items)
        except Exception:
            return None

    def _lifetime_stats(self):
        stats=[]
        for p in sorted(self.profile.stats_dir.glob("*.json")):
            if p.name == "species_shiny_totals.json": continue
            d=_read_json(p,{})
            if isinstance(d,dict): stats.append((p.stem,d))
        return stats

    def refresh(self, *_args, force=False):
        signature=self._data_signature()
        if self._loaded_once and signature is not None and signature == self._last_data_signature and not force:
            self._refresh_current()
            return
        rows=self._rows(); stats=self._lifetime_stats()
        self._loaded_once = True
        self._last_data_signature = signature
        life_seen=sum(int(d.get("lifetime_seen",0) or 0) for _,d in stats)
        life_shiny=sum(int(d.get("lifetime_shinies",0) or 0) for _,d in stats)
        total_time=sum(float(d.get("lifetime_hunt_seconds",0.0) or 0.0) for _,d in stats)
        overall_rate=(life_seen*3600.0/total_time) if total_time>0 else 0.0
        shiny_rate=(life_shiny/life_seen*100.0) if life_seen else 0.0
        self.summary_labels["Lifetime Encounters"].setText(f"{life_seen:,}")
        self.summary_labels["Shinies Found"].setText(f"{life_shiny:,}")
        self.summary_labels["Overall Shiny Rate"].setText(f"{shiny_rate:.4f}%" if life_seen else "—")
        self.summary_labels["Total Hunt Time"].setText(_fmt_time(total_time))
        self.summary_labels["Overall Encounters/hr"].setText(f"{overall_rate:.1f}")

        phase_records=[]
        for key,d in stats:
            phase=int(d.get("phase_seen",0) or 0); name=d.get("starter") or d.get("method") or key
            phase_records.append((phase,str(name)))
        if phase_records:
            phase,name=max(phase_records)
            self.summary_labels["Current Phase"].setText(str(name)); self.summary_labels["Phase Encounters"].setText(f"{phase:,}")
        known_phases=[int(r.get("phase_length",0) or 0) for r in rows if int(r.get("phase_length",0) or 0)>0]
        known_phases.extend(int(d.get("last_phase_seen",0) or 0) for _,d in stats if int(d.get("last_phase_seen",0) or 0)>0)
        longest=max(known_phases, default=0)
        self.summary_labels["Longest Phase"].setText(f"{longest:,}" if longest else "—")

        shinies=[r for r in rows if bool(r.get("shiny",r.get("is_shiny",False)))]
        phase_lengths=[int(r.get("phase_length",0) or 0) for r in shinies if int(r.get("phase_length",0) or 0)>0]
        if not phase_lengths:
            phase_lengths=[int(d.get("last_phase_seen",0) or 0) for _,d in stats if int(d.get("last_phase_seen",0) or 0)>0]
        fastest=min(phase_lengths, default=0)
        self.summary_labels["Fastest Shiny"].setText(f"{fastest:,} encounters" if fastest else "—")
        shiny_species=Counter(str(r.get("species_name") or r.get("species") or "Unknown") for r in shinies)
        self.summary_labels["Most Found"].setText((f"{shiny_species.most_common(1)[0][0]} ×{shiny_species.most_common(1)[0][1]}" if shiny_species else "—"))
        ordered=sorted(rows,key=lambda r:str(r.get("time") or ""), reverse=True)
        streak=0
        for r in ordered:
            if bool(r.get("shiny",r.get("is_shiny",False))): break
            streak += 1
        self.summary_labels["Current Shiny Streak"].setText(f"{streak:,}")
        game_agg=defaultdict(lambda:{"seen":0,"shiny":0,"time":0.0})
        for r in rows:
            game=str(r.get("game") or "Unknown"); game_agg[game]["seen"]+=1
            game_agg[game]["shiny"]+=int(bool(r.get("shiny",r.get("is_shiny",False))))
            try: game_agg[game]["time"]+=float(r.get("duration_s",0.0) or 0.0)
            except Exception: pass
        ora=game_agg.get("Omega Ruby",{}); asa=game_agg.get("Alpha Sapphire",{})
        or_count=int(ora.get("seen",0)); as_count=int(asa.get("seen",0)); combined=or_count+as_count
        game_text=f"OR {or_count:,} • AS {as_count:,} • ORAS {combined:,}"
        self.summary_labels["Game Breakdown"].setText(game_text)
        combined_shiny=int(ora.get("shiny",0))+int(asa.get("shiny",0)); combined_time=float(ora.get("time",0.0))+float(asa.get("time",0.0))
        self.game_breakdown_label.setText(
            f"Omega Ruby — {or_count:,} encounters / {int(ora.get('shiny',0))} shinies / {_fmt_time(ora.get('time',0))}  •  "
            f"Alpha Sapphire — {as_count:,} / {int(asa.get('shiny',0))} / {_fmt_time(asa.get('time',0))}  •  "
            f"Combined ORAS — {combined:,} / {combined_shiny} / {_fmt_time(combined_time)}"
        )

        self._fill_species(rows,stats); self._fill_locations(rows); self._fill_methods(rows,stats); self._fill_shinies(shinies); self._fill_records(rows,shinies,stats); self._fill_graphs(rows,shinies)
        self._refresh_current()

    def _set_table(self,t,rows):
        t.setRowCount(len(rows))
        for r,vals in enumerate(rows):
            for c,val in enumerate(vals): t.setItem(r,c,QTableWidgetItem(str(val)))

    def _fill_species(self, rows, stats):
        agg=defaultdict(lambda:{"seen":0,"shiny":0,"time":0.0,"phases":[]})
        for r in rows:
            n=str(r.get("species_name") or r.get("target") or r.get("species") or "Unknown")
            a=agg[n]; a["seen"]+=1; is_shiny=bool(r.get("shiny",r.get("is_shiny",False))); a["shiny"]+=int(is_shiny)
            try: a["time"]+=float(r.get("duration_s",0.0) or 0.0)
            except Exception: pass
            try:
                plen=int(r.get("phase_length",0) or 0)
                if is_shiny and plen>0: a["phases"].append(plen)
            except Exception: pass
        # Starter lifetime files can contain historic time/phases from before per-encounter duration logging.
        for _,d in stats:
            name=str(d.get("starter") or "")
            if name and name in agg:
                try: agg[name]["time"]=max(agg[name]["time"],float(d.get("lifetime_hunt_seconds",0.0) or 0.0))
                except Exception: pass
                try:
                    plen=int(d.get("last_phase_seen",0) or 0)
                    if plen>0: agg[name]["phases"].append(plen)
                except Exception: pass
        out=[]
        for n,a in sorted(agg.items(), key=lambda kv:(-kv[1]["seen"],kv[0])):
            avg=(a["seen"]/a["shiny"]) if a["shiny"] else None
            longest=max(a["phases"],default=0); shortest=min(a["phases"],default=0)
            out.append([n,f"{a['seen']:,}",a["shiny"],f"{longest:,}" if longest else "—",f"{shortest:,}" if shortest else "—",_fmt_time(a["time"]) if a["time"]>0 else "—",f"{avg:.1f}" if avg is not None else "—"])
        self._set_table(self.species_table,out)

    def _fill_locations(self,rows):
        agg=defaultdict(lambda:{"seen":0,"shiny":0,"time":0.0})
        for r in rows:
            loc=str(r.get("location_name") or r.get("location") or "Starter / N/A"); a=agg[loc]
            a["seen"]+=1; a["shiny"]+=int(bool(r.get("shiny",r.get("is_shiny",False))))
            try: a["time"]+=float(r.get("duration_s",0.0) or 0.0)
            except Exception: pass
        out=[]
        for k,a in sorted(agg.items(),key=lambda kv:-kv[1]["seen"]):
            rate=a["seen"]*3600.0/a["time"] if a["time"]>0 else None
            out.append([k,f"{a['seen']:,}",a["shiny"],_fmt_time(a["time"]) if a["time"]>0 else "—",f"{rate:.1f}" if rate is not None else "—"])
        self._set_table(self.location_table,out)

    def _fill_methods(self,rows,stats):
        agg=defaultdict(lambda:{"seen":0,"shiny":0,"time":0.0})
        def note(cat, shiny, duration):
            a=agg[cat]; a["seen"]+=1; a["shiny"]+=shiny; a["time"]+=duration
        for r in rows:
            shiny=int(bool(r.get("shiny",r.get("is_shiny",False))))
            try: duration=float(r.get("duration_s",0.0) or 0.0)
            except Exception: duration=0.0
            hunt=str(r.get("hunt_type") or "").lower()
            if hunt == "starter":
                note("Starter",shiny,duration); continue
            if hunt == "static":
                note("Static",shiny,duration); continue
            if hunt == "gift":
                note("Gift",shiny,duration); continue
            method=str(r.get("method_name") or r.get("method") or "").strip()
            mnorm={"acro_bunny":"Acro Bunny","walk":"Walk","run":"Run","horde":"Horde","cave":"Cave"}.get(method.lower(),method)
            if mnorm: note(mnorm,shiny,duration)
            env=str(r.get("environment") or "").strip()
            envmap={"tall grass":"Grass","grass":"Grass","land":"Grass","cave":"Cave","surf":"Surf","ocean":"Ocean","fishing":"Fishing","rock smash":"Rock Smash","horde":"Horde","dexnav exclusive":"DexNav Exclusive"}
            enorm=envmap.get(env.lower())
            if enorm: note(enorm,shiny,duration)
        categories=["Starter","Static","Gift","Grass","Cave","Surf","Ocean","Fishing","Rock Smash","Horde","DexNav Exclusive","Acro Bunny","Walk","Run"]
        present=[]
        for cat in categories:
            a=agg.get(cat,{"seen":0,"shiny":0,"time":0.0}); rate=a["seen"]*3600.0/a["time"] if a["time"]>0 else None
            present.append([cat,f"{a['seen']:,}",a["shiny"],_fmt_time(a["time"]) if a["time"]>0 else "—",f"{rate:.1f}" if rate is not None else "—"])
        self._set_table(self.method_table,present)

    def _fill_shinies(self,shinies):
        ordered=sorted(shinies,key=lambda r:str(r.get("time") or ""), reverse=True)
        out=[]
        for r in ordered:
            ivs=r.get("ivs") or {}; ivsum=r.get("iv_sum")
            if ivsum is None and isinstance(ivs,dict):
                try: ivsum=sum(int(x) for x in ivs.values())
                except Exception: ivsum="—"
            out.append([r.get("time","—"),r.get("species_name") or r.get("species","—"),r.get("game","ORAS"),r.get("location_name") or r.get("location","—"),r.get("method_name") or r.get("method") or r.get("hunt_type","—"),r.get("pid") or r.get("pokemon_pid","—"),r.get("sv",r.get("shiny_xor","—")),r.get("nature","—"),ivsum if ivsum is not None else "—"])
        self._shiny_rows = ordered
        self._set_table(self.shiny_table,out)
        if ordered and self.shiny_table.currentRow() < 0:
            self.shiny_table.setCurrentCell(0,0)

    def _shiny_row_selected(self, row, col, prow, pcol):
        if row < 0 or row >= len(self._shiny_rows): return
        rec=self._shiny_rows[row]
        try: species=int(rec.get("species") or rec.get("species_id") or 0)
        except Exception: species=0
        self.normal_sprite.setText("Normal —"); self.shiny_sprite.setText("Shiny —")
        if species:
            self.sprite_loader.request_sprite(1001,species,False); self.sprite_loader.request_sprite(1002,species,True)

    def _sprite_ready(self, slot, pixmap):
        target=self.normal_sprite if slot==1001 else self.shiny_sprite if slot==1002 else None
        if target is None: return
        if pixmap is not None and not pixmap.isNull():
            target.setText(""); target.setPixmap(pixmap.scaled(88,88,Qt.KeepAspectRatio,Qt.SmoothTransformation))

    def _fill_records(self,rows,shinies,stats):
        seen=Counter(str(r.get("species_name") or r.get("species") or "Unknown") for r in rows)
        shinyc=Counter(str(r.get("species_name") or r.get("species") or "Unknown") for r in shinies)
        phase_pairs=[]
        for r in shinies:
            plen=int(r.get("phase_length",0) or 0)
            if plen: phase_pairs.append((plen,r.get("species_name") or r.get("species") or "—"))
        for k,d in stats:
            plen=int(d.get("last_phase_seen",0) or 0)
            if plen: phase_pairs.append((plen,d.get("starter") or d.get("method") or k))
        longest=max(phase_pairs,default=(0,"—"),key=lambda x:x[0]); fastest=min(phase_pairs,default=(0,"—"),key=lambda x:x[0]) if phase_pairs else (0,"—")
        lowest=min(((int(r.get("sv",r.get("shiny_xor",65536)) or 65536), r) for r in shinies), default=(65536,None), key=lambda x:x[0])
        bestiv=[]
        for r in shinies:
            try:
                ivsum=r.get("iv_sum")
                if ivsum is None: ivsum=sum(int(v) for v in (r.get("ivs") or {}).values())
                bestiv.append((int(ivsum),r))
            except Exception: pass
        bestiv=max(bestiv,default=(0,None),key=lambda x:x[0])
        rates=[float(d.get("lifetime_fastest_rate",0) or 0) for _,d in stats]
        fast_rate=max(rates,default=0.0)
        self.record_labels["Longest Phase"].setText(f"{longest[0]:,} • {longest[1]}" if longest[0] else "—")
        self.record_labels["Most Hunted Species"].setText(f"{seen.most_common(1)[0][0]} • {seen.most_common(1)[0][1]:,}" if seen else "—")
        self.record_labels["Most Shinies of One Species"].setText(f"{shinyc.most_common(1)[0][0]} ×{shinyc.most_common(1)[0][1]}" if shinyc else "—")
        self.record_labels["Lowest Shiny XOR"].setText(f"{lowest[0]} • {(lowest[1] or {}).get('species_name','—')}" if lowest[1] else "—")
        self.record_labels["Best IV Shiny"].setText(f"{bestiv[0]}/186 • {(bestiv[1] or {}).get('species_name','—')}" if bestiv[1] else "—")
        self.record_labels["Fastest Shiny"].setText(f"{fastest[0]:,} encounters • {fastest[1]}" if fastest[0] else "—")
        self.record_labels["Fastest Encounter Rate"].setText(f"{fast_rate:.1f}/hr" if fast_rate else "—")

    def _fill_graphs(self, rows, shinies):
        chronological=sorted(rows,key=lambda r:str(r.get("time") or ""))
        buckets=defaultdict(lambda:[0,0])
        for r in chronological:
            ts=str(r.get("time") or "")
            hour=ts[:13]+":00" if len(ts)>=13 else "Unknown"
            buckets[hour][0]+=1; buckets[hour][1]+=int(bool(r.get("shiny",r.get("is_shiny",False))))
        ordered_hours=sorted((k,v) for k,v in buckets.items() if k!="Unknown")
        recent=ordered_hours[-24:]
        self._set_table(self.hourly_table,[[k,v[0],v[1]] for k,v in reversed(recent)])
        self.hourly_chart.set_values([v[0] for _,v in recent])
        cumulative=[]; cumulative_shiny=[]; c=0; sh=0
        for r in chronological:
            c+=1; sh+=int(bool(r.get("shiny",r.get("is_shiny",False)))); cumulative.append(c); cumulative_shiny.append(sh)
        # Downsample long histories so painting remains cheap on low-end machines.
        def sample(vals, limit=240):
            if len(vals)<=limit: return vals
            step=max(1,len(vals)//limit); out=vals[::step]
            if out[-1] != vals[-1]: out.append(vals[-1])
            return out
        self.cumulative_enc_chart.set_values(sample(cumulative)); self.cumulative_shiny_chart.set_values(sample(cumulative_shiny))
        species=Counter(str(r.get("species_name") or r.get("species") or "Unknown") for r in rows)
        methods=Counter(str(r.get("method_name") or r.get("method") or r.get("hunt_type") or "Unknown") for r in rows)
        self.species_chart.set_values([v for _,v in species.most_common(8)]); self.method_chart.set_values([v for _,v in methods.most_common(8)])
        self.cumulative_label.setText(f"Cumulative encounters: {len(rows):,} • cumulative shinies: {len(shinies):,}")
        sp=", ".join(f"{k} {v:,}" for k,v in species.most_common(8)) or "No species data"
        mt=", ".join(f"{k} {v:,}" for k,v in methods.most_common(8)) or "No method data"
        self.distribution_label.setText(f"Encounters by species: {sp}\nEncounters by method: {mt}")

    def _refresh_current(self):
        c = self.current_context
        self.current_labels["Target"].setText(str(c.get("target") or c.get("starter") or "—"))
        self.current_labels["Game"].setText(str(c.get("game") or "—"))
        self.current_labels["Route / Location"].setText(str(c.get("location_name") or c.get("location") or "—"))
        method = str(c.get("method_name") or c.get("method") or c.get("wild_method") or c.get("hunt_type") or "—")
        self.current_labels["Method"].setText(method)
        phase = int(c.get("phase_seen", 0) or 0)
        self.current_labels["Phase"].setText(f"{phase:,}")
        if c.get("target") or c.get("starter"):
            self.summary_labels["Current Phase"].setText(str(c.get("target") or c.get("starter")))
            self.summary_labels["Phase Encounters"].setText(f"{phase:,}")
        self.current_labels["Phase Time"].setText(str(c.get("elapsed") or "—"))
        rate = float(c.get("rate", 0) or 0)
        field_pace = float(c.get("field_pace_per_hour", 0) or 0)
        field_avg = float(c.get("field_to_encounter_average", 0) or 0)
        battle_avg = float(c.get("battle_to_field_average", 0) or 0)
        cycle_avg = float(c.get("measured_cycle_average", 0) or 0)
        self.current_labels["Current Rate/hr"].setText(
            f"{rate:.1f}" if rate > 0 else "—"
        )
        self.current_labels["Field Pace/hr"].setText(
            f"{field_pace:.1f}" if field_pace > 0 else "—"
        )
        self.current_labels["Field Avg"].setText(
            f"{field_avg:.2f}s" if field_avg > 0 else "—"
        )
        self.current_labels["Battle/Return Avg"].setText(
            f"{battle_avg:.2f}s" if battle_avg > 0 else "—"
        )
        self.current_labels["Cycle Avg"].setText(
            f"{cycle_avg:.2f}s" if cycle_avg > 0 else "—"
        )
        hunt = str(c.get("hunt_type") or "").casefold()
        charm = c.get("shiny_charm") or {}
        charm_detected = charm.get("detected")
        wild_method = str(c.get("wild_method") or c.get("method") or "").casefold()

        odds = None
        odds_text = c.get("shiny_odds_display")
        next_probability = None
        if hunt in {"starter", "gift"}:
            odds = resolve_shiny_odds(
                game=str(c.get("game") or "ORAS"),
                hunt_type="Starter" if hunt == "starter" else "Gift",
                shiny_charm_present=charm_detected,
                shiny_charm_applies=False,
            )
            next_probability = odds.probability
            odds_text = odds.display
        elif hunt in {"wild", "static"}:
            if charm_detected is not None:
                if hunt == "wild" and wild_method == "fishing":
                    chain = c.get("fishing_chain") or {}
                    next_odds = chain.get("next_odds") or {}
                    if next_odds.get("probability") is not None:
                        next_probability = float(next_odds.get("probability"))
                    if next_odds.get("one_in"):
                        odds_text = f"~1/{float(next_odds.get('one_in')):,.2f} next"
                else:
                    odds = resolve_shiny_odds(
                        game=str(c.get("game") or "ORAS"),
                        hunt_type="Static" if hunt == "static" else "Wild",
                        shiny_charm_present=charm_detected,
                        shiny_charm_applies=True,
                    )
                    next_probability = odds.probability
                    odds_text = odds.display

        cumulative = c.get("phase_cumulative_probability")
        if cumulative is not None:
            try:
                cumulative = min(1.0, max(0.0, float(cumulative)))
            except Exception:
                cumulative = None
        log_miss = c.get("phase_log_miss")
        if cumulative is None and log_miss is not None:
            cumulative = cumulative_probability_from_log_miss(log_miss)
        if cumulative is None and odds is not None:
            log_miss = phase_log_miss_for_constant(phase, odds.probability)
            cumulative = cumulative_probability_from_log_miss(log_miss)

        if cumulative is not None:
            chance_text = format_cumulative_percent(cumulative)
            self.current_labels["Cumulative Shiny Chance"].setText(chance_text)
            self.odds_bar.setRange(0, 10000)
            self.odds_bar.setValue(phase_progress_bar_value(cumulative))
            self.odds_label.setText(
                f"{phase:,} phase checks • cumulative chance {chance_text} • odds {odds_text or '—'}"
            )
        else:
            self.current_labels["Cumulative Shiny Chance"].setText("—")
            self.odds_bar.setRange(0, 10000)
            self.odds_bar.setValue(0)
            self.odds_label.setText("Cumulative odds unavailable — Shiny Charm authority is not verified")

        eta_text = "—"
        if rate > 0 and next_probability and cumulative is not None:
            if log_miss is None:
                import math
                log_miss = 0.0 if cumulative <= 0 else math.log1p(-cumulative)
            additional = additional_encounters_to_probability(log_miss, next_probability)
            if additional is not None:
                hours = float(additional) / rate
                eta_text = f"{hours:.1f}h"
                if hunt == "wild" and wild_method == "fishing":
                    eta_text += " at next-chain odds"
        self.current_labels["Est. Time to 63.2%"].setText(eta_text)

        resets = int(c.get("resets", c.get("session_resets", 0)) or 0)
        batches = int(c.get("batches", c.get("session_batches", 0)) or 0)
        if hunt == "gift" and (c.get("gift_key") == "fossil_batch" or batches):
            self.current_labels["Resets / Batches"].setText(f"{resets:,} / {batches:,}")
        else:
            self.current_labels["Resets / Batches"].setText(f"{resets:,}")

        fmt_extreme = lambda value: "—" if value is None else str(value)
        self.current_labels["Best / Worst SV"].setText(
            f"{fmt_extreme(c.get('lowest_sv'))} / {fmt_extreme(c.get('highest_sv'))}"
        )
        self.current_labels["Highest / Lowest IV"].setText(
            f"{fmt_extreme(c.get('highest_iv_sum'))} / {fmt_extreme(c.get('lowest_iv_sum'))}"
        )
