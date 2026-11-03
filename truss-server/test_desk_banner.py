"""The Desk's missed-window banner: the same words for every question,
and a part of the day that matches the window."""

from datetime import datetime

from alerts import desk_banner

NOW = datetime(2026, 11, 3, 22, 30)
TODAY = "2026-11-03"
YESTERDAY = "2026-11-02"

failures = 0


def check(name, ok):
    global failures
    print(("PASS " if ok else "FAIL ") + name)
    if not ok:
        failures += 1


def banner(question, until, date=TODAY, now=NOW):
    return desk_banner({"question": question, "until": until, "date": date}, now=now)


def test_no_question_has_its_own_words():
    for q in ("Did I take my night pills?", "Did I feed the dog?", "Did I lock the back door?"):
        headline, note = banner(q, "21:00")
        check(f"{q}: the note names no object", "taken them" not in note and "pills" not in note)
        check(f"{q}: the note is neutral", "It may well have been done; Truss just did not see it." in note)
    _, a = banner("Did I take my night pills?", "21:00")
    _, b = banner("Did I feed the dog?", "21:00")
    check("the note is the same for every question", a == b)


def test_the_part_of_the_day_matches_the_window():
    check("a morning window today", banner("Did I feed the dog?", "10:00")[0].startswith("One thing from this morning"))
    check("a midday window today", banner("Did I feed the dog?", "15:00")[0].startswith("One thing from this afternoon"))
    check("an evening window today", banner("Did I feed the dog?", "19:00")[0].startswith("One thing from this evening"))
    check("a late window today", banner("Did I take my night pills?", "22:00")[0].startswith("One thing from tonight"))
    check("a late window yesterday", banner("Did I take my night pills?", "22:00", YESTERDAY)[0].startswith("One thing from last night"))
    check("a midday window yesterday", banner("Did I feed the dog?", "15:00", YESTERDAY)[0].startswith("One thing from yesterday is"))
    check("no window time today", banner("Did I feed the dog?", None)[0].startswith("One thing from today"))
    check("a window that closed after midnight today", banner("Did I take my night pills?", "00:30")[0].startswith("One thing from last night"))
    check("a window that closed after midnight yesterday", banner("Did I take my night pills?", "00:30", YESTERDAY)[0].startswith("One thing from Monday"))
    check("an alert from three days ago names its day", banner("Did I take my night pills?", "22:00", "2026-10-31")[0].startswith("One thing from Saturday"))
    check("an alert older than a week", banner("Did I take my night pills?", "22:00", "2026-10-20")[0].startswith("One thing from earlier"))
    check("an unreadable date", banner("Did I take my night pills?", "22:00", "soon")[0].startswith("One thing from earlier"))
    check("an alert dated ahead of the clock", banner("Did I take my night pills?", "22:00", "2026-11-04")[0].startswith("One thing from earlier"))


def test_the_close_time_and_the_name():
    headline, note = banner("Did I feed the dog?", "13:00")
    check("the close time is said", "closed at 1:00 PM" in note)
    check("the short name", headline.endswith("still open: the dog."))
    _, note = banner("Is the stove off?", "bad")
    check("an unreadable time is left out", note.startswith("The window closed with nothing seen."))
    check("no question, no banner", desk_banner({"question": "", "until": "21:00", "date": TODAY}, now=NOW) is None)


if __name__ == "__main__":
    test_no_question_has_its_own_words()
    test_the_part_of_the_day_matches_the_window()
    test_the_close_time_and_the_name()
    print("ALL PASS" if failures == 0 else f"{failures} FAILED")
    raise SystemExit(1 if failures else 0)
