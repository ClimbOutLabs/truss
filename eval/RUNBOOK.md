# First eval numbers: runbook

One command on the DGX turns your staged photos into the scorecard, through the
same checker the server runs (Muse Glimmer on llama-server) and SAM on the DGX.
Only Meta models: the run stops if the model server is serving anything other
than `muse-glimmer-30b`.

## 1. Take the photos

Stage each question in your home and take the photos with the glasses, from where
you would naturally stand. Do the safety questions first:

| folder | question | yes looks like | no looks like |
|---|---|---|---|
| `q01-stove-off` | Is the stove off? | all knobs at off | one burner on |
| `q02-front-door-locked` | Is the front door locked? | deadbolt engaged | deadbolt open |
| `q03-pills-tonight-empty` | Is tonight's pill compartment empty? | tonight's compartment empty | tonight's pills present |
| `q04-oven-off` | Is the oven off? | dial at off | oven on, low |
| `q15-hair-straightener-unplugged` | Is the hair straightener unplugged? | unplugged | plugged in, light on |

The other 15 are in `eval/questions.yaml` (folder name = `id-slug`, e.g.
`q13-tv-off`).

Per question, aim for:

- **2 reference photos**: `ref-yes.jpg` and `ref-no.jpg`, the looks you would plant.
- **Frames**: 2 or more yes, 2 or more no. A name starting with `hard` marks a hard
  case (a burner on low, the door shut but unlocked).
- **1 or 2 cannot_tell frames** on each safety question: the deciding detail is not
  in view (knobs out of frame, pill organizer lid closed, too dark). The right
  answer there is cannot_tell, and a yes counts as a false all-clear.

Where the photos come from: the Meta AI app saves glasses photos to the phone's
gallery; or, with no questions standing on the Mac server, every photo the Truss
app sends stays in `truss-server/captures/`. Photos must be JPEG (or PNG/WebP);
export HEIC as JPEG first.

## 2. Drop them in, on the DGX

```
~/truss/eval/dataset/q01-stove-off/ref-yes.jpg
~/truss/eval/dataset/q01-stove-off/ref-no.jpg
~/truss/eval/dataset/q01-stove-off/yes/<any name>.jpg
~/truss/eval/dataset/q01-stove-off/no/<any name>.jpg
~/truss/eval/dataset/q01-stove-off/cannot_tell/<any name>.jpg
```

From the Mac, for example:
`scp -r q01-stove-off <user>@<dgx-ip>:~/truss/eval/dataset/`

The folder can also be named just `q01` or `stove-off`. `eval/dataset/` is
git-ignored; your photos never go into the repo.
`python3 eval/build_manifest.py --check` shows what it sees, per question.

## 3. Run it, on the DGX

```
cd ~/truss && git pull
./dgx/bringup.sh serve          # Glimmer on :8080, if not already up
./dgx/sam/setup.sh serve        # SAM on :8090 (optional; skipped if down)
python3 eval/first_numbers.py
```

It prints what each question has, checks Glimmer and SAM, compiles each
question's `looks.txt` from its two reference photos (the plant-time compile), shows
a time estimate (about 10 s per check; generic and planted are two checks per frame)
and waits for Enter. Then it checks every frame and writes the scorecard to
`eval/runs/first/SCORECARD.md` and `eval/SCORECARD.md`.

- Stop any time with Ctrl-C. The same command picks up where it left off, and
  retries checks that never got an answer (timeouts). A wrong or malformed reply
  is kept as scored, not retried.
- Add more photos later and run the same command: only the new frames are checked.
  A question whose reference photos change is recompiled and redone.
- `--modes generic,planted,planted-photos` also checks with the two reference
  photos as images (multi mode). `--questions q01,q03` limits the run.
  `--fresh` starts over. `--no-sam` skips SAM.
- If PyYAML is missing: `sudo apt install python3-yaml`.

## What the scorecard says

- **generic**: the frame and the question only. **planted**: plus the looks text
  compiled from her reference photos, which is what the product does.
- **Safety false all-clear**: on a safety question, a yes while the truth is no or not
  in view. The number that matters most.
- **Cannot tell**: its own rate; correct on cannot_tell frames and on q20.
- **Failed checks**: no answer came back (timeout, server error). Counted apart, not
  as the model saying it cannot see.
- **SAM**: how often SAM found the question's objects (the compiled `prompts.txt`
  names), by true answer, and its time per photo.

Commit `eval/SCORECARD.md` when the numbers are worth keeping; the per-run results
in `eval/runs/` stay on the DGX.
