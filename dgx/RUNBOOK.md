# Glimmer bring-up on the DGX: runbook (work item D)

Goal for the day: Muse Glimmer 30B answering a real glasses photo
through the Truss checker, then a 30-photo bench that says how fast it
is and whether multi mode can become the default.

Checked against the model card at
`huggingface.co/meta-models/Muse-Glimmer-30B-GGUF` on 2026-10-28. What
the card says, and the script assumes:

- Files: `Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf` (16.8 GB),
  `mmproj-Muse-Glimmer-30B-Q4_K_M.gguf` (1.4 GB, needed for images),
  `dflash-Muse-Glimmer-30B-Q4_K_M.gguf` (1.6 GB drafter), all in that
  one repo. meta-models has no 8-bit file; Q8_0 (29.6 GB) is on the
  Unsloth mirror, which has no Q4_K_M (its nearest is UD-Q4_K_XL).
- llama.cpp b10353 or later; b10344 and older cannot load the model.
- Drafter flags `-md <dflash file> -ngld 99`. `--jinja` is required.
- Glimmer always thinks before it answers (`--reasoning off` does
  nothing). Strength is low, medium, high (default) or xhigh. The
  script serves it at low, because the checker's reply cap
  (300 tokens in `verdict.py`) includes the thinking.
- Up to 4,096 visual tokens per image, so a three-image multi check
  needs ~12k tokens of context before the prompt.

## 1. Once: Hugging Face access (2 min)

Accept the license on the meta-models page, then on the DGX:

    hf auth login        # or: huggingface-cli login

If access has not come through, the script falls back to the Unsloth
mirror by itself (UD-Q4_K_XL instead of Q4_K_M).

## 2. Download, build, serve (about 30 min, mostly download and compile)

    cd ~/truss && git pull
    ./dgx/bringup.sh all

It stops with a plain `STOP:` line if anything is missing (nvcc, a tag
older than b10353, the drafter flags). A download that fails for any
reason other than a refused license stops too, instead of switching to
the mirror. When it finishes it prints the warm-up reply, how much
Glimmer thought (should be short at strength low), the timings of that
one request, and the `LLAMA_TAG` to pin so the Q4 and Q8 runs use the
same build.

The server listens on all interfaces (`HOST=0.0.0.0`) so the laptop can
reach it, with no key by default; it warns about that. On any network
you do not trust, serve with a key and give the laptop the same one:

    API_KEY=some-long-secret ./dgx/bringup.sh serve
    export TRUSS_API_KEY=some-long-secret     # on the laptop

The key goes to llama-server in a file, never on a command line.

## 3. Check the flags by eye (2 min)

    ./dgx/bringup.sh flags
    ./dgx/bringup.sh status

Look for:

- `-md, --model-draft` and `-ngld, --gpu-layers-draft` in the flags. If
  the build renamed them, edit the two lines under "The drafter" in
  `bringup.sh`, or serve without it: `DRAFT=0 ./dgx/bringup.sh serve`.
- In status: `n_ctx_slot` around 32768, the draft model loading, and
  all layers offloaded.

## 4. The acceptance check (item A's test, 1 min)

From the laptop (or on the DGX with the default URL):

    export TRUSS_BASE_URL=http://<dgx-ip>:8080/v1
    python3 truss-server/check_once.py some-glasses-photo.jpg "Is the stove off?"

Pass: a `yes`, `no` or `cannot_tell` with a one-sentence reason that
names something really in the photo, and `"model": "muse-glimmer-30b"`.
A reason ending "So no check was made." is a failure, not a verdict:
go to Troubleshooting.

## 5. The bench (about 15 min of your time, ~10 min of running)

Make the set: 10 glasses photos and the yes/no reference pair for each
of 3 questions (stove, door, pills is the natural set). Make at least
3 of the 10 deliberately uninformative for every question (ceiling,
floor, a blurred turn, the stove from behind with the knobs out of
view), and label the rest.

    mkdir -p dgx/bench/photos dgx/bench/refs
    cp dgx/bench-set.example.json dgx/bench/bench-set.json
    # put photos in dgx/bench/photos, refs in dgx/bench/refs,
    # and edit labels in bench-set.json to match what each photo shows

`dgx/bench/` is git-ignored, so photos of the house never reach the
public repo. A question can also point at a planted question folder
(`"folder": "..."`) to use its references and `looks.txt` as planted.

Run it:

    python3 dgx/bench_glimmer.py dgx/bench/bench-set.json

That is 10 × 3 × 2 modes = 60 checks. It prints each check as it goes,
then a summary, and saves `rows.jsonl` and `summary.md` under
`dgx/results/<time>/`.

Then the drafter comparison, same set, single mode only:

    ./dgx/bringup.sh stop && DRAFT=0 ./dgx/bringup.sh serve
    python3 dgx/bench_glimmer.py dgx/bench/bench-set.json --modes single
    ./dgx/bringup.sh stop && ./dgx/bringup.sh serve   # back to the drafter

## 6. Reading the summary

- **total p50**: the brief expects 6 to 10 s per verdict before the
  drafter. Decode speed is the tok/s line.
- **failed checks** should be 0. If "replies cut off by max_tokens" is
  above 0, the thinking ate the reply cap: rerun with
  `--max-tokens 600`; if that fixes it, `verdict.MAX_TOKENS` needs
  raising in the server (tell Iggy the number).
- **cannot_tell on uninformative frames**: the safety number. It should
  be near 100%. Every line under "yes where the label was no or
  cannot_tell" is a false all-clear candidate; read each reason.
- **multi vs single**: agreement, and which mode is right more often on
  labelled frames. Multi becomes the default only if it is at least as
  right as single, with no extra wrong yeses, at a time cost you
  accept.
- **drafter acceptance** above zero proves the drafter is in use.

Paste `summary.md` into the thread; that is the D result.

## Troubleshooting

- `STOP: ... older than b10353`: `LLAMA_TAG=` unset picks the newest
  tag; check `git -C ~/src/llama.cpp tag | sort -V | tail`.
- CUDA compile errors about the architecture: the toolkit may be too
  old for sm_121. Try `CUDA_ARCH=native ./dgx/bringup.sh build`.
- Load fails or runs out of memory: Aman's model may still be resident
  on the DGX. Stop it first (`nvidia-smi` shows what holds memory).
- Every check fails with "did not answer": wrong `TRUSS_BASE_URL`, or
  the server is on 127.0.0.1 only (default HOST is 0.0.0.0).
- "answered with an error (status 400)" on every check: the server is
  refusing the JSON-schema request; the checker retries without it
  once. If both fail, open `dgx/results/<time>/rows.jsonl` and look at
  `statuses`, then `tail ~/models/muse-glimmer/run/llama-server.log`.
- Verdicts poor on household scenes: per the brief, the fallback is
  Llama 3.2 Vision via Ollama behind the same `TRUSS_BASE_URL`. Never
  DeepSeek.

## After Oct 30: Q8

    ./dgx/bringup.sh stop
    QUANT=Q8_0 ./dgx/bringup.sh download
    QUANT=Q8_0 ./dgx/bringup.sh serve

Q8_0 comes from the Unsloth mirror; mmproj and the drafter stay the
meta-models Q4_K_M ones. Rerun the bench and compare.
