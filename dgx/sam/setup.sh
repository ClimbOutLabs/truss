#!/usr/bin/env bash
# SAM on the DGX for Truss (work item E).
#
# Installs SAM 3 from github.com/facebookresearch/sam3 into its own
# venv with a CUDA PyTorch for the GB10, downloads the checkpoint from
# Hugging Face, and keeps dgx/sam/sam_service.py warm on port 8090 so
# the laptop (truss-server/sam_client.py) can reach it at
# http://<dgx>:8090.
#
#   ./setup.sh all        install, download, serve (the usual first run)
#   ./setup.sh install    venv, PyTorch, the sam3 code; checks the GPU
#   ./setup.sh download   the checkpoint only
#   ./setup.sh serve      start the service in the background, wait
#                         until it has loaded and warmed up
#   ./setup.sh stop       stop the service this script started
#   ./setup.sh status     health, and the checkpoint load line
#
# Settings (environment variables, all optional):
#
#   SAM_VERSION    sam3.1 (default; facebook/sam3.1, sam3.1_multiplex.pt)
#                  or sam3 (facebook/sam3, sam3.pt). Still-image
#                  detection is SAM 3 accuracy either way. The sam3.1
#                  file is made for the video model and may not fit the
#                  image model: the service refuses it if it leaves
#                  image-model weights unloaded, and serve then falls
#                  back to sam3 by itself (download fetches both).
#   SAM_FALLBACK   1 (default): sam3.1 refused -> serve sam3. 0: stop.
#   SAM_HOME       default ~/sam (venv and the sam3 code)
#   SAM_MODELS_DIR default ~/models/sam (checkpoints, run files)
#   SAM_REF        sam3 commit to install; default the one this kit
#                  was written against (2026-10-23)
#   TORCH_INDEX    PyTorch wheel index; default cu130 (see install)
#   TORCH_VERSION  default 2.10.0, as the sam3 README installs
#   HOST           default 0.0.0.0, so the laptop can reach it
#   PORT           default 8090 (llama-server has 8080)
#   SAM_API_KEY    optional; when set, the service requires it and the
#                  laptop sets TRUSS_SAM_KEY to the same value. A key
#                  of its own: it crosses the LAN in plain http.
#   SAM_DTYPE      bf16 (default) or fp32
#
# Needs: git, python3.12 with venv (or uv), curl, the NVIDIA driver.
# facebook/sam3 is gated: accept it on huggingface.co (done for Sidd on
# 2026-10-28) and run `hf auth login` once; the token the Glimmer
# download used is the same one.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAM_VERSION="${SAM_VERSION:-sam3.1}"
SAM_FALLBACK="${SAM_FALLBACK:-1}"
SAM_HOME="${SAM_HOME:-$HOME/sam}"
SAM_MODELS_DIR="${SAM_MODELS_DIR:-$HOME/models/sam}"
SAM_REF="${SAM_REF:-f1647e8d0c1d280991f4d1b7d974c2704537fe66}"
TORCH_INDEX="${TORCH_INDEX:-https://download.pytorch.org/whl/cu130}"
TORCH_VERSION="${TORCH_VERSION:-2.10.0}"
HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-8090}"
SAM_API_KEY="${SAM_API_KEY:-}"
SAM_DTYPE="${SAM_DTYPE:-bf16}"

VENV="$SAM_HOME/venv"
PY="$VENV/bin/python"
CODE_DIR="$SAM_HOME/sam3"
RUN_DIR="$SAM_MODELS_DIR/run"
PID_FILE="$RUN_DIR/sam-service.pid"
LOG_FILE="$RUN_DIR/sam-service.log"

case "$SAM_VERSION" in
  sam3|sam3.1) ;;
  *) echo "STOP: SAM_VERSION must be sam3.1 or sam3." >&2; exit 1 ;;
esac

hf_repo() { echo "facebook/$1"; }
ckpt_for() {
  case "$1" in
    sam3)   echo "$SAM_MODELS_DIR/sam3/sam3.pt" ;;
    sam3.1) echo "$SAM_MODELS_DIR/sam3.1/sam3.1_multiplex.pt" ;;
  esac
}

