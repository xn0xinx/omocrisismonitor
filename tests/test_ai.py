"""AI sidebar: the change-detection gate (`_due`/`_signature`), the actual
refresh (happy path, runner error, reentrancy), the prompt shape, and the
pluggable backend selection. The real subprocess calls (`ClaudeBackend.run` /
`GeminiBackend.run`) are never exercised here — tests inject a raw `runner=`.
"""
from pathlib import Path

import pytest

from omocrisismonitor import config
from omocrisismonitor.ai import AiService, ClaudeBackend, GeminiBackend


def _cfg(tmp_path, **ai):
    c = config.load(Path(tmp_path) / "none.toml")
    for k, v in ai.items():
        setattr(c.ai, k, v)
    return c


class FakeHub:
    def __init__(self):
        self.events = []

    def emit(self, e):
        self.events.append(e)

    def of(self, t):
        return [e for e in self.events if e["type"] == t]


class FakeAis:
    def __init__(self, count=3):
        self.count = count

    def vessel_count(self):
        return self.count

    def summary(self):
        return {"count": self.count, "by_category": {"cargo": self.count}, "notable": []}


class FakeConflict:
    def __init__(self, counts=None):
        self.counts = counts or {"ukraine": 5}

    def bootstrap(self):
        return {"conflicts": [{"slug": k, "count": v} for k, v in self.counts.items()]}

    async def recent_highlights(self, **kw):
        return {"since_sort": 0, "by_theatre": self.counts,
                "highlights": {"ukraine": ["a drone strike near the port"]}}


class FakeFuel:
    def __init__(self, wti=100.0):
        self.wti = wti

    def snapshot(self):
        return {"crude": {"wti": {"usd": self.wti, "prev_close": self.wti - 1}},
                "retail_updated": 123, "retail_stale": False}


def _svc(tmp_path, runner=None, **cfg_kw):
    hub = FakeHub()
    svc = AiService(
        _cfg(tmp_path, **cfg_kw), hub,
        ais=FakeAis(), conflict=FakeConflict(), fuel=FakeFuel(),
        runner=runner,
    )
    return svc, hub


def _runner(calls=None, text="all quiet.", cost=0.01):
    calls = calls if calls is not None else []

    async def run(prompt):
        calls.append(prompt)
        return text, {"cost_usd": cost}
    return run, calls


# ---- change-detection gate ------------------------------------

async def test_due_true_before_any_summary(tmp_path):
    svc, _ = _svc(tmp_path)
    assert await svc._due() is True


async def test_due_false_when_disabled(tmp_path):
    svc, _ = _svc(tmp_path, enabled=False)
    assert await svc._due() is False


async def test_due_respects_min_interval_after_a_refresh(tmp_path):
    run, _ = _runner()
    svc, _ = _svc(tmp_path, min_interval_s=900, runner=run)
    await svc.refresh()
    assert await svc._due() is False           # just ran, nothing changed either


async def test_due_true_once_signature_changes_and_interval_elapsed(tmp_path):
    run, _ = _runner()
    svc, _ = _svc(tmp_path, min_interval_s=0, runner=run)
    await svc.refresh()
    assert await svc._due() is False            # interval elapsed but nothing changed
    svc.fuel.wti = 250.0                        # a notable move
    assert await svc._due() is True


async def test_signature_reflects_inputs(tmp_path):
    svc, _ = _svc(tmp_path)
    sig1 = await svc._signature()
    svc.ais.count = 99
    sig2 = await svc._signature()
    assert sig1 != sig2


# ---- refresh --------------------------------------------------

async def test_refresh_happy_path_emits_thinking_then_summary(tmp_path):
    run, calls = _runner(text="Ukraine saw 5 events; nothing near shipping lanes.")
    svc, hub = _svc(tmp_path, runner=run)
    out = await svc.refresh(reason="manual")
    assert out["text"] == "Ukraine saw 5 events; nothing near shipping lanes."
    assert out["reason"] == "manual" and out["cost_usd"] == 0.01
    assert hub.of("ai_status")[0]["state"] == "thinking"
    assert hub.of("ai_summary")[-1]["text"] == out["text"]
    assert svc.last_run > 0 and svc._sig is not None
    assert len(calls) == 1


