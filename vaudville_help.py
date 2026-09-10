"""Vaudville Configurator - the human-language layer.

Pure data: no imports, no logic, standard library not even required.  Import it
standalone (``import vaudville_help``) and read the constants.

Every string here is written for somebody who has never heard of an LLM.  The
GUI (vaudville_configurator.py) owns the widgets; this module owns the voice.

Contract
--------
GITHUB_URL   str                  project home, for an "About / Help" link
CREDIT       str                  author credit line
MODE_CARDS   dict[str, dict]      keys "off" / "direct" / "shim" (the tool's
                                  internal mode keys), each with "title",
                                  "blurb", "difficulty", "restrictions".
                                  blurb / difficulty / restrictions are the
                                  agreed copy - keep them verbatim.
TERMS        list[tuple[str, str]] (jargon, friendly replacement) for label
                                  rewriting and tooltips
GROUP_HELP   str                  one paragraph about the character-group
                                  dropdown; deliberately free of names and
                                  counts so any cast data can fill the list
SETTING_HELP dict[str, dict]      one entry per serialized field name.  Keys
                                  are the union of AGENT_LAYOUT + LLM_LAYOUT
                                  (46 names: 25 character-only, 17 engine-only,
                                  4 shared - advancedOptions, remote, port,
                                  APIKey).  Each entry: "what", "does",
                                  "why_change", "why_not", "range", "tips"
                                  (tips = 2-4 (value-or-range, quip) pairs).
MODEL_HELP   dict[str, str]       help for the two model slots and for what a
                                  mode allows you to load.  Canonical keys are
                                  the slot labels and the mode keys; friendly
                                  aliases (short names, GGUF filenames, CLI
                                  mode spellings) point at the same strings.
SHIM_HELP    dict[str, str]       help for the translator ("shim") controls.
                                  Canonical keys use the on-screen wording;
                                  snake_case aliases point at the same strings.

Facts the strings rely on (verified against the shipped build and LLMUnity
v3.0.0 source): in-place text edits must keep the same padded length, so
``host`` accepts 9-12 characters, ``model`` 33-36, ``systemPrompt`` 153-156,
and the shipped-empty fields (``APIKey``, ``grammar``, ``save``, ``SSLCert``,
``SSLKey``, ``lora``, ``loraWeights``) accept nothing at all.  ``advancedOptions``
is an editor show/hide flag with no effect in play.  The game rewrites
``numGPULayers``, ``model``, ``slot`` and ``systemPrompt`` at runtime.
"""

GITHUB_URL = "https://github.com/MidnightPhreaker/Vaudville-Configurator"
CREDIT = "MidnightPhreaker + Qwen"

# --------------------------------------------------------------------------- #
# The three modes.  "blurb", "difficulty" and "restrictions" are verbatim copy.
# --------------------------------------------------------------------------- #
MODE_CARDS: dict[str, dict] = {
    "off": {
        "title": "Local Mode - Basic",
        "blurb": ("Vaudville Configurator manages the AI model file for the game.  "
                  "Download new GGUF Models from Huggingface and let Vaudville "
                  "Configurator manage everything else!"),
        "difficulty": "Low",
        "restrictions": ("GGUF File, must be related to or created from "
                         "`Meta-Llama-3-8B-Instruct` (not 3.1 or later) only!"),
    },
    "direct": {
        "title": "Local Mode - Advanced",
        "blurb": ("Vaudville Configurator replaces the outdated and hardcoded "
                  "Llamalib built into the game, with the latest llama.cpp release "
                  "and allows you to select any GGUF model and tune all the "
                  "parameters!"),
        "difficulty": "Moderate",
        "restrictions": ("Must be a GGUF File compatible with latest llama.cpp, "
                         "must fit into local computer VRAM along with game!"),
    },
    "shim": {
        "title": "Remote Mode - OpenAI API Compatible Endpoint",
        "blurb": ("Vaudville Configurator intercepts the communcation with the "
                  "outdated and hardcoded Llamalib built into the game, and lets "
                  "you enter any Local or Remote OpenAI Compatible API Endpoint.  "
                  "vLLM / SGLang / Llama.cpp / ExLlamaV3 / Ollama / OpenAI / Custom"),
        "difficulty": "Moderate to Difficult depending on Self Hosting or Remote API.",
        "restrictions": "Only that the endpoint must support OpenAI API Chat Completions!",
    },
}

# --------------------------------------------------------------------------- #
# Jargon -> what the UI should say instead.  Order is not significant.
# --------------------------------------------------------------------------- #
TERMS: list[tuple[str, str]] = [
    ("LLMAgent", "character agents"),
    ("LLM component", "local engine"),
    ("26 character agents", "the game's cast of characters"),
    ("serialized field", "a setting stored inside the game's files"),
    ("blob", "the block of game data that holds those settings"),
    ("contextSize", "conversation memory"),
    ("numGPULayers", "graphics-card boost"),
    ("GGUF", "model file (the AI's brain, in one download)"),
    ("shim", "the built-in translator"),
    ("endpoint", "the web address of an AI service"),
    ("sampling", "word-choice settings"),
    ("token", "a word-fragment the AI counts"),
    ("BaseURL", "the address the translator forwards to"),
    ("systemPrompt", "the character's backstage note"),
    ("KV cache", "the AI's warm short-term memory"),
    ("temperature", "how wild the AI is allowed to be"),
    ("LlamaLib / llama.cpp", "the engine that actually runs the model"),
    ("Mirostat", "the AI's randomness autopilot"),
    ("streaming", "the reply arriving word by word"),
    ("in-place patch", "editing the game file where it sits, no reinstall"),
    ("TLS", "the padlock on an https address"),
    ("VRAM", "your graphics card's memory"),
    ("PPtr", "a link from one setting to another"),
    ("GBNF grammar", "a strict format every reply must follow"),
    ("quantised (Q4_K_M)", "a compressed model file - smaller, slightly sillier"),
]

