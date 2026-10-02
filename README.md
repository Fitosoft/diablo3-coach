# Diablo 3 Coach

Lokaler, screenshotbasierter Assistent für Diablo III. Der aktuelle Proof of
Concept erkennt Items sowie sichtbare Spielerwerte, Begleiterwerte und
Materialien in deutschen Screenshots und aktualisiert einen lokalen JSON-Status.
Ausrüstungs-Empfehlungen lassen sich manuell auslösen und werden als befolgt
erfasst. Eine automatische strategische Itembewertung ist noch nicht enthalten.

Die mitgelieferten Testfälle und bisherigen Screenshots sind nur Testdaten. Das
Projekt ist nicht an einen Build gebunden; insbesondere ist der frühere
Unheilige-Essenz-Build kein konfiguriertes Ziel.

## Technischer Ansatz

Der PoC verwendet bewusst eine kleine, nachvollziehbare Pipeline:

1. Ein Screenshot gelangt über `screenshots/incoming/` in eine dateibasierte
   Queue.
2. Bei 2560 × 1440 werden zunächst die roten Fensterrahmen an bekannten Stellen
   geprüft. Qwen liest nur die Titel kleiner Ausschnitte. Der Code unterscheidet
   anhand dieser Titel Spieler-/Begleiter-Details und Materialien und verwendet
   dafür die getesteten festen Listen-Ausschnitte. Begleiterporträts zählen nicht
   als geöffnete Begleiteransicht. Sonst bestimmt Qwen3-VL lokal in Ollama
   Bildschirmtyp, Panel-Rollen und normierte Koordinaten (`0..1000`). Falls
   das Vollbild gar kein Panel ergibt, versucht der PoC einmalig einen
   mittigen Bildausschnitt und rechnet dessen Koordinaten aufs Vollbild zurück.
   Bleibt auch dieser ohne vollständige Panels, folgen seriell überlappende
   linke/rechte Hälften (bis 51,6 % bzw. ab 48,4 % der Breite). Dort werden nur
   vollständige Item-Kandidaten übernommen, keine ausgerüsteten Vergleichsitems.
   Der erste erfolgreiche Ausschnitt beendet den Fallback. Zentrierte Ansichten
   werden weiterhin zuerst im Vollbild verarbeitet.
   Am Ausschnittrand abgeschnittene Panels werden verworfen. Für schmale
   Listenfenster gibt es zusätzlich einen geführten Modus mit festen
   Ausschnitten für die derzeitige Auflösung von 2560 × 1440.
3. Pillow schneidet jedes erkannte Item-Panel mit wenig Rand aus.
4. Dasselbe Vision-Modell extrahiert aus jedem Ausschnitt strukturierte
   Item-Daten oder sichtbare Listenzeilen. Vorgegebene Rollen wie `candidate`
   und `equipped` werden nicht erneut erraten.
5. Pydantic prüft das Modell-JSON strikt. Eine kleine deterministische
   Normalisierung trennt Qualitätsstufe (`legendary`, `ancient`, `primal`) von
   der unabhängigen Set-Zugehörigkeit (`is_set`). Eigenständige Edelsteine
   haben `kind=gem`; leere Sockel und eingesetzte Edelstein-Effekte stehen
   getrennt in `socket_effects` (auch mehrfach identische Effekte).
6. Das Ergebnis wird atomar unter `data/results/` gespeichert; der Screenshot
   wandert nach `done/` oder bei einem Fehler zusammen mit der Fehlermeldung
   nach `failed/`.

Der Watcher verarbeitet immer nur einen Screenshot gleichzeitig. So konkurrieren
nicht mehrere Modellaufrufe parallel mit Diablo III um die GPU. Zwischen
Lokalisierung und Extraktion bleibt das Modell kurz geladen; danach wird es auch
im Fehlerfall aus Ollama entladen. Der Zwei-Pass-Ansatz hält den zweiten Prompt
und Bildausschnitt klein und verhindert, dass benachbarte Tooltips vermischt
werden.

```text
Vollbild -> Layout-JSON -> Item-Crops -> Item-JSON -> Normalisierung -> Ergebnis
```

