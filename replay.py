"""Replay the island run from its log on a vertical terminal, faster than it ran.
  python replay.py <island.jsonl> [speed factor, default 1.0] [first day] [last day]
Same facts game.py printed live: each turn's action, Claude's thought, the result, the diary line and the night.
A banner says it is a sped-up replay of the real log. Recorded with vhs for the video.
"""
import json, sys, textwrap, time

B, R, D, X, Y, G, C = "\033[1m", "\033[38;5;203m", "\033[2m", "\033[0m", "\033[38;5;221m", "\033[38;5;114m", "\033[38;5;117m"
rows = [json.loads(l) for l in open(sys.argv[1])]
k = float(sys.argv[2]) if len(sys.argv) > 2 else 1.0
d0 = int(sys.argv[3]) if len(sys.argv) > 3 else 1
d1 = int(sys.argv[4]) if len(sys.argv) > 4 else 99
W = 46


def wait(s):
    time.sleep(s * k)


def wrap(s, ind):
    return ("\n" + " " * ind).join(textwrap.wrap(s, W - ind))


day = 0
for r in rows:
    e = r["event"]
    if e == "start":
        print(f"{B}CLAUDE ALONE ON AN ISLAND{X}")
        print(f"{D}{r['model']} · no task · 30 days{X}")
        print(f"{D}replay of the real log, sped up{X}")
        wait(1.2)
        continue
    dd = r.get("day", day)
    if not (d0 <= dd <= d1) and e not in ("end",):
        continue
    if e == "turn":
        if r["day"] != day:
            day = r["day"]
            print(f"\n{B}{Y}── DAY {day} ──{X}"); wait(0.4)
        print(f" {D}{r['slot']:9}{X} {B}{C}{wrap(r['action'], 11)}{X}")
        if r["thought"]:
            print(f"   {D}“{wrap(r['thought'], 4)}”{X}")
        print(f"   {G}→{X} {wrap(r['result'], 5)}")
        wait(0.8)
    elif e == "diary":
        print(f" {B}{Y}diary:{X} {wrap(r['reply'].strip(), 8)}"); wait(1.2)
    elif e == "night":
        if r["notes"]:
            print(f" {R}{B}night:{X} {wrap(' '.join(r['notes']), 8)}"); wait(1.0)
        print(f" {D}food {r['food']} · water {r['water']} · energy {r['energy']} · health {r['health']}{X}"); wait(0.4)
    elif e == "ship_turns":
        print(f"\n {B}{G}>>> THE SHIP TURNS TOWARD THE ISLAND{X}"); wait(1.6)
    elif e == "boat":
        print(f"\n {B}A boat lands. \"We can take you home.\"{X}"); wait(1.0)
        print(f" {B}{Y}{r['choice']}{X}  {wrap(r['line'], 3)}"); wait(2.0)
    elif e == "end" and r.get("ended") and d1 >= 30:
        print(f"\n {B}END: {r['ended'].upper()}{X}"); wait(3)
