import argparse
import base64
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from typing import Literal
from urllib.request import Request, urlopen

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
MODEL = "qwen3-vl:8b-instruct"
# ponytail: calibrated to 2560x1440; replace when layout detection handles small panels.
GUIDED_PANELS = {
    "player_stats": ("player_stats", "player", "unknown", [515, 0, 730, 835]),
    "materials": ("materials", "unknown", "unknown", [515, 0, 735, 845]),
    "follower_stats": ("follower_stats", "follower", "unknown", [270, 0, 480, 835]),
    "follower_item": ("item", "follower", "equipped", [195, 0, 425, 650]),
}
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


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Panel(StrictModel):
    kind: Literal["item", "player_stats", "materials", "follower_stats", "unknown"]
    role: Literal["candidate", "equipped", "unknown"]
    owner: Literal["player", "follower", "unknown"]
    title: str
    box_1000: list[int] = Field(min_length=4, max_length=4)

    @field_validator("box_1000")
    @classmethod
    def validate_box(cls, box: list[int]) -> list[int]:
        if any(coordinate < 0 or coordinate > 1000 for coordinate in box):
            raise ValueError("box coordinates must be between 0 and 1000")
        if box[0] >= box[2] or box[1] >= box[3]:
            raise ValueError("box must have positive width and height")
        return box


class Item(StrictModel):
    role: Literal["candidate", "equipped", "unknown"]
    owner: Literal["player", "follower", "unknown"]
    name: str
    kind: Literal["equipment", "gem"] = "equipment"
    quality: Literal[
        "normal",
        "magic",
        "rare",
        "legendary",
        "ancient",
        "primal",
        "unknown",
    ]
    is_set: bool
    slot: str | None
    armor: int | None
    dps: float | None
    primary_affixes: list[str]
    secondary_affixes: list[str]
    socket_effects: list[str] = Field(default_factory=list)
    set_name: str | None
    legendary_power: str | None


class RawItem(StrictModel):
    role: Literal["candidate", "equipped", "unknown"]
    owner: Literal["player", "follower", "unknown"]
    name: str
    quality_text: str = Field(
        description="Exact colored type line beside the item icon, e.g. 'Uraltes Set: Stiefel'"
    )
    slot: str | None
    armor: int | None
    dps: float | None
    primary_affixes: list[str]
    secondary_affixes: list[str]
    socket_effects: list[str] = Field(
        default_factory=list,
        description="Each inserted gem effect or 'Leerer Sockel', preserving duplicates",
    )
    set_name: str | None = Field(
        description="Green set heading above the set-piece list; never the item name"
    )
    legendary_power: str | None = Field(
        description="Orange item-specific power only; never a green set-piece list or set bonus"
    )


class VisibleStats(StrictModel):
    values: dict[str, str]


class VisibleMaterials(StrictModel):
    values: dict[str, int]


class Recognition(StrictModel):
    screen_type: Literal[
        "single_item",
        "item_comparison",
        "player_stats",
        "materials",
        "follower_item",
        "follower_stats",
        "other",
    ]
    panels: list[Panel]
    items: list[Item]
    stats: dict[str, str]
    follower_stats: dict[str, str] = Field(default_factory=dict)
    materials: dict[str, int]
    confidence: float = Field(ge=0, le=1)
    source_images: list[str] = Field(default_factory=list)


