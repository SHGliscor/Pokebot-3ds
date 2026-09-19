from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QGridLayout, QLabel, QCheckBox,
    QPushButton, QScrollArea, QFrame, QComboBox, QProgressBar, QLineEdit,
    QTabWidget, QGroupBox
)
from .widgets import Panel

# ORAS Professor Oak checklist based on the Alpha Sapphire-only ORAS
# Professor Oak notes published by Altissimo1. The challenge convention is
# catch/evolve everything obtainable before progressing to the next badge;
# variations exist, so this tracker is intentionally editable/manual.
# Source: https://altissimo1.github.io/Main-Series/ORAS/Pokedex/alphasapphire-only.html

OAK_SEGMENTS = {
    "Pre-Badge 1 — Roxanne": (40, [
        "Route 101 gift: one Hoenn starter + its evolutions",
        "Route 101: Zigzagoon + Linoone",
        "Route 101: Poochyena + Mightyena",
        "Route 101 / Petalburg Woods: Wurmple + Silcoon + Beautifly + Cascoon + Dustox",
        "Route 103: Wingull + Pelipper",
        "Route 102: Ralts + Kirlia + Gardevoir",
        "Route 102: Surskit + Masquerain",
        "Route 102: Lotad + Lombre",
        "Route 104: Taillow + Swellow",
        "Petalburg Woods: Shroomish + Breloom",
        "Petalburg Woods: Slakoth + Vigoroth + Slaking",
        "Rustboro in-game trade: Makuhita + Hariyama",
        "Route 116: Skitty",
        "Route 116: Whismur + Loudred + Exploud",
        "Route 116: Nincada + Ninjask + Shedinja",
        "Super Training: Skitty → Delcatty",
        "Super Training: male Kirlia → Gallade",
        "Super Training: Lombre → Ludicolo",
    ]),
    "Badge 1 → Badge 2 — Brawly": (53, [
        "Dewford Town Old Rod: Magikarp + Gyarados",
        "Dewford Town Old Rod: Tentacool + Tentacruel",
        "Petalburg City Old Rod: Goldeen + Seaking",
        "Granite Cave 1F: Zubat + Golbat + Crobat",
        "Granite Cave 1F: Abra + Kadabra",
        "Granite Cave 1F: Geodude + Graveler",
    ]),
    "Badge 2 → Badge 3 — Wattson": (80, [
        "Slateport Contest Hall gift: Cosplay Pikachu",
        "Route 110 Horde: Minun",
        "Route 110: Plusle",
        "Route 110: Electrike + Manectric",
        "Route 110: Gulpin + Swalot",
        "Route 110: Oddish + Gloom",
        "Route 110: Voltorb + Electrode",
        "Route 110 Horde: Magnemite + Magneton",
        "Super Training: Gloom → Vileplume",
        "Super Training: Gloom → Bellossom",
        "Granite Cave B1F/B2F: Aron + Lairon + Aggron",
        "Granite Cave B2F: Sableye",
        "Route 117: Volbeat",
        "Route 117: Illumise",
        "Route 117: Marill + Azumarill",
        "Route 117: Roselia",
        "Super Training: Roselia → Roserade",
        "Route 117 breeding: Budew",
        "Route 117 breeding: Azurill",
    ]),
    "Badge 3 → Badge 4 — Flannery": (104, [
        "Granite Cave B2F Rock Smash: Nosepass",
        "Route 112: Numel + Camerupt",
        "Route 112: Machop + Machoke",
        "Fiery Path: Grimer + Muk",
        "Fiery Path: Koffing + Weezing",
        "Fiery Path: Slugma + Magcargo",
        "Fiery Path: Torkoal",
        "Route 113: Spinda",
        "Route 113: Skarmory",
        "Route 113: Sandshrew + Sandslash",
        "Route 114: Swablu + Altaria",
        "Route 114: Seviper",
        "Meteor Falls: Lunatone",
        "Jagged Pass: Spoink + Grumpig",
        "Lavaridge Town egg: Wynaut + Wobbuffet",
    ]),
    "Badge 3 → Badge 5 — Norman": (113, [
        "Route 111: Trapinch + Vibrava + Flygon",
        "Route 111: Cacnea + Cacturne",
        "Route 111: Baltoy + Claydol",
        "Rustboro: revive Root or Claw Fossil + evolution",
    ]),
    "Badge 5 → Badge 6 — Winona": (133, [
        "Route 115: Jigglypuff + Wigglytuff",
        "Route 117 breeding: Igglybuff",
        "New Mauville: Magnezone",
        "New Mauville: Probopass",
        "Petalburg City Good Rod: Corphish + Crawdaunt",
        "Dewford Town Good Rod: Wailmer + Wailord",
        "Route 111 Good Rod: Barboach + Whiscash",
        "Route 118 Good Rod: Carvanha + Sharpedo",
        "Southern Island gift: Latias",
        "Route 118: Kecleon",
        "Route 119: Tropius",
        "Route 119 fishing: Feebas + Milotic",
        "Weather Institute gift: Castform",
        "Route 120: Absol",
    ]),
    "Badge 6 → Badge 7 — Tate & Liza": (170, [
        "Mt. Pyre: Shuppet + Banette",
        "Mt. Pyre interior: Duskull + Dusclops",
        "Mt. Pyre exterior: Vulpix + Ninetales",
        "Mt. Pyre exterior: Meditite + Medicham",
        "Mt. Pyre summit: Chimecho",
        "Safari Zone: Doduo + Dodrio",
        "Safari Zone: Psyduck + Golduck",
        "Safari Zone Area 1 normal grass: Rhyhorn + Rhydon",
        "Safari Zone Area 1 long grass: Heracross",
        "Safari Zone Area 2 normal grass: Donphan",
        "Safari Zone Area 2 long grass: Pinsir",
        "Safari Zone Area 3 normal grass: Xatu",
        "Safari Zone Area 4 normal grass: Pikachu → Raichu",
        "Safari Zone Area 4 long grass: Girafarig",
        "Route 117 breeding: Chingling",
        "Route 117 breeding: Phanpy",
        "Route 117 breeding: Natu",
        "Route 117 breeding: Pichu",
        "Shoal Cave: Spheal + Sealeo + Walrein",
        "Shoal Cave low tide: Snorunt + Glalie + Froslass",
        "Lilycove City Super Rod: Staryu + Starmie",
        "Route 128 Super Rod: Luvdisc",
        "Route 128 Super Rod: Corsola",
        "Route 130 Super Rod: Horsea + Seadra",
    ]),
    "Badge 7 → Badge 8 — Wallace": (387, [
        "Route 126 underwater: Clamperl",
        "Route 126 underwater: Chinchou + Lanturn",
        "Route 126 underwater: Relicanth",
        "Desert Ruins: Regirock",
        "Island Cave: Regice",
        "Ancient Tomb: Registeel",
        "Cave of Origin: Kyogre",
        "Soaring: Murkrow + Honchkrow",
        "Soaring: Drifloon + Drifblim",
        "Soaring: Braviary",
        "Mirage Spots: Venomoth",
        "Mirage Spots: Zebstrika",
        "Mirage Spots: Darmanitan",
        "Mirage Spots: Tangela + Tangrowth",
        "Mirage Spots: Larvesta + Volcarona",
        "Mirage Island: Persian",
        "Mirage Spots: Purugly / Glameow",
        "Mirage Spots: Porygon",
        "Mirage Spots: Audino",
        "Mirage Spots Rock Smash: Binacle + Barbaracle",
        "Mirage Spots: Munna + Musharna",
        "Mirage Spots / Mirage Cave: Ditto",
        "Mirage Island: Maractus",
        "Mirage Forest/Mountain: Forretress",
        "Mirage Forest/Mountain: Happiny + Chansey + Blissey",
        "Mirage Forest: Sunkern + Sunflora",
        "Mirage Forest/Mountain: Kricketune",
        "Mirage Forest: Petilil + Lilligant",
        "Mirage Forest: Cherrim",
        "Mirage Forest: Minccino + Cinccino",
        "Mirage Cave: Unown",
        "Mirage Cave: Tynamo + Eelektrik + Eelektross",
        "Mirage Cave: Slowpoke + Slowbro",
        "Mirage Cave: Klink + Klang + Klinklang",
        "Mirage Cave: Excadrill",
        "Mirage Cave: Cofagrigus",
        "Mirage Mountain: Stantler",
        "Mirage Mountain: Vullaby + Mandibuzz",
        "Mirage Mountain: Magby + Magmar",
        "Mirage Mountain: Elekid + Electabuzz",
        "Crescent Isle: Cresselia",
        "Mirage Spot Old Amber: Aerodactyl",
        "Mirage Spot Helix Fossil: Omanyte + Omastar",
        "Mirage Spot Skull Fossil: Cranidos + Rampardos",
        "Mirage Spot Cover Fossil: Tirtouga + Carracosta",
        "Pathless Plain: Cobalion",
        "Pathless Plain: Virizion",
        "Pathless Plain: Terrakion",
        "Nameless Cavern: Mesprit",
        "Nameless Cavern: Uxie",
        "Nameless Cavern: Azelf",
        "Fabled Cave: Zekrom",
        "Soaring sky: Dialga",
        "Soaring sky: Thundurus",
        "Route 101 DexNav: Lillipup + Herdier + Stoutland",
        "Route 101 DexNav: Zorua + Zoroark",
        "Route 101 DexNav: Sewaddle + Swadloon + Leavanny",
        "Route 103 DexNav: Chatot",
        "Route 103 DexNav: Shellos + Gastrodon",
        "Route 102 DexNav: Gothita + Gothorita + Gothitelle",
        "Route 102 DexNav: Tympole + Palpitoad + Seismitoad",
        "Route 104 DexNav: Pidove + Tranquill + Unfezant",
        "Petalburg Woods DexNav: Cottonee + Whimsicott",
        "Petalburg Woods DexNav: Paras + Parasect",
        "Petalburg Woods DexNav: Phantump",
        "Route 116 DexNav: Joltik + Galvantula",
        "Route 116 DexNav: Eevee + all eight evolutions",
        "Route 105 DexNav: Frillish + Jellicent",
        "Route 105 DexNav: Krabby + Kingler",
        "Route 105 DexNav: Clauncher + Clawitzer",
        "Island Cave: Regigigas",
        "Granite Cave DexNav: Timburr + Gurdurr",
        "Granite Cave DexNav: Axew + Fraxure + Haxorus",
        "Granite Cave DexNav: Onix",
        "Sea Mauville: Lugia",
        "Sea Mauville: Spiritomb",
        "Trackless Forest: Raikou",
        "Trackless Forest: Entei",
        "Trackless Forest: Suicune",
        "Route 110 DexNav: Trubbish + Garbodor",
        "Route 117 DexNav: Rattata + Raticate",
        "Route 117 DexNav: Deerling + Sawsbuck",
        "Route 111 DexNav: Sandile + Krokorok + Krookodile",
        "Route 111 DexNav: Dwebble + Crustle",
        "Route 111 DexNav: Gible + Gabite + Garchomp",
        "Route 112 DexNav: Ponyta + Rapidash",
        "Route 112 DexNav: Tyrogue + Hitmonlee + Hitmonchan + Hitmontop",
        "Route 112 DexNav: Sawk",
        "Fiery Path DexNav: Roggenrola + Boldore",
        "Fiery Path DexNav: Diglett + Dugtrio",
        "Route 113 DexNav: Scraggy + Scrafty",
        "Route 113 DexNav: Klefki",
        "Route 113 DexNav: Bouffalant",
        "Route 114 DexNav: Skorupi + Drapion",
        "Route 114 DexNav: Misdreavus + Mismagius",
        "Meteor Falls DexNav: Deino + Zweilous + Hydreigon",
        "Meteor Falls DexNav: Druddigon",
        "Meteor Falls DexNav: Clefairy + Clefable",
        "Jagged Pass DexNav: Mankey + Primeape",
        "Lavaridge egg: Togepi + Togetic + Togekiss",
        "Route 118 DexNav: Luxio + Luxray",
        "Route 118 DexNav: Aipom + Ambipom",
        "Scorched Slab: Heatran",
        "Route 121 DexNav: Elgyem + Beheeyem",
        "Route 121 DexNav: Hypno",
        "Safari Zone DexNav: Kakuna + Beedrill",
        "Safari Zone DexNav: Pidgeotto + Pidgeot",
        "Safari Zone DexNav: Buneary + Lopunny",
        "Route 122 DexNav: Finneon + Lumineon",
        "Route 122 DexNav: Alomomola",
        "Mt. Pyre DexNav: Bronzor + Bronzong",
        "Mt. Pyre DexNav: Growlithe + Arcanine",
        "Route 125 DexNav: Seel + Dewgong",
        "Shoal Cave DexNav: Cubchoo + Beartic",
        "Shoal Cave DexNav: Delibird",
        "Route 117 breeding: Rufflet",
        "Route 117 breeding: Venonat",
        "Route 117 breeding: Meowth",
        "Route 117 breeding: Pineco",
        "Route 117 breeding: Kricketot",
        "Route 117 breeding: Cherubi",
        "Route 117 breeding: Blitzle",
        "Route 117 breeding: Drilbur",
        "Route 117 breeding: Darumaka",
        "Route 117 breeding: Yamask",
        "Route 117 breeding: Glameow",
        "Route 117 breeding: Cleffa",
        "Route 117 breeding: Shinx",
        "Route 117 breeding: Drowzee",
        "Route 117 breeding: Weedle",
        "Route 117 breeding: Pidgey",
    ]),
    "Badge 8 → Elite Four": (390, ["Meteor Falls B1F back: Bagon + Shelgon + Salamence"]),
    "Delta Episode": (397, [
        "Route 101 gift: one Johto starter + its evolutions",
        "Sky Pillar: Ariados",
        "Route 117 breeding: Spinarak",
        "Sky Pillar: Rayquaza",
        "Sky Pillar: Deoxys",
    ]),
    "Post-Delta Episode": (410, [
        "Route 101 gift: one Unova starter + its evolutions",
        "Mossdeep City gift: Beldum + Metang + Metagross",
        "Battle Resort surfing: Mantyke + Mantine",
        "Battle Resort Super Rod: Remoraid + Octillery",
        "Route 101 gift after second Elite Four: one Sinnoh starter + its evolutions",
    ]),
}


