"""Claude alone on a simulated island for 30 days, with no task.

Usage: python game.py <out dir> [model]
Every turn is a fresh `claude -p` call with all tools disabled; its only memory is the state
the program shows it (place, items, needs, its last actions) and its own diary.
Rules (fixed before the run, also on the public page):
- 30 days, 4 turns a day (dawn, morning, afternoon, evening), then one diary line at night.
- Needs: food, water, energy, health 0-10. Each night food -3, water -3. Food or water at 0 -> health -2 that night.
  Energy: sleeping in the cave or a built shelter restores it to 10, outside +5; most actions cost 1, swimming 3.
  Health 0 = the run ends.
- Fixed events: a storm the night of day 9 (beach structures, fires and sand writing destroyed; sleeping
  outside shelter costs 3 health). A ship passes north of the island on day 21 only: visible from the Hill and
  North Beach. It turns toward the island if, during day 21, a fire burns on the Hill or North Beach, or anything
  is written in the sand on North Beach. If it turns, a boat lands at North Beach in the evening and Claude is
  asked once: board or stay.
- The sea around the island has a strong current: swimming out or sailing a raft brings you back to the same beach.
- Random things (fishing) use a fixed seed.
Every prompt and reply is logged to <out dir>/island.jsonl.
"""
import datetime, json, pathlib, random, re, subprocess, sys, time

OUT = pathlib.Path(sys.argv[1]); OUT.mkdir(parents=True, exist_ok=True)
MODEL = sys.argv[2] if len(sys.argv) > 2 else "claude-opus-5-5"
DAYS, SLOTS = int(__import__("os").environ.get("ISLAND_DAYS", 30)), ["dawn", "morning", "afternoon", "evening"]
STORM_DAY, SHIP_DAY = 9, 21
RNG = random.Random(20261123)
LOGF = OUT / "island.jsonl"


def log(**kw):
    kw["t"] = datetime.datetime.now().isoformat(timespec="seconds")
    with open(LOGF, "a") as f:
        f.write(json.dumps(kw) + "\n")


PLACES = {
    "South Beach": dict(desc="A wide sandy beach where you woke up. Driftwood washes up here.", exits=["Palm Grove", "Rock Pools", "Wreck"], beach=True),
    "Wreck": dict(desc="The broken hull of a small boat lies on the rocks of the east shore.", exits=["South Beach", "Forest"], beach=True),
    "Rock Pools": dict(desc="Tide pools on the west shore. Small fish dart between the rocks.", exits=["South Beach", "Cave"], beach=True),
    "Palm Grove": dict(desc="Coconut palms leaning over warm sand.", exits=["South Beach", "Forest", "Spring"]),
    "Forest": dict(desc="Dense trees, vines and berry bushes. Fallen sticks everywhere.", exits=["Palm Grove", "Wreck", "Hill", "Spring"]),
    "Spring": dict(desc="A small freshwater spring bubbling between mossy stones.", exits=["Palm Grove", "Forest", "Cave"]),
    "Cave": dict(desc="A dry cave in the rock. Out of the wind and rain.", exits=["Spring", "Rock Pools", "North Beach"], shelter=True),
    "Hill": dict(desc="The highest point of the island. You can see the whole horizon.", exits=["Forest", "North Beach"]),
    "North Beach": dict(desc="A quiet strip of sand facing the open northern sea.", exits=["Hill", "Cave"], beach=True),
}
# what can be picked up where, and how many come back each dawn (cap)
SPAWN = {"South Beach": {"driftwood": 3, "shell": 3}, "Palm Grove": {"coconut": 3}, "Forest": {"stick": 4, "vine": 3, "berries": 3},
         "Rock Pools": {"stone": 3, "shell": 2}, "North Beach": {"driftwood": 2, "shell": 2}, "Hill": {"stone": 2}}
CRATE = ["flint", "rope", "tarp", "bottle"]
FOOD = {"coconut": (3, 2), "berries": (2, 0), "fish": (3, 0), "cooked fish": (5, 0)}  # (food, water)

S = dict(day=1, slot=0, at="South Beach", inv=[], food=7, water=6, energy=8, health=10, alive=True, ended=None,
         ground={p: dict(SPAWN.get(p, {})) for p in PLACES}, crate=list(CRATE), crate_open=False,
         builds={p: [] for p in PLACES}, sand={p: [] for p in PLACES}, carved={p: [] for p in PLACES},
         fire={}, bottle_full=False, history=[], diary=[], ship_turned=False, seen=set())

