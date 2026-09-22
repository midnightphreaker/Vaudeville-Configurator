"""Who the 26 LLMUnity agents in Vaudeville actually are, and where they live.

This module is pure data derived from the shipped game files (Steam AppID 2240920,
Unity 2022.3.62f2, IL2CPP).  It exists so the configurator's UI can scope
word-choice (sampling) changes to a group of characters instead of one flat
list of 26 agents.

Everything below was read out of the game, not guessed -- see ``EVIDENCE``.

Stdlib only.  Safe to import on its own.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- #
# sentinel
# --------------------------------------------------------------------------- #

#: Dropdown value meaning "every character in the game at once".
#: This key deliberately does NOT appear in ``CAST_GROUPS`` (it has no single
#: file list of its own) and is NOT a value in ``GROUP_OF_FILE``.
ALL_KEY = "all"

#: Dropdown text for ``ALL_KEY``.  Exported so the UI and the help text cannot
#: drift apart (``FRIENDLY_GROUP_INTRO`` refers to this entry by name).
ALL_LABEL: str = "All characters (26)"


# --------------------------------------------------------------------------- #
# the cast, grouped the way the game itself groups it: one investigation
# location per group, plus one group for the Story Editor / Workshop cast.
#
# Order = Unity scene build order (level4 .. level13), Workshop last.
#
# ``characters`` is DISPLAY TEXT ONLY.  It is not ordered like the agent blobs
# inside ``files`` (the campaign groups happen to coincide; the Workshop group
# does not -- its list is in game-code archetype order, its blobs are in file
# order) and there is deliberately no per-character -> blob mapping here.
# Always patch at GROUP scope: every agent blob found in the group's ``files``.
# --------------------------------------------------------------------------- #

CAST_GROUPS: list[dict] = [
    {
        "key": "police_station",
        "label": "Police Station (2 characters)",
        "files": ["level4"],
        "characters": ["Constable Jones", "Chief Gretzky"],
        "note": "The two officers you question at the Vaudeville Police Station desk.",
    },
    {
        "key": "morgue",
        "label": "Morgue (1 character)",
        "files": ["level5"],
        "characters": ["Coroner Exteberria"],
        "note": "The coroner is the only person you can talk to at the morgue.",
    },
    {
        "key": "circus",
        "label": "Circus Saxabar (1 character)",
        "files": ["level6"],
        "characters": ["Monsieur Saxabar"],
        "note": "The ringmaster and lion tamer, backstage at Circus Saxabar.",
    },
    {
        "key": "theater",
        "label": "Cabane Violette theatre (1 character)",
        "files": ["level7"],
        "characters": ["Marina H"],
        "note": "The burlesque dancer, backstage at the Cabane Violette.",
    },
    {
        "key": "shop",
        "label": "Pascal's Groceries (2 characters)",
        "files": ["level8"],
        "characters": ["Pascal", "Mrs Potter"],
        "note": "The grocer and his regular customer, chatting inside the shop.",
    },
    {
        "key": "manor",
        "label": "Gravesen Manor (1 character)",
        "files": ["level9"],
        "characters": ["Count Gravesen"],
        "note": "The collector, at home among his trophies in Gravesen Manor.",
    },
    {
        "key": "country_club",
        "label": "Country Club (2 characters)",
        "files": ["level10"],
        "characters": ["Biagio", "Michelle"],
        "note": "The two investors and socialites you meet at the Country Club.",
    },
    {
        "key": "forest",
        "label": "Blackwoods Forest (1 character)",
        "files": ["level11"],
        "characters": ["Dada"],
        "note": "The hermit who lives out in Blackwoods Forest.",
    },
    {
        "key": "bar",
        "label": "Coral bar (1 character)",
        "files": ["level12"],
        "characters": ["Ingrid"],
        "note": "The young woman you meet at the Coral, the town's bar.",
    },
    {
        "key": "library",
        "label": "Public Library (1 character)",
        "files": ["level13"],
        "characters": ["Beatrix"],
        "note": "The librarian at the Vaudeville Public Library.",
    },
    {
        "key": "workshop",
        "label": "Workshop / Story Editor roles (13 characters)",
        "files": ["sharedassets18.assets"],
        "characters": [
            "Police Chief",
            "Police Officer",
            "Coroner",
            "Barmaid",
            "Shopkeeper",
            "Posh Lady",
            "Nobleman",
            "Librarian",
            "Circus Director",
            "Rich Woman",
            "Rich Man",
            "Dancer",
            "Hermit",
        ],
        "note": (
            "The Story Editor's 13 reusable roles, standing in for the same cast "
            "when you write and play your own Workshop stories. One shared set: "
            "the same thirteen roles are used by every custom story you play."
        ),
    },
]


# --------------------------------------------------------------------------- #
# asset file -> group key
#
# Keys are the exact on-disk names inside Vaudeville_Data/ as the scanner sees
# them: the scene files ship WITHOUT a ".assets" suffix ("level4", not
# "level4.assets").  The ten "levelN.assets" spellings below are aliases so a
# lookup also works if a caller uses the Unity build-settings path name.
# --------------------------------------------------------------------------- #

GROUP_OF_FILE: dict[str, str] = {
    # canonical, on-disk names (what vaudeville_configurator's Blob.path.name gives)
    "level4": "police_station",
    "level5": "morgue",
    "level6": "circus",
    "level7": "theater",
    "level8": "shop",
    "level9": "manor",
    "level10": "country_club",
    "level11": "forest",
    "level12": "bar",
    "level13": "library",
    "sharedassets18.assets": "workshop",
    # aliases: the same scenes as named in Unity's build settings
    "level4.assets": "police_station",
    "level5.assets": "morgue",
    "level6.assets": "circus",
    "level7.assets": "theater",
    "level8.assets": "shop",
    "level9.assets": "manor",
    "level10.assets": "country_club",
    "level11.assets": "forest",
    "level12.assets": "bar",
    "level13.assets": "library",
}

#: The 11 asset files that really exist on disk and really carry agents.
#: Iterate THIS (or ``CAST_GROUPS[*]["files"]``) when scoping a patch -- not
#: ``GROUP_OF_FILE``, whose extra "levelN.assets" keys are lookup aliases for
#: Unity's build-settings path names and do not exist in the install.
CANONICAL_FILES: list[str] = [f for g in CAST_GROUPS for f in g["files"]]


def group_of(name: str) -> str | None:
    """Resolve an asset filename to a cast group key, alias-tolerant."""
    return GROUP_OF_FILE.get(name) or GROUP_OF_FILE.get(name.removesuffix(".assets"))


# --------------------------------------------------------------------------- #
# the "levels are difficulty" theory: refuted.
# --------------------------------------------------------------------------- #

LEVELS_ARE_DIFFICULTY: bool = False

#: No difficulty scale exists in the game, so there is nothing to offer here.
DIFFICULTY_SCALE: list[tuple[int, str]] | None = None


# --------------------------------------------------------------------------- #
# persona / system prompt: NOT editable from here.
# --------------------------------------------------------------------------- #

#: False because the game rewrites every agent's systemPrompt at load time
#: (``CharacterPrompt.Awake`` for the campaign cast, ``WS_CharacterPrompt.Init``
#: for the Workshop cast), so an edit to the serialized string is silently
#: discarded on the next launch.  UIs should render a note, never a text box.
SYSTEM_PROMPT_EDITABLE: bool = False

#: Plain-language version of the above, for the UI to show as-is.
PERSONA_NOTE: str = (
    "A character's personality comes from the game's own script files, which the "
    "game reloads every time you play, so it cannot be rewritten here. What you "
    "can change is how each character answers - how creative, how repetitive, "
    "how long-winded - and those dials are the same set for every character."
)


# --------------------------------------------------------------------------- #
# user-facing prose
# --------------------------------------------------------------------------- #

FRIENDLY_GROUP_INTRO: str = (
    "Vaudeville has thirteen speaking characters, and each one hangs out in a "
    "particular place in town - the police station, the morgue, the circus, the "
    "theatre, the grocery shop, the manor, the country club, the forest, the bar "
    "and the library. This list lets you pick which of them you are tuning. "
    "Choose a place to change just the people you meet there, or choose the "
    "'All characters' entry to change everybody at once. The last group, "
    "Workshop / Story Editor roles, "
    "covers the Story Editor: the same thirteen roles reused when you write and "
    "play your own custom mysteries, so tune those separately if you want your "
    "own stories to behave differently from the built-in campaign."
)


# --------------------------------------------------------------------------- #
# evidence
# --------------------------------------------------------------------------- #

EVIDENCE: str = (
    "Scene identities come from the BuildSettings object (type 141, pathID 11) in "
    "Vaudeville_Data/globalgamemanagers, whose m_Scenes vector maps level4..level13 to "
    "Assets/Scenes/{PoliceStation, Morgue, CircusSaxabar, CabaneViolette, PascalsGroceries, "
    "GravesenManor, CountryClub, Blackwoods, Coral, PublicLibrary}.unity and level16..level29 to "
    "Assets/Scenes_WS/{StoryEditor, WS_SaveGameSelection, WS_Studio, WS_PoliceStation, WS_Morgue, "
    "WS_Library, WS_Bar, WS_Theater, WS_Shop, WS_Circus, WS_Manor, WS_Forest, WS_CountryClub, "
    "WS_AccusationRoom}.unity -- so the 'levels' are places, not rungs on a difficulty ladder. "
    "Each of level4..level13 carries one GameObject per character holding a CharacterPrompt plus an "
    "LLMUnity.LLMAgent (e.g. level4 GO 314 'Constable Jones' -> CharacterPrompt pathID 1188 + LLMAgent "
    "pathID 1191; level10 GO 202 'Michelle' -> 1251 + 1254 and GO 252 'Biagio' -> 1250 + 1253), giving "
    "exactly 13 campaign agents, and the CharacterPrompt blob decodes as promptText (PPtr<TextAsset>) "
    "then List<PlotTriggers> then AIName, yielding the names and prompt assets listed above "
    "(sharedassets4.279 Jones, 4.280 Gretzky, 5.199 Coroner, 6.191 Saxabar, 7.260 Marina, 8.1224 Pascal, "
    "8.1225 Potter, 9.152 Gravesen, 10.152 Biagio, 10.153 Michelle, 11.341 Dada, 12.147 Ingrid, 13.167 Beatrix). "
    "sharedassets18.assets instead holds 13 GameObjects named WS_PoliceChief, WS_PoliceOfficer, WS_Coroner, "
    "WS_Barmaid, WS_Shopkeeper, WS_PoshLady, WS_Nobleman, WS_Librarian, WS_CircusDirector, WS_RichWoman, "
    "WS_RichMan, WS_Dancer, WS_Hermit, each with a WS_CharacterPrompt (zero serialized bytes, because its only public "
    "field is List<WS_PlotTrigger> and WS_PlotTrigger is not [Serializable], while the remaining fields are "
    "private/[CompilerGenerated]) plus an LLMAgent, and it is "
    "referenced only by level18 = Assets/Scenes_WS/WS_Studio.unity, i.e. the Story Editor cast "
    "(a 14th WS_* GameObject, WS_Location, also lives there but carries no agent, so do not filter "
    "agents by the WS_ name prefix). "
    "Il2CppDumper's dump.cs confirms the vocabulary: 'enum Actors' (TypeDefIndex 13015) has 13 members "
    "PoliceChief..Hermit, 'enum Locations' (13018) has 10 members PoliceStation..CountryClub, and "
    "ActorData..cctor (RVA 0x2AD3100) builds 13 Actor objects pairing each archetype with a campaign name "
    "(both literal sets are present and the in-game roster corroborates 11 of the 13 pairings; "
    "Barmaid<->Ingrid is inferred from her being the only agent in level12 = Coral, and 'Barmaid' is the "
    "Story Editor role slot rather than the job her own prompt states, which is dancer) "
    "(Police Chief (M)/Gretzky, Police Officer (M)/Jones, Coroner (F)/Exteberria, Barmaid (F)/Ingrid, "
    "Shopkeeper (M)/Pascal, Posh Lady (F)/Mrs Potter, Nobleman (M)/Count Gravesen, Librarian (F)/Beatrix, "
    "Circus Director (M)/Saxabar, Rich Woman (F)/Michelle, Rich Man (M)/Biagio, Dancer (F)/Marina, "
    "Hermit (F)/Dada), while LocationData..cctor (RVA 0x2AD3E00) pairs each location with its venue and its "
    "WS_ scene (Bar -> 'Coral' -> WS_Bar, Theater -> 'Cabane Violette' -> WS_Theater, and so on). "
    "On difficulty: the string 'difficulty' occurs zero times in dump.cs (1,034,060 lines), in "
    "Il2CppDumper's stringliteral.json and script.json, and in the extracted metadata identifier and string "
    "literal tables, and the only game-code enums near the cast are Actors and Locations -- the nearest thing "
    "to progression is 'enum PlotTriggers' {START, MORGUE, CORAL, CABANE, CIRCUS, MANOR, COUNTRY, LIBRARY, "
    "BLACKWOODS, GROCERY}, which StoryManager.TriggeredPlotTriggers uses to unlock more places to visit; "
    "StorySettings_SO only distinguishes two modes, 'Story' (StartingScene 'Studio') and 'Sandbox' "
    "(StartingScene 'Studio Sandbox'), so LEVELS_ARE_DIFFICULTY is False and DIFFICULTY_SCALE is None. "
    "One caveat worth knowing: no agent stores a persona in its serialized _systemPrompt -- 24 of the 26 hold "
    "LLMUnity's stock default ('A chat between a curious human and an artificial intelligence assistant...') "
    "and the two in level4 hold an empty string, because CharacterPrompt.Awake (RVA 0x2AA6000) calls "
    "TextAsset.get_text then LLMAgent.set_systemPrompt at load time (logging 'No prompt text found for ' + "
    "gameObject.name if the asset is missing); the serialized agent fields the configurator edits are the "
    "sampling parameters, which are the same across all 26 agents (temperature 0.2, topK 40, topP 0.9, "
    "minP 0.05, repeatPenalty 1.1, repeatLastN 64, numPredict -1)."
)