# Omega Ruby version-specific substitutions. The Gen 6 community spreadsheet
# explicitly provides separate Omega Ruby and Alpha Sapphire routes; we keep
# them separate here rather than pretending the AS route is identical to OR.
# Source family: ProfessorOak Gen 6 spreadsheet (OR/AS), with the AS route
# above as the structural base. Version-exclusive encounters/legendaries are
# swapped below.
OR_REPLACEMENTS = {
    "Lotad + Lombre": "Seedot + Nuzleaf",
    "Lombre → Ludicolo": "Nuzleaf → Shiftry",
    "Sableye": "Mawile",
    "Seviper": "Zangoose",
    "Lunatone": "Solrock",
    "Southern Island gift: Latias": "Southern Island gift: Latios",
    "Cave of Origin: Kyogre": "Cave of Origin: Groudon",
    "Fabled Cave: Zekrom": "Fabled Cave: Reshiram",
    "Soaring sky: Dialga": "Soaring sky: Palkia",
    "Soaring sky: Thundurus": "Soaring sky: Tornadus",
    "Sea Mauville: Lugia": "Sea Mauville: Ho-Oh",
    "Route 101: Poochyena + Mightyena": "Route 101: Poochyena + Mightyena",
}

def _make_omega_ruby_segments():
    out = {}
    for segment, (target, tasks) in OAK_SEGMENTS.items():
        replaced = []
        for task in tasks:
            new_task = task
            for old, new in OR_REPLACEMENTS.items():
                new_task = new_task.replace(old, new)
            replaced.append(new_task)
        out[segment] = (target, replaced)
    return out

