"""German Diablo III recognition vocabulary, not application UI translations.

Adding a language requires prompts, matching labels and normalization tests.
Raw item names, affixes and visible values keep the game's language.
"""
from typing import Literal

HeaderTitle = Literal["", "TEMPLER", "VERZAUBERIN", "SCHUFT", "BEUTETRUHE", "DETAILS", "MATERIALIEN", "INVENTAR"]
FOLLOWER_TITLES = {"TEMPLER", "VERZAUBERIN", "SCHUFT"}
MATERIALS_TITLE = "MATERIALIEN"
DETAILS_TITLE = "DETAILS"
STASH_TITLE = "BEUTETRUHE"
INVENTORY_TITLE = "INVENTAR"
AMBIGUOUS_SLOTS = {"Finger", "Einhand", "Zweihand", "Nebenhand"}
WEAPON_SLOTS = {"einhand", "zweihand"}
JEWELRY_SLOTS = {"Finger", "Hals"}
GEM_LABEL = "edelstein"
SET_LABEL = "set"
LEGENDARY_LABEL = "legendär"
PRIMAL_LABEL = "archaisch"
ANCIENT_LABEL = "uralt"
RARE_LABEL = "selten"
MAGIC_LABEL = "magisch"
NORMAL_LABEL = "normal"
GEM_LEVEL_MARKER = " - Stufe "
EMPTY_SOCKET = "Leerer Sockel"
CRITICAL_DAMAGE_PREFIX = "Kritischer Trefferschaden"
EMANATES_MARKER = "(Strahlt aus)"

LAYOUT_PROMPT = """Analysiere das Layout dieses deutschen Diablo-III-Screenshots.
Lokalisiere nur schwarz umrandete Tooltip- oder Listenfenster mit lesbarem Text.
Ein Item-Panel enthält einen Itemnamen, Ausrüstungswerte und Affixe. Die Ansicht
INVENTAR allein ist KEIN Item-Panel. Erfinde keine Items aus Ausrüstungssymbolen.
DETAILS mit OFFENSIV/DEFENSIV ist player_stats, außer die Begleiteransicht TEMPLER
ist geöffnet: Dann ist es follower_stats mit owner=follower.
MATERIALIEN mit HANDWERK ist materials. Erfasse jeweils das gesamte sichtbare
Listenfenster, nicht das Inventar daneben.
Ein Item-Panel mit der Überschrift AUSGERÜSTET hat role=equipped. Ein daneben
angezeigtes Vergleichsitem hat role=candidate. In der TEMPLER-Ansicht ist
owner=follower, sonst owner=player.
Koordinaten in box_1000 sind ganze Zahlen von 0 bis 1000 relativ zum Vollbild.
Nutze other und leere panels, wenn keines der Fenster sicher sichtbar ist.
Erfinde weder Panels noch Koordinaten."""
ITEM_PROMPT = """Extrahiere exakt das einzelne sichtbare deutsche Diablo-III-Item.
role={role} und owner={owner} sind vorgegeben.
name ist die große Überschrift ganz oben im Tooltip, unabhängig von ihrer
Farbe (gold, grün, gelb, blau). Beispiel: PFEILMACHERGRIMM, nicht Seltener Köcher.
quality_text ist die farbige Typenzeile neben dem Item-Icon, zum Beispiel
"Seltener Köcher" oder "Uraltes Set: Brustschutz". "Nebenhand" und
"Dämonenjäger" sind keine quality_text-Werte. slot ist der Ausrüstungsplatz
rechts oben, nicht die Charakterklasse.
Übernimm sichtbare Zahlen, Wertebereiche und Affix-Texte exakt.
armor ist nur der große weiße Wert mit dem Label "Rüstung"; dps ist nur der
große weiße Wert mit dem Label "Schaden pro Sekunde". Dornenschaden ist kein dps.
socket_effects enthält jede Zeile mit eingesetztem Edelstein-Effekt separat
(auch identische Zeilen mehrfach) sowie "Leerer Sockel" für leere Sockel.
Diese Zeilen gehören nicht in primary_affixes oder secondary_affixes.
Bei einem eigenständigen Edelstein ist quality_text="Edelstein"; seine
Wirkungen bleiben in primary_affixes, socket_effects ist leer.
Ein eingesetzter legendärer Edelstein (Name, "Stufe" und Wirkungen) gehört
vollständig zu socket_effects, auch wenn seine Wirkung orange geschrieben ist.
legendary_power bezeichnet nur die Macht des Ausrüstungsgegenstands selbst.
set_name ist nur die grüne Set-Überschrift über der Set-Teileliste;
bei einem legendären Nicht-Set-Item muss set_name null sein.
legendary_power ist nur ein orangefarbener itemspezifischer Effekt mit
Rautenmarker. Goldener kursiver Flavor-Text ist kein Effekt und gehört weder
in set_name noch in legendary_power. Grüne Setlisten und Set-Boni gehören
ebenfalls nicht hinein.
Ein weißer Edelsteinwert wie "+280 Geschicklichkeit" und kosmetischer Text wie
"Verschwindendes Färbemittel" sind niemals legendary_power.
Auch der Edelsteinwert "Kritischer Trefferschaden um 130,0% erhöht" gehört
zu socket_effects, nicht zu legendary_power.
Ignoriere Werteveränderungen beim Anlegen, Set-Bonus-Texte, Flavor-Text, Verkaufswert und Haltbarkeit. Erfinde nichts."""
STATS_PROMPT = """Lies nur die sichtbaren Werte im DETAILS-Fenster für owner={owner}.
Gib jedes sichtbare Wertepaar als deutschen Anzeigenamen und exakten Wert-Text aus.
Lass Abschnittsüberschriften und abgeschnittene Zeilen weg. Erfinde nichts."""
MATERIALS_PROMPT = """Lies nur die sichtbaren Materialien im MATERIALIEN-Fenster.
Gib jeden Materialnamen und seine Anzahl als ganze Zahl ohne Tausenderpunkte aus.
Lass Abschnittsüberschriften und abgeschnittene Zeilen weg. Erfinde nichts."""
VIEW_PROMPT = """Lies nur den Fenstertitel in diesem Ausschnitt.
Erlaubte Titel sind TEMPLER, VERZAUBERIN, SCHUFT, BEUTETRUHE, DETAILS,
MATERIALIEN, INVENTAR. Bei keinem sichtbaren Titel leere Zeichenkette.
Keine Interpretation, nur sichtbare Buchstaben. Ignoriere Itemnamen und Symbole."""
