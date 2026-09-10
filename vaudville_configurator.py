#!/usr/bin/env python3
"""Vaudville Configurator — model / endpoint / sampling-parameter control for
Bumblebee Studios' "Vaudeville" (Steam AppID 2240920).

Single file, standard library only (tkinter for the GUI). Run it from anywhere:

    vaudville-configurator                  # GUI (symlink in ~/.local/bin)
    vaudville-configurator --cli list       # headless
    vaudville-configurator --selftest       # built-in verification

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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

APP_NAME = "Vaudville Configurator"
APP_SLUG = "vaudville-configurator"
APP_VERSION = "1.3.0"
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
        res.notes.append("no LLMUnity.LLM component found — is this the right build?")
    if not res.agents:
        res.notes.append("no LLMUnity.LLMAgent components found")
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


def plan_edits(res: ScanResult, agent_changes: dict, llm_changes: dict) -> tuple[list[Edit], list[str]]:
    edits: list[Edit] = []
    warnings: list[str] = []
    data_cache: dict[Path, bytes] = {}

    def read_at(path: Path, offset: int, n: int) -> bytes:
        if path not in data_cache:
            with open(path, "rb") as fh:
                fh.seek(offset)
                data_cache[path] = fh.read(n)
        return data_cache[path]

    for blob in res.blobs:
        changes = agent_changes if blob.kind == "AGENT" else llm_changes
        for name, new in changes.items():
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
    return False, (f"{field} {wanted!r} is {len(wanted.encode())} bytes, but the shipped value "
                   f"{current!r} reserves {a} bytes, so in-place patching allows {allowed}. "
                   f"Use the built-in shim (which keeps {field}={current!r}) or a BepInEx "
                   f"plugin for arbitrary values.")


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


def build_change_set(cfg: dict, res: ScanResult, overrides: dict | None = None
                     ) -> tuple[dict, dict, list[str]]:
    """Turn the GUI config into {agent_fields}, {llm_fields}, notes."""
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
        notes.append(f"{MODE_LABELS[MODE_OFF]}: agents use the in-process llama.cpp "
                     f"service (nothing leaves the machine)")
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
        notes.append(f"{MODE_LABELS[MODE_SHIM]}: agents -> built-in shim on "
                     f"{wanted_host}:{cfg.get('shim_port',13333)} "
                     f"-> {cfg.get('backend_url') or '<no BaseURL set>'} "
                     f"(model {cfg.get('backend_model') or '-'})")
        if not cfg.get("backend_url"):
            notes.append("WARNING: no remote BaseURL configured — the shim will fail every request")
        if cfg.get("api_key") and not cfg.get("save_api_key"):
            notes.append("API key is held in memory/env only (not written to the config file)")
    elif mode == "direct":
        agent_changes["remote"] = True
        url = (cfg.get("backend_url") or "").strip()
        host, port, path, tls = split_endpoint(url, int(cfg.get("direct_port", 0) or 0))
        problems = []
        if not host:
            problems.append(f"BLOCKED ({MODE_LABELS[MODE_DIRECT]}): no host in the BaseURL")
        if path:
            problems.append(
                f"BLOCKED ({MODE_LABELS[MODE_DIRECT]}): LlamaLib hands the host string straight to cpp-httplib "
                "and appends /completion, /health, /apply-template … itself, so a path prefix "
                f"like {path!r} is not supported. Give host[:port] only, or use the shim.")
        if tls:
            problems.append(
                f"BLOCKED ({MODE_LABELS[MODE_DIRECT]}): TLS needs the literal string 'https://<host>' inside the "
                "host field (LlamaLib strips the scheme and switches to SSLClient). That is 8 "
                "extra bytes and cannot fit the in-place 'localhost' slot — use the shim "
                "(which can do TLS for you) or a BepInEx plugin.")
        ok, why = host_fits(cur_host, host)
        if not ok:
            problems.append(f"BLOCKED ({MODE_LABELS[MODE_DIRECT]}): " + why)
        if problems:
            notes.extend(problems)
            notes.append(f"Switch to '{MODE_LABELS[MODE_SHIM]}' — it keeps host=localhost "
                         f"and holds the real BaseURL / model / API key itself.")
        else:
            agent_changes["host"] = host
            agent_changes["port"] = port
            notes.append(f"{MODE_LABELS[MODE_DIRECT]}: agents -> http://{host}:{port} directly "
                         f"(the server must speak the "
                         f"llama.cpp protocol: /health /apply-template /completion /tokenize)")
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
        notes.append("NOTE: the boot screen waits for the LOCAL model to start "
                     "(OffWorldInit.CheckLoading -> LLM.started), so keep a small GGUF "
                     "linked as the main model, or the loading screen will hang.")
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
    if vfile.is_file() and not getattr(sys, "frozen", False):
        check(vfile.read_text().strip() == APP_VERSION,
              f"VERSION file ({vfile.read_text().strip()}) matches APP_VERSION ({APP_VERSION})")

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
    for p, src in found:
        check(True, f"found {p}  ({src})")
    if not found:
        if ci:
            check(True, "no Vaudeville install here — "
                          "CI mode skips the game-dependent sections")
        else:
            check(False, "no Vaudeville install found")
    gd = game_dir or (found[0][0] if found else None)
    if gd is None:
        if not ci:
            print("== cannot continue without a game directory ==")
            return 1

    if gd is None:
        print("[3] skipped (CI mode: no game install on this runner)")
        print("[4] skipped (CI mode: no game install on this runner)")
    if gd is not None:
        print(f"[3] component scan of {gd}")
        t0 = time.time()
        res = scan_game(gd)
        check(len(res.llm) == 1, f"exactly one LLMUnity.LLM component ({len(res.llm)})")
        check(len(res.agents) > 0, f"{len(res.agents)} LLMUnity.LLMAgent components")
        check(all(b.leftover >= 0 for b in res.blobs), "all blobs decoded without over-read")
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
# GUI (tkinter)
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


def gui_main(args) -> int:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"{APP_NAME} {APP_VERSION} — Vaudeville LLM / endpoint control")
            self.geometry(getattr(args, "geometry", None) or "1080x760")
            self.minsize(900, 620)
            self.cfg = load_config()
            self.scan: ScanResult | None = None
            self.agent_vars: dict[str, tk.StringVar] = {}
            self.llm_vars: dict[str, tk.StringVar] = {}
            self._queue: list[str] = []
            self._build()
            want = (getattr(args, "tab", None) or "").strip().lower()
            if want:
                for i in range(self.nb.index("end")):
                    if self.nb.tab(i, "text").strip().lower().startswith(want):
                        self.nb.select(i)
                        break
            if getattr(args, "quit_after", 0):
                self.after(int(float(args.quit_after) * 1000), self.destroy)
            if not getattr(args, "gui_check", False):
                self.after(200, self._initial_scan)
                self.after(1000, self._poll_running)
            self.protocol("WM_DELETE_WINDOW", self._on_close)

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
                except Exception:
                    pass
            if msg:
                self.status(msg)

        def run(self, fn, done=None, what="working…"):
            self.busy(True, what)

            def worker():
                try:
                    out = fn()
                    err = None
                except Exception as exc:            # noqa: BLE001 - report everything
                    out, err = None, exc
                self.after(0, lambda: self._finish(out, err, done))
            threading.Thread(target=worker, daemon=True).start()

        def _finish(self, out, err, done):
            self.busy(False)
            if err is not None:
                self.log(f"ERROR: {err}")
                messagebox.showerror(APP_NAME, str(err))
                return
            if done:
                done(out)

        def game_dir(self) -> Path | None:
            p = Path(self.dir_var.get().strip()).expanduser()
            return p if str(p) else None

        # ---------- build ---------- #
        def _build(self):
            style = ttk.Style(self)
            if "clam" in style.theme_names():
                style.theme_use("clam")
            self.action_buttons: list = []

            top = ttk.Frame(self, padding=(10, 8, 10, 4))
            top.pack(fill="x")
            ttk.Label(top, text="Game folder:").pack(side="left")
            self.dir_var = tk.StringVar(value=self.cfg.get("game_dir", ""))
            ttk.Entry(top, textvariable=self.dir_var).pack(side="left", fill="x", expand=True, padx=6)
            b = ttk.Button(top, text="Browse…", command=self.browse); b.pack(side="left", padx=2)
            b = ttk.Button(top, text="Detect", command=self.detect); b.pack(side="left", padx=2)
            b = ttk.Button(top, text="Re-scan", command=self.rescan); b.pack(side="left", padx=2)
            self.action_buttons += [b]
            self.running_lbl = ttk.Label(top, text="", foreground="#a33")
            self.running_lbl.pack(side="left", padx=8)

            self.nb = ttk.Notebook(self)
            self.nb.pack(fill="both", expand=True, padx=10, pady=4)
            self._tab_game(); self._tab_models(); self._tab_remote()
            self._tab_params(); self._tab_backup()

            bottom = ttk.Frame(self, padding=(10, 2, 10, 8))
            bottom.pack(fill="both", expand=False)
            self.logbox = tk.Text(bottom, height=9, wrap="word", state="disabled",
                                  background="#111", foreground="#cfc")
            self.logbox.pack(fill="both", expand=True)
            bar = ttk.Frame(self); bar.pack(fill="x", padx=10, pady=(0, 6))
            self.status_var = tk.StringVar(value="ready")
            ttk.Label(bar, textvariable=self.status_var).pack(side="left")
            self.progress = ttk.Progressbar(bar, length=140, mode="determinate")
            self.progress.pack(side="right")

        # ---------- tab: game ---------- #
        def _tab_game(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text=" Game ")
            self.info_var = tk.StringVar(value="not scanned yet")
            ttk.Label(f, textvariable=self.info_var, justify="left").pack(anchor="w")
            box = ttk.LabelFrame(f, text="How it works", padding=8)
            box.pack(fill="both", expand=True, pady=8)
            tk.Label(box, justify="left", anchor="w", text=(
                "Vaudeville's dialogue AI is undreamai/LLMUnity v3.0.0 + LlamaLib v2.0.0 (a\n"
                "llama.cpp fork). Every sampling parameter and the local/remote switch is a\n"
                "serialized field inside the shipped Unity assets:\n\n"
                "   sharedassets3.assets            1 × LLMUnity.LLM       (model, context, GPU, server)\n"
                "   level4…level13, sharedassets18  26 × LLMUnity.LLMAgent  (per-character sampling)\n\n"
                "This tool finds those blobs with a structural signature, validates every field,\n"
                "and rewrites them in place at their absolute file offset (bool/int/float are\n"
                "always 4 bytes; strings only when ceil4(len) is unchanged). Hash-verified backups\n"
                "are written outside the game folder before anything is touched.\n\n"
                "Remote endpoints: the game speaks the llama.cpp-server protocol (/health,\n"
                "/apply-template, /completion, /tokenize, /detokenize, /embeddings) — not OpenAI's\n"
                "/v1/chat/completions. The built-in shim translates, so vLLM / Ollama / LM Studio /\n"
                "OpenAI / TGI all work. A real llama.cpp server needs no shim at all."),
                wraplength=980).pack(anchor="w")

        # ---------- tab: models ---------- #
        def _tab_models(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text=" Models ")
            ttk.Label(f, text=("The build hard-codes two GGUF filenames inside StreamingAssets. "
                               "Point each one at any GGUF you like (symlink swap, the shipped file "
                               "is preserved as gguf/ORIGINAL_…).")).pack(anchor="w")
            self.model_rows = {}
            for i, slot in enumerate(model_slots()):
                box = ttk.LabelFrame(f, text=f"{slot.label}   ({slot.name})", padding=8)
                box.pack(fill="x", pady=6)
                cur = ttk.Label(box, text="current: …", anchor="w")
                cur.pack(fill="x")
                row = ttk.Frame(box); row.pack(fill="x", pady=4)
                combo = ttk.Combobox(row, width=70)
                combo.pack(side="left", fill="x", expand=True)
                bb = ttk.Button(row, text="Browse…",
                                command=lambda s=slot, c=combo: self.pick_model(c))
                bb.pack(side="left", padx=4)
                ba = ttk.Button(row, text="Apply", command=lambda s=slot, c=combo, lbl=cur: self.apply_model(s, c, lbl))
                ba.pack(side="left")
                self.action_buttons.append(ba)
                self.model_rows[slot.name] = (cur, combo)
            lib = ttk.Frame(f); lib.pack(fill="x", pady=6)
            self.lib_var = tk.StringVar(value="")
            ttk.Label(lib, textvariable=self.lib_var).pack(side="left")
            b = ttk.Button(lib, text="Open library folder", command=self.open_library); b.pack(side="right")
            b = ttk.Button(lib, text="Refresh", command=self.refresh_models); b.pack(side="right", padx=4)
            ttk.Label(f, foreground="#555", text=(
                "Tip: on Steam Deck the game switches to the fallback model automatically "
                "(OffWorldInit.Awake). GPU offload is not here — it comes from the in-game "
                "Options slider / the 'GpuLoad' preference, and the Parameters tab can set it too.")).pack(anchor="w", pady=(8, 0))

        # ---------- tab: remote ---------- #
        def _tab_remote(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text=" Remote endpoint ")
            self.mode_var = tk.StringVar(value=normalize_mode(self.cfg.get("mode", MODE_SHIM)))
            modes = ttk.LabelFrame(f, text="Mode", padding=8)
            modes.pack(fill="x")
            for val in MODE_ORDER:
                r = ttk.Radiobutton(modes, text=MODE_LABELS[val], value=val,
                                    variable=self.mode_var, command=self._mode_changed)
                r.pack(anchor="w")
                ttk.Label(modes, text="      " + MODE_TIPS[val], foreground="#666").pack(anchor="w")

            be = ttk.LabelFrame(f, text="Backend (what the shim forwards to)", padding=8)
            be.pack(fill="x", pady=6)
            grid = ttk.Frame(be); grid.pack(fill="x")
            self.backend_var = tk.StringVar(value=self.cfg.get("backend_url", ""))
            self.bmodel_var = tk.StringVar(value=self.cfg.get("backend_model", ""))
            self.apikey_var = tk.StringVar(value=self.cfg.get("api_key", ""))
            self.savekey_var = tk.BooleanVar(value=bool(self.cfg.get("save_api_key")))
            for i, (label, var, width, show) in enumerate((
                    ("BaseURL", self.backend_var, 46, None),
                    ("Model name", self.bmodel_var, 26, None),
                    ("API key", self.apikey_var, 30, "*"))):
                ttk.Label(grid, text=label + ":").grid(row=i, column=0, sticky="w", pady=2)
                e = ttk.Entry(grid, textvariable=var, width=width, show=show)
                e.grid(row=i, column=1, sticky="we", padx=6, pady=2)
                if label == "API key":
                    e._is_key_entry = True
                    self.key_entry = e
            grid.columnconfigure(1, weight=1)
            opts = ttk.Frame(be); opts.pack(fill="x", pady=(6, 0))
            ttk.Checkbutton(opts, text="Save API key in config (chmod 600)",
                            variable=self.savekey_var).pack(side="left")
            self.showkey_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(opts, text="Show key", variable=self.showkey_var,
                            command=self._toggle_key).pack(side="left", padx=8)
            b = ttk.Button(opts, text="Load from Bitwarden…", command=self.load_bitwarden)
            b.pack(side="left", padx=4)
            ttk.Label(be, foreground="#555", text=(
                "Examples —  vLLM: http://127.0.0.1:8000/v1   Ollama: http://127.0.0.1:11434/v1\n"
                "                     LM Studio: http://127.0.0.1:1234/v1   OpenAI: https://api.openai.com/v1\n"
                "The key is passed to the shim through the environment, never on a command line.")).pack(anchor="w", pady=(6, 0))

            sh = ttk.LabelFrame(f, text="Built-in shim", padding=8)
            sh.pack(fill="x", pady=6)
            row = ttk.Frame(sh); row.pack(fill="x")
            ttk.Label(row, text="Listen:").pack(side="left")
            self.listen_var = tk.StringVar(value=self.cfg.get("shim_listen", "127.0.0.1"))
            ttk.Entry(row, textvariable=self.listen_var, width=15).pack(side="left", padx=4)
            ttk.Label(row, text="Port:").pack(side="left")
            self.shimport_var = tk.StringVar(value=str(self.cfg.get("shim_port", 13333)))
            ttk.Entry(row, textvariable=self.shimport_var, width=7).pack(side="left", padx=4)
            ttk.Label(row, text="Mode:").pack(side="left", padx=(12, 0))
            self.shimmode_var = tk.StringVar(value=self.cfg.get("shim_mode", "chat"))
            ttk.Combobox(row, textvariable=self.shimmode_var, width=6, state="readonly",
                         values=("chat", "raw")).pack(side="left", padx=4)
            self.strip_var = tk.BooleanVar(value=bool(self.cfg.get("strip_think", True)))
            ttk.Checkbutton(row, text="Strip <think> blocks", variable=self.strip_var).pack(side="left", padx=10)
            self.insecure_var = tk.BooleanVar(value=False)
            ttk.Checkbutton(row, text="Skip TLS verify", variable=self.insecure_var).pack(side="left")
            row2 = ttk.Frame(sh); row2.pack(fill="x", pady=(6, 0))
            self.shim_btn = ttk.Button(row2, text="Start shim", command=self.shim_start)
            self.shim_btn.pack(side="left"); self.action_buttons.append(self.shim_btn)
            b = ttk.Button(row2, text="Stop shim", command=self.shim_stop); b.pack(side="left", padx=4)
            b = ttk.Button(row2, text="Test completion", command=self.shim_test); b.pack(side="left", padx=4)
            self.action_buttons += [b]
            b = ttk.Button(row2, text="Show shim log", command=self.shim_log); b.pack(side="left", padx=4)
            self.shim_state = ttk.Label(row2, text="● stopped", foreground="#a33")
            self.shim_state.pack(side="left", padx=12)

            srv = ttk.LabelFrame(f, text="Reverse: expose the game's own model as an HTTP server", padding=8)
            srv.pack(fill="x", pady=6)
            self.expose_var = tk.BooleanVar(value=bool(self.cfg.get("expose_server")))
            ttk.Checkbutton(srv, text="Enable (LLM.remote = true) — serves /v1/chat/completions, /completion, /tokenize …",
                            variable=self.expose_var).pack(anchor="w")
            row3 = ttk.Frame(srv); row3.pack(fill="x", pady=4)
            ttk.Label(row3, text="Server port:").pack(side="left")
            self.serverport_var = tk.StringVar(value=str(self.cfg.get("server_port", 13333)))
            ttk.Entry(row3, textvariable=self.serverport_var, width=7).pack(side="left", padx=4)
            ttk.Label(srv, foreground="#555", text=(
                "One byte in sharedassets3.assets. Do not use the same port as the shim.")).pack(anchor="w")

            act = ttk.Frame(f); act.pack(fill="x", pady=8)
            b = ttk.Button(act, text="Preview changes", command=self.preview); b.pack(side="left")
            self.action_buttons.append(b)
            b = ttk.Button(act, text="Apply to game files", command=self.apply_changes)
            b.pack(side="left", padx=6); self.action_buttons.append(b)
            b = ttk.Button(act, text=f"Reset to {MODE_LABELS[MODE_OFF]}", command=self.set_local)
            b.pack(side="left"); self.action_buttons.append(b)
            self.plan_box = tk.Text(f, height=10, wrap="word", state="disabled")
            self.plan_box.pack(fill="both", expand=True)

        # ---------- tab: parameters ---------- #
        def _tab_params(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text=" Parameters ")
            ttk.Label(f, text=("Values apply to all 26 character agents / the single LLM component. "
                               "Blank = leave unchanged. Loaded from the game on scan.")).pack(anchor="w")
            cols = ttk.Frame(f); cols.pack(fill="both", expand=True, pady=6)
            left = ttk.LabelFrame(cols, text="Character agents (LLMUnity.LLMAgent ×26)", padding=6)
            left.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
            right = ttk.LabelFrame(cols, text="Local engine (LLMUnity.LLM ×1)", padding=6)
            right.grid(row=0, column=1, sticky="nsew")
            cols.columnconfigure(0, weight=1); cols.columnconfigure(1, weight=1)
            canv = tk.Canvas(left, highlightthickness=0)
            scroll = ttk.Scrollbar(left, orient="vertical", command=canv.yview)
            inner = ttk.Frame(canv)
            inner.bind("<Configure>", lambda e: canv.configure(scrollregion=canv.bbox("all")))
            canv.create_window((0, 0), window=inner, anchor="nw")
            canv.configure(yscrollcommand=scroll.set)
            canv.pack(side="left", fill="both", expand=True)
            scroll.pack(side="right", fill="y")
            for i, (name, kind, tip) in enumerate(AGENT_FIELDS_GUI):
                ttk.Label(inner, text=name).grid(row=i, column=0, sticky="w", pady=1)
                var = tk.StringVar()
                ttk.Entry(inner, textvariable=var, width=14).grid(row=i, column=1, padx=4, pady=1)
                ttk.Label(inner, text=tip, foreground="#666").grid(row=i, column=2, sticky="w")
                self.agent_vars[name] = var
            for i, (name, kind, tip) in enumerate(LLM_FIELDS_GUI):
                ttk.Label(right, text=name).grid(row=i, column=0, sticky="w", pady=1)
                var = tk.StringVar()
                ttk.Entry(right, textvariable=var, width=14).grid(row=i, column=1, padx=4, pady=1)
                ttk.Label(right, text=tip, foreground="#666").grid(row=i, column=2, sticky="w")
                self.llm_vars[name] = var
            btns = ttk.Frame(f); btns.pack(fill="x")
            b = ttk.Button(btns, text="Load current values", command=self.load_values); b.pack(side="left")
            self.action_buttons.append(b)
            b = ttk.Button(btns, text="Clear (leave unchanged)", command=self.clear_values); b.pack(side="left", padx=6)
            b = ttk.Button(btns, text="Game defaults", command=self.load_defaults); b.pack(side="left")
            b = ttk.Button(btns, text="Preview changes", command=self.preview); b.pack(side="left", padx=6)
            self.action_buttons.append(b)
            b = ttk.Button(btns, text="Apply to game files", command=self.apply_changes)
            b.pack(side="left"); self.action_buttons.append(b)

        # ---------- tab: backups ---------- #
        def _tab_backup(self):
            f = ttk.Frame(self.nb, padding=10)
            self.nb.add(f, text=" Backup / restore ")
            from tkinter import ttk as _ttk
            self.bk_tree = _ttk.Treeview(f, columns=("when", "files", "game", "labels"),
                                         show="headings", height=12)
            for c, w in (("when", 170), ("files", 60), ("game", 300), ("labels", 380)):
                self.bk_tree.heading(c, text=c.title()); self.bk_tree.column(c, width=w, anchor="w")
            self.bk_tree.pack(fill="both", expand=True)
            row = ttk.Frame(f); row.pack(fill="x", pady=6)
            b = ttk.Button(row, text="Refresh", command=self.refresh_backups); b.pack(side="left")
            b = ttk.Button(row, text="Restore selected", command=self.restore_selected); b.pack(side="left", padx=6)
            self.action_buttons.append(b)
            b = ttk.Button(row, text="Open backup folder", command=self.open_backups); b.pack(side="left")
            ttk.Label(f, foreground="#555", text=(
                "Restoring puts the original files back byte-for-byte (sha256 verified). "
                "Steam's 'verify integrity of game files' is another way back to stock.")).pack(anchor="w", pady=(6, 0))
            self.refresh_backups()

        # ---------- actions: game ---------- #
        def browse(self):
            d = filedialog.askdirectory(title="Select the Vaudeville game folder")
            if d:
                self.dir_var.set(d)
                self.rescan()

        def detect(self):
            found = find_game_dirs()
            if not found:
                messagebox.showwarning(APP_NAME, "No Vaudeville install found.")
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
                messagebox.showwarning(APP_NAME, "Set the game folder first.")
                return

            def work():
                return scan_game(gd, use_profile=False)

            def done(res):
                self.scan = res
                self.info_var.set(
                    f"{gd}\nbuild-guid {build_guid(gd)}   ·   {len(res.blobs)} LLMUnity components "
                    f"({len(res.llm)} LLM, {len(res.agents)} agents)   ·   scan {res.duration:.1f}s\n"
                    + res.summary())
                for n in res.notes:
                    self.log("note: " + n)
                self.load_values()
                self.refresh_models()
                self.status(f"scanned {len(res.blobs)} components")
                self.log("scan complete: " + res.summary())
            self.run(work, done, "scanning game assets…")

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
                    txt = f"⚠ game is RUNNING (pid {', '.join(map(str, pids))}) — patching is blocked"
            self.running_lbl.configure(text=txt)
            self.after(3000, self._poll_running)

        # ---------- actions: models ---------- #
        def refresh_models(self):
            gd = self.game_dir()
            if gd is None:
                return
            models = list_library_models(gd)
            self.lib_var.set(f"library: {library_dir(gd)}   ({len(models)} GGUF files)")
            names = [str(p) for p in models]
            for slot in model_slots():
                cur, combo = self.model_rows[slot.name]
                combo["values"] = names
                st = slot_status(gd, slot)
                tgt = st["target"] or "-"
                cur.configure(text=(f"current: {tgt}   ({human_size(st['size'])}"
                                    f"{', symlink' if st['is_link'] else ', real file'}"
                                    f"{', BROKEN LINK' if st['broken'] else ''})"))
                preset = self.cfg.get("primary_model" if slot.name == PRIMARY_MODEL_NAME else "deck_model")
                if preset and preset in names:
                    combo.set(preset)
                elif st["target"]:
                    combo.set(st["target"])

        def pick_model(self, combo):
            f = filedialog.askopenfilename(title="Choose a GGUF model",
                                           filetypes=[("GGUF models", "*.gguf"), ("All files", "*")])
            if f:
                combo.set(f)

        def apply_model(self, slot, combo, label):
            gd = self.game_dir()
            target = combo.get().strip()
            if gd is None or not target:
                messagebox.showwarning(APP_NAME, "Pick a game folder and a model first.")
                return

            def work():
                return set_slot_model(gd, slot, Path(target))
            def done(lines):
                for l in lines:
                    self.log(l)
                self.refresh_models()
                self.cfg["primary_model" if slot.name == PRIMARY_MODEL_NAME else "deck_model"] = target
                save_config(self._collect_config())
            self.run(work, done, f"linking {slot.name}…")

        def open_library(self):
            gd = self.game_dir()
            if gd is None:
                return
            d = library_dir(gd)
            d.mkdir(parents=True, exist_ok=True)
            open_in_folder(d)

        # ---------- actions: remote ---------- #
        def _mode_changed(self):
            self.status("mode: " + mode_label(self.mode_var.get()))

        def _toggle_key(self):
            try:
                self.key_entry.configure(show="" if self.showkey_var.get() else "*")
            except Exception:
                pass

        def load_bitwarden(self):
            if shutil.which("bw") is None:
                messagebox.showerror(APP_NAME, "Bitwarden CLI 'bw' not found in PATH.")
                return
            try:
                status = subprocess.run(["bw", "status"], capture_output=True, text=True, timeout=20)
                info = json.loads(status.stdout or "{}")
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"bw status failed: {exc}")
                return
            if info.get("status") != "unlocked":
                messagebox.showwarning(APP_NAME, "The Bitwarden vault is locked; run 'bw unlock' first.")
                return
            try:
                items = subprocess.run(["bw", "list", "items", "--search", "llm"],
                                       capture_output=True, text=True, timeout=60)
                data = json.loads(items.stdout or "[]")
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"bw list failed: {exc}")
                return
            names = [f"{i.get('name')}  [{i.get('id','')[:8]}]" for i in data if i.get("name")]
            if not names:
                messagebox.showinfo(APP_NAME, "No Bitwarden items matching 'llm'.")
                return
            win = tk.Toplevel(self); win.title("Pick a Bitwarden item"); win.geometry("460x320")
            lb = tk.Listbox(win); lb.pack(fill="both", expand=True, padx=8, pady=8)
            for n in names:
                lb.insert("end", n)

            def take():
                sel = lb.curselection()
                if not sel:
                    return
                item = data[sel[0]]
                secret = ""
                for f in ("login",):
                    login = item.get(f) or {}
                    secret = login.get("password") or ""
                if not secret:
                    for fld in item.get("fields") or []:
                        if fld.get("type") == 1:
                            secret = fld.get("value") or ""
                            break
                if secret:
                    self.apikey_var.set(secret)
                    self.log(f"loaded API key for Bitwarden item '{item.get('name')}' (not logged)")
                else:
                    self.log("that Bitwarden item has no password/hidden field")
                win.destroy()
            ttk.Button(win, text="Use selected key", command=take).pack(pady=(0, 8))

        def shim_cfg(self) -> dict:
            return {"listen": self.listen_var.get().strip() or "127.0.0.1",
                    "port": int(self.shimport_var.get() or 13333),
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
                self.log(msg)
                self._update_shim_state(sp)
                if ok:
                    save_config(self._collect_config())
            self.run(work, done, "starting shim…")

        def shim_stop(self):
            sp = ShimProcess(self.shim_cfg())
            def work():
                return sp.stop()
            def done(msg):
                self.log(msg); self._update_shim_state(sp)
            self.run(work, done, "stopping shim…")

        def shim_test(self):
            sp = ShimProcess(self.shim_cfg())
            def work():
                return sp.test_completion("Say hello in three words.")
            def done(res):
                ok, text = res
                self.log(("shim test OK: " if ok else "shim test FAILED: ") + text)
                if not ok:
                    self.log(sp.tail(20))
            self.run(work, done, "testing shim → backend…")

        def shim_log(self):
            sp = ShimProcess(self.shim_cfg())
            win = tk.Toplevel(self); win.title(f"shim log — {SHIM_LOG_FILE}"); win.geometry("820x460")
            t = tk.Text(win, wrap="word", background="#111", foreground="#cfc"); t.pack(fill="both", expand=True)
            t.insert("end", sp.tail(400))
            t.see("end")

        def _update_shim_state(self, sp: ShimProcess):
            pid = sp.pid()
            ok, _ = sp.health() if pid else (False, "")
            if pid and ok:
                self.shim_state.configure(text=f"● running (pid {pid})", foreground="#2a2")
            elif pid:
                self.shim_state.configure(text=f"● started but unhealthy (pid {pid})", foreground="#c80")
            else:
                self.shim_state.configure(text="● stopped", foreground="#a33")

        # ---------- actions: patching ---------- #
        def _collect_config(self) -> dict:
            cfg = dict(self.cfg)
            cfg.update({
                "game_dir": self.dir_var.get().strip(),
                "mode": self.mode_var.get(),
                "backend_url": self.backend_var.get().strip(),
                "backend_model": self.bmodel_var.get().strip(),
                "save_api_key": bool(self.savekey_var.get()),
                "api_key": self.apikey_var.get() if self.savekey_var.get() else cfg.get("api_key", ""),
                "shim_listen": self.listen_var.get().strip(),
                "shim_port": int(self.shimport_var.get() or 13333),
                "shim_mode": self.shimmode_var.get(),
                "strip_think": bool(self.strip_var.get()),
                "expose_server": bool(self.expose_var.get()),
                "server_port": int(self.serverport_var.get() or 13333),
            })
            if not cfg.get("save_api_key"):
                cfg["api_key"] = ""
            return cfg

        def _overrides(self) -> dict:
            out = {}
            for name, var in self.agent_vars.items():
                txt = var.get().strip()
                if txt == "":
                    continue
                try:
                    out[name] = coerce_for("AGENT", name, txt)
                except Exception as exc:
                    self.log(f"ignoring agent field {name}: {exc}")
            for name, var in self.llm_vars.items():
                txt = var.get().strip()
                if txt == "":
                    continue
                try:
                    out[name] = coerce_for("LLM", name, txt)
                except Exception as exc:
                    self.log(f"ignoring LLM field {name}: {exc}")
            return out

        def _plan(self):
            if self.scan is None:
                raise PatchError("scan the game first (Re-scan button)")
            cfg = self._collect_config()
            self.cfg = cfg
            agent_changes, llm_changes, notes = build_change_set(cfg, self.scan, self._overrides())
            edits, warnings = plan_edits(self.scan, agent_changes, llm_changes)
            return edits, warnings, notes, agent_changes, llm_changes

        def _show_plan(self, edits, warnings, notes):
            self.plan_box.configure(state="normal")
            self.plan_box.delete("1.0", "end")
            for n in notes:
                self.plan_box.insert("end", "• " + n + "\n")
            for w in warnings:
                self.plan_box.insert("end", "⚠ " + w + "\n")
            self.plan_box.insert("end", f"\n{len(edits)} byte edit(s):\n")
            for e in edits:
                self.plan_box.insert("end", f"  {e.label}\n")
            if not edits:
                self.plan_box.insert("end", "  (nothing to change — the game already has these values)\n")
            self.plan_box.configure(state="disabled")

        def preview(self):
            def work():
                return self._plan()
            def done(res):
                edits, warnings, notes, ag, llm = res
                self._show_plan(edits, warnings, notes)
                self.log(f"preview: {len(edits)} edits, {len(warnings)} warnings")
                self.status(f"{len(edits)} edits planned")
            self.run(work, done, "planning changes…")

        def apply_changes(self):
            def work():
                return self._plan()
            def done(res):
                edits, warnings, notes, ag, llm = res
                self._show_plan(edits, warnings, notes)
                if not edits:
                    messagebox.showinfo(APP_NAME, "Nothing to change.")
                    return
                gd = self.game_dir()
                if not messagebox.askyesno(
                        APP_NAME,
                        f"Apply {len(edits)} byte edit(s) to\n{gd}\n\n"
                        "A hash-verified backup is written first.\nContinue?"):
                    return
                def work2():
                    rec = apply_edits(edits, gd)
                    return rec, scan_game(gd, use_profile=False)
                def done2(res2):
                    rec, res2 = res2
                    self.scan = res2
                    self.log(f"applied {len(edits)} edits; backup at {rec.dir}")
                    self.log("verification: " + res2.summary())
                    self.load_values()
                    sample = res2.agents[0].values if res2.agents else {}
                    keys = [k for k in ("remote", "host", "port", "temperature", "numPredict") if k in sample]
                    self.log("first agent now: " + ", ".join(f"{k}={sample[k]!r}" for k in keys))
                    save_config(self._collect_config())
                    self.refresh_backups()
                    messagebox.showinfo(APP_NAME, f"Applied.\nBackup: {rec.dir}")
                self.run(work2, done2, "patching game files…")
            self.run(work, done, "planning changes…")

        def set_local(self):
            self.mode_var.set("off")
            self.expose_var.set(False)
            self.preview()

        # ---------- actions: parameters ---------- #
        def load_values(self):
            if self.scan is None:
                return
            agent_kinds = dict(AGENT_LAYOUT)
            llm_kinds = dict(LLM_LAYOUT)
            agents = self.scan.agents
            uniform = len({json.dumps(b.values, default=str, sort_keys=True) for b in agents}) <= 1
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
            if agents and not uniform:
                self.log("note: the 26 agent components do not all share the same values; "
                         "showing the first one (edits apply to all)")
            self.status("loaded current values")

        def clear_values(self):
            for var in list(self.agent_vars.values()) + list(self.llm_vars.values()):
                var.set("")
            self.status("all fields cleared (nothing will be changed)")

        def load_defaults(self):
            defaults = {"temperature": "0.2", "topK": "40", "topP": "0.9", "minP": "0.05",
                        "repeatPenalty": "1.1", "repeatLastN": "64", "presencePenalty": "0",
                        "frequencyPenalty": "0", "typicalP": "1.0", "mirostat": "0",
                        "mirostatTau": "5.0", "mirostatEta": "0.1", "seed": "0",
                        "numPredict": "-1", "cachePrompt": "true", "ignoreEos": "false",
                        "nProbs": "0", "numRetries": "5", "slot": "-1", "remote": "false",
                        "host": "localhost", "port": "13333"}
            for name, var in self.agent_vars.items():
                if name in defaults:
                    var.set(defaults[name])
            ldef = {"contextSize": "8192", "batchSize": "512", "numThreads": "-1",
                    "numGPULayers": "0", "parallelPrompts": "-1", "flashAttention": "false",
                    "reasoning": "false", "remote": "false", "port": "13333",
                    "dontDestroyOnLoad": "false", "embeddingsOnly": "false",
                    "embeddingLength": "0", "minContextLength": "0", "maxContextLength": "131072"}
            for name, var in self.llm_vars.items():
                if name in ldef:
                    var.set(ldef[name])
            self.status("shipped defaults loaded into the form (not applied yet)")

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
                messagebox.showinfo(APP_NAME, "Select a backup first.")
                return
            rec = next((b for b in self._backups if str(b.dir) == sel[0]), None)
            if rec is None:
                return
            if not messagebox.askyesno(APP_NAME, f"Restore {len(rec.manifest['files'])} file(s) from\n{rec.dir}?"):
                return
            def work():
                return restore_backup(rec, self.game_dir())
            def done(lines):
                for l in lines:
                    self.log(l)
                self.rescan()
                messagebox.showinfo(APP_NAME, "Restore finished (see log).")
            self.run(work, done, "restoring…")

        def open_backups(self):
            BACKUP_ROOT.mkdir(parents=True, exist_ok=True)
            open_in_folder(BACKUP_ROOT)

        def _on_close(self):
            try:
                save_config(self._collect_config())
            except Exception as exc:
                self.log(f"config save failed: {exc}")
            sp = ShimProcess(self.shim_cfg())
            if sp.pid():
                if messagebox.askyesno(APP_NAME, "The shim is still running. Stop it?"):
                    sp.stop()
            self.destroy()

    app = App()
    if getattr(args, "gui_check", False):
        app.withdraw()
        app.update_idletasks()
        app.update()
        print("GUI built OK (withdrawn); tabs:", app.nb.index("end"))
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
    p.add_argument("--tab", help="open this tab at startup (game|models|remote|parameters|backup)")
    p.add_argument("--quit-after", type=float, help="exit the GUI after N seconds (screenshots/testing)")
    p.add_argument("--version", action="store_true")
    return p


def main(argv=None) -> int:
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
