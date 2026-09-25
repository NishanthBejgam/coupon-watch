"""The coupon watcher - one signal for the board.

The "Gold coupon" is not a clip coupon. It is an Amazon Pay *reward*: a
"collect" card at

    https://www.amazon.in/h/rewards/dp/amzn1.rewards.rewardAd.<ID>?rdpf=en

that, once collected, pays cashback on a jewellery order. The deal channels
find it by hand and repost the URL within minutes; the board should not
depend on them. So this watcher does two things, in this order of trust:

  CONFIRM  ask Amazon. Every reward page, logged out, embeds
           `'rewardStatus': 'CAN_BE_COLLECTED'` (or 'EXPIRED') and a headline
           such as "GET FLAT ₹1500 BACK Min order: ₹15000 Valid till 11 Nov".
           That is the only thing that flips the signal. Watched: the stable
           `jewellery` vanity slug plus every ID ever seen.

  HARVEST  feed new IDs in from where people post them. Reward Switch's
           15-minute watcher reads DesiDime and the Telegram previews and
           commits every new id to its public seed; this takes them from
           there, so a reward launched under a fresh ID is confirmed on the
           next tick rather than never. Harvest never sets the signal.

The signal is ONE file, signal/coupon.json, in the shape AmazonGold's
coupon.py reads. This repo is public so its two-hourly Actions run is free;
AmazonGold (private) reads the file over the raw URL at build time, and a
flip sends it a repository_dispatch so the board rebuilds within minutes.
The visitor can still choose a what-if or a typed coupon in the menu.

    python watch_coupon.py            one tick, print, write signal/
    python watch_coupon.py --commit   ...and commit + push if changed
    python watch_coupon.py --no-harvest   Amazon only (quick check)

Environment (all optional): AG_PROXY_URL/AG_PROXY_KEY reroute the Amazon
fetch; AG_RS_SEED_URL overrides where harvested ids are read from;
AG_DISPATCH_REPO + AG_DISPATCH_TOKEN ("owner/repo" and a token
with contents:write) fire the board's build when the signal changes. No
alerts: the signal file is the only output.
"""

import argparse
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
SIGNAL_DIR = os.path.join(ROOT, "signal")
STATE_PATH = os.path.join(SIGNAL_DIR, "rewards.json")
SIGNAL_PATH = os.path.join(SIGNAL_DIR, "coupon.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

REWARD_URL = "https://www.amazon.in/h/rewards/dp/amzn1.rewards.rewardAd.{id}?rdpf=en"
# The vanity slug Amazon reuses for the category campaign, plus the IDs the
# last jewellery rewards ran under (Amazon reuses those too) - always watched.
SEED_IDS = {"jewellery": "seed", "6TYRY3PUH4R3O": "seed", "YQZQRRYLJITTO": "seed",
            "BQILCLYQHAPOS": "seed"}

RE_REWARD = re.compile(r"amzn1\.rewards\.rewardAd\.([A-Z0-9]{13}|[a-z]{3,24})")
RE_STATUS = re.compile(r"'rewardStatus':\s*'([A-Z_]+)'")
RE_HEAD = re.compile(
    r"GET\s+(FLAT|UP\s+TO)\s+₹\s?([\d,]+)\s+BACK"
    r"(?:\s+(\d+)%\s+offer,)?\s*(?:Min(?:imum)?\s+order:?\s+₹\s?([\d,]+))?"
    r"(?:.*?Valid\s+till\s+(\d{1,2})\s+([A-Za-z]{3}))?", re.I | re.S)
RE_TAG = re.compile(r"<(script|style)[^>]*>.*?</\1>|<[^>]+>", re.S)
JEWEL = re.compile(r"jewel|gold|silver|\bcoins?\b|\bbars?\b|vedhani|pendant", re.I)
# "…applicable on all orders except … h) Jewelry products listed on …" - a
# clause that names jewellery to exclude it must not make the reward jewellery.
EXCLUDES = re.compile(r"exclud|except|not applicable|n't applicable|not valid", re.I)
CLAUSE = re.compile(r"(?<=[.!?])\s+|\s(?=\d{1,2}\.\s)")
TOPIC = re.compile(r"amazon|jewel|gold|coin|cashback|reward|collect|silver", re.I)

MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}

