#!/usr/bin/env bash
# Muse Glimmer 30B bring-up on the DGX (work item D).
#
# Downloads the weights, builds llama.cpp with CUDA for the GB10, and
# keeps llama-server warm on port 8080 so the Truss checker
# (truss-server/endpoint.py) can reach it at http://<dgx>:8080/v1.
#
#   ./bringup.sh all        download, build, serve (the usual first run)
#   ./bringup.sh download   weights only
#   ./bringup.sh build      llama.cpp only
#   ./bringup.sh serve      start llama-server in the background, wait
#                           until it is healthy, send one warm-up request
#   ./bringup.sh stop       stop the server this script started
#   ./bringup.sh status     health, slot context, draft model in use
#   ./bringup.sh flags      print the draft and reasoning flags the
#                           installed build knows, for checking by eye
#
# Settings (environment variables, all optional):
#
#   QUANT        Q4_K_M (default, ~17 GB, from meta-models)
#                Q8_0 (~30 GB; meta-models publishes no 8-bit file, so
#                this comes from the Unsloth mirror; mmproj and the
#                dflash drafter still come from meta-models)
#   MODELS_DIR   default ~/models/muse-glimmer
#   LLAMA_DIR    default ~/src/llama.cpp
#   LLAMA_TAG    llama.cpp release tag to build; default: the newest
#                bNNNNN tag, which must be b10353 or later (older builds
#                do not register the Glimmer architecture at all)
#   CUDA_ARCH    default 121 (the GB10 is sm_121); 120 for an RTX 50
#                series card such as the 5060 / 5060 Ti
#   NGL          layers on the GPU, default 99 (all). On a card smaller
#                than the model (RTX 5060 8 GB, 5060 Ti 16 GB) set fewer
#                and the rest run on the CPU; see RUNBOOK "On an RTX 5060"
#   HOST         default 0.0.0.0, so the laptop can reach it on the LAN;
#                use 127.0.0.1 to keep it on the DGX only
#   PORT         default 8080
#   API_KEY      optional; when set, llama-server requires it. Leave it
#                unset for the laptop: the Truss checker never sends a
#                key over plain http to another machine, so a keyed
#                server refuses it. On an untrusted network use
#                HOST=127.0.0.1 and an SSH tunnel (RUNBOOK section 2).
#   CTX          default 32768 tokens for the one slot (see serve)
#   DRAFT        1 (default) loads the dflash drafter; 0 serves without
#                it, for the with/without speed comparison
#   REASONING    low (default) | medium | high | xhigh; Glimmer always
#                thinks first, this sets how much
#   REASONING_BUDGET  optional cap on thinking tokens (--reasoning-budget)
#   IMAGE_MAX_TOKENS  optional cap on tokens per image (--image-max-tokens).
#                Unset, a large photo can take 3,000+ tokens and ~10 s to
#                read on the DGX; 1024 bounds that near 2.5 s. Check that
#                small details (a knob angle) still read before keeping it.
#
# Needs: git, cmake, a C++ compiler, the CUDA toolkit (nvcc), curl,
# python3. The meta-models repo may be gated: accept the license on
# huggingface.co and run `hf auth login` once before downloading.

set -euo pipefail

QUANT="${QUANT:-Q4_K_M}"
MODELS_DIR="${MODELS_DIR:-$HOME/models/muse-glimmer}"
LLAMA_DIR="${LLAMA_DIR:-$HOME/src/llama.cpp}"
LLAMA_TAG="${LLAMA_TAG:-}"
MIN_BUILD=10353
CUDA_ARCH="${CUDA_ARCH:-121}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8080}"
API_KEY="${API_KEY:-}"
CTX="${CTX:-32768}"
DRAFT="${DRAFT:-1}"
NGL="${NGL:-99}"
REASONING="${REASONING:-low}"
REASONING_BUDGET="${REASONING_BUDGET:-}"
IMAGE_MAX_TOKENS="${IMAGE_MAX_TOKENS:-}"

META_REPO="meta-models/Muse-Glimmer-30B-GGUF"
UNSLOTH_REPO="unsloth/Muse-Glimmer-30B-GGUF"
ALIAS="muse-glimmer-30b"   # the name TRUSS_MODEL defaults to