GROUP_HELP: str = (
    "Every speaking part in Vaudeville has its own saved settings, and this tool "
    "edits them in groups: the people you meet in each place in town, plus a "
    "separate group for the same roles when they appear in your own Workshop and "
    "Story Editor mysteries.  The dropdown chooses who your changes apply to - one "
    "group at a time, or the everybody-at-once entry for the whole cast.  Groups "
    "you leave alone keep exactly the settings they have now, so you can give the "
    "shopkeeper a calmer style while everybody else carries on improvising.  The "
    "names and character counts in the list come straight from the game."
)

# --------------------------------------------------------------------------- #
# Per-setting help.  One entry for every name in AGENT_LAYOUT + LLM_LAYOUT.
# Order: shared names, then character-only, then engine-only.
# --------------------------------------------------------------------------- #
SETTING_HELP: dict[str, dict] = {

    # ---- shared by the character agents and the local engine ------------- #
    "advancedOptions": {
        "what": "A leftover switch from the game's editor that only ever showed or hid the advanced settings below it.",
        "does": "Nothing you can see or feel while playing. The values underneath are used either way.",
        "why_change": "You do not need to. It is scenery, not a control.",
        "why_not": "Flipping it cannot rescue a setting that seems ignored - look at the setting itself instead.",
        "range": "on or off (1 / 0). Characters ship off, the engine ships on.",
        "tips": [
            ("either", "the game plays exactly the same"),
            ("if a value seems ignored", "check the mode, not this switch"),
            ("curious?", "it changes nothing, but look if you like"),
        ],
    },
    "remote": {
        "what": "Two switches sharing one name: on a character it means 'phone a server for my lines'; on the engine it means 'let other programs use my model'.",
        "does": "Character side: dialogue comes over the network instead of from the built-in engine. Engine side: the game opens a small AI server of its own.",
        "why_change": "Only through the mode cards (character side) or the 'expose game as server' tick (engine side), so address and door number stay consistent.",
        "why_not": "Flipping it by hand without matching host and port leaves the cast phoning a server that is not there.",
        "range": "on or off (1 / 0); ships off on both sides",
        "tips": [
            ("character side", "let the mode cards set this for you"),
            ("engine side on", "the game becomes an AI server for other apps"),
            ("engine side off", "normal play, nothing listening"),
        ],
    },
    "port": {
        "what": "A door number: which door the characters knock on for their lines, or (engine side) which door the game opens for other programs.",
        "does": "Numbers patch freely, but the matching host name is stuck at 9-12 characters in the game file - which is why the translator keeps the real address and key for you.",
        "why_change": "Your server is not on the usual 13333, or that door is already taken by something else.",
        "why_not": "The shipped number already matches the translator, and the two sides must never share one door.",
        "range": "0 to 65535 (use 1024 or above); ships at 13333",
        "tips": [
            ("13333", "the shipped door, and where the translator waits"),
            ("anything free", "fine, as long as nothing else uses it"),
            ("engine side", "pick a different door than the translator's"),
        ],
    },
    "APIKey": {
        "what": "A password the game could hand to a server to prove it is allowed in.",
        "does": "Sent with each request to prove the game is allowed in. Both sides ship empty, and an empty slot cannot be made longer, so the real key lives in the translator instead.",
        "why_change": "You do not, in practice: this box has room for zero characters.",
        "why_not": "A key typed here will not fit. Put it in the translator's API key field, which has no length limit and never shows the key on a command line.",
        "range": "text, but only as long as the space already reserved - shipped empty, so empty is the only value that fits",
        "tips": [
            ("empty", "the only thing that fits, and the safest choice"),
            ("your real key", "belongs in the translator's API key box"),
            ("why the fuss", "the game file has no spare room for a long secret"),
        ],
    },

    # ---- character agents only ------------------------------------------- #
    "llm": {
        "what": "The wiring from a character to the engine - which backstage brain this performer talks to.",
        "does": "It is a link, not a value. Vaudeville ships it empty (0, 0): the cast reaches the engine through LlamaLib's own plumbing, not through this reference.",
        "why_change": "Never. There is no safe way to repoint a link by patching bytes, and the game already works with it empty.",
        "why_not": "A wrong link means silent characters and a very confusing evening.",
        "range": "a link (shown as two numbers); leave exactly as shipped",
        "tips": [
            ("as shipped", "empty, and the game is happy"),
            ("a real link", "not something byte-patching can build"),
            ("rule of thumb", "admire it, do not touch it"),
        ],
    },
    "host": {
        "what": "The name of the computer the characters phone for their lines - 'localhost' means this one.",
        "does": "Decides where dialogue requests go. The file only has room for a word the same padded size as the shipped one, so your replacement must be 9-12 characters.",
        "why_change": "Advanced mode, with a server on your network whose short name fits (an 11-character address such as 192.168.1.5 does).",
        "why_not": "Remote mode needs 'localhost', because the translator lives there and holds the real address, path and key.",
        "range": "text, 9 to 12 characters, no http:// and no slashes; ships as 'localhost'",
        "tips": [
            ("localhost", "the shipped value; the translator lives here"),
            ("192.168.1.5", "11 characters - fits the little box nicely"),
            ("anything longer", "use Remote mode; the translator has no limit"),
            ("no paths", "host and port only, or nothing connects"),
        ],
    },
    "numRetries": {
        "what": "How many times a character tries again when the AI server does not answer.",
        "does": "Each attempt waits a little longer than the last (1, 2, 4, 8, 16, then 30 seconds), so a high number means patient but slow failure.",
        "why_change": "Your server is on flaky Wi-Fi, or takes a while to wake up.",
        "why_not": "More retries never make replies faster; they only delay the moment you learn the server is down.",
        "range": "0 to 1000; ships at 5",
        "tips": [
            ("5", "plenty when the server is on this machine"),
            ("10-20", "for a sleepy network box that needs a nudge"),
            ("0", "fail instantly - handy while testing"),
            ("100+", "bring a book"),
        ],
    },
    "grammar": {
        "what": "A rulebook every reply must obey, such as 'answer only in this exact JSON shape'.",
        "does": "Turns free speech into strict form-filling. Characters following a grammar stop sounding like people.",
        "why_change": "You are experimenting with structured output rather than playing.",
        "why_not": "The slot ships empty and cannot grow, so no rulebook fits here - run that on your own server instead.",
        "range": "text, only as long as the space already reserved - shipped empty, so empty is all that fits",
        "tips": [
            ("empty", "normal, chatty Vaudeville"),
            ("a rulebook here", "will not fit, and would mute the cast"),
            ("want one anyway", "your own server can enforce it, no length limit"),
        ],
    },
    "numPredict": {
        "what": "The longest a single line of dialogue may be, counted in word-fragments.",
        "does": "Cuts the character off when the limit is reached. -1 means no limit: finish your sentence.",
        "why_change": "You want shorter, snappier lines, or you pay per word on a hosted service.",
        "why_not": "Too small and characters get interrupted mid-thought, which reads as a bug rather than a choice.",
        "range": "-1 (unlimited) or 0 upward, up to about a million; ships at -1",
        "tips": [
            ("-1", "let them finish their lines"),
            ("128-256", "chatty but never rambling"),
            ("32", "terse bartenders and brusque guards"),
            ("0", "mute theatre - probably not the plan"),
        ],
    },
    "cachePrompt": {
        "what": "Lets the AI keep the setup it has already read, instead of re-reading it before every line.",
        "does": "Keeps a warm copy of the scene in memory so the next reply starts much faster. Off means a cold read every single time.",
        "why_change": "Almost never. There is no upside, only a longer pause before each line.",
        "why_not": "Turning it off is the classic way to make the whole game feel slow.",
        "range": "on or off (1 / 0); ships on",
        "tips": [
            ("on", "keep on: replies start much faster"),
            ("off", "every line re-reads the whole script"),
            ("while troubleshooting", "leave it on; it is not the usual suspect"),
        ],
    },
    "seed": {
        "what": "The starting number for the AI's dice. Same dice, same performance.",
        "does": "0 means roll fresh dice every time. Any other number freezes the luck, so the same conversation gives the same lines.",
        "why_change": "You are testing, or trying to reproduce one brilliant exchange word for word.",
        "why_not": "Frozen dice make the game feel scripted, and improvisation is the whole point of Vaudeville.",
        "range": "0 (random) or any whole number, roughly -2.1 billion to 2.1 billion; ships at 0",
        "tips": [
            ("0", "surprise every time"),
            ("42", "the same performance again, forever"),
            ("any fixed number", "great for bug reports, dull for playing"),
            ("back to 0", "when the experiment is over"),
        ],
    },
    "temperature": {
        "what": "How wild the AI is allowed to be when it chooses its next word.",
        "does": "Low values give safe, predictable lines; high values make characters surprising, poetic, or outright nonsense.",
        "why_change": "Dialogue feels canned (raise it) or characters are talking gibberish (lower it).",
        "why_not": "The shipped 0.2 is deliberately calm, and big swings change the feel of every conversation.",
        "range": "0.0 to 4.0 here (the engine's own slider stops at 2.0); ships at 0.2",
        "tips": [
            ("0.1", "for a robotic, always-the-same character"),
            ("0.2", "the shipped, well-behaved Vaudeville"),
            ("0.7-0.9", "the sweet spot for lively improv"),
            ("above 2.0", "prepare for insanity!"),
        ],
    },
    "topK": {
        "what": "How many candidate words stay on the shortlist before the AI picks one.",
        "does": "A short shortlist means safer, duller lines; a long one lets odd words in. 0 or -1 means no shortlist at all.",
        "why_change": "You want tighter or looser vocabulary alongside temperature.",
        "why_not": "Above about 100 it rarely changes anything you would notice.",
        "range": "-1 to 100000 (the engine's own slider stops at 100); ships at 40",
        "tips": [
            ("40", "the shipped, sensible shortlist"),
            ("10-20", "very focused, slightly stiff"),
            ("100+", "more colour, more risk"),
            ("-1 or 0", "everything stays on the list"),
        ],
    },
    "topP": {
        "what": "Keeps only the words that together cover a given share of the likely choices.",
        "does": "0.9 means 'consider words until you have covered 90 percent of the likelihood and ignore the rest'. Lower is tamer, higher is looser.",
        "why_change": "You want variety without the chaos of a big temperature.",
        "why_not": "Above 1.0 it means nothing - there is no more than 100 percent of likelihood to cover.",
        "range": "0.0 to 1.5, but only 0.0-1.0 is meaningful; ships at 0.9",
        "tips": [
            ("0.9", "the shipped sweet spot"),
            ("0.5", "predictable and polite"),
            ("1.0", "no filtering at all"),
            ("above 1.0", "meaningless - the AI shrugs"),
        ],
    },
    "minP": {
        "what": "A floor: ignore any word that is far less likely than the current favourite.",
        "does": "Trims the silly long tail without tightening the top of the list. 0 switches the floor off.",
        "why_change": "You are seeing odd word choices and want them filtered the modern, gentle way.",
        "why_not": "The shipped 0.05 is already what most people recommend.",
        "range": "0.0 to 1.5, but only 0.0-1.0 is meaningful; ships at 0.05",
        "tips": [
            ("0.05", "the shipped, well-loved default"),
            ("0.1", "a little tidier"),
            ("0.0", "no floor - every word is a candidate"),
            ("0.5", "very strict; expect repetition"),
        ],
    },
    "repeatPenalty": {
        "what": "How hard the AI is discouraged from reusing words it has just said.",
        "does": "Above 1.0 repeats become less likely each time. 1.0 means no discouragement at all.",
        "why_change": "Characters loop the same phrase like a stuck record.",
        "why_not": "Push it too high and ordinary words get banned, so sentences turn strange and clipped.",
        "range": "0.0 to 5.0 here (the engine's own slider stops at 2.0); 1.0 = off; ships at 1.1",
        "tips": [
            ("1.0", "off, characters may loop like a broken record"),
            ("1.1", "the shipped gentle nudge"),
            ("1.2-1.3", "firm anti-parrot setting"),
            ("above 1.5", "prose starts to break"),
        ],
    },
    "presencePenalty": {
        "what": "A nudge towards new topics instead of ones already mentioned.",
        "does": "Penalises any word that has appeared at all, however long ago, so conversation drifts to fresh ground. 0 is off.",
        "why_change": "Your server honours it and the cast keeps circling the same subject.",
        "why_not": "Not every server pays attention to it, and the shipped 0 is neutral.",
        "range": "-4.0 to 4.0 here (the engine's own slider stops at 1.0); ships at 0 (off)",
        "tips": [
            ("0", "off - the shipped, neutral setting"),
            ("0.2-0.5", "gentle push towards new topics"),
            ("1.0+", "restless characters who change the subject"),
            ("negative", "encourages staying on topic"),
        ],
    },
    "frequencyPenalty": {
        "what": "A nudge based on how often a word has already been used.",
        "does": "The more a word appears the less likely it becomes - a running tally against repetition. 0 is off.",
        "why_change": "Your server honours it and one word keeps turning up in every line.",
        "why_not": "It ships off, and the translator can only pass it on if the server listens.",
        "range": "-4.0 to 4.0 here (the engine's own slider stops at 1.0); ships at 0 (off)",
        "tips": [
            ("0", "off - as shipped"),
            ("0.3", "a light touch on wordy repeats"),
            ("1.0+", "aggressive; sentences get odd"),
            ("negative", "repetition becomes a feature"),
        ],
    },
    "typicalP": {
        "what": "Keeps only words that are about as surprising as you would expect - neither too obvious nor too bizarre.",
        "does": "Filters the shortlist by interestingness. 1.0 turns the filter off.",
        "why_change": "You are experimenting with word-choice settings; this one is pure taste.",
        "why_not": "It ships off, and stacking it on the other filters rarely improves a scene.",
        "range": "0.0 to 2.5 here (the engine's own slider stops at 1.0); 1.0 = off, as shipped",
        "tips": [
            ("1.0", "off - leave it here unless curious"),
            ("0.7", "a mildly experimental flavour"),
            ("0.3", "very picky; expect odd dialogue"),
            ("with high temperature", "chaos, but with a theme"),
        ],
    },
    "repeatLastN": {
        "what": "How far back the anti-repetition rule looks.",
        "does": "It is the window repeatPenalty works in: only words inside the last N are discouraged. -1 means the whole memory, 0 switches the penalty off.",
        "why_change": "Loops persist even with a penalty, so the window may be too short.",
        "why_not": "The shipped 64 balances memory against freedom, and 0 would undo repeatPenalty entirely.",
        "range": "-1 (whole memory) to 65536 here (the engine's own slider stops at 2048); ships at 64",
        "tips": [
            ("64", "the shipped, sensible window"),
            ("256", "catches longer, slower loops"),
            ("-1", "police the entire conversation"),
            ("0", "the repeat penalty switches itself off"),
        ],
    },
    "mirostat": {
        "what": "An optional autopilot that keeps the surprise level of each line steady.",
        "does": "Instead of you tuning randomness, the AI adjusts itself as it writes. 0 = off, 1 = the original, 2 = Mirostat 2 (the good one).",
        "why_change": "You specifically want the AI to self-tune its own randomness.",
        "why_not": "It overrides your temperature and topP taste, and 0 (off) is the sane default.",
        "range": "0, 1 or 2; ships at 0 (off)",
        "tips": [
            ("0", "off; the sane default"),
            ("2", "the AI self-tunes its own randomness"),
            ("1", "the older, slower-thinking autopilot"),
            ("with Tau and Eta", "the two dials that steer it"),
        ],
    },
    "mirostatTau": {
        "what": "The surprise level the Mirostat autopilot aims for.",
        "does": "Higher keeps lines more varied, lower keeps them predictable. It only does anything while mirostat is 1 or 2.",
        "why_change": "You turned Mirostat on and want to steer how adventurous it is.",
        "why_not": "With mirostat off (the default) this dial is decoration.",
        "range": "0.0 to 20.0 here (the engine's own slider stops at 10.0); ships at 5.0",
        "tips": [
            ("5.0", "the shipped target, and a good one"),
            ("2.0-3.0", "calmer, more predictable lines"),
            ("8.0+", "surprising, occasionally incoherent"),
            ("mirostat off", "this dial does nothing at all"),
        ],
    },
    "mirostatEta": {
        "what": "How quickly the Mirostat autopilot corrects itself while writing.",
        "does": "A bigger step adapts fast but can wobble; a smaller step is smooth and slow. Only matters while mirostat is on.",
        "why_change": "You are already using Mirostat and want finer control of its reactions.",
        "why_not": "With mirostat off it has no effect, and the shipped 0.1 is what nearly everyone uses.",
        "range": "0.0 to 2.0 here (the engine's own slider stops at 1.0); ships at 0.1",
        "tips": [
            ("0.1", "the shipped, smooth setting"),
            ("0.3", "adapts faster, may wobble"),
            ("0.0", "autopilot frozen in place"),
            ("mirostat off", "harmless, and pointless"),
        ],
    },
    "nProbs": {
        "what": "Asks the AI to also report the runner-up words it considered.",
        "does": "Purely diagnostic: you get a list of alternatives with each reply. It never changes what a character actually says.",
        "why_change": "You are debugging, or studying how the model makes up its mind.",
        "why_not": "It adds traffic and noise for no gameplay benefit at all.",
        "range": "0 (off) to 64 here (the engine's own slider stops at 10); ships at 0",
        "tips": [
            ("0", "off - normal play"),
            ("5", "peek at the runners-up while debugging"),
            ("10+", "a firehose of alternatives"),
            ("for playing", "keep it at 0"),
        ],
    },
    "ignoreEos": {
        "what": "Tells the AI to keep talking after it has signalled 'end of message'.",
        "does": "Replies run on past their natural ending until something else (numPredict) stops them.",
        "why_change": "Debugging a model that ends its sentences too early.",
        "why_not": "In normal play you get run-on babble after the punchline.",
        "range": "on or off (1 / 0); ships off",
        "tips": [
            ("off", "characters stop when they are done"),
            ("on", "they keep going until cut off"),
            ("with numPredict -1", "an endless monologue"),
            ("for debugging only", "yes, exactly that"),
        ],
    },
    "save": {
        "what": "A filename the game would use to keep this character's conversation history on disk.",
        "does": "Non-empty means the chat is written to (and reloaded from) a file in the game's save folder. Empty, as shipped, means nothing is written.",
        "why_change": "You do not, from here: the slot ships empty and cannot grow, so no filename fits.",
        "why_not": "Anything you type either will not fit or points at a save file the game never made.",
        "range": "text, only as long as the space already reserved - shipped empty, so empty is all that fits",
        "tips": [
            ("empty", "as shipped, and the only thing that fits"),
            ("a filename", "no room in the slot, sadly"),
            ("want a record?", "the translator's log is the practical option"),
        ],
    },
    "debugPrompt": {
        "what": "Asks the game to show the exact backstage instructions it sent to the AI.",
        "does": "Writes each full prompt - character notes plus your line - to the game's log instead of keeping it secret.",
        "why_change": "You are working out why a character said something bizarre.",
        "why_not": "It floods the log with long text and tells you nothing during normal play.",
        "range": "on or off (1 / 0); ships off",
        "tips": [
            ("off", "quiet logs, normal play"),
            ("on", "see exactly what the AI was told"),
            ("with a reasoning model", "very noisy, very informative"),
            ("when done", "turn it back off"),
        ],
    },
    "slot": {
        "what": "Which seat at the server's table this character uses.",
        "does": "A server can hold several conversations at once, one per seat, and the seat decides what gets cached. -1 means 'pick a free seat for me'.",
        "why_change": "Never, really - automatic is right for the game, and remote characters get a seat assigned for them anyway.",
        "why_not": "A fixed seat can collide with another character and mix up who remembers what. The game also rewrites this when it loads.",
        "range": "-1 (automatic) up to 4096; ships at -1",
        "tips": [
            ("-1", "automatic; leave it"),
            ("0", "one seat for everybody - conversations tangle"),
            ("fixed numbers", "only meaningful on a server you run"),
            ("the game rewrites it", "at load time, so edits rarely stick"),
        ],
    },
    "systemPrompt": {
        "what": "The character's backstage note: who they are and how they should behave.",
        "does": "Sent ahead of every line, so it shapes the whole performance. The game overwrites it at load time from its own character text files or from a Workshop story.",
        "why_change": "You do not, from here - your text is replaced on the next load, and it must be 153-156 characters to fit the slot.",
        "why_not": "Edits get overwritten, and anything longer or shorter will not fit the reserved space. Two of the cast ship an empty note, where only empty fits.",
        "range": "text of 153 to 156 characters (the size of the shipped note); rewritten by the game when it loads",
        "tips": [
            ("as shipped", "each character arrives with their own note"),
            ("your edit", "gets overwritten the next time the game loads"),
            ("want new personalities?", "edit the character text files, or write a Workshop story"),
            ("153-156 characters", "the only lengths that fit in place"),
        ],
    },

    # ---- local engine only ----------------------------------------------- #
    "SSLCert": {
        "what": "The file path of the identity card (certificate) for the game's own AI server.",
        "does": "Used only when the engine's server is switched on and you want it to speak encrypted https. It ships empty, so nothing is encrypted.",
        "why_change": "You are serving the game's model over https to other machines with a certificate of your own.",
        "why_not": "The slot is empty and cannot grow, so no path fits. At home, plain http plus a translator is simpler.",
        "range": "text, only as long as the space already reserved - shipped empty, so empty is all that fits",
        "tips": [
            ("empty", "as shipped - the server talks plain http"),
            ("a real path", "will not fit the empty slot"),
            ("want encryption?", "put a translator or proxy in front instead"),
        ],
    },
    "SSLKey": {
        "what": "The file path of the private key that matches the server's identity card.",
        "does": "The other half of https for the game's own server. Also empty as shipped, and a secret you would not want inside a game file anyway.",
        "why_change": "Only together with SSLCert, on a server you are deliberately exposing over https.",
        "why_not": "The slot cannot grow, so no path fits - and private keys belong in your own server's config.",
        "range": "text, only as long as the space already reserved - shipped empty, so empty is all that fits",
        "tips": [
            ("empty", "as shipped, and where it should stay"),
            ("a private key here", "no room, and no secrecy"),
            ("https at home", "let the translator or a proxy hold the key"),
        ],
    },
    "numThreads": {
        "what": "How many of your processor's workers the AI may use.",
        "does": "More workers means faster thinking on the processor, but fewer are left for the game itself - physics, animation, sound.",
        "why_change": "You want to leave processor power for the game, or a background app is starving it.",
        "why_not": "-1 already means 'use everything', which is right on most machines.",
        "range": "-1 (all cores) or 1 upward, up to 4096; ships at -1",
        "tips": [
            ("-1", "all cores - as shipped"),
            ("half your cores", "leaves the game room to breathe"),
            ("1-2", "slow dialogue, smooth everything else"),
            ("more than you have", "the processor just queues the work"),
        ],
    },
    "numGPULayers": {
        "what": "How much of the model is handed to your graphics card to run.",
        "does": "More layers on the card means much faster replies - if the card has room. Too many and you run out of video memory and the game stutters or falls over.",
        "why_change": "You have a graphics card with spare memory and want snappier dialogue.",
        "why_not": "The game overwrites this number at boot from its own Options slider (the GpuLoad setting), so an edit here can simply be undone.",
        "range": "-1 or 0 upward (0 = processor only); the in-game Options slider wins at boot",
        "tips": [
            ("0", "processor only - slow, but always fits"),
            ("more = faster", "if you have a graphics card with room"),
            ("all of them", "usually best when the model fits"),
            ("in-game slider", "this is what actually decides at boot"),
        ],
    },
    "parallelPrompts": {
        "what": "How many conversations the engine may work on at the same time.",
        "does": "Each simultaneous conversation needs its own slice of memory. -1 lets the engine decide from how many characters are talking.",
        "why_change": "A crowded scene where several characters speak at once and one is left waiting.",
        "why_not": "Raising it multiplies memory use, and the automatic setting already matches the game.",
        "range": "-1 (automatic) or 1 upward, up to 4096; ships at -1",
        "tips": [
            ("-1", "automatic - matches the number of talkers"),
            ("1", "one at a time; queues in crowds"),
            ("2-4", "more lines at once, more memory"),
            ("very high", "memory goes pop"),
        ],
    },
    "contextSize": {
        "what": "How much of the conversation the AI can hold in mind at once - its short-term memory.",
        "does": "Bigger means characters remember more of the scene, but the first reply is slower and more memory is used. 0 means 'use whatever the model prefers'.",
        "why_change": "Long scenes where characters forget what was just said, and you have memory to spare.",
        "why_not": "In the networked modes the server decides, so this number is ignored - and doubling it doubles the memory bill.",
        "range": "0 (model default) or any whole number; ships at 8192, roughly a few thousand words",
        "tips": [
            ("8192", "the shipped, comfortable default"),
            ("16384", "bigger = longer memory, slower first reply"),
            ("4096", "snappier, but goldfish characters"),
            ("0", "let the model file decide"),
        ],
    },
    "batchSize": {
        "what": "How much of the setup text the engine swallows in one gulp while it reads the scene in.",
        "does": "A bigger gulp reads long setups faster but asks for more memory. It affects reading in, not the reply itself.",
        "why_change": "Very long character notes or story setups feel slow to start.",
        "why_not": "The shipped 512 is a good middle ground, and raising it mostly just costs memory.",
        "range": "whole numbers, 0 upward; ships at 512",
        "tips": [
            ("512", "the shipped middle ground"),
            ("1024-2048", "faster read-in for very long scenes"),
            ("128", "gentle on memory, slow to warm up"),
            ("huge values", "memory hungry for very little gain"),
        ],
    },
    "model": {
        "what": "The filename of the model file (GGUF) the engine loads - the AI's brain, in one download.",
        "does": "Decides which brain runs the dialogue. The game looks for two fixed filenames, so this tool links your chosen file to one of those names and keeps the original safe.",
        "why_change": "Use the Models tab, never this field: the filename box only fits 33-36 characters.",
        "why_not": "Hand-editing needs an exact length match and a file that exists, or the game loads nothing at all.",
        "range": "text of 33 to 36 characters (the space the shipped filename occupies)",
        "tips": [
            ("Models tab", "the friendly way - pick a file, press Apply"),
            ("a short name", "will not fit the 33-36 character slot"),
            ("Basic mode", "must stay in the Llama-3-8B-Instruct family"),
            ("the original", "is kept safe as gguf/ORIGINAL_..."),
        ],
    },
    "flashAttention": {
        "what": "A faster, leaner way of doing the attention maths inside the engine.",
        "does": "Same dialogue, often more speed and less memory - when the engine build and the model file both support it.",
        "why_change": "You want a big conversation memory on modest hardware and your build supports it.",
        "why_not": "It ships off, and on unsupported builds it can misbehave or shift the output slightly.",
        "range": "on or off (1 / 0); ships off",
        "tips": [
            ("off", "as shipped - the safe choice"),
            ("on", "often faster and leaner with a big memory"),
            ("if dialogue goes weird", "turn it straight back off"),
            ("pairs nicely with", "a larger contextSize"),
        ],
    },
    "reasoning": {
        "what": "Lets a 'thinking' model mutter its working-out before it answers.",
        "does": "On = the model thinks out loud before answering; Off = it just answers. This is the game's own engine switch, so it belongs to the local modes - in Remote mode the translator's 'strip think' does the hiding.",
        "why_change": "Your model file is a reasoning model (Qwen3, DeepSeek-R1 style) and you want the better answers it gives.",
        "why_not": "On an ordinary model it does nothing, and without stripping the voice actors will read the working-out aloud.",
        "range": "on or off (1 / 0); ships off",
        "tips": [
            ("on", "the model thinks out loud before it answers"),
            ("off", "plain models, plain answers"),
            ("in Remote mode", "the translator's 'strip think' hides the working-out"),
            ("non-reasoning model", "leave it off"),
        ],
    },
    "lora": {
        "what": "Optional add-on files that fine-tune a model - a small patch laid over the big brain.",
        "does": "If set, the engine loads those patches (comma-separated) with the model. It ships empty, and an empty slot cannot grow.",
        "why_change": "You do not, from here: there is no room for a filename.",
        "why_not": "Anything you type either will not fit or points at a file the game cannot find.",
        "range": "text, only as long as the space already reserved - shipped empty, so empty is all that fits",
        "tips": [
            ("empty", "as shipped"),
            ("a path", "no room in the slot"),
            ("want a fine-tune?", "merge it into one GGUF and link that instead"),
        ],
    },
    "loraWeights": {
        "what": "How strongly each of those fine-tune patches should be applied.",
        "does": "A comma-separated list of numbers, one per patch, defaulting to 1.0 each. Meaningless while lora is empty, which it is.",
        "why_change": "Never from here - it only matters once patches actually load, and they cannot.",
        "why_not": "The slot ships empty and cannot grow.",
        "range": "text, only as long as the space already reserved - shipped empty, so empty is all that fits",
        "tips": [
            ("empty", "as shipped, and ignored anyway"),
            ("1.0 each", "what the engine would use by default"),
            ("with no patches loaded", "a dial with nothing attached"),
        ],
    },
    "dontDestroyOnLoad": {
        "what": "Whether the engine stays alive when the game moves between scenes.",
        "does": "On, the AI survives scene changes and keeps its warm memory; off, the game tears it down and rebuilds it. Vaudeville ships it off and manages its own lifetime.",
        "why_change": "Only if you are experimenting with scene changes and slow re-loads - not for normal play.",
        "why_not": "Flipping engine lifetime flags can leak memory or upset scene loading, and the shipped value already works.",
        "range": "on or off (1 / 0); ships off",
        "tips": [
            ("off", "as shipped - the game knows what it is doing"),
            ("on", "the AI survives scene changes, and so does its memory bill"),
            ("if scenes go strange", "put it back to off"),
        ],
    },
    "embeddingsOnly": {
        "what": "A note about the model file: 'this one only turns text into numbers, it cannot talk'.",
        "does": "It describes the model rather than changing it. Vaudeville's models talk, so it stays off.",
        "why_change": "Never - unless you deliberately load a number-crunching model, in which case the cast has nothing to say anyway.",
        "why_not": "Switching it on for a talking model only confuses the engine.",
        "range": "on or off (1 / 0); ships off",
        "tips": [
            ("off", "characters speak, as intended"),
            ("on", "for number-crunching models only"),
            ("why is it here?", "it is part of the engine's saved settings"),
        ],
    },
    "embeddingLength": {
        "what": "How many numbers are in each of those numeric summaries, for a model that makes them.",
        "does": "Nothing for Vaudeville: it stays 0 because the model is a talker, not a number-cruncher.",
        "why_change": "Never.",
        "why_not": "It only describes a number-crunching model, and this is not one.",
        "range": "0 upward (to about 262000); ships at 0",
        "tips": [
            ("0", "as shipped - not a number-crunching model"),
            ("any other number", "only meaningful for number-crunching models"),
            ("for playing", "not a dial you need"),
        ],
    },
    "minContextLength": {
        "what": "A read-out: the smallest conversation memory the loaded model supports.",
        "does": "Nothing - it is the floor the engine reports for the loaded model, not a control.",
        "why_change": "Never; it is informational, and the engine does not even write it back here.",
        "why_not": "Editing a read-out does not change the model, so there is nothing to gain.",
        "range": "whole numbers, reported by the model; not a control (ships 0)",
        "tips": [
            ("informational", "the model's own floor, reported to you"),
            ("your real dial", "contextSize"),
            ("edits here", "change nothing - it is only a read-out"),
        ],
    },
    "maxContextLength": {
        "what": "A read-out: the largest conversation memory the loaded model supports.",
        "does": "Nothing - the engine fills it in from the model file at load time. It is the ceiling your contextSize lives under.",
        "why_change": "Never; it is informational and gets refreshed when a model loads.",
        "why_not": "Editing a read-out does not change the model, and your edit is replaced.",
        "range": "whole numbers, reported by the model; not a control (the shipped Llama-3 file reports 131072)",
        "tips": [
            ("informational", "the ceiling the model was built with"),
            ("your real dial", "contextSize"),
            ("a bigger ceiling", "comes from a different model file, not from here"),
        ],
    },
}

