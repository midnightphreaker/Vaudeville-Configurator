# Vaudville Configurator

*Vaudeville* (Bumblebee Studios, Steam AppID 2240920) is a detective game built in Unity
where the characters are not scripted: an AI running on your own machine writes every line
they say, using LLMUnity and a llama.cpp engine bundled inside the game folder. Vaudville
Configurator points that cast at the AI you actually want — your own model file, your own
local server, or your own OpenAI-compatible service — and lets you tune how the characters
talk.

It works by editing the game's files where they sit, so there is no reinstall, no mod
loader and no recompiling. Every write is preceded by a checked backup, and the tool
refuses to touch anything while the game is running.

The program is one self-contained Python file using the standard library plus tkinter,
with two optional pure-data modules beside it (`vaudville_help.py` for the wording and
`vaudville_cast.py` for the cast list). No pip installs, no UnityPy. Prebuilt single-file
binaries are available for Linux and Windows.

Project home, source, issues and downloads: <https://github.com/MidnightPhreaker/Vaudville-Configurator>

---

## The three setups

Open the tool and the first page asks one question — *How should Vaudeville talk to its
AI?* — and shows three cards. Choose one and press **Continue**. Only the settings that
belong to that setup become visible, so the window stays short and nothing irrelevant is
on screen. You can come back to the first page and switch at any time; switching rewrites
the same handful of values and never deletes anything.

| setup | what it does | difficulty | restrictions |
|---|---|---|---|
| **Local Mode - Basic** | Vaudville Configurator manages the AI model file for the game.  Download new GGUF Models from Huggingface and let Vaudville Configurator manage everything else! | Low | GGUF File, must be related to or created from `Meta-Llama-3-8B-Instruct` (not 3.1 or later) only! |
| **Local Mode - Advanced** | Vaudville Configurator replaces the outdated and hardcoded Llamalib built into the game, with the latest llama.cpp release and allows you to select any GGUF model and tune all the parameters! | Moderate | Must be a GGUF File compatible with latest llama.cpp, must fit into local computer VRAM along with game! |
| **Remote Mode - OpenAI API Compatible Endpoint** | Vaudville Configurator intercepts the communcation with the outdated and hardcoded Llamalib built into the game, and lets you enter any Local or Remote OpenAI Compatible API Endpoint.  vLLM / SGLang / Llama.cpp / ExLlamaV3 / Ollama / OpenAI / Custom | Moderate to Difficult depending on Self Hosting or Remote API. | Only that the endpoint must support OpenAI API Chat Completions! |

