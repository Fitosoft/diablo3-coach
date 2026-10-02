import argparse
import base64
import json
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic, sleep
from typing import Annotated, Literal
from urllib.request import Request, urlopen

from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}
OLLAMA_URL = "http://localhost:11434/api/chat"
OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
MODEL = "qwen3-vl:8b-instruct"
NonNegativeInt = Annotated[int, Field(ge=0)]
# ponytail: calibrated to 2560x1440; replace when layout detection handles small panels.
GUIDED_PANELS = {
    "player_stats": ("player_stats", "player", "unknown", [515, 0, 730, 835]),
    "materials": ("materials", "unknown", "unknown", [515, 0, 735, 845]),
    "follower_stats": ("follower_stats", "follower", "unknown", [270, 0, 480, 835]),
    "follower_item": ("item", "follower", "equipped", [195, 0, 425, 650]),
}
# ponytail: calibrated crops; full-screen detection remains first for centered views.
FALLBACK_BOX = [156, 0, 820, 1000]
HALF_BOXES = [[0, 0, 516, 1000], [484, 0, 1000, 1000]]
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
    values: dict[str, NonNegativeInt]


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
    materials: dict[str, NonNegativeInt]
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

    @model_validator(mode="after")
    def deduplicate_panels(self):
        unique: dict[tuple[str, tuple[int, ...]], Panel] = {}
        for panel in self.panels:
            key = (panel.kind, tuple(panel.box_1000))
            if key not in unique or panel.role == "equipped":
                unique[key] = panel
        self.panels = list(unique.values())
        if self.screen_type == "item_comparison" and len(self.panels) == 1:
            self.screen_type = "single_item"
        return self


class StoredItem(StrictModel):
    item: Item
    location: Literal["equipped", "inventory", "stash", "unknown"] = "unknown"
    evidence: Literal["observed", "assumed"] = "observed"
    souvenir: bool = False
    cube_status: Literal["unknown", "needed", "not_needed"] = "unknown"
    source_images: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def protect_primal(self):
        self.souvenir = self.souvenir or self.item.quality == "primal"
        return self


class EquipmentAdvice(StrictModel):
    item_id: str
    action: Literal["equip"] = "equip"
    replaced_item_id: str | None
    replaced_action: Literal["keep", "cube", "salvage"] | None
    reason: str


class CoachState(StrictModel):
    schema_version: Literal[1] = 1
    character_class: str = "Kreuzritter"
    build: str = "Aegis der Tapferkeit"
    season: str | None = None
    goals: list[str] = Field(default_factory=lambda: ["Saisonreise abschließen", "Altar aufrüsten"])
    season_journey: dict[str, Literal["unknown", "todo", "done"]] = Field(default_factory=dict)
    altar: dict[str, Literal["unknown", "todo", "done"]] = Field(default_factory=dict)
    stats: dict[str, str] = Field(default_factory=dict)
    follower_stats: dict[str, str] = Field(default_factory=dict)
    materials: dict[str, NonNegativeInt] = Field(default_factory=dict)
    resources: dict[str, NonNegativeInt] = Field(default_factory=dict)
    items: dict[str, StoredItem] = Field(default_factory=dict)
    observations: list[str] = Field(default_factory=list)
    recommendations: list[EquipmentAdvice] = Field(default_factory=list)


def load_state(path: Path) -> CoachState:
    return CoachState.model_validate_json(path.read_text(encoding="utf-8")) if path.exists() else CoachState()


