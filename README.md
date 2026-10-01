# Diablo 3 Coach

Lokaler, screenshotbasierter Assistent für Diablo III. Der aktuelle Proof of
Concept erkennt Items sowie sichtbare Spielerwerte, Begleiterwerte und
Materialien in deutschen Screenshots und schreibt sie als validiertes JSON.
Er automatisiert weder das Spiel noch strategische Entscheidungen.

Die mitgelieferten Testfälle und bisherigen Screenshots sind nur Testdaten. Das
Projekt ist nicht an einen Build gebunden; insbesondere ist der frühere
Unheilige-Essenz-Build kein konfiguriertes Ziel.

## Technischer Ansatz

Der PoC verwendet bewusst eine kleine, nachvollziehbare Pipeline:

1. Ein Screenshot gelangt über `screenshots/incoming/` in eine dateibasierte
   Queue.
2. Qwen3-VL lokal in Ollama bestimmt im automatischen Modus zunächst
   Bildschirmtyp, Panel-Rollen und normierte Koordinaten (`0..1000`). Für
   schmale Listenfenster gibt es einen geführten Modus mit festen Ausschnitten
   für die derzeitige Auflösung von 2560 × 1440.
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
Materialfenster ist noch unzuverlässig; auch einzelne Itemvergleiche werden
noch übersehen. Eine hohe Modell-`confidence` ist kein Beleg für vollständige
Erkennung. Strategieregeln, Charakter-State, Screenshot-Aufnahme und
Handy-Benachrichtigungen gehören noch nicht zum PoC. Würfel-Fertigkeiten,
Altar, Skillung und Rift-Ergebnisse sind spätere Ansichten.

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