Das ist der aktuelle Harness: feste Schemas, reproduzierbare Testdaten, eine
serielle Queue und deterministische Nachbearbeitung um ein probabilistisches
Vision-Modell. Die automatische Lokalisierung schmaler Details- und
Materialfenster wird deshalb über die Titel zu festen Ausschnitten geleitet;
das ist auf die getestete Auflösung und Fensterpositionen begrenzt.
Auch der zusätzliche Halbbild-Fallback
garantiert keine Erkennung: Beim Sunwuko-Testitem fand Qwen den Tooltip links,
rechts jedoch weiterhin kein Panel. Eine hohe Modell-`confidence` ist kein Beleg
für vollständige Erkennung. Strategieregeln, Screenshot-Aufnahme und
Handy-Benachrichtigungen gehören noch nicht zum PoC. Würfel-Fertigkeiten,
Altar, Skillung und Rift-Ergebnisse sind spätere Ansichten. Auch die gefüllte
oder leere Raute eines legendären Effekts (bereits extrahiert/nicht extrahierbar
gegenüber noch extrahierbar) wird noch nicht ausgewertet.

Ein kurzer A/B-Versuch mit `qwen2.5vl:7b` auf denselben Screenshots zeigte
keinen robusteren Gesamtpfad: Auf Item-Ausschnitten las es teils Namen besser,
übernahm aber auch Vergleichswerte als Affixe. Bei Vollbildern lieferte es
Pixelkoordinaten oder erfundene Panels statt der verlangten normierten Boxen.
`qwen3-vl:8b-instruct` bleibt deshalb Standard; ein Modellwechsel ist erst
nach weiteren reproduzierbaren Bildtests sinnvoll.
Beim zunächst übersehenen Buriza/Yang-Vergleich halfen weder Kontrast × 1,3
noch Autokontrast; der mittige Ausschnitt lokalisierte dagegen beide Tooltips.

## Voraussetzungen