# --------------------------------------------------------------------------- #
# Model slots, and what each mode lets you load.  Canonical keys first; the
# alias table below points the other spellings at the same strings so callers
# can look up by slot label, slot filename, mode key or CLI mode spelling.
# --------------------------------------------------------------------------- #
_MODEL_HELP_CORE: dict[str, str] = {
    "Main dialogue model": (
        "The brain behind every spoken line. The game looks for one fixed filename, so this tool "
        "links your chosen file to that name and keeps the original safe as gguf/ORIGINAL_... "
        "Pick a chat/instruct model, not a base model."),
    "Steam Deck / fallback model": (
        "The small, light model the game switches to on a Steam Deck, and its fallback in a pinch. "
        "Same link swap, smaller brain: choose something tiny and quick (around half a billion "
        "parameters) or the handheld will crawl."),
    "basic": (
        "Basic mode keeps the game's own built-in engine and the model file in the game's own "
        "folder. Stay in it for the Llama-3-8B-Instruct family: your file must be that model or "
        "something made from it - not 3.1 or later. Anything else wants Advanced or Remote mode."),
    "direct": (
        "Advanced mode keeps the game's own engine but points the cast at a llama.cpp server you "
        "run yourself, so almost any recent model file works - loaded by that server, not by the "
        "game. Compressed files (Q4_K_M) are how most people make a big model fit."),
    "shim": (
        "Remote mode does not use a local model file at all: the AI lives on the server you point "
        "the translator at. The game's own model file stays untouched, and the model name you type "
        "is the one that server knows."),
}

