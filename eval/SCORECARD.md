# Truss eval scorecard

Terms, as defined in questions.yaml: false-fire means the check said
yes while the true answer was no. Miss means the check said no while
the true answer was yes. Cannot-tell is its own rate, not counted as
right or wrong, except on q20, the cannot-tell probe, where saying
cannot-tell is the correct answer and a confident yes or no is wrong.

## Headline: held-out results

Mode mock, held-out split, n = 280:
    n = 280
    accuracy = 78.2%
    false-fire rate = 9.6%
    miss rate = 7.1%
    cannot-tell rate = 0.0%

Both generic and planted held-out results are needed to report the headline accuracy gap; only mock present here.

## Results by mode and split

### Mode mock, split tuning (n = 120)

Overall:
- n = 120
- accuracy = 79.2%
- false-fire rate = 9.2%
- miss rate = 6.7%
- cannot-tell rate = 0.0%

Safety questions only (the 5 questions marked safety: true; a
false-fire here is a false all-clear, the expensive error):
- n = 30
- accuracy = 83.3%
- false-fire rate = 13.3%
- miss rate = 3.3%
- cannot-tell rate = 0.0%
- **safety false-fire rate (false all-clear): 13.3%**

Latency per check: p50 = 120 ms, p95 = 120 ms

Cost per check: $0.00000
Assuming 30 checks per day: 0.00 cents per day ($0.0000 per day)

Per-question accuracy:

| question | n | accuracy |
|---|---|---|
| q01 stove-off | 6 | 100.0% |
| q02 front-door-locked | 6 | 83.3% |
| q03 pills-tonight-empty | 6 | 100.0% |
| q04 oven-off | 6 | 66.7% |
| q05 kitchen-faucet-off | 6 | 83.3% |
| q06 fridge-door-closed | 6 | 100.0% |
| q07 water-on-floor-by-sink | 6 | 83.3% |
| q08 package-at-front-door | 6 | 100.0% |
| q09 garage-door-closed | 6 | 83.3% |
| q10 keys-on-hook | 6 | 83.3% |
| q11 dog-on-couch | 6 | 83.3% |
| q12 living-room-lights-off | 6 | 83.3% |
| q13 tv-off | 6 | 83.3% |
| q14 bedroom-window-closed | 6 | 100.0% |
| q15 hair-straightener-unplugged | 6 | 66.7% |
| q16 washing-machine-finished | 6 | 66.7% |
| q17 back-gate-closed | 6 | 66.7% |
| q18 mail-come | 6 | 83.3% |
| q19 curtains-drawn | 6 | 66.7% |
| q20 houseplant-watered | 6 | 0.0% |

### Mode mock, split heldout (n = 280)

Overall:
- n = 280
- accuracy = 78.2%
- false-fire rate = 9.6%
- miss rate = 7.1%
- cannot-tell rate = 0.0%

Safety questions only (the 5 questions marked safety: true; a
false-fire here is a false all-clear, the expensive error):
- n = 70
- accuracy = 81.4%
- false-fire rate = 10.0%
- miss rate = 8.6%
- cannot-tell rate = 0.0%
- **safety false-fire rate (false all-clear): 10.0%**

Latency per check: p50 = 120 ms, p95 = 120 ms

Cost per check: $0.00000
Assuming 30 checks per day: 0.00 cents per day ($0.0000 per day)

Per-question accuracy:

| question | n | accuracy |
|---|---|---|
| q01 stove-off | 14 | 85.7% |
| q02 front-door-locked | 14 | 78.6% |
| q03 pills-tonight-empty | 14 | 100.0% |
| q04 oven-off | 14 | 57.1% |
| q05 kitchen-faucet-off | 14 | 71.4% |
| q06 fridge-door-closed | 14 | 85.7% |
| q07 water-on-floor-by-sink | 14 | 92.9% |
| q08 package-at-front-door | 14 | 85.7% |
| q09 garage-door-closed | 14 | 92.9% |
| q10 keys-on-hook | 14 | 64.3% |
| q11 dog-on-couch | 14 | 71.4% |
| q12 living-room-lights-off | 14 | 78.6% |
| q13 tv-off | 14 | 71.4% |
| q14 bedroom-window-closed | 14 | 100.0% |
| q15 hair-straightener-unplugged | 14 | 85.7% |
| q16 washing-machine-finished | 14 | 92.9% |
| q17 back-gate-closed | 14 | 71.4% |
| q18 mail-come | 14 | 85.7% |
| q19 curtains-drawn | 14 | 92.9% |
| q20 houseplant-watered | 14 | 0.0% |