The rest of this README follows that order: [getting it](#getting-it),
[quickstarts](#quickstarts), [the Characters page](#characters),
[the settings that matter](#settings-people-actually-change),
[what it cannot do](#what-this-tool-cannot-do), [backups](#backups-and-undo),
[platform notes](#platform-notes), [for the curious](#for-the-curious).

---

## Getting it

### Prebuilt binaries

Each release publishes two archives on the project's Releases page:

```text
Vaudville-Configurator.v<version>-linux-amd64.tar.gz     one Linux binary
Vaudville-Configurator.v<version>-windows-x86_64.zip     one Windows .exe
```

Unpack and run. The binary carries its own Python and its own Tk, so there is nothing to
install. Your settings, logs and backups go into your per-user folders, never next to the
executable. A single-file build unpacks itself into a temporary folder on every start,
which costs about a second.

Unsigned one-file executables occasionally upset antivirus heuristics. If yours objects,
allow-list the file — or run from source instead.

### From source

Python 3.10 or newer with tkinter. On Arch that is `sudo pacman -S tk`; the python.org
Windows installer includes it if you leave *tcl/tk and IDLE* ticked. Nothing else is
needed on any platform.

```bash
python3 vaudville_configurator.py            # the GUI
```

For a command on your PATH:

```bash
cd /path/to/Vaudville-Configurator
ln -sfn "$PWD/vaudville_configurator.py" ~/.local/bin/vaudville-configurator
```

On Windows use `tools\vaudville-configurator.cmd`, which finds `py -3` or `python` for
you. Do not copy any of these files into the game's `Vaudeville_Data/` folder — Steam
verifies that tree.

### Building your own single-file binary

```bash
bash tools/build_package.sh        # Linux and macOS  ->  dist/vaudville-configurator
tools\build_package.cmd            # run ON Windows   ->  dist\VaudvilleConfigurator.exe
```

Both scripts make a throw-away `.venv-build/` virtualenv, install PyInstaller into it and
build. Your system Python is never touched. PyInstaller cannot cross-compile, so run the
`.cmd` on Windows — this project's release workflow instead builds the `.exe` inside a Wine
container on a Linux runner.

---

## Quickstarts

All three start the same way: close the game, start the tool, let it find your install.
The **Game folder** box at the top of the window shows what it found, and **Detect**,
**Browse…** and **Re-scan** are there if it got it wrong or you moved things.

On every page, **Preview changes** works out exactly what would be written and shows it
without touching a file. **Apply to game files** writes it, after making a backup.

### Local Mode - Basic

1. Start the tool. On the first page choose **Local Mode - Basic** and press **Continue**.
2. In *Models — Meta-Llama-3-8B-Instruct family only*, pick a model file from your
   collection, or **Browse…** to any GGUF on the computer, then press **Use this model**.
   Your original file is kept and nothing is overwritten.
3. Optional: open **Characters** and change how the cast talks (see below).
4. **Preview changes**, then **Apply to game files**.
5. Launch Vaudeville from Steam as usual. Nothing else has to be running.

Basic mode only accepts model files from the `Meta-Llama-3-8B-Instruct` family — not 3.1
or later — because that is what the shipped game data expects. The tool refuses anything
else here rather than letting you break the game; use one of the other two setups for
other models. Drop new GGUF files into the folder behind **Open models folder** and press
**Refresh list** to see them.

### Local Mode - Advanced

1. Start a llama.cpp server with any recent model file. Leave it running:

   ```bash
   llama-server -m ~/models/Your-Model-Q4_K_M.gguf \
                --host 127.0.0.1 --port 8080 -c 8192 -ngl 99 --jinja
   ```

2. Start the tool, choose **Local Mode - Advanced**, press **Continue**.
3. Under *Your server*, set **Address** to `127.0.0.1` and **Port** to `8080`. The tool
   tells you live whether the address fits — it has to be 9 to 12 characters, with no
   `https://` and no `/v1` path.
4. Keep a small model file linked as *Main dialogue model*. The game's loading screen
   waits for its own engine to start even in this setup, and the shipped
   `Qwen3-0.6B-Q4_K_M.gguf` starts in about a second.
5. Set the *AI engine settings* if you want to, then **Preview changes** and
   **Apply to game files**.
6. Play, with your server still running.

Your server must speak the llama.cpp protocol (`/health`, `/apply-template`,
`/completion`, `/tokenize`). `llama-server` does. OpenAI-style services — Ollama, LM
Studio, vLLM, OpenAI — do not; use Remote mode for those.

### Remote Mode - OpenAI API Compatible Endpoint

1. Know your service's three details: the BaseURL, the model name it expects, and an API
   key if it wants one. Some common ones:

   ```text
   Ollama       http://127.0.0.1:11434/v1     e.g. llama3.1:8b
   LM Studio    http://127.0.0.1:1234/v1      the loaded model id
   vLLM         http://127.0.0.1:8000/v1      the served id
   llama.cpp    http://127.0.0.1:8080/v1      anything it serves
   OpenAI       https://api.openai.com/v1     e.g. gpt-4o-mini
   OpenRouter   https://openrouter.ai/api/v1  e.g. meta-llama/llama-3.1-70b-instruct
   ```

2. Start the tool, choose **Remote Mode - OpenAI API Compatible Endpoint**, **Continue**.
3. Fill in **BaseURL**, **Model name** and **API key**. Tick *Save API key in my config
   file* only if you want it kept on disk (the file is readable by your user alone).
   **Load from Bitwarden…** pulls one out of an unlocked vault.
4. Under *The translator on this computer*, press **Start**, then **Test**. A one-line
   answer in the log means it works; **Log** opens the translator's own log if it does not.
5. As with Advanced, keep the small model file linked as *Main dialogue model* — the
   loading screen still waits for the local engine.
6. **Preview changes**, then **Apply to game files**.
7. Play. The translator carries on running after you close this window; **Stop** ends it.

Remote mode has none of Advanced mode's length limits, because the game only ever talks to
the translator on `localhost` and the translator holds the real address, model name and
key. It also handles https, and it strips the `<think> …</think>` narration that reasoning
models emit so it cannot end up spoken aloud — untick *Hide "thinking" text* to keep it.

---

## Characters

Every speaking part in Vaudeville has its own saved copy of the word-choice settings, and
this tool edits them in groups. The groups are the places in town where you meet people —
the police station, the morgue, the circus, the theatre, the grocery shop, the manor, the
country club, the forest, the bar and the library — plus one extra group, *Workshop /
Story Editor roles*, which covers the same thirteen roles reused when you write and play
your own custom mysteries. That is eleven groups and twenty-six characters in total.

The **Which characters?** dropdown chooses who your changes apply to. Pick a place to
change just the people you meet there, or pick *All characters (26)* to change everybody
at once. Groups you leave alone keep exactly the settings they have now, so you can give
the shopkeeper a calmer style while everybody else carries on improvising.

Two things worth clearing up:

- **The "levels" are places, not difficulty.** The game's asset files are named `level4`
  through `level13`, which invites the idea that they are rungs on a difficulty ladder.
  They are not. There is no difficulty setting for these characters anywhere in the game,
  so there is no difficulty scale to offer here — just the ten locations.
- **You cannot rewrite a personality here.** A character's personality comes from the game's own script files, which the game reloads every time you play, so it cannot be rewritten here. What you can change is how each character answers - how creative, how repetitive, how long-winded - and those dials are the same set for every character.

**Load current values** reads what the first character of the chosen group uses right now
and puts it in the boxes. **Clear boxes** empties them, and an empty box means *leave that
setting alone*. **Reset this group to game defaults** fills the boxes with the values a
fresh install has, for the selected group; **Reset ALL groups to game defaults** does the
same for every character. Neither writes anything until you press **Apply to game files**.

Hover any setting name for what it is, what changing it does, its valid range and a few
recommended values.

---

## Settings people actually change

| setting | shipped | what it means |
|---|---|---|
| `temperature` | 0.2 | How wild the AI is allowed to be. 0.1 is robotic, 0.7–0.9 is lively improv, above 2.0 prepare for insanity. |
| `topK` | 40 | How many candidate words stay on the shortlist. Smaller is safer and duller; 0 or -1 keeps everything. |
| `topP` | 0.9 | Keeps only the words that together cover this share of the likely choices. Lower is tamer. |
| `minP` | 0.05 | A floor: ignore any word far less likely than the current favourite. 0 switches it off. |
| `repeatPenalty` | 1.1 | How hard the AI is discouraged from reusing words it just said. 1.0 is off. |
| `repeatLastN` | 64 | How far back that rule looks. -1 polices the whole conversation. |
| `numPredict` | -1 | The longest one line of dialogue may be, in word-fragments. -1 lets them finish. |
| `mirostat` | 0 | An autopilot that steadies the surprise level. 0 is off, 2 is the good one. |
| `seed` | 0 | The starting number for the AI's dice. 0 rolls fresh every time. |
| `cachePrompt` | on | Keeps the scene warm in memory so the next reply starts much faster. |
| `contextSize` | 8192 | How much of the conversation the AI holds in mind at once. Bigger remembers more and starts slower. |
| `numThreads` | -1 | How many of your processor's workers the AI may use. -1 is all of them; leave some for the game. |
| `numGPULayers` | 0 | How much of the model runs on your graphics card. The in-game Options slider overrides this at every boot. |
| `model` | `Meta-Llama-3-8B-Instruct-Q4_K_M.gguf` | Which model file the engine loads. |
| `host` / `port` | `localhost` / 13333 | Where the characters phone for their lines. |
| `remote` | off | On a character: get lines over the network. On the engine: let other programs use the model. |

`temperature` through `cachePrompt` live on the **Characters** page and can be set per
group. `contextSize` through `numGPULayers` are the engine's own settings and appear on
the **Local Mode - Advanced** page. `model`, `host`, `port` and `remote` are handled by
the mode pages for you.

Those are the ones people touch. There are 46 settings in total across the characters and
the engine, and every one of them has a hover tooltip in the app explaining what it is,
what changing it does, its valid range and a few recommended values with opinions attached.

---

## What this tool cannot do

Honest limits, because they shape which setup you should pick.

**Text has to fit the space already reserved for it.** This tool edits the game's files in
place, and a Unity file has no spare room: a string can only be replaced by another string
that rounds up to the same byte length. So:

- `host` ships as `localhost` (9 characters), which means your replacement must be 9 to 12
  characters. `127.0.0.1`, `192.168.1.50` and `myserver.lan` fit. `gpu-box.local`
  (13 characters), anything starting `https://` and anything with a `/v1` path does not.
- `APIKey`, `grammar`, `save`, `SSLCert`, `SSLKey`, `lora` and `loraWeights` all ship
  empty, and an empty slot has no room for any text at all. They can never be filled in.
  Your real API key therefore lives in the translator, which is exactly what **Remote
  Mode** is for.
- `model` accepts 33 to 36 characters, which is why the tool links one of the game's two
  fixed filenames at your chosen file rather than renaming anything.

Numbers and on/off switches are always four bytes, so those patch freely.

**Personalities do not stick.** The game reloads every character's backstage note from
its own script data each time a character loads, so anything written into that field is
silently discarded on the next launch — see [Characters](#characters).

**Graphics-card offload is not really yours to set.** Whatever `numGPULayers` says in the
files, the game overwrites it at boot from the AI/GPU slider on its own Options screen.
Change it in the game, then start a new game or restart for it to take effect.

**The game must be closed.** Writes are refused while Vaudeville is running, and the tool
says so at the top of the window.

---

## Backups and undo

Before any write, the tool copies the game files it is about to change into a timestamped
folder outside the game tree, checks each copy against a SHA-256 hash, and records a
`manifest.json` with the before and after hashes and a label for each edit.

```text
Linux, macOS   ~/.local/share/vaudville-configurator/backups/<UTC timestamp>-<game folder>/
Windows        %LOCALAPPDATA%\vaudville-configurator\data\backups\...
```

The **Backup / restore** page lists every backup with its timestamp, file count, game
folder and what changed. Select one and press **Restore selected** to copy those bytes
back and re-verify them; **Open backup folder** shows them in your file manager;
**Refresh** re-reads the folder after a command-line apply. Backups are kept until you
remove them, newest first.

From the command line:

```bash
vaudville-configurator --cli backups
vaudville-configurator --cli restore                    # latest
vaudville-configurator --cli restore --backup NAME --yes
```

Steam's own *verify integrity of game files* is a second, independent way back to a fresh
install.

---

## Platform notes

Linux is the primary target. Windows is supported end to end, and macOS has no known
blockers but is untested.

**Windows.** The Steam install is found through the registry
(`HKCU`/`HKLM\...\Valve\Steam`, then the usual `C:\Program Files (x86)\Steam`), and the
same library-parsing code as Linux takes it from there. "Is the game running" uses
`tasklist`. Your settings live in `%APPDATA%\vaudville-configurator`, and logs, the
cached scan profile and backups in `%LOCALAPPDATA%\vaudville-configurator\{state,data}`;
XDG variables still win if you have set them. No admin rights are needed for anything
except creating real symlinks, and that case falls back automatically to a same-volume
hardlink, which the game cannot tell apart. A hardlink needs the model file on the same
drive as the game — if yours is not, move it into
`Vaudeville_Data\StreamingAssets\gguf\` first. Two small quality-of-life notes: adding the
game folder to Defender's exclusions stops it re-reading several gigabytes of model file on
every scan, and `tests/*.sh` are bash scripts, so run them from Git Bash or WSL (or use
`--selftest --live`, which exercises the same native code path).

**Linux.** Steam libraries come from `libraryfolders.vdf`, plus `~/.steam`, Flatpak Steam,
`$STEAMPATH`, a copy next to the current directory, and the current directory itself.
Settings live in `~/.config/vaudville-configurator`, backups in
`~/.local/share/vaudville-configurator`, and logs plus the cached scan profile in
`~/.local/state/vaudville-configurator`.

---

## For the curious

**Headless mode.** Everything the GUI does is available without it:

```bash
vaudville-configurator --cli detect
vaudville-configurator --cli list [--show-prompts]
vaudville-configurator --cli models
vaudville-configurator --cli set-model --slot primary|deck --model /path/to/Model.gguf
vaudville-configurator --cli plan  --mode remote --backend-url http://127.0.0.1:11434/v1 \
                                   --backend-model llama3.1:8b --set temperature=0.8
vaudville-configurator --cli apply ...same flags... --yes
vaudville-configurator --cli backups
vaudville-configurator --cli restore [--backup NAME] --yes
vaudville-configurator --cli shim-start | shim-status | shim-test | shim-stop
```

`--mode` accepts the new names and the old internal keys interchangeably: `basic` or
`off`, `advanced` or `direct`, `remote` or `shim`. Other useful flags: `--game-dir PATH`,
`--rescan`, `--set NAME=VALUE` and `--llm-set NAME=VALUE` (both repeatable),
`--api-key-env VAR` to read the key from the environment instead of a command line,
`--dry-run`, `--no-backup`, `--tab NAME`, `--geometry WxH`, `--verbose`, `--version`.

**Self-test.** `vaudville-configurator --selftest` runs the built-in verification suite —
mode names and aliases, platform helpers, the scanning and patching logic, and a full
translator cycle against a mock backend. Add `--live` to also drive the game's own native
library, or `--ci` to skip the parts that need a real install.

**How the game finds its AI.** The characters' settings are ordinary serialised fields
inside the shipped Unity assets: one engine block in `Vaudeville_Data/sharedassets3.assets`,
thirteen characters across `level4` to `level13`, and thirteen more Workshop roles in
`sharedassets18.assets`. Player builds ship without type trees, so the tool finds those
blocks by structural signature, range-checks every field to make sure it has not
mis-guessed, caches the offsets per build, and re-reads the file before and after writing.

**The translator.** In Remote mode the game's native library speaks the llama.cpp server
protocol — `POST /health`, `/apply-template`, `/completion`, `/tokenize`, `/detokenize`,
`/embeddings`, with optional streaming — not the OpenAI protocol. The built-in translator
accepts exactly that and forwards it to any OpenAI-compatible service as
`/v1/chat/completions` (or `/v1/completions` if you pick *raw*), translating the stream
back. Two details it gets right that are easy to get wrong: it never sends the
`data: [DONE]` terminator, because the game's HTTP layer treats that as a failed transfer
and retries six times before throwing the reply away; and it strips `<think> …</think>`
blocks even when a tag is split across two chunks, so reasoning text never reaches the
speech synthesiser. The API key goes to the translator through the environment, never
through a command line.

The same translator code lives standalone in `tools/llamalib_shim.py`, and `tests/` has
end-to-end scripts that run it against a mock backend or a real `llama-server`.

**Releases.** `.forgejo/workflows/release.yml` builds both single-file binaries and
attaches them to a release whenever the version in `VERSION` changes, or on demand from
*Actions → release → Run workflow* with a `Major`, `minor` or `retry` choice. The Windows
`.exe` is cross-built in a Wine container on a Linux runner and smoke-tested there before
upload.

---

## Credit and legal

Vaudville Configurator is written by **MidnightPhreaker + Qwen**.
Source, issues and releases: <https://github.com/MidnightPhreaker/Vaudville-Configurator>

This is a community tool. It is not affiliated with or endorsed by Bumblebee Studios, and
it only reads and writes your own local copy of a game you own. Please do not redistribute
the developer's assets, prompts or bundled models. Llama-3 weights carry Meta's community
licence; Qwen3 is Apache-2.0. The upstream projects this works alongside are
`undreamai/LLMUnity` v3.0.0 and `undreamai/LlamaLib` v2.0.0.