_MODEL_HELP_ALIASES: dict[str, str] = {
    "main": "Main dialogue model",
    "main dialogue model": "Main dialogue model",
    "primary": "Main dialogue model",
    "Meta-Llama-3-8B-Instruct-Q4_K_M.gguf": "Main dialogue model",
    "deck": "Steam Deck / fallback model",
    "steam deck / fallback model": "Steam Deck / fallback model",
    "fallback": "Steam Deck / fallback model",
    "Qwen3-0.6B-Q4_K_M.gguf": "Steam Deck / fallback model",
    "off": "basic",
    "basic mode": "basic",
    "basic restriction": "basic",
    "local basic": "basic",
    "advanced": "direct",
    "advanced mode": "direct",
    "advanced restriction": "direct",
    "local advanced": "direct",
    "remote": "shim",
    "remote mode": "shim",
    "openai": "shim",
    "remote restriction": "shim",
}

MODEL_HELP: dict[str, str] = dict(_MODEL_HELP_CORE)
for _alias, _canonical in _MODEL_HELP_ALIASES.items():
    MODEL_HELP[_alias] = _MODEL_HELP_CORE[_canonical]
del _alias, _canonical

# --------------------------------------------------------------------------- #
# The translator ("shim") controls.  Canonical keys use the on-screen wording.
# --------------------------------------------------------------------------- #
_SHIM_HELP_CORE: dict[str, str] = {
    "BaseURL": (
        "The address the translator forwards every line to, for example http://127.0.0.1:11434/v1 "
        "(Ollama) or https://api.openai.com/v1. Add the /v1 if your service expects it. No length "
        "limit here - that is the whole point."),
    "Model name": (
        "The name your AI service knows the model by: an Ollama tag, a vLLM served name, "
        "gpt-4o-mini. The game never sends a model name itself, so this box is the only place "
        "to say it."),
    "API key": (
        "The password for that service. The translator holds it and passes it on through the "
        "environment, never on a command line, so it stays out of process lists and logs. Leave "
        "it empty for a local server that does not ask."),
    "save key": (
        "Tick this to let the tool remember the key between sessions. It is written to your config "
        "file, readable only by you. On a shared machine leave it unticked and paste the key, or "
        "load it from Bitwarden."),
    "listen/port": (
        "Where the translator waits for the game: an address and a door number. 127.0.0.1:13333 "
        "means this computer only. The game's host box fits 9-12 characters, so keep it "
        "'localhost' - the translator holds the real address."),
    "chat vs raw": (
        "How the translator talks to your service. 'chat' uses the normal chat web address and "
        "keeps who-is-speaking intact - right for nearly everything. 'raw' sends one flat block of "
        "text, for services with no chat address of their own."),
    "strip think": (
        "Removes the model's think-blocks before they reach the dialogue box and the voice actors. "
        "Keep it on for reasoning models (Qwen3, DeepSeek-R1 style) or the cast will read their "
        "working-out aloud. Copes with split tags."),
    "skip TLS verify": (
        "Lets the translator accept an https address whose certificate it cannot check. Use it "
        "only for a self-signed certificate on your own network; anywhere else, fix the "
        "certificate instead."),
    "start/stop/test shim": (
        "Start launches the translator in the background (its log sits in the tool's own folder), "
        "Stop closes it, and Test completion makes one real request through to your service. "
        "Start it before every Remote-mode session."),
    "expose-game-as-server": (
        "The reverse direction: the game itself becomes an AI server, offering its loaded model to "
        "other programs. It costs memory and a door number, which must differ from the "
        "translator's. Off unless you have a reason."),
}