OAK_SEGMENTS_BY_VERSION = {
    "Alpha Sapphire checklist": OAK_SEGMENTS,
    "Omega Ruby checklist": _make_omega_ruby_segments(),
}


STARTER_CHOICES = {
    "Hoenn starter": [
        ("Treecko line", ["Treecko", "Grovyle", "Sceptile"]),
        ("Torchic line", ["Torchic", "Combusken", "Blaziken"]),
        ("Mudkip line", ["Mudkip", "Marshtomp", "Swampert"]),
    ],
    "Johto starter": [
        ("Chikorita line", ["Chikorita", "Bayleef", "Meganium"]),
        ("Cyndaquil line", ["Cyndaquil", "Quilava", "Typhlosion"]),
        ("Totodile line", ["Totodile", "Croconaw", "Feraligatr"]),
    ],
    "Unova starter": [
        ("Snivy line", ["Snivy", "Servine", "Serperior"]),
        ("Tepig line", ["Tepig", "Pignite", "Emboar"]),
        ("Oshawott line", ["Oshawott", "Dewott", "Samurott"]),
    ],
    "Sinnoh starter": [
        ("Turtwig line", ["Turtwig", "Grotle", "Torterra"]),
        ("Chimchar line", ["Chimchar", "Monferno", "Infernape"]),
        ("Piplup line", ["Piplup", "Prinplup", "Empoleon"]),
    ],
}
FOSSIL_CHOICES = [
    ("Lileep line", ["Lileep", "Cradily"]),
    ("Anorith line", ["Anorith", "Armaldo"]),
]
EEVEE_EVOLUTIONS = ["Vaporeon", "Jolteon", "Flareon", "Espeon", "Umbreon", "Leafeon", "Glaceon", "Sylveon"]