RUN_DIR="$MODELS_DIR/run"
PID_FILE="$RUN_DIR/llama-server.pid"
LOG_FILE="$RUN_DIR/llama-server.log"
SERVER_BIN="$LLAMA_DIR/build/bin/llama-server"

say() { printf '\n== %s\n' "$*"; }
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

# -- download -----------------------------------------------------------------

HF_BIN=""

# Finds the Hugging Face CLI, installing it if it is missing. DGX OS is
# Ubuntu 24.04, where a bare `pip install --user` is refused (PEP 668),
# so this tries pipx, then uv, then a private venv.
find_hf() {
  [ -n "$HF_BIN" ] && return 0
  export PATH="$HOME/.local/bin:$PATH"
  if command -v hf >/dev/null 2>&1; then HF_BIN=hf; return 0; fi
  if command -v huggingface-cli >/dev/null 2>&1; then HF_BIN=huggingface-cli; return 0; fi
  say "Installing the Hugging Face CLI"
  if command -v pipx >/dev/null 2>&1; then
    pipx install "huggingface_hub[cli]" >/dev/null || true
  elif command -v uv >/dev/null 2>&1; then
    uv tool install "huggingface_hub[cli]" >/dev/null || true
  else
    python3 -m venv "$MODELS_DIR/.hf-venv" \
      && "$MODELS_DIR/.hf-venv/bin/pip" install -q -U "huggingface_hub[cli]" \
      && export PATH="$MODELS_DIR/.hf-venv/bin:$PATH" || true
  fi
  if command -v hf >/dev/null 2>&1; then HF_BIN=hf; return 0; fi
  die "Could not install the Hugging Face CLI. Install it yourself (sudo apt install pipx; pipx install 'huggingface_hub[cli]') and rerun."
}

# hf_get REPO DEST PATTERN...
# One download per pattern: the newer typer-based `hf` reads extra
# values after a single --include as file names, and a repeated
# --include on the older CLI keeps only the last one.
# Returns 0 on success, 3 when the repo refuses access (gated, no
# token, or not visible), and stops the script on any other failure
# (network, disk, Ctrl-C), so a half-finished download never quietly
# turns into a different quant from the mirror.
hf_get() {
  local repo="$1" dest="$2"; shift 2
  find_hf
  mkdir -p "$dest" "$RUN_DIR"
  local out="$RUN_DIR/hf-download.log"
  for pattern in "$@"; do
    local rc=0
    # pipefail (set at the top) makes rc the CLI's status, not tee's.
    "$HF_BIN" download "$repo" --include "$pattern" --local-dir "$dest" 2>&1 | tee "$out" || rc=$?
    if [ "$rc" != 0 ]; then
      # Access refusals, in the old huggingface-cli wording and the
      # current hf wording ("Access denied. Model '...' requires
      # approval.", and "Model '...' not found." for a 401). A bare
      # "not found" or a bare 401/403 is not matched: network errors and
      # traceback line numbers say those too.
      if grep -qiE "40[13] Client Error|Unauthorized|Forbidden|gated|restricted|RepositoryNotFound|Repository Not Found|access to model|Access denied|requires approval|Model '[^']*' not found" "$out"; then
        return 3
      fi
      die "Downloading $pattern from $repo failed (not an access refusal; see above). Fix that and rerun; finished files are kept."
    fi
  done
}