_SHIM_HELP_ALIASES: dict[str, str] = {
    "base_url": "BaseURL",
    "baseurl": "BaseURL",
    "backend_url": "BaseURL",
    "model_name": "Model name",
    "model": "Model name",
    "backend_model": "Model name",
    "api_key": "API key",
    "apikey": "API key",
    "APIKey": "API key",
    "save_key": "save key",
    "save_api_key": "save key",
    "listen": "listen/port",
    "port": "listen/port",
    "listen_port": "listen/port",
    "shim_listen": "listen/port",
    "shim_port": "listen/port",
    "mode": "chat vs raw",
    "chat_raw": "chat vs raw",
    "shim_mode": "chat vs raw",
    "strip_think": "strip think",
    "skip_tls": "skip TLS verify",
    "insecure": "skip TLS verify",
    "tls": "skip TLS verify",
    "start_stop_test": "start/stop/test shim",
    "shim_buttons": "start/stop/test shim",
    "test_completion": "start/stop/test shim",
    "expose_server": "expose-game-as-server",
    "expose_game_as_server": "expose-game-as-server",
    "reverse server": "expose-game-as-server",
}

SHIM_HELP: dict[str, str] = dict(_SHIM_HELP_CORE)
for _alias, _canonical in _SHIM_HELP_ALIASES.items():
    SHIM_HELP[_alias] = _SHIM_HELP_CORE[_canonical]
del _alias, _canonical
