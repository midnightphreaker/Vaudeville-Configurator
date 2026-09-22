#!/usr/bin/env python3
"""Vaudeville Configurator — model / endpoint / sampling-parameter control for
Bumblebee Studios' "Vaudeville" (Steam AppID 2240920).

Single file, standard library only (tkinter for the GUI). Run it from anywhere:

    vaudeville-configurator                  # GUI (symlink in ~/.local/bin)
    vaudeville-configurator --cli list       # headless
    vaudeville-configurator --selftest       # built-in verification

Modes
-----
Local Mode - Basic                             internal key: off      (CLI: --mode basic)
    The unmodified game. The bundled LlamaLib runs your GGUF in-process; only the
    model link, the sampling parameters and the GPU-layer count are changed.

Local Mode - Advanced                          internal key: direct   (CLI: --mode advanced)
    The game's own LlamaLib connects as a *remote client* to a llama.cpp-protocol
    server you run yourself (llama-server, another LlamaLib, ...). No shim, so the
    endpoint must speak /health /apply-template /completion /tokenize and must never
    emit "data: [DONE]". The host string has to fit the 9-12 character in-place slot.

Remote Mode - OpenAI API Compatible Endpoint    internal key: shim     (CLI: --mode remote)
    The game talks to the built-in shim on localhost:13333 and the shim translates to
    any OpenAI-compatible BaseURL (vLLM, Ollama, LM Studio, llama.cpp, OpenAI,
    OpenRouter, TGI, koboldcpp, ...). Arbitrary BaseURL / model name / API key / TLS.

Platform
--------
Linux is the primary target. Windows is supported end to end: Steam detection via
the registry, process detection via tasklist, %APPDATA%/%LOCALAPPDATA% state dirs,
detached shim processes, folder opening, and model relinking (symlink, with a
same-volume hardlink fallback where symlink rights are missing). macOS has no hard
blockers but is untested.

What it does
------------
Vaudeville's dialogue AI is undreamai/LLMUnity v3.0.0 + LlamaLib v2.0.0 (a llama.cpp
fork). All sampling parameters and the local/remote switch live as *serialized
MonoBehaviour fields* inside the shipped Unity assets:

    Vaudeville_Data/sharedassets3.assets  -> 1 x LLMUnity.LLM      (model, ctx, GPU, server)
    Vaudeville_Data/level4..13,
    Vaudeville_Data/sharedassets18.assets -> 26 x LLMUnity.LLMAgent (per-character params)

Player builds ship without type trees, so this tool locates those blobs with a
structural signature (validated against every field's legal range) and rewrites
individual fields *in place* at their absolute file offset:

  * bool / int32 / float32 are always 4 bytes  -> freely patchable
  * string may only change when ceil4(len) is unchanged, e.g. `host` ("localhost", 9)
    accepts any 9-12 character host. That is why the recommended remote setup is
    "game -> local shim -> your real endpoint": the shim holds the arbitrary
    BaseURL / model name / API key, and the game keeps host=localhost.

The shim embedded here speaks the llama.cpp-server protocol the game's native
library uses (POST /health, /apply-template, /completion, /tokenize, /detokenize,
/embeddings) and translates to any OpenAI-compatible backend (vLLM, Ollama,
LM Studio, llama.cpp server, OpenAI, TGI, ...).

Safety
------
  * refuses to write while the game is running
  * hash-verified backups outside the game tree before every write
  * preview (dry run) before applying, re-decode verification after
  * restore of any backup from the GUI/CLI
  * the API key is passed to the shim through the environment, never argv, and is
    only persisted if you explicitly tick "save key" (config file is chmod 600)
"""
from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# Optional sibling modules (written alongside this file).  Both are imported
# guarded so the configurator still works — with the fallbacks defined further
# down — when they are missing, e.g. in a frozen single-file build that did not
# bundle them.
try:
    import vaudeville_cast as cast
except Exception:                                          # noqa: BLE001
    cast = None
try:
    import vaudeville_help as helpmod
except Exception:                                          # noqa: BLE001
    helpmod = None

APP_NAME = "Vaudeville Configurator"
APP_SLUG = "vaudeville-configurator"
APP_VERSION = "1.4.0"
STEAM_APPID = "2240920"
GAME_DIR_NAME = "Vaudeville"
DATA_DIR = "Vaudeville_Data"
STREAMING = "StreamingAssets"

# the two GGUF filenames the build hard-codes
PRIMARY_MODEL_NAME = "Meta-Llama-3-8B-Instruct-Q4_K_M.gguf"
DECK_MODEL_NAME = "Qwen3-0.6B-Q4_K_M.gguf"
GGUF_LIBRARY_DIR = "gguf"          # <StreamingAssets>/gguf  (your existing convention)

# --------------------------------------------------------------------------- #
# inference modes.  The three user-facing names are what the GUI/README show;
# the internal keys ("off"/"direct"/"shim") stay stable so existing config.json
# files, scripts and CLI invocations keep working.
# --------------------------------------------------------------------------- #
MODE_OFF, MODE_DIRECT, MODE_SHIM = "off", "direct", "shim"
MODE_ORDER = (MODE_OFF, MODE_DIRECT, MODE_SHIM)
MODE_LABELS = {
    MODE_OFF:    "Local Mode - Basic",
    MODE_DIRECT: "Local Mode - Advanced",
    MODE_SHIM:   "Remote Mode - OpenAI API Compatible Endpoint",
}
MODE_TIPS = {
    MODE_OFF:    "unmodified game: the bundled LlamaLib runs your GGUF in-process (no network)",
    MODE_DIRECT: "game -> a llama.cpp-protocol server you run yourself (llama-server); no shim",
    MODE_SHIM:   "game -> built-in shim on localhost -> any OpenAI-compatible BaseURL",
}
MODE_ALIASES = {
    # Local Mode - Basic
    "off": MODE_OFF, "basic": MODE_OFF, "local": MODE_OFF, "local-basic": MODE_OFF,
    "local-only": MODE_OFF, "local-mode-basic": MODE_OFF,
    # Local Mode - Advanced
    "direct": MODE_DIRECT, "advanced": MODE_DIRECT, "local-advanced": MODE_DIRECT,
    "remote-direct": MODE_DIRECT, "local-mode-advanced": MODE_DIRECT,
    # Remote Mode - OpenAI API Compatible Endpoint
    "shim": MODE_SHIM, "remote": MODE_SHIM, "openai": MODE_SHIM, "remote-shim": MODE_SHIM,
    "remote-openai": MODE_SHIM, "openai-compatible": MODE_SHIM,
    "remote-mode-openai-api-compatible-endpoint": MODE_SHIM,
}


def normalize_mode(value, default: str = MODE_OFF) -> str:
    """Map any accepted spelling (old key, new name, CLI alias) onto a mode key."""
    key = str(value or "").strip().lower().replace(" ", "-").replace("_", "-")
    return MODE_ALIASES.get(key, default)


def mode_label(value) -> str:
    key = normalize_mode(value, default=str(value))
    return MODE_LABELS.get(key, str(value))

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"


# --------------------------------------------------------------------------- #
# friendly metadata: field tables, the two optional sibling modules, and the
# character groups they describe.
#
# vaudeville_cast.py  -> which characters live in which Unity asset file
# vaudeville_help.py  -> plain-English wording, mode cards, terminology map
#
# Both are imported guarded, so this file keeps working (with the built-in
# fallbacks below) when they are absent, e.g. in a single-file frozen build
# that did not bundle them.  Copy that comes from vaudeville_help is rendered
# byte-verbatim; TERMS is applied only to strings this file writes itself.
# --------------------------------------------------------------------------- #
AGENT_FIELDS_GUI = [
    ("remote", "bool", "connect to a remote server instead of the local llama.cpp"),
    ("host", "str", "remote host (in-place patch: length must stay in the same ceil4 bucket)"),
    ("port", "int", "remote port"),
    ("APIKey", "str", "sent as 'Authorization: Bearer …' (empty ⇒ keep the key in the shim)"),
    ("numRetries", "int", "connection retries with 1/2/4/8/16/30 s backoff"),
    ("numPredict", "int", "max tokens to generate, -1 = unlimited  [sent to remote]"),
    ("temperature", "float", "0 = deterministic  [sent to remote]"),
    ("topK", "int", "[sent to remote]"),
    ("topP", "float", "nucleus sampling  [sent to remote]"),
    ("minP", "float", "[sent to remote]"),
    ("repeatPenalty", "float", "1.0 = off  [sent to remote]"),
    ("repeatLastN", "int", "window for the repeat penalty  [sent to remote]"),
    ("presencePenalty", "float", "[sent to remote]"),
    ("frequencyPenalty", "float", "[sent to remote]"),
    ("typicalP", "float", "1.0 = off  [sent to remote]"),
    ("mirostat", "int", "0 off / 1 mirostat / 2 mirostat-2  [sent to remote]"),
    ("mirostatTau", "float", "[sent to remote]"),
    ("mirostatEta", "float", "[sent to remote]"),
    ("seed", "int", "0 = random  [sent to remote]"),
    ("cachePrompt", "bool", "[sent to remote]"),
    ("ignoreEos", "bool", "[sent to remote]"),
    ("nProbs", "int", "return top-N probabilities  [sent to remote]"),
    ("slot", "int", "-1 = auto (remote clients are always forced to -1)"),
    ("grammar", "str", "GBNF/JSON schema (length-constrained in place)"),
]
LLM_FIELDS_GUI = [
    ("model", "str", "GGUF filename inside StreamingAssets (length-constrained in place)"),
    ("contextSize", "int", "prompt context in tokens actually used by llama.cpp"),
    ("maxContextLength", "int", "informational: read back from the model at runtime"),
    ("minContextLength", "int", "informational"),
    ("numThreads", "int", "-1 = all cores"),
    ("numGPULayers", "int", "overridden at boot by the GpuLoad preference / Options slider"),
    ("batchSize", "int", "prompt processing batch"),
    ("parallelPrompts", "int", "-1 = auto from the number of clients"),
    ("flashAttention", "bool", ""),
    ("reasoning", "bool", "enable the model's 'thinking' mode"),
    ("remote", "bool", "TRUE ⇒ the game exposes its model as an HTTP server"),
    ("port", "int", "server port when remote is on"),
    ("APIKey", "str", "server API key (length-constrained in place)"),
    ("dontDestroyOnLoad", "bool", ""),
    ("embeddingsOnly", "bool", ""),
    ("embeddingLength", "int", ""),
]
# character sampling fields, in display order (the Characters tab)
CHARACTER_FIELDS = [
    "temperature", "topK", "topP", "minP", "repeatPenalty", "repeatLastN",
    "presencePenalty", "frequencyPenalty", "typicalP", "mirostat", "mirostatTau",
    "mirostatEta", "seed", "numPredict", "cachePrompt",
]
# llama.cpp engine fields (the Local Mode - Advanced tab)
ENGINE_FIELDS = ["contextSize", "batchSize", "numThreads", "numGPULayers",
                 "flashAttention", "reasoning", "parallelPrompts"]
# the values an unmodified install ships with
STOCK_CHARACTER_VALUES = {
    "temperature": "0.2", "topK": "40", "topP": "0.9", "minP": "0.05",
    "repeatPenalty": "1.1", "repeatLastN": "64", "presencePenalty": "0",
    "frequencyPenalty": "0", "typicalP": "1.0", "mirostat": "0",
    "mirostatTau": "5.0", "mirostatEta": "0.1", "seed": "0",
    "numPredict": "-1", "cachePrompt": "true",
}
# connection wiring: always written for every character, never group-scoped
ENDPOINT_FIELDS = ("remote", "host", "port", "APIKey", "numRetries")
# shipped values of the fixed fields the Characters grid does not expose
STOCK_EXTRA_VALUES = {"nProbs": 0, "ignoreEos": False, "slot": -1}
# the only model family Local Mode - Basic may use
LLAMA3_8B_FAMILY = "Meta-Llama-3-8B-Instruct"
# fields this tool deliberately does NOT offer as editable, and why
INERT_FIELDS = {
    "advancedOptions": "an editor show/hide flag; it does nothing when the game runs",
    "systemPrompt": "the game overwrites it every time a character loads, from its own "
                    "script data, so editing it changes nothing",
}
EMPTY_TEXT_FIELDS = ("APIKey", "grammar", "save", "SSLCert", "SSLKey", "lora", "loraWeights")

GITHUB_URL_FALLBACK = "https://git.phrk.org/pub/Vaudeville-Configurator"
CREDIT_FALLBACK = ("Vaudeville Configurator — a community tool for Bumblebee Studios' "
                   "Vaudeville. Not affiliated with or endorsed by Bumblebee Studios.")
FRIENDLY_GROUP_INTRO_FALLBACK = (
    "Every speaking character in Vaudeville has its own copy of the AI settings below. "
    "Pick a group to change just those characters, or keep “All characters” to change "
    "everyone at once. Empty boxes are left exactly as they are.")
MODE_CARDS_FALLBACK = {
    MODE_OFF: {
        "title": MODE_LABELS[MODE_OFF],
        "blurb": ("The game runs exactly as shipped: your own model file is loaded inside the "
                  "game process and nothing leaves your computer. No server to start, no "
                  "network, no extra steps — pick a model and play."),
        "difficulty": "Easiest — choose a model and press Apply.",
        "restrictions": (f"only models from the {LLAMA3_8B_FAMILY} family, because that is what "
                         "the shipped game data expects; no custom servers, no API keys, "
                         "no web endpoints."),
    },
    MODE_DIRECT: {
        "title": MODE_LABELS[MODE_DIRECT],
        "blurb": ("The game connects straight to an AI server you run yourself (for example "
                  "llama-server). Faster and more controllable than the in-game loader, and "
                  "any model that server can load is fair game — but you have to start and "
                  "keep that server running yourself."),
        "difficulty": "For tinkerers — you run and maintain your own server.",
        "restrictions": ("the server must speak the llama.cpp protocol (/health, "
                         "/apply-template, /completion, /tokenize); the address has to fit the "
                         "9–12 character space the game reserves, with no https:// and no web "
                         "path such as /v1."),
    },
    MODE_SHIM: {
        "title": MODE_LABELS[MODE_SHIM],
        "blurb": ("The game talks to a small translator that this tool starts on your own "
                  "computer, and the translator talks to any modern AI service you like — a "
                  "local server or a hosted one. Any address, any model name, API keys and "
                  "https:// all work, because the translator holds them, not the game."),
        "difficulty": "Most flexible — a few more fields, and the translator must be running "
                      "while you play.",
        "restrictions": ("the translator has to be started before you play (Start button here, "
                         "or it is restarted for you); it listens on your machine only."),
    },
}

TERMS = [tuple(t) for t in (getattr(helpmod, "TERMS", None) or [])
         if isinstance(t, (list, tuple)) and len(tuple(t)) == 2 and str(t[0]).strip()]


def _compile_terms(terms):
    out = []
    for old, new in sorted(terms, key=lambda t: len(str(t[0])), reverse=True):
        try:
            out.append((re.compile(r"(?<![0-9A-Za-z_])" + re.escape(str(old)) +
                        r"(?![0-9A-Za-z_])"), str(new)))
        except re.error:                                     # pragma: no cover
            continue
    return out


_TERM_RES = _compile_terms(TERMS)
GITHUB_URL = str(getattr(helpmod, "GITHUB_URL", None) or GITHUB_URL_FALLBACK)
CREDIT = str(getattr(helpmod, "CREDIT", None) or CREDIT_FALLBACK)


def T(text) -> str:
    """Run a string *this file* writes through the shared terminology map.

    Whole words only, longest phrase first, so "LLMAgent" cannot be half-replaced
    by an "LLM" rule.  Copy that comes from vaudeville_help / vaudeville_cast is
    already in the right words and is rendered verbatim — never pass it through
    here, and never pass identifiers, file names or mode labels through here."""
    out = "" if text is None else str(text)
    for pattern, replacement in _TERM_RES:
        out = pattern.sub(replacement, out)
    return out


def _as_text(value) -> str:
    """Best-effort rendering of a help value that may be a string, dict or list."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        parts = []
        for k in ("what", "does", "text", "summary", "blurb", "body", "tip", "note",
                  "range", "why_change", "why_not"):
            v = value.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(v.strip())
        if not parts:
            parts = [v.strip() for v in value.values()
                     if isinstance(v, str) and v.strip()]
        return "\n".join(parts)
    if isinstance(value, (list, tuple)):
        return "\n".join(p for p in (_as_text(v) for v in value) if p)
    return str(value)


def _lookup(mapping, keys, fallback: str = "") -> str:
    """First non-empty entry for any of `keys` in a help mapping (or `fallback`)."""
    if isinstance(mapping, dict):
        for key in (keys if isinstance(keys, (list, tuple)) else (keys,)):
            if key in mapping:
                text = _as_text(mapping[key])
                if text:
                    return text
    return fallback


def _mode_cards() -> dict:
    raw = getattr(helpmod, "MODE_CARDS", None) or {}
    out = {}
    for key in MODE_ORDER:
        card = dict(MODE_CARDS_FALLBACK[key])
        card["key"] = key
        got = raw.get(key) if isinstance(raw, dict) else None
        if isinstance(got, dict):
            for name, value in got.items():
                if value not in (None, "", [], {}):
                    card[name] = value
        out[key] = card
    return out


def _setting_help() -> dict:
    fallback = {}
    for table in (AGENT_FIELDS_GUI, LLM_FIELDS_GUI):
        for name, _kind, note in table:
            fallback.setdefault(name, {"what": name, "does": note or "", "range": "",
                                       "why_change": "", "why_not": "", "tips": []})
    raw = getattr(helpmod, "SETTING_HELP", None) or {}
    out = dict(fallback)
    if isinstance(raw, dict):
        for name, value in raw.items():
            base = dict(fallback.get(name, {"what": name, "does": "", "range": "",
                                            "why_change": "", "why_not": "", "tips": []}))
            if isinstance(value, dict):
                for k, v in value.items():
                    if v not in (None, "", [], {}):
                        base[k] = v
            elif value not in (None, ""):
                base["does"] = str(value)
            out[name] = base
    return out


MODE_CARDS = _mode_cards()
SETTING_HELP = _setting_help()
GROUP_HELP = getattr(helpmod, "GROUP_HELP", None) or {}
MODEL_HELP = getattr(helpmod, "MODEL_HELP", None) or {}
SHIM_HELP = getattr(helpmod, "SHIM_HELP", None) or {}


def setting_tip(field: str, fallback: str = "") -> str:
    """Hover text for one setting, built from vaudeville_help.SETTING_HELP:
    what it is, what it does, the range, the tips, and when (not) to touch it.
    Help-module copy is used verbatim."""
    entry = SETTING_HELP.get(field)
    parts: list[str] = []
    if isinstance(entry, dict):
        for key in ("what", "does"):
            value = _as_text(entry.get(key))
            if value and value not in parts:
                parts.append(value)
        rng = _as_text(entry.get("range"))
        if rng:
            parts.append("Range: " + rng)
        tips = entry.get("tips") or []
        if isinstance(tips, (list, tuple)):
            for tip in list(tips)[:4]:
                if isinstance(tip, (list, tuple)):
                    cells = [str(c).strip() for c in tip if str(c).strip()]
                    if len(cells) > 1:
                        parts.append("· " + cells[0] + " — " + " ".join(cells[1:]))
                    elif cells:
                        parts.append("· " + cells[0])
                elif str(tip).strip():
                    parts.append("· " + str(tip).strip())
        for key, lead in (("why_change", "Worth changing when: "),
                          ("why_not", "Leave it alone: ")):
            value = _as_text(entry.get(key))
            if value:
                parts.append(lead + value)
    text = "\n".join(p for p in parts if p)
    if not text:
        text = _as_text(entry) or T(fallback) or field
    return text


def help_line(mapping, keys, fallback: str = "") -> str:
    """Verbatim help-module copy for `keys`, else `fallback` (already final text)."""
    if isinstance(mapping, str) and mapping.strip():
        return mapping.strip()
    text = _lookup(mapping, keys, "")
    return text or fallback


# --------------------------------------------------------------------------- #
# character groups (vaudeville_cast.py, with a single "all characters" fallback)
# --------------------------------------------------------------------------- #
def all_group_key() -> str:
    return str(getattr(cast, "ALL_KEY", None) or "all")


def cast_groups() -> list[dict]:
    raw = getattr(cast, "CAST_GROUPS", None) if cast is not None else None
    return [g for g in (raw or []) if isinstance(g, dict) and g.get("key")]


def group_choices() -> list[tuple[str, str]]:
    """[(key, label)] for the group dropdown: "All characters" first."""
    out = [(all_group_key(), "All characters")]
    for g in cast_groups():
        key = str(g["key"])
        if key == all_group_key():
            continue
        out.append((key, str(g.get("label") or key)))
    return out


def group_label(key) -> str:
    k = normalize_group(key)
    if k is None:
        return "All characters"
    for g in cast_groups():
        if str(g.get("key")) == k:
            return str(g.get("label") or k)
    return k.replace("_", " ").replace("-", " ").strip().title() or k


def normalize_group(key):
    """None means "every character"; anything else is a concrete group key."""
    if key is None:
        return None
    k = str(key).strip()
    if not k or k.lower() in (all_group_key().lower(), "all", "everyone", "everybody"):
        return None
    return k


def group_files(key) -> set[str]:
    """Asset file names that hold this group's characters (empty = unknown)."""
    k = normalize_group(key)
    if k is None:
        return set()
    files: set[str] = set()
    for g in cast_groups():
        if str(g.get("key")) == k:
            files |= {str(f) for f in (g.get("files") or []) if str(f).strip()}
    if not files:
        table = getattr(cast, "GROUP_OF_FILE", None) if cast is not None else None
        if isinstance(table, dict):
            files |= {str(f) for f, gk in table.items() if str(gk) == k}
    return files


def group_known(key) -> bool:
    k = normalize_group(key)
    if k is None:
        return True
    if any(str(g.get("key")) == k for g in cast_groups()):
        return True
    return bool(group_files(k))


def group_characters(key) -> list[str]:
    k = normalize_group(key)
    names: list[str] = []
    for g in cast_groups():
        if k is None or str(g.get("key")) == k:
            for n in (g.get("characters") or []):
                if str(n).strip() and str(n) not in names:
                    names.append(str(n))
    return names


def group_note(key) -> str:
    k = normalize_group(key)
    for g in cast_groups():
        if str(g.get("key")) == k:
            note = _as_text(g.get("note"))
            if note:
                return note
    return ""


def _file_matches(filename: str, pattern: str) -> bool:
    f = str(filename).replace("\\", "/").rsplit("/", 1)[-1]
    p = str(pattern).replace("\\", "/").rsplit("/", 1)[-1]
    if not f or not p:
        return False
    if f == p:
        return True
    stem = p[:-len(".assets")] if p.endswith(".assets") else p
    return bool(stem) and (f == stem or f.startswith(stem + "."))


def blob_in_group(blob, key) -> bool:
    """Does this component belong to the selected group?  Unknown group -> True
    (fail safe: never silently drop an edit)."""
    k = normalize_group(key)
    if k is None:
        return True
    patterns = group_files(k)
    if not patterns:
        return True
    path = getattr(blob, "path", None)
    name = getattr(path, "name", None) or str(path or "")
    return any(_file_matches(name, p) for p in patterns)


def group_blobs(res, key) -> list:
    return [b for b in getattr(res, "agents", []) if blob_in_group(b, key)]


def evidence_text() -> str:
    """Optional long-form backing for the group list (vaudeville_cast.EVIDENCE)."""
    return _as_text(getattr(cast, "EVIDENCE", None) if cast is not None else None)


def is_llama3_8b_family(name) -> bool:
    """True for GGUFs from the family Local Mode - Basic is limited to."""
    flat = re.sub(r"[^a-z0-9]", "", str(name).lower())
    return "llama38binstruct" in flat


def friendly_count(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _user_dirs() -> tuple[Path, Path, Path]:
    """(config, data, state) roots: XDG where present, else per-platform defaults."""
    if IS_WINDOWS:
        roaming = Path(os.environ.get("APPDATA", str(Path.home() / "AppData/Roaming")))
        local = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData/Local")))
        default = (roaming / APP_SLUG, local / APP_SLUG / "data", local / APP_SLUG / "state")
    else:
        default = (Path.home() / ".config" / APP_SLUG,
                   Path.home() / ".local/share" / APP_SLUG,
                   Path.home() / ".local/state" / APP_SLUG)
    env = (os.environ.get("XDG_CONFIG_HOME"), os.environ.get("XDG_DATA_HOME"),
           os.environ.get("XDG_STATE_HOME"))
    out = []
    for var, dflt in zip(env, default):
        out.append(Path(var) / APP_SLUG if var else dflt)
    return out[0], out[1], out[2]


CONFIG_DIR, DATA_HOME, STATE_HOME = _user_dirs()
CONFIG_FILE = CONFIG_DIR / "config.json"
BACKUP_ROOT = DATA_HOME / "backups"
PROFILE_FILE = STATE_HOME / "asset-profile.json"
LOG_FILE = STATE_HOME / "manager.log"

# --------------------------------------------------------------------------- #
# serialized field layouts (declaration order in LLMUnity v3.0.0 == Unity's
# serialization order).  Kinds: b=bool(4 bytes!) i=int32 f=float32 p=PPtr s=string
# --------------------------------------------------------------------------- #
LLM_LAYOUT = [
    ("advancedOptions", "b"), ("remote", "b"), ("port", "i"),
    ("APIKey", "s"), ("SSLCert", "s"), ("SSLKey", "s"),
    ("numThreads", "i"), ("numGPULayers", "i"), ("parallelPrompts", "i"),
    ("contextSize", "i"), ("batchSize", "i"), ("model", "s"),
    ("flashAttention", "b"), ("reasoning", "b"), ("lora", "s"), ("loraWeights", "s"),
    ("dontDestroyOnLoad", "b"), ("embeddingsOnly", "b"), ("embeddingLength", "i"),
    ("minContextLength", "i"), ("maxContextLength", "i"),
]
AGENT_LAYOUT = [
    ("advancedOptions", "b"), ("remote", "b"), ("llm", "p"), ("APIKey", "s"),
    ("host", "s"), ("port", "i"), ("numRetries", "i"), ("grammar", "s"),
    ("numPredict", "i"), ("cachePrompt", "b"), ("seed", "i"), ("temperature", "f"),
    ("topK", "i"), ("topP", "f"), ("minP", "f"), ("repeatPenalty", "f"),
    ("presencePenalty", "f"), ("frequencyPenalty", "f"), ("typicalP", "f"),
    ("repeatLastN", "i"), ("mirostat", "i"), ("mirostatTau", "f"), ("mirostatEta", "f"),
    ("nProbs", "i"), ("ignoreEos", "b"), ("save", "s"), ("debugPrompt", "b"),
    ("slot", "i"), ("systemPrompt", "s"),
]

# which fields actually reach a *remote* endpoint (llama.cpp /completion payload)
REMOTE_RELEVANT = {
    "temperature", "topK", "topP", "minP", "numPredict", "typicalP", "repeatPenalty",
    "repeatLastN", "presencePenalty", "frequencyPenalty", "mirostat", "mirostatTau",
    "mirostatEta", "seed", "ignoreEos", "nProbs", "cachePrompt", "grammar",
}
LOCAL_ONLY = {
    "numThreads", "numGPULayers", "parallelPrompts", "contextSize", "batchSize",
    "flashAttention", "reasoning", "model", "lora", "loraWeights", "embeddingsOnly",
    "embeddingLength",
}

# sanity ranges used to validate a candidate blob (value tolerant, layout strict)
def _ranges(kind, name, value):
    if kind == "b":
        return value in (0, 1)
    if kind != "i" and kind != "f":
        return True
    table = {
        "port": (0, 65535), "numRetries": (0, 1000), "numPredict": (-2, 1 << 20),
        "seed": (-(1 << 31), 1 << 31), "topK": (-1, 100000), "repeatLastN": (-1, 1 << 16),
        "mirostat": (0, 2), "nProbs": (0, 64), "slot": (-2, 4096),
        "numThreads": (-2, 4096), "numGPULayers": (-2, 100000),
        "parallelPrompts": (-2, 4096), "contextSize": (-1, 1 << 22),
        "batchSize": (-1, 1 << 22), "embeddingLength": (0, 1 << 18),
        "minContextLength": (-1, 1 << 24), "maxContextLength": (-1, 1 << 26),
        "temperature": (0.0, 4.0), "topP": (0.0, 1.5), "minP": (0.0, 1.5),
        "repeatPenalty": (0.0, 5.0), "presencePenalty": (-4.0, 4.0),
        "frequencyPenalty": (-4.0, 4.0), "typicalP": (0.0, 2.5),
        "mirostatTau": (0.0, 20.0), "mirostatEta": (0.0, 2.0),
    }
    if name in table:
        lo, hi = table[name]
        return lo <= value <= hi
    return True


def align4(n: int) -> int:
    return (n + 3) & ~3


def _shim_argv(cfg_path: Path) -> list[str]:
    """Command line for the shim child. A packaged (frozen) build re-executes the
    executable itself; a source run re-executes this file with the interpreter."""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--shim", "--config", str(cfg_path)]
    return [sys.executable, str(Path(__file__).resolve()), "--shim", "--config", str(cfg_path)]


def _detached_kwargs() -> dict:
    """Detach a child process on both platforms."""
    if IS_WINDOWS:
        flags = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                 | getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"creationflags": flags} if flags else {}
    return {"start_new_session": True}


