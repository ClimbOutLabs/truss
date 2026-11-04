import io
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import serverlog

FIXED = lambda: datetime(2026, 11, 3, 5, 30, 45)


class TidyTest(unittest.TestCase):
    def test_polls_are_left_out(self):
        for line in (
            '[2026-11-03 05:30:17] "GET /watch HTTP/1.1" 200 -',
            '[2026-11-03 05:30:17] "POST /capture HTTP/1.1" 200 -',
            "GET /alerts -> 0 pending",
            "GET /questions -> 20 questions",
            "GET / -> 200 OK",
        ):
            self.assertIsNone(serverlog.tidy(line), line)

    def test_polls_kept_when_asked(self):
        self.assertEqual(
            serverlog.tidy('[x] "GET /watch HTTP/1.1" 200 -', keep_polls=True),
            ("http", "GET /watch -> 200"),
        )

    def test_handler_lines_get_their_tag(self):
        self.assertEqual(
            serverlog.tidy("POST /capture -> saved capture_1.jpg (176142 bytes)"),
            ("capture", "saved capture_1.jpg (176142 bytes)"),
        )
        self.assertEqual(serverlog.tidy("GET /alerts -> 1 pending"), ("alerts", "1 pending"))
        self.assertEqual(serverlog.tidy("POST /alerts/ack -> 2026-11-03-pills"), ("alerts", "2026-11-03-pills"))
        self.assertEqual(serverlog.tidy("POST /tts -> It is past 5:40 AM."), ("said", "It is past 5:40 AM."))

    def test_failures_are_kept(self):
        self.assertEqual(
            serverlog.tidy('[x] "GET /answers HTTP/1.1" 404 -'), ("http", "GET /answers -> 404")
        )
        self.assertEqual(
            serverlog.tidy("[x] code 404, message No such day"),
            ("error", "404 No such day"),
        )

    def test_no_glasses_view_yet_is_a_poll(self):
        self.assertIsNone(serverlog.tidy('[x] "GET /latest HTTP/1.1" 404 -'))
        self.assertIsNone(serverlog.tidy("[x] code 404, message No view from the glasses in the last two minutes."))
        self.assertEqual(
            serverlog.tidy('[x] "GET /latest HTTP/1.1" 404 -', keep_polls=True),
            ("http", "GET /latest -> 404"),
        )

    def test_check_lines(self):
        self.assertEqual(
            serverlog.tidy("  check: no question is due on this frame"),
            ("check", "no question is due on this frame"),
        )


class TidyLogTest(unittest.TestCase):
    def test_writes_console_and_file_and_folds_repeats(self):
        with tempfile.TemporaryDirectory() as tmp:
            console = io.StringIO()
            path = Path(tmp) / "log" / "server.log"
            out = serverlog.TidyLog(console, path, keep_polls=False, clock=FIXED)
            out.write("POST /capture -> saved a.jpg\n")
            for _ in range(3):
                out.write("[x] code 500, message Broken\n")
            out.write('[x] "GET /watch HTTP/1.1" 200 -\n')
            out.write("POST /capture -> saved b")
            out.write(".jpg\n")
            text = path.read_text()
            self.assertEqual(text, console.getvalue())
            self.assertEqual(
                text.splitlines(),
                [
                    "2026-11-03 05:30:45 | capture  | saved a.jpg",
                    "2026-11-03 05:30:45 | error    | 500 Broken",
                    "2026-11-03 05:30:45 | error    | (the line above, 2 more times)",
                    "2026-11-03 05:30:45 | capture  | saved b.jpg",
                ],
            )


class FeedTest(unittest.TestCase):
    def test_since_and_audio(self):
        feed = serverlog.Feed(size=2, clock=FIXED)
        a = feed.add("one", b"wav1")
        b = feed.add("two")
        c = feed.add("three", b"wav3")
        self.assertEqual([l["text"] for l in feed.since(0)], ["two", "three"])
        self.assertEqual([l["id"] for l in feed.since(b)], [c])
        self.assertEqual(feed.since(0)[0]["audio"], False)
        self.assertEqual(feed.audio(c), b"wav3")
        self.assertIsNone(feed.audio(a))
        self.assertNotEqual(feed.run, serverlog.Feed().run)


class LogTabTest(unittest.TestCase):
    def test_keys_are_hidden(self):
        env = {"TRUSS_META_API_KEY": "LLM|12345|abcdefghij", "AMAN_TOKEN": "short"}
        self.assertEqual(
            serverlog.hide_keys("sent LLM|12345|abcdefghij to meta", env),
            "sent [key hidden] to meta",
        )
        self.assertEqual(serverlog.hide_keys("short stays", env), "short stays")
        self.assertEqual(
            serverlog.hide_keys("Authorization: Bearer abc123def456", {}),
            "Authorization: Bearer [key hidden]",
        )

    def test_recent_keeps_the_tidy_lines(self):
        recent = serverlog.Recent(size=3)
        log = serverlog.TidyLog(io.StringIO(), keep_polls=False, clock=FIXED, recent=recent)
        for n in range(5):
            log.write(f"POST /capture -> saved capture_{n}.jpg\n")
        lines = recent.since(0)
        self.assertEqual([l["id"] for l in lines], [3, 4, 5])
        self.assertEqual(lines[-1], {"id": 5, "at": "2026-11-03 05:30:45", "tag": "capture", "text": "saved capture_4.jpg"})
        self.assertEqual([l["id"] for l in recent.since(4)], [5])
        self.assertEqual(recent.since(5), [])


if __name__ == "__main__":
    unittest.main()
