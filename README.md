# Truss

**Trust the Truss. Your witness for your life.**

Truss watches for the answers to questions you leave standing around your
day: "Is the stove off?" "Did I take my night pills?" "Is the front door
locked?" You plant each question once, with two photos from your own home:
what yes looks like and what no looks like. As you go about your day wearing
Meta glasses, the photos they take are checked only against the questions
that are due. When a photo answers one, Mannie, the voice of Truss, speaks
first, without being asked, and the photo that answered it is kept on your
own laptop as evidence. Photos that answer nothing are deleted.

Built for the Meta Global AI Developer Hackathon (Indie track, "Build for
Your World"). Every model call in Truss goes to a Meta model, and in the
demo every one of them runs on hardware in the home.

## Who it is for

An older woman living alone who forgets: the gas stove, her pills, whether
the dog was fed. She can look at the stove herself. What fails is trusting
her own memory of having looked, so she frets for hours or does the thing
again to be safe. A second dose of the night pills is where the harm is.

Truss does not sell seeing. Seeing is free. It sells remembering, at the
moment she is about to do it twice. Her daughter or a caretaker can plant
the questions for her.

## Two kinds of question

- **State questions** ask how the world is now: is the stove off, is the
  door locked. The current photo answers them.
- **Done questions** ask about something she did: did I take my night pills.
  Here the current state can lie (a fed dog's bowl is empty an hour later
  because he ate), so the answer comes from witnessed history. A done
  question has a time window, such as 9 to 10 PM. The first time the glasses
  see the yes-look inside the window, Truss keeps that one photo and its
  time as the witness. If she comes back to the organizer later in the
  window, Mannie says once that it is already done, and when. If the window
  closes with no witness, an alert is raised that survives restarts and
  clears only when the phone confirms it was spoken.

Truss uses done questions only where the act leaves a trace that does not
undo itself: a pill organizer compartment that goes empty, a deadbolt that
turns. A bowl that fills and then empties, or pills taken from a bottle,
need act recognition, which Truss does not attempt.

## Safety posture

The failure designed against is a wrong all-clear, above all on the stove
and the pills.

- **Cannot tell, instead of a guess.** Every check returns `yes`, `no` or
  `cannot_tell`, with a one-sentence reason naming what in the photo decides
  it, and what is missing when it cannot tell. A yes or no with no reason
  is read as cannot tell. A check that fails for any reason (timeout, a
  reply cut off, invalid JSON) is never a verdict.
- **Description over verdict.** A missed pills window never says "you
  didn't take your pills": she may have taken them with the glasses off,
  and that sentence is how Truss would cause the double dose it exists to
  prevent. It tells her where to look instead ("Check the Tuesday PM
  compartment first, before you act on this"), and a test fails the build
  if any alert sentence holds an action verb such as take, turn, feed or
  lock (`truss-server/test_safety.py`).
- **Coverage is stated.** A missed-window alert says how much of the window
  was actually watched, counted from the photos that were really checked:
  the whole window, from 9:10 to 9:50, or not at all.
- **The evidence is always kept with the answer**, and every line in the
  answer log names the model that made the call.

Truss reduces fretting and redoing. It is not the last line of defense on a
stove.

## Architecture

```
 Meta glasses            Android phone                 Laptop (home)                  The DGX (home)
 (Oakley Meta    BT  ┌──────────────────┐  LAN  ┌──────────────────────────┐  LAN  ┌──────────────────────────┐
  Vanguard)  ──────► │ Truss app        │ ────► │ truss-server             │ ────► │ llama-server             │
  photo + mic        │ Wearables toolkit│ HTTP  │ Python standard library  │ HTTP  │ Muse Glimmer 30B (GGUF)  │
             ◄────── │ capture, diff,   │ ◄──── │ question store (files)   │ ◄──── │ OpenAI-compatible API    │
  speaker            │ speak, alerts    │       │ check loop, done windows │       ├──────────────────────────┤
                     └──────────────────┘       │ alerts, evidence, log    │ ────► │ SAM 3.1 service          │
                                                │ Whisper STT, local TTS   │       │ (evidence outline)       │
                                                └──────────────────────────┘       └──────────────────────────┘
```

- **Glasses and phone (`app/`).** An Android app on Meta's Wearables Device
  Access Toolkit takes photos and voice from the glasses over Bluetooth and
  sends them to the laptop. The camera session always ends before any
  answer is spoken, because the toolkit session and the Bluetooth audio
  channel cannot be held open together. Watch hands-free takes a photo every
  15, 30 or 60 seconds and skips one that is nearly identical to the last
  photo sent. The app talks only to the laptop: no cloud SDKs, no analytics.
  See [`app/README.md`](app/README.md).
- **Laptop server (`truss-server/`).** Python standard library only, no
  database, no cloud account. Each question is a plain folder: its words,
  the yes and no reference photos, its watch pattern, and two files written
  at plant time (below). Answers go to a plain-text log per day, with the
  evidence photos beside it. Only questions that are due ever see a photo,
  and a question rests once it has fired. See
  [`truss-server/README.md`](truss-server/README.md).
- **Checking.** One OpenAI-compatible `chat/completions` endpoint with
  image inputs and a strict JSON schema reply. By default it is Muse
  Glimmer 30B under llama.cpp's `llama-server` on an NVIDIA DGX Spark on
  the home network. The same code runs against the Meta Model API by
  changing two settings (`TRUSS_BASE_URL`, `TRUSS_MODEL`).
- **Plant-time compile.** When a question has both reference photos, one
  call to the same model writes `looks.txt` (in words, what tells this
  home's yes-look from its no-look, such as the knob positions) and
  `prompts.txt` (two or three short noun phrases for SAM: `stove`,
  `pill organizer`). Checks then read the current photo plus `looks.txt`.
- **Evidence outline.** When a question fires, SAM 3.1 on the DGX can find
  the object by those phrases and save a copy of the evidence photo with
  the object outlined. It runs in the background and can never delay or
  stop a spoken answer.
- **Voice.** Whisper (whisper.cpp) on the laptop for speech to text, the
  laptop's own text to speech back through the phone to the glasses.

### Where your data goes

Photos go from the glasses to the phone to the laptop, and from the laptop
to the DGX for the check. All three are in the home. A photo no question
fires on is deleted after its check. Nothing is uploaded to any cloud
service in the demo configuration.

## Meta models used

| Model | Where it runs | What it does | Status |
|---|---|---|---|
| **Muse Glimmer 30B** (open weights, `meta-models/Muse-Glimmer-30B-GGUF`, Q4_K_M with its mmproj and dflash drafter) | Locally, llama.cpp `llama-server` on an NVIDIA DGX Spark | Every check, and the plant-time compile | Running; answers real glasses photos end to end |
| **SAM 3.1** (open weights, `facebook/sam3.1`; SAM 3 as fallback) | Locally, a small HTTP service on the same DGX (`dgx/sam/`) | Outlines the object in kept evidence photos | <!-- TODO: SAM acceptance result --> _Kit built; live result pending_ |
| **Muse Spark 1.3** (Meta Model API, standard tier `muse-spark-1.3`) | Meta's API | Optional: the same checker code with different settings; the eval harness's API mode | <!-- TODO: used or not --> _Not used in the demo_ |

Rules Truss holds itself to:

- No non-Meta model makes any check in the demo or the eval.
- Never the Muse Spark contributor tier, which trains on what it is sent.
  Any model name containing `contributor` is refused in code before
  anything is sent.
- An API key is never sent over plain HTTP to another machine, so the
  local `llama-server` runs without one.

How to bring the models up on a DGX Spark:
[`dgx/RUNBOOK.md`](dgx/RUNBOOK.md) (Glimmer) and
[`dgx/sam/RUNBOOK.md`](dgx/sam/RUNBOOK.md) (SAM).

## Eval

`eval/` is a harness of 20 staged household questions, five of them safety
questions (stove, oven, front door, tonight's pills, hair straightener)
where a false yes is a false all-clear. Frames are split into tuning and
held-out; prompts are tuned on tuning only, and numbers are reported on
held-out. The false all-clear rate on safety questions is reported as its
own headline number, never averaged into overall accuracy. One question is
a cannot-tell probe, where a confident answer is wrong.

The headline comparison is generic questions (the question alone) against
planted questions (with the home's own reference looks).

<!-- TODO: fill from the F v0/v1 run. Mock numbers in eval/SCORECARD.md only validate the harness. -->

| Held-out, Muse Glimmer 30B | Generic | Planted |
|---|---|---|
| Accuracy | _TBD_ | _TBD_ |
| Safety false all-clear rate | _TBD_ | _TBD_ |
| Miss rate | _TBD_ | _TBD_ |
| Cannot-tell rate | _TBD_ | _TBD_ |
| Frames (n) | _TBD_ | _TBD_ |

## Cost card

Truss is designed so that the expensive thing, a model looking at a photo,
happens rarely. The gates that keep photos away from the model are all free:

1. **Due-question gating.** A photo is checked only against questions that
   are due now (an interval, a nightly time, or a done window). With
   nothing due, no model call is made.
2. **Rest once fired.** A question that has answered rests until its next
   due time.
3. **On-phone diff.** The phone skips a photo whose 16 by 16 grey
   thumbnail is within 8 levels of the last one sent in the past two
   minutes.

A photo-a-second design was costed and rejected.

**Method.** Cost per day = checks per day × cost per check. In the demo
configuration the cost per check is the DGX's electricity for the seconds
the check takes; there is no per-call fee. For comparison, the same check
over the Meta Model API is priced from its measured tokens at Muse Spark
1.3 standard rates ($1.25 per million input tokens, $4.25 per million
output tokens), and an outline over the API at SAM 3.1's $2.50 per 1,000
images. On a worn device, battery bounds watching before dollars do, so
wear-hours per charge sits on the card next to cost per day.

<!-- TODO: fill from the eval run (latency, tokens) and the one-hour wear (watch-log.txt). -->

| | Measured |
|---|---|
| Checks per day (bedtime round + daytime questions) | _TBD_ |
| Time per check on the DGX, p50 / p95 | _TBD_ |
| Tokens per check, in / out | _TBD_ |
| Cost per day, local (electricity) | _TBD_ |
| Cost per day, same checks over the Meta Model API | _TBD_ |
| Photos sent per hour of wear, and skipped by the diff | _TBD_ |
| Glasses battery per hour of wear at the demo cadence | _TBD_ |

In the first live end-to-end run, a glasses photo took about 11 to 13
seconds from arriving at the laptop to Glimmer's verdict, and about
15 seconds to Mannie speaking.

## Repository

| Path | What |
|---|---|
| `app/` | Android companion app (Kotlin, Compose, Meta Wearables toolkit) |
| `truss-server/` | Laptop server: question store, check loop, done windows, alerts, voice, evidence |
| `dgx/` | Bring-up and bench scripts for Muse Glimmer 30B under `llama-server` |
| `dgx/sam/` | Local SAM 3.1 service, its setup script and acceptance run |
| `eval/` | Eval harness: question set, capture tool, runner, scorer |
| `REVIEW.md` | The review checklist every change goes through |

## Run it

You need an Android phone, Meta glasses with Developer Mode on in the Meta
AI app, a Mac on the same network (the server uses macOS `say`,
`afconvert` and the `en0` interface), and a machine that can serve Muse
Glimmer 30B (an NVIDIA DGX Spark here).

1. Serve Glimmer: `./dgx/bringup.sh all` on the DGX
   ([`dgx/RUNBOOK.md`](dgx/RUNBOOK.md)).
2. Optional, the evidence outline: `./dgx/sam/setup.sh all` on the DGX
   ([`dgx/sam/RUNBOOK.md`](dgx/sam/RUNBOOK.md)).
3. On the Mac:

   ```bash
   export TRUSS_BASE_URL=http://<dgx-ip>:8080/v1
   python3 truss-server/server.py
   ```

   For speech to text, `brew install whisper-cpp` and the model file in
   [`truss-server/README.md`](truss-server/README.md).
4. Build and install the app ([`app/README.md`](app/README.md)), enter
   the laptop's address under Settings, connect the glasses, and plant a
   question.

Tests are plain Python, standard library only: run any
`truss-server/test_*.py` with `python3`.

## Beyond the demo

- **Sensor-agnostic.** Nothing in the server knows the photos come from
  glasses: a question, its two looks, and a frame. The worn, first-person
  viewpoint is the point of Truss, because it sees what she sees where she
  is, but the same store and checks would take frames from any camera.
- **The guide.** `truss-server/guide.py` is an over-the-shoulder mode: a
  job stated as steps, each step a question a glance can settle, spoken one
  at a time and checked before the next. It checks only presence, absence,
  count, position and order, never a safety verdict, keeps no photos, and
  is not in the demo.

## Built during the event

All code in this repository was written during the hackathon, starting
October 26, 2026. Pre-existing pieces it relies on:

- Meta's Wearables Device Access Toolkit; the app's registration and
  permission flow follows its CameraAccess sample.
- llama.cpp, Whisper (whisper.cpp), and the SAM 3 code from
  `facebookresearch/sam3`, installed by the scripts, not vendored.
- Hanken Grotesk, under the SIL Open Font License
  ([`third_party/fonts/`](third_party/fonts/)).
- Before Glimmer was running locally, early end-to-end checks went through
  a bridge to the author's own local assistant, a separate project from
  before the event that runs a non-Meta model (`truss-server/aman_check.py`).
  It is off by default, never used in the demo or the eval, and nothing it
  answered counts as a Truss result.