say() { printf '\n== %s\n' "$*"; }
die() { printf 'STOP: %s\n' "$*" >&2; exit 1; }

# -- install ------------------------------------------------------------------

make_venv() {
  [ -x "$PY" ] && return 0
  mkdir -p "$SAM_HOME"
  say "Making a Python 3.12 venv at $VENV"
  if command -v uv >/dev/null 2>&1; then
    uv venv --seed --python 3.12 "$VENV"
  elif command -v python3.12 >/dev/null 2>&1; then
    python3.12 -m venv "$VENV" \
      || die "python3.12 could not make a venv. Install it (sudo apt install python3.12-venv) and rerun."
  else
    die "No python3.12 and no uv. DGX OS (Ubuntu 24.04) ships python3.12; or install uv."
  fi
}

# SAM needs a PyTorch built for this GPU. The GB10 is Blackwell (sm_121)
# on an ARM (aarch64) CPU, so the wheel must be an aarch64 CUDA build
# new enough to know it: cu130 by default. The check after the install
# runs real bf16 work on the GPU and compares it with the CPU, so a
# wheel that installs but has no kernels for this chip stops here, not
# halfway through the first photo.
gpu_check() {
  "$PY" - <<'EOF'
import sys
import torch
print("torch", torch.__version__, "built for CUDA", torch.version.cuda)
if not torch.cuda.is_available():
    sys.exit("STOP: torch sees no CUDA device (driver, or a CPU-only wheel).")
name = torch.cuda.get_device_name(0)
cap = torch.cuda.get_device_capability(0)
print("GPU:", name, "sm_%d%d" % cap, "| arch list:", " ".join(torch.cuda.get_arch_list()))
try:
    a = torch.randn(512, 512, device="cuda", dtype=torch.bfloat16)
    b = torch.randn(512, 512, device="cuda", dtype=torch.bfloat16)
    mm = (a @ b).float().cpu()
    ref = a.float().cpu() @ b.float().cpu()
    x = torch.randn(1, 3, 64, 64, device="cuda")
    w = torch.randn(8, 3, 3, 3, device="cuda")
    conv = torch.nn.functional.conv2d(x, w).cpu()
    conv_ref = torch.nn.functional.conv2d(x.cpu(), w.cpu())
    torch.cuda.synchronize()
except Exception as e:
    sys.exit(f"STOP: the GPU could not run PyTorch kernels ({type(e).__name__}: {e}).")
err = (mm - ref).abs().max().item() / ref.abs().max().item()
cerr = (conv - conv_ref).abs().max().item()
print(f"bf16 matmul relative error {err:.4f}, conv error {cerr:.5f}")
if err > 0.05 or cerr > 1e-2:
    sys.exit("STOP: the GPU's answers do not match the CPU's.")
print("GPU check passed.")
EOF
}