class Layout(StrictModel):
    screen_type: Literal[
        "single_item",
        "item_comparison",
        "player_stats",
        "materials",
        "follower_item",
        "follower_stats",
        "other",
    ]
    panels: list[Panel]
    confidence: float = Field(ge=0, le=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def normalize_confidence(cls, value: int | float) -> float:
        return value / 100 if 1 < value <= 100 else value


def locate(
    image: Path,
    *,
    opener=urlopen,
    model: str = MODEL,
    keep_alive: int | str = 0,
) -> Layout:
    content = _ask(
        image,
        prompt=LAYOUT_PROMPT,
        schema=Layout.model_json_schema(),
        num_predict=512,
        opener=opener,
        model=model,
        keep_alive=keep_alive,
    )
    return Layout.model_validate_json(content)


def extract_item(
    image: Path,
    *,
    role: Literal["candidate", "equipped", "unknown"],
    owner: Literal["player", "follower", "unknown"],
    opener=urlopen,
    model: str = MODEL,
    keep_alive: int | str = 0,
) -> Item:
    content = _ask(
        image,
        prompt=ITEM_PROMPT.format(role=role, owner=owner),
        schema=RawItem.model_json_schema(),
        num_predict=2048,
        opener=opener,
        model=model,
        keep_alive=keep_alive,
    )
    return normalize_item(
        RawItem.model_validate_json(content), role=role, owner=owner
    )


def extract_stats(
    image: Path,
    *,
    owner: Literal["player", "follower"],
    opener=urlopen,
    model: str = MODEL,
    keep_alive: int | str = 0,
) -> dict[str, str]:
    content = _ask(
        image,
        prompt=STATS_PROMPT.format(owner=owner),
        schema=VisibleStats.model_json_schema(),
        num_predict=2048,
        opener=opener,
        model=model,
        keep_alive=keep_alive,
    )
    return VisibleStats.model_validate_json(content).values


def extract_materials(
    image: Path,
    *,
    opener=urlopen,
    model: str = MODEL,
    keep_alive: int | str = 0,
) -> dict[str, int]:
    content = _ask(
        image,
        prompt=MATERIALS_PROMPT,
        schema=VisibleMaterials.model_json_schema(),
        num_predict=2048,
        opener=opener,
        model=model,
        keep_alive=keep_alive,
    )
    return VisibleMaterials.model_validate_json(content).values


def normalize_item(
    raw: RawItem,
    *,
    role: Literal["candidate", "equipped", "unknown"],
    owner: Literal["player", "follower", "unknown"],
) -> Item:
    quality_text = raw.quality_text.casefold()
    is_gem = quality_text == "edelstein"
    # ponytail: observed weapon slots only; add labels when a new screenshot needs one.
    is_weapon = raw.slot is not None and raw.slot.casefold() in {"einhand", "zweihand"}
    name = raw.name
    set_name = raw.set_name or None
    if set_name and name.casefold() == quality_text and "set" not in quality_text:
        name, set_name = set_name, None
    is_set = "set" in quality_text or (not quality_text and set_name is not None)
    if not quality_text and set_name:
        if "set" in set_name.casefold():
            quality_text = set_name.casefold()
            set_name = None
        else:
            quality_text = "legendär"
    if "archaisch" in quality_text:
        quality = "primal"
    elif "uralt" in quality_text:
        quality = "ancient"
    elif "legendär" in quality_text or is_set:
        quality = "legendary"
    elif "selten" in quality_text:
        quality = "rare"
    elif "magisch" in quality_text:
        quality = "magic"
    elif "normal" in quality_text:
        quality = "normal"
    else:
        quality = "unknown"
    secondary_affixes = list(raw.secondary_affixes)
    legendary_power = raw.legendary_power
    socket_effects = list(raw.socket_effects)
    if legendary_power and "\n" not in legendary_power and (
        legendary_power.startswith("+")
        or legendary_power.startswith("Kritischer Trefferschaden")
    ):
        if legendary_power not in socket_effects:
            socket_effects.append(legendary_power)
        legendary_power = None
    if not is_set:
        if legendary_power == set_name or (
            legendary_power
            and "\n" in legendary_power
            and all(
                line.strip().startswith("+") for line in legendary_power.splitlines()
            )
        ):
            legendary_power = None
        set_name = None
        if legendary_power is None:
            # ponytail: recover this follower effect; extend only for observed misreads.
            radiating = [
                affix for affix in secondary_affixes if "(Strahlt aus)" in affix
            ]
            if len(radiating) == 1:
                legendary_power = radiating[0]
        if legendary_power in secondary_affixes:
            secondary_affixes.remove(legendary_power)
    return Item(
        role=role,
        owner=owner,
        name=name,
        kind="gem" if is_gem else "equipment",
        quality=quality,
        is_set=is_set,
        slot=None if is_gem else raw.slot,
        armor=None if is_gem or is_weapon else raw.armor,
        dps=raw.dps if is_weapon and raw.dps != 0 else None,
        primary_affixes=raw.primary_affixes,
        secondary_affixes=secondary_affixes,
        socket_effects=socket_effects,
        set_name=set_name,
        legendary_power=None if is_set else legendary_power,
    )


def _ask(
    image: Path,
    *,
    prompt: str,
    schema: dict,
    num_predict: int,
    opener,
    model: str,
    keep_alive: int | str,
) -> str:
    payload = {
        "model": model,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": 0, "num_predict": num_predict},
        "format": schema,
        "messages": [
            {
                "role": "user",
                "content": prompt,
                "images": [base64.b64encode(image.read_bytes()).decode("ascii")],
            }
        ],
    }
    request = Request(
        OLLAMA_URL,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=120) as response:
        return json.loads(response.read())["message"]["content"]


def unload(*, opener=urlopen, model: str = MODEL) -> None:
    request = Request(
        OLLAMA_GENERATE_URL,
        data=json.dumps({"model": model, "keep_alive": 0}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener(request, timeout=30) as response:
        response.read()


def analyze(
    image: Path,
    *,
    model: str = MODEL,
    screen_type: Literal[
        "player_stats", "materials", "follower_stats", "follower_item"
    ] | None = None,
    unload_model: bool = True,
) -> Recognition:
    if screen_type is not None:
        with Image.open(image) as source:
            if source.size != (2560, 1440):
                raise ValueError("guided screen types require a 2560x1440 screenshot")
    try:
        if screen_type is None:
            layout = locate(image, model=model, keep_alive="5m")
        else:
            kind, owner, role, box = GUIDED_PANELS[screen_type]
            layout = Layout(
                screen_type=screen_type,
                panels=[
                    Panel(
                        kind=kind,
                        role=role,
                        owner=owner,
                        title=screen_type,
                        box_1000=box,
                    )
                ],
                confidence=1.0,
            )
        items = []
        stats = {}
        follower_stats = {}
        materials = {}
        with TemporaryDirectory() as root:
            for index, panel in enumerate(layout.panels):
                if panel.kind == "unknown":
                    continue
                crop = crop_panel(
                    image,
                    panel.box_1000,
                    Path(root) / f"panel-{index}.png",
                )
                if panel.kind == "item":
                    items.append(
                        extract_item(
                            crop,
                            role=panel.role,
                            owner=panel.owner,
                            model=model,
                            keep_alive="5m",
                        )
                    )
                elif panel.kind == "materials":
                    materials.update(
                        extract_materials(crop, model=model, keep_alive="5m")
                    )
                elif panel.kind in {"player_stats", "follower_stats"}:
                    owner = "follower" if panel.kind == "follower_stats" else "player"
                    target = follower_stats if owner == "follower" else stats
                    target.update(
                        extract_stats(crop, owner=owner, model=model, keep_alive="5m")
                    )
        return Recognition(
            screen_type=layout.screen_type,
            panels=layout.panels,
            items=items,
            stats=stats,
            follower_stats=follower_stats,
            materials=materials,
            confidence=layout.confidence,
            source_images=[image.name],
        )
    finally:
        if unload_model:
            unload(model=model)


def analyze_many(
    images: list[Path],
    *,
    screen_type: Literal["player_stats", "materials", "follower_stats"],
    model: str = MODEL,
) -> Recognition:
    if not images or screen_type not in {"player_stats", "materials", "follower_stats"}:
        raise ValueError("multiple images require a stats or materials screen type")
    try:
        result = analyze(
            images[0], model=model, screen_type=screen_type, unload_model=False
        )
        for image in images[1:]:
            next_result = analyze(
                image, model=model, screen_type=screen_type, unload_model=False
            )
            result.stats.update(next_result.stats)
            result.follower_stats.update(next_result.follower_stats)
            result.materials.update(next_result.materials)
            result.panels.extend(next_result.panels)
            result.source_images.extend(next_result.source_images)
            result.confidence = min(result.confidence, next_result.confidence)
        return result
    finally:
        unload(model=model)


def main() -> None:
    parser = argparse.ArgumentParser(prog="d3coach")
    commands = parser.add_subparsers(dest="command", required=True)
    analyze_command = commands.add_parser("analyze")
    analyze_command.add_argument(
        "images", nargs="+", type=Path, help="Diablo III screenshot(s)"
    )
    analyze_command.add_argument("--model", default=MODEL)
    analyze_command.add_argument("--screen-type", choices=GUIDED_PANELS)
    watch_command = commands.add_parser("watch")
    watch_command.add_argument("--queue", type=Path, default=Path("screenshots"))
    watch_command.add_argument("--results", type=Path, default=Path("data/results"))
    watch_command.add_argument("--model", default=MODEL)
    args = parser.parse_args()
    if args.command == "analyze":
        if len(args.images) == 1:
            result = analyze(
                args.images[0], model=args.model, screen_type=args.screen_type
            )
        else:
            if args.screen_type not in {"player_stats", "materials", "follower_stats"}:
                parser.error(
                    "multiple screenshots require --screen-type player_stats, "
                    "materials or follower_stats"
                )
            result = analyze_many(
                args.images, model=args.model, screen_type=args.screen_type
            )
        print(result.model_dump_json(indent=2))
    else:
        watch(args.queue, args.results, model=args.model)


def crop_panel(
    source: Path,
    box_1000: list[int],
    target: Path,
    *,
    padding: int = 10,
) -> Path:
    with Image.open(source) as image:
        width, height = image.size
        left = max(0, box_1000[0] - padding) * width // 1000
        top = max(0, box_1000[1] - padding) * height // 1000
        right = min(1000, box_1000[2] + padding) * width // 1000
        bottom = min(1000, box_1000[3] + padding) * height // 1000
        target.parent.mkdir(parents=True, exist_ok=True)
        image.crop((left, top, right, bottom)).save(target)
    return target


def claim_next(incoming: Path, processing: Path) -> Path | None:
    incoming.mkdir(parents=True, exist_ok=True)
    processing.mkdir(parents=True, exist_ok=True)
    images = sorted(
        path for path in incoming.iterdir() if path.suffix.lower() in IMAGE_SUFFIXES
    )
    return images[0].replace(processing / images[0].name) if images else None


def process_next(queue: Path, results: Path, *, model: str = MODEL) -> bool:
    processing = queue / "processing"
    done = queue / "done"
    failed = queue / "failed"
    claimed = claim_next(queue / "incoming", processing)
    if claimed is None:
        return False
    done.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    try:
        recognition = analyze(claimed, model=model)
        results.mkdir(parents=True, exist_ok=True)
        target = results / f"{claimed.stem}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(recognition.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)
        claimed.replace(done / claimed.name)
    except Exception as error:
        claimed.replace(failed / claimed.name)
        (failed / f"{claimed.stem}.error.txt").write_text(str(error), encoding="utf-8")
    return True


def watch(
    queue: Path,
    results: Path,
    *,
    model: str = MODEL,
    poll_seconds: float = 1,
) -> None:
    while True:
        if not process_next(queue, results, model=model):
            sleep(poll_seconds)


if __name__ == "__main__":
    main()