def _species_entries_for_version(version, choices=None):
    """Build the individual-Pokémon Oak list from the researched route guide."""
    from pokebot.common.species_names import SPECIES_NAMES
    choices = choices or {}
    segments = OAK_SEGMENTS_BY_VERSION[version]
    names = [(int(i), n) for i, n in SPECIES_NAMES.items() if int(i) > 0]
    names.sort(key=lambda x: len(x[1]), reverse=True)
    found = {}

    for segment, (_target, tasks) in segments.items():
        for idx, task in enumerate(tasks):
            low = task.lower()
            for dex, name in names:
                if name.lower() in low:
                    rec = found.setdefault(name, {"dex": dex, "sources": [], "gate": segment})
                    source = {"gate": segment, "task": task, "task_index": idx}
                    if source not in rec["sources"]:
                        rec["sources"].append(source)

    # The guide uses shorthand for Eevee's eight evolutions. Add those eight
    # explicitly so the Pokémon tracker matches the guide's 9-Pokémon increment.
    eevee_gate = "Badge 7 → Badge 8 — Wallace"
    eevee_task = "Route 116 DexNav: Eevee + all eight evolutions"
    for name in EEVEE_EVOLUTIONS:
        if name in SPECIES_NAMES.values():
            dex = next(i for i, n in SPECIES_NAMES.items() if n == name)
            found.setdefault(name, {"dex": dex, "sources": [{"gate": eevee_gate, "task": eevee_task, "task_index": -3}], "gate": eevee_gate})

    # Replace each "one starter" shorthand with exactly one selected starter
    # line. This keeps the total at the researched single-game target instead of
    # incorrectly requiring all three starter families.
    starter_gate = {
        "Hoenn starter": "Pre-Badge 1 — Roxanne",
        "Johto starter": "Delta Episode",
        "Unova starter": "Post-Delta Episode",
        "Sinnoh starter": "Post-Delta Episode",
    }
    for group, options in STARTER_CHOICES.items():
        selected = choices.get(group, options[0][0])
        line = next((species for label, species in options if label == selected), options[0][1])
        gate = starter_gate[group]
        task = f"Choice group: {group} — {selected}"
        for name in line:
            if name in SPECIES_NAMES.values():
                dex = next(i for i, n in SPECIES_NAMES.items() if n == name)
                found[name] = {"dex": dex, "sources": [{"gate": gate, "task": task, "task_index": -1}], "gate": gate}

    # Only one Route 111 fossil line counts toward a single-game Oak total.
    selected_fossil = choices.get("Fossil", FOSSIL_CHOICES[0][0])
    fossil_line = next((species for label, species in FOSSIL_CHOICES if label == selected_fossil), FOSSIL_CHOICES[0][1])
    fossil_gate = "Badge 3 → Badge 5 — Norman"
    fossil_task = f"Fossil choice: {selected_fossil}"
    for name in fossil_line:
        if name in SPECIES_NAMES.values():
            dex = next(i for i, n in SPECIES_NAMES.items() if n == name)
            found[name] = {"dex": dex, "sources": [{"gate": fossil_gate, "task": fossil_task, "task_index": -2}], "gate": fossil_gate}

    opposite = {
        "Omega Ruby checklist": {"Lotad", "Lombre", "Ludicolo", "Sableye", "Seviper", "Lunatone", "Latias", "Kyogre", "Zekrom", "Dialga", "Thundurus", "Lugia"},
        "Alpha Sapphire checklist": {"Seedot", "Nuzleaf", "Shiftry", "Mawile", "Zangoose", "Solrock", "Latios", "Groudon", "Reshiram", "Palkia", "Tornadus", "Ho-Oh"},
    }
    for name in list(found):
        if name in opposite.get(version, set()):
            del found[name]
    return sorted(found.items(), key=lambda kv: (kv[1]["dex"], kv[0]))


