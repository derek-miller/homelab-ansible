#!/usr/bin/env python3
"""Drive pve-lan-watchdog's state machine through scenarios with a fake clock."""

import importlib.util, importlib.machinery, os, sys

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "files", "pve-lan-watchdog")


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
        "STATE_DIR": "/tmp/wd-state",
        "PEER_DIR": "/tmp/wd-peers",
    }
    base.update(env)
    os.environ.clear()
    os.environ.update(base)
    spec = importlib.util.spec_from_loader("wd", importlib.machinery.SourceFileLoader("wd", SCRIPT))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Harness:
    def __init__(self, env=None, link_script=None, peers=1, bounce_fixes=False, initial_state=None):
        os.system("rm -rf /tmp/wd-state /tmp/wd-peers")
        self.mod = load(env or {})
        self.clock = FakeClock()
        self.mod.time = self.clock
        self.link_script = list(link_script or [])
        self.peers = peers
        self.bounce_fixes = bounce_fixes
        self.link_up = True
        self.events = []
        self.maint = None
        self.iters = 0
        if initial_state:
            self.mod.save_state(initial_state)

        m = self.mod
        m.probe = self.probe
        m.bounce = self.bounce
        m.set_maintenance = self.set_maintenance
        m.publish = lambda healthy: True
        m.healthy_peers = lambda: self.peers
        m.log = self.log

    def log(self, level, msg):
        self.events.append((level, msg))

    def probe(self):
        # Each scripted event fires once, so a bounce that repairs the link is
        # not immediately undone by replaying the same "link died" entry.
        while self.link_script and self.clock.t - 1_000_000.0 >= self.link_script[0][0]:
            self.link_up = self.link_script.pop(0)[1]
        self.iters += 1
        if self.iters > 4000:
            raise SystemExit("loop guard")
        return self.link_up

    def bounce(self):
        self.events.append(("act", "bounce"))
        if self.bounce_fixes:
            self.link_up = True
        return True

    def set_maintenance(self, enabled):
        self.events.append(("act", "maintenance %s" % ("enable" if enabled else "disable")))
        self.maint = enabled
        return True

    def run(self, seconds):
        limit = 1_000_000.0 + seconds
        real_sleep = self.clock.sleep

        def guarded(n):
            real_sleep(n)
            if self.clock.t > limit:
                raise SystemExit("done")

        self.clock.sleep = guarded
        try:
            self.mod.main()
        except SystemExit:
            pass
        return self

    def acts(self):
        return [m for lvl, m in self.events if lvl == "act"]

    def phase(self):
        return self.mod.load_state()["phase"]


def check(name, got, want):
    ok = got == want
    print("%s %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("     got:  %r\n     want: %r" % (got, want))
    return ok


results = []

# 1. Healthy link: nothing ever happens.
h = Harness().run(3600)
results.append(check("1 healthy link is inert", h.acts(), []))
results.append(check("1 stays healthy", h.phase(), "healthy"))

# 2. Link dies, first bounce fixes it. No drain.
h = Harness(link_script=[(60, False)], bounce_fixes=True).run(1200)
results.append(check("2 bounce fixes it", h.acts(), ["bounce"]))
results.append(check("2 no maintenance", h.maint, None))
results.append(check("2 back to healthy", h.phase(), "healthy"))

# 3. Link dies for good, peers healthy, MODE=drain -> drains after 2 bounces.
h = Harness(link_script=[(60, False)]).run(1200)
results.append(
    check("3 two bounces then drain", h.acts(), ["bounce", "bounce", "maintenance enable"])
)
results.append(check("3 phase drained", h.phase(), "drained"))

# 4. Same failure but no healthy peers -> LAN-wide, must NOT drain.
h = Harness(link_script=[(60, False)], peers=0).run(3600)
results.append(check("4 no drain when peers also down", h.acts(), ["bounce", "bounce"]))
results.append(check("4 phase stays degraded", h.phase(), "degraded"))

# 5. MODE=monitor never drains.
h = Harness(env={"MODE": "monitor"}, link_script=[(60, False)]).run(3600)
results.append(check("5 monitor mode never drains", h.acts(), ["bounce", "bounce"]))

# 6. Flap guard: two trips already on record -> third is refused.
past = [1_000_000 - 100, 1_000_000 - 200]
h = Harness(
    link_script=[(60, False)],
    initial_state={"phase": "healthy", "owns_maintenance": False, "trips": past},
).run(3600)
results.append(check("6 flap guard blocks third trip", h.acts(), ["bounce", "bounce"]))
results.append(check("6 no maintenance call", h.maint, None))

# 7. Drained node recovers: maintenance cleared only after the stable window.
h = Harness(link_script=[(60, False), (900, True)]).run(1400)
results.append(check("7 drains then waits", h.acts(), ["bounce", "bounce", "maintenance enable"]))
h = Harness(link_script=[(60, False), (900, True)]).run(2200)
results.append(
    check(
        "7 clears after stable window",
        h.acts(),
        ["bounce", "bounce", "maintenance enable", "maintenance disable"],
    )
)
results.append(check("7 phase healthy again", h.phase(), "healthy"))

# 8. Inquorate: publish fails -> stand by, never drain.
h = Harness(link_script=[(60, False)])
h.mod.publish = lambda healthy: False
h.run(3600)
results.append(check("8 inquorate does not drain", h.acts(), ["bounce", "bounce"]))

# 9. Restart while drained: state survives and is not re-tripped.
h = Harness(
    link_script=[(0, True)],
    initial_state={"phase": "drained", "owns_maintenance": True, "trips": []},
)
h.run(200)
results.append(check("9 resumes drained, holds", h.acts(), []))
h = Harness(
    link_script=[(0, True)],
    initial_state={"phase": "drained", "owns_maintenance": True, "trips": []},
)
h.run(900)
results.append(check("9 clears after stable window", h.acts(), ["maintenance disable"]))

# 10. --rearm clears the flap counter, keeps the drain bookkeeping, and the next
#     real fault is allowed to drain again. The last assertion is the point: it
#     proves re-arming restores the behaviour test 6 blocks.
past = [1_000_000 - 100, 1_000_000 - 200]
h = Harness(initial_state={"phase": "drained", "owns_maintenance": True, "trips": past})
calls = []
h.mod.run = lambda argv, timeout=30: (calls.append(argv), (0, ""))[1]
rc = h.mod.rearm()
after = h.mod.load_state()
results.append(check("10 rearm exits clean", rc, 0))
results.append(
    check("10 restarts the service", calls, [["systemctl", "restart", "pve-lan-watchdog"]])
)
results.append(check("10 trips cleared", after["trips"], []))
results.append(
    check(
        "10 drain bookkeeping kept",
        (after["phase"], after["owns_maintenance"]),
        ("drained", True),
    )
)

h = Harness(
    link_script=[(60, False)],
    initial_state={"phase": "healthy", "owns_maintenance": False, "trips": []},
).run(3600)
results.append(
    check(
        "10 re-armed guard allows the next drain",
        h.acts(),
        ["bounce", "bounce", "maintenance enable"],
    )
)

# 11. A failed state write must not report success, and must not restart: the
#     daemon would come back still flap-guarded while the operator saw exit 0.
h = Harness(initial_state={"phase": "degraded", "owns_maintenance": False, "trips": past})
calls = []
h.mod.run = lambda argv, timeout=30: (calls.append(argv), (0, ""))[1]
h.mod.save_state = lambda state: False
results.append(check("11 rearm fails loudly", h.mod.rearm(), 1))
results.append(check("11 no restart on failed write", calls, []))

print("\n%d/%d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
