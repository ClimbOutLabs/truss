#!/usr/bin/env python3
"""
The item E acceptance run: glasses photos through the SAM service.

    python3 dgx/sam/sam_accept.py PHOTO PHOTO PHOTO --phrases "stove,knob"
    python3 dgx/sam/sam_accept.py PHOTO... --folder truss-server/questions/<slug>

For each photo: the time per frame (the round trip as the laptop sees
it, and SAM's own share), every mask checked against its box, and the
photo saved with the union of the matches outlined. The service cuts
each mask to its box, so the check that means something is SAM's own
mask before the cut (inside_box_raw): at least 90% of it must lie
inside its box. The cut masks are also decoded here and must lie fully
inside, which checks the plumbing. Results go under dgx/sam/results/<time>/: one outlined JPEG
per photo that matched, rows.jsonl, and summary.md.

Passes when there are at least three photos, every call answered,
every mask is inside its box (raw at least 90%, cut fully), and at
least one photo has its outline.
Open the outlined photos and look: that last check is by eye.

Standard library only (it is the laptop's own client,
truss-server/sam_client.py), so it runs on the laptop or the DGX.
Reads TRUSS_SAM_URL and TRUSS_SAM_KEY like the server does.
"""

import argparse
import json
import sys
import time
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent / "truss-server"))

import sam_client  # noqa: E402

RAW_INSIDE_MIN = 0.9


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("photos", nargs="+")
    ap.add_argument("--phrases", help="comma-separated noun phrases")
    ap.add_argument("--folder", help="a planted question folder; its prompts.txt gives the phrases")
    ap.add_argument("--threshold", type=float, default=None)
    ap.add_argument("--out", default=None, help="results folder (default dgx/sam/results/<time>)")
    args = ap.parse_args()

    phrases = []
    if args.phrases:
        phrases += [" ".join(p.split()) for p in args.phrases.split(",") if p.strip()]
    if args.folder:
        found = sam_client.read_prompts(args.folder)
        if not found:
            print(f"STOP: no prompts.txt phrases in {args.folder}.")
            return 2
        phrases += [p for p in found if p not in phrases]
    if not phrases:
        print("STOP: give --phrases or --folder.")
        return 2

    cfg = sam_client.config_from_env()
    out = Path(args.out) if args.out else HERE / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    print(f"SAM at {cfg.url}; phrases: {', '.join(phrases)}; results in {out}")

    rows = []
    for photo in args.photos:
        photo = Path(photo)
        row = {"photo": photo.name}
        try:
            t = time.perf_counter()
            reply = sam_client.segment(photo, phrases, threshold=args.threshold, config=cfg)
            row["segment_ms"] = round((time.perf_counter() - t) * 1000)
            row["sam_ms"] = reply.get("timing_ms", {}).get("total")
            dets = [(p["phrase"], d) for p in reply["phrases"] for d in p["detections"]]
            row["model"] = reply.get("model")
            row["size"] = [reply.get("width"), reply.get("height")]
            row["detections"] = [
                {"phrase": ph, "score": d["score"], "box": d["box"], "area": d["area"],
                 "inside_box_raw": d["inside_box_raw"],
                 "outside_px": sam_client.rle_outside_box(d["mask"], d["box"]),
                 "mask_area_ok": sam_client.rle_area(d["mask"]) == d["area"]}
                for ph, d in dets
            ]
            row["masks_inside"] = all(
                d["outside_px"] == 0 and d["mask_area_ok"] and d["inside_box_raw"] >= RAW_INSIDE_MIN
                for d in row["detections"])
            row["matched"] = reply["union"].get("phrases_matched", [])

            dest = out / f"{photo.stem}_outline.jpg"
            t = time.perf_counter()
            o = sam_client.outline(photo, phrases, dest, threshold=args.threshold, config=cfg)
            row["outline_ms"] = round((time.perf_counter() - t) * 1000)
            row["outline"] = dest.name if o["written"] else None
        except (sam_client.SamError, OSError, KeyError, TypeError) as e:
            row["error"] = str(e) or type(e).__name__
        rows.append(row)
        print_row(row)

    with open(out / "rows.jsonl", "w") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")
    ok, summary = summarize(rows, phrases, cfg.url)
    (out / "summary.md").write_text(summary)
    print()
    print(summary)
    return 0 if ok else 1


def print_row(row):
    if "error" in row:
        print(f"  {row['photo']}: FAILED: {row['error']}")
        return
    found = ", ".join(f"{d['phrase']} {d['score']:.2f}" for d in row["detections"]) or "nothing"
    print(f"  {row['photo']}: {row['segment_ms']} ms round trip ({row['sam_ms']} ms on SAM), "
          f"found {found}; masks inside boxes: {'yes' if row['masks_inside'] else 'NO'}; "
          f"outline: {row['outline'] or 'none'}")


def summarize(rows, phrases, url):
    good = [r for r in rows if "error" not in r]
    failures = []
    if len(rows) < 3:
        failures.append(f"only {len(rows)} photos; the acceptance needs three")
    if len(good) < len(rows):
        failures.append(f"{len(rows) - len(good)} photos got no answer")
    if not all(r["masks_inside"] for r in good):
        failures.append("a mask lies outside its box (see below, and rows.jsonl)")
    if not any(r.get("outline") for r in good):
        failures.append("no photo got an outline (nothing matched)")
    warn = [(r["photo"], d) for r in good for d in r["detections"] if d["inside_box_raw"] < RAW_INSIDE_MIN]

    lines = [f"# SAM acceptance, {datetime.now().strftime('%Y-%m-%d %H:%M')}", ""]
    model = next((r["model"] for r in good), "?")
    lines += [f"Service {url}, model {model}, phrases: {', '.join(phrases)}.", ""]
    lines += ["| photo | round trip ms | SAM ms | found | masks inside boxes | outline |",
              "|---|---|---|---|---|---|"]
    for r in rows:
        if "error" in r:
            lines.append(f"| {r['photo']} | failed | | {r['error']} | | |")
            continue
        found = ", ".join(f"{d['phrase']} {d['score']:.2f}" for d in r["detections"]) or "nothing"
        lines.append(f"| {r['photo']} | {r['segment_ms']} | {r['sam_ms']} | {found} | "
                     f"{'yes' if r['masks_inside'] else 'NO'} | {r['outline'] or 'none'} |")
    times = sorted(r["segment_ms"] for r in good)
    if times:
        lines += ["", f"Per frame: median {times[len(times) // 2]} ms, worst {times[-1]} ms (round trip)."]
    if warn:
        lines += ["", f"FAIL: before the cut, {len(warn)} masks were under {RAW_INSIDE_MIN:.0%} inside their "
                  "boxes (SAM's masks and boxes disagree: a checkpoint that did not load right?):"]
        lines += [f"- {p}: {d['phrase']} {d['inside_box_raw']:.2f}" for p, d in warn]
    lines += [""]
    if failures:
        lines += ["**FAIL**: " + "; ".join(failures) + "."]
    else:
        lines += ["**PASS** on the scripted checks. Now open the outlined photos: the line must "
                  "follow the object, not a box around it."]
    return not failures, "\n".join(lines) + "\n"


if __name__ == "__main__":
    sys.exit(main())
