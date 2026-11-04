# Truss Server

Local server for the Truss Android app. Handles photo uploads and text-to-speech.

## Requirements

- Python 3.6+
- macOS (uses `say` and `afconvert` commands). Speech uses the most
  natural voice installed, a Premium or Enhanced English one when there
  is one (see `voice.py`). For the best voice, add one once in System
  Settings > Accessibility > Spoken Content > System voice > Manage
  Voices (for example English (US) > Ava (Premium)), then restart the
  server; its start-up banner names the voice. `TRUSS_VOICE` picks a
  voice by name and `TRUSS_VOICE_RATE` sets words per minute.

## Installation

No dependencies needed beyond Python standard library and macOS built-in tools.
There is no speech-to-text: questions are planted by typing them in the app.

## Running

```bash
cd truss-server
python3 server.py
```

The server will:
- Bind to your Wi-Fi interface (en0) on port 8792
- Print the address it's listening on
- Save uploaded photos to `truss-server/captures/`
- Log all requests to the console

## API Endpoints

- `GET /` - Health check, returns 200 OK
- `GET /desk` - Truss Desk, the Mac page described below
- `POST /capture` - Upload JPEG photo (multipart field "image"), returns `{"bytes": n}`
- `POST /tts` - Send JSON `{"text": "..."}`, returns audio/wav file
- `POST /plant/question` - Start planting a standing question, JSON `{"text": "..."}` with optional `"kind"`, `"pattern"`, `"safety"` (true or false), and `"look_at"`, returns the slug and the next prompt
- `POST /plant/bedtime` - Plant the bedtime round (stove, front door, tonight's pills) in walk order, JSON with optional `"after"` and `"gap_minutes"`
- `POST /guide/start` - Arm a guided job on steps the person has seen, JSON `{"title": "...", "steps": ["Is the shelf level?", ...]}`. Steps not from the last draft are judged by intent first, and an all-clear is left out. Returns the first step as a line to speak
- `POST /guide/task` - Write a guided job's steps from its name, JSON `{"task": "lock up for the night"}`; the model writes the steps (see "Guided tasks"). With `"draft": true` the steps come back for the person to check and edit, and nothing starts until `/guide/start`
- `POST /guide/skip` - Skip the guide's current step
- `POST /guide/stop` - Stop the guide at once; nothing is kept
- `GET /guide/status` - The guide's state, step, and current step
- `POST /plant/ref?slug=...&kind=yes|no` - Save a reference look (multipart field "image")
- `GET /questions` - List the standing questions and their states
- `POST /questions/retire` - Stop watching a question, JSON `{"slug": "..."}`; its folder and history stay
- `GET /answers?day=YYYY-MM-DD` - The day's answers from its `answers.txt`, newest first (today when no day is given), each naming its evidence photo
- `GET /evidence?day=YYYY-MM-DD&name=<photo>.jpg` - One kept photo from that day's log folder; only a plain photo name is served
- `GET /status` - The server's clock and zone, how checks will be made, the live-test settings in force, and when the phone app last polled and last sent a photo (read by `preflight.py`)
- `GET /alerts` - List the missed-window alerts still waiting to be spoken
- `POST /alerts/ack` - Confirm one alert was spoken, JSON `{"id": "..."}`; the only way an alert clears
- `POST /watch` - The Desk asks the phone to start or stop watching, JSON `{"on": true}`
- `POST /watch/phone` - The phone checks in every few seconds with how its watching stands and gets the Desk's latest request back
- `GET /watch` - The Desk's latest request and the phone's last check-in
- `GET /latest` - The glasses' latest view as a JPEG, held in memory only, for taking a look from the Desk; 404 when none came in the last two minutes
- `GET /settings` - What the Desk's Settings tab shows: whether the Meta key and the Aman token are set (never their values), the Aman address and check chat, and who answers the checks
- `POST /settings` - Save settings from the Desk, JSON with any of `meta_key`, `aman_base`, `aman_token`, `aman_session`, `checker` (`endpoint` or `aman`), `clear`; JSON only, and only from the Desk's own page at the Mac's address
- `POST /settings/test` - Try a connection, JSON `{"what": "meta"}` (one words-only Muse Spark call, no photo) or `{"what": "aman"}` (signs in and lists a few chats, no check)

## Truss Desk

Open `http://<the address the server prints>:8792/desk` in a browser on
the Mac. It is one static page (`desk.html`) that only calls the
endpoints listed above, so it changes nothing about how checks are made.

Four tabs, one job each:

- **Today**: the whole day in one list, by time. Each planted question
  shows when it is checked and how it stands in a word (Watching,
  Needs its photos, Not seen, Yes). Sample steps not yet planted (morning
  pills, lights off after lunch, the bedtime round, night pills) sit in
  the same list, one click from planting. A question still waiting for
  its photos (the bedtime round plants without them) takes them right
  there. A missed-window alert shows as a banner until the phone has
  spoken it; only the phone clears an alert.
- **Plant**: the question in the wearer's words, the kind, when to
  check, the yes and no looks dragged in from Photos as JPEGs, and the
  safety flag. Folded below it, **Test with a saved photo** sends a
  JPEG to `POST /capture` exactly as the phone does. It counts as a
  real sighting, so it is refused while any safety or done question is
  watched (an old photo would otherwise mark tonight's pills done or
  the stove off), and it takes a second click to send.
- **Answers**: a day's answers with their evidence photos.
- **Settings**: the Meta Model API key for Muse Spark, the connection
  to Aman (its address, sign-in token and check chat), and who answers
  the checks, each with a Test button. See Settings below.
- **Watching on the phone**: the photo loop runs in the phone app, so
  the Desk asks the phone through the server (`phone_watch.py`).
  Planting a question, or a question getting its last look, asks the
  phone to start; the button at the top starts or stops it by hand,
  and the line beside it says how the phone stands. The phone picks a
  request up within a few seconds while the Truss app is open, and
  starts once the glasses and the laptop are both connected. Each
  request is acted on once, so Stop on the phone is not overruled, and
  one older than ten minutes is ignored.

## Settings

The Desk's Settings tab saves to `~/.truss/settings.json` on the Mac
(`TRUSS_SETTINGS` names another file), never inside the repo. The
folder is created `0700` and the file `0600`. On start the server puts
each saved value into its environment under the usual name
(`TRUSS_META_API_KEY`, `AMAN_BASE`, `AMAN_TOKEN`,
`AMAN_CHECK_SESSION_ID`), and a saved value wins over one exported in
the shell. A save takes effect at once, without a restart.

Keys are never sent back to the page, printed or logged: the page only
learns whether one is set and where it came from. A token saved for
one Aman address is dropped when the address changes, unless a new
token is typed with it. Settings can only be changed by a JSON request
from the Desk itself, opened at the Mac's address, so no other web page
can change or test them. Exporting the key from the keychain, as in
the main README, still works and needs no Settings at all.

## Live test settings

Two settings make a live test session quicker. Leave both unset for
the demo and for normal use.

| Variable | Default | |
|---|---|---|
| `TRUSS_PLANT_MINUTES` | `15` | how often a question planted from the app is checked; `0` checks it on every capture until it fires |
| `TRUSS_BEDTIME_AFTER` | `20:00` | when the app's bedtime round becomes due each night, like `00:00` |

## Standing questions

Questions live as plain files under `truss-server/questions/`, one folder
per question: its words, a yes and a no reference look, and its watch
pattern (see `questions.py`).

Every captured photo is offered to the standing questions whose pattern
says a check is due (see `checkloop.py`). A question that fires keeps the
photo as evidence in `truss-server/log/<date>/`, writes a line to that
day's `answers.txt`, and its spoken answer is returned to the app in the
`/capture` response under `"speak"`. A photo no question fires on is
deleted: kept photos are only ones a question fired on. When no question
is standing, `/capture` behaves exactly as it did before the loop.

## Checking

Checks, plant-time compiles and guide checks go to an OpenAI-compatible
`chat/completions` endpoint (see `endpoint.py` and `verdict.py`).

**Muse Spark first.** With `TRUSS_META_API_KEY` set, every call goes to
Muse Spark 1.3 over the Meta Model API:

| Variable | Default | |
| --- | --- | --- |
| `TRUSS_META_API_KEY` | unset (off) | the Meta key; https only, never logged |
| `TRUSS_META_BASE_URL` | `https://api.meta.ai/v1` | must be https |
| `TRUSS_META_MODEL` | `muse-spark-1.3` | never the contributor tier |
| `TRUSS_META_TIMEOUT_S` | `120` (the whole call, retry included) | |
| `TRUSS_META_REASONING_EFFORT` | `low` (how hard Muse Spark thinks on a check: minimal, low, medium, high, xhigh; `default` leaves it to the model) | |
| `TRUSS_META_SAFETY_REASONING_EFFORT` | `medium` (the same, for a safety check such as the stove or the pills) | |

The endpoint below is then a fallback only when `TRUSS_BASE_URL` is set
explicitly, so Glimmer need not run at all, and only while it lists a
Meta model at `GET /models` (a name with "glimmer" or "muse" in it):
whatever else might answer on that port is never asked. When a Muse Spark call fails,
the same call goes to the fallback and the server log says so, naming
both models; when the failure says the API is down or refusing (no
answer, timeout, a refused key, an HTTP error), Muse Spark rests for five
minutes so each check does not wait out its timeout first. With no
fallback, a failed call is a `cannot_tell` that names Muse Spark, never a
guess. Every answer line names the model that answered. Check the
route with `python3 endpoint.py --smoke` (words only, no photo).

**The local endpoint.** Without the Meta key, every call goes here, as
before:

| Variable | Default | Meta Model API |
| --- | --- | --- |
| `TRUSS_BASE_URL` | `http://127.0.0.1:8080/v1` | `https://api.meta.ai/v1` |
| `TRUSS_MODEL` | `muse-glimmer-30b` | `muse-spark-1.3` (never the contributor tier) |
| `TRUSS_API_KEY` | none (falls back to `MODEL_API_KEY`) | the key |
| `TRUSS_TIMEOUT_S` | `120` (the whole check, retry included) | |
| `TRUSS_MAX_TOKENS` | `8192` (room to think, then answer) | |
| `TRUSS_CHECK_MODE` | `single` | |
| `TRUSS_BATCH_CHECKS` | `1` (every question due on a photo in one call; `0` = one call each) | |

Any model name containing `contributor` is refused before anything is
sent: that tier trains on what it is sent. A key is only sent over
https or to this machine; over plain http to the DGX it is left off
(llama-server needs none), so an exported `MODEL_API_KEY` never crosses
the LAN in the clear.

Run `llama-server` with `--reasoning-format deepseek` so Glimmer's
thinking lands in `reasoning_content`, which is never read. Thinking
left in the reply is stripped before it is read either way.

Set `TRUSS_BASE_URL` to the DGX's address, for example
`http://<dgx>:8080/v1`. Set `TRUSS_MODEL` to the name `llama-server`
reports, if it is not `muse-glimmer-30b` (llama-server serves one model
and does not check the name, but the answer log records it). The server
prints the model, address and mode it will use at startup.

Every check asks for this reply, as a strict JSON schema
(`response_format` json_schema, strict), with the schema also spelled
out in the prompt:

```json
{"verdict": "yes | no | cannot_tell",
 "reason": "one sentence naming what in the frame decides it",
 "missing": "what would be needed if cannot_tell, else empty"}
```

The reply is validated against the schema here as well, strictly: after
thinking is stripped and one code fence allowed, the whole reply must be
one JSON object with no duplicate keys, so prose around it, or a draft
inside the thinking, is never read as the answer. A reply cut off by the
token limit is never read at all, whatever it holds; it is retried once
with twice the room. A reply that does not validate, or a server that
refuses `response_format`, gets one retry. A second failure, an
unreachable endpoint, a refused key or a timeout is a cannot-tell that
says no check was made, never a verdict, and the result's `failure`
field names why (`truncated`, `invalid`, `timeout`, ...), so the eval
can tell a cut-off from the model honestly saying cannot-tell. A yes or
no with an empty reason, or with something named as missing, is read as
cannot-tell. Each line in the answer log, and the guide's done line,
names the model that made the call (`unknown` if a checker does not
say; Aman's answers are `aman` or `aman:<AMAN_MODEL_NAME>`).

Two modes, set by `TRUSS_CHECK_MODE`:

- `single` (default): the current frame, plus the question's
  `looks.txt` (the plant-time words for what tells this home's yes-look
  from its no-look) when the question folder has one, quoted in the
  user turn as description, never in the system prompt. A question with
  no `looks.txt` (planted before the compile, or a re-shot look whose
  recompile has not landed) is checked from the frame and question
  alone, with no reference at all; the result's `looks` field says
  `none` when that happened.
- `multi`: the yes-look, the no-look and the current frame as three
  images. Stays off until the multi-image test on the DGX passes.

One live check from the command line, for acceptance and bring-up:

```bash
export TRUSS_BASE_URL=http://<dgx>:8080/v1
python3 check_once.py photo.jpg "Is the tv on?"
```

## The evidence outline (SAM on the DGX)

When a question fires, the kept evidence photo can get a copy with the
object outlined, found by SAM on the DGX from the question's compiled
phrases (`prompts.txt`, one per line). The evidence photo is never
changed; the copy goes beside it as `<evidence>_outline.jpg` in the
day's log folder. It is off by default and runs in the background, so
SAM can never delay or stop a spoken answer. Every checked question
gets a `prompts.txt` (see the plant-time compile below); one that does
not have it yet, because its compile is still running or failed, is
outlined by the object words left in its question ("Is the TV on?"
gives `tv`), said in the server's output.

| Variable | Default | |
|---|---|---|
| `TRUSS_EVIDENCE_OUTLINE` | off | `1` turns it on |
| `TRUSS_SAM_URL` | `http://127.0.0.1:8090` | `http://<dgx>:8090` |
| `TRUSS_SAM_KEY` | none | the SAM service's own key, if it has one (plain http on the LAN: never the Meta key) |
| `TRUSS_SAM_TIMEOUT_S` | `15` | |

The DGX side, and the acceptance run, are in `dgx/sam/RUNBOOK.md`.
`gate.py` holds the gate-miss detector for the later SAM presence gate;
the gate itself is not wired until the app samples frames on its own.

## Checking with Aman for now

Before `llama-server` is up on the DGX, the server can ask your own Aman
instance to make the checks instead (see `aman_check.py`). This is a
bridge for now, not a Meta check, and it is never used in the demo: nothing about it counts as a Meta
model check, and the eval stays Meta-only. Each check is one full Aman
turn in a dedicated chat, and the verdict comes back in the same shape
the endpoint checker returns, so the check loop and the guide work
unchanged.

Setup:

1. In Aman, make a chat on a model that can read images, with vision
   enabled in that model's settings (Settings, then the model
   provider, then the model's Vision option). A model without vision
   enabled only receives text read out of the photo, and most visual
   questions honestly come back cannot-tell.
2. Get the sign-in token from the browser by clipboard, never typed
   into a file: in the browser tab where Aman is signed in, open
   DevTools, then Application, then Cookies, then the Aman address,
   and copy the value of the cookie whose name starts with
   `accessToken`. In the terminal, load it with
   `export AMAN_TOKEN="$(pbpaste)"`.
3. Find the chat's session id by running the spike probe,
   `python3 eval/aman_truss_spike.py probe`, from the repo root. The
   chat is listed with its id. A chat with no message in it does not
   appear yet, so send it any first message.
4. Set the checker and start the server:

   ```bash
   export AMAN_CHECK_SESSION_ID=<the chat's session id>
   export TRUSS_CHECKER=aman
   python3 server.py
   ```

   `AMAN_BASE` defaults to `http://127.0.0.1:8791`; set it if Aman
   answers at a different address. At startup the server prints
   "Checks will be made by Aman" when the setting is on. Any other
   `TRUSS_CHECKER` value is reported at startup and the endpoint
   checker is used instead.

Honest limits:

- The token expires. When it does, Aman refuses it and every check
  reads cannot-tell, never a wrong verdict, until `AMAN_TOKEN` is
  refreshed the same way and the server is restarted.
- Each check is a full Aman turn, about 11 seconds and a full persona
  prompt of tokens on the Aman side (about 15,000 input tokens in the
  owner's spike runs). Questions are checked on glances, so the wait
  is livable; the token cost is the price of the bridge.
- A check never answers an approval card. If an Aman turn pauses for
  one, that check reads cannot-tell.

## Plant-time compile

When a question has both reference looks, the server makes one call
to the verdict model with the question's words and the two photos
(see `plant_compile.py`), and writes two plain files into the
question's folder:

- `prompts.txt`: two or three short noun phrases, one per line, for
  SAM to find the object by: `stove`, `pill organizer`, `deadbolt`.
  One or two words, no adjectives; leftover modifiers like colors,
  materials, and states are stripped on the way in.
- `looks.txt`: what the yes-look and the no-look show in this home,
  and the one visible detail that decides it, such as the knob
  positions on the front of the stove. The single check mode reads
  it in place of the two photos, and a check that cannot see the
  deciding detail can name it as what is missing.

A question started standing on fewer than both looks (its words decide
it, like a number on a display) gets a smaller compile instead: one
call with its words, and its yes look if it has one, that writes
`prompts.txt` alone (one to three phrases), so a photo it fires on is
still outlined. It gets no `looks.txt`. If both looks arrive later,
the full compile replaces it.

A check answers what the question means, not only its most literal
reading. The deciding detail in `looks.txt` is the usual way to tell,
not the only one: any clear, direct sign of the asked state in the
photo decides it, even when that detail cannot be read. A pot at a
rolling boil or a lit flame shows the stove is on whatever its knobs
look like (the Nov 1 live run said `cannot_tell` four times with the
chicken boiling, because the knobs were unreadable). The absence of a
sign (no flame, no steam, no light) is never a sign on its own, and
every question, safety flag or not, holds an all-clear (off, closed,
locked, done) to the deciding detail or something as conclusive. There
are no per-object rules in code: every question gets the same schema
and the same prompt.

The compile runs in the background, so planting answers at once. A
re-shot look deletes both files at once, before anything else can
read them, and compiles again; a compile still running on the old
photos notices the change and writes nothing. So a check never reads
a description of photos that are no longer there. A compile that
fails is retried after 30 seconds, 2 minutes, and 10 minutes, and at
startup the server compiles any question that has both looks but no
`looks.txt`. The compile never runs on a contributor-tier model,
because it sends photos of the wearer's home. The call goes
through the checker endpoint (`endpoint.py`), so it uses the same
`TRUSS_BASE_URL` and `TRUSS_MODEL` as the checks: llama-server on the
DGX by default, or the Meta Model API with no code change. A compile
that fails writes nothing. The question stands either way, and the
server log says why.

To compile questions planted before this existed, or to retry:

```bash
python3 plant_compile.py              # every question with both looks
python3 plant_compile.py is-the-stove-off
```

Stop the server first. The hand-run compile cannot share the server's
store lock, so it could race a look being re-shot.

## Done questions

A state question asks how things are: is the stove off. A done
question watches for a thing the wearer has to do, inside a time window:
take the night pills between 9 and 10, feed the dog between 8 and
10 in the morning. It is planted like any question, with a "done"
kind and a window pattern (see `questions.py`), and it is checked
against its own reference looks like any question.

If a check sees it done, the loop remembers the time and keeps
the photo as the witness. If the wearer goes back to the same place later
that day, about to do it again, one reminder is spoken: that is
already done, and the time it was done. The reminder is spoken
once, and its photo is kept as evidence like any firing.

A thing done several times a day is planted once per window, with
the same words each time: "Did I feed the dog?" from 8 to 10, from
1 to 3, and from 7 to 9. The plantings are one routine. A second
feed after the 9 AM one, inside the morning window or after it, is
met with "Just so you know, you already fed the dog at 9:00 AM. I saw
you. The next window is at 1:00 PM." Once the 1 PM window opens, feeding is the next
meal, not a repeat, so the morning feed is no longer watched for one.

If the window closes and the loop never saw it done, that is not
left as a note in a file. The server raises a missed-window alert
(see `alerts.py`): the phone app polls for alerts about every 30
seconds while it runs, speaks the line through the glasses, and
only then tells the server it was spoken. An alert that could not
be spoken stays pending and is offered again on the next poll. The
only thing that ever clears an alert is that confirmation.

An alert never says the thing was not done, because a missed witness
is not a missed act: it may have been done with the glasses off. It
says how much of the window was watched, read from the frames that
were actually checked for that question, never from frames merely
arriving (`coverage.py`; every check of a done question that returns
a verdict leaves one line with its time in
`log/<date>/checks-<slug>.txt`, while the photo itself is still
deleted when nothing fires). A frame a guide took, a failed check, or
a frame that arrived while the question rested is not watching. A gap
of more than ten minutes between checks counts as not watching. A
done window planted with no gap is checked on every frame inside it,
since an act takes seconds. The three cases are worded as "I was with
you the whole time, from 9:00 to 10:00 PM, and I didn't see it happen",
"I was with you from 9:10 to 9:50 PM, and I didn't see it then. I wasn't
with you for the rest of the window, so I can't be sure", and "I wasn't
with you between 9:00 and 10:00 PM, so I honestly can't say either way."
Each alert opens like a companion checking in, with the question said
back to the wearer: "Just checking in: it's past 10:00 PM. You asked me
to keep an eye on this one: did you take your night pills?"

## Safety questions

A question planted with `"safety": true` (the stove, the pills) is
one where a wrong word does harm. A done question is a safety
question unless planted with `"safety": false`. "You didn't take your pills" after
a missed witness is how Truss would cause the double dose it exists
to prevent, so a safety alert says where to look and never says
to act: "Could you have a quick look at the Tuesday PM compartment
yourself before you act on this? Once I can see it, I'll tell you
what's there." The thing to look at is
the optional `"look_at"` phrase given at planting; `{weekday}` in it
is spoken as the day's name. Every sentence an alert can speak lives
in one template table, and `test_safety.py` fails if any of them
holds an action verb such as take, turn, feed, or lock, in any form
(`wording.py`). The same scan refuses a look-at phrase with one, and a
safety question's own words, which the alert speaks back, must open
as a question (is, did, has, and so on), so "Take my night pills" is
refused at planting.

After a safety miss, a state check is owed for the rest of that day.
The next frame that shows the thing is checked as it is now, against
its own reference looks, and the result is spoken once as a
description, never as a yes or no on the question ("Here's what I can
see at the Tuesday PM compartment right now: the compartment still
holds a pill."): an empty compartment is not proof the pills were
taken, so the wearer hears what is there and decides. The frame is
kept as evidence.
The Desk and the app show it with a "Looked later" chip. A cannot_tell, or a yes or
no with no description behind it, is silent, and the check waits for
the next frame, with no gap between checks. The check and its spoken
line name the look-at phrase for the missed day, so a check owed for
Tuesday and made at 3 AM Wednesday asks about Tuesday's compartment.
A miss settled after midnight is checked through the small hours
(until 4 AM), and only when its look-at phrase names the day as
`{weekday}`. A look-at phrase that names a day by itself (a weekday
name, today, tonight, tomorrow, yesterday) or holds any other `{...}`
is refused at planting, since it would be wrong on other days.
Every done question, safety or not, must open as a question. The state check never records
a witness: it says what is there, never when anything was done.

## The guide

`guide.py` is the over-the-shoulder mode: the wearer states a job as
steps, each step a question a glance can settle, and the guide speaks
one step at a time, checking each captured photo against the current
step only. While a guide is running, captures go to it instead of the
standing questions. A yes advances, a no is silent, two cannot-tells
in a row speak the step again, and the wearer can skip a step or stop
at any moment. The guide keeps no photos; a finished job leaves one
line in the day's answer log, a stopped one leaves nothing.

### Guided tasks

The Desk's Guide tab takes a job in plain words ("lock up for the
night", "assemble the bookshelf", "chicken curry") and sends it to
`POST /guide/task`. One words-only call to the model (Muse Spark first,
no photo) writes two to eight steps, each a yes/no question a glance
can settle, and the guide starts on them (`task_steps.py`).

A guide never makes a safety call by eye, judged by intent rather
than by a list of objects. The writing call is told to leave out any
step whose yes would tell the person something is now safe (off, out,
unplugged, locked, taken, cooked through). A second words-only call
then judges each written step the same way, and every all-clear is
moved out; if that call fails or skips a step, the step is left out
(fail closed). What was left out is spoken first ("Not checked in this
guide: ..."). Those belong in a standing question with its two looks.
Doneness becomes a reading off a probe thermometer's display ("Does
the thermometer read 74 or more?"), which the person judges.

Start, skip and stop calls that carry `"phone": true` (the Desk's do)
also queue their lines for the phone, which speaks them from its alert
poll (`GET /alerts`, ids starting `guide-`, cleared by `POST
/alerts/ack`), so the first step reaches the glasses within about half
a minute. A capture whose reply speaks a newer guide line drops any
older one still queued, and a stop drops them all. Like the guide,
the queue is in memory: a restart forgets both.

## What Mannie says

Every sentence the check loop speaks lives in a template table in
`checkloop.py`:

- A firing names the time it was seen and what was seen: "Is the
  stove off? Yes, that's how it looked at 9:41 PM. All four knobs point
  to off." A done question's firing says "Yes, I saw it happen at
  1:02 PM". The question is said back to the wearer ("Did I" is
  spoken as "Did you").
- The redo reminder (the stopped reach) says what was done, in the
  wearer's own words said back, when, and that it was seen: "Just so you
  know, you already took your night pills at 1:02 PM. I saw you." A
  done question that does not open "Did I ..." gets "Just so you know,
  that's already done. I saw it at 1:02 PM." So does one whose next word is not a known
  verb ("Did I already take ..."). In History the reminder shows the witness
  photo, the one of it being done.
- The refusal, below.

## The refusal

A safety question (the stove) that cannot be read from where the wearer is
says so out loud, instead of staying silent: "Is the stove off? I
can't tell from here. I can't quite see the four knobs on the front
of the stove." It is spoken when all of these hold:

- the question is a safety state question (the bedtime round plants
  the stove and the pills compartment that way), with a compiled
  `prompts.txt`;
- the model itself answered cannot_tell and named what it lacked; a
  check that failed or timed out is never spoken as a refusal;
- one more plain check, "Is any part of the stove itself in this
  photo?", says yes, so a hallway frame where the stove is not stays
  silent (after a no, that question gets no presence check for a
  minute);
- no refusal was spoken for that question in the last ten minutes.

The frame is kept as evidence and the answer log marks it "cannot
tell". The detail named is the checker's own "missing" when it is a
short plain noun phrase with no action verb in it; otherwise the line
is "Is the stove off? I can't tell from here, so I won't guess." `TRUSS_REFUSALS=0`
turns refusals off.

A safety check holds its all-clear (the stove is off, the door is
locked) to the deciding detail itself, or something just as
conclusive: Glimmer is told that no flame, no glow or no light is not
enough on its own. The first live run had answered "Is the stove off?"
yes from "no blue flame is visible". The opposite answer, that the
danger is there, needs only a clear, direct sign of it, such as a lit
flame.

## Pre-flight

Before a recording, with the server running and the app open:

```bash
python3 preflight.py                      # the whole list
python3 preflight.py --wait-capture 120   # and take one photo with the glasses
python3 preflight.py --photo doorway.jpg  # rehearse the refusal from that spot
```

It checks the clock, that the server checks on a Meta model on the
endpoint (never Aman or the contributor tier) with no live-test
settings left on, that the phone polled in the last 90 seconds, that
a photo came from the glasses, that `llama-server` and SAM on the DGX
are healthy, and that the questions each shot needs are planted, have
both looks and compiled words, and are due at the planned times
(`--witness-at 13:00 --reach-at 13:40 --bedtime-at 20:30`, and
`--refusal-at` when the refusal is not shot on the bedtime round).
Every FAIL line says how to fix it. It changes nothing.

## When the clocks change

Times in the question store are plain local clock times. Gaps between
checks are measured as real time, so on the night the clocks go back
(2 AM to 1 AM, Nov 1) a question checked at 1:55 AM is due again at
the second 1:10 AM, not an hour later. Nightly and window questions
are set by the clock on the wall, so the pills window is 9 to 10 PM
on both sides of the change. The check log that alerts read keeps
each time with its UTC offset, so the repeated hour is counted once
each pass. `test_demo.py` runs these in US Pacific time.

## The bedtime round

`bedtime.py` plants three nightly questions in the order the wearer
meets them on one walk: the stove, the front door, and tonight's pill
compartment (the stove and the pills as safety questions), with a short one-minute check gap so a question a pass
cannot settle is checked again on the next pass. Run
`python3 bedtime_demo.py` to see the round with scripted checks and no
hardware, or `python3 test_bedtime.py` for the tested choreography.

## Notes

- No cloud services are called by the server itself by default; model
  checks go to the endpoint in `TRUSS_BASE_URL` (the DGX's
  `llama-server` unless it points at the Meta Model API), or to your
  own Aman instance when `TRUSS_CHECKER=aman`
- Speech, photos, and the question store are handled on this machine;
  the model check is the only call off it
- Photos are saved with timestamps in the captures/ directory
