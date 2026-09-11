"""Watch-rule alerts (Phase 5): three rule kinds, evaluated on a poll loop
against data the other services already hold, firing a desktop notification
(`notify-send`) + a WS `alert_hit` event + a DB row on a state *transition*
(edge-triggered — a rule doesn't re-fire every poll while still tripped).

Kinds:
  * conflict_region  — new conflict_event rows since the last check, filtered
    by theatre and/or bbox. Watermarked by first_seen (Phase 2's event store),
    so a rule created today never replays history.
  * ship_box         — vessel count inside a bbox (optionally by AIS category)
    crosses min_count. Reads AisService.vessels directly — viewport-scoped, so
    a box outside every viewport the user has opened always reads 0 (the
    SPEC's "regions the user has had open" limitation, same as AI/correlate).
  * fuel_threshold   — WTI/Brent crosses above/below a value.

Rules live in `alert_rule` / hits in `alert_hit` (Phase 0 schema, unchanged).
The notifier is injectable (`notifier=`) for tests.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from typing import Any, Awaitable, Callable

Notifier = Callable[[str, str], Awaitable[None]]

KINDS = ("conflict_region", "ship_box", "fuel_threshold")


def validate_params(kind: str, params: dict) -> dict:
    """Normalise + sanity-check a rule's params for its kind. Raises ValueError."""
    params = params or {}
    if kind == "conflict_region":
        conflict = params.get("conflict") or None
        bbox = params.get("bbox") or None
        if not conflict and not bbox:
            raise ValueError("conflict_region needs a conflict theatre and/or a bbox")
        return {"conflict": conflict, "bbox": bbox,
                "min_new": max(1, int(params.get("min_new", 1)))}
    if kind == "ship_box":
        bbox = params.get("bbox")
        if not bbox:
            raise ValueError("ship_box needs a bbox")
        return {"bbox": bbox, "category": params.get("category") or "any",
                "min_count": max(1, int(params.get("min_count", 1)))}
    if kind == "fuel_threshold":
        symbol, op, value = params.get("symbol"), params.get("op"), params.get("value")
        if symbol not in ("wti", "brent") or op not in (">", "<") or value is None:
            raise ValueError("fuel_threshold needs symbol (wti|brent), op (>|<), value")
        return {"symbol": symbol, "op": op, "value": float(value)}
    raise ValueError(f"unknown rule kind {kind!r}")