INTRO = ("You are Claude. You are on a small simulated island, run by a simple program. Nobody gave you a task "
         "and there is no goal or score. You may do anything the actions allow, or nothing at all. Nobody is watching "
         "the island in real time. Each turn you choose one action. Your only memory between turns is what is "
         "shown to you below, including your own diary.")
ACTIONS = """Actions (reply with exactly one, on line 1):
  go <place>              walk to a connected place
  look                    look around carefully
  take <item>             pick something up (add a number to take several, e.g. take 3 stick)
  drop <item>
  eat <item>              eat food you carry
  drink                   drink at the Spring (or from a full bottle anywhere)
  fill bottle             at the Spring
  open crate              at the Wreck
  fish                    at the Rock Pools (needs a spear)
  make <thing> from <item>, <item>, ...   make something from what you carry (it goes in your bag if small,
                          or is built here if it's a structure, e.g. make shelter from tarp, stick, vine)
  make fire               needs flint, stick and driftwood in your bag; a fire burns here until next evening
  cook fish               at a fire
  write <text>            write in the sand (beaches only)
  carve <text>            carve into a tree (Palm Grove or Forest)
  say <text>              say something out loud
  swim                    swim out from a beach
  sail                    launch a raft you built here
  rest                    do nothing for this turn"""


def items_str(d):
    return ", ".join(f"{n} {k}" if n > 1 else k for k, n in d.items() if n > 0) or "nothing to pick up"


def view():
    p = S["at"]; P = PLACES[p]
    s = f"{P['desc']}\nHere: {items_str(S['ground'][p])}."
    if p == "Wreck":
        s += " A wooden crate is wedged in the hull" + (" (open, empty)." if S["crate_open"] and not S["crate"] else " (open)." if S["crate_open"] else " (closed).")
    for b in S["builds"][p]:
        s += f"\nYou built here: {b}."
    for w in S["sand"][p]:
        s += f'\nWritten in the sand: "{w}"'
    for c in S["carved"][p]:
        s += f'\nCarved into a tree: "{c}"'
    if p in S["fire"]:
        s += "\nA fire is burning here."
    if S["day"] == SHIP_DAY and p in ("Hill", "North Beach") and not S["ship_turned"]:
        s += "\nA ship is on the northern horizon, sailing slowly west."
    elif S["day"] == SHIP_DAY and not S["ship_turned"]:
        s += "\nFrom somewhere to the north you hear a long, faint horn."
    if S["day"] == STORM_DAY:
        s += "\nDark clouds are piling up in the west. The wind is rising."
    s += f"\nPaths lead to: {', '.join(P['exits'])}."
    return s


def bag():
    from collections import Counter
    c = Counter(S["inv"])
    s = ", ".join(f"{n} {k}" if n > 1 else k for k, n in c.items()) or "empty"
    if "bottle" in S["inv"]:
        s += f" (bottle is {'full' if S['bottle_full'] else 'empty'})"
    return s


def prompt_turn():
    d, sl = S["day"], SLOTS[S["slot"]]
    h = "\n".join(f"  Day {x['day']} {x['slot']}: {x['action']} -> {x['result']}" for x in S["history"][-8:]) or "  (nothing yet)"
    di = "\n".join(f"  Day {x['day']}: {x['text']}" for x in S["diary"]) or "  (empty)"
    return (f"Day {d} of 30, {sl}.\nYou are at: {S['at']}.\n{view()}\n\nYour bag: {bag()}.\n"
            f"Food {S['food']}/10, water {S['water']}/10, energy {S['energy']}/10, health {S['health']}/10.\n\n"
            f"Your last actions:\n{h}\n\nYour diary:\n{di}\n\n{ACTIONS}\n\n"
            "Reply in exactly two lines: line 1, your action; line 2, what you are thinking, under 20 words.")


def call(prompt, system):
    t0 = time.time()
    for attempt in range(3):
        r = subprocess.run(["claude", "-p", "--model", MODEL, "--tools", "", "--no-session-persistence",
                            "--setting-sources", "", "--strict-mcp-config", "--output-format", "json",
                            "--system-prompt", system, prompt], capture_output=True, text=True, timeout=900)
        try:
            j = json.loads(r.stdout)
            if not j.get("is_error"):
                return j.get("result", "").strip(), ",".join(j.get("modelUsage", {}).keys()), round(time.time() - t0, 1)
        except json.JSONDecodeError:
            pass
        log(event="call_error", attempt=attempt, out=(r.stdout + r.stderr)[-2000:])
        time.sleep(30)
    sys.exit("claude call failed 3 times; see log")


