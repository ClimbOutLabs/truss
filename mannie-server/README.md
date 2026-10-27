# Mannie Bridge Server

Local server for the Mannie Bridge Android app. Handles photo uploads, speech-to-text, and text-to-speech.

## Requirements

- Python 3.6+
- macOS (uses `say` and `afconvert` commands)
- Optional: `whisper-cpp` for speech-to-text

## Installation

No dependencies needed beyond Python standard library and macOS built-in tools.

To enable speech-to-text, install whisper.cpp and download a model:

```bash
brew install whisper-cpp
mkdir -p mannie-server/models
curl -L -o mannie-server/models/ggml-small.en.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.en.bin
```

The server uses `whisper-cli` from the Homebrew whisper.cpp package.

## Running

```bash
cd mannie-server
python3 server.py
```

The server will:
- Bind to your Wi-Fi interface (en0) on port 8792
- Print the address it's listening on
- Save uploaded photos to `mannie-server/captures/`
- Log all requests to the console

## API Endpoints

- `GET /` - Health check, returns 200 OK
- `POST /capture` - Upload JPEG photo (multipart field "image"), returns `{"bytes": n}`
- `POST /stt` - Upload WAV audio (multipart field "audio"), returns `{"text": "..."}`
- `POST /tts` - Send JSON `{"text": "..."}`, returns audio/wav file

## Notes

- No cloud services are called
- All processing is local
- Photos are saved with timestamps in the captures/ directory
- If whisper-cli is not installed, STT returns an error
- The whisper model (ggml-small.en.bin) should be in mannie-server/models/
