"""AI sidebar: a short synthesis over the last day of conflict activity, the
ships currently in the user's map view, and the crude/retail fuel move — run
through a CLI on the user's own subscription/login (no API key). One-shot,
tools disabled where the CLI supports it, no session persistence: the sidebar
is a snapshot, not a conversation.

Pluggable backend (`AiBackend`), same shape as fuel.py's `CrudeSource`:
  * ClaudeBackend — `claude -p`, verified working.
  * GeminiBackend — `gemini -p`. **Unverified on this box**: `gemini -p` hung
    indefinitely in testing (no output, timed out at 45s, twice, with no other
    gemini process running) — looks like a stale OAuth token trying to refresh
    rather than failing fast. `GeminiBackend.available` stays False, and
    `run()` refuses with a clear message, until someone confirms `gemini -p
    "hi" -o json --approval-mode plan` returns promptly after a fresh `gemini`
    interactive login — then flip `available = True` here.

Auto-refreshes when something notable changed *and* at least `ai.min_interval_s`
has passed since the last run; always available on-demand via `refresh()`.
Pulls context from the other services (`ais=`, `conflict=`, `fuel=` — each
optional, so this works standalone in tests). `runner=` is a raw test seam that
bypasses backend selection entirely; production code goes through `backends=`.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, Awaitable, Callable, Protocol

Runner = Callable[[str], Awaitable[tuple[str, dict]]]

CLAUDE_DISALLOWED_TOOLS = [
    "Bash", "Read", "Write", "Edit", "WebFetch", "WebSearch",
    "Task", "Glob", "Grep", "NotebookEdit",
]

SYSTEM_PROMPT = (
    "You are the AI sidebar of OmoCrisisMonitor, a themed situational-awareness "
    "console. You are given a JSON snapshot of conflict activity, shipping in "
    "the user's current map view, and crude/retail fuel prices. Write a short, "
    "dense, non-alarmist synthesis: plain text, no markdown, no headers, at "
    "most 180 words. Cover, in this order: notable conflict developments in "
    "the last day by theatre; whether any are near shipping lanes or "
    "chokepoints given the ships currently in view; the crude and retail fuel "
    "move. If a section has no data, say so in one clause and move on. Do not "
    "editorialise beyond the given data."
)


async def _communicate(proc: asyncio.subprocess.Process, timeout: float) -> tuple[bytes, bytes]:
    try:
        return await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        raise RuntimeError("AI CLI timed out") from None


class AiBackend(Protocol):
    key: str
    label: str
    available: bool
    async def run(self, prompt: str, system: str) -> tuple[str, dict]: ...


class ClaudeBackend:
    key = "claude"
    label = "Claude"
    available = True

    def __init__(self, cfg: Any) -> None:
        self.bin = cfg.ai.claude_bin
        self.model = cfg.ai.model

    async def run(self, prompt: str, system: str) -> tuple[str, dict]:  # pragma: no cover - needs `claude`
        proc = await asyncio.create_subprocess_exec(
            self.bin, "-p", prompt,
            "--model", self.model,
            "--system-prompt", system,
            "--output-format", "json",
            "--no-session-persistence",
            "--disallowedTools", *CLAUDE_DISALLOWED_TOOLS,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await _communicate(proc, 120)
        if proc.returncode != 0:
            raise RuntimeError((err.decode(errors="replace") or "claude CLI failed")[:400])
        try:
            data = json.loads(out.decode())
        except json.JSONDecodeError:
            raise RuntimeError("claude CLI returned non-JSON output") from None
        if data.get("is_error"):
            raise RuntimeError(str(data.get("result") or "claude reported an error"))
        return data.get("result", ""), {"cost_usd": data.get("total_cost_usd")}


class GeminiBackend:
    key = "gemini"
    label = "Gemini"
    # See the module docstring — this stays False until someone confirms
    # `gemini -p` actually returns on this machine.
    available = False

    def __init__(self, cfg: Any) -> None:
        self.bin = cfg.ai.gemini_bin
        self.model = cfg.ai.gemini_model or ""

    async def run(self, prompt: str, system: str) -> tuple[str, dict]:  # pragma: no cover - needs `gemini`
        if not self.available:
            raise RuntimeError(
                "Gemini backend is unverified on this machine (gemini -p hung "
                "in testing). Run `gemini` interactively once to refresh its "
                "login, confirm `gemini -p \"hi\" -o json --approval-mode "
                "plan` returns promptly, then set GeminiBackend.available = "
                "True in ai.py."
            )
        # gemini CLI has no --system-prompt flag (as of 0.59.0) — fold it in.
        args = [self.bin, "-p", f"{system}\n\n{prompt}",
                "-o", "json", "--approval-mode", "plan"]
        if self.model:
            args += ["-m", self.model]
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        out, err = await _communicate(proc, 120)
        if proc.returncode != 0:
            raise RuntimeError((err.decode(errors="replace") or "gemini CLI failed")[:400])
        try:
            data = json.loads(out.decode())
        except json.JSONDecodeError:
            raise RuntimeError("gemini CLI returned non-JSON output") from None
        # response field name unconfirmed — try the likely candidates rather
        # than fail outright once `available` is finally flipped on.
        text = data.get("response") or data.get("text") or data.get("result") or ""
        if not text:
            raise RuntimeError(f"unrecognised gemini CLI output shape: {sorted(data)}")
        return text, {"cost_usd": None}


class AiService:
    def __init__(
        self,
        cfg: Any,
        hub: Any,
        *,
        ais: Any = None,
        conflict: Any = None,
        fuel: Any = None,
        runner: Runner | None = None,
        backends: dict[str, AiBackend] | None = None,
    ) -> None:
        self.cfg = cfg
        self.hub = hub
        self.ais = ais
        self.conflict = conflict
        self.fuel = fuel
        self._runner = runner          # test seam: bypasses backend selection entirely
        self.backends: dict[str, AiBackend] = backends or {
            "claude": ClaudeBackend(cfg),
            "gemini": GeminiBackend(cfg),
        }
        self.backend_key = cfg.ai.backend if cfg.ai.backend in self.backends else "claude"
        self.summary: dict | None = None
        self.busy = False
        self.last_run = 0.0
        self._sig: str | None = None
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    @property
    def backend(self) -> AiBackend:
        return self.backends[self.backend_key]

    def list_backends(self) -> list[dict]:
        return [
            {"key": b.key, "label": b.label, "available": b.available}
            for b in self.backends.values()
        ]

    def set_backend(self, key: str) -> None:
        if key not in self.backends:
            raise KeyError(key)
        self.backend_key = key

    # ---- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        if self.cfg.ai.enabled and self.cfg.ai.auto_refresh:
            self._task = asyncio.create_task(self._loop(), name="ai-loop")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _loop(self) -> None:
        # check every couple of minutes; min_interval_s + the signature gate
        # keep actual CLI invocations rare
        tick = max(60, min(180, self.cfg.ai.min_interval_s // 3 or 60))
        while not self._stopped.is_set():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=tick)
            if self._stopped.is_set():
                break
            if await self._due():
                await self.refresh(reason="auto")

    async def _due(self) -> bool:
        if self.busy or not self.cfg.ai.enabled:
            return False
        if time.time() - self.last_run < self.cfg.ai.min_interval_s:
            return False
        if self.summary is None:
            return True
        return await self._signature() != self._sig

    async def _signature(self) -> str:
        """Cheap fingerprint of "has anything notable changed" — no CLI call."""
        events = 0
        if self.conflict is not None:
            c = self.conflict.bootstrap()
            events = sum(x.get("count", 0) for x in c.get("conflicts", []))
        wti = 0.0
        if self.fuel is not None:
            crude = self.fuel.snapshot().get("crude", {})
            wti = round((crude.get("wti") or {}).get("usd") or 0.0, 1)
        ships = self.ais.vessel_count() if self.ais is not None else 0
        return f"{events}:{wti}:{ships}"

    # ---- the actual run --------------------------------------------
    async def refresh(self, reason: str = "manual") -> dict:
        if self.busy:
            return self.summary or {}
        self.busy = True
        self.hub.emit({"type": "ai_status", "state": "thinking", "reason": reason})
        try:
            ctx = await self._gather()
            if self._runner is not None:
                text, meta = await self._runner(self._prompt(ctx))
                backend_key, model = "test", self.cfg.ai.model
            else:
                text, meta = await self.backend.run(self._prompt(ctx), SYSTEM_PROMPT)
                backend_key, model = self.backend_key, getattr(self.backend, "model", "") or self.backend.label
            self.summary = {
                "text": text.strip(), "ts": time.time(), "model": model,
                "backend": backend_key, "reason": reason,
                "cost_usd": meta.get("cost_usd"),
            }
            self._sig = await self._signature()
            self.last_run = time.time()
            self.hub.emit({"type": "ai_summary", **self.summary})
            return self.summary
        except Exception as e:  # noqa: BLE001 - surface, never crash the app
            self.hub.emit({"type": "ai_status", "state": "error", "detail": str(e)})
            return self.summary or {}
        finally:
            self.busy = False

    async def _gather(self) -> dict:
        conflict_ctx = (
            await self.conflict.recent_highlights() if self.conflict is not None else {}
        )
        ais_ctx = self.ais.summary() if self.ais is not None else {}
        fuel_ctx = self.fuel.snapshot() if self.fuel is not None else {}
        return {"conflict": conflict_ctx, "ais": ais_ctx, "fuel": fuel_ctx}

    @staticmethod
    def _prompt(ctx: dict) -> str:
        return (
            "CONFLICT (last-day event counts by theatre, plus a few recent "
            f"descriptions for the busiest ones):\n{json.dumps(ctx['conflict'])}\n\n"
            f"SHIPPING (vessels in the user's current map view):\n{json.dumps(ctx['ais'])}\n\n"
            f"FUEL (latest crude + retail freshness):\n{json.dumps(ctx['fuel'])}"
        )