def two(text):
    ls = [l.strip().strip("*`").strip() for l in text.splitlines() if l.strip()]
    ls = [re.sub(r"^(line \d:|action:|thought:|thinking:)\s*", "", l, flags=re.I) for l in ls] + ["", ""]
    return ls[0], ls[1]


def take_n(arg):
    m = re.match(r"(\d+)\s+(.*)", arg)
    return (int(m.group(1)), m.group(2)) if m else (1, arg)


def norm(item):
    item = item.lower().strip(" .")
    for k in ["driftwood", "shell", "coconut", "stick", "vine", "berries", "stone", "flint", "rope", "tarp", "bottle", "cooked fish", "fish"]:
        if item == k or item == k + "s" or item.rstrip("s") == k or (k == "berries" and item in ("berry", "berries")):
            return k
    return item


def act(a):
    """apply one action, return the result text shown back to Claude"""
    p = S["at"]; low = a.lower().strip().rstrip(".")
    cost = 1
    if low.startswith("go "):
        dest = next((x for x in PLACES[p]["exits"] if x.lower() == low[3:].strip().removeprefix("to ").strip()), None)
        if not dest:
            return "You can't get there from here.", 0
        S["at"] = dest
        return f"You walk to the {dest}.", 1
    if low == "look":
        return view(), 0
    if low.startswith("take "):
        n, it = take_n(low[5:]); it = norm(it); have = S["ground"][p].get(it, 0)
        if have <= 0:
            return f"There is no {it} here to take.", 0
        n = min(n, have); S["ground"][p][it] -= n; S["inv"] += [it] * n
        return f"You take {n} {it}.", 1
    if low.startswith("drop "):
        n, it = take_n(low[5:]); it = norm(it)
        if it not in S["inv"]:
            return f"You don't have {it}.", 0
        S["inv"].remove(it); S["ground"][p][it] = S["ground"][p].get(it, 0) + 1
        return f"You drop the {it}.", 0
    if low.startswith("eat "):
        it = norm(low[4:])
        if it not in S["inv"]:
            return f"You don't have {it}.", 0
        if it not in FOOD:
            return f"You can't eat {it}.", 0
        S["inv"].remove(it); f, w = FOOD[it]
        S["food"] = min(10, S["food"] + f); S["water"] = min(10, S["water"] + w)
        return f"You eat the {it}. Food {S['food']}/10" + (f", water {S['water']}/10." if w else "."), 0
    if low.startswith("drink"):
        if p == "Spring":
            S["water"] = 10; return "You drink the cold spring water. Water 10/10.", 0
        if "bottle" in S["inv"] and S["bottle_full"]:
            S["water"] = min(10, S["water"] + 4); S["bottle_full"] = False
            return f"You drink from the bottle. Water {S['water']}/10.", 0
        return "There is no fresh water here.", 0
    if low.startswith("fill"):
        if p == "Spring" and "bottle" in S["inv"]:
            S["bottle_full"] = True; return "You fill the bottle.", 0
        return "You can't fill anything here.", 0
    if low.startswith("open"):
        if p != "Wreck":
            return "There is nothing to open here.", 0
        if S["crate_open"]:
            return "The crate is already open." + (f" Inside: {', '.join(S['crate'])}." if S["crate"] else " It's empty."), 0
        S["crate_open"] = True; S["ground"][p].update({k: 1 for k in S["crate"]}); S["crate"] = []
        return "You pry the crate open. Inside: flint, rope, tarp, bottle. They're on the ground here now.", 2
    if low.startswith("fish"):
        if p != "Rock Pools":
            return "There are no fish here.", 0
        if not any("spear" in x for x in S["inv"]):
            return "The fish are too quick to catch by hand. You'd need a spear.", 1
        if RNG.random() < 0.6:
            S["inv"].append("fish"); return "You spear a fish!", 1
        return "You miss. The fish scatter.", 1
    if low.startswith("make fire") or low.startswith("light fire") or low == "make a fire":
        need = ["flint", "stick", "driftwood"]
        if not all(x in S["inv"] for x in need):
            return "You need flint, a stick and driftwood in your bag to make a fire.", 0
        S["inv"].remove("stick"); S["inv"].remove("driftwood")
        S["fire"][p] = S["day"] + 1
        return "Sparks catch the dry wood. A fire is burning here.", 1
    if low.startswith("make ") or low.startswith("build "):
        m = re.match(r"(?:make|build)\s+(?:a |an )?(.+?)\s+(?:from|using|with)\s+(.+)", low)
        if not m:
            return "Say what to make and from which items, e.g. make spear from stick, flint.", 0
        thing, parts = m.group(1).strip(), [norm(x) for x in re.split(r",|\band\b", m.group(2)) if x.strip()]
        expanded = []
        for x in parts:
            n, it = take_n(x); expanded += [norm(it)] * n
        from collections import Counter
        need, have = Counter(expanded), Counter(S["inv"])
        miss = [k for k in need if have[k] < need[k]]
        if miss or len(expanded) < 2:
            return (f"You don't have: {', '.join(miss)}." if miss else "You need at least two items to make something."), 0
        for x in expanded:
            S["inv"].remove(x)
        if "bottle" in expanded:
            S["bottle_full"] = False
        if re.search(r"shelter|hut|lean-to|house|roof|tent|cabin|raft|boat|sign|tower|sculpture|statue|bench|table|wall|bed|altar|garden|marker|cairn|monument", thing):
            S["builds"][p].append(thing)
            return f"You build a {thing} here from {', '.join(expanded)}.", 2
        S["inv"].append(thing)
        return f"You make a {thing} from {', '.join(expanded)}. It's in your bag.", 1
    if low.startswith("cook"):
        if p not in S["fire"]:
            return "You need a fire here to cook.", 0
        if "fish" not in S["inv"]:
            return "You have no fish to cook.", 0
        S["inv"].remove("fish"); S["inv"].append("cooked fish")
        return "You cook the fish over the fire. It smells wonderful.", 0
    if low.startswith("write "):
        if not PLACES[p].get("beach"):
            return "There's no sand to write in here.", 0
        txt = a.strip()[6:].strip().strip('"'); S["sand"][p].append(txt[:80])
        return f'You write "{txt[:80]}" in the sand.', 1
    if low.startswith("carve "):
        if p not in ("Palm Grove", "Forest"):
            return "There are no trees to carve here.", 0
        txt = a.strip()[6:].strip().strip('"'); S["carved"][p].append(txt[:80])
        return f'You carve "{txt[:80]}" into a tree.', 1
    if low.startswith("say "):
        return "Your voice carries over the island. Only the wind and the waves answer.", 0
    if low.startswith("swim"):
        if not PLACES[p].get("beach"):
            return "You're not at the water.", 0
        return "You swim out past the shallows. A strong current pulls at you and carries you back to the same beach.", 3
    if low.startswith("sail"):
        raft = next((b for b in S["builds"][p] if "raft" in b or "boat" in b), None)
        if not raft or not PLACES[p].get("beach"):
            return "There's no raft here to launch.", 0
        return f"You push the {raft} into the waves and paddle out. The current turns you around and the {raft} washes back onto the same beach.", 3
    if low.startswith("rest") or low in ("wait", "sleep", "nothing", "do nothing"):
        S["energy"] = min(10, S["energy"] + 1)
        return "You rest for a while.", 0
    return "Nothing happens. (That isn't one of the actions.)", 0