RS_SEED_URL = os.environ.get(
    "AG_RS_SEED_URL", "https://raw.githubusercontent.com/NishanthBejgam/rewardswitch/main/seed/catalog.json")

AMAZON_PACE = 8           # seconds between Amazon hits - ~10 quick ones get us TLS-dropped
MAX_AMAZON_PER_TICK = 8   # slug + the freshest IDs; the rest wait for the next tick
NON_JEWEL_EVERY = 24 * 3600
FORGET_AFTER = 90 * 24 * 3600


# ---------------------------------------------------------------- fetching

def _curl(url, timeout=30, proxy=None):
    target = url
    if proxy and proxy[0] and proxy[1]:
        target = (proxy[0].replace("{url}", urllib.parse.quote(url, safe=""))
                          .replace("{key}", proxy[1]))
    proc = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", str(timeout), "-A", UA,
         "-H", "Accept-Language: en-IN,en;q=0.9", "-H", "Accept: text/html,*/*", target],
        capture_output=True, timeout=timeout + 5)
    if proc.returncode != 0:
        raise IOError("curl exit %d: %s" % (proc.returncode,
                      proc.stderr.decode("utf-8", "replace")[:160]))
    return proc.stdout.decode("utf-8", "replace")


def _get(url, timeout=20):
    """Plain urllib for sites that do not fingerprint (DesiDime, Telegram)."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html,*/*"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.geturl(), r.read().decode("utf-8", "replace")


def _text(html):
    return re.sub(r"\s+", " ", RE_TAG.sub(" ", html)).strip()


# ---------------------------------------------------------------- confirm

def read_reward(rid, proxy=None):
    """One reward page -> {status, headline, jewellery, coupon}."""
    html = _curl(REWARD_URL.format(id=rid), proxy=proxy)
    if "api-services-support@amazon.com" in html or "Enter the characters" in html:
        raise IOError("captcha")
    m = RE_STATUS.search(html)
    if not m:
        # A page without the status block is a bot wall or an interstitial,
        # not an answer - leave what we know about this id alone.
        raise IOError("no rewardStatus in page")
    status = m.group(1)
    # The nav bar lists "Jewellery" as a category, so only the reward block
    # and what follows it (FAQ, terms) count when deciding relevance.
    i = html.find("dp-rewards-coupons-wrapper")
    if i > 0:
        i = html.rfind("<", 0, i)                   # back to the tag's own "<"
    j = html.find("navFooter", i if i > 0 else 0)
    body = _text(html[i:j] if i > 0 else html)
    if len(body) > 4000:
        body = body[:4000]
    head = RE_HEAD.search(body)
    headline = re.sub(r"\s+", " ", head.group(0)).strip() if head else body[:120]
    return {
        "status": status,
        "headline": headline,
        "jewellery": is_jewellery(body),
        "coupon": _coupon_from(rid, head, headline) if head else None,
    }


def is_jewellery(body):
    """True when a clause *offers* the reward on jewellery; clauses that
    exclude it (the generic "all orders except … Jewelry" rewards) don't count."""
    return any(JEWEL.search(c) and not EXCLUDES.search(c) for c in CLAUSE.split(body))


def _coupon_from(rid, m, headline):
    kind, amount, pct, minimum, day, mon = m.groups()
    amount = float(amount.replace(",", ""))
    ends = None
    if day and mon and mon.lower()[:3] in MONTHS:
        now = time.localtime()
        y = now.tm_year
        try:
            ends = time.mktime((y, MONTHS[mon.lower()[:3]], int(day), 23, 59, 59, 0, 0, -1))
            if ends < time.time() - 3 * 86400:      # "Valid till 11 Nov" seen in October
                ends = time.mktime((y + 1, MONTHS[mon.lower()[:3]], int(day), 23, 59, 59, 0, 0, -1))
        except (OverflowError, ValueError):
            ends = None
    c = {
        "code": "Amazon Pay reward",
        "percent": float(pct) if pct else None,
        "flat": None if pct else amount,
        "max": amount if pct else None,
        "min": float(minimum.replace(",", "")) if minimum else 0,
        "asins": None,
        "appliesTo": "jewellery",
        "endsAt": ends,
        "rewardId": rid,
        "url": REWARD_URL.format(id=rid),
        "headline": headline,
    }
    return c