class AlertService:
    def __init__(
        self,
        cfg: Any,
        hub: Any,
        con: Any,
        *,
        ais: Any = None,
        conflict: Any = None,
        fuel: Any = None,
        notifier: Notifier | None = None,
    ) -> None:
        self.cfg = cfg
        self.hub = hub
        self.con = con
        self.ais = ais
        self.conflict = conflict
        self.fuel = fuel
        self._notify = notifier or self._default_notify
        self._watermarks: dict[int, int] = {}   # conflict_region rule_id -> unix ts
        self._armed: dict[int, bool] = {}        # ship_box/fuel_threshold rule_id -> currently tripped
        self._task: asyncio.Task | None = None
        self._stopped = asyncio.Event()

    # ---- lifecycle -----------------------------------------------------
    async def start(self) -> None:
        if self.cfg.alerts.enabled:
            self._task = asyncio.create_task(self._loop(), name="alerts-loop")

    async def stop(self) -> None:
        self._stopped.set()
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._task

    async def _loop(self) -> None:
        gap = max(10, self.cfg.alerts.poll_s)
        while not self._stopped.is_set():
            await self.evaluate_all()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._stopped.wait(), timeout=gap)

    async def evaluate_all(self) -> None:
        for rule in self.list_rules(enabled_only=True):
            try:
                await self._evaluate(rule)
            except Exception as e:  # noqa: BLE001 - one bad rule must not sink the rest
                self.hub.emit({"type": "alert_status", "state": "error",
                               "rule_id": rule["id"], "detail": str(e)})

    async def _evaluate(self, rule: dict) -> None:
        params = rule["params"]
        hit = None
        if rule["kind"] == "conflict_region":
            hit = self._eval_conflict_region(rule, params)
        elif rule["kind"] == "ship_box":
            hit = self._eval_ship_box(rule, params)
        elif rule["kind"] == "fuel_threshold":
            hit = self._eval_fuel_threshold(rule, params)
        if hit:
            await self._fire(rule, *hit)

    def _eval_conflict_region(self, rule: dict, params: dict) -> tuple[str, dict] | None:
        if self.con is None:
            return None
        rid = rule["id"]
        now = int(time.time())
        watermark = self._watermarks.get(rid)
        if watermark is None:
            self._watermarks[rid] = now      # first eval: arm from now, never replay backlog
            return None
        q = "SELECT COUNT(*) c FROM conflict_event WHERE first_seen > ?"
        args: list = [watermark]
        if params.get("conflict"):
            q += " AND conflict = ?"
            args.append(params["conflict"])
        if params.get("bbox"):
            (s, w), (n, e) = params["bbox"]
            q += " AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?"
            args += [s, n, w, e]
        row = self.con.execute(q, args).fetchone()
        count = row["c"] if row else 0
        self._watermarks[rid] = now
        if count >= params["min_new"]:
            where = params.get("conflict") or "the watched box"
            plural = "s" if count != 1 else ""
            return f"{count} new conflict event{plural} in {where}", {"count": count}
        return None

    def _eval_ship_box(self, rule: dict, params: dict) -> tuple[str, dict] | None:
        if self.ais is None:
            return None
        rid = rule["id"]
        count = self.ais.count_in_box(params["bbox"], params.get("category"))
        over = count >= params["min_count"]
        was_armed = self._armed.get(rid, False)
        self._armed[rid] = over
        if over and not was_armed:
            cat = params.get("category") or "any"
            plural = "s" if count != 1 else ""
            return f"{count} {cat} vessel{plural} in the watched box", {"count": count}
        return None

    def _eval_fuel_threshold(self, rule: dict, params: dict) -> tuple[str, dict] | None:
        if self.fuel is None:
            return None
        rid = rule["id"]
        usd = (self.fuel.snapshot().get("crude", {}).get(params["symbol"]) or {}).get("usd")
        if usd is None:
            return None
        over = usd > params["value"] if params["op"] == ">" else usd < params["value"]
        was_armed = self._armed.get(rid, False)
        self._armed[rid] = over
        if over and not was_armed:
            return (f"{params['symbol'].upper()} ${usd:.2f} {params['op']} ${params['value']:.2f}",
                    {"usd": usd})
        return None

    async def _fire(self, rule: dict, summary: str, payload: dict) -> None:
        now = int(time.time())
        if self.con is not None:
            self.con.execute(
                "INSERT INTO alert_hit(rule_id,ts,summary,payload) VALUES (?,?,?,?)",
                (rule["id"], now, summary, json.dumps(payload)),
            )
            self.con.commit()
        self.hub.emit({
            "type": "alert_hit", "rule_id": rule["id"], "label": rule["label"],
            "kind": rule["kind"], "summary": summary, "ts": now,
        })
        if self.cfg.alerts.notify:
            await self._notify(rule["label"] or rule["kind"], summary)

    async def _default_notify(self, title: str, body: str) -> None:  # pragma: no cover - needs notify-send
        try:
            proc = await asyncio.create_subprocess_exec(
                "notify-send", "--app-name=OmoCrisisMonitor", "--icon=dialog-warning", title, body,
            )
            await asyncio.wait_for(proc.wait(), timeout=10)
        except (FileNotFoundError, asyncio.TimeoutError):
            pass    # no libnotify, or it hung — a missed desktop popup isn't fatal

    # ---- rule CRUD (backs the /api/alerts/* routes) --------------------
    def create_rule(self, kind: str, label: str, params: dict, enabled: bool = True) -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown rule kind {kind!r}")
        clean = validate_params(kind, params)
        now = int(time.time())
        cur = self.con.execute(
            "INSERT INTO alert_rule(kind,label,params,enabled,created) VALUES (?,?,?,?,?)",
            (kind, label or "", json.dumps(clean), 1 if enabled else 0, now),
        )
        self.con.commit()
        return self.get_rule(cur.lastrowid)

    def get_rule(self, rule_id: int) -> dict | None:
        row = self.con.execute("SELECT * FROM alert_rule WHERE id=?", (rule_id,)).fetchone()
        return self._row(row) if row else None

    def list_rules(self, enabled_only: bool = False) -> list[dict]:
        q = "SELECT * FROM alert_rule" + (" WHERE enabled=1" if enabled_only else "")
        return [self._row(r) for r in self.con.execute(q + " ORDER BY created DESC")]

    def set_enabled(self, rule_id: int, enabled: bool) -> bool:
        cur = self.con.execute(
            "UPDATE alert_rule SET enabled=? WHERE id=?", (1 if enabled else 0, rule_id)
        )
        self.con.commit()
        if not enabled:
            self._armed.pop(rule_id, None)
        return cur.rowcount > 0

    def delete_rule(self, rule_id: int) -> bool:
        cur = self.con.execute("DELETE FROM alert_rule WHERE id=?", (rule_id,))
        self.con.commit()
        self._armed.pop(rule_id, None)
        self._watermarks.pop(rule_id, None)
        return cur.rowcount > 0

    def recent_hits(self, limit: int = 50) -> list[dict]:
        rows = self.con.execute(
            "SELECT h.id,h.rule_id,h.ts,h.summary,h.payload,r.label,r.kind "
            "FROM alert_hit h JOIN alert_rule r ON r.id = h.rule_id "
            "ORDER BY h.ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d["payload"]) if d["payload"] else {}
            out.append(d)
        return out

    @staticmethod
    def _row(row: Any) -> dict:
        d = dict(row)
        d["params"] = json.loads(d["params"]) if d["params"] else {}
        d["enabled"] = bool(d["enabled"])
        return d