do_download() {
  mkdir -p "$MODELS_DIR"
  local meta="$MODELS_DIR/meta" unsloth="$MODELS_DIR/unsloth"

  # mmproj and the dflash drafter: meta-models publishes them as
  # mmproj-Muse-Glimmer-30B-Q4_K_M.gguf and
  # dflash-Muse-Glimmer-30B-Q4_K_M.gguf, beside the Q4_K_M weights.
  local meta_ok=1
  if [ "$QUANT" = "Q4_K_M" ]; then
    say "Downloading $META_REPO: Q4_K_M weights, mmproj, dflash drafter (~20 GB)"
    hf_get "$META_REPO" "$meta" "*Q4_K_M.gguf" || meta_ok=0
  else
    say "Downloading $META_REPO: mmproj and dflash drafter"
    hf_get "$META_REPO" "$meta" "mmproj-*.gguf" "dflash-*.gguf" || meta_ok=0
  fi

  if [ "$meta_ok" = 0 ]; then
    echo "The meta-models repo refused access (gated, or no access yet). Trying the Unsloth mirror."
    [ "$QUANT" = "Q4_K_M" ] && echo "Unsloth publishes no Q4_K_M; its closest file is UD-Q4_K_XL (~16 GB)."
    local want="$QUANT"
    [ "$want" = "Q4_K_M" ] && want="UD-Q4_K_XL"
    # *mmproj* may pull every precision Unsloth publishes (1-2 GB each);
    # resolve_files uses the first.
    hf_get "$UNSLOTH_REPO" "$unsloth" "*${want}*.gguf" "*mmproj*" "*dflash*" "*DFlash*" \
      || die "Neither $META_REPO nor $UNSLOTH_REPO gave access. Check 'hf auth login' and the license on huggingface.co."
  elif [ "$QUANT" != "Q4_K_M" ]; then
    say "Downloading $UNSLOTH_REPO: $QUANT weights"
    hf_get "$UNSLOTH_REPO" "$unsloth" "*${QUANT}*.gguf" \
      || die "$UNSLOTH_REPO refused access to $QUANT."
  fi
  resolve_files
  echo "Weights:  $MODEL_FILE"
  echo "mmproj:   $MMPROJ_FILE"
  echo "Drafter:  ${DRAFT_FILE:-none found}"
}

# Sets MODEL_FILE, MMPROJ_FILE, DRAFT_FILE from what is on disk.
# meta-models files win over Unsloth ones for mmproj and the drafter.
resolve_files() {
  local all
  all=$(find "$MODELS_DIR/meta" "$MODELS_DIR/unsloth" -name '*.gguf' 2>/dev/null | sort || true)
  MMPROJ_FILE=$(printf '%s\n' "$all" | grep -i '/mmproj' | head -n1 || true)
  DRAFT_FILE=$(printf '%s\n' "$all" | grep -i 'dflash' | head -n1 || true)
  local want="$QUANT"
  MODEL_FILE=$(printf '%s\n' "$all" | grep -vi -e '/mmproj' -e 'dflash' | grep -- "$want" | head -n1 || true)
  if [ -z "$MODEL_FILE" ] && [ "$QUANT" = "Q4_K_M" ]; then
    MODEL_FILE=$(printf '%s\n' "$all" | grep -vi -e '/mmproj' -e 'dflash' | grep -- "UD-Q4_K_XL" | head -n1 || true)
  fi
  # Split files (-00001-of-0000N.gguf): llama.cpp wants the first part.
  [ -n "$MODEL_FILE" ] && [ -z "${MODEL_FILE##*-of-*}" ] && \
    MODEL_FILE=$(printf '%s\n' "$all" | grep -- "$want" | grep -- '-00001-of-' | head -n1)
  [ -n "$MODEL_FILE" ] || die "No $QUANT weights under $MODELS_DIR. Run: ./bringup.sh download"
  [ -n "$MMPROJ_FILE" ] || die "No mmproj file under $MODELS_DIR; images cannot be read without it."
  if [ "$DRAFT" = 1 ] && [ -z "$DRAFT_FILE" ]; then
    echo "No dflash drafter found; serving without it (DRAFT=0)."
    DRAFT=0
  fi
}

# -- build --------------------------------------------------------------------

