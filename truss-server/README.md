# Truss Server

Local server for the Truss Android app. Handles photo uploads, speech-to-text, and text-to-speech.

## Requirements

- Python 3.6+
- macOS (uses `say` and `afconvert` commands)
- Optional: `whisper-cpp` for speech-to-text

## Installation

No dependencies needed beyond Python standard library and macOS built-in tools.

To enable speech-to-text, install whisper.cpp and download a model:

```bash
brew install whisper-cpp
mkdir -p truss-server/models
curl -L -o truss-server/models/ggml-small.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
```

The server uses `whisper-cli` from the Homebrew whisper.cpp package.

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
- `POST /capture` - Upload JPEG photo (multipart field "image"), returns `{"bytes": n}`
- `POST /stt` - Upload WAV audio (multipart field "audio"), returns `{"text": "..."}`
- `POST /tts` - Send JSON `{"text": "..."}`, returns audio/wav file
- `POST /plant/question` - Start planting a standing question, JSON `{"text": "..."}`, returns the slug and the next prompt
- `POST /plant/bedtime` - Plant the bedtime round (stove, front door, tonight's pills) in walk order, JSON with optional `"after"` and `"gap_minutes"`
- `POST /guide/start` - Start a guided job, JSON `{"title": "...", "steps": ["Is the water shut off?", ...]}`, returns the first step as a line to speak
- `POST /guide/skip` - Skip the guide's current step
- `POST /guide/stop` - Stop the guide at once; nothing is kept
- `GET /guide/status` - The guide's state, step, and current step
- `POST /plant/ref?slug=...&kind=yes|no` - Save a reference look (multipart field "image")
- `GET /questions` - List the standing questions and their states

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

Checks run through the canonical checker in `eval/checker.py`, in planted
mode. Without `MODEL_API_KEY` in the environment no real check can be
made, and every check honestly reports cannot-tell.

## The guide

`guide.py` is the over-the-shoulder mode: the wearer states a job as
steps, each step a question a glance can settle, and the guide speaks
one step at a time, checking each captured photo against the current
step only. While a guide is running, captures go to it instead of the
standing questions. A yes advances, a no is silent, two cannot-tells
in a row speak the step again, and the wearer can skip a step or stop
at any moment. The guide keeps no photos; a finished job leaves one
line in the day's answer log, a stopped one leaves nothing.

## The bedtime round

`bedtime.py` plants three nightly questions in the order the wearer
meets them on one walk: the stove, the front door, and tonight's pill
compartment, with a short one-minute check gap so a question a pass
cannot settle is checked again on the next pass. Run
`python3 bedtime_demo.py` to see the round with scripted checks and no
hardware, or `python3 test_bedtime.py` for the tested choreography.

## Notes

- No cloud services are called by the server itself; model checks go to
  the Meta Model API only when a key is set
- Speech, photos, and the question store are handled on this machine;
  the model check is the only outside call, and only with a key set
- Photos are saved with timestamps in the captures/ directory
- If whisper-cli is not installed, STT returns an error
- The whisper model (ggml-small.en.bin) should be in truss-server/models/
