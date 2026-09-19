APP_QSS = """
QWidget {
    background: #202225;
    color: #d7d9dc;
    font-family: "Segoe UI";
    font-size: 8.5pt;
}
QMainWindow, QFrame#MainFrame {
    background: #202225;
}
QFrame#Panel {
    background: #25282c;
    border: 1px solid #454a50;
    border-radius: 0px;
}
QFrame#TopTabs {
    background: #1c1e21;
    border: none;
    border-bottom: 1px solid #4a4e54;
}
QFrame#StatusBar {
    background: #191b1e;
    border-top: 1px solid #44484d;
}
QLabel#WindowTitle {
    color: #d7d9dc;
    font-size: 8.5pt;
}
QLabel#PanelTitle {
    color: #bfc3c7;
    font-size: 8pt;
    font-weight: 600;
    padding: 0px 0px 3px 0px;
}
QLabel#FieldLabel {
    color: #b8bcc1;
    font-weight: 600;
}
QLabel#Muted {
    color: #8f959c;
}
QLabel#Value {
    color: #eceeef;
    font-weight: 500;
}
QLabel#GreenText {
    color: #8fd18a;
    font-weight: 600;
}
QLabel#AmberText {
    color: #d9b66f;
    font-weight: 600;
}
QLabel#RedText {
    color: #df8585;
    font-weight: 600;
}
QLabel#IdleText {
    color: #cfd3d6;
    font-size: 12pt;
    font-weight: 600;
}
QLabel#SmallGreen {
    color: #8fd18a;
    font-weight: 600;
    font-size: 8pt;
}
QLabel#NotShiny {
    color: #d8dade;
    font-size: 12pt;
    font-weight: 600;
}
QPushButton {
    background: #34383d;
    color: #e5e7e9;
    border: 1px solid #5a6067;
    border-radius: 2px;
    padding: 4px 8px;
    font-weight: 500;
    min-height: 20px;
}
QPushButton:hover {
    background: #3d4248;
    border-color: #777d84;
}
QPushButton:pressed {
    background: #2c3034;
}
QPushButton:disabled {
    background: #292c30;
    color: #6f757b;
    border-color: #3d4247;
}
QPushButton#TabButton {
    background: #1c1e21;
    border: none;
    border-right: 1px solid #34383d;
    border-bottom: 2px solid transparent;
    border-radius: 0px;
    padding: 8px 14px 7px 14px;
    font-size: 8.5pt;
    font-weight: 600;
    color: #b8bcc1;
}
QPushButton#TabButton[active="true"] {
    background: #25282c;
    color: #f0f1f2;
    border-bottom: 2px solid #6d91b5;
}
QPushButton#TabButton:hover {
    background: #292d31;
    color: #ffffff;
}
QPushButton#GameButtonActive, QPushButton#HuntButtonActive {
    background: #3b4d5f;
    color: #ffffff;
    border: 1px solid #718aa3;
    border-radius: 2px;
    font-size: 8.5pt;
    font-weight: 600;
    min-height: 25px;
}
QPushButton#GameButtonInactive, QPushButton#HuntButtonInactive {
    background: #30343a;
    color: #c4c8cc;
    border: 1px solid #555b62;
    border-radius: 2px;
    font-size: 8.5pt;
    font-weight: 500;
    min-height: 25px;
}
QPushButton#SmallAction {
    background: #34383d;
    border: 1px solid #565c63;
    padding: 3px 7px;
    min-height: 19px;
}
QPushButton#SelectedStarter {
    background: #3b4d5f;
    border: 1px solid #718aa3;
    color: white;
    padding: 3px 7px;
    min-height: 19px;
}
QPushButton#StartButton {
    background: #3f7045;
    color: white;
    border: 1px solid #67936c;
    font-size: 9pt;
    font-weight: 600;
    min-height: 29px;
}
QPushButton#StartButton:hover { background: #497e50; }
QPushButton#StopButton {
    background: #743f3f;
    color: white;
    border: 1px solid #9a6666;
    font-size: 9pt;
    font-weight: 600;
    min-height: 29px;
}
QPushButton#StopButton:hover { background: #824848; }
QPushButton#ResetButton {
    background: #34383d;
    color: #d9dcdf;
    border: 1px solid #5a6067;
    font-size: 8.5pt;
    font-weight: 500;
    min-height: 24px;
}
QComboBox, QLineEdit, QSpinBox, QDoubleSpinBox {
    background: #181a1d;
    border: 1px solid #555b62;
    border-radius: 1px;
    padding: 3px 6px;
    color: #eceeef;
    min-height: 20px;
    selection-background-color: #4b6884;
}
QComboBox::drop-down {
    border: none;
    width: 18px;
}
QComboBox QAbstractItemView {
    background: #24272b;
    border: 1px solid #555b62;
    color: #e5e7e9;
    selection-background-color: #4b6884;
}
QTableWidget {
    background: #181a1d;
    alternate-background-color: #1e2125;
    border: 1px solid #484d53;
    color: #e6e8ea;
    gridline-color: #34383d;
    selection-background-color: #3d566e;
}
QTableWidget::item {
    padding: 2px 4px;
}
QHeaderView::section {
    background: #2a2d31;
    color: #cfd2d5;
    border: 0px;
    border-right: 1px solid #454a50;
    border-bottom: 1px solid #4b5056;
    padding: 4px;
    font-weight: 600;
}
QPlainTextEdit {
    background: #151719;
    border: 1px solid #484d53;
    color: #d7dadc;
    font-family: "Cascadia Mono", "Consolas";
    font-size: 8pt;
}
QCheckBox {
    color: #d2d5d8;
    spacing: 5px;
}
QScrollArea { border: none; }
QToolTip {
    background: #f2f2f2;
    color: #202020;
    border: 1px solid #8d8d8d;
    padding: 4px;
}
QFrame#PartyCard {
    background: #1d1f22;
    border: 1px solid #3f444a;
    border-radius: 0px;
}
QFrame#PartyCard:hover {
    background: #25292d;
    border: 1px solid #666d75;
}
QLabel#PartyImage {
    background: #17191c;
    border: none;
    color: #6f757b;
    font-size: 10pt;
}
QLabel#PartySlot {
    color: #9da2a7;
    font-weight: 600;
    font-size: 7.8pt;
}
QLabel#PartyName {
    color: #f0f1f2;
    font-weight: 600;
    font-size: 8.5pt;
}
QTableWidget#LastSeenTable {
    background: #181a1d;
    alternate-background-color: #181a1d;
    border: 1px solid #41464c;
    color: #e8eaec;
    gridline-color: transparent;
    selection-background-color: #181a1d;
    font-size: 8.5pt;
    font-weight: 500;
}
QTableWidget#LastSeenTable::item {
    background: #181a1d;
    border: none;
    border-bottom: 1px solid #25282c;
    padding: 1px 2px;
}
QTableWidget#LastSeenTable QHeaderView::section {
    background: #292c30;
    color: #c7cbd0;
    border: none;
    border-bottom: 1px solid #44494f;
    padding: 3px 2px;
    font-size: 8pt;
    font-weight: 600;
}
QPushButton#StatsResetButton {
    background: #743f3f;
    color: white;
    border: 1px solid #9a6666;
    border-radius: 2px;
    padding: 6px 10px;
    font-size: 9pt;
    font-weight: 600;
}
QLabel#PhaseProgressHeader {
    color: #bfc4c8;
    font-weight: 600;
    font-size: 8pt;
}
QProgressBar#PhaseProgress {
    border: 1px solid #4b5056;
    border-radius: 0px;
    background: #17191c;
    min-height: 10px;
}
QProgressBar#PhaseProgress::chunk {
    background: #607b94;
}
QListWidget#EncounterLocationList {
    background: #191b1e;
    border: 1px solid #484d53;
    color: #e0e2e4;
    padding: 2px;
}
QListWidget#EncounterLocationList::item {
    padding: 6px;
    border-bottom: 1px solid #34383d;
}
QListWidget#EncounterLocationList::item:selected {
    background: #3d566e;
    color: #ffffff;
}
QFrame#EncounterCard {
    background: #1d1f22;
    border: 1px solid #41464c;
    border-radius: 0px;
}
QFrame#EncounterCard:hover {
    border: 1px solid #6a7179;
    background: #25282c;
}
QLabel#EncounterName {
    color: #f0f1f2;
    font-size: 9pt;
    font-weight: 600;
}
QLabel#EncounterLocationHeader {
    color: #d7dade;
    font-size: 10pt;
    font-weight: 600;
}
QFrame#EncounterImageFrame {
    background: #191b1e;
    border: 1px solid #3d4247;
    border-radius: 0px;
}
QLabel#EncounterImage {
    background: transparent;
    border: none;
    color: #6d7379;
}
QLabel#EncounterImageCaption {
    color: #969ca2;
    font-size: 7pt;
    font-weight: 500;
}
QLabel#ShinyFoundCount {
    color: #d4b56c;
    font-weight: 600;
}
"""
