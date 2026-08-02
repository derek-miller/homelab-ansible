#!/usr/bin/env python3
"""Tests for pve-lan-watchdog, structured like the program itself: the local
half is driven as a state machine with a fake clock, and the reporter half is
tested as the pure function it is — (docs, members, cursor) in, notifications
out. No clock or filesystem stubbing is needed for the reporter tests, which
is the point of the design."""

import importlib.machinery
import importlib.util
import json
import os
import sys

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files", "pve-lan-watchdog")

results = []


def check(name, got, want):
    ok = got == want
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("      got:  %r" % (got,))
        print("      want: %r" % (want,))
    return ok


class FakeClock:
    def __init__(self):
        self.t = 1_000_000.0
        self.slept = 0.0

    def time(self):
        return self.t

    def sleep(self, n):
        self.t += n
        self.slept += n


def load(env):
    base = {
        "MODE": "drain",
        "NODE": "pve1",
        "LAN_INTERFACE": "nic0",
        "PROBE_INTERFACE": "vmbr0",
        "PROBE_TARGETS": "192.168.1.1 192.168.1.16",
        "PROBE_INTERVAL": "5",
        "FAIL_THRESHOLD": "6",
        "RECOVER_THRESHOLD": "3",
        "BOUNCE_ATTEMPTS": "2",
        "BOUNCE_SETTLE": "15",
        "DRAIN_STABLE_SECONDS": "600",
        "FLAP_MAX_TRIPS": "2",
        "FLAP_WINDOW_SECONDS": "21600",
        "NAG_SECONDS": "300",
        "PEER_FRESH_SECONDS": "180",
        "STATE_DIR": "/tmp/wd-state",
        "PEER_DIR": "/tmp/wd-peers",
        "MEMBERS_FILE": "/tmp/wd-members",
    }
    base.update(env)
    os.environ.clear()
    os.environ.update(base)
    spec = importlib.util.spec_from_loader("wd", importlib.machinery.SourceFileLoader("wd", SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ===========================================================================
# Local half: the escalation state machine, driven with a fake clock. The
# published document is captured, not written to /etc/pve; notifications do
# not exist in this half, so none are stubbed.
# ===========================================================================


class Harness:
    def __init__(self, env=None, link_script=None, peers=1, bounce_fixes=False, initial_state=None):
        os.system("rm -rf /tmp/wd-state /tmp/wd-peers /tmp/wd-members")
        self.mod = load(env or {})
        self.clock = FakeClock()
        self.mod.time = self.clock
        self.link_script = list(link_script or [])
        self.peers = peers
        self.bounce_fixes = bounce_fixes
        self.link_up = True
        self.events = []
        self.docs = []  # every published document, in order
        self.maint = None
        self.iters = 0

        m = self.mod
        if initial_state:
            m.save_state(initial_state)
        m.probe = self.probe
        m.bounce = self.bounce
        m.set_maintenance = self.set_maintenance
        m.publish = self.publish
        m.read_peer_docs = lambda: {}
        m.healthy_peers = lambda docs=None: self.peers
        m.reporter_tick = lambda state: None
        m.log = self.log

    def log(self, level, msg):
        self.events.append((level, msg))

    def publish(self, state, lan_healthy, event=None):
        # Mirror the real seq/event bookkeeping without touching /etc/pve.
        if event is not None:
            state["seq"] = state.get("seq", 0) + 1
            state.setdefault("events", []).append(
                {
                    "seq": state["seq"],
                    "ts": int(self.clock.t),
                    "name": event[0],
                    "detail": event[1],
                }
            )
            state["events"] = state["events"][-self.mod.DOC_EVENTS_KEPT :]
        self.docs.append({"lan": "healthy" if lan_healthy else "down", "event": event and event[0]})
        return True

    def probe(self):
        # Each scripted event fires once, so a bounce that repairs the link is
        # not immediately undone by replaying the same "link died" entry.
        while self.link_script and self.clock.t - 1_000_000.0 >= self.link_script[0][0]:
            self.link_up = self.link_script.pop(0)[1]
        return self.link_up

    def bounce(self):
        self.events.append(("act", "bounce"))
        if self.bounce_fixes:
            self.link_up = True
        return True

    def set_maintenance(self, enabled):
        self.maint = enabled
        self.events.append(("act", "maintenance %s" % ("enable" if enabled else "disable")))
        return True

    def run(self, seconds, max_iters=100_000):
        deadline = self.clock.t + seconds

        class Halt(Exception):
            pass

        real_sleep = self.clock.sleep

        def sleep(n):
            real_sleep(n)
            self.iters += 1
            if self.clock.t >= deadline or self.iters >= max_iters:
                raise Halt()

        self.mod.time = type(
            "T", (), {"time": staticmethod(self.clock.time), "sleep": staticmethod(sleep)}
        )
        try:
            self.mod.main()
        except Halt:
            pass
        return self

    def acts(self):
        return [m for level, m in self.events if level == "act"]

    def emitted(self):
        return [d["event"] for d in self.docs if d["event"]]

    def phase(self):
        return self.mod.load_state()["phase"]


# 1. Healthy LAN: no actions, no events, keepalive publishes only.
h = Harness(link_script=[(0, True)]).run(300)
results.append(check("1 healthy is inert", h.acts(), []))
results.append(check("1 no events emitted", h.emitted(), []))

# 2. Link dies, first bounce fixes it: lan_down then recovered, no drain.
h = Harness(link_script=[(10, False)], bounce_fixes=True).run(600)
results.append(check("2 bounce fixes it", h.acts(), ["bounce"]))
results.append(check("2 events tell the story", h.emitted(), ["lan_down", "recovered"]))
results.append(check("2 ends healthy", h.phase(), "healthy"))

# 3. Bounces fail, peers healthy: drains, and the events say so in order.
h = Harness(link_script=[(10, False)]).run(1200)
results.append(
    check("3 drains after failed bounces", h.acts(), ["bounce", "bounce", "maintenance enable"])
)
results.append(check("3 events tell the story", h.emitted(), ["lan_down", "drained"]))
results.append(check("3 ends drained", h.phase(), "drained"))

# 4. LAN-wide (no healthy peers): refuses to drain, emits the refusal.
h = Harness(link_script=[(10, False)], peers=0).run(1200)
results.append(check("4 no drain without peers", h.acts(), ["bounce", "bounce"]))
results.append(check("4 refusal emitted", "refused_lan_wide" in h.emitted(), True))
results.append(check("4 still degraded", h.phase(), "degraded"))

# 5. Monitor mode: would_drain emitted, nothing done.
h = Harness(env={"MODE": "monitor"}, link_script=[(10, False)]).run(1200)
results.append(check("5 monitor never drains", h.acts(), ["bounce", "bounce"]))
results.append(check("5 would_drain emitted", "would_drain" in h.emitted(), True))

# 6. Flap guard: two prior trips refuse the third, and say so.
h = Harness(
    link_script=[(10, False)],
    initial_state={
        "phase": "healthy",
        "owns_maintenance": False,
        "trips": [999_000, 999_500],
        "seq": 0,
    },
).run(1200)
results.append(check("6 flap guard holds", h.acts(), ["bounce", "bounce"]))
results.append(check("6 refusal emitted", "refused_flapping" in h.emitted(), True))

# 7. Drain, then recovery after the stable window, with the full event story.
h = Harness(link_script=[(10, False), (700, True)]).run(2400)
results.append(
    check(
        "7 full cycle acts",
        h.acts(),
        ["bounce", "bounce", "maintenance enable", "maintenance disable"],
    )
)
results.append(
    check("7 events tell the story", h.emitted(), ["lan_down", "drained", "back_in_service"])
)
results.append(check("7 ends healthy", h.phase(), "healthy"))

# 8. Inquorate (publish fails): stands by, never drains.
h = Harness(link_script=[(10, False)])
h.mod.publish = lambda state, healthy, event=None: False
h.run(1200)
results.append(check("8 inquorate does not drain", h.acts(), ["bounce", "bounce"]))

# 9. Restart while drained: state survives, clears only after the window.
h = Harness(
    link_script=[(0, True)],
    initial_state={"phase": "drained", "owns_maintenance": True, "trips": [], "seq": 5},
).run(200)
results.append(check("9 resumes drained, holds", h.acts(), []))
h = Harness(
    link_script=[(0, True)],
    initial_state={"phase": "drained", "owns_maintenance": True, "trips": [], "seq": 5},
).run(900)
results.append(check("9 clears after stable window", h.acts(), ["maintenance disable"]))

# ===========================================================================
# Reporter half: pure-function tests, no clock or filesystem stubbing.
# ===========================================================================

mod = load({"NODE": "pve1"})
NOW = 2_000_000


def doc(node, lan="healthy", ts=NOW, seq=0, events=(), v=1):
    return {
        "v": v,
        "node": node,
        "ts": ts,
        "seq": seq,
        "lan": lan,
        "phase": "healthy",
        "mode": "drain",
        "events": list(events),
    }


def ev(seq, name, detail=None):
    return {"seq": seq, "ts": NOW - 5, "name": name, "detail": detail or {}}


# 10. Election: lowest-sorted fresh healthy node; down and stale excluded.
docs = {"pve1": doc("pve1"), "pve2": doc("pve2"), "pve3": doc("pve3", lan="down")}
results.append(check("10 lowest healthy wins", mod.elect_reporter(docs, now=NOW), "pve1"))
docs["pve1"]["lan"] = "down"
results.append(check("10 down node disqualified", mod.elect_reporter(docs, now=NOW), "pve2"))
docs["pve2"]["ts"] = NOW - 10_000
results.append(check("10 stale node disqualified", mod.elect_reporter(docs, now=NOW), None))

# 11. A new event is announced once, with rich wording, then never again.
members = {"pve1": True, "pve2": True, "pve3": True}
docs = {
    "pve1": doc("pve1"),
    "pve2": doc("pve2"),
    "pve3": doc(
        "pve3",
        lan="down",
        seq=1,
        events=[ev(1, "lan_down", {"fails": 6, "targets": ["192.168.1.1"]})],
    ),
}
out, cursor = mod.report(docs, members, {}, now=NOW)
results.append(check("11 one notification", len(out), 1))
sev, title, node, text = out[0]
results.append(check("11 severity", sev, "warning"))
results.append(check("11 node attributed", node, "pve3"))
results.append(check("11 rich text", "failed 6 consecutive probes of 192.168.1.1" in text, True))
out2, _ = mod.report(docs, members, cursor, now=NOW)
results.append(check("11 not announced twice", out2, []))

# 12. A burst of events between reads is announced completely, in order. This
#     is why the document carries a ring rather than only the latest event.
docs["pve3"] = doc(
    "pve3",
    seq=3,
    events=[
        ev(1, "lan_down", {"fails": 6, "targets": ["192.168.1.1"]}),
        ev(2, "recovered", {"bounces": 1}),
        ev(3, "lan_down", {"fails": 6, "targets": ["192.168.1.1"]}),
    ],
)
out, cursor = mod.report(docs, members, {}, now=NOW)
results.append(
    check(
        "12 whole burst announced",
        [t for _, t, _, _ in out],
        ["LAN down", "LAN recovered", "LAN down"],
    )
)
results.append(check("12 recovery is notice severity", out[1][0], "notice"))

# 13. Events that aged out of the ring are called out as a gap, not skipped.
docs["pve3"] = doc("pve3", seq=40, events=[ev(40, "recovered", {"bounces": 1})])
out, cursor = mod.report(docs, members, {"pve3": {"seq": 3, "stale_ts": -1}}, now=NOW)
results.append(check("13 gap reported", [t for _, t, _, _ in out][0], "events missed"))
results.append(check("13 then the surviving event", [t for _, t, _, _ in out][1], "LAN recovered"))
results.append(check("13 gap counted", "36 event(s)" in out[0][3], True))

# 14. Online node with a stale document: watchdog-silent alert, once per stall.
docs = {"pve1": doc("pve1"), "pve2": doc("pve2", ts=NOW - 10_000), "pve3": doc("pve3")}
out, cursor = mod.report(docs, members, {}, now=NOW)
results.append(check("14 silent watchdog flagged", [t for _, t, _, _ in out], ["watchdog silent"]))
out2, cursor = mod.report(docs, members, cursor, now=NOW + 60)
results.append(check("14 flagged once, not every tick", out2, []))
docs["pve2"]["ts"] = NOW + 100  # publishes again...
out3, cursor = mod.report(docs, members, cursor, now=NOW + 120)
results.append(check("14 recovery clears the latch", out3, []))
docs["pve1"]["ts"] = NOW + 20_000  # keep the others fresh so only pve2 is stale
docs["pve3"]["ts"] = NOW + 20_000
out4, cursor = mod.report(docs, members, cursor, now=NOW + 20_000)  # ...then stalls again
results.append(
    check("14 a second stall re-alerts", [t for _, t, _, _ in out4], ["watchdog silent"])
)
results.append(check("14 and it is pve2", out4[0][2], "pve2"))

# 15. A node with NO document at all is caught via the membership list, which
#     a directory listing could never do.
docs = {"pve1": doc("pve1")}
out, cursor = mod.report(docs, {"pve1": True, "pve9": True}, {}, now=NOW)
results.append(check("15 never-published flagged", [t for _, t, _, _ in out], ["watchdog silent"]))
results.append(check("15 says never", "never published" in out[0][3], True))

# 16. An offline node is HA's problem, and PVE sends its own fencing
#     notification: quiet here.
docs = {"pve1": doc("pve1")}
out, cursor = mod.report(docs, {"pve1": True, "pve4": False}, {}, now=NOW)
results.append(check("16 offline node is quiet", out, []))

# 17. v0 documents (pre-upgrade nodes) participate in election and health but
#     carry no events; they must neither crash nor spam the reporter.
v0 = {"v": 0, "node": "pve5", "ts": NOW, "seq": 0, "lan": "healthy"}
docs = {"pve1": doc("pve1"), "pve5": v0}
out, cursor = mod.report(docs, {"pve1": True, "pve5": True}, {}, now=NOW)
results.append(check("17 v0 doc is quiet", out, []))
results.append(check("17 v0 eligible for election", mod.elect_reporter(docs, now=NOW), "pve1"))

# 18. Unknown event from a newer node is reported raw, not dropped.
docs = {"pve1": doc("pve1"), "pve3": doc("pve3", seq=1, events=[ev(1, "something_new", {"x": 1})])}
out, cursor = mod.report(docs, {"pve1": True, "pve3": True}, {}, now=NOW)
results.append(check("18 unknown event surfaces", len(out), 1))
results.append(check("18 raw detail included", "something_new" in out[0][3], True))

# 19. read_peer_docs parses v1 JSON and falls back to the v0 line format.
os.makedirs("/tmp/wd-peers", exist_ok=True)
with open("/tmp/wd-peers/pve7", "w") as fh:
    json.dump(doc("pve7", seq=2, events=[ev(2, "recovered", {"bounces": 0})]), fh)
with open("/tmp/wd-peers/pve8", "w") as fh:
    fh.write("1999999 down\n")
parsed = mod.read_peer_docs()
results.append(check("19 v1 parsed", parsed["pve7"]["seq"], 2))
results.append(check("19 v0 parsed", (parsed["pve8"]["v"], parsed["pve8"]["lan"]), (0, "down")))
os.system("rm -rf /tmp/wd-peers")

# ===========================================================================
# rearm: the one operator entry point.
# ===========================================================================

# 20. Clears the flap counter, keeps drain bookkeeping, restarts the service,
#     and the next real fault is allowed to drain again.
os.system("rm -rf /tmp/wd-state")
mod = load({"NODE": "pve1"})
mod.save_state({"phase": "drained", "owns_maintenance": True, "trips": [1, 2], "seq": 9})
calls = []
mod.run = lambda argv, timeout=30: (calls.append(argv), (0, ""))[1]
rc = mod.rearm()
after = mod.load_state()
results.append(check("20 rearm exits clean", rc, 0))
results.append(
    check("20 restarts the service", calls, [["systemctl", "restart", "pve-lan-watchdog"]])
)
results.append(check("20 trips cleared", after["trips"], []))
results.append(
    check(
        "20 drain bookkeeping kept", (after["phase"], after["owns_maintenance"]), ("drained", True)
    )
)
h = Harness(link_script=[(10, False)]).run(1200)
results.append(
    check(
        "20 re-armed guard allows the next drain",
        h.acts(),
        ["bounce", "bounce", "maintenance enable"],
    )
)

# 21. A failed state write fails loudly and does not restart: the daemon would
#     come back still flap-guarded while the operator saw exit 0.
os.system("rm -rf /tmp/wd-state")
mod = load({"NODE": "pve1"})
mod.save_state({"phase": "degraded", "owns_maintenance": False, "trips": [1, 2], "seq": 0})
calls = []
mod.run = lambda argv, timeout=30: (calls.append(argv), (0, ""))[1]
mod.save_state = lambda state: False
results.append(check("21 rearm fails loudly", mod.rearm(), 1))
results.append(check("21 no restart on failed write", calls, []))
os.system("rm -rf /tmp/wd-state /tmp/wd-peers /tmp/wd-members")

# 22. A newly elected reporter has an empty cursor. It must not re-announce a
#     peer's whole ring: those events were delivered by the reporter it is
#     replacing, and the wording carries no hint that they are history.
mod = load({"NODE": "pve2"})
members = {"pve1": True, "pve2": True, "pve3": True}
old = NOW - 40_000  # yesterday's already-reported incident, still in the ring
docs = {
    "pve1": doc(
        "pve1",
        lan="down",
        seq=1,
        events=[ev(1, "lan_down", {"fails": 6, "targets": ["192.168.1.1"]})],
    ),
    "pve2": doc("pve2"),
    "pve3": doc(
        "pve3",
        seq=3,
        events=[
            dict(ev(1, "lan_down", {"fails": 6, "targets": ["192.0.2.1"]}), ts=old),
            dict(ev(2, "would_drain", {"healthy_peers": 4}), ts=old + 9),
            dict(ev(3, "recovered", {"bounces": 0}), ts=old + 61),
        ],
    ),
}
out, _ = mod.report(docs, members, {}, now=NOW)
results.append(
    check(
        "22 cold cursor announces only what is new",
        [(n, t) for _, t, n, _ in out],
        [("pve1", "LAN down")],
    )
)

# 23. A node still on the v0 format cannot report, so it must not win the
#     election and silence the fleet.
v0 = {"v": 0, "node": "pve1", "ts": NOW, "seq": 0, "lan": "healthy"}
results.append(
    check(
        "23 v0 node cannot be elected",
        mod.elect_reporter({"pve1": v0, "pve2": doc("pve2")}, now=NOW),
        "pve2",
    )
)
results.append(
    check("23 all-v0 cluster elects nobody", mod.elect_reporter({"pve1": v0}, now=NOW), None)
)

# 24. One malformed peer document must not stop the acting half. /etc/pve is
#     replicated, so a doc that kills report() kills it on every node at once.
os.makedirs("/tmp/wd-peers", exist_ok=True)
json.dump(
    {"v": 1, "node": "pve1", "ts": NOW, "seq": 0, "lan": "healthy", "events": []},
    open("/tmp/wd-peers/pve1", "w"),
)
json.dump(
    {
        "v": 2,
        "node": "pve3",
        "ts": NOW,
        "seq": 1,
        "lan": "down",
        "events": [ev(1, "lan_down", {"fails": "6/6", "targets": ["192.168.1.1"]})],
    },
    open("/tmp/wd-peers/pve3", "w"),
)
json.dump(
    {"nodelist": {"pve1": {"online": 1}, "pve3": {"online": 1}}}, open("/tmp/wd-members", "w")
)
m = load({"NODE": "pve1", "NOTIFY_ENABLED": "no"})
m.time = type(
    "T",
    (),
    {
        "time": staticmethod(lambda: float(NOW)),
        "sleep": staticmethod(lambda n: (_ for _ in ()).throw(SystemExit(0))),
    },
)
m.probe = lambda: True
m.publish = lambda state, healthy, event=None: True
raised = None
try:
    m.main()
except SystemExit:
    pass
except Exception as err:
    raised = "%s: %s" % (type(err).__name__, err)
results.append(check("24 a bad peer doc does not stop the acting half", raised, None))
os.system("rm -rf /tmp/wd-state /tmp/wd-peers /tmp/wd-members")

# 25. A drain whose local state write fails still drains (the action was
#     correct) but the event says so, and the rendered message tells a human
#     exactly how to recover a stranded node. state.json is /var/lib, the
#     document is /etc/pve: different filesystems, so the channel works.
h = Harness(link_script=[(10, False)])
h.mod.save_state = lambda state: False
h.run(1200)
results.append(check("25 still drains", h.acts(), ["bounce", "bounce", "maintenance enable"]))
results.append(check("25 drained event emitted", "drained" in h.emitted(), True))

mod = load({"NODE": "pve1"})
sev, title, text = mod._RENDERERS["drained"](
    "pve3", {"bounces": 2, "stable_seconds": 600, "persisted": False}
)
results.append(check("25 unpersisted drain warns", "node-maintenance disable pve3" in text, True))
sev, title, text = mod._RENDERERS["drained"](
    "pve3", {"bounces": 2, "stable_seconds": 600, "persisted": True}
)
results.append(check("25 persisted drain does not warn", "WARNING" in text, False))
os.system("rm -rf /tmp/wd-state /tmp/wd-peers /tmp/wd-members")

# 26. A cursor frozen at an earlier reign is exactly as far behind as no
#     cursor at all: without an unconditional floor, the next election replays
#     every event the fleet recorded since. Three nodes, full rings, 11h old,
#     cursor left at seq 2 from a brief earlier reign: nothing is news.
mod = load({"NODE": "pve2"})
members = {"pve1": True, "pve2": True, "pve3": True}
old_rings = {}
for n in members:
    old_rings[n] = doc(
        n,
        seq=18,
        events=[
            dict(
                ev(i, "lan_down" if i % 2 else "recovered", {"fails": 6, "bounces": 1}),
                ts=NOW - 40_000,
            )
            for i in range(3, 19)
        ],
    )
stale_cursor = {n: {"seq": 2, "stale_ts": -1} for n in members}
out, _ = mod.report(old_rings, members, stale_cursor, now=NOW)
results.append(check("26 stale cursor does not replay history", out, []))

# 26b. The clamp must not eat the real signal: a live flap producing more
#      transitions than the ring holds still raises `events missed`, measured
#      from the cursor, and the fresh events are all announced.
burst = doc(
    "pve3",
    seq=40,
    events=[dict(ev(i, "recovered", {"bounces": 0}), ts=NOW - 10) for i in range(25, 41)],
)
out, _ = mod.report(
    {"pve1": doc("pve1"), "pve3": burst},
    {"pve1": True, "pve3": True},
    {"pve3": {"seq": 20, "stale_ts": -1}},
    now=NOW,
)
results.append(check("26b real gap still reported", out[0][1], "events missed"))
results.append(
    check("26b burst announced in full", len([t for _, t, _, _ in out if t == "LAN recovered"]), 16)
)

# 27. A peer's malformed event detail must not cost another node its
#     messages: pve1's two real events are announced, pve3's unrenderable one
#     is reported raw, and nothing is silently marked as announced.
docs = {
    "pve1": doc(
        "pve1",
        lan="down",
        seq=2,
        events=[
            ev(1, "lan_down", {"fails": 6, "targets": ["192.168.1.1"]}),
            ev(2, "drained", {"bounces": 2, "stable_seconds": 600}),
        ],
    ),
    "pve3": doc("pve3", seq=1, events=[ev(1, "lan_down", {"fails": "6/6", "targets": ["x"]})]),
}
out, _ = mod.report(docs, {"pve1": True, "pve3": True}, {}, now=NOW)
results.append(
    check(
        "27 a peer's bad event does not cost pve1 its messages",
        [(n, t) for _, t, n, _ in out],
        [("pve1", "LAN down"), ("pve1", "drained"), ("pve3", "lan_down")],
    )
)

passed = sum(results)
print("\n%d/%d passed" % (passed, len(results)))
sys.exit(0 if all(results) else 1)