class OakChallengePage(QWidget):
    """Manual ORAS Professor Oak challenge progress tracker.

    This intentionally tracks challenge milestones rather than claiming that
    RAM can prove every catch/evolution. It is local and persistent.
    """

    def __init__(self, profile_root, parent=None):
        super().__init__(parent)
        self.profile_root = Path(profile_root)
        self.state_path = self.profile_root / "professor_oak_oras.json"
        self.state = self._load()
        self.current_version = self.state.get("version", "Alpha Sapphire checklist")
        if self.current_version not in OAK_SEGMENTS_BY_VERSION:
            self.current_version = "Alpha Sapphire checklist"

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 8, 8, 8)
        outer.setSpacing(7)

        header = Panel("PROFESSOR OAK CHALLENGE — ORAS")
        top = QHBoxLayout()
        top.addWidget(QLabel("Version"))
        self.version = QComboBox()
        self.version.addItems(list(OAK_SEGMENTS_BY_VERSION.keys()))
        self.version.setCurrentText(self.current_version)
        self.version.currentTextChanged.connect(self._version_changed)
        self.version.setToolTip(
            "OR and AS have separate version-exclusive Pokémon and legendary routes. "
            "Select the game you are actually playing; progress is stored separately."
        )
        top.addWidget(self.version)
        self.active = QCheckBox("Oak Challenge active")
        self.active.setChecked(bool(self.state.get("active", False)))
        self.active.toggled.connect(self._active_changed)
        top.addWidget(self.active)
        top.addStretch(1)
        self.reset_btn = QPushButton("RESET PROGRESS")
        self.reset_btn.clicked.connect(self._reset)
        top.addWidget(self.reset_btn)
        header.outer.addLayout(top)

        note = QLabel(
            "Rule basis: obtain/catch and fully evolve everything available before "
            "the next badge/progression gate. Variations exist, so this tracker is "
            "a manual checklist. It is based on the researched Alpha Sapphire "
            "Professor Oak route and is not a new hunt automation mode."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        header.outer.addWidget(note)

        self.overall = QLabel()
        self.overall.setObjectName("EncounterLocationHeader")
        header.outer.addWidget(self.overall)
        self.bar = QProgressBar()
        self.bar.setRange(0, 100)
        self.bar.setTextVisible(True)
        header.outer.addWidget(self.bar)
        outer.addWidget(header)

        self.tabs = QTabWidget()
        outer.addWidget(self.tabs, 1)

        # Pokémon-level tracker: the useful day-to-day Oak view.
        species_page = QWidget()
        species_outer = QVBoxLayout(species_page)
        self.choice_layout = QHBoxLayout()
        self.choice_combos = {}
        for group, options in STARTER_CHOICES.items():
            self.choice_layout.addWidget(QLabel(group))
            combo = QComboBox()
            combo.addItems([label for label, _ in options])
            selected = self.state.setdefault("starter_choices", {}).get(group, options[0][0])
            combo.setCurrentText(selected)
            combo.currentTextChanged.connect(lambda value, g=group: self._choice_changed(g, value))
            self.choice_layout.addWidget(combo)
            self.choice_combos[group] = combo
        self.choice_layout.addWidget(QLabel("Fossil"))
        fossil_combo = QComboBox()
        fossil_combo.addItems([label for label, _ in FOSSIL_CHOICES])
        fossil_selected = self.state.setdefault("starter_choices", {}).get("Fossil", FOSSIL_CHOICES[0][0])
        fossil_combo.setCurrentText(fossil_selected)
        fossil_combo.currentTextChanged.connect(lambda value: self._choice_changed("Fossil", value))
        self.choice_layout.addWidget(fossil_combo)
        self.choice_combos["Fossil"] = fossil_combo
        self.choice_layout.addStretch(1)
        species_outer.addLayout(self.choice_layout)
        species_filters = QHBoxLayout()
        species_filters.addWidget(QLabel("Find"))
        self.species_search = QLineEdit()
        self.species_search.setPlaceholderText("Search Pokémon, gate or source…")
        self.species_search.textChanged.connect(self._apply_species_filter)
        species_filters.addWidget(self.species_search, 1)
        self.species_incomplete = QCheckBox("Incomplete only")
        self.species_incomplete.toggled.connect(self._apply_species_filter)
        species_filters.addWidget(self.species_incomplete)
        self.species_reset_btn = QPushButton("RESET POKÉMON")
        self.species_reset_btn.clicked.connect(self._reset_species)
        species_filters.addWidget(self.species_reset_btn)
        self.copy_missing_btn = QPushButton("COPY MISSING")
        self.copy_missing_btn.clicked.connect(self._copy_missing_species)
        species_filters.addWidget(self.copy_missing_btn)
        species_outer.addLayout(species_filters)
        self.species_scroll = QScrollArea()
        self.species_scroll.setWidgetResizable(True)
        species_content = QWidget()
        self.species_layout = QVBoxLayout(species_content)
        self.species_layout.setContentsMargins(0, 0, 0, 0)
        self.species_layout.setSpacing(4)
        self.species_scroll.setWidget(species_content)
        species_outer.addWidget(self.species_scroll, 1)
        self.tabs.addTab(species_page, "POKÉMON")

        # Keep the researched grouped route as a reference/guide.
        guide_page = QWidget()
        guide_outer = QVBoxLayout(guide_page)
        guide_filters = QHBoxLayout()
        guide_filters.addWidget(QLabel("Find"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search route, Pokémon, evolution, fishing, DexNav…")
        self.search.textChanged.connect(self._apply_filter)
        guide_filters.addWidget(self.search, 1)
        self.incomplete_only = QCheckBox("Incomplete only")
        self.incomplete_only.toggled.connect(self._apply_filter)
        guide_filters.addWidget(self.incomplete_only)
        guide_outer.addLayout(guide_filters)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        self.sections = QVBoxLayout(content)
        self.sections.setContentsMargins(0, 0, 0, 0)
        self.sections.setSpacing(7)
        scroll.setWidget(content)
        guide_outer.addWidget(scroll, 1)
        self.tabs.addTab(guide_page, "GUIDE / GATES")

        self._build_sections()
        self._build_species()

    def _choice_changed(self, group, value):
        self.state.setdefault("starter_choices", {})[group] = value
        self._save()
        self._build_species()

    def _build_species(self):
        while self.species_layout.count():
            item = self.species_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._species_checks = {}
        self._species_meta = {}
        completed = self.state.setdefault("species_completed_by_version", {}).setdefault(self.current_version, {})
        entries = _species_entries_for_version(self.current_version, self.state.get("starter_choices", {}))

        # Upgrade any HF98/HF99 grouped milestone checks into individual
        # Pokémon checks where the task names identify the species. This keeps
        # existing progress useful without pretending that a grouped check can
        # prove an individual catch when the source used shorthand.
        migrated = self.state.setdefault("species_migrated_by_version", {})
        if not migrated.get(self.current_version):
            guide_completed = self.state.setdefault("completed_by_version", {}).get(self.current_version, {})
            for key, value in guide_completed.items():
                if not value or "|" not in key:
                    continue
                segment, idx_text = key.rsplit("|", 1)
                try:
                    idx = int(idx_text)
                except ValueError:
                    continue
                tasks = OAK_SEGMENTS_BY_VERSION[self.current_version].get(segment, (0, []))[1]
                if not (0 <= idx < len(tasks)):
                    continue
                task = tasks[idx].lower()
                for name, meta in entries:
                    if name.lower() in task:
                        completed[f"{meta['dex']}|{name}"] = True
            migrated[self.current_version] = True
            self._save()
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

    def _load(self):
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    # HF98 stored a single AS completion map. Preserve it as AS
                    # when upgrading to the version-aware tracker.
                    if "completed_by_version" not in data:
                        old = data.get("completed", {})
                        data["completed_by_version"] = {
                            "Alpha Sapphire checklist": old if isinstance(old, dict) else {},
                            "Omega Ruby checklist": {},
                        }
                    data.setdefault("active", False)
                    data.setdefault("version", "Alpha Sapphire checklist")
                    data.setdefault("starter_choices", {
                        "Hoenn starter": "Treecko line",
                        "Johto starter": "Chikorita line",
                        "Unova starter": "Snivy line",
                        "Sinnoh starter": "Turtwig line",
                        "Fossil": "Lileep line",
                    })
                    data.setdefault("species_migrated_by_version", {
                        "Alpha Sapphire checklist": False,
                        "Omega Ruby checklist": False,
                    })
                    data.setdefault("species_completed_by_version", {
                        "Alpha Sapphire checklist": {},
                        "Omega Ruby checklist": {},
                    })
                    data["species_completed_by_version"].setdefault("Alpha Sapphire checklist", {})
                    data["species_completed_by_version"].setdefault("Omega Ruby checklist", {})
                    return data
        except Exception:
            pass
        return {
            "active": False,
            "version": "Alpha Sapphire checklist",
            "completed_by_version": {
                "Alpha Sapphire checklist": {},
                "Omega Ruby checklist": {},
            },
            "starter_choices": {
                "Hoenn starter": "Treecko line",
                "Johto starter": "Chikorita line",
                "Unova starter": "Snivy line",
                "Sinnoh starter": "Turtwig line",
                "Fossil": "Lileep line",
            },
            "species_migrated_by_version": {
                "Alpha Sapphire checklist": False,
                "Omega Ruby checklist": False,
            },
            "species_completed_by_version": {
                "Alpha Sapphire checklist": {},
                "Omega Ruby checklist": {},
            },
        }

    def _save(self):
        self.profile_root.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(
            json.dumps(self.state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def _active_changed(self, value):
        self.state["active"] = bool(value)
        self._save()

    def _version_changed(self, version):
        self.current_version = version
        self.state["version"] = version
        self._save()
        self._build_sections()
        self._build_species()

    def _reset(self):
        completed = self.state.setdefault("completed_by_version", {})
        completed[self.current_version] = {}
        self._save()
        self._build_sections()
        self._build_species()

    def _current_segments(self):
        return OAK_SEGMENTS_BY_VERSION[self.current_version]

    def _build_sections(self):
        # Rebuild the scroll contents so switching OR/AS also swaps the actual
        # version-specific checklist, not just the label.
        while self.sections.count():
            item = self.sections.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._checks = {}
        self._segment_labels = {}
        completed = self.state.setdefault("completed_by_version", {}).setdefault(
            self.current_version, {}
        )
        segments = self._current_segments()
        for segment, (target, tasks) in segments.items():
            panel = Panel(segment)
            row = QHBoxLayout()
            label = QLabel(f"Target Pokédex: {target}")
            label.setObjectName("Muted")
            row.addWidget(label)
            row.addStretch(1)
            self._segment_labels[segment] = QLabel()
            self._segment_labels[segment].setObjectName("Muted")
            row.addWidget(self._segment_labels[segment])
            panel.outer.addLayout(row)

            for idx, task in enumerate(tasks):
                key = f"{segment}|{idx}"
                cb = QCheckBox(task)
                cb.setChecked(bool(completed.get(key, False)))
                cb.setToolTip("Mark this catch/evolution milestone complete.")
                cb.toggled.connect(lambda checked, k=key: self._task_changed(k, checked))
                panel.outer.addWidget(cb)
                self._checks[key] = cb
            self.sections.addWidget(panel)
        self.sections.addStretch(1)
        self._apply_filter()
        self._refresh_progress()

    def _task_changed(self, key, checked):
        completed = self.state.setdefault("completed_by_version", {}).setdefault(
            self.current_version, {}
        )
        completed[key] = bool(checked)
        self._save()
        self._refresh_progress()
        self._apply_filter()

    def _apply_filter(self):
        query = self.search.text().strip().lower() if hasattr(self, "search") else ""
        incomplete = self.incomplete_only.isChecked() if hasattr(self, "incomplete_only") else False
        for cb in getattr(self, "_checks", {}).values():
            text = cb.text().lower()
            cb.setVisible((not query or query in text) and (not incomplete or not cb.isChecked()))

        # Keep section panels visible when they contain a matching task.
        for i in range(self.sections.count()):
            item = self.sections.itemAt(i)
            panel = item.widget()
            if panel is None or not hasattr(panel, "outer"):
                continue
            matches = False
            for cb in panel.findChildren(QCheckBox):
                if cb is self.active:
                    continue
                if cb.isVisible():
                    matches = True
                    break
            panel.setVisible(matches or (not query and not incomplete))

    def _refresh_progress(self):
        segments = self._current_segments()
        total = sum(len(tasks) for _, tasks in segments.values())
        done = sum(1 for cb in self._checks.values() if cb.isChecked())
        pct = int(round(done * 100 / total)) if total else 0
        self.overall.setText(
            f"{self.current_version}: {done}/{total} checklist milestones complete  •  {pct}%"
        )
        self.bar.setValue(pct)

        species_checks = getattr(self, "_species_checks", {})
        species_done = sum(1 for cb in species_checks.values() if cb.isChecked())
        species_total = len(species_checks)
        species_pct = int(round(species_done * 100 / species_total)) if species_total else 0
        if hasattr(self, "overall"):
            self.overall.setText(
                f"{self.current_version}: {species_done}/{species_total} Pokémon tracked complete  •  {species_pct}%  •  {done}/{total} guide milestones"
            )
            self.bar.setValue(species_pct)

        for segment, (_, tasks) in segments.items():
            count = sum(
                1 for idx in range(len(tasks))
                if self._checks.get(f"{segment}|{idx}") and
                self._checks[f"{segment}|{idx}"].isChecked()
            )
            self._segment_labels[segment].setText(
                f"{count}/{len(tasks)} milestones"
            )