def save_state(path: Path, state: CoachState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(state.model_dump_json(indent=2), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def state_lock(path: Path, *, timeout: float = 5):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix(path.suffix + ".lock")
    deadline = monotonic() + timeout
    while True:
        try:
            handle = lock.open("x")
            break
        except FileExistsError:
            if monotonic() >= deadline:
                raise TimeoutError(f"State is locked: {lock}; after a crash remove this lock only when no writer is running")
            sleep(0.1)
    try:
        yield
    finally:
        handle.close()
        lock.unlink()


def disposal_advice(stored: StoredItem) -> tuple[str, str]:
    if stored.souvenir or stored.item.quality == "primal":
        return "keep", "Andenken/archaisches Item: aufheben."
    if stored.cube_status == "needed":
        return "cube", "Für die noch benötigte Würfelmacht aufheben und extrahieren."
    if stored.cube_status == "unknown":
        return "keep", "Würfelstatus unbekannt: vor dem Zerlegen prüfen."
    return "salvage", "Würfelprüfung abgeschlossen; kein Andenken."


def recommend_equip(state: CoachState, item_id: str, *, replace_id: str | None = None) -> EquipmentAdvice:
    stored = state.items[item_id]
    item = stored.item
    if item.kind != "equipment" or item.owner == "unknown" or not item.slot:
        raise ValueError("equipping requires equipment with a known owner and slot")
    # ponytail: rings and weapon combinations need explicit replacement, not a guessed slot.
    ambiguous = item.slot in {"Finger", "Einhand", "Zweihand", "Nebenhand"}
    if ambiguous and replace_id is None:
        raise ValueError("rings/weapons require --replace-id; an empty slot is not assumed")
    existing = [key for key, value in state.items.items() if key != item_id
                and value.location == "equipped" and value.item.owner == item.owner
                and value.item.slot == item.slot]
    if replace_id is None:
        if len(existing) > 1:
            raise ValueError("multiple equipped items require --replace-id")
        replace_id = existing[0] if existing else None
    elif replace_id == item_id or replace_id not in existing:
        raise ValueError("replacement must be an equipped item with the same owner and slot")
    action, reason = disposal_advice(state.items[replace_id]) if replace_id else (None, "Kein erfasstes Ersatzitem.")
    advice = EquipmentAdvice(item_id=item_id, replaced_item_id=replace_id,
                             replaced_action=action, reason=reason)
    if replace_id:
        state.items[replace_id].location = "inventory"
        state.items[replace_id].evidence = "assumed"
    stored.location = "equipped"
    stored.evidence = "assumed"
    stored.item = item.model_copy(update={"role": "equipped"})
    state.recommendations.append(advice)
    return advice


def update_state(state: CoachState, result: Recognition, *, location: str = "unknown",
                 item_id: str | None = None) -> None:
    if location not in {"unknown", "inventory", "stash"}:
        raise ValueError("candidate location must be unknown, inventory or stash")
    if item_id is not None and (item_id not in state.items or len(result.items) != 1):
        raise ValueError("--item-id requires one recognized item and an existing ID")
    if item_id and state.items[item_id].item.name.casefold() != result.items[0].name.casefold():
        raise ValueError("recognized name does not match --item-id")
    observation = sha256((result.model_dump_json() + location + str(item_id)).encode()).hexdigest()
    if observation in state.observations:
        return
    state.stats.update(result.stats)
    state.follower_stats.update(result.follower_stats)
    state.materials.update(result.materials)
    for index, item in enumerate(result.items):
        matches = [key for key, value in state.items.items()
                   if value.item.model_dump(exclude={"role"}) == item.model_dump(exclude={"role"})]
        known_slot = item.owner != "unknown" and item.slot and item.slot not in {"Finger", "Einhand", "Zweihand", "Nebenhand"}
        key = item_id
        if key is None and item.role == "equipped" and known_slot and len(matches) == 1:
            key = matches[0]
        if key is None:
            # ponytail: no fuzzy identity matching; new screenshots can be duplicate observations.
            key = sha256(json.dumps([result.source_images, index, item.model_dump()],
                                   sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]
        previous = state.items.get(key)
        if item.role == "equipped" and known_slot:
            for other_key, other in state.items.items():
                if other_key != key and other.location == "equipped" and other.item.owner == item.owner and other.item.slot == item.slot:
                    other.location = "unknown"
        state.items[key] = StoredItem(
            item=item, location="equipped" if item.role == "equipped" else
            location if location != "unknown" or previous is None else previous.location,
            souvenir=previous.souvenir if previous else False,
            cube_status=previous.cube_status if previous else "unknown",
            source_images=result.source_images,
        )
    state.observations.append(observation)


def persist_recognition(path: Path, result: Recognition, *, location: str = "unknown",
                        item_id: str | None = None) -> None:
    with state_lock(path):
        state = load_state(path)
        update_state(state, result, location=location, item_id=item_id)
        save_state(path, state)


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


def locate_with_fallback(
    image: Path, *, model: str = MODEL, keep_alive: int | str = 0
) -> Layout:
    layout = locate(image, model=model, keep_alive=keep_alive)
    if layout.panels:
        return layout
    with TemporaryDirectory() as root:
        for index, region in enumerate([FALLBACK_BOX, *HALF_BOXES]):
            crop = crop_panel(image, region, Path(root) / f"retry-{index}.png", padding=0)
            retry = locate(crop, model=model, keep_alive=keep_alive)
            panels = []
            for panel in retry.panels:
                if index and (panel.kind != "item" or panel.role != "candidate"):
                    continue
                box = panel.box_1000.copy()
                if (region[0] > 0 and box[0] <= 5) or (region[2] < 1000 and box[2] >= 995):
                    continue
                box[0] = region[0] + box[0] * (region[2] - region[0]) // 1000
                box[2] = region[0] + box[2] * (region[2] - region[0]) // 1000
                panels.append(panel.model_copy(update={"box_1000": box}))
            if panels:
                return Layout(screen_type="single_item" if len(panels) == 1 and
                              panels[0].kind == "item" else retry.screen_type,
                              panels=panels, confidence=retry.confidence)
    return layout


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
    if legendary_power and " - Stufe " in legendary_power.splitlines()[0]:
        for line in reversed(legendary_power.splitlines()):
            if line not in socket_effects:
                socket_effects.insert(0, line)
        legendary_power = None
    if raw.slot in {"Finger", "Hals"} and any(
        " - Stufe " in effect for effect in socket_effects
    ):
        socket_effects = [
            effect for effect in socket_effects if effect != "Leerer Sockel"
        ]
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
        armor=(
            None if is_gem or is_weapon or raw.slot in {"Finger", "Hals"} else raw.armor
        ),
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
            layout = locate_with_fallback(image, model=model, keep_alive="5m")
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
    analyze_command.add_argument("--state", type=Path, default=Path("data/state.json"))
    analyze_command.add_argument("--location", choices=["unknown", "inventory", "stash"], default="unknown")
    analyze_command.add_argument("--item-id", help="Existing item identity for a single-item screenshot")
    watch_command = commands.add_parser("watch")
    watch_command.add_argument("--queue", type=Path, default=Path("screenshots"))
    watch_command.add_argument("--results", type=Path, default=Path("data/results"))
    watch_command.add_argument("--model", default=MODEL)
    watch_command.add_argument("--state", type=Path, default=Path("data/state.json"))
    state_command = commands.add_parser("state", help="Inspect/update the local character state")
    state_command.add_argument("--file", type=Path, default=Path("data/state.json"))
    actions = state_command.add_subparsers(dest="state_action", required=True)
    actions.add_parser("show")
    configure = actions.add_parser("configure")
    configure.add_argument("--class", dest="character_class")
    configure.add_argument("--build")
    configure.add_argument("--season")
    configure.add_argument("--goal", action="append", help="Ordered goals; replaces the goal list")
    progress = actions.add_parser("progress")
    progress.add_argument("area", choices=["season_journey", "altar"])
    progress.add_argument("key")
    progress.add_argument("status", choices=["unknown", "todo", "done"])
    resource = actions.add_parser("resource")
    resource.add_argument("name", choices=["gold", "blood_shards"])
    resource.add_argument("amount", type=int)
    mark = actions.add_parser("item")
    mark.add_argument("id")
    mark.add_argument("--location", choices=["unknown", "inventory", "stash"])
    mark.add_argument("--souvenir", action="store_true")
    mark.add_argument("--cube-status", choices=["unknown", "needed", "not_needed"])
    equip = actions.add_parser("equip", help="Issue an equip recommendation and assume it is followed")
    equip.add_argument("id")
    equip.add_argument("--replace-id")
    ingest = actions.add_parser("ingest", help="Import saved recognition JSON without another GPU call")
    ingest.add_argument("result", type=Path)
    ingest.add_argument("--location", choices=["unknown", "inventory", "stash"], default="unknown")
    ingest.add_argument("--item-id")
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
        persist_recognition(args.state, result, location=args.location, item_id=args.item_id)
        print(result.model_dump_json(indent=2))
    elif args.command == "watch":
        watch(args.queue, args.results, model=args.model, state_path=args.state)
    else:
        with state_lock(args.file):
            state = load_state(args.file)
            if args.state_action == "configure":
                for field in ("character_class", "build", "season"):
                    if getattr(args, field) is not None:
                        setattr(state, field, getattr(args, field))
                if args.goal is not None:
                    state.goals = args.goal
            elif args.state_action == "progress":
                getattr(state, args.area)[args.key] = args.status
            elif args.state_action == "resource":
                if args.amount < 0:
                    parser.error("resource amount cannot be negative")
                state.resources[args.name] = args.amount
            elif args.state_action == "item":
                stored = state.items[args.id]
                if args.location:
                    stored.location = args.location
                if args.souvenir:
                    stored.souvenir = True
                if args.cube_status:
                    stored.cube_status = args.cube_status
            elif args.state_action == "equip":
                advice = recommend_equip(state, args.id, replace_id=args.replace_id)
                save_state(args.file, state)
                print(advice.model_dump_json(indent=2))
                return
            elif args.state_action == "ingest":
                update_state(state, Recognition.model_validate_json(args.result.read_text(encoding="utf-8")),
                             location=args.location, item_id=args.item_id)
            if args.state_action != "show":
                save_state(args.file, state)
            print(state.model_dump_json(indent=2))


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


def process_next(queue: Path, results: Path, *, model: str = MODEL,
                 state_path: Path | None = None) -> bool:
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
        persist_recognition(state_path or results.parent / "state.json", recognition)
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
    state_path: Path | None = None,
) -> None:
    while True:
        if not process_next(queue, results, model=model, state_path=state_path):
            sleep(poll_seconds)


if __name__ == "__main__":
    main()
