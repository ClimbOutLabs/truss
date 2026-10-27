#!/usr/bin/env python3
"""
Truss local server.
Handles photo uploads, speech-to-text, and text-to-speech for the Truss app.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from bedtime import plant_round
from checkloop import CheckLoop
from questions import QuestionStore

# The canonical checker lives in eval/ (see eval/checker.py). Import it
# if it can be imported; the loop falls back to cannot_tell without it.
sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))
try:
    import checker as eval_checker
except Exception:
    eval_checker = None

# Get the Wi-Fi interface address
def get_wifi_address():
    try:
        result = subprocess.run(
            ["ipconfig", "getifaddr", "en0"],
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError:
        print("Warning: Could not get en0 address, falling back to 0.0.0.0")
        return "0.0.0.0"

# Check if whisper-cli is available
def check_whisper_cli():
    try:
        # Check for whisper-cli from Homebrew
        result = subprocess.run(
            ["which", "whisper-cli"],
            capture_output=True,
            text=True,
            check=True
        )
        whisper_path = result.stdout.strip()
        # Check for the model
        model_path = Path(__file__).parent / "models" / "ggml-small.en.bin"
        if model_path.exists():
            return whisper_path, str(model_path)
        else:
            print(f"Warning: Model not found at {model_path}")
            return None, None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None, None

WHISPER_CMD, WHISPER_MODEL = check_whisper_cli()
if WHISPER_CMD and WHISPER_MODEL:
    print(f"whisper-cli found: {WHISPER_CMD}")
    print(f"Model: {WHISPER_MODEL}")
else:
    print("Warning: whisper-cli or model not found. STT will fail with an error.")

# Ensure captures directory exists
CAPTURES_DIR = Path(__file__).parent / "captures"
CAPTURES_DIR.mkdir(exist_ok=True)

# The standing-question store (see questions.py)
STORE = QuestionStore(Path(__file__).parent / "questions")


def production_check(frame_path, entry):
    """The loop's check function. With a model key in the environment,
    run the canonical checker in planted mode with the question's own
    reference looks. Without a key, no real check can be made, so the
    honest answer is cannot_tell: silent, and the frame is not kept."""
    if os.environ.get("MODEL_API_KEY") and eval_checker is not None:
        return eval_checker.check(
            str(frame_path),
            entry["question"],
            "planted",
            ref_yes=entry["ref_yes_path"],
            ref_no=entry["ref_no_path"],
        )
    return {
        "verdict": "cannot_tell",
        "reason": "No model key is set, so no real check was made.",
        "latency_ms": 0,
        "usage": {"spark_input_tokens": 0, "spark_output_tokens": 0, "sam_images": 0},
    }


# The check loop (see checkloop.py). Spoken answers are returned to the
# app in the /capture response; speaking them is the app's side.
LOOP = CheckLoop(STORE, production_check, Path(__file__).parent / "log")

class TrussHandler(BaseHTTPRequestHandler):
    
    def log_message(self, format, *args):
        """Override to customize logging format."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{timestamp}] {format % args}")
    
    def send_json(self, obj, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        """Handle GET requests."""
        path = urlparse(self.path).path
        if path == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            message = "Truss server is running.\n"
            self.wfile.write(message.encode())
            print(f"GET / -> 200 OK")
        elif path == "/questions":
            self.handle_questions()
        else:
            self.send_error(404, "Not Found")

    def do_POST(self):
        """Handle POST requests."""
        path = urlparse(self.path).path
        if path == "/capture":
            self.handle_capture()
        elif path == "/stt":
            self.handle_stt()
        elif path == "/tts":
            self.handle_tts()
        elif path == "/plant/question":
            self.handle_plant_question()
        elif path == "/plant/bedtime":
            self.handle_plant_bedtime()
        elif path == "/plant/ref":
            self.handle_plant_ref()
        else:
            self.send_error(404, "Not Found")

    def handle_questions(self):
        """List the standing questions and their states."""
        entries = []
        for entry in STORE.list():
            entries.append({
                "slug": entry["slug"],
                "question": entry["question"],
                "state": entry["state"],
                "pattern": entry["pattern"],
                "has_ref_yes": entry["has_ref_yes"],
                "has_ref_no": entry["has_ref_no"],
            })
        self.send_json({"questions": entries})
        print(f"GET /questions -> {len(entries)} questions")

    def handle_plant_question(self):
        """Start planting a question: POST JSON {"text": ..., "pattern": ...}.

        Voice planting uses the same endpoint: the app sends the recording
        to /stt first, then posts the transcript here as text.
        """
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())
            text = data.get("text", "").strip()
            if not text:
                self.send_error(400, "No question text provided")
                return
            slug = STORE.create(text, data.get("pattern"))
            self.send_json({
                "slug": slug,
                "state": "planting",
                "next": "Show me what yes looks like.",
            })
            print(f"POST /plant/question -> {slug}: {text[:50]}")
        except ValueError as e:
            self.send_error(400, str(e))
        except Exception as e:
            print(f"POST /plant/question -> ERROR: {e}")
            self.send_error(
                500,
                "Something went wrong planting that question. "
                "The error is in the server log.",
            )

    def handle_plant_bedtime(self):
        """Plant the bedtime round: POST JSON, optional "after" (HH:MM)
        and "gap_minutes". The three questions are planted in walk
        order; their reference looks are then shown one question at a
        time through /plant/ref, as for any planting."""
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        opts = {}
        if length:
            try:
                opts = json.loads(self.rfile.read(length) or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                self.send_error(400, "Body must be JSON")
                return
            if not isinstance(opts, dict):
                self.send_error(400, "Body must be a JSON object")
                return
        try:
            planted = plant_round(
                STORE,
                after=opts.get("after", "20:00"),
                gap_minutes=opts.get("gap_minutes", 1),
            )
        except ValueError as e:
            self.send_error(400, str(e))
            return
        except Exception as e:
            print(f"POST /plant/bedtime -> ERROR: {e}")
            self.send_error(
                500,
                "Something went wrong planting the round. "
                "The error is in the server log.",
            )
            return
        slugs = planted["slugs"]
        questions = [STORE.get(slug)["question"] for slug in slugs]
        self.send_json(
            {"slugs": slugs, "questions": questions, "notes": planted["notes"]}
        )
        print(f"POST /plant/bedtime -> {slugs}")

    def handle_plant_ref(self):
        """Save a reference look: POST multipart image, ?slug=...&kind=yes|no."""
        try:
            query = parse_qs(urlparse(self.path).query)
            slug = query.get("slug", [""])[0]
            kind = query.get("kind", [""])[0]
            if not slug or kind not in ("yes", "no"):
                self.send_error(400, "Need slug and kind=yes or kind=no")
                return
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                self.send_error(400, "Expected multipart/form-data")
                return
            boundary = content_type.split("boundary=")[-1].encode()
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            image_data = self.extract_multipart_field(body, boundary, b"image")
            if not image_data:
                self.send_error(400, "No image field found")
                return
            state = STORE.save_ref(slug, kind, image_data)
            entry = STORE.get(slug)
            if state == "standing":
                next_step = f"Standing question planted: {entry['question']}"
            elif kind == "yes":
                next_step = "Now show me what no looks like."
            else:
                next_step = "Now show me what yes looks like."
            self.send_json({"slug": slug, "state": state, "next": next_step})
            print(f"POST /plant/ref -> {slug} {kind} ({len(image_data)} bytes), state {state}")
        except KeyError as e:
            self.send_error(404, str(e))
        except Exception as e:
            print(f"POST /plant/ref -> ERROR: {e}")
            self.send_error(500, str(e))
    
    def handle_capture(self):
        """Handle photo upload."""
        try:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                self.send_error(400, "Expected multipart/form-data")
                return
            
            # Parse boundary
            boundary = content_type.split("boundary=")[-1].encode()
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            
            # Extract image data
            image_data = self.extract_multipart_field(body, boundary, b"image")
            if not image_data:
                self.send_error(400, "No image field found")
                return
            
            # Save with timestamp
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
            filename = f"capture_{timestamp}.jpg"
            filepath = CAPTURES_DIR / filename
            
            with open(filepath, "wb") as f:
                f.write(image_data)
            
            file_size = len(image_data)

            # Offer the frame to the check loop. When questions are
            # standing, the loop may move the frame into the answer log
            # as evidence or delete it; when none are standing, the
            # frame stays exactly as day 1 left it. A loop failure must
            # never break the capture itself.
            try:
                loop_result = LOOP.process_frame(filepath)
            except Exception as e:
                print(f"POST /capture -> loop failed, frame kept: {e}")
                loop_result = {"managed": False, "fired": [], "speak": []}
            response = {"bytes": file_size}
            if loop_result["managed"]:
                response["fired"] = loop_result["fired"]
                response["speak"] = loop_result["speak"]

            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())

            print(f"POST /capture -> saved {filename} ({file_size} bytes)")
            if loop_result["managed"]:
                print(
                    f"POST /capture -> loop: fired={loop_result['fired']} "
                    f"errors={loop_result['errors']}"
                )
        
        except Exception as e:
            print(f"POST /capture -> ERROR: {e}")
            self.send_error(500, str(e))
    
    def handle_stt(self):
        """Handle speech-to-text."""
        try:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("multipart/form-data"):
                self.send_error(400, "Expected multipart/form-data")
                return
            
            # Parse boundary
            boundary = content_type.split("boundary=")[-1].encode()
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            
            # Extract audio data
            audio_data = self.extract_multipart_field(body, boundary, b"audio")
            if not audio_data:
                self.send_error(400, "No audio field found")
                return
            
            # Transcribe with whisper-cli
            if not WHISPER_CMD or not WHISPER_MODEL:
                self.send_error(500, "whisper-cli or model not found. Install with: brew install whisper-cpp")
                return
            
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
                tmp.write(audio_data)
                tmp_path = tmp.name
            
            try:
                # whisper-cli with no timestamps (-nt flag)
                result = subprocess.run(
                    [WHISPER_CMD, "-m", WHISPER_MODEL, "-f", tmp_path, "-l", "en", "-nt"],
                    capture_output=True,
                    text=True,
                    timeout=30
                )
                
                if result.returncode != 0:
                    raise RuntimeError(f"whisper-cli failed: {result.stderr}")
                
                # Parse the output - whisper-cli with -nt outputs just the text
                lines = result.stdout.strip().split('\n')
                # Filter out empty lines and any remaining metadata
                text_lines = [line.strip() for line in lines 
                             if line.strip() and not line.strip().startswith('[')]
                text = ' '.join(text_lines) if text_lines else ""
                
                if not text:
                    text = "(no speech detected)"
            finally:
                os.unlink(tmp_path)
            
            response = {"text": text}
            
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(response).encode())
            
            print(f"POST /stt -> transcribed: {text[:50]}...")
        
        except Exception as e:
            print(f"POST /stt -> ERROR: {e}")
            self.send_error(500, str(e))
    
    def handle_tts(self):
        """Handle text-to-speech."""
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(content_length)
            data = json.loads(body.decode())
            
            text = data.get("text", "")
            if not text:
                self.send_error(400, "No text provided")
                return
            
            # Generate speech with macOS 'say' command
            with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp_aiff:
                aiff_path = tmp_aiff.name
            
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
                wav_path = tmp_wav.name
            
            try:
                # Generate AIFF with 'say'
                subprocess.run(
                    ["say", "-o", aiff_path, text],
                    check=True,
                    timeout=10
                )
                
                # Convert to 16-bit WAV with afconvert
                subprocess.run(
                    ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", aiff_path, wav_path],
                    check=True,
                    timeout=10
                )
                
                # Read WAV file
                with open(wav_path, "rb") as f:
                    wav_data = f.read()
                
                self.send_response(200)
                self.send_header("Content-Type", "audio/wav")
                self.send_header("Content-Length", str(len(wav_data)))
                self.end_headers()
                self.wfile.write(wav_data)
                
                print(f"POST /tts -> generated {len(wav_data)} bytes for: {text[:50]}...")
            
            finally:
                if os.path.exists(aiff_path):
                    os.unlink(aiff_path)
                if os.path.exists(wav_path):
                    os.unlink(wav_path)
        
        except Exception as e:
            print(f"POST /tts -> ERROR: {e}")
            self.send_error(500, str(e))
    
    def extract_multipart_field(self, body, boundary, field_name):
        """Extract a field from multipart/form-data."""
        parts = body.split(b"--" + boundary)
        for part in parts:
            if field_name in part:
                # Find the data after the headers
                header_end = part.find(b"\r\n\r\n")
                if header_end != -1:
                    data = part[header_end + 4:]
                    # Remove trailing \r\n if present
                    if data.endswith(b"\r\n"):
                        data = data[:-2]
                    return data
        return None

def main():
    host = get_wifi_address()
    port = 8792
    
    server = HTTPServer((host, port), TrussHandler)
    print(f"\nTruss server starting on {host}:{port}")
    print(f"Captures will be saved to: {CAPTURES_DIR.absolute()}")
    print(f"Press Ctrl+C to stop\n")
    
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down server...")
        server.shutdown()

if __name__ == "__main__":
    main()