- Windows mit Python 3.12
- [uv](https://docs.astral.sh/uv/) für die projektlokale virtuelle Umgebung
- [Ollama](https://ollama.com/) mit `qwen3-vl:8b-instruct`

```powershell
ollama pull qwen3-vl:8b-instruct
uv sync
```

`uv` legt die Abhängigkeiten in `.venv` innerhalb des Projekts ab. Damit bleibt
die globale Python-Installation unangetastet und parallele Projekte verwenden
ihre eigenen Versionen.

## Verwendung

### Screenshots automatisch verarbeiten (Windows)

`start-coach.cmd` im Projektverzeichnis doppelklicken. Der Starter verwendet
die bestehende `.venv`, benötigt keine Administratorrechte und zeigt den
festen Eingangsordner an: `<Projektverzeichnis>\screenshots\incoming\`.
In Greenshot diesen Ordner als Speicherziel für PNG-Screenshots einstellen
und eindeutige Namen mit Datum/Uhrzeit verwenden. Den Starter während des
Spielens geöffnet lassen; `Strg+C` beendet die Verarbeitung.

Jeder Screenshot wird automatisch zugeordnet und seriell verarbeitet.
Spieler-Details, Materialien und Begleiter-Details benötigen bei der getesteten
Auflösung von 2560 × 1440 kein `--screen-type`. Ein eingeblendetes
ausgerüstetes Vergleichsitem darf sichtbar bleiben: Beide Tooltips werden
getrennt verarbeitet. Zum Erfassen der Ausrüstung direkt über den getragenen
Gegenstand fahren; zum Erfassen eines Drops über das Inventar-/Truhenitem.
Bei erkannter Truhe und Inventar wird ein Kandidat anhand seiner Tooltipposition
links/rechts als Truhen-/Inventaritem zugeordnet; ohne eindeutigen Kontext bleibt
der Ort unbekannt. Dafür gelten die bisher getesteten Tooltippositionen.

Neue Screenshots dürfen jederzeit eintreffen, auch während einer Analyse.
Der Watcher wartet kurz auf unveränderte Dateigröße/Änderungszeit. Das Fenster
zeigt `OK`, `PRÜFEN` oder `FEHLER` an. Unter `screenshots/` landen Bilder in:

- `done/`: verwertbare Erkennung, Status aktualisiert;
- `review/`: keine verwertbaren Daten, Status unverändert, Ergebnis-JSON erhalten;
- `failed/`: Fehler, Bild und Fehlermeldung erhalten;
- `processing/`: laufende Arbeit; nach einem Abbruch beim nächsten Start fortgesetzt.

Ergebnisse stehen in `data/results/`, der Status in `data/state.json`.
Zwei Scroll-Screenshots werden nacheinander eingelesen und ergänzen dieselben
Listen. Eine Sperrdatei verhindert einen zweiten Watcher derselben Queue. Nach
einem harten Prozessabbruch gegebenenfalls `screenshots/watcher.lock` entfernen,
aber erst nachdem der alte Watcher sicher beendet ist.

Ein Live-Test mit fünf lokalen Beispielen erkannte automatisch Spieler-Stats,
Materialien, Begleiter-Stats, ein Begleiter-Item und einen Itemvergleich und
aktualisierte einen getrennten Teststatus. Die Bilder sind nicht versioniert.
Die Textauslesung kann weiterhin Fehler enthalten; die Ordnerverarbeitung ist
noch keine Garantie korrekter Itembewertungen. Würfel, Altar und Rift-Ergebnisse
werden weiterhin nicht speziell ausgewertet.

Einen vorhandenen Screenshot direkt analysieren:

```powershell
uv run python d3coach.py analyze "C:\Pfad\zum\screenshot.png"
```

Für die bereits getesteten Ansichten bei 2560 × 1440 den Fenstertyp angeben:

```powershell
uv run python d3coach.py analyze "C:\Pfad\zum\materialien.png" --screen-type materials
uv run python d3coach.py analyze "C:\Pfad\zum\details.png" --screen-type player_stats
uv run python d3coach.py analyze "C:\Pfad\zum\templer-details.png" --screen-type follower_stats
uv run python d3coach.py analyze "C:\Pfad\zum\templer-item.png" --screen-type follower_item
```

Zwei Scroll-Screenshots derselben Liste lassen sich in einem seriellen Aufruf
zusammenführen. Gleiche *exakt geschriebene* Zeilennamen werden einmal
ausgegeben; der Wert des später angegebenen Bildes gewinnt:

```powershell
uv run python d3coach.py analyze "oben.png" "unten.png" --screen-type materials
```

Das Ergebnis nennt beide Quelldateien in `source_images`. `confidence` beschreibt
die Panelauswahl, nicht die Zuverlässigkeit jedes gelesenen Wertes. Im
geführten Modus ist die Panelauswahl vorgegeben. Modellfehler bei Namen oder
Zahlen müssen daher weiter am Screenshot geprüft werden.

Oder die serielle Queue starten und PNG-/JPG-Dateien nach
`screenshots/incoming/` kopieren:

```powershell
uv run python d3coach.py watch
```

## Lokaler Status

`analyze` und `watch` aktualisieren standardmäßig `data/state.json` nach einer
erfolgreichen Erkennung; `--state <pfad>` wählt eine andere Datei. Die Datei ist
Git-ignoriert. Sie enthält Charakterklasse, Build, Saison, geordnete Ziele,
Saisonreise-/Altarfortschritt, Spieler-/Begleiterwerte, Materialien, Ressourcen
und Items mit ID, Ort, Quelldateien, Andenkenschutz und Würfelstatus.
Standardprofil: Kreuzritter, Aegis der Tapferkeit; die Saison wird nicht erraten.

```powershell
uv run python d3coach.py state show
uv run python d3coach.py state configure --season "Meine Saison" --goal "Saisonreise abschließen" --goal "Altar aufrüsten"
uv run python d3coach.py state progress season_journey "Kapitel I" done
uv run python d3coach.py state progress altar "Mein nächster Knoten" todo
uv run python d3coach.py state resource gold 100000
uv run python d3coach.py state resource blood_shards 500
```

Saisonreise, Altar, Gold und Blutsplitter werden derzeit manuell gepflegt; ihre
Screenshot-Erkennung folgt später. Unbekannter Fortschritt wird nicht als
abgeschlossen gewertet. Sichtbare Stats/Materialien ergänzen bisherige Daten;
fehlende Scrollzeilen löschen nichts, redundante Werte werden ersetzt, nicht
addiert. Screenshots müssen deshalb in zeitlicher Reihenfolge eingelesen werden.
Gleiche Ergebnisdaten mit denselben Quellen und derselben Zuordnung sind
idempotent. Leere Erkennung löscht keine bestehende Ausrüstung.

Ein Item-Tooltip allein belegt keinen vollständigen Inventar- oder Truhenbestand.
Kandidaten bekommen bei erkanntem Kontext einen beobachteten Ort, sonst
`location=unknown`; ihr Ort kann beim direkten Aufruf angegeben werden.
Ein bereits gespeichertes Ergebnis lässt sich ohne
neuen GPU-Aufruf importieren:

```powershell
uv run python d3coach.py analyze "drop.png" --location stash
uv run python d3coach.py state ingest "data/results/drop.json" --location inventory
uv run python d3coach.py state item ITEM_ID --location stash --souvenir
```

Neue Kandidatenaufnahmen können dasselbe reale Item erneut erfassen. Gleiche
Namen oder Werte beweisen keine Identität, da mehrere Kopien existieren können.
Für ein bekanntes Item, insbesondere nach Verzaubern oder zum Korrigieren eines
angenommenen Ausrüstungswechsels, dessen ID mitgeben. Dafür muss das Ergebnis
genau einen Kandidaten mit passendem Namen oder ein einzelnes Item enthalten.
Ein ausgerüstetes Referenzitem darf daneben sichtbar sein:

```powershell
uv run python d3coach.py analyze "nach-verzaubern.png" --item-id ITEM_ID
uv run python d3coach.py state ingest "korrigiertes-ergebnis.json" --item-id ITEM_ID
```

Beobachtete ausgerüstete Items aktualisieren eindeutige Slots desselben Besitzers.
Spieler und Begleiter bleiben getrennt. Ringe und Waffen können mehrere Plätze
oder Kombinationen haben und werden deshalb nicht automatisch gegeneinander
ersetzt. Waffenwechsel zwischen verschiedenen Slottypen sind noch nicht unterstützt.

```powershell
uv run python d3coach.py state equip NEUE_ITEM_ID
uv run python d3coach.py state equip NEUE_RING_ID --replace-id ALTER_RING_ID
```

`state equip` ist eine vom Benutzer ausgewählte Empfehlung, kein automatisch
berechnetes Upgrade. Sie gibt auch die Folgeaktion für das ersetzte Item aus
und speichert die neue Ausrüstung sofort mit `evidence=assumed`. Das alte Item
wird zunächst im Inventar angenommen: Eine Empfehlung zum Zerlegen oder
Extrahieren behauptet nicht, dass diese Aktion bereits ausgeführt wurde.
Ein neuer Screenshot kann die Annahme korrigieren (`evidence=observed`);
ein dabei verdrängtes Item bekommt den Ort `unknown`, nicht einen erfundenen
Truhenplatz. Angezeigte Itemrollen bleiben die letzte Beobachtung; `location`
beschreibt den aktuellen gespeicherten Ort.

Archaische Items sind automatisch dauerhaft als Andenken geschützt. Auch
andere Items lassen sich mit `--souvenir` schützen. Für ersetzte Items gilt:
Andenken behalten; bei benötigter Würfelmacht zur Extraktion aufheben; bei
unbekanntem Würfelstatus behalten und prüfen. Nur nach expliziter Prüfung
(`not_needed`) kommt eine Zerlege-Empfehlung infrage. Andere strategische
Aufbewahrungsgründe müssen bis zur Regelengine als Andenken markiert werden.

```powershell
uv run python d3coach.py state item ITEM_ID --cube-status needed
uv run python d3coach.py state item ITEM_ID --cube-status not_needed
```

Die Raute wird noch nicht automatisch gelesen; der Würfelstatus bleibt daher
zunächst unbekannt. Die Speicherung nutzt atomaren Dateiersatz und eine
Schreibsperre gegen parallele Statusbefehle. Ungültiges Status-JSON wird nicht
zurückgesetzt. Nach einem Prozessabbruch kann eine `.json.lock` zurückbleiben;
diese erst entfernen, wenn kein Status-Schreiber mehr läuft. Scheitert im
Watcher nur das Statusupdate, bleiben Ergebnis-JSON und Screenshot erhalten
(Screenshot in `failed/`), damit sich das Ergebnis später erneut importieren lässt.

Alternative Modelle können beide Befehle mit `--model <ollama-name>` erhalten.
Die Verarbeitung bleibt vollständig lokal; der Code sendet Bilder nur an die
lokale Ollama-API unter `localhost:11434`.

## Entwicklung

Die Tests verwenden keine laufende Ollama-Instanz und müssen vor jedem Commit
grün sein:

```powershell
uv run python -m unittest -v
```

Die Tests erzeugen ihre kleinen Bilder selbst und simulieren Ollama. Echte
Spiel-Screenshots sind für andere Nutzer optional: eigene Bilder mit `analyze`
prüfen und das JSON mit der Anzeige vergleichen. Insbesondere Sockel, Effekte
und Klassenbeschränkungen sollten vor einer Entscheidung manuell geprüft
werden. Es sind keine Diablo-Bilder im öffentlichen Repository nötig.

`.idea/`, `*.iml`, `.venv/`, Screenshots und lokale Ergebnisdaten werden nicht
versioniert.

## Lizenz und Hinweis

Der Quellcode steht unter der [MIT-Lizenz](LICENSE). Sie erlaubt Nutzung,
Änderung und Weitergabe bei Erhalt des Copyright- und Lizenzhinweises.

Diablo und Diablo III sind Marken von Blizzard Entertainment. Dieses
Fan-Projekt ist weder mit Blizzard Entertainment verbunden noch von Blizzard
Entertainment unterstützt.