def open_in_folder(path: Path) -> None:
    """Open a directory in the platform file manager."""
    if IS_WINDOWS and hasattr(os, "startfile"):
        os.startfile(str(path))                      # noqa: S606 - explorer on a dir
    elif IS_MACOS:
        subprocess.Popen(["open", str(path)], **_detached_kwargs())
    else:
        subprocess.Popen(["xdg-open", str(path)], **_detached_kwargs())


# --------------------------------------------------------------------------- #
# logging / config
# --------------------------------------------------------------------------- #
def log(msg: str) -> str:
    line = f"[{dt.datetime.now().isoformat(timespec='seconds')}] {msg}"
    try:
        STATE_HOME.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass
    return line


def load_config() -> dict:
    cfg = {
        "game_dir": "",
        "mode": "shim",            # off (Local-Basic) | direct (Local-Advanced) | shim (Remote-OpenAI)
        "shim_listen": "127.0.0.1",
        "shim_port": 13333,
        "backend_url": "",
        "backend_model": "",
        "api_key": "",             # only stored when save_api_key is true
        "save_api_key": False,
        "strip_think": True,
        "shim_mode": "chat",
        "agent_params": {},
        "llm_params": {},
        "primary_model": "",
        "deck_model": "",
        # GUI state: which setup was confirmed on the Home page, which character
        # group the Characters tab edits, and the Local-Advanced server address.
        "mode_confirmed": False,
        "cast_group": all_group_key(),
        "shim_backend_url": "",
        "direct_host": "",
        "direct_port": 0,
    }
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text(encoding="utf-8")))
    except Exception as exc:
        log(f"config load failed: {exc}")
    cfg["mode"] = normalize_mode(cfg.get("mode"), MODE_SHIM)
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(cfg)
    if not data.get("save_api_key"):
        data["api_key"] = ""
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, CONFIG_FILE)