do_build() {
  command -v nvcc >/dev/null 2>&1 || [ -x /usr/local/cuda/bin/nvcc ] \
    || die "nvcc not found. On DGX OS the CUDA toolkit is at /usr/local/cuda; add /usr/local/cuda/bin to PATH."
  command -v nvcc >/dev/null 2>&1 || export PATH="/usr/local/cuda/bin:$PATH"

  if [ ! -d "$LLAMA_DIR/.git" ]; then
    say "Cloning llama.cpp into $LLAMA_DIR"
    mkdir -p "$(dirname "$LLAMA_DIR")"
    git clone https://github.com/ggml-org/llama.cpp "$LLAMA_DIR"
  fi
  git -C "$LLAMA_DIR" fetch --tags --force --quiet origin

  local tag="$LLAMA_TAG"
  if [ -z "$tag" ]; then
    tag=$(git -C "$LLAMA_DIR" tag --list 'b[0-9]*' | sort -V | tail -n1)
  fi
  local n="${tag#b}"
  [[ "$n" =~ ^[0-9]+$ ]] || die "LLAMA_TAG=$tag is not a bNNNNN release tag."
  [ "$n" -ge "$MIN_BUILD" ] || die "$tag is older than b$MIN_BUILD, which cannot load Glimmer."

  say "Building llama.cpp $tag with CUDA for sm_$CUDA_ARCH"
  git -C "$LLAMA_DIR" checkout --quiet "$tag"
  cmake -S "$LLAMA_DIR" -B "$LLAMA_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DGGML_CUDA=ON \
    -DCMAKE_CUDA_ARCHITECTURES="$CUDA_ARCH" \
    -DLLAMA_CURL=OFF
  cmake --build "$LLAMA_DIR/build" -j "$(nproc)" --target llama-server llama-mtmd-cli
  "$SERVER_BIN" --version 2>&1 | head -n 4
}

# -- serve --------------------------------------------------------------------

help_has() { "$SERVER_BIN" --help 2>&1 | grep -qE -- "$1"; }

do_flags() {
  [ -x "$SERVER_BIN" ] || die "No llama-server at $SERVER_BIN. Run: ./bringup.sh build"
  "$SERVER_BIN" --version 2>&1 | head -n 2
  "$SERVER_BIN" --help 2>&1 | grep -E -- '-md,|--model-draft|-ngld|gpu-layers-draft|spec-draft|reasoning|chat-template-kwargs|--mmproj|image-(min|max)-tokens|--parallel' || true
}

# True only when the pid file names a live llama-server on this port, so
# a stale pid reused by some other process is never treated as ours.
# True when the pid in the pid file is still alive, whatever it is
# running. Right after launch the child may not have exec'd llama-server
# yet, so the load loop uses only this.
pid_alive() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

# True only when the pid file names a live process running this script's
# llama-server binary, so a stale pid reused by some other process is
# never treated as ours. Matched on the executable, not the arguments.
server_running() {
  pid_alive || return 1
  local pid exe want
  pid=$(cat "$PID_FILE")
  exe=$(readlink -f "/proc/$pid/exe" 2>/dev/null) || return 1
  want=$(readlink -f "$SERVER_BIN" 2>/dev/null) || return 1
  [ -n "$exe" ] && [ "$exe" = "$want" ] && return 0
  # A wrapper script (or an interpreter) runs as another executable but
  # carries the binary's path in its arguments.
  case "$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null)" in
    *"$want "*|*"$SERVER_BIN "*) return 0 ;;
  esac
  return 1
}

# Where this script reaches the server: loopback when it listens on all
# interfaces, else the address it was told to bind.
local_host() {
  case "$HOST" in
    0.0.0.0|::|127.0.0.1|localhost) echo 127.0.0.1 ;;
    *:*) echo "[$HOST]" ;;
    *) echo "$HOST" ;;
  esac
}

health_ok() { curl -sf -m 5 "http://$(local_host):$PORT/health" >/dev/null 2>&1; }

# The key goes in files only readable by this user, never on a command
# line, where `ps` would show it.
write_key_files() {
  [ -n "$API_KEY" ] || return 0
  mkdir -p "$RUN_DIR"
  (umask 077
   printf '%s\n' "$API_KEY" > "$RUN_DIR/api-key"
   printf 'Authorization: Bearer %s\n' "$API_KEY" > "$RUN_DIR/auth-header")
}

curl_auth() {
  if [ -n "$API_KEY" ]; then curl -sS -H @"$RUN_DIR/auth-header" "$@"; else curl -sS "$@"; fi
}

