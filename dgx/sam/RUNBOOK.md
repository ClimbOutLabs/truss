# SAM on the DGX: runbook (work item E)

Goal: SAM serving on the DGX next to llama-server, finding objects by
noun phrase in real glasses photos, and the kept evidence photo of a
firing getting a copy with the object outlined.

Checked against `github.com/facebookresearch/sam3` at commit `f1647e8`
(2026-10-23) on 2026-10-28. What the code says, and the kit assumes:

- Checkpoints: `facebook/sam3.1` has `sam3.1_multiplex.pt`, the
  default; `facebook/sam3` has `sam3.pt`, the fallback. Both gated, both
  approved for you on 2026-10-28; the download fetches both.
- The image API is `build_sam3_image_model()` plus `Sam3Processor`:
  `set_image(photo)`, then `set_text_prompt(phrase)` gives boxes,
  masks and scores. One image pass serves every phrase, so three
  phrases cost one encoder run plus three cheap prompt runs.
- SAM 3.1's change is Object Multiplex, for video tracking. Its
  checkpoint is built for the video model; whether its detector loads
  cleanly into the image model is **not verified**. The service counts
  the weights a checkpoint leaves unloaded and, for sam3.1, refuses to
  start if any are, so sam3.1 can never quietly serve a broken model;
  `serve` then starts sam3 by itself and says `FALLBACK:`. Still-image
  detection is SAM 3 accuracy either way, so nothing is lost. For sam3
  the service warns and starts, as the upstream loader does.
- Wants Python 3.12+, PyTorch 2.7+ (README installs 2.10.0), CUDA
  12.6+. The package forgets `einops`; the script adds it.

**Not verified from here** (no DGX, no PyTorch index from this
sandbox): that PyTorch 2.10.0 has an aarch64 cu130 wheel with sm_121
kernels, and that triton installs on the GB10. The install stops with
a plain `STOP:` line if either is wrong, and runs real bf16 work on
the GPU against the CPU before it says yes.

## 1. Install, download, serve (about 15 min, mostly the PyTorch download)

Glimmer can stay up; SAM needs a few GB beside it.

    cd ~/truss && git pull
    ./dgx/sam/setup.sh all

`hf auth login` from the Glimmer bring-up is reused. Both checkpoints
download (a few GB each). On success it prints which model it serves,
the health reply and the checkpoint line:

    Up after ...s, serving sam3.1.
    sam3.1_multiplex.pt: N tensors loaded, 0 model weights missing, ...
    Warm-up done in ...s.

`0 model weights missing` is the line that matters. If sam3.1 does not
fit, you see its `STOP:` line, then `FALLBACK: ... serving sam3
instead`, and `serving sam3`: that is fine, say which one served when
you paste the result. If sam3 itself reports more than 0 missing, it
starts anyway with a WARNING line: paste that into the thread before
trusting the acceptance numbers. The service
listens on all interfaces, port 8090, with no key by default (it warns).
On a network you do not trust:

    ./dgx/sam/setup.sh stop
    SAM_API_KEY=some-long-secret ./dgx/sam/setup.sh serve
    export TRUSS_SAM_KEY=some-long-secret       # on the laptop

That key crosses the LAN in plain http: it keeps other devices off the
service, nothing more. Never use the Meta key for it.

## 2. The acceptance run (10 min)