do_install() {
  command -v git >/dev/null 2>&1 || die "git not found."
  command -v nvidia-smi >/dev/null 2>&1 || die "nvidia-smi not found; the NVIDIA driver is needed."
  make_venv
  "$PY" -m pip install -q -U pip

  say "Installing PyTorch $TORCH_VERSION from $TORCH_INDEX"
  "$PY" -m pip install "torch==$TORCH_VERSION" torchvision --index-url "$TORCH_INDEX" \
    || die "No torch==$TORCH_VERSION for this machine at $TORCH_INDEX. Try TORCH_VERSION= set to a version listed there, or TORCH_INDEX=https://download.pytorch.org/whl/cu129 (RUNBOOK, Troubleshooting)."
  gpu_check || die "The GPU check failed (above). See RUNBOOK.md, Troubleshooting."

  if [ ! -d "$CODE_DIR/.git" ]; then
    say "Cloning facebookresearch/sam3 into $CODE_DIR"
    git clone https://github.com/facebookresearch/sam3 "$CODE_DIR"
  fi
  git -C "$CODE_DIR" fetch --quiet origin
  git -C "$CODE_DIR" checkout --quiet "$SAM_REF" \
    || die "sam3 has no commit $SAM_REF. Set SAM_REF to a commit or branch (main) and rerun."
  echo "sam3 at $(git -C "$CODE_DIR" log -1 --format='%h %cd' --date=short)"

  say "Installing sam3 and what its image path imports"
  # einops is imported by the model but not listed by the package;
  # pkg_resources (setuptools before 81) finds the tokenizer file.
  # Torch stays exactly the build installed above.
  "$PY" -m pip install -q -e "$CODE_DIR" einops "setuptools<81" "torch==$TORCH_VERSION" \
    --extra-index-url "$TORCH_INDEX"
  if ! "$PY" -c "import triton" 2>/dev/null; then
    echo "triton is missing; installing it (the sam3 model imports it)."
    "$PY" -m pip install -q triton || die "No triton for this machine. See RUNBOOK.md, Troubleshooting."
  fi
  "$PY" -c "
from sam3.model_builder import build_sam3_image_model
from sam3.model.sam3_image_processor import Sam3Processor
import numpy, PIL
print('sam3 imports fine; numpy', numpy.__version__, 'Pillow', PIL.__version__)
" || die "sam3 does not import (above). Install what the error names into $VENV and rerun."
  echo "Installed."
}

# -- download -----------------------------------------------------------------

download_one() {
  local version="$1" ckpt name out="$RUN_DIR/hf-download.log"
  ckpt=$(ckpt_for "$version"); name=$(basename "$ckpt")
  mkdir -p "$SAM_MODELS_DIR/$version" "$RUN_DIR"
  say "Downloading $(hf_repo "$version"): $name and config.json"
  for f in config.json "$name"; do
    local rc=0
    "$VENV/bin/hf" download "$(hf_repo "$version")" "$f" --local-dir "$SAM_MODELS_DIR/$version" 2>&1 | tee "$out" || rc=$?
    if [ "$rc" != 0 ]; then
      if grep -qiE '401|403|gated|restricted|RepositoryNotFound|Repository Not Found|access to model' "$out"; then
        echo "$(hf_repo "$version") refused access. Check 'hf auth login' (\"$VENV/bin/hf\" auth whoami) and the approval email."
        return 3
      fi
      die "Downloading $f failed (not an access refusal; see above). Rerun; finished files are kept."
    fi
  done
  ls -la "$ckpt"
}

# sam3.1 is the default; sam3 is fetched beside it as the fallback, so
# serve never has to stop for a download if sam3.1 does not fit.
do_download() {
  [ -x "$VENV/bin/hf" ] || die "No hf CLI in $VENV. Run: ./setup.sh install"
  local got=0
  download_one "$SAM_VERSION" && got=1
  if [ "$SAM_VERSION" = sam3.1 ] && [ "$SAM_FALLBACK" = 1 ]; then
    download_one sam3 && got=1 || echo "No sam3 fallback downloaded (above)."
  fi
  [ "$got" = 1 ] || die "No checkpoint could be downloaded."
}

# -- serve --------------------------------------------------------------------

running() { [ -f "$PID_FILE" ] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null; }

local_host() {
  case "$HOST" in
    0.0.0.0|::|127.0.0.1|localhost) echo 127.0.0.1 ;;
    *) echo "$HOST" ;;
  esac
}

health() { curl -sf -m 5 "http://$(local_host):$PORT/health" 2>/dev/null; }

