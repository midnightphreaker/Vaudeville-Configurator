#!/usr/bin/env python3
"""Proof of concept: drive Vaudeville's OWN LlamaLib (llama.cpp) binary in
"remote client" mode against an arbitrary HTTP endpoint.

This is the exact code path the game uses when a LLMClient/LLMAgent component has
`remote = true`:

    LLMUnity.LLMClient.SetupCallerObject()
      -> new UndreamAI.LlamaLib.LLMClient(host, port, APIKey, numRetries)
        -> LLMClient_Construct_Remote(url, port, apiKey, numRetries)   [native]
    LLMUnity.LLMClient.SetCompletionParameters()
      -> LLM_Set_Completion_Parameters(ptr, jsonString)                [native]
    LLMUnity.LLMAgent.Chat()
      -> LLMAgent_Construct(ptr, systemPrompt) + LLMAgent_Chat(...)    [native]

Run:  python3 poc_remote_llamalib.py --port 13333
"""
import argparse, ctypes, json, os, sys, time
from pathlib import Path

_REL = ("Vaudeville_Data/StreamingAssets/LlamaLib-v2.0.0/linux-x64/native/"
        "libllamalib_linux-x64_avx2.so")


def find_native() -> str:
    """Locate the game's own libllamalib: $VLM_NATIVE_LIB, then any install we can see."""
    env = os.environ.get("VLM_NATIVE_LIB")
    if env:
        return str(Path(env).expanduser())
    here = Path(__file__).resolve()
    roots = [here.parents[2] / "Vaudeville",                       # sibling of the configurator
             Path.home() / "Workspace/Vaudeville",
             Path.home() / ".local/share/Steam/steamapps/common/Vaudeville",
             Path.home() / ".steam/steam/steamapps/common/Vaudeville",
             Path.cwd()]
    for r in roots:
        c = r / _REL
        if c.is_file():
            return str(c)
    return str(roots[0] / _REL)


NATIVE = find_native()

ap = argparse.ArgumentParser()
ap.add_argument("--lib", default=NATIVE)
ap.add_argument("--host", default="http://127.0.0.1")
ap.add_argument("--port", type=int, default=13333)
ap.add_argument("--api-key", default="")
ap.add_argument("--prompt", default="Hello! Who are you?")
ap.add_argument("--system", default="You are Biagio Ferrari, an entrepreneur in Vaudeville, 1914.")
args = ap.parse_args()

lib = ctypes.CDLL(args.lib)

CALLBACK = ctypes.CFUNCTYPE(None, ctypes.c_char_p)

lib.LLM_Debug.argtypes = [ctypes.c_int]
lib.LLM_Status_Code.restype = ctypes.c_int
lib.LLM_Status_Message.restype = ctypes.c_char_p
lib.LLMClient_Construct_Remote.restype = ctypes.c_void_p
lib.LLMClient_Construct_Remote.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
lib.LLMClient_Is_Server_Alive.restype = ctypes.c_bool
lib.LLMClient_Is_Server_Alive.argtypes = [ctypes.c_void_p]
lib.LLM_Set_Completion_Parameters.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lib.LLMAgent_Construct.restype = ctypes.c_void_p
lib.LLMAgent_Construct.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lib.LLMAgent_Chat.restype = ctypes.c_char_p
lib.LLMAgent_Chat.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_bool, CALLBACK,
                              ctypes.c_bool, ctypes.c_bool]
lib.LLM_Apply_Template.restype = ctypes.c_char_p
lib.LLM_Apply_Template.argtypes = [ctypes.c_void_p, ctypes.c_char_p]
lib.LLM_Tokenize.restype = ctypes.c_char_p
lib.LLM_Tokenize.argtypes = [ctypes.c_void_p, ctypes.c_char_p]

lib.LLM_Debug(1)

def status():
    return lib.LLM_Status_Code(), (lib.LLM_Status_Message() or b"").decode("utf-8", "replace")

print(f"[poc] loading {args.lib}")
client = lib.LLMClient_Construct_Remote(args.host.encode(), args.port,
                                        args.api_key.encode(), 5)
print("[poc] LLMClient_Construct_Remote ->", hex(client or 0), "status:", status())
if not client:
    sys.exit("remote client construction failed")

print("[poc] LLMClient_Is_Server_Alive ->", lib.LLMClient_Is_Server_Alive(client))

# Exactly the JSON LLMUnity.LLMClient.SetCompletionParameters() builds from the
# serialized component values found in the game's scenes.
params = {
    "temperature": 0.2, "top_k": 40, "top_p": 0.9, "min_p": 0.05, "n_predict": -1,
    "typical_p": 1.0, "repeat_penalty": 1.1, "repeat_last_n": 64,
    "presence_penalty": 0.0, "frequency_penalty": 0.0, "mirostat": 0,
    "mirostat_tau": 5.0, "mirostat_eta": 0.1, "seed": 0, "ignore_eos": False,
    "n_probs": 0, "cache_prompt": True,
}
lib.LLM_Set_Completion_Parameters(client, json.dumps(params).encode())
print("[poc] completion parameters set:", status())

tok = lib.LLM_Tokenize(client, b"Detective Martini arrives at the country club.")
print("[poc] LLM_Tokenize ->", (tok or b"")[:120])

tpl = lib.LLM_Apply_Template(client, json.dumps(
    [{"role": "system", "content": args.system},
     {"role": "user", "content": args.prompt}]).encode())
print("[poc] LLM_Apply_Template ->", repr((tpl or b"")[:160]))

agent = lib.LLMAgent_Construct(client, args.system.encode())
print("[poc] LLMAgent_Construct ->", hex(agent or 0), status())

chunks = []
def cb(s):
    txt = (s or b"").decode("utf-8", "replace")
    chunks.append(txt)
    print(f"[poc] stream chunk ({len(txt)} chars): {txt[-70:]!r}", flush=True)

t0 = time.time()
result = lib.LLMAgent_Chat(agent, args.prompt.encode(), True, CALLBACK(cb), False, False)
dt = time.time() - t0
print(f"[poc] LLMAgent_Chat returned in {dt:.2f}s, status={status()}")
print("[poc] FINAL TEXT:", repr((result or b'').decode('utf-8','replace')))
print("[poc] streamed callbacks:", len(chunks))