async def test_refresh_error_emits_status_and_keeps_prior_summary(tmp_path):
    run, _ = _runner(text="first summary")
    svc, hub = _svc(tmp_path, min_interval_s=0, runner=run)
    await svc.refresh()

    async def boom(prompt):
        raise RuntimeError("claude CLI timed out")
    svc._runner = boom
    out = await svc.refresh()
    assert out["text"] == "first summary"       # unchanged on failure
    assert hub.of("ai_status")[-1] == {
        "type": "ai_status", "state": "error", "detail": "claude CLI timed out"
    }
    assert svc.busy is False                    # never wedges


async def test_refresh_is_not_reentrant(tmp_path):
    run, calls = _runner()
    svc, _ = _svc(tmp_path, runner=run)
    svc.busy = True
    out = await svc.refresh()
    assert calls == []                          # runner never invoked
    assert out == {}                             # no summary yet


async def test_prompt_carries_conflict_ais_and_fuel_context(tmp_path):
    svc, _ = _svc(tmp_path)
    ctx = await svc._gather()
    prompt = svc._prompt(ctx)
    assert "ukraine" in prompt and "drone strike" in prompt
    assert '"count": 3' in prompt
    assert '"usd": 100.0' in prompt


async def test_gather_passes_current_view_into_conflict_scoping(tmp_path):
    calls = []

    class ScopedConflict(FakeConflict):
        async def recent_highlights(self, **kw):
            calls.append(kw.get("bbox"))
            return await super().recent_highlights(**kw)

    svc, _ = _svc(tmp_path)
    svc.conflict = ScopedConflict()
    box = [[10.0, 20.0], [30.0, 40.0]]

    ctx = await svc._gather()          # no view set yet -> global
    assert calls == [None] and ctx["view"] is None

    svc.current_view = box
    ctx = await svc._gather()
    assert calls == [None, box] and ctx["view"] == box


def test_prompt_names_the_region_when_a_view_is_set():
    box = [[10.0, 20.0], [30.0, 40.0]]
    with_view = AiService._prompt({"conflict": {}, "ais": {}, "fuel": {}, "view": box})
    without_view = AiService._prompt({"conflict": {}, "ais": {}, "fuel": {}, "view": None})
    assert "MAP VIEW" in with_view and str(box) in with_view
    assert "no specific region" in without_view


async def test_refresh_records_the_view_the_summary_was_generated_for(tmp_path):
    run, _ = _runner()
    svc, _ = _svc(tmp_path, runner=run)
    box = [[1.0, 2.0], [3.0, 4.0]]
    svc.current_view = box
    out = await svc.refresh()
    assert out["view"] == box


async def test_start_and_stop_are_clean(tmp_path):
    run, _ = _runner()
    svc, _ = _svc(tmp_path, runner=run)
    await svc.start()
    await svc.stop()


async def test_disabled_service_never_starts_the_loop(tmp_path):
    svc, _ = _svc(tmp_path, enabled=False)
    await svc.start()
    assert svc._task is None
    await svc.stop()


# ---- pluggable backends ------------------------------------------

def test_default_backends_are_claude_and_unverified_gemini(tmp_path):
    svc, _ = _svc(tmp_path)
    listing = {b["key"]: b for b in svc.list_backends()}
    assert listing["claude"]["available"] is True
    assert listing["gemini"]["available"] is False
    assert svc.backend_key == "claude" and svc.backend.key == "claude"


def test_set_backend_switches_and_rejects_unknown(tmp_path):
    svc, _ = _svc(tmp_path)
    svc.set_backend("gemini")
    assert svc.backend_key == "gemini" and svc.backend.key == "gemini"
    with pytest.raises(KeyError):
        svc.set_backend("grok")


def test_unknown_configured_backend_falls_back_to_claude(tmp_path):
    svc, _ = _svc(tmp_path, backend="grok")
    assert svc.backend_key == "claude"


async def test_gemini_backend_refuses_to_run_while_unverified(tmp_path):
    c = config.load(Path(tmp_path) / "none.toml")
    with pytest.raises(RuntimeError, match="unverified"):
        await GeminiBackend(c).run("prompt", "system")


def test_claude_backend_reads_bin_and_model_from_config(tmp_path):
    c = config.load(Path(tmp_path) / "none.toml")
    c.ai.claude_bin = "/usr/local/bin/claude"
    c.ai.model = "claude-opus-5"
    b = ClaudeBackend(c)
    assert b.bin == "/usr/local/bin/claude" and b.model == "claude-opus-5"
