# Diablo 3 Coach

Lokaler, screenshotbasierter Assistent für Diablo III. Aktuell entsteht ein kleiner
PoC für serielle Screenshot-Verarbeitung und strukturierte Erkennung mit Ollama.

## Entwicklung

```powershell
uv run python -m unittest -v
uv run python d3coach.py analyze "data/replay/screenshot.png"
uv run python d3coach.py watch
```