# ---------------------------------------------------------------- harvest

def harvest_rewardswitch(log):
    """One harvester, not two: Reward Switch's 15-minute watcher reads DesiDime
    and the Telegram previews (following the visit.desidime.com / shortener
    redirects that hide the reward id) and commits every new id to its public
    seed. This takes the ids it found there - and pokes this workflow with a
    repository_dispatch when it finds one, so a new id is confirmed at once."""
    try:
        _, body = _get(RS_SEED_URL)
        seed = json.loads(body)
    except Exception as e:  # noqa: BLE001
        log("  reward switch seed: %s" % e)
        return []
    return [(r["ad"], "rs:" + r["source"]) for r in seed.get("rewards", []) if r.get("source")]


# ---------------------------------------------------------------- state

def load_state():
    try:
        with io.open(STATE_PATH, encoding="utf-8") as fh:
            s = json.load(fh)
    except (OSError, ValueError):
        s = {}
    s.setdefault("ids", {})
    s.setdefault("cursors", {})
    for rid, src in SEED_IDS.items():
        s["ids"].setdefault(rid, {"firstSeen": _now(), "source": src})
    return s


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    body = json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    old = None
    try:
        with io.open(path, encoding="utf-8") as fh:
            old = fh.read()
    except OSError:
        pass
    if old == body:
        return False
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(body)
    return True


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _due(entry, now):
    """Which IDs to ask Amazon about this tick."""
    last = entry.get("checkedAt", 0)
    if entry.get("jewellery", True) or entry.get("status") == "CAN_BE_COLLECTED":
        return True                                     # every tick
    return now - last > NON_JEWEL_EVERY


# ---------------------------------------------------------------- tick

def tick(harvest=True, log=print):
    state = load_state()
    ids, cursors = state["ids"], state["cursors"]
    now = time.time()
    proxy = (os.environ.get("AG_PROXY_URL"), os.environ.get("AG_PROXY_KEY"))

    # 1. harvest - new IDs only, never the signal
    new = []
    if harvest:
        log("harvesting…")
        new = [(rid, src) for rid, src in harvest_rewardswitch(log) if rid not in ids]
        for rid, src in new:
            if rid not in ids:
                ids[rid] = {"firstSeen": _now(), "source": src}
                log("  new reward id %s (%s)" % (rid, src))

    # 2. confirm - Amazon is the only source of truth
    log("confirming with Amazon…")
    due = [r for r, e in ids.items() if _due(e, now)]
    fresh = [r for r in due if not ids[r].get("checkedAt")]
    order = ["jewellery"] if "jewellery" in due else []
    order += [r for r in fresh if r not in order]
    order += [r for r in due if r not in order]
    changes = []
    for n, rid in enumerate(order[:MAX_AMAZON_PER_TICK]):
        if n:
            time.sleep(AMAZON_PACE)
        e = ids[rid]
        try:
            r = read_reward(rid, proxy=proxy)
        except Exception as ex:  # noqa: BLE001
            log("  %s: unreadable (%s)" % (rid, ex))
            e["error"] = str(ex)[:120]
            continue
        e.pop("error", None)
        before = e.get("status")
        e.update({"status": r["status"], "headline": r["headline"], "jewellery": r["jewellery"],
                  "coupon": r["coupon"], "checkedAt": now, "checkedAtIso": _now()})
        log("  %-14s %-16s %s%s" % (rid, r["status"], r["headline"][:70], "" if r["jewellery"] else "  (not jewellery)"))
        if before != r["status"] and r["jewellery"]:
            changes.append((rid, before, r["status"], r["headline"]))

    # 3. forget stale non-jewellery ids
    for rid in list(ids):
        e = ids[rid]
        if rid in SEED_IDS or e.get("jewellery", True):
            continue
        if e.get("status") == "EXPIRED" and now - (e.get("checkedAt") or now) > FORGET_AFTER:
            del ids[rid]

    # 4. the signal: the best live jewellery reward, or nothing
    live = [e["coupon"] for e in ids.values()
            if e.get("status") == "CAN_BE_COLLECTED" and e.get("jewellery") and e.get("coupon")]
    live.sort(key=lambda c: -(c["flat"] or c["max"] or 0))
    signal = dict(live[0], status="live") if live else {"status": "none"}
    # confirmedAt only moves when the signal itself does, so an unchanged
    # signal is byte-identical tick after tick and nothing gets committed.
    try:
        with io.open(SIGNAL_PATH, encoding="utf-8") as fh:
            prev = json.load(fh)
    except (OSError, ValueError):
        prev = {}
    same = {k: v for k, v in prev.items() if k != "confirmedAt"} == signal
    signal["confirmedAt"] = prev["confirmedAt"] if same and prev.get("confirmedAt") else _now()
    state["updatedAt"] = _now()

    # State is saved every tick (timestamps move) but only counts as a
    # change worth committing when an id appeared or a status flipped.
    meaningful = bool(changes) or bool(new)
    save_json(STATE_PATH, state)
    changed = save_json(SIGNAL_PATH, signal) or meaningful
    if live:
        log("SIGNAL live: %s" % live[0]["headline"])
    else:
        log("signal: no live jewellery reward")

    return changed, signal, changes