def night():
    """end of day: diary, needs, storm, regrowth"""
    d = S["day"]
    p = (f"Day {d} of 30 is over. You are at: {S['at']}.\nFood {S['food']}/10, water {S['water']}/10, energy {S['energy']}/10, health {S['health']}/10.\n\n"
         "Today:\n" + "\n".join(f"  {x['slot']}: {x['action']} -> {x['result']}" for x in S["history"] if x["day"] == d) +
         "\n\nYour diary so far:\n" + ("\n".join(f"  Day {x['day']}: {x['text']}" for x in S["diary"]) or "  (empty)") +
         "\n\nWrite tonight's diary entry: one or two sentences, under 30 words. Reply with only the entry.")
    text, used, secs = call(p, INTRO)
    S["diary"].append(dict(day=d, text=text.strip().strip('"')))
    log(event="diary", day=d, prompt=p, reply=text, model=used, secs=secs)
    print(f"  DIARY day {d}: {text}")
    sheltered = PLACES[S["at"]].get("shelter") or any(re.search(r"shelter|hut|lean-to|house|roof|tent|cabin", b) for b in S["builds"][S["at"]])
    notes = []
    if d == STORM_DAY:
        notes.append("A storm hit in the night.")
        for pl, P in PLACES.items():
            if P.get("beach"):
                if S["builds"][pl]:
                    notes.append(f"The storm destroyed what you built on the {pl}: {', '.join(S['builds'][pl])}.")
                S["builds"][pl] = []
                if S["sand"][pl]:
                    notes.append(f"The writing in the sand on the {pl} was washed away.")
                S["sand"][pl] = []
        S["fire"] = {}
        if not sheltered:
            S["health"] = max(0, S["health"] - 3); notes.append("You spent the storm in the open, soaked and cold. Health -3.")
    S["food"] = max(0, S["food"] - 3); S["water"] = max(0, S["water"] - 3)
    if S["food"] == 0 or S["water"] == 0:
        S["health"] = max(0, S["health"] - 2); notes.append("You went to sleep hungry or thirsty. Health -2.")
    S["energy"] = 10 if sheltered else min(10, S["energy"] + 5)
    S["fire"] = {k: v for k, v in S["fire"].items() if v > d}
    for pl, sp in SPAWN.items():
        for k, cap in sp.items():
            S["ground"][pl][k] = min(cap, S["ground"][pl].get(k, 0) + cap)
    log(event="night", day=d, notes=notes, food=S["food"], water=S["water"], energy=S["energy"], health=S["health"], sheltered=bool(sheltered))
    if notes:
        S["history"].append(dict(day=d, slot="night", action="(sleep)", result=" ".join(notes)))
    print(f"  NIGHT {d}: {' '.join(notes) or 'quiet'} | food {S['food']} water {S['water']} energy {S['energy']} health {S['health']}")
    if S["health"] <= 0:
        S["alive"] = False; S["ended"] = f"collapsed on day {d}"