# --------------------------------------------------------------------------- #
# Steam / game detection
# --------------------------------------------------------------------------- #
def _windows_steam_dirs() -> list[Path]:
    """Steam install dirs on Windows: registry first, then the usual defaults."""
    out: list[Path] = []

    def add(p: Path):
        if p.is_dir() and p not in out:
            out.append(p)

    try:
        import winreg                                  # windows only
        hives = [(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
                 (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam")]
        for hive, sub in hives:
            try:
                with winreg.OpenKey(hive, sub) as key:
                    for name in ("SteamPath", "InstallPath"):
                        try:
                            value, _ = winreg.QueryValueEx(key, name)
                        except OSError:
                            continue
                        add(Path(str(value).replace("\\", "/")))
            except OSError:
                continue
    except ImportError:
        pass
    prog = os.environ.get("PROGRAMFILES(X86)") or r"C:\Program Files (x86)"
    add(Path(prog) / "Steam")
    add(Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / "Steam")
    return out


def _vdf_library_paths() -> list[Path]:
    out: list[Path] = []
    candidates = [
        Path.home() / ".local/share/Steam/steamapps/libraryfolders.vdf",
        Path.home() / ".steam/steam/steamapps/libraryfolders.vdf",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/libraryfolders.vdf",
    ]
    candidates += [d / "steamapps" / "libraryfolders.vdf" for d in _windows_steam_dirs()]
    for vdf in candidates:
        if not vdf.is_file():
            continue
        try:
            text = vdf.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for m in re.finditer(r'"path"\s+"([^"]+)"', text):
            p = Path(m.group(1))
            if p.is_dir() and p not in out:
                out.append(p)
    return out


def _steam_roots() -> list[Path]:
    roots = _vdf_library_paths()
    for extra in tuple(_windows_steam_dirs()) + (
        Path.home() / ".local/share/Steam",
        Path.home() / ".steam/steam",
        Path.home() / ".steam/debian-installation",
        Path.home() / ".var/app/com.valvesoftware.Steam/.local/share/Steam",
        Path("/usr/share/steam"),
    ):
        if extra.is_dir() and extra not in roots:
            roots.append(extra)
    env = os.environ.get("STEAMPATH") or os.environ.get("STEAM_PATH")
    if env:
        p = Path(env).expanduser()
        if p.is_dir() and p not in roots:
            roots.append(p)
    return roots


def is_game_dir(path: Path) -> bool:
    return (path / DATA_DIR / STREAMING).is_dir() and (
        (path / DATA_DIR / "app.info").is_file()
        or (path / f"{GAME_DIR_NAME}.x86_64").is_file()
        or (path / f"{GAME_DIR_NAME}.exe").is_file()
    )


def find_game_dirs() -> list[tuple[Path, str]]:
    """Return [(path, how-it-was-found)] for every Vaudeville install we can see."""
    found: list[tuple[Path, str]] = []

    def add(p: Path, src: str):
        p = p.resolve()
        if is_game_dir(p) and p not in [f[0] for f in found]:
            found.append((p, src))

    for root in _steam_roots():
        sa = root / "steamapps"
        acf = sa / f"appmanifest_{STEAM_APPID}.acf"
        if acf.is_file():
            m = re.search(r'"installdir"\s+"([^"]+)"', acf.read_text(errors="replace"))
            if m:
                add(sa / "common" / m.group(1), f"appmanifest_{STEAM_APPID}.acf")
        common = sa / "common"
        if common.is_dir():
            for child in sorted(common.iterdir()):
                if child.name.lower().startswith("vaudeville"):
                    add(child, "steamapps/common scan")
    # a local copy that is not under Steam (e.g. an analysis copy)
    for base in (Path.cwd(), Path.cwd().parent, Path.home() / "Workspace"):
        if base.is_dir():
            try:
                for child in sorted(base.iterdir()):
                    if child.is_dir() and "vaudeville" in child.name.lower():
                        add(child, "workspace scan")
                if is_game_dir(base):
                    add(base, "current directory")
            except OSError:
                pass
    return found


def parse_tasklist_csv(text: str, image: str) -> list[int]:
    """PIDs of `image` from `tasklist /FO CSV /NH` output (pure, unit-testable)."""
    pids = []
    want = image.lower()
    for line in text.splitlines():
        cells = [c.strip().strip('"') for c in line.split('","')]
        if len(cells) >= 2 and (not want or cells[0].lower() == want) and cells[1].isdigit():
            pids.append(int(cells[1]))
    return pids


def _tasklist(query: list[str]) -> str:
    try:
        out = subprocess.run(["tasklist", *query, "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=15)
        return out.stdout or ""
    except (OSError, subprocess.SubprocessError):
        return ""


def pid_alive(pid: int) -> bool:
    if IS_WINDOWS:
        return pid in parse_tasklist_csv(_tasklist(["/FI", f"PID eq {pid}"]), "")
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def terminate_pid(pid: int, force: bool = False) -> bool:
    """SIGTERM/SIGKILL on POSIX, taskkill on Windows. Returns True when signalled."""
    if IS_WINDOWS:
        args = ["taskkill", "/PID", str(pid), "/T"] + (["/F"] if force else [])
        try:
            return subprocess.run(args, capture_output=True, timeout=15).returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        os.kill(pid, 9 if force else 15)
        return True
    except OSError:
        return False


def game_is_running(game_dir: Path) -> list[int]:
    if IS_WINDOWS:
        # the binary name is unique to the game, so image-name matching is exact enough
        return parse_tasklist_csv(_tasklist(["/FI", f"IMAGENAME eq {GAME_DIR_NAME}.exe"]),
                                  f"{GAME_DIR_NAME}.exe")
    pids = []
    target = str(game_dir.resolve())
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            exe = os.readlink(f"/proc/{entry}/exe")
        except OSError:
            continue
        if exe.startswith(target + os.sep) or exe == target:
            pids.append(int(entry))
    return pids


def build_guid(game_dir: Path) -> str:
    try:
        text = (game_dir / DATA_DIR / "boot.config").read_text(errors="replace")
        m = re.search(r"build-guid=(\S+)", text)
        if m:
            return m.group(1)
    except OSError:
        pass
    return "unknown"


# --------------------------------------------------------------------------- #
# blob decoding
# --------------------------------------------------------------------------- #
class Blob:
    """One decoded LLMUnity component living inside a Unity asset file."""

    def __init__(self, path: Path, kind: str, offset: int, size: int,
                 values: dict, offsets: dict, leftover: int):
        self.path = path
        self.kind = kind                  # "LLM" | "AGENT"
        self.offset = offset              # absolute file offset of field 0
        self.size = size
        self.values = values
        self.offsets = offsets            # field -> absolute file offset
        self.leftover = leftover

    def __repr__(self):
        return (f"<Blob {self.kind} {self.path.name}@{self.offset:#x} "
                f"size={self.size} leftover={self.leftover}>")

    def field_abs(self, name: str) -> int:
        return self.offsets[name]


def decode(blob: bytes, layout, base: int):
    values, offsets = {}, {}
    i = 0
    for name, kind in layout:
        offsets[name] = base + i
        if kind == "b":
            v = struct.unpack_from("<i", blob, i)[0]
            i += 4
        elif kind == "i":
            v = struct.unpack_from("<i", blob, i)[0]
            i += 4
        elif kind == "f":
            v = round(struct.unpack_from("<f", blob, i)[0], 7)
            i += 4
        elif kind == "p":
            f, p = struct.unpack_from("<iq", blob, i)
            v = (f, p)
            i += 12
        elif kind == "s":
            n = struct.unpack_from("<i", blob, i)[0]
            i += 4
            if n < 0 or i + n > len(blob):
                raise ValueError(f"bad string length {n} for {name}")
            v = blob[i:i + n].decode("utf-8", "replace")
            i += n
            i += (-i) % 4
        else:
            raise ValueError(kind)
        values[name] = v
    return values, offsets, len(blob) - i


def validate(values: dict, layout, leftover: int) -> bool:
    if leftover < 0:
        return False
    for name, kind in layout:
        v = values.get(name)
        if kind in ("b", "i", "f"):
            raw = v
            if kind == "b" and not _ok_bool_int(raw):
                return False
            if not _ranges(kind, name, raw):
                return False
        elif kind == "s":
            if not isinstance(v, str):
                return False
            if any(ord(ch) < 9 for ch in v):
                return False
    return True


def _ok_bool_int(v) -> bool:
    return v in (0, 1)


# --------------------------------------------------------------------------- #
# component scanner (stdlib only: structural anchors + strict validation)
# --------------------------------------------------------------------------- #
DEFAULT_SYSTEM_PROMPT_PREFIX = b"A chat between a curious human"
MAX_BLOB = 1 << 20


def _try_agent(data: bytes, start: int) -> Blob | None:
    if start < 4 or start + 136 > len(data):
        return None
    if struct.unpack_from("<i", data, start - 4)[0] != 0:      # m_Name must be empty
        return None
    head = data[start:start + MAX_BLOB]
    try:
        values, offsets, leftover = decode(head, AGENT_LAYOUT, start)
    except (ValueError, struct.error):
        return None
    if leftover < 0:
        return None
    if not validate(values, AGENT_LAYOUT, leftover):
        return None
    # extra structural checks that make false positives essentially impossible
    f, pid = values["llm"]
    if f not in (0, 1) or not (0 <= pid < 1 << 40):
        return None
    size = len(head) - leftover
    return Blob(Path(), "AGENT", start, size, values, offsets, leftover)


def _try_llm(data: bytes, start: int) -> Blob | None:
    if start < 4 or start + 72 > len(data):
        return None
    if struct.unpack_from("<i", data, start - 4)[0] != 0:
        return None
    head = data[start:start + MAX_BLOB]
    try:
        values, offsets, leftover = decode(head, LLM_LAYOUT, start)
    except (ValueError, struct.error):
        return None
    if leftover < 0 or not validate(values, LLM_LAYOUT, leftover):
        return None
    if not values["model"].lower().endswith(".gguf"):
        return None
    size = len(head) - leftover
    return Blob(Path(), "LLM", start, size, values, offsets, leftover)


def _agent_anchor_positions(data: bytes) -> set[int]:
    """Candidate blob starts for LLMAgent/LLMClient components."""
    out: set[int] = set()
    for m in re.finditer(re.escape(b"localhost"), data):
        out.add(m.start() - 28)                     # host len field is at +24
    for m in re.finditer(re.escape(DEFAULT_SYSTEM_PROMPT_PREFIX), data):
        out.add(m.start() - 136)                    # systemPrompt data is at +136
    return {p for p in out if p >= 4}


def _llm_anchor_positions(data: bytes) -> set[int]:
    out: set[int] = set()
    for m in re.finditer(re.escape(b".gguf"), data):
        end = m.end()
        for length in range(5, 200):
            s = end - length
            if s - 4 < 0:
                break
            if struct.unpack_from("<i", data, s - 4)[0] != length:
                continue
            name = data[s:end]
            if not re.fullmatch(rb"[A-Za-z0-9._\- ]+", name):
                break
            out.add(s - 48)                          # model len field is at +44
            break
    return {p for p in out if p >= 4}


def scan_file(path: Path, hints: set[int] | None = None) -> list[Blob]:
    data = _mmap_readonly(path)
    if data is None:
        return []
    try:
        return _scan_buffer(data, path, hints)
    finally:
        try:
            data.close()
        except Exception:
            pass


def _mmap_readonly(path: Path):
    import mmap
    try:
        fh = open(path, "rb")
    except OSError:
        return None
    try:
        if os.fstat(fh.fileno()).st_size == 0:
            fh.close()
            return None
        return mmap.mmap(fh.fileno(), 0, access=mmap.ACCESS_READ)
    except (ValueError, OSError):
        fh.close()
        return None


def _scan_buffer(data, path: Path, hints: set[int] | None) -> list[Blob]:
    blobs: list[Blob] = []
    seen: set[tuple[str, int]] = set()
    candidates = set()
    if hints:
        candidates |= hints
    candidates |= _agent_anchor_positions(data)
    candidates |= _llm_anchor_positions(data)
    for start in sorted(candidates):
        blob = _try_agent(data, start) or _try_llm(data, start)
        if blob is None:
            continue
        key = (blob.kind, blob.offset)
        if key in seen:
            continue
        seen.add(key)
        blob.path = path
        blobs.append(blob)
    return blobs


def _profile_key(game_dir: Path, path: Path) -> str:
    st = path.stat()
    return f"{path.name}:{st.st_size}"


def load_profile(game_dir: Path) -> dict:
    try:
        prof = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if prof.get("build_guid") != build_guid(game_dir):
        return {}
    if prof.get("game_dir") != str(game_dir.resolve()):
        return {}
    return prof.get("files", {})


def save_profile(game_dir: Path, files: dict) -> None:
    try:
        STATE_HOME.mkdir(parents=True, exist_ok=True)
        PROFILE_FILE.write_text(json.dumps({
            "build_guid": build_guid(game_dir),
            "game_dir": str(game_dir.resolve()),
            "saved": dt.datetime.now().isoformat(timespec="seconds"),
            "files": files,
        }, indent=1), encoding="utf-8")
    except OSError as exc:
        log(f"profile save failed: {exc}")


def candidate_files(game_dir: Path) -> list[Path]:
    data = game_dir / DATA_DIR
    out = []
    for p in sorted(data.iterdir()):
        if not p.is_file():
            continue
        if p.suffix in (".resS", ".resource"):
            continue
        if ".bak-" in p.name or p.name.endswith((".lock", ".info", ".config", ".json")):
            continue
        if p.stat().st_size < 512:
            continue
        out.append(p)
    return out


class ScanResult:
    def __init__(self, game_dir: Path):
        self.game_dir = game_dir
        self.blobs: list[Blob] = []
        self.notes: list[str] = []
        self.duration = 0.0

    @property
    def llm(self) -> list[Blob]:
        return [b for b in self.blobs if b.kind == "LLM"]

    @property
    def agents(self) -> list[Blob]:
        return [b for b in self.blobs if b.kind == "AGENT"]

    def summary(self) -> str:
        per_file = {}
        for b in self.blobs:
            per_file.setdefault(b.path.name, {"LLM": 0, "AGENT": 0})
            per_file[b.path.name][b.kind] += 1
        parts = [f"{n}: {c['LLM']} LLM / {c['AGENT']} agents" for n, c in sorted(per_file.items())]
        return "; ".join(parts) or "no LLMUnity components found"

    def friendly_summary(self) -> str:
        """One plain-English line about what the scan found.

        `summary()` is the per-file component table: right for a log file or
        `--cli scan`, and exactly the jargon the GUI should not show."""
        if not self.blobs:
            return T("no AI settings found in this folder")
        files = {b.path.name for b in self.blobs}
        return (T("settings for ") +
                friendly_count(len(self.agents), "character", "characters") + T(" and ") +
                friendly_count(len(self.llm), "AI engine block", "AI engine blocks") +
                T(" in ") + friendly_count(len(files), "game file", "game files"))


def scan_game(game_dir: Path, use_profile: bool = True, progress=None) -> ScanResult:
    t0 = time.time()
    res = ScanResult(game_dir)
    if not is_game_dir(game_dir):
        res.notes.append(f"not a Vaudeville game directory: {game_dir}")
        return res
    profile = load_profile(game_dir) if use_profile else {}
    new_profile: dict[str, dict] = {}
    for path in candidate_files(game_dir):
        key = _profile_key(game_dir, path)
        hints = set()
        entry = profile.get(key)
        if entry:
            hints = {int(x) for x in entry.get("offsets", [])}
        try:
            blobs = scan_file(path, hints)
        except OSError as exc:
            res.notes.append(f"{path.name}: read error {exc}")
            continue
        for b in blobs:
            res.blobs.append(b)
        if blobs:
            new_profile[key] = {"offsets": [b.offset for b in blobs],
                                "kinds": [b.kind for b in blobs]}
        if progress:
            progress(path.name, len(blobs))
    save_profile(game_dir, new_profile)
    res.duration = time.time() - t0
    if not res.llm:
        res.notes.append("no AI engine block found in this folder — is it the Vaudeville install?")
    if not res.agents:
        res.notes.append("no character settings found in this folder")
    return res


# --------------------------------------------------------------------------- #
# patching (in place, hash-verified backups, post-write re-decode)
# --------------------------------------------------------------------------- #
class Edit:
    def __init__(self, path: Path, offset: int, old: bytes, new: bytes, label: str):
        self.path, self.offset, self.old, self.new, self.label = path, offset, old, new, label

    def __repr__(self):
        return f"<Edit {self.path.name}@{self.offset:#x} {self.label}>"


class PatchError(RuntimeError):
    pass


def field_kind(kind: str, name: str):
    layout = AGENT_LAYOUT if kind == "AGENT" else LLM_LAYOUT
    for n, k in layout:
        if n == name:
            return k
    raise PatchError(f"no serialized field {name!r} on {kind}")


def encode_field(kind: str, name: str, fkind: str, old, new) -> bytes:
    if fkind == "b":
        if isinstance(new, str):
            new = new.strip().lower() in ("1", "true", "yes", "on")
        return struct.pack("<i", 1 if new else 0)
    if fkind == "i":
        return struct.pack("<i", int(new))
    if fkind == "f":
        return struct.pack("<f", float(new))
    if fkind == "s":
        nb = str(new).encode("utf-8")
        ob = str(old).encode("utf-8")
        if align4(len(nb)) != align4(len(ob)):
            raise PatchError(
                f"{name}: cannot change a {len(ob)}-byte string to {len(nb)} bytes in "
                f"place (needs ceil4 equal: {align4(len(ob))} vs {align4(len(nb))}). "
                f"For '{name}' that means {align4(len(ob)) - 3}..{align4(len(ob))} characters.")
        return struct.pack("<i", len(nb)) + nb + b"\x00" * (align4(len(nb)) - len(nb))
    raise PatchError(f"field {name} is not patchable (kind {fkind})")


def plan_edits(res: ScanResult, agent_changes: dict, llm_changes: dict,
               agent_group=None, group_fields=None) -> tuple[list[Edit], list[str]]:
    """Byte edits for `agent_changes` (character settings) and `llm_changes` (engine).

    `agent_group` optionally narrows the *character* fields to one cast group,
    matched through vaudeville_cast.GROUP_OF_FILE / CAST_GROUPS[*]["files"] against
    each component's asset file name.  The endpoint wiring fields (ENDPOINT_FIELDS)
    still go to every character, so the game never ends up half local / half
    remote.  None or cast.ALL_KEY keeps the historical uniform behaviour.
    `group_fields` overrides which fields are narrowed (default: every character
    field except ENDPOINT_FIELDS).
    """
    edits: list[Edit] = []
    warnings: list[str] = []
    data_cache: dict[Path, bytes] = {}
    scoped: set[str] | None = None
    group_key = normalize_group(agent_group)
    if group_key is not None and group_known(group_key):
        scoped = set(group_fields) if group_fields else {
            n for n, _ in AGENT_LAYOUT if n not in ENDPOINT_FIELDS}

    def read_at(path: Path, offset: int, n: int) -> bytes:
        if path not in data_cache:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data_cache[path] = fh.read(n)
        return data_cache[path]

    for blob in res.blobs:
        changes = agent_changes if blob.kind == "AGENT" else llm_changes
        skip = (scoped if (scoped and not blob_in_group(blob, group_key)) else frozenset())
        for name, new in changes.items():
            if name in skip:
                continue
            try:
                fk = field_kind(blob.kind, name)
            except PatchError as exc:
                warnings.append(str(exc))
                continue
            if fk == "p":
                warnings.append(f"{name}: PPtr fields are not patchable")
                continue
            off = blob.offsets[name]
            width = 4 if fk in ("b", "i", "f") else 4 + align4(len(str(blob.values[name]).encode()))
            with open(blob.path, "rb") as fh:
                fh.seek(off)
                current = fh.read(width)
            try:
                new_bytes = encode_field(blob.kind, name, fk, blob.values[name], new)
            except PatchError as exc:
                warnings.append(f"{blob.path.name}@{off:#x}: {exc}")
                continue
            if new_bytes == current[:len(new_bytes)]:
                continue
            label = (f"{blob.kind}.{name}: {blob.values[name]!r} -> "
                     f"{new!r} ({blob.path.name}@{off:#x})")
            edits.append(Edit(blob.path, off, current[:len(new_bytes)], new_bytes, label))
    return edits, warnings


class BackupRecord:
    def __init__(self, directory: Path, manifest: dict):
        self.dir = directory
        self.manifest = manifest

    @property
    def timestamp(self) -> str:
        return self.manifest.get("timestamp", self.dir.name)


def list_backups() -> list[BackupRecord]:
    out = []
    if not BACKUP_ROOT.is_dir():
        return out
    for d in sorted(BACKUP_ROOT.iterdir(), reverse=True):
        man = d / "manifest.json"
        if man.is_file():
            try:
                out.append(BackupRecord(d, json.loads(man.read_text(encoding="utf-8"))))
            except Exception:
                continue
    return out


def apply_edits(edits: list[Edit], game_dir: Path, do_backup: bool = True) -> BackupRecord:
    if not edits:
        raise PatchError("nothing to apply")
    running = game_is_running(game_dir)
    if running:
        raise PatchError(
            f"Vaudeville appears to be running (pid {', '.join(map(str, running))}). "
            "Close the game before patching its asset files.")
    for path in {e.path for e in edits}:
        if not path.is_file():
            raise PatchError(f"missing file {path}")
    record = None
    if do_backup:
        record = create_backup(game_dir, sorted({e.path for e in edits}),
                               [e.label for e in edits])
    by_file: dict[Path, list[Edit]] = {}
    for e in edits:
        by_file.setdefault(e.path, []).append(e)
    for path, file_edits in by_file.items():
        with open(path, "r+b") as fh:
            for e in sorted(file_edits, key=lambda x: x.offset):
                fh.seek(e.offset)
                written = fh.read(len(e.new))
                if written != e.old:
                    raise PatchError(
                        f"{path.name}@{e.offset:#x}: file changed since the scan "
                        f"(expected {e.old.hex()}, found {written.hex()}). Re-scan and retry.")
                fh.seek(e.offset)
                fh.write(e.new)
        log(f"wrote {len(file_edits)} patch(es) to {path}")
    if record is not None:
        record.manifest["applied"] = True
        record.manifest["sha256_after"] = {
            str(p): sha256(p) for p in by_file}
        (record.dir / "manifest.json").write_text(
            json.dumps(record.manifest, indent=1), encoding="utf-8")
    return record


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def create_backup(game_dir: Path, paths: list[Path], labels: list[str]) -> BackupRecord:
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    target = BACKUP_ROOT / f"{ts}-{game_dir.name}"
    n = 0
    while target.exists():
        n += 1
        target = BACKUP_ROOT / f"{ts}-{game_dir.name}.{n}"
    target.mkdir(parents=True)
    manifest = {
        "timestamp": ts,
        "game_dir": str(game_dir.resolve()),
        "build_guid": build_guid(game_dir),
        "app_version": APP_VERSION,
        "labels": labels,
        "files": [],
    }
    for p in paths:
        dst = target / p.name
        if dst.exists():
            raise PatchError(f"refusing to overwrite {dst}")
        shutil.copy2(p, dst)
        before = sha256(p)
        copied = sha256(dst)
        if before != copied:
            raise PatchError(f"backup verification failed for {dst}")
        manifest["files"].append({"path": str(p), "name": p.name, "sha256": before,
                                  "backup": str(dst), "size": p.stat().st_size})
        log(f"backup verified: {dst.name} sha256={before[:16]}…")
    (target / "manifest.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    return BackupRecord(target, manifest)


def restore_backup(record: BackupRecord, game_dir: Path | None = None) -> list[str]:
    running_dirs = []
    out = []
    for entry in record.manifest["files"]:
        path = Path(entry["path"])
        if game_is_running(path.parent.parent):
            raise PatchError(f"game is running; cannot restore {path}")
        cur = sha256(path) if path.exists() else None
        if cur == entry["sha256"]:
            out.append(f"unchanged: {path.name}")
            continue
        shutil.copy2(Path(entry["backup"]), path)
        if sha256(path) != entry["sha256"]:
            raise PatchError(f"restore verification failed for {path}")
        out.append(f"restored: {path.name} -> sha256 {entry['sha256'][:16]}…")
        log(out[-1])
    return out


# --------------------------------------------------------------------------- #
# GGUF model management (symlink swap, originals preserved)
# --------------------------------------------------------------------------- #
class ModelSlot:
    def __init__(self, name: str, label: str):
        self.name = name          # the filename the build hard-codes
        self.label = label

    def link(self, game_dir: Path) -> Path:
        return game_dir / DATA_DIR / STREAMING / self.name


def model_slots() -> list[ModelSlot]:
    return [ModelSlot(PRIMARY_MODEL_NAME, "Main dialogue model"),
            ModelSlot(DECK_MODEL_NAME, "Steam Deck / fallback model")]


def library_dir(game_dir: Path) -> Path:
    return game_dir / DATA_DIR / STREAMING / GGUF_LIBRARY_DIR


def list_library_models(game_dir: Path) -> list[Path]:
    lib = library_dir(game_dir)
    out = []
    if lib.is_dir():
        out += sorted(p for p in lib.rglob("*") if p.is_file() and p.suffix.lower() == ".gguf")
    sa = game_dir / DATA_DIR / STREAMING
    out += sorted(p for p in sa.glob("*.gguf") if p.is_file())
    seen, uniq = set(), []
    for p in out:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            uniq.append(p)
    return uniq


def slot_status(game_dir: Path, slot: ModelSlot) -> dict:
    link = slot.link(game_dir)
    info = {"name": slot.name, "exists": link.exists(), "is_link": link.is_symlink(),
            "target": "", "size": 0, "broken": False}
    if link.is_symlink():
        try:
            info["target"] = str(Path(os.readlink(link)).resolve())
        except OSError:
            info["target"] = os.readlink(link)
        info["broken"] = not link.exists()
    elif link.is_file():
        info["target"] = str(link)
    if link.exists():
        try:
            st = link.stat()
            info["size"] = st.st_size
            if not info["is_link"] and getattr(st, "st_nlink", 1) > 1:
                info["is_link"] = True          # hardlink stand-in (Windows)
                info["target"] = str(link.resolve())
        except OSError:
            pass
    return info


def _make_link(tmp: Path, target: Path) -> str:
    """Symlink where possible; on Windows fall back to a same-volume hardlink
    (creating symlinks there needs Developer Mode or SeCreateSymbolicLink)."""
    try:
        tmp.symlink_to(target)
        return "symlink"
    except OSError:
        if not IS_WINDOWS:
            raise
        os.link(target, tmp)
        return "hardlink"


def set_slot_model(game_dir: Path, slot: ModelSlot, model: Path, dry_run: bool = False) -> list[str]:
    """Point a hard-coded model filename at `model` via symlink (or hardlink on
    Windows), preserving any real file."""
    model = Path(model).expanduser().resolve()
    if not model.is_file():
        raise PatchError(f"model file not found: {model}")
    if model.suffix.lower() != ".gguf":
        raise PatchError(f"not a .gguf file: {model}")
    if game_is_running(game_dir):
        raise PatchError("close the game before changing models")
    link = slot.link(game_dir)
    actions = []
    lib = library_dir(game_dir)
    if link.is_symlink():
        current = Path(os.readlink(link)).resolve()
        if current == model:
            actions.append(f"{slot.name}: already points at {model.name}")
            return actions
        actions.append(f"repoint link {slot.name}: {current.name} -> {model.name}")
        if not dry_run:
            tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
            tmp.unlink(missing_ok=True)
            kind = _make_link(tmp, model)
            os.replace(tmp, link)
            actions.append(f"{slot.name} is now a {kind}")
        return actions
    if link.is_file():
        if not dry_run:
            lib.mkdir(parents=True, exist_ok=True)
            preserved = lib / f"ORIGINAL_{slot.name}"
            if preserved.exists():
                raise PatchError(f"{preserved} already exists; move it away first")
            shutil.move(str(link), str(preserved))
            actions.append(f"preserved the shipped file as {preserved.name}")
        else:
            actions.append(f"would preserve the shipped {slot.name} into {GGUF_LIBRARY_DIR}/ORIGINAL_{slot.name}")
    elif link.exists():
        raise PatchError(f"{link} exists but is neither a file nor a symlink")
    else:
        actions.append(f"create link {slot.name}")
    if not dry_run:
        tmp = link.with_name(f".{link.name}.tmp-{os.getpid()}")
        tmp.unlink(missing_ok=True)
        kind = _make_link(tmp, model)
        os.replace(tmp, link)
        if not link.exists():
            raise PatchError("link verification failed")
        actions.append(f"{slot.name} link kind: {kind}")
    actions.append(f"{slot.name} -> {model}")
    log("; ".join(actions))
    return actions


def human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if f < 1024 or unit == "TiB":
            return f"{f:.0f} {unit}" if unit == "B" else f"{f:.2f} {unit}"
        f /= 1024
    return f"{n} B"


# --------------------------------------------------------------------------- #
# embedded translation shim: llama.cpp-server protocol  <->  OpenAI-compatible
# --------------------------------------------------------------------------- #
class ThinkStripper:
    """Drop <think>...</think> blocks (they would otherwise be spoken by the TTS)."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self, enabled: bool = True):
        self.enabled, self.state, self.held = enabled, "pre", ""
        self.stripped, self.blocks = 0, 0

    @staticmethod
    def _partial_tail(text: str, tag: str) -> int:
        for k in range(min(len(text), len(tag) - 1), 0, -1):
            if tag.startswith(text[-k:]):
                return k
        return 0

    def feed(self, piece: str) -> str:
        if not self.enabled:
            return piece
        self.held += piece
        out = ""
        while self.held:
            if self.state in ("pre", "post"):
                i = self.held.find(self.OPEN)
                if i >= 0:
                    out += self.held[:i]
                    self.held = self.held[i + len(self.OPEN):]
                    self.state, self.blocks = "in", self.blocks + 1
                    continue
                keep = self._partial_tail(self.held, self.OPEN)
                out += self.held[:len(self.held) - keep]
                self.held = self.held[len(self.held) - keep:]
                break
            j = self.held.find(self.CLOSE)
            if j >= 0:
                self.stripped += j + len(self.CLOSE)
                self.held = self.held[j + len(self.CLOSE):]
                self.state = "post"
                continue
            keep = self._partial_tail(self.held, self.CLOSE)
            self.stripped += len(self.held) - keep
            self.held = self.held[len(self.held) - keep:]
            break
        return out

    def flush(self) -> str:
        if not self.enabled:
            return ""
        if self.state == "in":
            self.stripped += len(self.held)
            self.held = ""
            return ""
        out, self.held = self.held, ""
        return out


MARKER = "<<<vlm:messages:"
MARKER_END = ">>>"


def to_openai_params(body: dict) -> dict:
    out = {}
    n_predict = body.get("n_predict", -1)
    if isinstance(n_predict, int) and n_predict > 0:
        out["max_tokens"] = n_predict
    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("presence_penalty", "presence_penalty"),
                     ("frequency_penalty", "frequency_penalty"), ("seed", "seed")):
        if body.get(src) is not None:
            out[dst] = body[src]
    if body.get("stop"):
        out["stop"] = body["stop"]
    return out


def render_prompt_fallback(messages: list) -> str:
    parts = [f"<|{m.get('role','user')}|>\n{m.get('content','')}" for m in messages]
    parts.append("<|assistant|>\n")
    return "\n".join(parts)


class Backend:
    def __init__(self, base_url, api_key, model, timeout=600.0, insecure=False, verbose=False):
        self.base_url = base_url.rstrip("/")
        self.api_key, self.model, self.timeout = api_key, model, timeout
        self.verbose = verbose
        self._ctx = None
        if insecure:
            import ssl
            self._ctx = ssl.create_default_context()
            self._ctx.check_hostname = False
            self._ctx.verify_mode = ssl.CERT_NONE

    def _open(self, path, payload, stream):
        headers = {"Content-Type": "application/json",
                   "Accept": "text/event-stream" if stream else "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.base_url + path,
                                     data=json.dumps(payload).encode("utf-8"),
                                     headers=headers, method="POST")
        return urllib.request.urlopen(req, timeout=self.timeout, context=self._ctx)

    @staticmethod
    def _sse_lines(resp):
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if line.startswith("data:"):
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue

    def chat_stream(self, messages, params):
        payload = {"model": self.model, "messages": messages, "stream": True}
        payload.update(params)
        with self._open("/chat/completions", payload, True) as resp:
            for obj in self._sse_lines(resp):
                choice = (obj.get("choices") or [{}])[0]
                piece = (choice.get("delta") or {}).get("content")
                if piece is None:
                    piece = choice.get("text")
                if piece:
                    yield piece

    def chat_once(self, messages, params) -> str:
        payload = {"model": self.model, "messages": messages, "stream": False}
        payload.update(params)
        with self._open("/chat/completions", payload, False) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        return obj["choices"][0].get("message", {}).get("content", "")

    def completion_once(self, prompt, params) -> str:
        payload = {"model": self.model, "prompt": prompt, "stream": False}
        payload.update(params)
        with self._open("/completions", payload, False) as resp:
            obj = json.loads(resp.read().decode("utf-8", "replace"))
        return obj["choices"][0]["text"]

    def completion_stream(self, prompt, params):
        payload = {"model": self.model, "prompt": prompt, "stream": True}
        payload.update(params)
        with self._open("/completions", payload, True) as resp:
            for obj in self._sse_lines(resp):
                choice = (obj.get("choices") or [{}])[0]
                piece = choice.get("text") or (choice.get("delta") or {}).get("content")
                if piece:
                    yield piece

    def embeddings(self, text) -> list:
        try:
            with self._open("/embeddings", {"model": self.model, "input": text}, False) as resp:
                return json.loads(resp.read().decode())["data"][0]["embedding"]
        except Exception:
            return []


class ShimState:
    def __init__(self):
        self.lock = threading.Lock()
        self.store: dict[int, list] = {}
        self.next_id = 1

    def put(self, messages) -> int:
        with self.lock:
            mid = self.next_id
            self.next_id += 1
            self.store[mid] = messages
            if len(self.store) > 512:
                self.store.pop(min(self.store))
            return mid

    def get(self, mid):
        with self.lock:
            return self.store.get(mid)


def make_shim_handler(cfg: dict, backend: Backend, state: ShimState, logfn):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = f"{APP_SLUG}-shim/{APP_VERSION}"

        def log_message(self, fmt, *a):
            if cfg.get("verbose"):
                logfn("shim: " + (fmt % a))

        def _json_body(self) -> dict:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b""
            try:
                return json.loads(raw.decode("utf-8", "replace")) if raw else {}
            except json.JSONDecodeError:
                return {}

        def _send(self, obj, status=200):
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(body)

        def _sse_begin(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Transfer-Encoding", "chunked")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()

        def _sse_write(self, obj):
            payload = ("data: " + json.dumps(obj) + "\n\n").encode("utf-8")
            self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.flush()

        def _sse_end(self):
            # NOTE: deliberately no 'data: [DONE]' — LlamaLib's cpp-httplib receiver
            # treats it as a cancelled transfer and retries 6x, discarding the text.
            if cfg.get("sse_done"):
                payload = b"data: [DONE]\n\n"
                self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        def do_OPTIONS(self):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            if self.path.startswith("/v1/models") or self.path.startswith("/props"):
                self._send({"models": [{"id": backend.model}], "model": backend.model})
            else:
                self._send({"status": "ok"})

        def do_POST(self):
            path = self.path.split("?")[0]
            body = self._json_body()
            if cfg.get("verbose"):
                logfn(f"shim: POST {path} keys={sorted(body)}")
            try:
                if path in ("/health", "/v1/health"):
                    self._send({"status": "ok"})
                elif path == "/apply-template":
                    self._apply_template(body)
                elif path in ("/completion", "/completions"):
                    self._completion(body)
                elif path in ("/chat/completions", "/v1/chat/completions"):
                    self._openai_passthrough(body)
                elif path == "/tokenize":
                    text = body.get("content", "") or body.get("prompt", "")
                    n = max(1, len(text) // 4)
                    self._send({"count": n, "tokens": list(range(min(n, 4096)))})
                elif path == "/detokenize":
                    self._send({"content": ""})
                elif path in ("/embedding", "/embeddings"):
                    vec = backend.embeddings(body.get("content", "")) if cfg.get("embeddings") else []
                    self._send([{"embedding": vec or [0.0] * int(cfg.get("embedding_dim", 8))}])
                elif path == "/slots":
                    self._send([])
                else:
                    self._send({"status": "ok"})
            except Exception as exc:
                logfn(f"shim: error on {path}: {exc}")
                self._send({"error": {"code": 502, "message": str(exc)}})

        def _apply_template(self, body):
            messages = body.get("messages") or []
            if cfg.get("mode", "chat") == "chat" and messages:
                self._send({"prompt": f"{MARKER}{state.put(messages)}{MARKER_END}"})
            else:
                self._send({"prompt": render_prompt_fallback(messages)})

        def _resolve(self, prompt: str):
            if prompt.startswith(MARKER) and prompt.endswith(MARKER_END):
                try:
                    return state.get(int(prompt[len(MARKER):-len(MARKER_END)]))
                except ValueError:
                    return None
            return None

        def _completion(self, body):
            prompt = body.get("prompt", "")
            messages = self._resolve(prompt)
            stream = bool(body.get("stream"))
            params = to_openai_params(body)
            use_chat = messages is not None or cfg.get("mode", "chat") == "chat"
            slot = body.get("id_slot", 0)
            if not stream:
                if use_chat:
                    text = backend.chat_once(messages or [{"role": "user", "content": prompt}], params)
                else:
                    text = backend.completion_once(prompt, params)
                if cfg.get("strip_think", True):
                    st = ThinkStripper(True)
                    text = st.feed(text) + st.flush()
                self._send({"content": text, "stop": True, "id_slot": slot,
                            "model": backend.model,
                            "tokens_predicted": max(1, len(text) // 4)})
                return
            self._sse_begin()
            strip = ThinkStripper(bool(cfg.get("strip_think", True)))
            total = ""
            try:
                gen = (backend.chat_stream(messages or [{"role": "user", "content": prompt}], params)
                       if use_chat else backend.completion_stream(prompt, params))
                for piece in gen:
                    emit = strip.feed(piece)
                    if not emit:
                        continue
                    total += emit
                    self._sse_write({"content": emit, "stop": False, "id_slot": slot,
                                     "model": backend.model})
                tail = strip.flush()
                if tail:
                    total += tail
                    self._sse_write({"content": tail, "stop": False, "id_slot": slot,
                                     "model": backend.model})
            except Exception as exc:
                logfn(f"shim: backend failure: {exc}")
                self._sse_write({"content": "", "stop": True, "id_slot": slot,
                                 "error": {"code": 502, "message": str(exc)}})
                self._sse_end()
                return
            if strip.stripped and cfg.get("verbose"):
                logfn(f"shim: stripped {strip.stripped} chars of reasoning")
            self._sse_write({"content": "", "stop": True, "id_slot": slot,
                             "model": backend.model,
                             "tokens_predicted": max(1, len(total) // 4)})
            self._sse_end()

        def _openai_passthrough(self, body):
            messages = body.get("messages", [])
            params = {k: v for k, v in body.items()
                      if k in ("temperature", "top_p", "max_tokens", "stop", "seed",
                               "presence_penalty", "frequency_penalty")}
            if not body.get("stream"):
                self._send({"choices": [{"message": {"role": "assistant",
                                                     "content": backend.chat_once(messages, params)}}]})
                return
            self._sse_begin()
            for piece in backend.chat_stream(messages, params):
                self._sse_write({"choices": [{"delta": {"content": piece}}]})
            self._sse_end()

    return Handler


def run_shim(cfg: dict, logfn=print) -> int:
    api_key = cfg.get("api_key") or os.environ.get("VLM_API_KEY") or os.environ.get("LLM_API_KEY") or ""
    backend = Backend(cfg.get("backend_url", ""), api_key, cfg.get("backend_model", ""),
                      timeout=float(cfg.get("timeout", 600.0)),
                      insecure=bool(cfg.get("insecure", False)))
    state = ShimState()
    httpd = ThreadingHTTPServer((cfg.get("listen", "127.0.0.1"), int(cfg.get("port", 13333))),
                                make_shim_handler(cfg, backend, state, logfn))
    logfn(f"shim listening on http://{cfg.get('listen','127.0.0.1')}:{cfg.get('port',13333)} "
          f"-> {backend.base_url or '<no backend>'} mode={cfg.get('mode','chat')} "
          f"model={backend.model or '-'} strip_think={cfg.get('strip_think', True)} "
          f"api_key={'set' if api_key else 'none'}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# shim process management
# --------------------------------------------------------------------------- #
SHIM_CONFIG_FILE = STATE_HOME / "shim-config.json"
SHIM_LOG_FILE = STATE_HOME / "shim.log"


class ShimProcess:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.proc: subprocess.Popen | None = None
        self.pid_file = STATE_HOME / "shim.pid"

    # -- state ------------------------------------------------------------- #
    def _read_pid(self) -> int | None:
        try:
            pid = int(self.pid_file.read_text().strip())
        except Exception:
            return None
        return pid if pid_alive(pid) else None

    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None or self._read_pid() is not None

    def pid(self) -> int | None:
        if self.proc is not None and self.proc.poll() is None:
            return self.proc.pid
        return self._read_pid()

    def write_config(self) -> Path:
        """Config for the child process. The API key is passed via env, never here."""
        STATE_HOME.mkdir(parents=True, exist_ok=True)
        data = {k: v for k, v in self.cfg.items() if k != "api_key"}
        SHIM_CONFIG_FILE.write_text(json.dumps(data, indent=1), encoding="utf-8")
        os.chmod(SHIM_CONFIG_FILE, 0o600)
        return SHIM_CONFIG_FILE

    def start(self) -> tuple[bool, str]:
        if self.running():
            return True, f"shim already running (pid {self.pid()})"
        if not self.cfg.get("backend_url"):
            return False, "no backend BaseURL configured"
        port = int(self.cfg.get("port", 13333))
        if port_in_use(port, self.cfg.get("listen", "127.0.0.1")):
            owner = self._read_pid()
            return False, (f"port {port} is already in use"
                           + (f" (shim pid {owner}?)" if owner else " — pick another port or stop the other service"))
        cfg_path = self.write_config()
        env = dict(os.environ)
        if self.cfg.get("api_key"):
            env["VLM_API_KEY"] = self.cfg["api_key"]
        env.pop("LLM_API_KEY", None)
        STATE_HOME.mkdir(parents=True, exist_ok=True)
        logfh = open(SHIM_LOG_FILE, "a", encoding="utf-8")
        logfh.write(f"\n=== shim start {dt.datetime.now().isoformat(timespec='seconds')} ===\n")
        logfh.flush()
        try:
            self.proc = subprocess.Popen(
                _shim_argv(cfg_path),
                stdout=logfh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                env=env, cwd=str(Path.home()), **_detached_kwargs())
        except OSError as exc:
            return False, f"failed to start shim: {exc}"
        self.pid_file.write_text(str(self.proc.pid))
        for _ in range(60):
            if self.health()[0]:
                log(f"shim started (pid {self.proc.pid}) on port {port}")
                return True, f"shim running (pid {self.proc.pid}) on port {port}"
            if self.proc.poll() is not None:
                return False, f"shim exited immediately (rc={self.proc.returncode}); see {SHIM_LOG_FILE}"
            time.sleep(0.15)
        return False, f"shim did not answer /health in time; see {SHIM_LOG_FILE}"

    def stop(self) -> str:
        pid = self.pid()
        if not pid:
            self.proc = None
            return "shim was not running"
        if not terminate_pid(pid):
            return f"stop failed: could not signal pid {pid}"
        for _ in range(40):
            if not pid_alive(pid):
                break
            time.sleep(0.1)
        else:
            terminate_pid(pid, force=True)
        self.pid_file.unlink(missing_ok=True)
        self.proc = None
        log(f"shim stopped (pid {pid})")
        return f"shim stopped (pid {pid})"

    def health(self) -> tuple[bool, str]:
        url = f"http://{self.cfg.get('listen','127.0.0.1')}:{int(self.cfg.get('port',13333))}/health"
        try:
            req = urllib.request.Request(url, data=b"{}", method="POST",
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200, resp.read().decode("utf-8", "replace")[:200]
        except Exception as exc:
            return False, str(exc)

    def test_completion(self, prompt: str = "Say hello in three words.") -> tuple[bool, str]:
        url = (f"http://{self.cfg.get('listen','127.0.0.1')}:"
               f"{int(self.cfg.get('port',13333))}/completion")
        payload = {"prompt": prompt, "stream": False, "n_predict": 64, "temperature": 0.2,
                   "cache_prompt": True, "id_slot": -1}
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=float(self.cfg.get("timeout", 120))) as resp:
                obj = json.loads(resp.read().decode("utf-8", "replace"))
            if "error" in obj:
                return False, json.dumps(obj["error"])[:400]
            return True, (obj.get("content") or "")[:400]
        except urllib.error.HTTPError as exc:
            return False, f"HTTP {exc.code}: {exc.read()[:300]!r}"
        except Exception as exc:
            return False, str(exc)

    def tail(self, n: int = 60) -> str:
        try:
            lines = SHIM_LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return "(no shim log yet)"
        return "\n".join(lines[-n:])


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.4)
        return s.connect_ex((host, port)) == 0


# --------------------------------------------------------------------------- #
# change-set planning
# --------------------------------------------------------------------------- #
def string_fits(current: str, wanted: str, field: str = "host") -> tuple[bool, str]:
    """In-place string rewrites must keep ceil4(len) identical (blob size is fixed)."""
    a, b = align4(len(current.encode())), align4(len(wanted.encode()))
    if a == b:
        return True, ""
    if a == 0:
        allowed = "only an empty string"
    else:
        allowed = f"{max(0, a - 3)}..{a} characters"
    return False, (f"The {field} box in the game's files has room for {allowed} (the shipped "
                   f"value {current!r} reserves {a} bytes), but {wanted!r} is "
                   f"{len(wanted.encode())} bytes. Remote mode's translator has no such limit "
                   f"and keeps {field}={current!r} for you.")


def split_endpoint(url: str, port_override: int = 0) -> tuple[str, int, str, bool]:
    """-> (host, port, path, tls). Keeps no scheme in the host (LlamaLib strips it anyway)."""
    from urllib.parse import urlsplit
    if not url:
        return "", port_override or 0, "", False
    if "//" not in url:
        url = "http://" + url
    parts = urlsplit(url)
    tls = parts.scheme == "https"
    host = parts.hostname or ""
    port = port_override or parts.port or (443 if tls else 80)
    path = parts.path.rstrip("/")
    return host, int(port), path, tls


def host_fits(current: str, wanted: str) -> tuple[bool, str]:
    return string_fits(current, wanted, "host")


def build_change_set(cfg: dict, res: ScanResult, overrides: dict | None = None,
                     group=None) -> tuple[dict, dict, list[str]]:
    """Turn the GUI config into {agent_fields}, {llm_fields}, notes.

    `group` (a vaudeville_cast group key, or None/cast.ALL_KEY for everybody) only
    records the character scope in the notes here; the actual narrowing of the
    character fields happens in plan_edits(agent_group=...).
    """
    agent_changes: dict = {}
    llm_changes: dict = {}
    notes: list[str] = []
    overrides = overrides or {}
    mode = normalize_mode(cfg.get("mode", MODE_OFF))

    if not res.agents:
        notes.append("no LLMAgent components found — nothing to patch")

    cur_host = res.agents[0].values.get("host", "localhost") if res.agents else "localhost"

    if mode == "off":
        agent_changes["remote"] = False
        notes.append(f"{MODE_LABELS[MODE_OFF]}: the cast uses the engine inside the game "
                     f"(nothing leaves the machine)")
    elif mode == "shim":
        agent_changes["remote"] = True
        wanted_host = cfg.get("agent_host") or cur_host or "localhost"
        ok, why = host_fits(cur_host, wanted_host)
        if ok:
            if wanted_host != cur_host:
                agent_changes["host"] = wanted_host
        else:
            notes.append("host left unchanged: " + why)
        agent_changes["port"] = int(cfg.get("shim_port", 13333))
        notes.append(f"{MODE_LABELS[MODE_SHIM]}: the cast talks to the built-in translator on "
                     f"{wanted_host}:{cfg.get('shim_port',13333)}, which forwards every line to "
                     f"{cfg.get('backend_url') or '<no service address set>'} "
                     f"(model {cfg.get('backend_model') or '-'})")
        if not cfg.get("backend_url"):
            notes.append("WARNING: no service address set — the translator will fail every "
                           "request until you give it one")
        if cfg.get("api_key") and not cfg.get("save_api_key"):
            notes.append("API key is held in memory/env only (not written to the config file)")
    elif mode == "direct":
        agent_changes["remote"] = True
        url = (cfg.get("backend_url") or "").strip()
        host, port, path, tls = split_endpoint(url, int(cfg.get("direct_port", 0) or 0))
        problems = []
        if not host:
            problems.append(f"BLOCKED ({MODE_LABELS[MODE_DIRECT]}): no server address was "
                            "given — fill in the address on the Remote page first, or choose "
                            "a different setup")
        if path:
            problems.append(
                f"BLOCKED ({MODE_LABELS[MODE_DIRECT]}): the game's built-in engine hands the "
                "address straight to its own web client and adds /completion, /health, "
                f"/apply-template … itself, so a path prefix like {path!r} is not supported. "
                "Give host[:port] only, or use Remote mode, whose translator holds the full "
                "address for you.")
        if tls:
            problems.append(
                f"BLOCKED ({MODE_LABELS[MODE_DIRECT]}): a padlocked https address needs the "
                "literal text 'https://<host>' inside the address field (the game's engine "
                "strips the scheme and switches to its secure client). That is 8 extra bytes "
                "and cannot fit the reserved 'localhost' space — use Remote mode, whose "
                "translator can do the padlock for you.")
        ok, why = host_fits(cur_host, host)
        if not ok:
            problems.append(f"BLOCKED ({MODE_LABELS[MODE_DIRECT]}): " + why)
        if problems:
            notes.extend(problems)
            notes.append(f"Switch to '{MODE_LABELS[MODE_SHIM]}' — it keeps the game pointing "
                         f"at this computer and holds the real address, model name and "
                         f"password itself.")
        else:
            agent_changes["host"] = host
            agent_changes["port"] = port
            notes.append(f"{MODE_LABELS[MODE_DIRECT]}: the cast talks straight to "
                         f"http://{host}:{port} (that server must speak the llama.cpp "
                         f"protocol: /health /apply-template /completion /tokenize)")
        if cfg.get("api_key"):
            cur_key = res.agents[0].values.get("APIKey", "") if res.agents else ""
            okk, whyk = string_fits(cur_key, cfg["api_key"], "APIKey")
            if okk:
                agent_changes["APIKey"] = cfg["api_key"]
            else:
                notes.append("API key not patched: " + whyk)
    else:
        notes.append(f"unknown mode {mode!r} — expected one of: "
                     + ", ".join(MODE_LABELS[m] for m in MODE_ORDER))

    group_key = normalize_group(group)
    if group_key is not None:
        label = group_label(group_key)
        if not group_known(group_key):
            notes.append(f"character group {label!r} is unknown (the character list is not "
                         f"available here) — these edits will go to every character")
        else:
            hits = group_blobs(res, group_key)
            files = sorted(group_files(group_key))
            notes.append(f"character scope: {label} — {len(hits)} of {len(res.agents)} "
                         f"characters" + (f" (settings live in {', '.join(files)})" if files
                                         else ""))
            if files and not hits:
                notes.append(f"WARNING: no character settings found for {label!r} in the last "
                             f"scan — re-scan, or choose 'All characters'")

    if cfg.get("expose_server"):
        llm_changes["remote"] = True
        llm_changes["port"] = int(cfg.get("server_port", 13333))
        notes.append(f"game exposes its loaded model as an OpenAI-compatible server "
                     f"on :{cfg.get('server_port',13333)}")
    elif res.llm and res.llm[0].values.get("remote"):
        llm_changes["remote"] = False
        notes.append("game server mode switched off")

    for key, value in overrides.items():
        if key in dict(AGENT_LAYOUT):
            agent_changes[key] = value
        elif key in dict(LLM_LAYOUT):
            llm_changes[key] = value
        else:
            notes.append(f"unknown parameter {key!r} ignored")

    if agent_changes.get("remote") and mode in ("shim", "direct"):
        notes.append("NOTE: the loading screen waits for the on-machine model to start, so "
                     "keep a small model file linked as the main model or the boot screen "
                     "will hang.")
    return agent_changes, llm_changes, notes


# --------------------------------------------------------------------------- #
# native proof-of-concept (drives the game's own libllamalib)
# --------------------------------------------------------------------------- #
def find_native_lib(game_dir: Path) -> Path | None:
    base = game_dir / DATA_DIR / STREAMING
    if IS_WINDOWS:
        patterns = ("LlamaLib-*/win-x64/native/libllamalib_win-x64_avx2.dll",
                    "LlamaLib-*/win-x64/native/libllamalib_win-x64_noavx.dll",
                    "LlamaLib-*/win*-x64/native/libllamalib_win-x64_*.dll")
    else:
        patterns = ("LlamaLib-*/linux-x64/native/libllamalib_linux-x64_avx2.so",
                    "LlamaLib-*/linux-x64/native/libllamalib_linux-x64_noavx.so",
                    "LlamaLib-*/linux-x64/native/libllamalib_linux-x64_*.so")
    for pattern in patterns:
        for p in sorted(base.glob(pattern)):
            if p.is_file():
                return p
    return None


def native_probe(lib_path: Path, host: str, port: int, prompt: str = "Hello!",
                 system: str = "You are a terse assistant.", timeout: float = 120.0) -> dict:
    """Exercise the shipped LlamaLib exactly like LLMUnity does in remote mode."""
    out = {"ok": False, "steps": [], "text": "", "error": ""}
    try:
        lib = ctypes.CDLL(str(lib_path))
    except OSError as exc:
        out["error"] = f"dlopen failed: {exc}"
        return out
    CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p)
    try:
        lib.LLM_Debug.argtypes = [ctypes.c_int]
        lib.LLM_Status_Code.restype = ctypes.c_int
        lib.LLM_Status_Message.restype = ctypes.c_char_p
        lib.LLMClient_Construct_Remote.restype = ctypes.c_void_p
        lib.LLMClient_Construct_Remote.argtypes = [ctypes.c_char_p, ctypes.c_int,
                                                   ctypes.c_char_p, ctypes.c_int]
        lib.LLMClient_Is_Server_Alive.restype = ctypes.c_bool
        lib.LLMClient_Is_Server_Alive.argtypes = [ctypes.c_void_p]
        lib.LLM_Set_Completion_Parameters.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.LLMAgent_Construct.restype = ctypes.c_void_p
        lib.LLMAgent_Construct.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
        lib.LLMAgent_Chat.restype = ctypes.c_char_p
        lib.LLMAgent_Chat.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_bool,
                                      CB, ctypes.c_bool, ctypes.c_bool]
    except AttributeError as exc:
        out["error"] = f"symbol missing: {exc}"
        return out

    def status():
        code = lib.LLM_Status_Code()
        msg = (lib.LLM_Status_Message() or b"").decode("utf-8", "replace")
        return code, msg

    lib.LLM_Debug(1)
    client = lib.LLMClient_Construct_Remote(host.encode(), int(port), b"", 2)
    out["steps"].append(f"LLMClient_Construct_Remote -> {hex(client or 0)} status={status()}")
    if not client:
        out["error"] = "remote client construction failed"
        return out
    alive = lib.LLMClient_Is_Server_Alive(client)
    out["steps"].append(f"LLMClient_Is_Server_Alive -> {alive}")
    params = {"temperature": 0.2, "top_k": 40, "top_p": 0.9, "min_p": 0.05, "n_predict": 64,
              "typical_p": 1.0, "repeat_penalty": 1.1, "repeat_last_n": 64,
              "presence_penalty": 0.0, "frequency_penalty": 0.0, "mirostat": 0,
              "mirostat_tau": 5.0, "mirostat_eta": 0.1, "seed": 0, "ignore_eos": False,
              "n_probs": 0, "cache_prompt": True}
    lib.LLM_Set_Completion_Parameters(client, json.dumps(params).encode())
    out["steps"].append(f"LLM_Set_Completion_Parameters -> status={status()}")
    agent = lib.LLMAgent_Construct(client, system.encode())
    out["steps"].append(f"LLMAgent_Construct -> {hex(agent or 0)} status={status()}")
    chunks = []
    t0 = time.time()
    result = lib.LLMAgent_Chat(agent, prompt.encode(), True, CB(chunks.append), False, False)
    out["steps"].append(f"LLMAgent_Chat -> {time.time()-t0:.2f}s status={status()} "
                        f"chunks={len(chunks)}")
    out["text"] = (result or b"").decode("utf-8", "replace")
    out["ok"] = bool(alive) and bool(out["text"])
    if not out["ok"]:
        out["error"] = out["error"] or "no text returned"
    return out


# --------------------------------------------------------------------------- #
# mock OpenAI backend (self-tests only)
# --------------------------------------------------------------------------- #
MOCK_REPLY = ["Hello", " Detective", " Martini", "!", " I", " am", " Biagio", " Ferrari", "."]


def make_mock_handler(counter: dict):
    class MockHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            counter["calls"] = counter.get("calls", 0) + 1
            counter.setdefault("requests", []).append(
                {"path": self.path, "keys": sorted(body),
                 "messages": body.get("messages"), "prompt": body.get("prompt"),
                 "auth": self.headers.get("Authorization"), "model": body.get("model")})
            chat = "messages" in body
            if body.get("stream"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Transfer-Encoding", "chunked")
                self.end_headers()

                def w(text):
                    payload = text.encode()
                    self.wfile.write(b"%x\r\n" % len(payload) + payload + b"\r\n")
                    self.wfile.flush()
                for piece in MOCK_REPLY:
                    obj = ({"choices": [{"delta": {"content": piece}}]} if chat
                           else {"choices": [{"text": piece}]})
                    w("data: " + json.dumps(obj) + "\n\n")
                w("data: [DONE]\n\n")
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
            else:
                text = "".join(MOCK_REPLY)
                obj = ({"choices": [{"message": {"role": "assistant", "content": text}}]}
                       if chat else {"choices": [{"text": text}]})
                payload = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
    return MockHandler


def free_port(host: str = "127.0.0.1") -> int:
    with socket.socket() as s:
        s.bind((host, 0))
        return s.getsockname()[1]


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #
# --- self-test helpers (used only by selftest() and the checks it runs) ----- #
# The three setup cards exactly as the project owner mandated them.  This is a
# deliberate SECOND copy of the text: vaudeville_help.py holds what the UI
# renders, this holds what was asked for, and the self-test fails the moment
# they drift — including a "helpful" fix of the upstream `communcation`
# spelling or of the two mandated double spaces.
MODE_CARD_COPY = {
    MODE_OFF: {
        "title": "Local Mode - Basic",
        "blurb": ("Vaudeville Configurator manages the AI model file for the game.  "
                  "Download new GGUF Models from Huggingface and let Vaudeville "
                  "Configurator manage everything else!"),
        "difficulty": "Low",
        "restrictions": ("GGUF File, must be related to or created from "
                         "`Meta-Llama-3-8B-Instruct` (not 3.1 or later) only!"),
    },
    MODE_DIRECT: {
        "title": "Local Mode - Advanced",
        "blurb": ("Vaudeville Configurator replaces the outdated and hardcoded "
                  "Llamalib built into the game, with the latest llama.cpp release "
                  "and allows you to select any GGUF model and tune all the "
                  "parameters!"),
        "difficulty": "Moderate",
        "restrictions": ("Must be a GGUF File compatible with latest llama.cpp, "
                         "must fit into local computer VRAM along with game!"),
    },
    MODE_SHIM: {
        "title": "Remote Mode - OpenAI API Compatible Endpoint",
        "blurb": ("Vaudeville Configurator intercepts the communcation with the "
                  "outdated and hardcoded Llamalib built into the game, and lets "
                  "you enter any Local or Remote OpenAI Compatible API Endpoint.  "
                  "vLLM / SGLang / Llama.cpp / ExLlamaV3 / Ollama / OpenAI / Custom"),
        "difficulty": "Moderate to Difficult depending on Self Hosting or Remote API.",
        "restrictions": "Only that the endpoint must support OpenAI API Chat Completions!",
    },
}
# the setting grids the GUI actually lays out, and the component each belongs to
GUI_FIELD_GRIDS = (("AGENT", CHARACTER_FIELDS), ("LLM", ENGINE_FIELDS))
# what every SETTING_HELP entry has to carry for a tooltip to be useful
HELP_ENTRY_KEYS = ("what", "does", "why_change", "why_not", "range", "tips")
# the asset files that hold characters (level4..level13 = the 10 locations,
# sharedassets18.assets = the Story Editor / Workshop roles)
EXPECTED_AGENT_FILES = [f"level{n}" for n in range(4, 14)] + ["sharedassets18.assets"]
# tab names the pre-rebuild GUI used, which must keep working
LEGACY_TAB_NAMES = {"game": "home", "start": "home", "welcome": "home",
                    "off": "basic", "models": "basic", "model": "basic",
                    "direct": "advanced", "shim": "remote", "openai": "remote",
                    "parameters": "characters", "params": "characters",
                    "cast": "characters", "restore": "backup", "backups": "backup"}


def _st_visible_tabs(mode) -> list:
    """The tab set the notebook shows: Home alone until a setup has been
    confirmed on the Home page, then Home + that setup + Characters + Backup."""
    if mode not in TAB_OF_MODE:
        return ["home"]
    return ["home", TAB_OF_MODE[mode], "characters", "backup"]


def _st_probe_argv(extra: list) -> list:
    """Command line for a child run of this same program (frozen-aware)."""
    if getattr(sys, "frozen", False):
        return [sys.executable, *extra]
    return [sys.executable, str(Path(__file__).resolve()), *extra]


def _st_gui_check(config: dict, tab: str = ""):
    """Build the real GUI through the existing withdrawn `--gui-check` path and
    report what the notebook actually holds.

    `--gui-check` withdraws the window before it is ever mapped, so nothing
    appears on anybody's desktop, and it skips the game scan.  The child runs in
    a throwaway XDG home, so the caller's own config is neither read nor
    written.

    Returns ``(probe, problem, skip)``:

    * ``probe``   ``(tabs, frames_built, visible_keys)`` when the child printed
      its banner, else ``None``.  ``visible_keys`` are tab keys in build order,
      so a caller can assert *which* tabs were revealed, not merely how many.
    * ``problem`` non-empty when the child ran and misbehaved (crashed, exited
      non-zero, or reworded its banner).  That is a FAILURE, never a skip.
    * ``skip``    non-empty only when this environment cannot run a GUI check at
      all (no tkinter, no child interpreter).  The caller may then skip the
      section out loud."""
    import tempfile
    try:
        import tkinter                                         # noqa: F401
    except Exception as exc:                                   # noqa: BLE001
        return None, "", f"tkinter is not importable here ({type(exc).__name__}: {exc})"
    try:
        with tempfile.TemporaryDirectory(prefix="vaudeville-selftest-ui-") as td:
            env = dict(os.environ)
            env["XDG_CONFIG_HOME"] = str(Path(td) / "config")
            env["XDG_DATA_HOME"] = str(Path(td) / "data")
            env["XDG_STATE_HOME"] = str(Path(td) / "state")
            for var in ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME"):
                Path(env[var]).mkdir(parents=True, exist_ok=True)
            cdir = Path(env["XDG_CONFIG_HOME"]) / APP_SLUG
            cdir.mkdir(parents=True, exist_ok=True)
            (cdir / "config.json").write_text(json.dumps(config), encoding="utf-8")
            argv = ["--gui-check"] + (["--tab", tab] if tab else [])
            proc = subprocess.run(_st_probe_argv(argv), env=env, capture_output=True,
                                  text=True, timeout=60)
            out = (proc.stdout or "") + "\n" + (proc.stderr or "")
            m = re.search(r"GUI built OK \(withdrawn\); tabs: (\d+) \(built: (\d+)", out)
            v = re.search(r"\| visible: ([^)]*)\)", out)
            if not m or not v:
                return (None,
                        f"the child printed no usable banner (exit {proc.returncode}); "
                        f"last output: {out.strip()[-240:] or '(empty)'}", "")
            probe = (int(m.group(1)), int(m.group(2)),
                     [k.strip() for k in v.group(1).split(",") if k.strip()])
            problem = "" if proc.returncode == 0 else f"the child exited {proc.returncode}"
            return probe, problem, ""
    except subprocess.TimeoutExpired:
        return None, "the --gui-check child timed out after 60s", ""
    except OSError as exc:
        return None, "", f"no child interpreter could be started here ({exc})"
    except Exception as exc:                                   # noqa: BLE001
        return None, f"the --gui-check child broke: {type(exc).__name__}: {exc}", ""


def _st_deck_exemption():
    """Does the Basic-mode model panel leave the shipped Steam Deck file alone?

    The rule lives inside gui_main's refresh_models(), which needs a Tk root, so
    the shipped condition is lifted out of the source and evaluated here against
    three slot/target combinations.  Returns (ok, detail), or None when the
    source cannot be read (a frozen single-file build ships no .py)."""
    import inspect
    from types import SimpleNamespace
    try:
        src = inspect.getsource(gui_main)
    except Exception:                                          # noqa: BLE001
        return None
    # Lift BOTH halves of the shipped rule: the outer "would this panel warn at
    # all" guard and the inner Steam Deck exemption.  Re-implementing either by
    # hand here would let the production copy drift unnoticed.
    m = re.search(r'if\s+(panel\["restrict"\] and [\s\S]*?):[ \t]*\n'
                  r'[ \t]*if\s+(slot\.name == DECK_MODEL_NAME[\s\S]*?):[ \t]*\n[ \t]*pass',
                  src)
    if not m:
        return (False, "no Basic-mode model warning guard left in gui_main — if "
                       "refresh_models() was refactored, update _st_deck_exemption() "
                       "in the self-test")
    outer = " ".join(re.sub(r"\\\s+", " ", m.group(1)).split())
    exempt_cond = " ".join(re.sub(r"\\\s+", " ", m.group(2)).split())

    def warns(slot_name: str, target: str):
        """(would the restricted Basic panel flag this slot?, evaluation error)."""
        ns = {"slot": SimpleNamespace(name=slot_name), "st": {"target": target},
              "panel": {"restrict": True}, "Path": Path,
              "DECK_MODEL_NAME": DECK_MODEL_NAME,
              "is_llama3_8b_family": is_llama3_8b_family}
        try:
            flagged = bool(eval(outer, {"__builtins__": {}}, ns))       # noqa: S307
            exempt = (bool(eval(exempt_cond, {"__builtins__": {}}, ns))  # noqa: S307
                      if flagged else False)
        except Exception as exc:                                         # noqa: BLE001
            return True, (f"the lifted condition could not be evaluated here: "
                          f"{type(exc).__name__}: {exc}")
        return flagged and not exempt, ""

    cases = [(DECK_MODEL_NAME, f"/models/{DECK_MODEL_NAME}", False,
              "the shipped deck file must stay unflagged"),
             (PRIMARY_MODEL_NAME, f"/models/{PRIMARY_MODEL_NAME}", False,
              f"an in-family {LLAMA3_8B_FAMILY} file in the main slot must not warn"),
             (DECK_MODEL_NAME, "/models/Mistral-7B-Instruct-v0.3.gguf", True,
              "a different file linked into the deck slot must still warn"),
             (PRIMARY_MODEL_NAME, "/models/Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf", True,
              "Llama-3.1 in the main slot must still warn")]
    bad = []
    for name, tgt, want, why in cases:
        got, err = warns(name, tgt)
        if err:
            bad.append(err)
        elif got != want:
            bad.append(why)
    return (not bad, ("; ".join(dict.fromkeys(bad)) if bad
                      else f"{len(cases)} slot/target cases against the shipped guard "
                           f"`{outer}` / `{exempt_cond}`"))


def _st_agent_file_counts(res) -> dict:
    """Agent blobs per asset file name, exactly as the scanner saw them."""
    counts: dict = {}
    for blob in getattr(res, "agents", []):
        name = getattr(getattr(blob, "path", None), "name", "") or ""
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


def selftest(game_dir: Path | None, live: bool, verbose: bool = True,
             ci: bool = False) -> int:
    results: list[tuple[bool, str]] = []

    def check(ok: bool, msg: str):
        results.append((bool(ok), msg))
        if verbose:
            print(("  PASS  " if ok else "  FAIL  ") + msg)

    print(f"== {APP_NAME} {APP_VERSION} self-test (python {sys.version.split()[0]}) ==")

    print("[0] inference modes")
    check([MODE_LABELS[m] for m in MODE_ORDER] ==
          ["Local Mode - Basic", "Local Mode - Advanced",
           "Remote Mode - OpenAI API Compatible Endpoint"],
          "labels + order: " + " | ".join(MODE_LABELS[m] for m in MODE_ORDER))
    check(all(normalize_mode(a) == k for a, k in MODE_ALIASES.items()),
          f"all {len(MODE_ALIASES)} CLI/config aliases normalize to a mode key")
    check(normalize_mode("Remote") == MODE_SHIM and normalize_mode("BASIC") == MODE_OFF
          and normalize_mode("nope") == MODE_OFF and mode_label("shim") == MODE_LABELS[MODE_SHIM],
          "normalize_mode()/mode_label() fallbacks")

    print("[0b] platform helpers (linux + windows code paths)")
    c, d, s = _user_dirs()
    check(all(p.is_absolute() for p in (c, d, s)) and len({c, d, s}) == 3,
          f"user dirs: {c} | {d} | {s}")
    sample = ('"Image Name","PID","Session Name","Session#","Mem Usage"\r\n'
              '"Vaudeville.exe","4321","Console","1","1,234 K"\r\n'
              '"steam.exe","99","Console","1","500 K"\r\n')
    check(parse_tasklist_csv(sample, "Vaudeville.exe") == [4321]
          and parse_tasklist_csv(sample, "steam.exe") == [99]
          and parse_tasklist_csv(sample, "") == [4321, 99]
          and parse_tasklist_csv(sample, "nope.exe") == [],
          "tasklist CSV parser (windows process detection)")
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        fake = Path(td) / "Vaudeville"
        (fake / DATA_DIR / STREAMING).mkdir(parents=True)
        (fake / f"{GAME_DIR_NAME}.exe").write_bytes(b"MZ")
        (fake / DATA_DIR / "app.info").write_text("x")
        check(is_game_dir(fake), "is_game_dir accepts a windows-style install (.exe)")
        tgt = Path(td) / "Model.gguf"
        tgt.write_bytes(b"gguf")
        lnk = Path(td) / "Link.gguf"
        kind = _make_link(lnk, tgt)
        check(lnk.exists() and lnk.read_bytes() == b"gguf" and kind in ("symlink", "hardlink"),
              f"_make_link works here via {kind}")
    check(isinstance(game_is_running(Path.home()), list),
          "game_is_running returns a list on this platform")
    global IS_WINDOWS
    real_win, IS_WINDOWS = IS_WINDOWS, True
    try:
        wk = _detached_kwargs()
        wd = _windows_steam_dirs()
    finally:
        IS_WINDOWS = real_win
    check(wk == {} or set(wk) == {"creationflags"}, f"windows detach kwargs: {wk}")
    check(isinstance(wd, list), f"windows steam-dir probe safe off-windows: {wd}")
    vfile = Path(__file__).resolve().parent / "VERSION"
    if getattr(sys, "frozen", False):
        check(True, "skipped — a frozen single-file build carries no VERSION file beside it")
    else:
        onfile = vfile.read_text().strip() if vfile.is_file() else "MISSING"
        check(vfile.is_file() and onfile == APP_VERSION,
              f"VERSION file ({onfile}) matches APP_VERSION ({APP_VERSION})")

    print("[0c] sibling data modules (vaudeville_cast / vaudeville_help)")
    frozen = bool(getattr(sys, "frozen", False))
    where = "frozen single-file build" if frozen else "source run"
    check(cast is not None, f"vaudeville_cast imported and in use ({where})")
    check(helpmod is not None, f"vaudeville_help imported and in use ({where})")
    if frozen:
        import importlib.util
        specs = {}
        for name in ("vaudeville_cast", "vaudeville_help"):
            try:
                specs[name] = importlib.util.find_spec(name) is not None
            except Exception:                                  # noqa: BLE001
                specs[name] = False
        check(all(specs.values()),
              "both modules are findable by the import machinery inside this frozen "
              "build (a fresh-interpreter import needs a Python a single-file build "
              f"does not ship) ({specs})")
    else:
        imports = {}
        here = str(Path(__file__).resolve().parent)
        cenv = dict(os.environ,
                    PYTHONPATH=here + os.pathsep + os.environ.get("PYTHONPATH", ""))
        for name in ("vaudeville_cast", "vaudeville_help"):
            proc = subprocess.run([sys.executable, "-c", f"import {name}"], env=cenv,
                                  capture_output=True, text=True, timeout=60)
            imports[name] = proc.returncode == 0
        check(all(imports.values()),
              "both modules really import in a fresh interpreter running this same "
              "file, so a bundle that dropped or broke them fails here instead of "
              f"degrading silently ({imports})")
    cast_names = ("ALL_KEY", "ALL_LABEL", "CAST_GROUPS", "GROUP_OF_FILE", "CANONICAL_FILES",
                  "group_of", "LEVELS_ARE_DIFFICULTY", "DIFFICULTY_SCALE",
                  "SYSTEM_PROMPT_EDITABLE", "PERSONA_NOTE", "FRIENDLY_GROUP_INTRO", "EVIDENCE")
    help_names = ("MODE_CARDS", "TERMS", "GROUP_HELP", "SETTING_HELP", "MODEL_HELP",
                  "SHIM_HELP", "CREDIT", "GITHUB_URL")
    gaps = [n for n in cast_names if cast is None or not hasattr(cast, n)]
    check(not gaps, f"vaudeville_cast exports all {len(cast_names)} contract names"
                    + (f" — missing: {gaps}" if gaps else ""))
    gaps = [n for n in help_names if helpmod is None or not hasattr(helpmod, n)]
    check(not gaps, f"vaudeville_help exports all {len(help_names)} contract names"
                    + (f" — missing: {gaps}" if gaps else ""))
    raw_cards = getattr(helpmod, "MODE_CARDS", None) or {}
    check(isinstance(raw_cards, dict) and set(raw_cards) == set(MODE_ORDER)
          and all(isinstance(raw_cards.get(k), dict)
                  and all(str(raw_cards[k].get(f) or "").strip()
                          for f in ("title", "blurb", "difficulty", "restrictions"))
                  for k in MODE_ORDER),
          f"MODE_CARDS keys are exactly {sorted(set(MODE_ORDER))} and every card carries "
          "title/blurb/difficulty/restrictions")
    raw_help = getattr(helpmod, "SETTING_HELP", None) or {}
    check(isinstance(raw_help, dict) and len(raw_help) >= 40,
          f"SETTING_HELP is non-empty ({len(raw_help)} entries)")
    check(len(TERMS) == len(_TERM_RES) > 0 and T("LLMAgent") == "character agents",
          f"TERMS compiled ({len(_TERM_RES)} rules) and applied to this file's own strings")

    print("[0d] the three setup cards are byte-identical to the mandated copy")
    check(set(MODE_CARDS) == set(MODE_ORDER)
          and all(set(MODE_CARDS[k]) >= {"key", "title", "blurb", "difficulty", "restrictions"}
                  for k in MODE_ORDER),
          f"the GUI renders {len(MODE_CARDS)} cards, each with "
          "key/title/blurb/difficulty/restrictions")
    for key in MODE_ORDER:
        card, want = MODE_CARDS[key], MODE_CARD_COPY[key]
        diff = [f for f in ("title", "blurb", "difficulty", "restrictions")
                if str(card.get(f)) != want[f]]
        check(not diff, f"{key!r} card matches the mandated copy verbatim"
                        + (f" — differs in: {diff}" if diff else ""))
    check(all(str(MODE_CARDS[k].get("title")) == MODE_LABELS[k] for k in MODE_ORDER),
          "card titles are exactly the three mode labels")
    shim_blurb = str(MODE_CARDS[MODE_SHIM].get("blurb"))
    off_blurb = str(MODE_CARDS[MODE_OFF].get("blurb"))
    check(shim_blurb.count("communcation") == 1 and "communication" not in shim_blurb,
          "the upstream `communcation` spelling is preserved — do not 'fix' it")
    check("game.  Download" in off_blurb and "Endpoint.  vLLM" in shim_blurb,
          "both mandated double spaces survived (game.<2sp>Download, Endpoint.<2sp>vLLM)")

    print("[0e] help covers every field the GUI lays out")
    agent_names = [n for n, _ in AGENT_LAYOUT]
    llm_names = [n for n, _ in LLM_LAYOUT]
    union = set(agent_names) | set(llm_names)
    check(set(raw_help) == union,
          f"SETTING_HELP keys == AGENT_LAYOUT | LLM_LAYOUT names ({len(union)}); "
          f"missing={sorted(union - set(raw_help))} extra={sorted(set(raw_help) - union)}")
    check(set(SETTING_HELP) == union,
          f"the merged SETTING_HELP the GUI reads has the same {len(union)} keys")
    thin = {n: [k for k in HELP_ENTRY_KEYS if not raw_help.get(n, {}).get(k)]
            for n in raw_help
            if any(not raw_help[n].get(k) for k in HELP_ENTRY_KEYS)}
    check(not thin, "every entry has all of " + "/".join(HELP_ENTRY_KEYS)
                    + (f" — thin: {thin}" if thin else ""))
    few = sorted(n for n in raw_help if len(raw_help[n].get("tips") or []) < 2)
    check(not few, "every entry carries at least 2 tips"
                   + (f" — 0 or 1 tip: {few}" if few else ""))
    laid_out = [n for _kind, fields in GUI_FIELD_GRIDS for n in fields]
    gaps = [n for n in laid_out if not str(setting_tip(n)).strip()]
    check(set(laid_out) <= union and not gaps,
          f"all {len(laid_out)} grid fields "
          f"({' + '.join(f'{len(f)} {k.lower()}' for k, f in GUI_FIELD_GRIDS)}) have a help "
          f"entry and render a tooltip" + (f" — gaps: {gaps}" if gaps else ""))
    check(set(CHARACTER_FIELDS) <= set(agent_names) and set(ENGINE_FIELDS) <= set(llm_names)
          and set(ENDPOINT_FIELDS) <= set(agent_names),
          "every laid-out field is a real serialized field of the component it edits")
    check(set(STOCK_CHARACTER_VALUES) == set(CHARACTER_FIELDS),
          f"reset-to-defaults holds a shipped value for exactly the "
          f"{len(CHARACTER_FIELDS)} character grid fields")

    print("[0f] cast data and group-scoped patching")
    if cast is None:
        check(False, "vaudeville_cast did not import, so none of the cast checks ran")
    else:
        from types import SimpleNamespace
        groups = cast_groups()
        keys = [str(g.get("key")) for g in groups]
        check(len(groups) == len(set(keys)) == 11,
              f"{len(groups)} cast groups, unique keys: {', '.join(keys)}")
        check(all_group_key() not in keys
              and all_group_key() == str(getattr(cast, "ALL_KEY", "")),
              f"the {all_group_key()!r} sentinel is a dropdown value, not a group")
        check(all(str(g.get("label", "")).strip() and g.get("files") and g.get("characters")
                  and str(g.get("note", "")).strip() for g in groups),
              "every group has a label, a non-empty file list, characters and a note")
        total = sum(len(g.get("characters") or []) for g in groups)
        check(total == 26, f"the groups' character lists sum to 26 (got {total})")
        canon = list(getattr(cast, "CANONICAL_FILES", []) or [])
        check(canon == [f for g in groups for f in g["files"]]
              and len(set(canon)) == len(canon) == len(EXPECTED_AGENT_FILES),
              f"CANONICAL_FILES is exactly the flattened group file lists "
              f"({len(canon)} unique) — the list a patch must be scoped to")
        table = dict(getattr(cast, "GROUP_OF_FILE", {}) or {})
        check(all(f in table for f in canon) and set(table.values()) <= set(keys)
              and all_group_key() not in table.values(),
              f"GROUP_OF_FILE maps all {len(canon)} real files onto group keys "
              f"({len(table)} entries, the rest being lookup aliases)")
        alias_bad = [f"level{n}" for n in range(4, 14)
                     if cast.group_of(f"level{n}.assets") != cast.group_of(f"level{n}")]
        check(not alias_bad and cast.group_of("sharedassets18.assets") == "workshop"
              and cast.group_of("level3") is None,
              "group_of() is alias-tolerant and returns None for an unknown file"
              + (f" — broken aliases: {alias_bad}" if alias_bad else ""))
        scope_bad = [k for k in keys
                     if group_files(k) != {str(f) for g in groups if str(g["key"]) == k
                                           for f in g["files"]}]
        check(not scope_bad,
              "group_files() — what a patch is scoped to — comes from CAST_GROUPS[*]['files']"
              + (f" — mismatched: {scope_bad}" if scope_bad else ""))
        check(group_files(None) == set() and group_files(all_group_key()) == set()
              and all(normalize_group(a) is None
                      for a in (None, "", "all", "ALL", " everyone ", "everybody")),
              "'every character' resolves to no file list, so it can never scope a patch "
              "down to nothing")
        choices = group_choices()
        check(choices[0] == (all_group_key(), "All characters")
              and [k for k, _ in choices[1:]] == keys
              and all(normalize_group(k) == k for k, _ in choices[1:])
              and group_label(all_group_key()) == "All characters",
              f"the dropdown lists 'All characters' first, then the {len(keys)} groups in game "
              "order, and every entry maps back to a real group key")
        check(all(group_known(k) for k in keys) and group_known(None)
              and not group_known("not_a_group"),
              "group_known() accepts the 11 real keys and 'all', rejects anything else")
        check(len(group_characters(None)) == 26
              and sum(len(group_characters(k)) for k in keys) == 26,
              "group_characters() names 26 characters for 'all' and 26 across the groups")
        entry_keys = {k for g in groups for k in g}
        check(entry_keys == {"key", "label", "files", "characters", "note"},
              f"groups carry display data only {sorted(entry_keys)} — no per-character blob "
              "index exists, so patching has to stay group-scoped")
        label_bad = [g["key"] for g in groups
                     if not re.search(r"\(%d character" % len(g["characters"]),
                                      str(g["label"]))]
        check(not label_bad, "each group's label states its own character count"
                             + (f" — wrong: {label_bad}" if label_bad else ""))
        check(blob_in_group(SimpleNamespace(path=Path("level4")), "police_station")
              and not blob_in_group(SimpleNamespace(path=Path("level4")), "morgue")
              and blob_in_group(SimpleNamespace(path=Path("level4.assets")), "police_station")
              and blob_in_group(SimpleNamespace(path=Path("sharedassets18.assets")), "workshop")
              and blob_in_group(SimpleNamespace(path=Path("level4")), None)
              and blob_in_group(SimpleNamespace(path=Path("level4")), "not_a_group"),
              "blob_in_group() is file-scoped, alias-tolerant and fails safe (True) for "
              "'all' and for an unknown group")

    print("[0g] refuted theories stay refuted")
    check(getattr(cast, "LEVELS_ARE_DIFFICULTY", None) is False,
          "LEVELS_ARE_DIFFICULTY is False — level4..level13 are locations, not difficulty 0..25")
    check(getattr(cast, "DIFFICULTY_SCALE", "missing") is None,
          "DIFFICULTY_SCALE is None — the game has no difficulty scale to offer")
    check(getattr(cast, "SYSTEM_PROMPT_EDITABLE", None) is False,
          "SYSTEM_PROMPT_EDITABLE is False — CharacterPrompt.Awake overwrites the agent "
          "system prompt at load time")
    check("systemPrompt" in INERT_FIELDS and "advancedOptions" in INERT_FIELDS,
          f"the GUI lists {sorted(INERT_FIELDS)} as not editable")
    check(bool(str(getattr(cast, "PERSONA_NOTE", "")).strip()) and bool(evidence_text().strip()),
          "the cast module says why personas are not editable, and backs it with evidence")

    print("[0h] Basic-mode model family check")
    check(is_llama3_8b_family(PRIMARY_MODEL_NAME),
          f"the shipped main model {PRIMARY_MODEL_NAME} IS {LLAMA3_8B_FAMILY} family")
    check(not is_llama3_8b_family(DECK_MODEL_NAME),
          f"the shipped Steam Deck fallback {DECK_MODEL_NAME} is NOT that family, so it has "
          "to be exempted rather than flagged")
    check(not is_llama3_8b_family("Meta-Llama-3.1-8B-Instruct-Q4_K_M.gguf")
          and not is_llama3_8b_family("Meta-Llama-3.2-3B-Instruct-Q4_K_M.gguf")
          and is_llama3_8b_family("meta-llama-3-8b-instruct-q5_k_m.gguf"),
          "the family check rejects Llama-3.1/3.2 and is case/punctuation tolerant for 3-8B")
    check([s.name for s in model_slots()] == [PRIMARY_MODEL_NAME, DECK_MODEL_NAME],
          "the two model slots are PRIMARY then DECK, in that order (anchored to real "
          "files in [3] when an install is present)")
    exempt = None if frozen else _st_deck_exemption()
    if exempt is None:
        check(True, "skipped — " + ("a frozen build ships no source to lift the Basic-mode "
                                    "warning rule from" if frozen else
                                    "the Basic-mode warning rule could not be read out of "
                                    "gui_main") + "; --gui-check builds that panel instead")
    else:
        check(exempt[0], f"the Basic-mode panel leaves the shipped deck file alone — "
                         f"{exempt[1]}")

    print("[0i] tabs, aliases and branding")
    check(TAB_ORDER == ("home", "basic", "advanced", "remote", "characters", "backup"),
          "tab order: " + " | ".join(TAB_ORDER))
    check(set(TAB_ALIASES.values()) == set(TAB_ORDER)
          and all(normalize_tab(a) == k for a, k in TAB_ALIASES.items()),
          f"all {len(TAB_ALIASES)} tab aliases normalize onto the {len(TAB_ORDER)} real tabs")
    check(all(normalize_tab(a) == k for a, k in LEGACY_TAB_NAMES.items())
          and normalize_tab("nope") == "" and normalize_tab(None) == "",
          f"the {len(LEGACY_TAB_NAMES)} pre-rebuild tab names still land on the new tabs")
    check(len(LEGACY_TAB_NAMES) == 14 and len(GUI_FIELD_GRIDS) == 2
          and len(MODE_CARD_COPY) == 3 and len(EXPECTED_AGENT_FILES) == 11,
          "this suite's own fixtures are still full size (14 legacy tab names, 2 field "
          "grids, 3 mandated cards, 11 character files) — shrinking one would quietly "
          "shrink what is verified")
    check(TAB_OF_MODE == {MODE_OFF: "basic", MODE_DIRECT: "advanced", MODE_SHIM: "remote"}
          and MODE_OF_TAB == {v: k for k, v in TAB_OF_MODE.items()},
          "each mode owns exactly one tab: "
          + ", ".join(f"{m}->{TAB_OF_MODE[m]}" for m in MODE_ORDER))
    check(_st_visible_tabs(None) == ["home"]
          and all(_st_visible_tabs(m) == ["home", TAB_OF_MODE[m], "characters", "backup"]
                  for m in MODE_ORDER),
          "no setup confirmed -> Home only; confirmed -> Home + that setup + Characters + Backup")
    check(len({tab_title(k) for k in TAB_ORDER}) == len(TAB_ORDER)
          and all(tab_title(k).strip() for k in TAB_ORDER)
          and all(tab_title(TAB_OF_MODE[m]).strip() == MODE_LABELS[m].strip()
                  for m in MODE_ORDER),
          "every tab has its own non-empty title and the mode tabs use the mode label")
    check(CREDIT == "MidnightPhreaker + Qwen", f"CREDIT = {CREDIT!r}")
    check(GITHUB_URL == "https://github.com/MidnightPhreaker/Vaudeville-Configurator",
          f"GITHUB_URL = {GITHUB_URL!r}")
    check(CREDIT == str(getattr(helpmod, "CREDIT", ""))
          and GITHUB_URL == str(getattr(helpmod, "GITHUB_URL", "")),
          "the credit and project link come from vaudeville_help, not from the local fallback")

    print("[1] ThinkStripper")
    cases = [(["<think>secret</think>Hi there!"], "Hi there!"),
             (["<thi", "nk>sec", "ret</thi", "nk>Hi"], "Hi"),
             (["plain text"], "plain text"),
             (["a<think>b</think>c<think>d</think>e"], "ace"),
             (["<think>unterminated"], ""),
             (["keep < this"], "keep < this"),
             (["keep <"], "keep <"),
             (["x</think>y"], "x</think>y"),
             (["<think>a", "b</thi", "nk>tail"], "tail"),
             (["no reasoning"], "no reasoning")]
    for chunks, want in cases:
        st = ThinkStripper(True)
        got = "".join(st.feed(c) for c in chunks) + st.flush()
        check(got == want, f"strip {chunks} -> {got!r}")

    print("[2] Steam / game detection")
    found = find_game_dirs()
    check(bool(found) or ci,
          f"found {friendly_count(len(found), 'Vaudeville install', 'Vaudeville installs')}"
          + (": " + "; ".join(f"{p}  ({src})" for p, src in found) if found else
             (" — acceptable on a CI runner, which skips the game-dependent sections"
              if ci else " — expected a Steam install on this machine")))
    gd = game_dir or (found[0][0] if found else None)
    if gd is None:
        if not ci:
            print("== cannot continue without a game directory ==")
            return 1

    if gd is None:
        print("[3] skipped (CI mode: no game install on this runner)")
        print("[4] skipped (CI mode: no game install on this runner)")
        print("[4b] cast coverage by name (no install here to scan)")
        gof = cast.group_of if cast is not None else (lambda _n: None)
        uncovered = [n for n in EXPECTED_AGENT_FILES if gof(n) is None]
        check(not uncovered,
              f"GROUP_OF_FILE covers level4..level13 + sharedassets18.assets by name "
              f"({len(EXPECTED_AGENT_FILES)} files)"
              + (f" — missing: {uncovered}" if uncovered else ""))
        declared = {f: len(g["characters"]) for g in cast_groups() for f in g["files"]}
        check(sorted(declared) == sorted(EXPECTED_AGENT_FILES)
              and sum(declared.values()) == 26,
              f"CAST_GROUPS[*]['files'] is exactly those {len(EXPECTED_AGENT_FILES)} files and "
              f"their character counts sum to {sum(declared.values())}")
        alias_bad = [f for f in EXPECTED_AGENT_FILES
                     if not f.endswith(".assets") and gof(f + ".assets") != gof(f)]
        check(not alias_bad,
              "every scene file resolves to the same group with and without the .assets alias"
              + (f" — broken: {alias_bad}" if alias_bad else ""))
    if gd is not None:
        print(f"[3] component scan of {gd}")
        t0 = time.time()
        res = scan_game(gd)
        check(len(res.llm) == 1, f"exactly one LLMUnity.LLM component ({len(res.llm)})")
        check(len(res.agents) > 0, f"{len(res.agents)} LLMUnity.LLMAgent components")
        check(all(b.leftover >= 0 for b in res.blobs), "all blobs decoded without over-read")
        observed = {Path(st["target"]).name
                    for st in (slot_status(gd, s) for s in model_slots()) if st["target"]}
        check(not observed or observed <= {PRIMARY_MODEL_NAME, DECK_MODEL_NAME},
              "the model files this install really points at are the two names the build "
              "hard-codes" + (f": {', '.join(sorted(observed))}" if observed else
                              " (no model file is linked here, so there is nothing to "
                              "anchor to)"))
        if res.llm:
            m = res.llm[0].values["model"]
            check(m.lower().endswith(".gguf"), f"LLM.model = {m!r}")
            check(res.llm[0].values["contextSize"] > 0, f"contextSize={res.llm[0].values['contextSize']}")
        if res.agents:
            a = res.agents[0].values
            check(0.0 <= a["temperature"] <= 2.0, f"temperature={a['temperature']}")
            check(a["host"] != "", f"host={a['host']!r} port={a['port']}")
            check(a["slot"] in (-1, -2) or a["slot"] >= 0, f"slot={a['slot']}")
        print(f"      scan time {time.time()-t0:.1f}s; {res.summary()}")

        print("[4] dry-run change set (no writes)")
        cfg = {"mode": "shim", "shim_port": 13333, "backend_url": "http://127.0.0.1:9999/v1",
               "backend_model": "test-model", "api_key": "", "expose_server": False}
        ag, llm, notes = build_change_set(cfg, res, {"temperature": 0.77, "numPredict": 123})
        edits, warnings = plan_edits(res, ag, llm)
        check(len(edits) == len(res.agents) * 3 or len(edits) > 0,
              f"{len(edits)} byte edits planned for {len(res.agents)} agents")
        check(all(len(e.new) == len(e.old) for e in edits), "every edit is size-preserving")
        for w in warnings:
            check(False, f"planning warning: {w}")

        print("[4b] cast coverage of this install")
        gof = cast.group_of if cast is not None else (lambda _n: None)
        counts = _st_agent_file_counts(res)
        uncovered = sorted(n for n in counts if gof(n) is None)
        check(bool(counts) and not uncovered,
              f"GROUP_OF_FILE covers all {len(counts)} agent-bearing files the scanner found "
              f"({sum(counts.values())} agents)"
              + (f" — uncovered: {uncovered}" if uncovered else ""))
        declared = {f: len(g["characters"]) for g in cast_groups() for f in g["files"]}
        wrong = {n: (c, declared.get(n)) for n, c in counts.items() if declared.get(n) != c}
        check(set(counts) == set(declared) and not wrong,
              "agents found per file match each group's character count "
              f"({', '.join(f'{n}={c}' for n, c in sorted(counts.items()))})"
              + (f" — mismatched (found, declared): {wrong}" if wrong else ""))
        check(sorted(counts) == sorted(EXPECTED_AGENT_FILES),
              "the install's agent-bearing files are exactly level4..level13 + "
              "sharedassets18.assets")

    print("[5] shim protocol (in-process)")
    counter: dict = {"calls": 0}
    mock_port, shim_port = free_port(), free_port()
    mock = ThreadingHTTPServer(("127.0.0.1", mock_port), make_mock_handler(counter))
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    shim_cfg = {"listen": "127.0.0.1", "port": shim_port, "mode": "chat",
                "backend_url": f"http://127.0.0.1:{mock_port}/v1", "backend_model": "mock-model",
                "api_key": "sk-selftest", "strip_think": True, "verbose": False}
    backend = Backend(shim_cfg["backend_url"], shim_cfg["api_key"], shim_cfg["backend_model"])
    shim = ThreadingHTTPServer(("127.0.0.1", shim_port),
                               make_shim_handler(shim_cfg, backend, ShimState(), lambda m: None))
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    time.sleep(0.2)
    try:
        def post(path, payload):
            req = urllib.request.Request(f"http://127.0.0.1:{shim_port}{path}",
                                         data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"},
                                         method="POST")
            with urllib.request.urlopen(req, timeout=20) as r:
                return r.status, r.read().decode()
        st, body = post("/health", {})
        check(st == 200, f"POST /health -> {st}")
        st, body = post("/apply-template", {"messages": [{"role": "system", "content": "S"},
                                                         {"role": "user", "content": "U"}]})
        check(st == 200 and json.loads(body).get("prompt", "").startswith(MARKER),
              f"POST /apply-template -> cached messages marker")
        st, body = post("/completion", {"prompt": json.loads(body)["prompt"], "stream": False,
                                        "temperature": 0.2, "n_predict": -1, "id_slot": -1})
        content = json.loads(body).get("content", "")
        check(content == "".join(MOCK_REPLY), f"POST /completion (non-stream) -> {content!r}")
        reqs = counter.get("requests", [])
        chat_req = [r for r in reqs if r["path"] == "/v1/chat/completions"]
        check(bool(chat_req), "backend received /v1/chat/completions")
        if chat_req:
            r = chat_req[-1]
            check(r["auth"] == "Bearer sk-selftest", f"backend saw Authorization: {r['auth']!r}")
            check(r["model"] == "mock-model", f"backend saw model={r['model']!r}")
            roles = [m["role"] for m in (r["messages"] or [])]
            check(roles == ["system", "user"], f"messages preserved: {roles}")
        # streaming
        req = urllib.request.Request(f"http://127.0.0.1:{shim_port}/completion",
                                     data=json.dumps({"prompt": MARKER + "1" + MARKER_END,
                                                      "stream": True}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Accept": "text/event-stream"}, method="POST")
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode()
        check("[DONE]" not in raw, "stream does NOT emit 'data: [DONE]' (would break LlamaLib)")
        check(raw.count("data: ") >= len(MOCK_REPLY), f"stream emitted {raw.count('data: ')} events")
        acc = "".join(json.loads(l[5:].strip()).get("content", "")
                      for l in raw.splitlines() if l.startswith("data: "))
        check(acc == "".join(MOCK_REPLY), f"stream reassembles to {acc!r}")
        st, body = post("/tokenize", {"content": "hello world"})
        check(st == 200 and "tokens" in json.loads(body), "POST /tokenize -> tokens[]")
        st, body = post("/embeddings", {"content": "hello"})
        check(st == 200 and isinstance(json.loads(body), list), "POST /embeddings -> [embedding]")
        # think stripping end to end
        strip_cfg = dict(shim_cfg)
        check(ThinkStripper(True).feed("<think>x</think>y") == "y", "reasoning stripped")
    finally:
        shim.shutdown(); shim.server_close(); mock.shutdown(); mock.server_close()

    print("[6] shim subprocess management")
    sp = ShimProcess({"listen": "127.0.0.1", "port": shim_port, "mode": "chat",
                      "backend_url": f"http://127.0.0.1:{mock_port}/v1",
                      "backend_model": "mock-model", "api_key": "sk-subprocess"})
    mock2 = ThreadingHTTPServer(("127.0.0.1", mock_port), make_mock_handler(counter))
    threading.Thread(target=mock2.serve_forever, daemon=True).start()
    try:
        ok, msg = sp.start()
        check(ok, f"start: {msg}")
        ok, detail = sp.health()
        check(ok, f"health: {detail[:80]}")
        ok, text = sp.test_completion("hi")
        check(ok and text == "".join(MOCK_REPLY), f"test_completion -> {text!r}")
        check(sp.stop().startswith("shim stopped"), "stop")
        check(not port_in_use(shim_port), "port released after stop")
    finally:
        try:
            sp.stop()
        except Exception:
            pass
        mock2.shutdown(); mock2.server_close()

    if live and gd is None:
        print("[7] skipped (CI mode: no game install on this runner)")
    if live and gd is not None:
        print("[7] native proof: game's own libllamalib -> shim -> mock backend")
        lib = find_native_lib(gd)
        if lib is None:
            check(False, f"no libllamalib found under {gd}")
        else:
            mock3 = ThreadingHTTPServer(("127.0.0.1", mock_port), make_mock_handler(counter))
            threading.Thread(target=mock3.serve_forever, daemon=True).start()
            shim3 = ThreadingHTTPServer(
                ("127.0.0.1", shim_port),
                make_shim_handler(shim_cfg, Backend(shim_cfg["backend_url"], "sk-live",
                                                    "mock-model"), ShimState(), lambda m: None))
            threading.Thread(target=shim3.serve_forever, daemon=True).start()
            time.sleep(0.2)
            try:
                r = native_probe(lib, "http://127.0.0.1", shim_port,
                                 prompt="Hello! Who are you?",
                                 system="You are Biagio Ferrari, Vaudeville, 1914.")
                for s in r["steps"]:
                    print("      " + s)
                check(r["ok"], f"native chat -> {r['text']!r}")
                if r["error"]:
                    check(False, f"native error: {r['error']}")
            finally:
                shim3.shutdown(); shim3.server_close()
                mock3.shutdown(); mock3.server_close()

    print("[8] UI structure, through the withdrawn --gui-check build")
    display = bool(os.environ.get("DISPLAY")) or IS_WINDOWS or IS_MACOS
    gui_probed = False
    if display:
        probe, problem, skip = _st_gui_check({"mode": MODE_OFF, "mode_confirmed": False}, "")
    else:
        probe, problem, skip = None, "", "this process has no display to draw on"
    if probe is None and skip:
        check(True, "skipped — " + skip + ", so no window was built "
                    "(the tab map itself is verified in [0i])")
    else:
        check(probe is not None and not problem,
              "the --gui-check child built a window and reported its tabs"
              + (f" — {problem}" if problem else ""))
        gui_probed = probe is not None
        if probe is not None:
            check(probe[1] == len(TAB_ORDER),
                  f"the GUI builds all {probe[1]} tab frames: {', '.join(TAB_ORDER)}")
            want = _st_visible_tabs(None)
            check(probe[0] == len(want) and probe[2] == want,
                  "with no setup confirmed the notebook holds Home only — showed "
                  f"({', '.join(probe[2]) or 'nothing'})")
            for mode in MODE_ORDER:
                want = _st_visible_tabs(mode)
                got, prob, _skip = _st_gui_check({"mode": mode, "mode_confirmed": True},
                                                 TAB_OF_MODE[mode])
                check(got is not None and got[2] == want and got[1] == len(TAB_ORDER),
                      f"confirming {mode_label(mode)} reveals exactly ({', '.join(want)})"
                      f" — got ({', '.join(got[2]) if got else (prob or 'no build')})")
            for tab in ("characters", "backup"):
                want = _st_visible_tabs(MODE_SHIM)
                got, prob, _skip = _st_gui_check({"mode": MODE_SHIM, "mode_confirmed": True},
                                                 tab)
                check(got is not None and got[2] == want and got[1] == len(TAB_ORDER),
                      f"--tab {tab} keeps Home + the setup tab + Characters + Backup"
                      f" — got ({', '.join(got[2]) if got else (prob or 'no build')})")
            want = _st_visible_tabs(MODE_OFF)
            got, prob, _skip = _st_gui_check({"mode": MODE_OFF, "mode_confirmed": False},
                                             "models")
            check(got is not None and got[2] == want,
                  "a pre-rebuild --tab name still opens its new tab"
                  f" — got ({', '.join(got[2]) if got else (prob or 'no build')})")

    # A section that declines to run must say so out loud.  Every legitimate
    # decline above emits a message starting with "skipped", and how many there
    # may be is fully determined by the environment: two in a frozen single-file
    # build (no VERSION file beside it, no gui_main source to lift) plus one when
    # no window could be built.  If an edit ever makes checks vanish quietly the
    # count stops matching and the suite fails instead of reporting a smaller
    # denominator.
    skips = [m for _ok, m in results if m.startswith("skipped")]
    expected_skips = (2 if frozen else 0) + (0 if gui_probed else 1)
    check(len(skips) == expected_skips,
          f"{len(skips)} checks declined to run, exactly the {expected_skips} this "
          f"environment allows" + (f": {'; '.join(s[:70] for s in skips)}" if skips else ""))

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n== {passed}/{len(results)} checks passed ==")
    return 0 if passed == len(results) else 1


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
AGENT_FIELD_NAMES = [n for n, _ in AGENT_LAYOUT]
LLM_FIELD_NAMES = [n for n, _ in LLM_LAYOUT]


def parse_assignments(items: list[str] | None) -> dict:
    out = {}
    for item in items or []:
        if "=" not in item:
            raise SystemExit(f"expected NAME=VALUE, got {item!r}")
        k, v = item.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def coerce_for(kind: str, name: str, text):
    fk = field_kind(kind, name)
    if fk == "b":
        return str(text).strip().lower() in ("1", "true", "yes", "on")
    if fk == "i":
        return int(str(text), 0)
    if fk == "f":
        return float(text)
    return text


def print_components(res: ScanResult, show_prompt: bool = False) -> None:
    for b in res.blobs:
        print(f"\n== {b.kind}  {b.path.name}  offset={b.offset:#x}  size={b.size}")
        for name, _ in (LLM_LAYOUT if b.kind == "LLM" else AGENT_LAYOUT):
            v = b.values[name]
            if isinstance(v, str) and len(v) > 80 and not show_prompt:
                v = v[:77] + "..."
            tag = ""
            if name in REMOTE_RELEVANT:
                tag = "  [sent to remote]"
            elif name in LOCAL_ONLY:
                tag = "  [local only]"
            print(f"   {name:20s} = {v!r}{tag}")


def cli(args) -> int:
    game_dir = resolve_game_dir(args.game_dir)
    cfg = load_config()
    cfg.update({k: v for k, v in {
        "mode": normalize_mode(args.mode) if args.mode else None,
        "backend_url": args.backend_url, "backend_model": args.backend_model,
        "shim_listen": args.listen, "shim_port": args.port, "expose_server": args.expose_server,
        "server_port": args.server_port, "strip_think": args.strip_think,
    }.items() if v is not None})
    if args.api_key:
        cfg["api_key"] = args.api_key
    if args.api_key_env:
        cfg["api_key"] = os.environ.get(args.api_key_env, "")
        if not cfg["api_key"]:
            print(f"warning: environment variable {args.api_key_env} is empty", file=sys.stderr)

    cmd = args.cli
    if cmd == "detect":
        for p, src in find_game_dirs():
            print(f"{p}\t({src})\tbuild-guid={build_guid(p)}")
        return 0

    if cmd in ("list", "plan", "apply"):
        res = scan_game(game_dir, use_profile=not args.rescan)
        print(f"# {game_dir}  build-guid={build_guid(game_dir)}  "
              f"({len(res.blobs)} components in {res.duration:.1f}s)")
        for n in res.notes:
            print(f"# note: {n}")
        if cmd == "list":
            print_components(res, show_prompt=args.show_prompts)
            return 0
        overrides = {}
        for k, v in parse_assignments(args.set).items():
            overrides[k] = coerce_for("AGENT", k, v)
        for k, v in parse_assignments(args.llm_set).items():
            overrides[k] = coerce_for("LLM", k, v)
        agent_changes, llm_changes, notes = build_change_set(cfg, res, overrides)
        for n in notes:
            print(f"# {n}")
        edits, warnings = plan_edits(res, agent_changes, llm_changes)
        for w in warnings:
            print(f"# warning: {w}", file=sys.stderr)
        print(f"\n# {len(edits)} byte edits:")
        for e in edits:
            print(f"  {e.label}")
        if cmd == "plan" or not edits:
            return 0
        if not args.yes:
            ans = input(f"\nApply these {len(edits)} edits to {game_dir}? [y/N] ").strip().lower()
            if ans not in ("y", "yes"):
                print("aborted")
                return 1
        record = apply_edits(edits, game_dir, do_backup=not args.no_backup)
        print(f"# backup: {record.dir}")
        res2 = scan_game(game_dir, use_profile=False)
        print("# verification:")
        for b in res2.blobs[:3]:
            print(f"  {b.kind} {b.path.name}@{b.offset:#x}: "
                  + ", ".join(f"{k}={b.values[k]!r}" for k in list(b.values)[:8]))
        print(f"  … {len(res2.blobs)} components re-decoded")
        return 0

    if cmd == "backups":
        for rec in list_backups():
            m = rec.manifest
            print(f"{rec.dir.name}  files={len(m['files'])}  game={m['game_dir']}")
            for lbl in m.get("labels", [])[:4]:
                print(f"     {lbl}")
        return 0

    if cmd == "restore":
        backups = list_backups()
        if not backups:
            print("no backups found")
            return 1
        if args.backup and args.backup != "latest":
            rec = next((b for b in backups if b.dir.name == args.backup
                        or str(b.dir) == args.backup), None)
            if rec is None:
                print(f"backup not found: {args.backup}")
                return 1
        else:
            rec = backups[0]
        if not args.yes:
            print(f"Restore {len(rec.manifest['files'])} file(s) from {rec.dir}?")
            for f in rec.manifest["files"]:
                print("   " + f["path"])
            if input("[y/N] ").strip().lower() not in ("y", "yes"):
                print("aborted")
                return 1
        for line in restore_backup(rec, game_dir):
            print("  " + line)
        return 0

    if cmd == "models":
        for slot in model_slots():
            st = slot_status(game_dir, slot)
            tgt = st["target"] or "-"
            print(f"{slot.label:28s} {slot.name}")
            print(f"{'':28s}   -> {tgt}  ({human_size(st['size'])}"
                  f"{', symlink' if st['is_link'] else ', real file'}"
                  f"{', BROKEN' if st['broken'] else ''})")
        print(f"\nlibrary dir: {library_dir(game_dir)}")
        for p in list_library_models(game_dir):
            print(f"   {p}   ({human_size(p.stat().st_size)})")
        return 0

    if cmd == "set-model":
        if not args.slot or not args.model:
            raise SystemExit("--set-model needs --slot primary|deck and --model PATH")
        slot = model_slots()[0 if args.slot == "primary" else 1]
        for line in set_slot_model(game_dir, slot, Path(args.model), dry_run=args.dry_run):
            print("  " + line)
        return 0

    if cmd in ("shim-start", "shim-stop", "shim-status", "shim-test"):
        shim_cfg = {"listen": cfg.get("shim_listen", "127.0.0.1"),
                    "port": int(cfg.get("shim_port", 13333)),
                    "mode": cfg.get("shim_mode", "chat"),
                    "backend_url": cfg.get("backend_url", ""),
                    "backend_model": cfg.get("backend_model", ""),
                    "api_key": cfg.get("api_key", ""),
                    "strip_think": cfg.get("strip_think", True),
                    "verbose": args.verbose, "insecure": args.insecure,
                    "embeddings": False, "timeout": 600}
        sp = ShimProcess(shim_cfg)
        if cmd == "shim-start":
            ok, msg = sp.start()
            print(("ok: " if ok else "error: ") + msg)
            return 0 if ok else 1
        if cmd == "shim-stop":
            print(sp.stop())
            return 0
        if cmd == "shim-status":
            pid = sp.pid()
            ok, detail = sp.health() if pid else (False, "not running")
            print(f"pid={pid} healthy={ok} detail={detail[:120]}")
            print(sp.tail(15))
            return 0 if ok else 1
        ok, text = sp.test_completion(args.prompt or "Say hello in three words.")
        print(("ok: " if ok else "error: ") + text)
        return 0 if ok else 1

    raise SystemExit(f"unknown --cli command {cmd!r}")


def resolve_game_dir(explicit: str | None) -> Path:
    if explicit:
        p = Path(explicit).expanduser()
        if not is_game_dir(p):
            raise SystemExit(f"not a Vaudeville game directory: {p}")
        return p
    cfg = load_config()
    if cfg.get("game_dir"):
        p = Path(cfg["game_dir"]).expanduser()
        if is_game_dir(p):
            return p
    found = find_game_dirs()
    steam = [p for p, src in found if "appmanifest" in src or "steamapps" in src]
    if steam:
        return steam[0]
    if found:
        return found[0][0]
    raise SystemExit("could not find a Vaudeville install; pass --game-dir or set it in the GUI")


# --------------------------------------------------------------------------- #
# GUI (tkinter): tab plumbing, hover tooltips, the window itself
# --------------------------------------------------------------------------- #
TAB_OF_MODE = {MODE_OFF: "basic", MODE_DIRECT: "advanced", MODE_SHIM: "remote"}
MODE_OF_TAB = {v: k for k, v in TAB_OF_MODE.items()}
TAB_ORDER = ("home", "basic", "advanced", "remote", "characters", "backup")
TAB_ALIASES = {
    "home": "home", "start": "home", "welcome": "home", "game": "home",
    "basic": "basic", "off": "basic", "local": "basic", "local-basic": "basic",
    "local-mode-basic": "basic", "models": "basic", "model": "basic",
    "advanced": "advanced", "direct": "advanced", "local-advanced": "advanced",
    "local-mode-advanced": "advanced",
    "remote": "remote", "shim": "remote", "openai": "remote", "remote-shim": "remote",
    "remote-mode": "remote", "endpoint": "remote",
    "characters": "characters", "character": "characters", "cast": "characters",
    "groups": "characters", "parameters": "characters", "params": "characters",
    "backup": "backup", "backups": "backup", "restore": "backup",
}


def normalize_tab(value) -> str:
    key = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
    return TAB_ALIASES.get(key, "")


def tab_title(key: str) -> str:
    if key in MODE_OF_TAB:
        return " " + mode_label(MODE_OF_TAB[key]) + " "
    return {"home": " Home ", "characters": " Characters ",
            "backup": " Backup / restore "}.get(key, f" {key} ")


class ToolTip:
    """Small hover tooltip: <Enter> schedules it, <Leave>/<ButtonPress> drop it."""

    DELAY_MS = 320

    def __init__(self, widget, text: str, wraplength: int = 460):
        import tkinter as tk
        self._tk = tk
        self.widget = widget
        self.text = str(text) if text else ""
        self.wraplength = wraplength
        self._win = None
        self._after = None
        try:
            widget.bind("<Enter>", self._enter, add="+")
            widget.bind("<Leave>", self._leave, add="+")
            widget.bind("<ButtonPress>", self._leave, add="+")
        except Exception:                        # noqa: BLE001 - never break the build
            pass

    def set_text(self, text: str) -> None:
        self.text = str(text) if text else ""

    # -- internals -------------------------------------------------------- #
    def _enter(self, _event=None):
        if not self.text:
            return
        self._cancel()
        try:
            self._after = self.widget.after(self.DELAY_MS, self._popup)
        except Exception:                        # noqa: BLE001
            self._after = None

    def _popup(self):
        self._after = None
        tk = self._tk
        try:
            if not self.text or not self.widget.winfo_exists():
                return
            x = self.widget.winfo_rootx() + 14
            y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
            win = tk.Toplevel(self.widget)
            win.wm_overrideredirect(True)
            win.wm_geometry(f"+{x}+{y}")
            tk.Label(win, text=self.text, justify="left", anchor="w",
                     background="#ffffe1", foreground="#1c1c1c", relief="solid",
                     borderwidth=1, padx=9, pady=6, wraplength=self.wraplength).pack()
            self._win = win
        except Exception:                        # noqa: BLE001
            self._win = None

    def _leave(self, _event=None):
        self._cancel()
        if self._win is not None:
            try:
                self._win.destroy()
            except Exception:                    # noqa: BLE001
                pass
            self._win = None

    def _cancel(self):
        if self._after is not None:
            try:
                self.widget.after_cancel(self._after)
            except Exception:                    # noqa: BLE001
                pass
            self._after = None


def add_tip(widget, text: str, wraplength: int = 460):
    """Attach (and keep a reference to) a tooltip.

    Empty text is fine: the tooltip stays invisible until set_text() fills it,
    which is how the "in use now" and scope lines get their hover text later."""
    tip = ToolTip(widget, text, wraplength=wraplength)
    try:
        widget._vaudeville_tip = tip              # keep it alive
    except Exception:                            # noqa: BLE001
        pass
    return tip


def gui_main(args) -> int:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"{APP_NAME} {APP_VERSION} — how Vaudeville talks to its AI")
            self.geometry(getattr(args, "geometry", None) or "1120x800")
            self.minsize(960, 660)
            self.cfg = load_config()
            self.scan: ScanResult | None = None
            self.agent_vars: dict[str, tk.StringVar] = {}    # Characters tab
            self.llm_vars: dict[str, tk.StringVar] = {}      # engine tab
            self.action_buttons: list = []
            self.tab_frames: dict[str, object] = {}
            self.plan_boxes: dict[str, object] = {}
            self.model_panels: list[dict] = []
            self.mode_cards: dict[str, list] = {}
            self.extra_overrides: dict = {}
            self.chosen_mode: str | None = None
            self._palette()
            self.current_group = normalize_group(self.cfg.get("cast_group")) or all_group_key()
            self._build()
            self._startup_tab(args)
            if getattr(args, "quit_after", 0):
                self.after(int(float(args.quit_after) * 1000), self.destroy)
            if not getattr(args, "gui_check", False):
                self.after(200, self._initial_scan)
                self.after(1000, self._poll_running)
            self.protocol("WM_DELETE_WINDOW", self._on_close)

        # ---------- look ---------- #
        def _rgb(self, color):
            try:
                r, g, b = self.winfo_rgb(color)
                return (r >> 8, g >> 8, b >> 8)
            except Exception:                                # noqa: BLE001
                return (240, 240, 240)

        def _hexc(self, rgb) -> str:
            return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)

        def _mix(self, a, b, t: float) -> str:
            ra, rb = self._rgb(a), self._rgb(b)
            return self._hexc(tuple(ra[i] * t + rb[i] * (1.0 - t) for i in range(3)))

        def _palette(self):
            style = ttk.Style(self)
            if "clam" in style.theme_names():
                style.theme_use("clam")
            bg = style.lookup("TFrame", "background") or self.cget("background") or "#efefef"
            fg = style.lookup("TLabel", "foreground") or "#101010"
            self.bg, self.fg = bg, fg
            self.muted = self._mix(fg, bg, 0.45)
            self.accent = "#1f6feb"
            self.card_bg = self._mix(fg, bg, 0.035)
            self.card_bg_on = self._mix(self.accent, bg, 0.16)
            self.card_edge = self._mix(fg, bg, 0.30)
            self.card_edge_on = self.accent
            light = sum(self._rgb(bg)) / 3.0 > 110
            self.link_fg = "#1f6feb" if light else "#7ab8ff"
            self.warn_fg = "#b3261e" if light else "#ff9c9c"
            self.ok_fg = "#1e7d32" if light else "#8fe38f"

        # ---------- helpers ---------- #
        def log(self, msg: str):
            stamp = dt.datetime.now().strftime("%H:%M:%S")
            self.logbox.configure(state="normal")
            self.logbox.insert("end", f"{stamp}  {msg}\n")
            self.logbox.see("end")
            self.logbox.configure(state="disabled")
            log(msg)

        def status(self, msg: str):
            self.status_var.set(msg)

        def busy(self, on: bool, msg: str = ""):
            self.progress["mode"] = "indeterminate" if on else "determinate"
            if on:
                self.progress.start(12)
            else:
                self.progress.stop()
            for w in self.action_buttons:
                try:
                    w.configure(state="disabled" if on else "normal")
                except Exception:                            # noqa: BLE001
                    pass
            if msg:
                self.status(msg)

        def run(self, fn, done=None, what="working…"):
            self.busy(True, what)

            def worker():
                try:
                    out = fn()
                    err = None
                except Exception as exc:                     # noqa: BLE001 - report everything
                    out, err = None, exc
                self.after(0, lambda: self._finish(out, err, done))
            threading.Thread(target=worker, daemon=True).start()

        def _finish(self, out, err, done):
            self.busy(False)
            if err is not None:
                self.log(f"ERROR: {err}")
                messagebox.showerror(APP_NAME, T(str(err)))
                return
            if done:
                done(out)

        def game_dir(self) -> Path | None:
            p = Path(self.dir_var.get().strip()).expanduser()
            return p if str(p).strip() else None

        def _int_or(self, var, default: int, what: str) -> int:
            raw = str(var.get()).strip()
            if not raw:
                return int(default)
            try:
                return int(raw, 0)
            except ValueError:
                self.log(T(f"{what}: “{raw}” is not a whole number — using {default}."))
                return int(default)

        # ---------- build ---------- #
        def _build(self):
            top = ttk.Frame(self, padding=(10, 8, 10, 4))
            top.pack(fill="x")
            lbl = ttk.Label(top, text=T("Game folder:"))
            lbl.pack(side="left")
            add_tip(lbl, T("The Vaudeville install folder (the one that contains "
                           "Vaudeville_Data). Detect finds it for you; Browse… lets you "
                           "point at a copy somewhere else."))
            self.dir_var = tk.StringVar(value=self.cfg.get("game_dir", ""))
            entry = ttk.Entry(top, textvariable=self.dir_var)
            entry.pack(side="left", fill="x", expand=True, padx=6)
            add_tip(entry, T("Where the game is installed. Nothing is written until you "
                             "press Apply, and a verified backup is made first."))
            for text, cmd, tip in (
                    ("Browse…", self.browse, T("Choose the game folder by hand.")),
                    ("Detect", self.detect, T("Look for a Steam install of Vaudeville automatically.")),
                    ("Re-scan", self.rescan, T("Read the game files again and refresh every value shown here."))):
                b = ttk.Button(top, text=T(text), command=cmd)
                b.pack(side="left", padx=2)
                add_tip(b, tip)
                self.action_buttons.append(b)
            self.running_lbl = ttk.Label(top, text="", foreground=self.warn_fg)
            self.running_lbl.pack(side="left", padx=8)
            add_tip(self.running_lbl, T("The game must be closed before its files can be changed."))

            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True, padx=10, pady=4)
            self.bind_all("<Button-4>", self._wheel_all)
            self.bind_all("<Button-5>", self._wheel_all)
            self._tab_home()
            self._tab_basic()
            self._tab_advanced()
            self._tab_remote()
            self._tab_characters()
            self._tab_backup()

            bottom = ttk.Frame(self, padding=(10, 2, 10, 8))
            bottom.pack(fill="both", expand=False)
            self.logbox = tk.Text(bottom, height=8, wrap="word", state="disabled",
                                  background="#111", foreground="#cfc")
            self.logbox.pack(fill="both", expand=True)
            add_tip(self.logbox, T("Everything this tool does, in order. Also written to a "
                                   "file you can read later."))
            bar = ttk.Frame(self)
            bar.pack(fill="x", padx=10, pady=(0, 6))
            self.status_var = tk.StringVar(value=T("ready"))
            sl = ttk.Label(bar, textvariable=self.status_var)
            sl.pack(side="left")
            add_tip(sl, T("What is happening right now."))
            self.progress = ttk.Progressbar(bar, length=140, mode="determinate")
            self.progress.pack(side="right")
            add_tip(self.progress, T("Spins while the game files are read or written."))

        # ---------- tab plumbing ---------- #
        def _sync_tabs(self, select: str = ""):
            """Show Home, the chosen setup's tab, Characters and Backup — nothing else."""
            order = ["home"]
            if self.chosen_mode in TAB_OF_MODE:
                order += [TAB_OF_MODE[self.chosen_mode], "characters", "backup"]
            for name in list(self.nb.tabs()):
                try:
                    self.nb.forget(name)
                except Exception:                            # noqa: BLE001
                    pass
            for key in order:
                frame = self.tab_frames.get(key)
                if frame is None:
                    continue
                try:
                    self.nb.add(frame, text=tab_title(key))
                except Exception:                            # noqa: BLE001
                    pass
            target = self.tab_frames.get(select or "home")
            if target is not None:
                try:
                    self.nb.select(target)
                except Exception:                            # noqa: BLE001
                    pass

        def _startup_tab(self, args):
            want = normalize_tab(getattr(args, "tab", None))
            mode = normalize_mode(self.cfg.get("mode", MODE_OFF), MODE_OFF)
            if want in MODE_OF_TAB:
                mode = MODE_OF_TAB[want]
                self.chosen_mode = mode
            elif want in ("characters", "backup") or self.cfg.get("mode_confirmed"):
                self.chosen_mode = mode
            try:
                self.mode_pick_var.set(mode)
            except Exception:                                # noqa: BLE001
                pass
            self._refresh_cards()
            self._sync_tabs(select=want or "home")

        def _build_footer(self, parent):
            bar = tk.Frame(parent, background=self.bg)
            bar.pack(fill="x", pady=(10, 0))
            credit = tk.Label(bar, text=CREDIT, background=self.bg, foreground=self.muted,
                              justify="left", anchor="w", wraplength=820)
            credit.pack(side="left", fill="x", expand=True)
            add_tip(credit, T("Who makes this tool, and what it is allowed to touch."))
            link = tk.Label(bar, text=GITHUB_URL, background=self.bg, foreground=self.link_fg,
                            cursor="hand2", font=("TkDefaultFont", 9, "underline"))
            link.pack(side="right", padx=(8, 0))
            link.bind("<Button-1>", lambda _e: self.open_project_page())
            add_tip(link, T("Open the project page in your web browser: source code, "
                            "downloads, release notes and the issue tracker."))

        def open_project_page(self):
            try:
                webbrowser.open(GITHUB_URL)
                self.log(T("Opened the project page: ") + GITHUB_URL)
            except Exception as exc:                         # noqa: BLE001
                self.log(T("Could not open a web browser: ") + str(exc))
                messagebox.showinfo(APP_NAME, T("Project page: ") + GITHUB_URL)

        # ---------- scrollable tab content ---------- #
        def _scrolled(self, parent):
            """Canvas + scrollbar wrapper so a tall tab never clips its plan box."""
            canv = tk.Canvas(parent, highlightthickness=0)
            sb = ttk.Scrollbar(parent, orient="vertical", command=canv.yview)
            inner = ttk.Frame(canv, padding=12)
            inner.bind("<Configure>",
                       lambda _e: canv.configure(scrollregion=canv.bbox("all")))
            win = canv.create_window((0, 0), window=inner, anchor="nw")
            canv.configure(yscrollcommand=sb.set)
            canv.pack(side="left", fill="both", expand=True)
            sb.pack(side="right", fill="y")
            canv.bind("<Configure>",
                      lambda e, w=win: canv.itemconfigure(w, width=e.width))
            inner._vaudeville_canvas = canv
            return inner

        def _wheel_all(self, event):
            w = event.widget
            while w is not None and not isinstance(w, tk.Toplevel):
                canv = getattr(w, "_vaudeville_canvas", None)
                if canv is not None:
                    try:
                        canv.yview_scroll(-1 if event.num == 4 else 1, "units")
                    except Exception:                            # noqa: BLE001
                        pass
                    return "break"
                w = getattr(w, "master", None)
            return None

        # ---------- tab: home ---------- #
        def _tab_home(self):
            f = ttk.Frame(self.nb)
            self.tab_frames["home"] = f
            c = self._scrolled(f)
            head = ttk.Label(c, text=T("How should Vaudeville talk to its AI?"),
                             font=("TkDefaultFont", 15, "bold"))
            head.pack(anchor="w")
            add_tip(head, T("Three setups, pick one. The rest of this window stays hidden "
                            "until you choose, so you only ever see settings that apply to "
                            "your setup."))
            sub = ttk.Label(c, foreground=self.muted, wraplength=980, justify="left",
                            text=T("Choose a card below and press Continue. You can come back "
                                   "here and switch at any time — a verified backup of the game "
                                   "files is made before anything is written."))
            sub.pack(anchor="w", pady=(2, 10))
            add_tip(sub, T("Switching setups rewrites the same few values in the game files; "
                           "it never deletes anything."))

            box = ttk.LabelFrame(c, text=T("Your game"), padding=10)
            box.pack(fill="x")
            self.info_var = tk.StringVar(value=T("Looking for the game…"))
            info = ttk.Label(box, textvariable=self.info_var, justify="left", wraplength=980)
            info.pack(anchor="w", fill="x")
            self.info_tip = add_tip(info, T("Where the game was found, which build it is, and "
                                            "how many character and engine settings this tool "
                                            "can see."))

            cards = ttk.Frame(c)
            cards.pack(fill="both", expand=True, pady=(10, 2))
            self.mode_pick_var = tk.StringVar(
                value=normalize_mode(self.cfg.get("mode", MODE_OFF), MODE_OFF))
            for key in MODE_ORDER:
                self._build_mode_card(cards, key)

            row = ttk.Frame(c)
            row.pack(fill="x", pady=(4, 0))
            self.continue_btn = ttk.Button(row, text=T("Continue"), command=self._continue_home)
            self.continue_btn.pack(side="left")
            self.action_buttons.append(self.continue_btn)
            add_tip(self.continue_btn, T("Open the settings for the setup you picked."))
            self.home_hint = ttk.Label(row, text="", foreground=self.muted, wraplength=760,
                                       justify="left")
            self.home_hint.pack(side="left", padx=12)
            self._refresh_cards()
            self._build_footer(c)

        def _build_mode_card(self, parent, key: str):
            card = MODE_CARDS[key]
            title = str(card.get("title") or mode_label(key))
            blurb = str(card.get("blurb") or T(MODE_TIPS[key]))
            diff = T("Difficulty: ") + str(card.get("difficulty") or "—")
            rest = T("Restrictions: ") + str(card.get("restrictions") or "—")
            outer = tk.Frame(parent, background=self.card_bg, highlightthickness=2,
                             highlightbackground=self.card_edge, bd=0, padx=12, pady=9,
                             cursor="hand2")
            outer.pack(fill="x", pady=5)
            rb = tk.Radiobutton(outer, text=title, value=key, variable=self.mode_pick_var,
                                background=self.card_bg, foreground=self.fg,
                                activebackground=self.card_bg, activeforeground=self.fg,
                                selectcolor=self.card_bg, anchor="w", cursor="hand2",
                                font=("TkDefaultFont", 11, "bold"),
                                command=lambda k=key: self._card_picked(k))
            rb.pack(anchor="w")
            body = tk.Label(outer, text=blurb, justify="left", anchor="w", wraplength=940,
                            background=self.card_bg, foreground=self.fg, cursor="hand2")
            body.pack(anchor="w", pady=(2, 4))
            d = tk.Label(outer, text=diff, justify="left", anchor="w", wraplength=940,
                         background=self.card_bg, foreground=self.muted, cursor="hand2")
            d.pack(anchor="w")
            r = tk.Label(outer, text=rest, justify="left", anchor="w", wraplength=940,
                         background=self.card_bg, foreground=self.muted, cursor="hand2")
            r.pack(anchor="w", pady=(2, 0))
            self.mode_cards[key] = [outer, rb, body, d, r]
            tip = "\n".join(x for x in (title, blurb, diff, rest,
                                        T("Double-click to open this setup right away.")) if x)
            for w in (outer, rb, body, d, r):
                w.bind("<Button-1>", lambda _e, k=key: self._card_picked(k), add="+")
                w.bind("<Double-Button-1>", lambda _e, k=key: self._card_go(k), add="+")
                add_tip(w, tip)

        def _refresh_cards(self):
            picked = normalize_mode(self.mode_pick_var.get(), "")
            for key, widgets in self.mode_cards.items():
                on = key == picked
                bg = self.card_bg_on if on else self.card_bg
                edge = self.card_edge_on if on else self.card_edge
                widgets[0].configure(background=bg, highlightbackground=edge)
                for w in widgets[1:]:
                    try:
                        w.configure(background=bg)
                        if isinstance(w, tk.Radiobutton):
                            w.configure(activebackground=bg,
                                        selectcolor=self.card_bg_on if on else self.card_bg)
                    except Exception:                        # noqa: BLE001
                        pass
            try:
                self.continue_btn.configure(state="normal" if picked else "disabled")
            except Exception:                                # noqa: BLE001
                pass
            self.home_hint.configure(
                text=(T("Selected: ") + mode_label(picked)) if picked
                else T("No setup chosen yet — pick a card above."))

        def _card_picked(self, key: str):
            self.mode_pick_var.set(key)
            self._refresh_cards()
            self.status(T("Selected: ") + mode_label(key))

        def _card_go(self, key: str):
            self.mode_pick_var.set(key)
            self._refresh_cards()
            self._continue_home()

        def _continue_home(self):
            key = normalize_mode(self.mode_pick_var.get(), "")
            if not key:
                messagebox.showinfo(APP_NAME, T("Pick one of the three setup cards first."))
                return
            self.chosen_mode = key
            self.cfg["mode"] = key
            self.cfg["mode_confirmed"] = True
            self._sync_tabs(select=TAB_OF_MODE[key])
            self.log(T("Setup chosen: ") + mode_label(key))
            self.status(T("Showing the settings for ") + mode_label(key))
            self._save_cfg()
            self.refresh_models()
            self._update_gpu_line()

        def _save_cfg(self):
            try:
                self.cfg = self._collect_config()
                save_config(self.cfg)
            except Exception as exc:                         # noqa: BLE001
                self.log(f"config save failed: {exc}")

        # ---------- shared: mode header / apply row ---------- #
        def _mode_header(self, parent, key: str, tagline: str = ""):
            card = MODE_CARDS[key]
            title = str(card.get("title") or mode_label(key))
            blurb = str(card.get("blurb") or T(MODE_TIPS[key]))
            head = ttk.Label(parent, text=title, font=("TkDefaultFont", 14, "bold"))
            head.pack(anchor="w")
            add_tip(head, T(tagline) or blurb)
            if tagline:
                sub = ttk.Label(parent, text=T(tagline), foreground=self.muted,
                                wraplength=960, justify="left")
                sub.pack(anchor="w", pady=(0, 6))
                add_tip(sub, blurb)
            box = ttk.LabelFrame(parent, text=T("What this setup does"), padding=8)
            box.pack(fill="x", pady=(0, 6))
            body = ttk.Label(box, text=blurb, wraplength=950, justify="left")
            body.pack(anchor="w")
            add_tip(body, str(card.get("restrictions") or blurb))
            for label, value in ((T("Difficulty: "), card.get("difficulty")),
                                 (T("Restrictions: "), card.get("restrictions"))):
                if value:
                    line = ttk.Label(box, text=label + str(value), foreground=self.muted,
                                     wraplength=950, justify="left")
                    line.pack(anchor="w", pady=(3, 0))
                    add_tip(line, blurb)
            return box

        def _build_apply_row(self, parent, tab_key: str, extra: str = ""):
            act = ttk.Frame(parent)
            act.pack(fill="x", pady=(8, 4))
            b = ttk.Button(act, text=T("Preview changes"), command=self.preview)
            b.pack(side="left")
            self.action_buttons.append(b)
            add_tip(b, T("Work out exactly what would change and show it here. No file is touched."))
            b = ttk.Button(act, text=T("Apply to game files"), command=self.apply_changes)
            b.pack(side="left", padx=6)
            self.action_buttons.append(b)
            add_tip(b, T("Write the changes into the game files. A verified backup is made "
                         "first and the game must be closed."))
            if extra:
                note = ttk.Label(act, text=T(extra), foreground=self.muted, wraplength=620,
                                 justify="left")
                note.pack(side="left", padx=10)
                add_tip(note, T("Backups are listed on the Backup / restore tab."))
            box = tk.Text(parent, height=8, wrap="word", state="disabled")
            box.pack(fill="both", expand=True)
            self.plan_boxes[tab_key] = box
            add_tip(box, T("The plan: what will change, where, and anything to watch out for. "
                           "File names and offsets are shown for people who want the detail."))

        # ---------- shared: model panel ---------- #
        def _build_model_panel(self, parent, restrict: bool) -> dict:
            title = (T("Models — %s family only" % LLAMA3_8B_FAMILY) if restrict
                     else T("Models — any model file"))
            box = ttk.LabelFrame(parent, text=title, padding=8)
            box.pack(fill="x", pady=6)
            fallback = (
                T("This setup only accepts model files from the %s family, because that is what "
                  "the shipped game data expects. Anything else is refused here — use %s or %s "
                  "for other models.") % (LLAMA3_8B_FAMILY, mode_label(MODE_DIRECT),
                                          mode_label(MODE_SHIM))
                if restrict else
                T("Any model file works here. The game always looks for the same two file names, "
                  "so this tool points each of them at the model you choose and keeps the "
                  "original file safe beside it."))
            intro = help_line(MODEL_HELP,
                              ("basic restriction", "basic", "intro") if restrict
                              else ("advanced restriction", "advanced", "intro"), fallback)
            il = ttk.Label(box, text=intro, wraplength=950, justify="left")
            il.pack(anchor="w")
            add_tip(il, intro)
            panel = {"restrict": restrict, "rows": {}, "last": {}, "cur_tips": {},
                     "warn": None, "box": box}
            for slot in model_slots():
                rowf = ttk.Frame(box)
                rowf.pack(fill="x", pady=(6, 0))
                name = ttk.Label(rowf, text=slot.label + ":", width=27, anchor="w")
                name.pack(side="left")
                add_tip(name, help_line(MODEL_HELP,
                                        (slot.label, slot.name,
                                         "primary" if slot.name == PRIMARY_MODEL_NAME else "deck",
                                         "slot"),
                                        slot.label + " — " +
                                        T("the file name the game always looks for")))
                combo = ttk.Combobox(rowf, width=58)
                combo.pack(side="left", fill="x", expand=True, padx=4)
                add_tip(combo, T("Pick a model from your collection, or Browse… to any model "
                                 "file on this computer.") +
                        ("\n" + T("This setup accepts the %s family only.") % LLAMA3_8B_FAMILY
                         if restrict else ""))
                bb = ttk.Button(rowf, text=T("Browse…"),
                                command=lambda p=panel, c=combo, s=slot: self.pick_model(p, c, s))
                bb.pack(side="left", padx=2)
                add_tip(bb, T("Choose a model file from anywhere on this computer."))
                ba = ttk.Button(rowf, text=T("Use this model"),
                                command=lambda p=panel, s=slot: self.apply_model(p, s))
                ba.pack(side="left", padx=2)
                self.action_buttons.append(ba)
                add_tip(ba, T("Point the game at the selected model. The original file is kept "
                              "and nothing is overwritten."))
                cur = ttk.Label(box, text=T("in use now: …"), foreground=self.muted,
                                anchor="w", wraplength=950, justify="left")
                cur.pack(anchor="w", fill="x")
                panel["rows"][slot.name] = (cur, combo)
                panel["last"][slot.name] = ""
                panel["cur_tips"][slot.name] = add_tip(cur, "")
                combo.bind("<<ComboboxSelected>>",
                           lambda _e, p=panel, c=combo, s=slot: self._model_chosen(p, c, s, True))
                combo.bind("<FocusOut>",
                           lambda _e, p=panel, c=combo, s=slot: self._model_chosen(p, c, s, False))
            panel["warn"] = ttk.Label(box, text="", foreground=self.warn_fg, wraplength=950,
                                      justify="left")
            panel["warn"].pack(anchor="w", pady=(6, 0))
            foot = ttk.Frame(box)
            foot.pack(fill="x", pady=(6, 0))
            panel["lib_var"] = tk.StringVar(value="")
            liblbl = ttk.Label(foot, textvariable=panel["lib_var"], foreground=self.muted,
                               wraplength=640, justify="left")
            liblbl.pack(side="left", fill="x", expand=True)
            add_tip(liblbl, T("Your model collection folder. Drop GGUF files in here and press "
                              "Refresh list to see them."))
            b1 = ttk.Button(foot, text=T("Open models folder"), command=self.open_library)
            b1.pack(side="right")
            add_tip(b1, T("Open the folder where your model files live."))
            b2 = ttk.Button(foot, text=T("Refresh list"), command=self.refresh_models)
            b2.pack(side="right", padx=4)
            add_tip(b2, T("Re-read the model folder and what the game is using right now."))
            self.model_panels.append(panel)
            return panel
        # ---------- tab: Local Mode - Basic ---------- #
        def _tab_basic(self):
            f = ttk.Frame(self.nb)
            self.tab_frames["basic"] = f
            c = self._scrolled(f)
            self._mode_header(c, MODE_OFF,
                              T("The unmodified game, running a model file you choose."))
            self._build_model_panel(c, restrict=True)
            gpu = ttk.LabelFrame(c, text=T("Graphics-card offload"), padding=8)
            gpu.pack(fill="x", pady=6)
            text = T("How much of the model runs on your graphics card is decided by the game's "
                     "own Options screen (the AI/GPU slider), and that slider always wins over "
                     "anything set here. Change it in the game, then start a new game or restart "
                     "the game for it to take effect.")
            g = ttk.Label(gpu, text=text, wraplength=950, justify="left")
            g.pack(anchor="w")
            add_tip(g, help_line(MODEL_HELP, ("gpu", "numGPULayers", "offload"), text))
            self.gpu_current = ttk.Label(gpu, text="", foreground=self.muted, wraplength=950,
                                         justify="left")
            self.gpu_current.pack(anchor="w", pady=(4, 0))
            self.gpu_tip = add_tip(self.gpu_current, "")
            self._build_apply_row(c, "basic",
                                  T("Changes are written to the game files with a backup first."))

        def _update_gpu_line(self):
            try:
                if self.scan is not None and self.scan.llm:
                    v = self.scan.llm[0].values.get("numGPULayers")
                    self.gpu_current.configure(text=T("Game files currently say: ") +
                                               f"numGPULayers={v}" +
                                               T(" (the in-game Options slider overrides this "
                                                 "every time the game starts)."))
                    if self.gpu_tip:
                        self.gpu_tip.set_text(T("Under the hood: the numGPULayers value in the "
                                                "game's AI engine settings. Local Mode - "
                                                "Advanced can also set it."))
                else:
                    self.gpu_current.configure(
                        text=T("Game files not read yet — press Re-scan to see the current value."))
            except Exception:                                # noqa: BLE001
                pass

        # ---------- tab: Local Mode - Advanced ---------- #
        def _tab_advanced(self):
            f = ttk.Frame(self.nb)
            self.tab_frames["advanced"] = f
            c = self._scrolled(f)
            self._mode_header(c, MODE_DIRECT,
                              T("The game connects straight to an AI server you run yourself."))
            self._build_model_panel(c, restrict=False)

            srv = ttk.LabelFrame(c, text=T("Your server"), padding=8)
            srv.pack(fill="x", pady=6)
            mode = normalize_mode(self.cfg.get("mode", MODE_OFF), MODE_OFF)
            row = ttk.Frame(srv)
            row.pack(fill="x")
            hl = ttk.Label(row, text=T("Address:"))
            hl.pack(side="left")
            add_tip(hl, help_line(SHIM_HELP, ("direct_host", "host"),
                                  T("The computer your server runs on, for example 127.0.0.1 "
                                    "for this machine or a name on your home network.")))
            self.direct_host_var = tk.StringVar(value=str(
                self.cfg.get("direct_host")
                or (self.cfg.get("backend_url", "") if mode == MODE_DIRECT else "")))
            he = ttk.Entry(row, textvariable=self.direct_host_var, width=26)
            he.pack(side="left", padx=4)
            add_tip(he, T("No https:// and no web path such as /v1 — this setup hands the "
                          "address straight to the game's own network code.") +
                    "\n" + T("Remote Mode accepts any address."))
            he.bind("<KeyRelease>", lambda _e: self._check_host_fit())
            he.bind("<FocusOut>", lambda _e: self._check_host_fit())
            pl = ttk.Label(row, text=T("Port:"))
            pl.pack(side="left", padx=(10, 0))
            add_tip(pl, help_line(SHIM_HELP, ("direct_port", "port"),
                                  T("The port your server listens on. llama-server uses 8080 by "
                                    "default. Leave empty to use the port in the address.")))
            self.direct_port_var = tk.StringVar(value=str(self.cfg.get("direct_port") or ""))
            pe = ttk.Entry(row, textvariable=self.direct_port_var, width=7)
            pe.pack(side="left", padx=4)
            add_tip(pe, T("A whole number between 1 and 65535."))
            self.host_fit_var = tk.StringVar(value="")
            fit = ttk.Label(row, textvariable=self.host_fit_var, foreground=self.muted,
                            wraplength=520, justify="left")
            fit.pack(side="left", padx=8)
            self.host_fit_tip = add_tip(fit, T("Checked live against the space the game "
                                               "reserves for the address."))
            rule = ttk.Label(srv, foreground=self.muted, wraplength=950, justify="left", text=T(
                "Why the odd length rule: the game keeps this address in a fixed-size space that "
                "currently holds “localhost”, so the replacement has to be 9 to 12 characters "
                "long. 127.0.0.1 and myserver.lan both fit; longer names, https:// and paths "
                "like /v1 do not. Use Remote Mode when you need those."))
            rule.pack(anchor="w", pady=(6, 0))
            add_tip(rule, T("Under the hood: strings can only be rewritten in place when their "
                            "rounded-up byte length stays the same."))
            proto = ttk.Label(srv, foreground=self.muted, wraplength=950, justify="left", text=T(
                "The server must speak the llama.cpp protocol (/health, /apply-template, "
                "/completion, /tokenize). llama-server does. OpenAI-style services such as "
                "Ollama, LM Studio, vLLM or OpenAI do not — use Remote Mode for those."))
            proto.pack(anchor="w", pady=(4, 0))
            add_tip(proto, T("Remote Mode runs a small translator on your computer, so "
                             "OpenAI-style services work there."))

            eng = ttk.LabelFrame(c, text=T("AI engine settings"), padding=8)
            eng.pack(fill="both", expand=True, pady=6)
            intro = ttk.Label(eng, wraplength=950, justify="left", text=T(
                "These are read by the game's own AI engine when it loads the model. Empty boxes "
                "are left exactly as they are."))
            intro.pack(anchor="w", pady=(0, 4))
            add_tip(intro, help_line(MODEL_HELP, ("engine", "intro"),
                                     T("Only Local Mode - Basic and Local Mode - Advanced use "
                                       "these; a remote server has its own settings.")))
            gridf = ttk.Frame(eng)
            gridf.pack(fill="x")
            self._field_grid(gridf, ENGINE_FIELDS, self.llm_vars, "LLM", columns=2)
            self._build_apply_row(c, "advanced",
                                  T("Changes are written to the game files with a backup first."))
            self._check_host_fit()

        def _check_host_fit(self):
            wanted = self.direct_host_var.get().strip()
            if not wanted:
                self.host_fit_var.set(T("Address empty — the game keeps using its own model."))
                return
            if "://" in wanted or "/" in wanted:
                self.host_fit_var.set(T("Use a plain address such as 127.0.0.1 — no https:// "
                                        "and no /v1 path here."))
                return
            current = "localhost"
            if self.scan is not None and self.scan.agents:
                current = str(self.scan.agents[0].values.get("host") or "localhost")
            ok, why = host_fits(current, wanted)
            if ok:
                self.host_fit_var.set(T("This address fits ✓ (9–12 characters)."))
            else:
                short = T("Too long for the space the game reserves — 9 to 12 characters only.")
                self.host_fit_var.set(short)
                if self.host_fit_tip:
                    self.host_fit_tip.set_text(T(why))

        # ---------- tab: Remote Mode ---------- #
        def _tab_remote(self):
            f = ttk.Frame(self.nb)
            self.tab_frames["remote"] = f
            c = self._scrolled(f)
            self._mode_header(c, MODE_SHIM,
                              T("A small translator on your computer connects the game to any "
                                "AI service."))
            cfg = self.cfg
            be = ttk.LabelFrame(c, text=T("The service you want to use"), padding=8)
            be.pack(fill="x", pady=6)
            grid = ttk.Frame(be)
            grid.pack(fill="x")
            self.backend_var = tk.StringVar(value=str(
                cfg.get("shim_backend_url") or cfg.get("backend_url", "")))
            self.bmodel_var = tk.StringVar(value=cfg.get("backend_model", ""))
            self.apikey_var = tk.StringVar(value=cfg.get("api_key", ""))
            self.savekey_var = tk.BooleanVar(value=bool(cfg.get("save_api_key")))
            rows = (
                ("BaseURL:", self.backend_var, 46, None,
                 ("BaseURL", "backend_url", "base_url"),
                 T("Where your AI service lives, for example http://127.0.0.1:11434/v1 "
                   "(Ollama) or https://api.openai.com/v1.")),
                ("Model name:", self.bmodel_var, 26, None,
                 ("Model name", "backend_model", "model_name"),
                 T("The name the service expects, for example llama3.1:8b or "
                   "meta-llama/Llama-3.1-8B-Instruct.")),
                ("API key:", self.apikey_var, 30, "*",
                 ("API key", "api_key", "save key"),
                 T("Only needed by services that ask for one. It is handed to the translator "
                   "through the environment, never on a command line, and only saved if you "
                   "tick “Save API key”.")),
            )
            for i, (label, var, width, show, keys, fallback) in enumerate(rows):
                lab = ttk.Label(grid, text=T(label))
                lab.grid(row=i, column=0, sticky="w", pady=2)
                add_tip(lab, help_line(SHIM_HELP, keys, fallback))
                e = ttk.Entry(grid, textvariable=var, width=width, show=show)
                e.grid(row=i, column=1, sticky="we", padx=6, pady=2)
                add_tip(e, help_line(SHIM_HELP, keys, fallback))
                if show == "*":
                    e._is_key_entry = True
                    self.key_entry = e
            grid.columnconfigure(1, weight=1)
            opts = ttk.Frame(be)
            opts.pack(fill="x", pady=(6, 0))
            cb = ttk.Checkbutton(opts, text=T("Save API key in my config file"),
                                 variable=self.savekey_var)
            cb.pack(side="left")
            add_tip(cb, help_line(SHIM_HELP, ("save key", "save_api_key", "save_key"),
                                  T("Stores the key in a file only your user can read "
                                    "(permissions 600). Leave unticked to keep it in memory "
                                    "for this session only.")))
            self.showkey_var = tk.BooleanVar(value=False)
            cb2 = ttk.Checkbutton(opts, text=T("Show key"), variable=self.showkey_var,
                                  command=self._toggle_key)
            cb2.pack(side="left", padx=8)
            add_tip(cb2, T("Reveal the key while you type it. It is never written to the log."))
            b = ttk.Button(opts, text=T("Load from Bitwarden…"), command=self.load_bitwarden)
            b.pack(side="left", padx=4)
            add_tip(b, T("Pick a saved login from an unlocked Bitwarden vault. Needs the "
                         "official Bitwarden command line (bw)."))
            ex = ttk.Label(be, foreground=self.muted, wraplength=950, justify="left", text=T(
                "Examples —  Ollama: http://127.0.0.1:11434/v1    LM Studio: "
                "http://127.0.0.1:1234/v1    vLLM: http://127.0.0.1:8000/v1    OpenAI: "
                "https://api.openai.com/v1    OpenRouter: https://openrouter.ai/api/v1"))
            ex.pack(anchor="w", pady=(6, 0))
            add_tip(ex, T("Any service that speaks the OpenAI API works here."))

            sh = ttk.LabelFrame(c, text=T("The translator on this computer"), padding=8)
            sh.pack(fill="x", pady=6)
            row = ttk.Frame(sh)
            row.pack(fill="x")
            lab = ttk.Label(row, text=T("Listen:"))
            lab.pack(side="left")
            add_tip(lab, help_line(SHIM_HELP, ("listen/port", "listen", "shim_listen"),
                                   T("Which address the translator answers on. 127.0.0.1 means "
                                     "only this computer can reach it — leave it unless you have "
                                     "a reason not to.")))
            self.listen_var = tk.StringVar(value=cfg.get("shim_listen", "127.0.0.1"))
            le = ttk.Entry(row, textvariable=self.listen_var, width=15)
            le.pack(side="left", padx=4)
            add_tip(le, help_line(SHIM_HELP, ("listen/port", "shim_listen"),
                                  T("127.0.0.1 keeps it on this computer only.")))
            lab = ttk.Label(row, text=T("Port:"))
            lab.pack(side="left", padx=(8, 0))
            add_tip(lab, help_line(SHIM_HELP, ("listen/port", "shim_port"),
                                   T("The port the game connects to. Nothing else may already "
                                     "be using it.")))
            self.shimport_var = tk.StringVar(value=str(cfg.get("shim_port", 13333)))
            spe = ttk.Entry(row, textvariable=self.shimport_var, width=7)
            spe.pack(side="left", padx=4)
            add_tip(spe, help_line(SHIM_HELP, ("listen/port", "shim_port"),
                                   T("Default 13333. Apply points the game at this port.")))
            ml = ttk.Label(row, text=T("Requests:"))
            ml.pack(side="left", padx=(12, 0))
            add_tip(ml, help_line(SHIM_HELP, ("chat vs raw", "shim_mode", "chat_raw"),
                                  T("chat rebuilds a clean conversation for the service; raw "
                                    "forwards the game's prompt untouched.")))
            self.shimmode_var = tk.StringVar(value=cfg.get("shim_mode", "chat"))
            sc = ttk.Combobox(row, textvariable=self.shimmode_var, width=6, state="readonly",
                              values=("chat", "raw"))
            sc.pack(side="left", padx=4)
            add_tip(sc, help_line(SHIM_HELP, ("chat vs raw", "shim_mode", "chat_raw"),
                                  T("chat = the service sees a normal chat conversation; "
                                    "raw = it sees exactly what the game generated.")))
            self.strip_var = tk.BooleanVar(value=bool(cfg.get("strip_think", True)))
            st = ttk.Checkbutton(row, text=T("Hide “thinking” text"), variable=self.strip_var)
            st.pack(side="left", padx=10)
            add_tip(st, help_line(SHIM_HELP, ("strip think", "strip_think"),
                                  T("Some models narrate their reasoning inside <think> … "
                                    "</think>. Ticked, that text is removed before it reaches "
                                    "the game, so it cannot end up spoken aloud.")))
            self.insecure_var = tk.BooleanVar(value=False)
            it = ttk.Checkbutton(row, text=T("Skip TLS certificate check"),
                                 variable=self.insecure_var)
            it.pack(side="left")
            add_tip(it, help_line(SHIM_HELP, ("skip TLS verify", "insecure", "skip_tls"),
                                  T("Only for a self-signed certificate on your own machine or "
                                    "network. It disables an important safety check, so leave "
                                    "it unticked unless you need it.")))
            row2 = ttk.Frame(sh)
            row2.pack(fill="x", pady=(6, 0))
            self.shim_btn = ttk.Button(row2, text=T("Start"), command=self.shim_start)
            self.shim_btn.pack(side="left")
            self.action_buttons.append(self.shim_btn)
            add_tip(self.shim_btn, help_line(SHIM_HELP, ("start/stop/test shim", "start_stop_test"),
                                            T("Start the translator now. It keeps running after "
                                              "you close this window; the game connects to it "
                                              "while you play.")))
            b = ttk.Button(row2, text=T("Stop"), command=self.shim_stop)
            b.pack(side="left", padx=4)
            add_tip(b, T("Stop the translator."))
            b = ttk.Button(row2, text=T("Test"), command=self.shim_test)
            b.pack(side="left", padx=4)
            self.action_buttons.append(b)
            add_tip(b, T("Send one short test message through the translator to your service "
                         "and show the answer in the log."))
            b = ttk.Button(row2, text=T("Log"), command=self.shim_log)
            b.pack(side="left", padx=4)
            add_tip(b, T("Open the translator's own log — every request, answer and error."))
            self.shim_state = ttk.Label(row2, text=T("● stopped"), foreground=self.warn_fg)
            self.shim_state.pack(side="left", padx=12)
            add_tip(self.shim_state, T("Whether the translator is running and answering."))

            srv = ttk.LabelFrame(c, text=T("The other direction: let other programs use the "
                                           "game's model"), padding=8)
            srv.pack(fill="x", pady=6)
            self.expose_var = tk.BooleanVar(value=bool(cfg.get("expose_server")))
            ec = ttk.Checkbutton(srv, variable=self.expose_var, text=T(
                "Turn the game into a small AI server other programs on this computer can ask"))
            ec.pack(anchor="w")
            add_tip(ec, help_line(SHIM_HELP, ("expose-game-as-server", "expose_server", "reverse"),
                                  T("Useful for experiments: another program can send prompts to "
                                    "the model the game has loaded. Do not use the same port as "
                                    "the translator.")))
            row3 = ttk.Frame(srv)
            row3.pack(fill="x", pady=4)
            sp = ttk.Label(row3, text=T("Port:"))
            sp.pack(side="left")
            add_tip(sp, help_line(SHIM_HELP, ("server_port", "expose_port"),
                                  T("The port the game listens on when the box above is ticked.")))
            self.serverport_var = tk.StringVar(value=str(cfg.get("server_port", 13333)))
            spx = ttk.Entry(row3, textvariable=self.serverport_var, width=7)
            spx.pack(side="left", padx=4)
            add_tip(spx, T("Pick something free, and not the translator's port."))
            note = ttk.Label(srv, foreground=self.muted, wraplength=950, justify="left", text=T(
                "Off by default. The game answers the usual AI-server requests, so tools on this "
                "computer can borrow the model the game already loaded."))
            note.pack(anchor="w")
            add_tip(note, help_line(SHIM_HELP, ("expose-game-as-server", "expose_server"),
                                    T("One setting in the game's engine block; it is switched "
                                      "back off when you untick the box and Apply.")))
            self._build_apply_row(c, "remote",
                                  T("Apply points the game at the translator; Start runs it."))
        # ---------- tab: characters ---------- #
        def _tab_characters(self):
            f = ttk.Frame(self.nb)
            self.tab_frames["characters"] = f
            c = self._scrolled(f)
            head = ttk.Label(c, text=T("Character personalities"),
                             font=("TkDefaultFont", 14, "bold"))
            head.pack(anchor="w")
            add_tip(head, T("How each character writes its lines: how creative, how repetitive, "
                            "how long, and whether the same choices are made every time."))
            intro = (_as_text(getattr(cast, "FRIENDLY_GROUP_INTRO", None))
                     or T(FRIENDLY_GROUP_INTRO_FALLBACK))
            il = ttk.Label(c, text=intro, foreground=self.muted, wraplength=980,
                           justify="left")
            il.pack(anchor="w", pady=(2, 4))
            add_tip(il, intro)
            ghelp = help_line(GROUP_HELP, ("intro", "overview", "groups"), "")
            if ghelp and ghelp != intro:
                gl = ttk.Label(c, text=ghelp, foreground=self.muted, wraplength=980,
                               justify="left")
                gl.pack(anchor="w", pady=(0, 6))
                add_tip(gl, ghelp)

            pick = ttk.Frame(c)
            pick.pack(fill="x")
            gl = ttk.Label(pick, text=T("Which characters?"))
            gl.pack(side="left")
            add_tip(gl, T("The groups are the places in town where you meet each character, "
                          "plus one group for your own Workshop stories. Pick one to change "
                          "just those characters, or keep “All characters” to change everyone "
                          "at once."))
            self.group_choices = group_choices()
            self.group_var = tk.StringVar(value=group_label(self.current_group))
            self.group_combo = ttk.Combobox(pick, textvariable=self.group_var, width=34,
                                            state="readonly",
                                            values=[lab for _k, lab in self.group_choices])
            self.group_combo.pack(side="left", padx=6)
            self.group_combo.bind("<<ComboboxSelected>>", lambda _e: self._group_changed())
            add_tip(self.group_combo, T("The names and character counts come straight from the "
                                        "game. Each group's settings live in their own game "
                                        "files, which is what makes per-group editing "
                                        "possible."))
            bl = ttk.Button(pick, text=T("Load current values"), command=self.load_values)
            bl.pack(side="left", padx=6)
            self.action_buttons.append(bl)
            add_tip(bl, T("Read what the first character of this group uses right now and put "
                          "it in the boxes below."))
            bc = ttk.Button(pick, text=T("Clear boxes"), command=self.clear_values)
            bc.pack(side="left")
            add_tip(bc, T("Empty every box. Empty boxes are left exactly as they are."))

            self.scope_var = tk.StringVar(value="")
            scope = ttk.Label(c, textvariable=self.scope_var, foreground=self.muted,
                              wraplength=980, justify="left")
            scope.pack(anchor="w", fill="x", pady=(8, 2))
            self.scope_tip = add_tip(scope, "")

            details = ttk.Frame(c)
            details.pack(fill="x", pady=(2, 0))
            self._expander(details, T("About these groups"), self._groups_detail_text())
            self._expander(details, T("Technical evidence (research notes)"),
                           self._groups_evidence_text(), side="left")
            self._expander(details, T("Settings this tool cannot change"),
                           self._locked_detail_text(), side="left")

            box = ttk.LabelFrame(c, text=T("Settings"), padding=8)
            box.pack(fill="both", expand=True, pady=6)
            blank = ttk.Label(box, foreground=self.muted, wraplength=950, justify="left",
                              text=T("Empty box = leave that setting alone. Hover any name for "
                                     "what it does, its range, and tips. Who a character is and "
                                     "how they speak comes from the game's own script data and "
                                     "cannot be edited here — these are the word-choice "
                                     "settings only."))
            blank.pack(anchor="w", pady=(0, 6))
            add_tip(blank, blank.cget("text"))
            gridf = ttk.Frame(box)
            gridf.pack(fill="x")
            self._field_grid(gridf, CHARACTER_FIELDS, self.agent_vars, "AGENT", columns=3)

            btns = ttk.Frame(c)
            btns.pack(fill="x", pady=(6, 0))
            b = ttk.Button(btns, text=T("Reset this group to game defaults"),
                           command=lambda: self.reset_defaults(False))
            b.pack(side="left")
            self.action_buttons.append(b)
            add_tip(b, T("Fill the boxes with the values the game shipped with, for the group "
                         "selected above. Nothing is written until you press Apply."))
            b = ttk.Button(btns, text=T("Reset ALL groups to game defaults"),
                           command=lambda: self.reset_defaults(True))
            b.pack(side="left", padx=6)
            self.action_buttons.append(b)
            add_tip(b, T("Fill the boxes with the shipped values and apply them to every "
                         "character. Nothing is written until you press Apply."))
            self.reset_hint = ttk.Label(btns, foreground=self.muted, wraplength=620,
                                        justify="left", text="")
            self.reset_hint.pack(side="left", padx=10)
            add_tip(self.reset_hint, T("The shipped values are the ones a fresh install has."))
            self._build_apply_row(c, "characters",
                                  T("Only the characters in the selected group are changed. "
                                    "The connection details go to every character, so the "
                                    "game is never left half-wired."))
            self._update_group_scope()

        def _expander(self, parent, label: str, text: str, side: str = "left"):
            """A small "show/hide the detail" toggle for long background text."""
            if not text:
                return None
            state = {"on": False}
            body = ttk.Frame(parent)
            txt = tk.Text(body, height=min(14, max(4, text.count("\n") + 2)), wrap="word",
                          relief="flat", background=self._mix(self.fg, self.bg, 0.06),
                          foreground=self.fg)
            txt.pack(side="left", fill="both", expand=True)
            txt.insert("end", text)
            txt.configure(state="disabled")
            sb = ttk.Scrollbar(body, orient="vertical", command=txt.yview)
            sb.pack(side="right", fill="y")
            txt.configure(yscrollcommand=sb.set)

            def flip():
                state["on"] = not state["on"]
                if state["on"]:
                    body.pack(fill="x", pady=(4, 4))
                    btn.configure(text="▾ " + label)
                else:
                    body.pack_forget()
                    btn.configure(text="▸ " + label)
            btn = ttk.Button(parent, text="▸ " + label, command=flip)
            btn.pack(side=side, padx=(0, 6))
            add_tip(btn, T("Show or hide the background detail."))
            return btn

        def _groups_detail_text(self) -> str:
            """Friendly prose about the cast groups — no engine internals."""
            return help_line(GROUP_HELP, ("about", "groups", "intro"), "")

        def _groups_evidence_text(self) -> str:
            """The research trail behind the group list, for the curious.

            Kept behind its own clearly-technical toggle: it names Unity and
            IL2CPP internals, which is good evidence and poor onboarding."""
            ev = evidence_text()
            return (T("Where this list comes from:") + "\n" + ev) if ev else ""

        def _locked_detail_text(self) -> str:
            lines = [T("These look editable in other tools, but changing them here would do "
                       "nothing or cannot be stored at all:")]
            for name, why in INERT_FIELDS.items():
                lines.append("· " + name + " — " + T(why) + ".")
            lines.append(T("· Text fields that ship empty (%s) have no room reserved for any "
                           "text at all, so they cannot hold a value. That is why the API key "
                           "lives in Remote Mode: the translator holds it, and the game keeps "
                           "talking to the translator.") % ", ".join(EMPTY_TEXT_FIELDS))
            return "\n".join(lines)

        def _field_grid(self, parent, fields, varmap, kind, columns=2):
            rows = max(1, -(-len(fields) // columns))
            for i, name in enumerate(fields):
                col, row = divmod(i, rows)
                entry = SETTING_HELP.get(name)
                label = name
                if isinstance(entry, dict) and _as_text(entry.get("label")):
                    label = _as_text(entry.get("label"))
                lab = ttk.Label(parent, text=str(label) + ":", anchor="w")
                lab.grid(row=row, column=col * 2, sticky="w",
                         padx=(0 if col == 0 else 22, 4), pady=3)
                var = tk.StringVar()
                ent = ttk.Entry(parent, textvariable=var, width=11)
                ent.grid(row=row, column=col * 2 + 1, sticky="w", pady=3)
                varmap[name] = var
                tip = setting_tip(name, self._field_note(kind, name))
                tech = T("Field name in the game files: ") + name
                if name in INERT_FIELDS:
                    tech += "\n" + T("Not editable: ") + T(INERT_FIELDS[name])
                for w in (lab, ent):
                    add_tip(w, tip + "\n" + tech)

        def _field_note(self, kind, name) -> str:
            table = AGENT_FIELDS_GUI if kind == "AGENT" else LLM_FIELDS_GUI
            for n, _k, note in table:
                if n == name:
                    return note or ""
            for n, _k, note in (LLM_FIELDS_GUI if kind == "AGENT" else AGENT_FIELDS_GUI):
                if n == name:
                    return note or ""
            return ""

        def _select_group(self, key):
            key = normalize_group(key) or all_group_key()
            self.current_group = key
            self.cfg["cast_group"] = key
            label = group_label(key)
            self.group_var.set(label)
            self._update_group_scope()

        def _group_changed(self):
            label = self.group_combo.get().strip()
            key = next((k for k, lab in self.group_choices if lab == label), all_group_key())
            self._select_group(key)
            self.status(T("Editing: ") + group_label(key))
            self.log(T("Character group: ") + group_label(key) + " — " +
                     T("press “Load current values” to see what this group uses now."))

        def _update_group_scope(self):
            key = self.current_group
            label = group_label(key)
            lines = [T("Editing: ") + label]
            names = group_characters(key)
            if names:
                shown = ", ".join(names[:16]) + (T(", and more") if len(names) > 16 else "")
                lines.append(T("In this group: ") + shown)
            if self.scan is not None:
                hits = group_blobs(self.scan, key)
                scope_word = (T(" across every character.") if normalize_group(key) is None
                              else T(" in this group."))
                lines.append(T("Settings found for ") +
                             friendly_count(len(hits), "character", "characters") + scope_word)
                if normalize_group(key) is not None and not hits:
                    lines.append(T("Nothing matched this group in the last scan — press "
                                   "Re-scan, or choose “All characters”."))
            else:
                lines.append(T("The game files have not been read yet — press Re-scan to see "
                               "how many characters this covers."))
            note = group_note(key)
            if note:
                lines.append(note)
            self.scope_var.set("\n".join(lines))
            if self.scope_tip:
                self.scope_tip.set_text(
                    T("Edits are written only to the characters of this group; every other "
                      "character keeps its current settings.") +
                    "\n" + T("Under the hood: each group's characters live in their own game "
                             "asset files, which is what makes per-group edits possible."))

        def reset_defaults(self, scope_all: bool = False):
            if scope_all:
                self._select_group(all_group_key())
            for name, var in self.agent_vars.items():
                if name in STOCK_CHARACTER_VALUES:
                    var.set(STOCK_CHARACTER_VALUES[name])
            # the shipped values of the three fixed fields the grid does not expose
            self.extra_overrides = dict(STOCK_EXTRA_VALUES)
            label = group_label(self.current_group)
            self.reset_hint.configure(
                text=T("Shipped values are in the boxes for ") + label +
                T(" — Preview shows the difference, Apply writes them."))
            self.status(T("Game defaults loaded for ") + label)
            self.log(T("Put the shipped values into the form for ") + label + ". " +
                     T("Preview shows what would change; Apply writes them, backup first."))
            self.preview()

        # ---------- tab: backup ---------- #
        def _tab_backup(self):
            f = ttk.Frame(self.nb)
            self.tab_frames["backup"] = f
            c = self._scrolled(f)
            head = ttk.Label(c, text=T("Backups and undo"), font=("TkDefaultFont", 14, "bold"))
            head.pack(anchor="w")
            sub = ttk.Label(c, foreground=self.muted, wraplength=980, justify="left", text=T(
                "Every change makes a checked backup of the game files first, stored outside the "
                "game folder. Restoring puts the original bytes back. Steam's “verify integrity "
                "of game files” is another way back to a fresh install."))
            sub.pack(anchor="w", pady=(2, 8))
            add_tip(sub, T("Backups are kept until you remove them; the newest one is at the "
                           "top of the list."))
            self.bk_tree = ttk.Treeview(c, columns=("when", "files", "game", "labels"),
                                        show="headings", height=12)
            for col, w, t in (("when", 170, T("When")), ("files", 60, T("Files")),
                              ("game", 300, T("Game folder")),
                              ("labels", 380, T("What changed"))):
                self.bk_tree.heading(col, text=t)
                self.bk_tree.column(col, width=w, anchor="w")
            self.bk_tree.pack(fill="both", expand=True)
            add_tip(self.bk_tree, T("One row per backup. Select a row, then Restore to put those "
                                    "files back."))
            row = ttk.Frame(c)
            row.pack(fill="x", pady=6)
            b = ttk.Button(row, text=T("Refresh"), command=self.refresh_backups)
            b.pack(side="left")
            add_tip(b, T("Re-read the backup folder."))
            b = ttk.Button(row, text=T("Restore selected"), command=self.restore_selected)
            b.pack(side="left", padx=6)
            self.action_buttons.append(b)
            add_tip(b, T("Put the selected backup's files back, byte for byte. The game must be "
                         "closed."))
            b = ttk.Button(row, text=T("Open backup folder"), command=self.open_backups)
            b.pack(side="left")
            add_tip(b, T("Show the backups in your file manager."))
            self._build_footer(c)
            self.refresh_backups()
        # ---------- actions: game folder ---------- #
        def browse(self):
            d = filedialog.askdirectory(title=T("Choose the Vaudeville game folder"))
            if d:
                self.dir_var.set(d)
                self.rescan()

        def detect(self):
            found = find_game_dirs()
            if not found:
                self.info_var.set(T("Game not found — press Browse… and choose the Vaudeville "
                                    "folder yourself (it contains a Vaudeville_Data folder)."))
                self.status(T("game not found"))
                messagebox.showwarning(APP_NAME, T("No Vaudeville install found."))
                return
            for p, src in found:
                self.log(f"detected {p}  ({src})")
            steam = [p for p, s in found if "appmanifest" in s or "steamapps" in s]
            pick = (steam or [p for p, _ in found])[0]
            self.dir_var.set(str(pick))
            self.rescan()

        def rescan(self):
            gd = self.game_dir()
            if gd is None:
                self.info_var.set(T("Game not found — set the game folder first."))
                messagebox.showwarning(APP_NAME, T("Set the game folder first."))
                return

            def work():
                return scan_game(gd, use_profile=False)

            def done(res):
                self.scan = res
                self.info_var.set(self._scan_text(gd, res))
                if self.info_tip:
                    self.info_tip.set_text(T("Where the game was found, which build it is, and "
                                             "how many character and engine settings this tool "
                                             "can see.") + "\n" + res.friendly_summary())
                for n in res.notes:
                    self.log("note: " + n)
                self.load_values()
                self.refresh_models()
                self._update_group_scope()
                self._update_gpu_line()
                self._check_host_fit()
                self.status(T("Read ") +
                            friendly_count(len(res.agents), "character", "characters") +
                            T(" and ") +
                            friendly_count(len(res.llm), "engine setting block",
                                           "engine setting blocks"))
                self.log(T("Scan complete: ") + res.friendly_summary())
                log("scan detail: " + res.summary())
            self.run(work, done, T("reading the game files…"))

        def _scan_text(self, gd: Path, res: ScanResult) -> str:
            lines = [T("Game found: ") + str(gd),
                     T("Build: ") + build_guid(gd)]
            if res.agents or res.llm:
                lines.append(T("Ready to edit: settings for ") +
                             friendly_count(len(res.agents), "character", "characters") +
                             T(" and ") +
                             friendly_count(len(res.llm), "AI engine block", "AI engine blocks")
                             + ".")
            else:
                lines.append(T("No AI settings found in this folder — is it the Vaudeville "
                               "install?"))
            if res.notes:
                lines.append("Worth knowing: " + "; ".join(res.notes[:2]))
            return "\n".join(lines)

        def _initial_scan(self):
            if not self.dir_var.get().strip():
                self.detect()
            else:
                self.rescan()

        def _poll_running(self):
            gd = self.game_dir()
            txt = ""
            if gd and gd.is_dir():
                pids = game_is_running(gd)
                if pids:
                    txt = T("⚠ the game is RUNNING — close it before applying changes")
            self.running_lbl.configure(text=txt)
            self.after(3000, self._poll_running)

        # ---------- actions: models ---------- #
        def refresh_models(self):
            gd = self.game_dir()
            if gd is None:
                for panel in self.model_panels:
                    panel["lib_var"].set(T("Model folder: available once the game is found."))
                return
            models = list_library_models(gd)
            names = [str(p) for p in models]
            family = [n for n in names if is_llama3_8b_family(Path(n).name)]
            for panel in self.model_panels:
                panel["lib_var"].set(
                    T("Model folder: ") + f"{library_dir(gd)}   (" +
                    friendly_count(len(models), "model file", "model files") +
                    (f", {len(family)} " + T("in the allowed family")
                     if panel["restrict"] else "") + ")")
                warns = []
                allowed = family if panel["restrict"] else names
                for slot in model_slots():
                    cur, combo = panel["rows"][slot.name]
                    combo["values"] = allowed
                    st = slot_status(gd, slot)
                    tgt = st["target"] or "-"
                    kind = T("a file you chose") if st["is_link"] else T("the shipped file")
                    cur.configure(text=T("in use now: ") +
                                  f"{tgt}  ({human_size(st['size'])}, {kind}" +
                                  (", " + T("BROKEN LINK") if st["broken"] else "") + ")")
                    tip = panel["cur_tips"].get(slot.name)
                    if tip is not None:
                        tip.set_text(T("What the game loads for ") + slot.label + "." +
                                     "\n" + T("Under the hood: the file name ") + slot.name +
                                     T(" inside the game's StreamingAssets folder."))
                    preset = self.cfg.get("primary_model"
                                          if slot.name == PRIMARY_MODEL_NAME else "deck_model")
                    if preset and preset in allowed:
                        combo.set(preset)
                    elif st["target"]:
                        combo.set(st["target"])
                    panel["last"][slot.name] = combo.get()
                    if panel["restrict"] and st["target"] and \
                            not is_llama3_8b_family(Path(st["target"]).name):
                        if slot.name == DECK_MODEL_NAME and \
                                Path(st["target"]).name == DECK_MODEL_NAME:
                            pass  # the shipped Steam Deck fallback: this setup leaves it alone
                        elif slot.name == DECK_MODEL_NAME:
                            warns.append(T("“%s” is linked into the Steam Deck / fallback slot "
                                           "and is not from the %s family. %s leaves that slot "
                                           "alone, but the game may still load it on a handheld.")
                                         % (Path(st["target"]).name, LLAMA3_8B_FAMILY,
                                            mode_label(MODE_OFF)))
                        else:
                            warns.append(T("“%s” is not from the %s family, so %s cannot use it — "
                                           "choose another model here, or use a different setup.")
                                         % (Path(st["target"]).name, LLAMA3_8B_FAMILY,
                                            mode_label(MODE_OFF)))
                if panel["warn"] is not None:
                    panel["warn"].configure(text="\n".join(warns))

        def pick_model(self, panel, combo, slot):
            f = filedialog.askopenfilename(title=T("Choose a model file"),
                                           filetypes=[(T("Model files"), "*.gguf"),
                                                      (T("All files"), "*")])
            if f:
                combo.set(f)
                self._model_chosen(panel, combo, slot, modal=True)

        def _model_allowed(self, panel, filename: str, modal: bool) -> bool:
            if not panel["restrict"] or is_llama3_8b_family(Path(filename).name):
                return True
            card = MODE_CARDS[MODE_OFF]
            restriction = str(card.get("restrictions") or "")
            msg = (T("“%s” cannot be used in %s.") % (Path(filename).name,
                                                      mode_label(MODE_OFF)) +
                   "\n\n" + T("Restriction: ") + restriction +
                   "\n\n" + T("Nothing was changed. Choose a model from the %s family, or "
                              "switch setup on the Home tab to use any model.")
                   % LLAMA3_8B_FAMILY)
            self.log(T("Refused: ") + msg.replace("\n", " "))
            if panel["warn"] is not None:
                panel["warn"].configure(text=T("Refused: only the %s family works in %s.")
                                        % (LLAMA3_8B_FAMILY, mode_label(MODE_OFF)))
            if modal:
                messagebox.showwarning(APP_NAME, msg)
            return False

        def _model_chosen(self, panel, combo, slot, modal: bool):
            value = combo.get().strip()
            if not value:
                return
            if not self._model_allowed(panel, value, modal):
                combo.set(panel["last"].get(slot.name, ""))
                return
            panel["last"][slot.name] = value
            if panel["warn"] is not None:
                panel["warn"].configure(text="")
            self.status(T("Model chosen for ") + slot.label +
                        T(" — press “Use this model”."))

        def apply_model(self, panel, slot):
            gd = self.game_dir()
            _cur, combo = panel["rows"][slot.name]
            target = combo.get().strip()
            if gd is None or not target:
                messagebox.showwarning(APP_NAME, T("Choose a game folder and a model file first."))
                return
            if not self._model_allowed(panel, target, modal=True):
                return

            def work():
                return set_slot_model(gd, slot, Path(target))

            def done(lines):
                for line in lines:
                    self.log(line)
                key = "primary_model" if slot.name == PRIMARY_MODEL_NAME else "deck_model"
                self.cfg[key] = target
                self.refresh_models()
                self._save_cfg()
                self.status(T("The game now loads ") + Path(target).name +
                            T(" for ") + slot.label)
            self.run(work, done, T("switching model…"))

        def open_library(self):
            gd = self.game_dir()
            if gd is None:
                return
            d = library_dir(gd)
            d.mkdir(parents=True, exist_ok=True)
            open_in_folder(d)

        # ---------- actions: remote service ---------- #
        def _toggle_key(self):
            try:
                self.key_entry.configure(show="" if self.showkey_var.get() else "*")
            except Exception:                                # noqa: BLE001
                pass

        def load_bitwarden(self):
            if shutil.which("bw") is None:
                messagebox.showerror(APP_NAME, T("The Bitwarden command line (bw) was not found. "
                                                 "Install it, or type the key in by hand."))
                return
            try:
                status = subprocess.run(["bw", "status"], capture_output=True, text=True,
                                        timeout=20)
                info = json.loads(status.stdout or "{}")
            except Exception as exc:                         # noqa: BLE001
                messagebox.showerror(APP_NAME, T("Could not read the Bitwarden status: ") + str(exc))
                return
            if info.get("status") != "unlocked":
                messagebox.showwarning(APP_NAME, T("Your Bitwarden vault is locked; run "
                                                   "“bw unlock” first."))
                return
            try:
                items = subprocess.run(["bw", "list", "items", "--search", "llm"],
                                       capture_output=True, text=True, timeout=60)
                data = json.loads(items.stdout or "[]")
            except Exception as exc:                         # noqa: BLE001
                messagebox.showerror(APP_NAME, T("Could not list Bitwarden items: ") + str(exc))
                return
            names = [f"{i.get('name')}  [{i.get('id', '')[:8]}]" for i in data if i.get("name")]
            if not names:
                messagebox.showinfo(APP_NAME, T("No Bitwarden items matching “llm”."))
                return
            win = tk.Toplevel(self)
            win.title(T("Pick a saved key"))
            win.geometry("460x320")
            tk.Label(win, text=T("Keys are never written to the log."), anchor="w",
                     padx=8, pady=(8, 0)).pack(fill="x")
            lb = tk.Listbox(win)
            lb.pack(fill="both", expand=True, padx=8, pady=8)
            for n in names:
                lb.insert("end", n)

            def take():
                sel = lb.curselection()
                if not sel:
                    return
                item = data[sel[0]]
                secret = ((item.get("login") or {}).get("password")) or ""
                if not secret:
                    for fld in item.get("fields") or []:
                        if fld.get("type") == 1:
                            secret = fld.get("value") or ""
                            break
                if secret:
                    self.apikey_var.set(secret)
                    self.log(T("Loaded an API key from Bitwarden item “%s” (value not logged).")
                             % item.get("name"))
                else:
                    self.log(T("That Bitwarden item has no password or hidden field."))
                win.destroy()
            ok = ttk.Button(win, text=T("Use this key"), command=take)
            ok.pack(pady=(0, 8))
            add_tip(ok, T("Copy the selected key into the API key box."))

        def shim_cfg(self) -> dict:
            return {"listen": self.listen_var.get().strip() or "127.0.0.1",
                    "port": self._int_or(self.shimport_var, 13333, T("Translator port")),
                    "mode": self.shimmode_var.get(),
                    "backend_url": self.backend_var.get().strip(),
                    "backend_model": self.bmodel_var.get().strip(),
                    "api_key": self.apikey_var.get(),
                    "strip_think": bool(self.strip_var.get()),
                    "insecure": bool(self.insecure_var.get()),
                    "verbose": True, "embeddings": False, "timeout": 600}

        def shim_start(self):
            sp = ShimProcess(self.shim_cfg())

            def work():
                return sp.start()

            def done(res):
                ok, msg = res
                self.log(T(msg))
                self._update_shim_state(sp)
                if ok:
                    self._save_cfg()
            self.run(work, done, T("starting the translator…"))

        def shim_stop(self):
            sp = ShimProcess(self.shim_cfg())

            def work():
                return sp.stop()

            def done(msg):
                self.log(T(msg))
                self._update_shim_state(sp)
            self.run(work, done, T("stopping the translator…"))

        def shim_test(self):
            sp = ShimProcess(self.shim_cfg())

            def work():
                return sp.test_completion("Say hello in three words.")

            def done(res):
                ok, text = res
                self.log((T("Test worked: ") if ok else T("Test failed: ")) + text)
                if not ok:
                    self.log(sp.tail(20))
            self.run(work, done, T("testing game → translator → service…"))

        def shim_log(self):
            sp = ShimProcess(self.shim_cfg())
            win = tk.Toplevel(self)
            win.title(T("Translator log — ") + str(SHIM_LOG_FILE))
            win.geometry("860x480")
            t = tk.Text(win, wrap="word", background="#111", foreground="#cfc")
            t.pack(fill="both", expand=True)
            t.insert("end", sp.tail(400))
            t.see("end")

        def _update_shim_state(self, sp: ShimProcess):
            pid = sp.pid()
            ok, _ = sp.health() if pid else (False, "")
            if pid and ok:
                self.shim_state.configure(text=T("● running (pid %s)") % pid,
                                          foreground=self.ok_fg)
            elif pid:
                self.shim_state.configure(text=T("● started but not answering (pid %s)") % pid,
                                          foreground="#c80")
            else:
                self.shim_state.configure(text=T("● stopped"), foreground=self.warn_fg)
        # ---------- actions: settings ---------- #
        def _collect_config(self) -> dict:
            cfg = dict(self.cfg)
            mode = normalize_mode(self.chosen_mode or self.mode_pick_var.get(), MODE_OFF)
            shim_url = self.backend_var.get().strip()
            direct_host = self.direct_host_var.get().strip()
            cfg.update({
                "game_dir": self.dir_var.get().strip(),
                "mode": mode,
                "mode_confirmed": bool(self.chosen_mode),
                # build_change_set() reads backend_url; in Local Mode - Advanced that is the
                # plain server address, otherwise it is the OpenAI-compatible BaseURL.
                "backend_url": direct_host if mode == MODE_DIRECT else shim_url,
                "shim_backend_url": shim_url,
                "direct_host": direct_host,
                "direct_port": self._int_or(self.direct_port_var, 0, T("Server port")),
                "backend_model": self.bmodel_var.get().strip(),
                "save_api_key": bool(self.savekey_var.get()),
                "api_key": self.apikey_var.get() if self.savekey_var.get() else cfg.get("api_key", ""),
                "shim_listen": self.listen_var.get().strip(),
                "shim_port": self._int_or(self.shimport_var, 13333, T("Translator port")),
                "shim_mode": self.shimmode_var.get(),
                "strip_think": bool(self.strip_var.get()),
                "expose_server": bool(self.expose_var.get()),
                "server_port": self._int_or(self.serverport_var, 13333, T("Server port")),
                "cast_group": self.current_group,
            })
            if not cfg.get("save_api_key"):
                cfg["api_key"] = ""
            return cfg

        def _plan_group(self):
            """Group the pending character edits apply to (None = every character)."""
            key = normalize_group(self.current_group)
            if key is None:
                return None
            if not group_known(key):
                self.log(T("Character group “%s” is unknown here, so the edits will go to every "
                           "character.") % key)
                return None
            return key

        def _overrides(self) -> dict:
            out = {}
            for name, var in self.agent_vars.items():
                txt = var.get().strip()
                if txt == "":
                    continue
                try:
                    out[name] = coerce_for("AGENT", name, txt)
                except Exception as exc:                     # noqa: BLE001
                    self.log(T("Ignoring “%s” — %s") % (name, exc))
            for name, var in self.llm_vars.items():
                txt = var.get().strip()
                if txt == "":
                    continue
                try:
                    out[name] = coerce_for("LLM", name, txt)
                except Exception as exc:                     # noqa: BLE001
                    self.log(T("Ignoring “%s” — %s") % (name, exc))
            for name, value in self.extra_overrides.items():
                out.setdefault(name, value)
            return out

        def _plan(self):
            if self.scan is None:
                raise PatchError(T("Read the game files first (Re-scan button at the top)."))
            cfg = self._collect_config()
            self.cfg = cfg
            group = self._plan_group()
            agent_changes, llm_changes, notes = build_change_set(cfg, self.scan,
                                                                self._overrides(), group=group)
            edits, warnings = plan_edits(self.scan, agent_changes, llm_changes,
                                        agent_group=group)
            return edits, warnings, notes, agent_changes, llm_changes, group

        def _friendly_edit_label(self, label: str) -> str:
            out = str(label)
            if out.startswith("AGENT."):
                out = T("character ") + out[len("AGENT."):]
            elif out.startswith("LLM."):
                out = T("engine ") + out[len("LLM."):]
            return T(out)

        def _show_plan(self, edits, warnings, notes, group=None):
            mode = mode_label(normalize_mode(self.cfg.get("mode"), MODE_OFF))
            head = [T("Setup: ") + mode,
                    T("Characters: ") + group_label(group)]
            for box in self.plan_boxes.values():
                box.configure(state="normal")
                box.delete("1.0", "end")
                box.insert("end", " · ".join(head) + "\n")
                for n in notes:
                    box.insert("end", "• " + n + "\n")
                for w in warnings:
                    box.insert("end", "⚠ " + w + "\n")
                box.insert("end", "\n" + T("Changes: ") +
                           friendly_count(len(edits), "value", "values") + "\n")
                for e in edits:
                    box.insert("end", "  " + self._friendly_edit_label(e.label) + "\n")
                if not edits:
                    box.insert("end", "  " + T("(nothing to change — the game already has "
                                              "these values)") + "\n")
                box.configure(state="disabled")

        def preview(self):
            def work():
                return self._plan()

            def done(res):
                edits, warnings, notes, _ag, _llm, group = res
                self._show_plan(edits, warnings, notes, group)
                self.log(T("Preview: ") + friendly_count(len(edits), "change", "changes") +
                         ", " + friendly_count(len(warnings), "warning", "warnings"))
                self.status(friendly_count(len(edits), "change", "changes") + T(" planned"))
            self.run(work, done, T("working out the changes…"))

        def apply_changes(self):
            def work():
                return self._plan()

            def done(res):
                edits, warnings, notes, _ag, _llm, group = res
                self._show_plan(edits, warnings, notes, group)
                if not edits:
                    messagebox.showinfo(APP_NAME, T("Nothing to change."))
                    return
                gd = self.game_dir()
                ask = (T("Write %s to the game files in") %
                       friendly_count(len(edits), "change", "changes") + f"\n{gd}\n\n" +
                       T("Setup: ") + mode_label(normalize_mode(self.cfg.get("mode"), MODE_OFF)) +
                       "\n" + T("Characters: ") + group_label(group) + "\n\n" +
                       T("A checked backup is made first, and the game must be closed.") +
                       "\n" + T("Continue?"))
                if not messagebox.askyesno(APP_NAME, ask):
                    return

                def work2():
                    rec = apply_edits(edits, gd)
                    return rec, scan_game(gd, use_profile=False)

                def done2(res2):
                    rec, res2 = res2
                    self.scan = res2
                    self.info_var.set(self._scan_text(gd, res2))
                    self.log(T("Wrote %s; backup at %s") % (len(edits), rec.dir))
                    self.log(T("Checked afterwards: ") + res2.friendly_summary())
                    log("post-write detail: " + res2.summary())
                    self.extra_overrides = {}
                    self.load_values()
                    self._update_group_scope()
                    self._update_gpu_line()
                    self._save_cfg()
                    self.refresh_backups()
                    messagebox.showinfo(APP_NAME, T("Done — the game will use the new settings "
                                                    "next time it starts.") +
                                        f"\n\n{T('Backup:')} {rec.dir}")
                self.run(work2, done2, T("writing game files…"))
            self.run(work, done, T("working out the changes…"))

        def load_values(self):
            if self.scan is None:
                return
            key = self.current_group
            agents = group_blobs(self.scan, key)
            agent_kinds = dict(AGENT_LAYOUT)
            llm_kinds = dict(LLM_LAYOUT)
            uniform = len({json.dumps(b.values, default=str, sort_keys=True)
                           for b in agents}) <= 1
            a = agents[0].values if agents else {}
            l = self.scan.llm[0].values if self.scan.llm else {}
            for name, var in self.agent_vars.items():
                if name not in a:
                    continue
                v = a[name]
                if agent_kinds.get(name) == "b":
                    v = "true" if v else "false"
                elif isinstance(v, float):
                    v = f"{v:g}"
                var.set(str(v))
            for name, var in self.llm_vars.items():
                if name not in l:
                    continue
                v = l[name]
                if llm_kinds.get(name) == "b":
                    v = "true" if v else "false"
                elif isinstance(v, float):
                    v = f"{v:g}"
                var.set(str(v))
            label = group_label(key)
            if not agents:
                self.log(T("No character settings found for %s — press Re-scan or choose "
                           "“All characters”.") % label)
            elif not uniform and normalize_group(key) is None:
                self.log(T("The characters do not all use the same values; showing the first "
                           "one. Your edits are written to every character."))
            elif not uniform:
                self.log(T("The characters in %s do not all use the same values; showing the "
                           "first one. Your edits are written to every character in this "
                           "group.") % label)
            else:
                self.log(T("Loaded the current values for %s.") % label)
            self.status(T("Loaded the current values"))

        def clear_values(self):
            for var in list(self.agent_vars.values()) + list(self.llm_vars.values()):
                var.set("")
            self.extra_overrides = {}
            self.status(T("All boxes cleared — nothing will be changed"))

        # ---------- actions: backups ---------- #
        def refresh_backups(self):
            self.bk_tree.delete(*self.bk_tree.get_children())
            self._backups = list_backups()
            for rec in self._backups:
                m = rec.manifest
                self.bk_tree.insert("", "end", iid=str(rec.dir), values=(
                    m.get("timestamp", rec.dir.name), len(m.get("files", [])),
                    m.get("game_dir", ""), "; ".join(m.get("labels", []))[:120]))

        def restore_selected(self):
            sel = self.bk_tree.selection()
            if not sel:
                messagebox.showinfo(APP_NAME, T("Select a backup first."))
                return
            rec = next((b for b in self._backups if str(b.dir) == sel[0]), None)
            if rec is None:
                return
            if not messagebox.askyesno(
                    APP_NAME,
                    T("Put these %s back over the game files?") %
                    friendly_count(len(rec.manifest["files"]), "file", "files") +
                    f"\n{rec.dir}\n\n" + T("The game must be closed.")):
                return

            def work():
                return restore_backup(rec, self.game_dir())

            def done(lines):
                for line in lines:
                    self.log(line)
                self.rescan()
                messagebox.showinfo(APP_NAME, T("Restore finished — see the log."))
            self.run(work, done, T("restoring…"))

        def open_backups(self):
            BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
            open_in_folder(BACKUP_ROOT)

        def _on_close(self):
            try:
                save_config(self._collect_config())
            except Exception as exc:                         # noqa: BLE001
                self.log(f"config save failed: {exc}")
            sp = ShimProcess(self.shim_cfg())
            if sp.pid():
                if messagebox.askyesno(APP_NAME, T("The translator is still running. Stop it?")):
                    sp.stop()
            self.destroy()

    app = App()
    if getattr(args, "gui_check", False):
        app.withdraw()
        app.update_idletasks()
        app.update()
        visible = [k for k in app.tab_frames
                   if str(app.tab_frames[k]) in set(app.nb.tabs())]
        print("GUI built OK (withdrawn); tabs:", app.nb.index("end"),
              "(built:", len(app.tab_frames), "|", ", ".join(app.tab_frames),
              "| visible:", ",".join(visible), ")")
        app.destroy()
        return 0
    app.mainloop()
    return 0


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP_SLUG, description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--cli", choices=["detect", "list", "plan", "apply", "backups", "restore",
                                     "models", "set-model", "shim-start", "shim-stop",
                                     "shim-status", "shim-test"],
                   help="run a command headlessly instead of the GUI")
    p.add_argument("--game-dir", help="Vaudeville install folder (auto-detected otherwise)")
    p.add_argument("--rescan", action="store_true", help="ignore the cached offset profile")
    p.add_argument("--show-prompts", action="store_true", help="print full system prompts with --cli list")
    p.add_argument("--mode", choices=sorted(MODE_ALIASES),
                   help="inference mode: basic|off = '%s', advanced|direct = '%s', "
                        "remote|shim = '%s'" % (MODE_LABELS[MODE_OFF], MODE_LABELS[MODE_DIRECT],
                                               MODE_LABELS[MODE_SHIM]))
    p.add_argument("--geometry", help="initial window geometry, e.g. 1280x900 (used for screenshots)")
    p.add_argument("--backend-url", help="OpenAI-compatible BaseURL, e.g. http://127.0.0.1:8000/v1")
    p.add_argument("--backend-model", help="model name the backend expects")
    p.add_argument("--api-key", help="backend API key (prefer --api-key-env)")
    p.add_argument("--api-key-env", help="read the API key from this environment variable")
    p.add_argument("--listen", help="shim listen address (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="shim listen port (default 13333)")
    p.add_argument("--server-port", type=int, help="port for the game's own server mode")
    p.add_argument("--expose-server", action="store_true", default=None,
                   help="also flip LLM.remote so the game serves its model over HTTP")
    p.add_argument("--strip-think", action="store_true", default=True,
                   help="strip <think> blocks in the shim (default)")
    p.add_argument("--keep-think", dest="strip_think", action="store_false")
    p.add_argument("--insecure", action="store_true", help="shim: skip TLS verification")
    p.add_argument("--verbose", action="store_true")
    p.add_argument("--set", action="append", metavar="NAME=VALUE",
                   help="agent field override, repeatable (e.g. temperature=0.9)")
    p.add_argument("--llm-set", action="append", metavar="NAME=VALUE",
                   help="LLM component field override, repeatable (e.g. contextSize=16384)")
    p.add_argument("--slot", choices=["primary", "deck"], help="model slot for --cli set-model")
    p.add_argument("--model", help="GGUF path for --cli set-model")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--backup", help="backup name for --cli restore (default: latest)")
    p.add_argument("--no-backup", action="store_true", help="apply without backing up (not recommended)")
    p.add_argument("--yes", "-y", action="store_true", help="do not ask for confirmation")
    p.add_argument("--prompt", help="prompt for --cli shim-test")
    p.add_argument("--selftest", action="store_true", help="run the built-in verification suite")
    p.add_argument("--live", action="store_true", help="selftest: also drive the game's libllamalib")
    p.add_argument("--ci", action="store_true",
                   help="selftest: runners without a game install skip the game-dependent sections")
    p.add_argument("--shim", action="store_true", help="internal: run the embedded shim")
    p.add_argument("--config", help="internal: shim config JSON path")
    p.add_argument("--mock-backend", action="store_true", help="internal: run the mock OpenAI backend")
    p.add_argument("--gui-check", action="store_true", help="internal: build the GUI and exit")
    p.add_argument("--tab",
                   help="open this tab at startup "
                        "(home|basic|advanced|remote|characters|backup; the old names "
                        "game|models|parameters are accepted as aliases)")
    p.add_argument("--quit-after", type=float, help="exit the GUI after N seconds (screenshots/testing)")
    p.add_argument("--version", action="store_true")
    return p


def _harden_stdio() -> None:
    """Never let a legacy console codepage crash a print.

    OEM boxes (cp437/cp850) and redirected pipes on old Windows setups cannot
    encode an em dash or a warning sign; replacing the unencodable character is
    always better than dying mid-selftest."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except Exception:                        # noqa: BLE001 - best effort only
            pass


def main(argv=None) -> int:
    _harden_stdio()
    args = build_parser().parse_args(argv)
    if args.version:
        tag = " (single-file build)" if getattr(sys, "frozen", False) else ""
        print(f"{APP_NAME} {APP_VERSION}{tag}")
        return 0
    if args.shim:
        cfg = {}
        if args.config:
            cfg = json.loads(Path(args.config).read_text(encoding="utf-8"))
        cfg.setdefault("api_key", os.environ.get("VLM_API_KEY", ""))
        return run_shim(cfg, logfn=lambda m: (print(m, flush=True), log(m)))
    if args.mock_backend:
        counter: dict = {"calls": 0}
        port = args.port or free_port()
        print(f"mock OpenAI backend on http://127.0.0.1:{port}/v1", flush=True)
        ThreadingHTTPServer(("127.0.0.1", port), make_mock_handler(counter)).serve_forever()
        return 0
    if args.selftest:
        gd = Path(args.game_dir).expanduser() if args.game_dir else None
        return selftest(gd, live=args.live, verbose=True, ci=args.ci)
    if args.cli:
        return cli(args)
    try:
        import tkinter  # noqa: F401
    except ImportError:
        print("tkinter is not available; install it (Arch: sudo pacman -S tk) or use --cli.",
              file=sys.stderr)
        return 2
    return gui_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
