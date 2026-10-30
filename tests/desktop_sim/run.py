"""Run the desktop-application scenarios against the dev stack.

    python -m tests.desktop_sim.run [--tier live|sandbox|all] [--only TEXT ...]
                                    [--list] [--report FILE]

Normally run through ``tests/test_e2e_desktop.sh``, which signs the fixture user
in (2FA and all) and exports USER_TOKEN. Environment: BASE (REST door), IDM
(ldap_manager), CSAI (dev csai), FE_TENANT, USER_TOKEN; optionally USER2_TOKEN
for a second user (cross-user isolation), DESKTOP_SIM_OUT for logs/report.

Exit status 0 only when every selected scenario passed."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
import time
from pathlib import Path

from . import scenarios_live, scenarios_sandbox  # noqa: F401  (register)
from .app import Stack
from .harness import REGISTRY, Env, run_one


def _select(tier: str, only: list) -> list:
    out = [s for s in REGISTRY if tier == "all" or s.tier == tier]
    if only:
        out = [s for s in out if any(o.lower() in s.name.lower() for o in only)]
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tier", default="all", choices=["live", "sandbox", "all"])
    ap.add_argument("--only", nargs="*", default=[], help="substring(s) of scenario names")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--report", default="")
    args = ap.parse_args(argv)
    for noisy in ("httpx", "mcp", "uvicorn"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    chosen = _select(args.tier, args.only)
    if args.list:
        for s in chosen:
            print(f"[{s.tier:7}] {s.name}")
        return 0
    if not chosen:
        print("no scenarios selected")
        return 1

    out_dir = Path(os.environ.get("DESKTOP_SIM_OUT") or tempfile.mkdtemp(prefix="desktop-sim-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    stack = Stack(rest=os.environ.get("BASE", "http://localhost:8090"),
                  idm=os.environ.get("IDM", "http://localhost:8093"),
                  csai=os.environ.get("CSAI", "http://localhost:8092"),
                  tenant=os.environ.get("FE_TENANT", "default"),
                  user_token=os.environ["USER_TOKEN"])
    env = Env(stack)
    env.log_dir = out_dir
    if os.environ.get("USER2_TOKEN"):
        env.user2 = Stack(rest=stack.rest, idm=stack.idm, csai=stack.csai, tenant=stack.tenant,
                          user_token=os.environ["USER2_TOKEN"])

    outcomes = []
    t0 = time.time()
    try:
        live = [s for s in chosen if s.tier == "live"]
        for sc in live:
            outcomes.append(run_one(env, sc, stack.csai))
        sandbox = [s for s in chosen if s.tier == "sandbox"]
        if sandbox:
            from .sandbox import Sandbox
            from .stub_mcp import StubMcp
            from .stub_model import StubModel
            env.model = StubModel().start()
            env.at_exit(env.model.stop)
            env.mcp = StubMcp().start()
            env.at_exit(env.mcp.stop)
            env.sandbox = Sandbox(env.model.url, log_dir=out_dir).start()
            env.at_exit(env.sandbox.stop)
            print(f"sandbox csai {env.sandbox.url}  model {env.model.url}  mcp {env.mcp.url}")
            for sc in sandbox:
                outcomes.append(run_one(env, sc, env.sandbox.url))
    finally:
        env.close()

    passed = sum(1 for o in outcomes if o.passed)
    checks = sum(len(o.checks) for o in outcomes)
    ok_checks = sum(1 for o in outcomes for c in o.checks if c.ok)
    print()
    print(f"desktop sim: {passed}/{len(outcomes)} scenarios passed, "
          f"{ok_checks}/{checks} checks, {time.time() - t0:.0f}s")
    for o in outcomes:
        if not o.passed:
            print(f"  FAILED [{o.tier}] {o.scenario}" + (f": {o.error}" if o.error else ""))
            for c in o.checks:
                if not c.ok:
                    print(f"      - {c.name}: {c.detail[:240]}")
    report = Path(args.report) if args.report else out_dir / "report.json"
    report.write_text(json.dumps([{
        "scenario": o.scenario, "tier": o.tier, "passed": o.passed, "error": o.error,
        "elapsed_s": round(o.elapsed_s, 1), "notes": o.notes,
        "checks": [{"name": c.name, "ok": c.ok, "detail": c.detail} for c in o.checks]}
        for o in outcomes], indent=1))
    print(f"report: {report}")
    return 0 if passed == len(outcomes) else 1


if __name__ == "__main__":
    sys.exit(main())