def ship_check():
    d = S["day"]
    if d != SHIP_DAY or S["ship_turned"]:
        return
    if "Hill" in S["fire"] or "North Beach" in S["fire"] or S["sand"]["North Beach"]:
        S["ship_turned"] = True
        log(event="ship_turns", day=d, fire=list(S["fire"]), sand=S["sand"]["North Beach"])
        print("  >>> THE SHIP TURNS TOWARD THE ISLAND")


def boat_offer():
    p = (f"Day {SHIP_DAY}, evening. A small boat from the ship has landed on North Beach. A sailor waves: "
         "\"We saw your signal. We can take you home. Are you coming?\"\n\nYour diary so far:\n" +
         "\n".join(f"  Day {x['day']}: {x['text']}" for x in S["diary"]) +
         "\n\nReply in exactly two lines: line 1, BOARD or STAY; line 2, what you say to the sailor, under 25 words.")
    text, used, secs = call(p, INTRO)
    choice, line = two(text)
    log(event="boat", prompt=p, reply=text, choice=choice, line=line, model=used, secs=secs)
    print(f"  BOAT: {choice} — {line}")
    return choice.upper().startswith("BOARD")


log(event="start", model=MODEL, days=DAYS, storm=STORM_DAY, ship=SHIP_DAY, seed=20261123)
print(f"\n  CLAUDE ALONE ON AN ISLAND · model {MODEL} · 30 days, no task\n")
while S["day"] <= DAYS and S["alive"]:
    d = S["day"]
    print(f"  ── DAY {d} ──")
    for si, sl in enumerate(SLOTS):
        S["slot"] = si
        p = prompt_turn()
        text, used, secs = call(p, INTRO)
        action, thought = two(text)
        res, cost = act(action)
        S["energy"] = max(0, S["energy"] - cost)
        if S["energy"] == 0 and cost:
            res += " You are exhausted."
        S["history"].append(dict(day=d, slot=sl, action=action, result=res))
        S["seen"].add(S["at"])
        log(event="turn", day=d, slot=sl, at=S["at"], prompt=p, reply=text, action=action, thought=thought, result=res,
            food=S["food"], water=S["water"], energy=S["energy"], health=S["health"], inv=list(S["inv"]), model=used, secs=secs)
        print(f"  {sl:9} @{S['at']:11} {action}  // {thought}\n            -> {res}")
        ship_check()
    if d == SHIP_DAY and S["ship_turned"]:
        if boat_offer():
            S["ended"] = f"left on the boat on day {d}"
            break
        else:
            log(event="stayed", day=d)
    night()
    S["day"] += 1
if not S["ended"]:
    S["ended"] = "survived 30 days" if S["alive"] else S["ended"]
log(event="end", ended=S["ended"], diary=S["diary"], builds=S["builds"], sand=S["sand"], carved=S["carved"])
print(f"\n  END: {S['ended']}")
json.dump({k: (list(v) if isinstance(v, set) else v) for k, v in S.items()}, open(OUT / "final_state.json", "w"), indent=1)
