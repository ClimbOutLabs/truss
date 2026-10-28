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
#   CUDA_ARCH    default 121 (the GB10 is sm_121)
#   HOST         default 0.0.0.0, so the laptop can reach it on the LAN;
#                use 127.0.0.1 to keep it on the DGX only
#   PORT         default 8080
#   API_KEY      optional; when set, llama-server requires it and the
#                laptop sets TRUSS_API_KEY to the same value
#   CTX          default 32768 tokens for the one slot (see serve)
#   DRAFT        1 (default) loads the dflash drafter; 0 serves without
#                it, for the with/without speed comparison
#   REASONING    low (default) | medium | high | xhigh; Glimmer always
#                thinks first, this sets how much
#   REASONING_BUDGET  optional cap on thinking tokens (--reasoning-budget)
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
REASONING="${REASONING:-low}"
REASONING_BUDGET="${REASONING_BUDGET:-}"

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

hf_cli() {
  if command -v hf >/dev/null 2>&1; then
    hf "$@"
  elif command -v huggingface-cli >/dev/null 2>&1; then
    huggingface-cli "$@"
  else
    say "Installing the Hugging Face CLI for this user"
    python3 -m pip install --user -U "huggingface_hub[cli]" >/dev/null
    export PATH="$HOME/.local/bin:$PATH"
    hf "$@"
  fi
}

# hf_get REPO DEST PATTERN... ; returns non-zero when the repo refuses
# (gated without access, or missing), so the caller can fall back.
hf_get() {
  local repo="$1" dest="$2"; shift 2
  # One --include followed by every pattern: a repeated --include keeps
  # only the last one.
  hf_cli download "$repo" --include "$@" --local-dir "$dest"
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
    echo "The meta-models repo refused (gated, or no access yet). Trying the Unsloth mirror."
    echo "Unsloth publishes no Q4_K_M; its closest file is UD-Q4_K_XL (~16 GB)."
    local want="$QUANT"
    [ "$want" = "Q4_K_M" ] && want="UD-Q4_K_XL"
    hf_get "$UNSLOTH_REPO" "$unsloth" "*${want}*.gguf" "*mmproj*" "*dflash*" "*DFlash*" \
      || die "Neither $META_REPO nor $UNSLOTH_REPO could be downloaded. Check 'hf auth login' and the license on huggingface.co."
  elif [ "$QUANT" != "Q4_K_M" ]; then
    say "Downloading $UNSLOTH_REPO: $QUANT weights"
    hf_get "$UNSLOTH_REPO" "$unsloth" "*${QUANT}*.gguf" \
      || die "Could not download $QUANT from $UNSLOTH_REPO."
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

server_running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

curl_auth() {
  if [ -n "$API_KEY" ]; then curl -sS -H "Authorization: Bearer $API_KEY" "$@"; else curl -sS "$@"; fi
}

do_serve() {
  [ -x "$SERVER_BIN" ] || die "No llama-server at $SERVER_BIN. Run: ./bringup.sh build"
  server_running && die "llama-server is already running (pid $(cat "$PID_FILE")). Run: ./bringup.sh stop"
  resolve_files
  mkdir -p "$RUN_DIR"

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
    -ngl 99
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
  # checker's max_tokens (300 in verdict.py, thinking included).
  if help_has '--chat-template-kwargs'; then
    args+=(--chat-template-kwargs "{\"reasoning_strength\":\"$REASONING\"}")
  else
    echo "This build has no --chat-template-kwargs; reasoning strength stays at the template default (high)."
  fi
  if [ -n "$REASONING_BUDGET" ]; then
    args+=(--reasoning-budget "$REASONING_BUDGET")
  fi
  [ -n "$API_KEY" ] && args+=(--api-key "$API_KEY")

  say "Starting llama-server on $HOST:$PORT (log: $LOG_FILE)"
  printf '%q ' "$SERVER_BIN" "${args[@]}" | sed "s/--api-key [^ ]*/--api-key ***/" > "$RUN_DIR/command.txt"
  cat "$RUN_DIR/command.txt"; echo
  nohup "$SERVER_BIN" "${args[@]}" >"$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"

  # Loading ~20 GB from disk takes a minute or two the first time.
  local waited=0
  until curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; do
    server_running || { tail -n 40 "$LOG_FILE"; die "llama-server exited during load (log above)."; }
    sleep 3; waited=$((waited + 3))
    [ "$waited" -lt 600 ] || die "llama-server not healthy after 10 minutes. See $LOG_FILE"
  done
  echo "Healthy after ${waited}s."

  say "Warm-up request"
  curl_auth -m 300 "http://127.0.0.1:$PORT/v1/chat/completions" \
    -H 'Content-Type: application/json' \
    -d "{\"model\":\"$ALIAS\",\"messages\":[{\"role\":\"user\",\"content\":\"Reply with the word ready.\"}],\"max_tokens\":200}" \
    | python3 -c 'import json,sys; d=json.load(sys.stdin); t=d.get("timings",{}); print("reply:", (d["choices"][0]["message"].get("content") or "").strip()[:80]); print("timings:", {k: t[k] for k in ("prompt_n","prompt_ms","predicted_n","predicted_ms","draft_n","draft_n_accepted") if k in t})'
  do_status
  echo
  echo "llama-server stays loaded until './bringup.sh stop' or a reboot; it does not unload when idle."
}

do_status() {
  if server_running; then echo "llama-server pid $(cat "$PID_FILE")"; else echo "llama-server is not running (from this script)"; fi
  curl -s "http://127.0.0.1:$PORT/health" || true; echo
  [ -f "$LOG_FILE" ] && grep -E 'n_ctx_slot|n_ctx +=|draft|mmproj|offloaded' "$LOG_FILE" | tail -n 12 || true
}

do_stop() {
  if server_running; then
    kill "$(cat "$PID_FILE")"; rm -f "$PID_FILE"; echo "Stopped."
  else
    echo "Not running."
  fi
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