do_serve() {
  [ -x "$SERVER_BIN" ] || die "No llama-server at $SERVER_BIN. Run: ./bringup.sh build"
  server_running && die "llama-server is already running (pid $(cat "$PID_FILE")). Run: ./bringup.sh stop"
  health_ok && die "Something already answers on port $PORT (another llama-server?). Stop it first, or set PORT."
  resolve_files
  mkdir -p "$RUN_DIR"

  if [ "$HOST" != 127.0.0.1 ] && [ "$HOST" != localhost ] && [ -z "$API_KEY" ]; then
    echo "WARNING: serving on $HOST with no API_KEY: anyone on this network can use the model and send it images."
    echo "         Fine on your home LAN. On a network you do not trust, use HOST=127.0.0.1 and an SSH tunnel (RUNBOOK section 2)."
  fi

  # Context: one slot (-np 1), since one checker asks one question at a
  # time and llama-server splits -c across slots. A multi-mode check is
  # three images; Glimmer takes up to 4,096 visual tokens per image, so
  # 12,288 for the images, plus the prompt, the thinking and the reply.
  # 32768 leaves room for all of that; the card's own example is
  # -c 131072 -np 4. Check n_ctx_slot in the log after start.
  local args=(
    -m "$MODEL_FILE"
    --mmproj "$MMPROJ_FILE"
    -a "$ALIAS"
    -ngl "$NGL"
    -c "$CTX" -np 1
    --host "$HOST" --port "$PORT"
    --jinja
    --temp 1.0 --top-p 0.95 --top-k 64
  )

  # The drafter. The model card gives `-md <dflash file> -ngld 99`.
  # VERIFY these against the installed build before trusting the speed
  # numbers: run `./bringup.sh flags` and look for -md/--model-draft and
  # -ngld/--gpu-layers-draft. llama.cpp renames speculative flags often
  # (--draft-max is already gone in favor of --spec-draft-n-max). After
  # start, the log must show the draft model loading, and the bench's
  # draft_accepted column must be above zero; if not, the drafter is
  # not in use whatever the flags say.
  if [ "$DRAFT" = 1 ]; then
    help_has '(-md,|--model-draft)' || die "This build has no -md/--model-draft flag. Check './bringup.sh flags', or serve with DRAFT=0."
    help_has '(-ngld|--gpu-layers-draft)' || die "This build has no -ngld/--gpu-layers-draft flag. Check './bringup.sh flags', or serve with DRAFT=0."
    args+=(-md "$DRAFT_FILE" -ngld 99)
  fi

  # Glimmer's template always opens a thinking channel; --reasoning off
  # does nothing. Keep thinking short so a verdict stays inside the
  # checker's max_tokens (TRUSS_MAX_TOKENS, thinking included). The
  # flag existing does not prove the template reads reasoning_strength:
  # the warm-up below prints how much it thought, so check that it is
  # short.
  if help_has '--chat-template-kwargs'; then
    args+=(--chat-template-kwargs "{\"reasoning_strength\":\"$REASONING\"}")
  else
    echo "This build has no --chat-template-kwargs; reasoning strength stays at the template default (high)."
  fi
  # Thinking goes in reasoning_content, which the checker never reads,
  # so only the answer is parsed as the verdict.
  if help_has '--reasoning-format'; then
    args+=(--reasoning-format deepseek)
  else
    echo "This build has no --reasoning-format; thinking may reach the reply text (the checker strips it, but check the warm-up)."
  fi
  if [ -n "$REASONING_BUDGET" ]; then
    help_has '--reasoning-budget' || die "This build has no --reasoning-budget flag; unset REASONING_BUDGET."
    args+=(--reasoning-budget "$REASONING_BUDGET")
  fi
  if [ -n "$IMAGE_MAX_TOKENS" ]; then
    help_has '--image-max-tokens' || die "This build has no --image-max-tokens flag; unset IMAGE_MAX_TOKENS."
    args+=(--image-max-tokens "$IMAGE_MAX_TOKENS")
  fi

  if [ -n "$API_KEY" ]; then
    write_key_files
    if help_has '--api-key-file'; then
      args+=(--api-key-file "$RUN_DIR/api-key")
    else
      export LLAMA_API_KEY="$API_KEY"   # environment, not argv
    fi
  fi

  say "Starting llama-server on $HOST:$PORT (log: $LOG_FILE)"
  printf '%q ' "$SERVER_BIN" "${args[@]}" > "$RUN_DIR/command.txt"
  cat "$RUN_DIR/command.txt"; echo
  nohup "$SERVER_BIN" "${args[@]}" >"$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"

  # Loading ~20 GB from disk takes a minute or two the first time.
  local waited=0
  until health_ok; do
    pid_alive || { tail -n 40 "$LOG_FILE"; die "llama-server exited during load (log above)."; }
    sleep 3; waited=$((waited + 3))
    [ "$waited" -lt 600 ] || die "llama-server not healthy after 10 minutes. See $LOG_FILE"
  done
  server_running || { tail -n 40 "$LOG_FILE"; die "The server that answered is not the one just started (log above)."; }
  echo "Healthy after ${waited}s."

  # A key the build silently ignored would leave the server open: an
  # unauthenticated request must be refused.
  if [ -n "$API_KEY" ]; then
    local code
    # /health and /v1/models are public in llama-server; chat is not.
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 -H 'Content-Type: application/json' \
      -d '{"messages":[{"role":"user","content":"hi"}],"max_tokens":1}' \
      "http://$(local_host):$PORT/v1/chat/completions" || true)
    [ "$code" = 401 ] || die "With API_KEY set, a request without the key got status $code, not 401: the server is not enforcing the key. Stop it (./bringup.sh stop)."
  fi

  say "Warm-up request"
  curl_auth -m 300 "http://$(local_host):$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$ALIAS\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word ready.\"}],\"max_tokens\":400}" \
    | python3 -c '