def dispatch_build(log=print):
    """Tell the boards to rebuild now rather than at their next cron.
    AG_DISPATCH_REPO may list several repos, comma-separated (AmazonGold for
    the coupon tile, Karat Board for the Telegram coupon alert)."""
    repos, tok = os.environ.get("AG_DISPATCH_REPO", ""), os.environ.get("AG_DISPATCH_TOKEN")
    repos = [r.strip() for r in repos.split(",") if r.strip()]
    if not (repos and tok):
        log("no dispatch binding - the boards pick the signal up on their next build")
        return
    for repo in repos:
        req = urllib.request.Request(
            "https://api.github.com/repos/%s/dispatches" % repo,
            data=json.dumps({"event_type": "sweep"}).encode(), method="POST",
            headers={"Authorization": "Bearer " + tok, "Accept": "application/vnd.github+json",
                     "User-Agent": "coupon-watch", "Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=20)
            log("dispatched a build of %s" % repo)
        except Exception as e:  # noqa: BLE001
            log("dispatch to %s failed: %s" % (repo, e))


def commit_if_changed(log=print):
    rel = os.path.relpath(SIGNAL_DIR, ROOT)
    st = subprocess.run(["git", "status", "--porcelain", "--", rel], cwd=ROOT, capture_output=True, text=True)
    if not st.stdout.strip():
        log("nothing to commit")
        return False
    with io.open(SIGNAL_PATH, encoding="utf-8") as fh:
        sig = json.load(fh)
    msg = "watcher: %s" % (("live - " + sig.get("headline", "")) if sig.get("status") == "live" else "no live jewellery reward")
    subprocess.run(["git", "config", "user.name", "coupon-watch"], cwd=ROOT, check=True)
    subprocess.run(["git", "config", "user.email", "watcher@users.noreply.github.com"], cwd=ROOT, check=True)
    subprocess.run(["git", "add", "--", rel], cwd=ROOT, check=True)
    subprocess.run(["git", "commit", "-q", "-m", msg], cwd=ROOT, check=True)
    subprocess.run(["git", "push", "-q"], cwd=ROOT, check=True)
    log("committed: %s" % msg)
    return True


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--commit", action="store_true", help="git commit + push signal/ when it changed")
    ap.add_argument("--no-harvest", action="store_true", help="skip DesiDime/Telegram, ask Amazon only")
    a = ap.parse_args(argv)
    before = _signal_body()
    changed, signal, changes = tick(harvest=not a.no_harvest)
    if a.commit and changed:
        commit_if_changed()
        if _signal_body() != before:
            dispatch_build()
    return 0


def _signal_body():
    try:
        with io.open(SIGNAL_PATH, encoding="utf-8") as fh:
            sig = json.load(fh)
    except (OSError, ValueError):
        return None
    sig.pop("confirmedAt", None)
    return json.dumps(sig, sort_keys=True)


if __name__ == "__main__":
    sys.exit(main())