Pick three glasses photos with the stove (or the pill organizer) in
them, one of them at an angle or partly in view. Put them in
`dgx/sam/photos/` (git-ignored; photos of the house never reach the
public repo). From the laptop:

    export TRUSS_SAM_URL=http://<dgx-ip>:8090
    python3 dgx/sam/sam_accept.py dgx/sam/photos/*.jpg --phrases "stove,knob"

or with a planted question's own compiled phrases:

    python3 dgx/sam/sam_accept.py dgx/sam/photos/*.jpg --folder truss-server/questions/<slug>

It prints each photo: the time per frame (round trip, and SAM's own
share), what was found with its score, whether every mask is inside
its box (checked here by decoding the masks), and the outlined copy.
It saves the outlined photos, `rows.jsonl` and `summary.md` under
`dgx/sam/results/<time>/`.

Pass, per the brief:

- three photos, each with a time per frame (expect well under a second
  of SAM time once warm; the first request after a start is slower);
- masks inside their boxes: `yes` on every row. A summary line about
  masks "under 90% inside their boxes before the cut" means SAM's
  masks and boxes disagree: the checkpoint did not load right;
- **open the outlined photos**: the yellow line must follow the
  object's edge, not a box around it. That one is by eye.

Paste `summary.md` into the thread; that is the E result.

## 3. The evidence outline in the real loop (5 min)

On the laptop, the server outlines evidence when told to:

    export TRUSS_SAM_URL=http://<dgx-ip>:8090
    export TRUSS_EVIDENCE_OUTLINE=1
    python3 truss-server/server.py

It says `Evidence photos will be outlined by SAM at ...` at start. Plant
a stove question as usual. It needs a `prompts.txt` in its folder: the
plant-time compile (item B) writes one; until that is merged, write it
by hand, one phrase per line:

    printf 'stove\nknob\n' > truss-server/questions/<slug>/prompts.txt

Make it fire (look at the stove with the glasses, or send a photo the
check answers yes to). The server prints `Outlined stove in
..._outline.jpg (N ms on SAM)`, and the copy sits next to the evidence
photo in `truss-server/log/<day>/`. The evidence photo itself is
unchanged. If SAM is down or finds nothing, the answer is spoken as
always and the server says why there is no outline.

## 4. Choosing the model by hand

    ./dgx/sam/setup.sh stop
    SAM_VERSION=sam3 ./dgx/sam/setup.sh serve      # sam3 only
    SAM_FALLBACK=0 ./dgx/sam/setup.sh serve        # sam3.1 or stop, no fallback

To compare the two (when sam3.1 served), run step 2 once on each with
the same photos, and paste both summaries.

## Troubleshooting

- `STOP: No torch==2.10.0 for this machine`: open the `TORCH_INDEX`
  page in a browser, pick a listed torch with an `aarch64` cp312 wheel,
  and rerun with `TORCH_VERSION=<it> ./dgx/sam/setup.sh install`. The
  cu129 and cu128 indexes are the next ones to try.
- `STOP: the GPU could not run PyTorch kernels` (often "no kernel image
  is available"): that wheel has no sm_121 code. Try a newer
  `TORCH_VERSION`, or the cu130 nightly
  (`TORCH_INDEX=https://download.pytorch.org/whl/nightly/cu130`).
  Last resort: NVIDIA's PyTorch container for the DGX
  (`nvcr.io/nvidia/pytorch`, the newest tag), with this repo and the
  checkpoint mounted, running `pip install -e sam3 einops` and
  `python dgx/sam/sam_service.py` inside it.
- `No triton for this machine`: check `pip index versions triton` in
  the venv; the sam3 model imports it for its tracker even on images.
- `sam3 does not import`: the error names the missing module; install
  it with `~/sam/venv/bin/pip install <name>` and rerun install.
- Every call from the laptop fails with "did not answer": wrong
  `TRUSS_SAM_URL`, or the DGX firewall blocks 8090.
- Out of memory: `nvidia-smi` shows what holds memory; Aman's model
  may still be resident.
- Nothing found where the object is plainly in view: try a plainer
  phrase (one or two words, no adjectives: "stove", not "gas stove
  burner"), or `--threshold 0.3`. Note what worked for the compile.

## Not built here (on purpose)

The SAM presence gate (frames reach a question's verdict only when SAM
sees its object) waits for app sampling (item G). Its guard is ready
in `truss-server/gate.py`: after 10 frames in a row with no SAM match
on a due question, the gate opens for that question and says so in
`log/<day>/gate.txt`. The eval harness's locate step (item F) can call
`sam_client.segment` the same way.
