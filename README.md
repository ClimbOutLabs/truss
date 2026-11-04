# Truss

**Trust the Truss. Your witness for your life.**

Truss watches for the answers to questions you leave standing around your
day: "Is the stove off?" "Did I take my night pills?" "Is the front door
locked?" You plant each question once, with two photos from your own home:
what yes looks like and what no looks like. As you go about your day wearing
Meta glasses, the photos they take are checked only against the questions
that are due. When a photo answers one, Mannie, the voice of Truss, speaks
first, without being asked, and the photo that answered it is kept on your
own computer as evidence. Photos that answer nothing are deleted.
**Truss Memory** then shows, for any question, every look Truss took at it
today, in time order, with what it saw each time.

Built for the Meta Global AI Developer Hackathon (Indie track, "Build for
Your World"). Truss uses Meta models only: every check and every
plant-time compile goes to Muse Spark 1.3 over the Meta Model API, with
Muse Glimmer 30B on your own hardware as an optional fallback, and
evidence outlines come from SAM 3.1, also on your own hardware.

## Run it yourself

This section takes a developer with a pair of Meta glasses from a fresh
clone to Mannie answering a question, in about 30 minutes. Three machines
are involved, all on the same Wi-Fi network:

- **The glasses**: Meta glasses that Meta's Wearables Device Access
  Toolkit supports, paired with the Meta AI app on the phone. Truss was
  built and tested on Oakley Meta Vanguard.
- **The phone**: an Android phone (Android 12 or newer) running the Truss
  app. It takes the photos from the glasses and plays Mannie's voice back
  through them.
- **The computer**: runs the Truss server and the Truss Desk, a web page
  where you plant questions and watch what Truss sees. macOS, Windows or
  Linux.

### 1. What you need

| | |
|---|---|
| Python 3.10 or newer | The server is Python standard library only: nothing to `pip install`. On Linux, also `espeak-ng` for Mannie's voice. |
| A Meta Model API key | From the Meta Model API console. Checks go to Muse Spark 1.3. |
| Android Studio (Narwhal or newer) with JDK 17 and Android SDK 36 | To build the app. The Meta Wearables toolkit (`com.meta.wearable:mwdat-core` and `mwdat-camera` 1.0.1) comes from Maven Central by itself; there is nothing to download by hand. |
| The Meta AI app on the phone, with the glasses paired | And **Developer Mode** turned on for the glasses in the Meta AI app's settings. |
| Git | To clone this repository. |

Optional, for running everything in your own home with no cloud call:
an NVIDIA DGX Spark or a GPU machine with about 24 GB of memory, for Muse
Glimmer 30B and SAM 3.1 (step 8).

### 2. Get the code

```bash
git clone https://github.com/ClimbOutLabs/truss.git
cd truss
```

### 3. Start the server

The server is `truss-server/server.py`. It listens on port 8792 and prints
the address the phone and the Desk should use.

**macOS**

```bash
cd truss-server
python3 server.py
```

Mannie's voice is the Mac's built-in speech (`say`). For the most
natural voice, add a Premium or Enhanced English voice once in System
Settings > Accessibility > Spoken Content > System voice > Manage Voices,
then restart the server; its start-up banner names the voice it uses.

**Windows** (PowerShell, Python 3.10 or newer from python.org)

```powershell
cd truss-server
py server.py
```

Mannie's voice uses Windows' built-in speech engine; there is nothing to
install. `TRUSS_VOICE` can name an installed voice, such as
`"Microsoft Zira Desktop"`. Windows voices sound flatter than the Mac's
Premium ones. The server switches itself to UTF-8 mode;
`py -X utf8 server.py` also works. In PowerShell, settings are set with
`$env:NAME = "value"`, not `export`.

**Linux**

```bash
sudo apt install espeak-ng      # Mannie's voice; TRUSS_VOICE picks an espeak voice
cd truss-server
python3 server.py
```

On Windows and Linux the server uses the address of the network adapter
that routes to your network (on macOS, the Wi-Fi address). If it picks
the wrong one, such as a VPN, Docker, WSL or Ethernet instead of Wi-Fi,
set `TRUSS_HOST` to this computer's Wi-Fi IP (or `0.0.0.0` for every
adapter) and restart. With no speech engine installed the server still
runs and logs each line Mannie would say, but nothing is spoken in the
glasses. Windows support has been tested on Linux and with simulated
Windows calls, not yet on a real Windows machine.

On any system, the first start may raise a firewall prompt (on Windows,
Windows Defender Firewall): allow Python on private networks, or the
phone cannot reach port 8792.

### 4. Give the server your Meta key