# start_service VERSION: 0 when up, 4 when the service refused the
# checkpoint (image-model weights missing), else it stops the script.
start_service() {
  local version="$1" ckpt
  ckpt=$(ckpt_for "$version")
  [ -f "$ckpt" ] || { echo "No checkpoint at $ckpt."; return 4; }
  say "Starting the SAM service ($version, $SAM_DTYPE) on $HOST:$PORT (log: $LOG_FILE)"
  env PYTHONNOUSERSITE=1 SAM_VERSION="$version" SAM_CHECKPOINT="$ckpt" SAM_DTYPE="$SAM_DTYPE" \
    "${key_env[@]}" nohup "$PY" "$HERE/sam_service.py" --host "$HOST" --port "$PORT" \
    >"$LOG_FILE" 2>&1 &
  echo $! > "$PID_FILE"

  local waited=0
  until health >/dev/null; do
    if ! running; then
      tail -n 20 "$LOG_FILE"
      grep -q 'weights of the image model are not in' "$LOG_FILE" && return 4
      die "The SAM service exited during load (log above)."
    fi
    sleep 2; waited=$((waited + 2))
    [ "$waited" -lt 600 ] || die "The SAM service is not up after 10 minutes. See $LOG_FILE"
  done
  echo "Up after ${waited}s, serving $version."
}

do_serve() {
  [ -x "$PY" ] || die "No venv at $VENV. Run: ./setup.sh install"
  running && die "The SAM service is already running (pid $(cat "$PID_FILE")). Run: ./setup.sh stop"
  health >/dev/null && die "Something already answers on port $PORT. Stop it first, or set PORT."
  mkdir -p "$RUN_DIR"

  key_env=()
  if [ -n "$SAM_API_KEY" ]; then
    (umask 077; printf '%s\n' "$SAM_API_KEY" > "$RUN_DIR/api-key")
    key_env=("SAM_API_KEY_FILE=$RUN_DIR/api-key")
  else
    rm -f "$RUN_DIR/api-key"
    if [ "$HOST" != 127.0.0.1 ] && [ "$HOST" != localhost ]; then
      echo "WARNING: serving on $HOST with no SAM_API_KEY: anyone on this network can send it photos."
      echo "         Set SAM_API_KEY (and TRUSS_SAM_KEY on the laptop) on any network you do not trust."
    fi
  fi

  local rc=0
  start_service "$SAM_VERSION" || rc=$?
  if [ "$rc" = 4 ]; then
    rm -f "$PID_FILE"
    if [ "$SAM_VERSION" = sam3.1 ] && [ "$SAM_FALLBACK" = 1 ]; then
      echo
      echo "FALLBACK: sam3.1 could not be served as an image model (above); serving sam3 instead."
      echo "          Still-image detection is SAM 3 accuracy either way."
      rc=0
      start_service sam3 || rc=$?
      [ "$rc" = 0 ] || die "sam3 could not be served either (above). Run: SAM_VERSION=sam3 ./setup.sh download"
    else
      die "$SAM_VERSION could not be served (above). Run: ./setup.sh download, or try SAM_VERSION=sam3."
    fi
  fi
  do_status
  echo
  echo "Laptop: export TRUSS_SAM_URL=http://<dgx-ip>:$PORT  (and TRUSS_SAM_KEY if you set SAM_API_KEY)"
}

do_status() {
  if running; then echo "SAM service pid $(cat "$PID_FILE")"; else echo "The SAM service is not running (from this script)."; fi
  health || true; echo
  [ -f "$LOG_FILE" ] && grep -E 'tensors loaded|missing, first|Warm-up|listening|STOP' "$LOG_FILE" | tail -n 6 || true
}

do_stop() {
  running || { echo "Not running."; return 0; }
  local pid waited=0
  pid=$(cat "$PID_FILE")
  kill "$pid"
  while kill -0 "$pid" 2>/dev/null; do
    sleep 1; waited=$((waited + 1))
    if [ "$waited" -ge 20 ]; then kill -9 "$pid" 2>/dev/null || true; fi
    [ "$waited" -lt 30 ] || die "The SAM service (pid $pid) did not exit."
  done
  rm -f "$PID_FILE"
  echo "Stopped."
}

case "${1:-}" in
  all) do_install; do_download; do_serve ;;
  install) do_install ;;
  download) do_download ;;
  serve) do_serve ;;
  stop) do_stop ;;
  status) do_status ;;
  *) sed -n '2,16p' "$0"; exit 2 ;;
esac