import json, sys
raw = sys.stdin.read()
try:
    d = json.loads(raw)
    m = d["choices"][0]["message"]
except Exception:
    print("The warm-up did not get a normal reply:", raw[:300]); sys.exit(0)
t = d.get("timings", {})
print("reply:", (m.get("content") or "").strip()[:80])
print("thinking:", len(m.get("reasoning_content") or ""), "chars (at reasoning_strength low this should be short; hundreds of words means the setting is ignored)")
print("finish_reason:", d["choices"][0].get("finish_reason"))
print("timings:", {k: t[k] for k in ("prompt_n", "prompt_ms", "predicted_n", "predicted_ms", "draft_n", "draft_n_accepted") if k in t})
'
  do_status
  echo
  echo "llama-server stays loaded until './bringup.sh stop' or a reboot; it does not unload when idle."
  echo "Pin this build for the Q4 and Q8 runs: LLAMA_TAG=$(git -C "$LLAMA_DIR" describe --tags 2>/dev/null || echo '?')"
}

do_status() {
  if server_running; then echo "llama-server pid $(cat "$PID_FILE")"; else echo "llama-server is not running (from this script)"; fi
  curl -s -m 5 "http://$(local_host):$PORT/health" || true; echo
  [ -f "$LOG_FILE" ] && grep -E 'n_ctx_slot|n_ctx +=|draft|mmproj|offloaded' "$LOG_FILE" | tail -n 12 || true
}

do_stop() {
  if ! server_running; then
    echo "Not running."
    return 0
  fi
  local pid waited=0
  pid=$(cat "$PID_FILE")
  kill "$pid"
  # Wait for the process to exit and free the port, so a serve right
  # after this cannot find the old server still answering.
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1; waited=$((waited + 1))
    if [ "$waited" -ge 30 ]; then kill -9 "$pid" 2>/dev/null || true; fi
    [ "$waited" -lt 40 ] || die "llama-server (pid $pid) did not exit."
  done
  rm -f "$PID_FILE"
  echo "Stopped."
}

case "${1:-}" in
  all) do_download; do_build; do_serve ;;
  download) do_download ;;
  build) do_build ;;
  serve) do_serve ;;
  stop) do_stop ;;
  status) do_status ;;
  flags) do_flags ;;
  *) sed -n '2,20p' "$0"; exit 2 ;;
esac