With the key set, each photo that a due question is checked against is
sent to Meta's Model API, with the question's words and, at planting, its
two reference photos. Read [Where your data goes](#where-your-data-goes)
first; step 8 keeps everything in your home instead.

Either way below works; the key is never written into the repository,
never printed and never logged.

- **From the Desk (any system).** Open the Desk at
  `http://<the address the server printed>:8792/desk`, go to
  **Settings**, paste the key, and press **Test**. It is saved to
  `~/.truss/settings.json` (on Windows, `%USERPROFILE%\.truss\settings.json`),
  readable only by you, and takes effect at once.
- **From the shell**, before starting the server:

  ```bash
  export TRUSS_META_API_KEY="<your key>"            # macOS, Linux
  ```
  ```powershell
  $env:TRUSS_META_API_KEY = "<your key>"            # Windows PowerShell
  ```

  On a Mac only, you can keep it in the keychain instead of typing it:
  `export TRUSS_META_API_KEY="$(security find-generic-password -s truss-meta -w)"`.

To check the route: the Desk's **Test** button makes one words-only
Muse Spark call (no photo). With the key exported, the same check from a
terminal is `python3 truss-server/endpoint.py --smoke`.

### 5. Build and install the app

1. Open the **repository root** in Android Studio and let Gradle sync.
2. Android Studio writes `local.properties` with `sdk.dir`. With Developer
   Mode on in the Meta AI app, nothing else is needed: the toolkit's
   `mwdat_application_id` and `mwdat_client_token` default to `0`. Without
   Developer Mode, add both to `local.properties` from the Wearables
   Developer Center.
3. Plug in the phone with USB debugging on, choose the `app` run
   configuration, and press Run. From a terminal instead:
   `./gradlew installDebug` (on Windows, `gradlew.bat installDebug`).

### 6. Connect the phone, the glasses and the computer

1. Open Truss on the phone and grant the Bluetooth and nearby-devices
   permissions it asks for.
2. Tap **Connect glasses** on the Home screen (or **Connect** under
   Settings > Connections). The Meta AI app opens to approve Truss;
   approve it and come back.
3. **Settings > Connections > Laptop address**: type the address the
   server printed (just the IP, such as `192.168.1.20`), then tap
   **Check connection**.
4. Optional: **Settings > Diagnostics** takes one test photo and speaks a
   test sentence through the glasses, each with a step-by-step log.
   **Look now** on the Home screen takes one photo and checks it.

### 7. Plant a question and start watching

On the computer, open the Desk at `http://<address>:8792/desk`:

1. **Watch for me**: type the question the way you would say it
   ("Is the stove off?"), and say whether it is something to check or
   something you did.
2. Show Truss what yes and no look like: look at the thing with the
   glasses and press **Take the yes photo with the glasses**, then the
   same for no. You can also drop in two JPEGs.
3. Choose when Truss should look (every few minutes, from a time until it
   is done, or during set hours), tick **Be extra careful** for the stove,
   pills or anything where a wrong answer could do harm, and press
   **Start watching for me**. The phone starts watching within a few
   seconds while the Truss app is open.
4. Wear the glasses and go look at the thing. When a photo answers the
   question, Mannie says so through the glasses.

The other Desk tabs:

- **Today**: every question by time, with how it stands in a word
  (Watching, Needs its photos, Not seen, Yes), sample questions one click
  from planting, and a banner for any missed-window alert.
- **Live view**: what the glasses see now, and what Truss said. It can
  play Mannie's lines out loud on the computer too.
- **Watch me do this**: a guided task, one step at a time, each step
  checked before the next.
- **History**: a day's answers with their evidence photos.
- **Truss Memory**: pick a question and see every check of it today,
  oldest first: the time, what Truss decided, why, and the photo when one
  was kept. Checks that found nothing are listed too, so you can see that
  Truss was looking. The app has the same timeline under History.
- **Settings**: the Meta key and who answers the checks, each with a
  Test button.
- **Log**: the server's log, without a terminal.

Planting also works from the app (**Questions**), by typing.

### 8. Optional: run the models in your own home

To check with no cloud call, serve Muse Glimmer 30B under llama.cpp's
`llama-server` and point the server at it. On an NVIDIA DGX Spark (or a
Linux machine with a CUDA GPU):

```bash
./dgx/bringup.sh all            # downloads Glimmer, builds llama.cpp, serves on :8080
./dgx/sam/setup.sh all          # optional: SAM 3.1 for evidence outlines, on :8090
```

Then, where the server runs, set `TRUSS_BASE_URL=http://<gpu-machine>:8080/v1`
(and `TRUSS_EVIDENCE_OUTLINE=1`, `TRUSS_SAM_URL=http://<gpu-machine>:8090`
for outlines) and restart it. With the Meta key also set, Glimmer is the
fallback when Muse Spark fails; with the key unset, every check goes to
Glimmer and nothing leaves your home. Details:
[`dgx/RUNBOOK.md`](dgx/RUNBOOK.md) and [`dgx/sam/RUNBOOK.md`](dgx/sam/RUNBOOK.md).

### Settings you may want

All are environment variables read when the server starts; none is
needed for a first run beyond the Meta key.

| Variable | Default | What it does |
|---|---|---|
| `TRUSS_META_API_KEY` | unset | The Meta key. Turns on Muse Spark 1.3. Sent over https only. |
| `TRUSS_META_MODEL` | `muse-spark-1.3` | The Muse Spark model. The contributor tier is always refused. |
| `TRUSS_META_REASONING_EFFORT` | `low` | How hard Muse Spark thinks on an ordinary check: `minimal`, `low`, `medium`, `high`, `xhigh`, or `default` to leave it to the model. Lower is faster. |
| `TRUSS_META_SAFETY_REASONING_EFFORT` | `medium` | The same, for a question marked extra careful (the stove, the pills). |
| `TRUSS_META_TIMEOUT_S` | `120` | Longest wait for one Muse Spark call. |
| `TRUSS_HOLD_ON` | off | `1` lets Mannie say "Hold on, let me check something." within a couple of seconds when you reach for or look closely at the thing a question is about, then give the full answer. A "did I…" question already seen done is then reminded every time the thing is in shot again, not once per window. Needs an app built from the same commit. |
| `TRUSS_BASE_URL` | unset | The local fallback, Glimmer under `llama-server` (step 8). |
| `TRUSS_EVIDENCE_OUTLINE` / `TRUSS_SAM_URL` | off / `http://127.0.0.1:8090` | Outline the object in kept evidence photos with SAM 3.1. |
| `TRUSS_REFUSALS` | `1` | Mannie says when it cannot see the deciding detail ("I can't see the knobs from here"). `0` keeps every cannot-tell silent. |
| `TRUSS_BATCH_CHECKS` | `1` | All questions due on a photo in one call. `0` asks each separately. |
| `TRUSS_HOST` | found by itself | The address to listen on, when the server picks the wrong network adapter. `0.0.0.0` listens on all. |
| `TRUSS_VOICE` / `TRUSS_VOICE_RATE` | best installed / system | Mannie's voice by name (a macOS, Windows or espeak voice), and words per minute (macOS and Linux). |
| `TRUSS_SETTINGS` | `~/.truss/settings.json` | Where the Desk saves its settings. |
| `TRUSS_PLANT_MINUTES` / `TRUSS_BEDTIME_AFTER` | `15` / `20:00` | For quick live tests only; leave unset otherwise. |

The full list, and how each part works, is in
[`truss-server/README.md`](truss-server/README.md).

### If something does not work

- **The phone cannot reach the computer.** Both must be on the same Wi-Fi
  network, and the computer's firewall must allow Python on it. If the
  address the server printed is not this computer's Wi-Fi address, set
  `TRUSS_HOST`. Open
  `http://<address>:8792/` in the phone's browser: it should answer.
- **Mannie never speaks.** Run `python3 truss-server/preflight.py` with
  the server running and the app open: it checks the clock, the model
  route, that the phone polled and sent a photo, and the planted
  questions, and every FAIL line says how to fix it. The Desk's **Log**
  tab shows each check as it happens.
- **Every answer is "cannot tell" naming Muse Spark.** The key is missing
  or refused: test it on the Desk's Settings tab.
- **The glasses take no photo.** Check Developer Mode in the Meta AI app,
  then tap **Connect** again under Settings > Connections in Truss. Truss pauses while
  the glasses are off your face.

Tests are plain Python, standard library only: run any
`truss-server/test_*.py` with `python3`.

## Who it is for

An older person living alone who forgets: the gas stove, the pills, whether
the dog was fed. They can look at the stove themselves. What fails is trusting
their own memory of having looked, so they fret for hours or do the thing
again to be safe. A second dose of the night pills is where the harm is.

Truss does not sell seeing. Seeing is free. It sells remembering, at the
moment they are about to do it twice. A family member or a caretaker can
plant the questions for them.

## Two kinds of question

- **State questions** ask how the world is now: is the stove off, is the
  door locked. The current photo answers them.
- **Done questions** ask about something the wearer did: did I take my night pills.
  Here the current state can lie (a fed dog's bowl is empty an hour later
  because the dog ate), so the answer comes from witnessed history. A done
  question has a time window, such as 9 to 10 PM. The first time the glasses
  see the yes-look inside the window, Truss keeps that one photo and its
  time as the witness. If the wearer comes back to the organizer later in the
  window, Mannie says it is already done, and when ("Just so you know, you
  already took them at 9:12. I saw you."): once per window by default, or
  every time it is in shot again with `TRUSS_HOLD_ON=1`. If the window
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
  didn't take your pills": the wearer may have taken them with the glasses off,
  and that sentence is how Truss would cause the double dose it exists to
  prevent. It asks the wearer to look instead ("Could you have a quick look at
  the Tuesday PM compartment yourself before you act on this?"), and a test fails the build
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
  photo              │ Wearables toolkit│ HTTP  │ Python standard library  │ HTTP  │ Muse Glimmer 30B (GGUF)  │
             ◄────── │ capture, diff,   │ ◄──── │ question store (files)   │ ◄──── │ OpenAI-compatible API    │
  speaker            │ speak, alerts    │       │ check loop, done windows │       ├──────────────────────────┤
                     └──────────────────┘       │ alerts, evidence, log    │ ────► │ SAM 3.1 service          │
                                                │ local text to speech     │       │ (evidence outline)       │
                                                └──────────────────────────┘       └──────────────────────────┘
```

- **Glasses and phone (`app/`).** An Android app on Meta's Wearables Device
  Access Toolkit takes photos from the glasses and sends them to the laptop,
  and plays Mannie's spoken answers back through the glasses. Questions are
  planted by typing them in the app. The camera session always ends before any
  answer is spoken, because the toolkit session and the Bluetooth audio
  channel cannot be held open together. Watch hands-free either keeps the
  glasses video open and takes a photo when the view changes and holds still
  (live view), or takes a photo every 15, 30 or 60 seconds and skips one that
  is nearly identical to the last photo sent (timed photos). It pauses while
  the glasses are off your face. The app talks only to the laptop: no cloud SDKs, no analytics.
  See [`app/README.md`](app/README.md).
- **Laptop server (`truss-server/`).** Python standard library only, no
  database, no cloud account. Each question is a plain folder: its words,
  the yes and no reference photos, its watch pattern, and two files written
  at plant time (below). Answers go to a plain-text log per day, with the
  evidence photos beside it. Only questions that are due ever see a photo,
  and a question rests once it has fired. See
  [`truss-server/README.md`](truss-server/README.md).
- **Checking.** One OpenAI-compatible `chat/completions` call with image
  inputs and a strict JSON schema reply. With `TRUSS_META_API_KEY` set,
  as in the demo, it goes to Muse Spark 1.3 over the Meta Model API. If
  the API fails and `TRUSS_BASE_URL` points at Muse Glimmer 30B under
  llama.cpp's `llama-server` on an NVIDIA DGX Spark in the home, that
  answers instead, and Muse Spark rests for five minutes after an outage.
  Every answer line names the model that answered.
- **Plant-time compile.** When a question has both reference photos, one
  call to the same route writes `looks.txt` (in words, what tells this
  home's yes-look from its no-look, such as the knob positions) and
  `prompts.txt` (two or three short noun phrases for SAM: `stove`,
  `pill organizer`). Checks then read the current photo plus `looks.txt`.
- **Evidence outline.** When a question fires, SAM 3.1 on the DGX can find
  the object by those phrases and save a copy of the evidence photo with
  the object outlined. It runs in the background and can never delay or
  stop a spoken answer.
- **Voice out.** Mannie's lines are spoken by the computer's own text to
  speech (`say` on macOS, the built-in engine on Windows, espeak-ng on
  Linux) and played through the phone to the glasses. There is no
  speech to text: nothing the wearer says is recorded.

### Where your data goes

Photos go from the glasses to the phone to the laptop. From the laptop,
each photo that a due question is checked against goes to Meta's Model
API (Muse Spark), together with the question's words and, at planting,
its two reference photos. That is the one place anything leaves the home.
The API account runs on the hackathon's free credits, and Meta's billing
page says that while free credits are in use, content may be used for
product improvement. So in the demo configuration, those photos and
words can be used by Meta. Only due questions ever see a photo, and a
question rests once it has fired, so few photos go. Anyone who wants
nothing to leave the home leaves `TRUSS_META_API_KEY` unset: every call
then goes to Glimmer on the DGX, the same code.

The kept record stays on the laptop: evidence photos and the answer log
are plain files there. A photo no question fires on is deleted from the
laptop after its check. SAM outlines run on the DGX, in the home.

## Meta models used

| Model | Where it runs | What it does | Status |
|---|---|---|---|
| **Muse Spark 1.3** (Meta Model API, standard tier `muse-spark-1.3`) | Meta's API | Every check, and the plant-time compile | <!-- TODO: live result --> _Wired; first live run pending_ |
| **Muse Glimmer 30B** (open weights, `meta-models/Muse-Glimmer-30B-GGUF`, Q4_K_M with its mmproj and dflash drafter) | Locally, llama.cpp `llama-server` on an NVIDIA DGX Spark | Optional fallback when the API fails; every call when no Meta key is set | Answered real glasses photos end to end before the API key arrived |
| **SAM 3.1** (open weights, `facebook/sam3.1`; SAM 3 as fallback) | Locally, a small HTTP service on the same DGX (`dgx/sam/`) | Outlines the object in kept evidence photos | <!-- TODO: SAM acceptance result --> _Kit built; live result pending_ |

Rules Truss holds itself to:

- No non-Meta model makes any check, compile or outline, in the demo or
  the eval.
- Never the Muse Spark contributor tier, which trains on what it is sent.
  Any model name containing `contributor` is refused in code before
  anything is sent.
- The Meta key is only ever sent over https to the Meta Model API. It is
  never sent to the fallback, never logged and never in the repo; the
  local `llama-server` runs without a key.

How to bring the models up on a DGX Spark:
[`dgx/RUNBOOK.md`](dgx/RUNBOOK.md) (Glimmer) and
[`dgx/sam/RUNBOOK.md`](dgx/sam/RUNBOOK.md) (SAM).

## Eval

`eval/` is a harness of 21 staged household questions, five of them safety
questions (stove, oven, front door, tonight's pills, hair straightener)
where a false yes is a false all-clear. Frames are split into tuning and
held-out; prompts are tuned on tuning only, and numbers are reported on
held-out. The false all-clear rate on safety questions is reported as its
own headline number, never averaged into overall accuracy. One question is
a cannot-tell probe, where a confident answer is wrong, and so is any
frame staged with the deciding detail out of view.

The eval runs through the product's own checker against Muse Glimmer on
the DGX, and asks SAM whether it finds each question's objects:
`python3 eval/first_numbers.py` ([`eval/RUNBOOK.md`](eval/RUNBOOK.md)).

The headline comparison is generic questions (the question alone) against
planted questions (with the home's own reference looks).

<!-- TODO: fill from the F v0/v1 run (eval/first_numbers.py, Muse Glimmer 30B). Mock numbers in eval/SCORECARD.md only validate the harness. -->

| Held-out, _model TBD_ | Generic | Planted |
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
| `truss-server/` | Laptop server: question store, check loop, done windows, alerts, spoken answers, evidence |
| `dgx/` | Bring-up and bench scripts for Muse Glimmer 30B under `llama-server` |
| `dgx/sam/` | Local SAM 3.1 service, its setup script and acceptance run |
| `eval/` | Eval harness: question set, capture tool, runner, scorer |
| `REVIEW.md` | The review checklist every change goes through |

## Beyond the demo

- **Sensor-agnostic.** Nothing in the server knows the photos come from
  glasses: a question, its two looks, and a frame. The worn, first-person
  viewpoint is the point of Truss, because it sees what the wearer sees where
  they are, but the same store and checks would take frames from any camera.
- **The guide.** `truss-server/guide.py` is an over-the-shoulder mode: a
  job stated as steps, each step a question a glance can settle, spoken one
  at a time and checked before the next. It checks only presence, absence,
  count, position and order, never a safety verdict, keeps no photos, and
  is not in the demo.

## Built during the event

All code in this repository was written during the hackathon, which began
October 26, 2026; the first commit is from October 27. Pre-existing pieces it relies on:

- Meta's Wearables Device Access Toolkit; the app's registration and
  permission flow follows its CameraAccess sample.
- llama.cpp and the SAM 3 code from
  `facebookresearch/sam3`, installed by the scripts, not vendored.
- Hanken Grotesk, under the SIL Open Font License
  ([`third_party/fonts/`](third_party/fonts/)).
- Before Glimmer was running locally, early end-to-end checks went through
  a bridge to the author's own local assistant, a separate project from
  before the event that runs a non-Meta model (`truss-server/aman_check.py`,
  and the spike script `eval/aman_truss_spike.py`).
  It is off by default, never used in the demo or the eval, and nothing it
  answered counts as a Truss result.

## License

MIT. See [`LICENSE`](LICENSE). The Hanken Grotesk font files keep their own
SIL Open Font License.
