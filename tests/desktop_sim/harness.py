"""Scenario registry, per-scenario context and the shared fixtures."""
from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field
from typing import Callable, Optional

from .app import DesktopApp, Stack

REGISTRY: list = []


@dataclass
class Scenario:
    name: str
    tier: str            # live | sandbox
    fn: Callable
    doc: str


def scenario(name: str, tier: str):
    def deco(fn):
        REGISTRY.append(Scenario(name, tier, fn, (fn.__doc__ or "").strip()))
        return fn
    return deco


@dataclass
class Check:
    name: str
    ok: bool
    detail: str = ""


@dataclass
class Outcome:
    scenario: str
    tier: str
    checks: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    error: Optional[str] = None
    elapsed_s: float = 0.0

    @property
    def passed(self) -> bool:
        return self.error is None and all(c.ok for c in self.checks)


class Env:
    """What every scenario shares: the stack, the tier's csai, the stubs, and
    fixtures built once per run."""

    def __init__(self, stack: Stack, *, verbose: bool = True):
        self.stack = stack
        self.verbose = verbose
        self.sandbox = None          # sandbox.Sandbox
        self.model = None            # stub_model.StubModel
        self.mcp = None              # stub_mcp.StubMcp
        self.log_dir = None
        self.user2: Optional[Stack] = None
        self._fixtures: dict = {}
        self._teardown: list = []

    def fixture(self, name: str, build: Callable[[], object]):
        if name not in self._fixtures:
            self._fixtures[name] = build()
        return self._fixtures[name]

    def at_exit(self, fn: Callable[[], None]) -> None:
        self._teardown.append(fn)

    def close(self) -> None:
        for fn in reversed(self._teardown):
            try:
                fn()
            except Exception:
                traceback.print_exc()
        self._teardown.clear()


class Ctx:
    def __init__(self, env: Env, outcome: Outcome, csai: str):
        self.env, self.out, self.csai = env, outcome, csai
        self.stack = env.stack
        self._defer: list = []

    # --- assertions
    def check(self, name: str, ok, detail="") -> bool:
        ok = bool(ok)
        self.out.checks.append(Check(name, ok, "" if ok else str(detail)[:600]))
        if self.env.verbose:
            print(f"    {'PASS' if ok else 'FAIL'}: {name}" + ("" if ok else f"  {str(detail)[:300]}"))
        return ok

    def note(self, text: str) -> None:
        """A measurement: reported, not asserted."""
        self.out.notes.append(text)
        if self.env.verbose:
            print(f"    note: {text}")

    def defer(self, fn: Callable[[], None]) -> None:
        self._defer.append(fn)

    def cleanup(self) -> None:
        for fn in reversed(self._defer):
            try:
                fn()
            except Exception:
                pass

    # --- a desktop application with its own fresh key
    def app(self, label: str, *, scopes=("api",), sign_in=True, **kw) -> DesktopApp:
        key, secret = self.stack.mint_key(label, scopes)
        self.defer(lambda: self.stack.revoke_key(key))
        a = DesktopApp(self.stack, key, secret, name=label, csai=kw.pop("csai", self.csai), **kw)
        if sign_in:
            r = a.sign_in()
            if r.status_code != 200:
                raise RuntimeError(f"key sign-in refused: {r.status_code} {r.text[:200]}")
        return a

    def wait(self, what: str, pred: Callable[[], object], timeout: float = 20,
             every: float = 0.5):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = pred()
            if v:
                return v
            time.sleep(every)
        return None


def run_one(env: Env, sc: Scenario, csai: str) -> Outcome:
    out = Outcome(sc.name, sc.tier)
    ctx = Ctx(env, out, csai)
    t0 = time.time()
    if env.verbose:
        print(f"[{sc.tier}] {sc.name}")
    try:
        sc.fn(ctx)
    except Exception as e:
        out.error = f"{type(e).__name__}: {e}"
        if env.verbose:
            print("    ERROR: " + out.error)
            traceback.print_exc()
    finally:
        ctx.cleanup()
        out.elapsed_s = time.time() - t0
    return out
