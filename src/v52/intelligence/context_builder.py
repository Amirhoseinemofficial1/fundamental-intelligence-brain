from __future__ import annotations

import json
import time
from typing import Any

from ..config import Settings
from ..storage.sqlite import connect


class RuntimeSemanticContextBuilder:
    """Build point-in-time surprise/priced-in context without look-ahead.

    ``event_anchor`` is the latest timestamp that may be considered "pre-event".
    ``decision_cutoff`` is the latest timestamp whose data was actually available to
    the runtime decision.  Every query is bounded by both as appropriate.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_dir / "brain" / "intelligence.sqlite3"

    def build(
        self,
        event_type: str,
        asset_ids: list[str],
        payload: dict[str, Any],
        analogues: list[dict[str, Any]],
        *,
        event_anchor: float | None = None,
        decision_cutoff: float | None = None,
    ) -> dict[str, Any]:
        built_at = time.time()
        cutoff = min(float(decision_cutoff or built_at), built_at)
        anchor = min(float(event_anchor or cutoff), cutoff)
        context = dict(payload)
        provenance: list[dict[str, Any]] = []
        def _safe_epoch(value:Any)->float|None:
            if isinstance(value,(int,float)):
                n=float(value);return n/1000.0 if n>1e12 else n
            if isinstance(value,str):
                from ..timeutil import parse_datetime
                parsed=parse_datetime(value);return parsed.timestamp() if parsed else None
            return None
        # Never accept a caller-labelled "pre-event" value unless its own
        # availability timestamp proves it existed before the event anchor.
        supplied_price_cutoff=_safe_epoch(context.get('pre_event_price_available_at') or context.get('pre_event_price_cutoff'))
        if context.get('pre_event_price_move') is not None and (supplied_price_cutoff is None or supplied_price_cutoff>anchor):
            context.pop('pre_event_price_move',None)
            provenance.append({'field':'pre_event_price_move','source':'caller','rejected':True,'reason':'missing_or_post_event_availability'})
        supplied_positioning_cutoff=_safe_epoch(context.get('positioning_available_at'))
        if context.get('positioning_zscore') is not None and (supplied_positioning_cutoff is None or supplied_positioning_cutoff>anchor):
            context.pop('positioning_zscore',None)
            provenance.append({'field':'positioning_zscore','source':'caller','rejected':True,'reason':'missing_or_post_event_availability'})
        unit = str(context.get("surprise_unit") or context.get("unit") or "native")
        family = str(context.get("surprise_family") or event_type or "GENERAL")
        with connect(self.path, self.settings) as con:
            event_columns = {str(r["name"]) for r in con.execute("PRAGMA table_info(events)").fetchall()}
            if {"raw_surprise", "surprise_unit", "surprise_family"} <= event_columns:
                history_rows = con.execute(
                    """SELECT raw_surprise FROM events
                       WHERE event_type=? AND raw_surprise IS NOT NULL
                         AND surprise_unit=? AND surprise_family=? AND created_at<?
                       ORDER BY created_at DESC LIMIT 200""",
                    (event_type, unit, family, anchor),
                ).fetchall()
                history = [float(row["raw_surprise"]) for row in history_rows]
                history_source = "events.raw_surprise"
            else:
                # Backward-compatible fallback for pre-V57 databases. This path is
                # intentionally restricted to rows older than the event anchor.
                history = [float(row["surprise"]) for row in con.execute(
                    "SELECT surprise FROM events WHERE event_type=? AND surprise IS NOT NULL AND created_at<? ORDER BY created_at DESC LIMIT 200",
                    (event_type, anchor),
                ).fetchall()]
                history_source = "events.legacy_surprise"
            context["historical_surprise_distribution"] = history
            context["surprise_unit"] = unit
            context["surprise_family"] = family
            provenance.append({"field": "historical_surprise_distribution", "source": history_source, "samples": len(history), "cutoff": anchor, "unit": unit, "family": family})

            # Only analogues already available before the event anchor are supplied by
            # the temporal analogue engine. Keep the caller count as a deterministic
            # summary of that point-in-time set.
            context["prior_related_stories"] = len(analogues)
            context["anticipation_score"] = max((float(item.get("score") or 0.0) for item in analogues), default=0.0)
            rumor_count = int(con.execute(
                """SELECT COUNT(*) count FROM events
                   WHERE event_type=? AND created_at>? AND created_at<?
                     AND (LOWER(title) LIKE '%rumor%' OR LOWER(summary) LIKE '%rumor%')""",
                (event_type, max(0.0, anchor - 30 * 86400), anchor),
            ).fetchone()["count"])
            context["rumors"] = rumor_count > 0
            provenance.append({"field": "rumor_history", "source": "events", "count": rumor_count, "cutoff": anchor})

            if asset_ids and context.get("pre_event_price_move") is None:
                # Critical anti-leakage contract: BOTH observed and available time must
                # be <= the event anchor. A post-event print can never become a
                # pre-event feature merely because semantic processing happened later.
                prices = con.execute(
                    """SELECT observed_at,available_at,price FROM reference_prices
                       WHERE asset_id=? AND observed_at<=? AND available_at<=?
                       ORDER BY observed_at DESC LIMIT 2""",
                    (asset_ids[0], anchor, anchor),
                ).fetchall()
                if len(prices) == 2 and float(prices[1]["price"]):
                    context["pre_event_price_move"] = float(prices[0]["price"]) / float(prices[1]["price"]) - 1.0
                    provenance.append({"field": "pre_event_price_move", "source": "reference_prices", "asset_id": asset_ids[0], "timestamps": [float(x["observed_at"]) for x in prices], "available_times": [float(x["available_at"]) for x in prices], "event_anchor": anchor})

            positioning = con.execute(
                """SELECT observation_type,value,source_id,available_time,event_time FROM canonical_observations
                   WHERE available_time<=? AND event_time<=?
                     AND observation_type IN ('LONG_SHORT_RATIO','FUNDING_ZSCORE','POSITIONING_ZSCORE')
                   ORDER BY available_time DESC LIMIT 1""",
                (anchor, anchor),
            ).fetchone()
            if positioning and context.get("positioning_zscore") is None and positioning["value"] is not None:
                context["positioning_zscore"] = float(positioning["value"])
                provenance.append({"field": "positioning_zscore", "source": str(positioning["source_id"]), "available_at": float(positioning["available_time"]), "event_time": float(positioning["event_time"]), "event_anchor": anchor})

        context["context_provenance"] = provenance
        context["event_anchor"] = anchor
        context["decision_cutoff"] = cutoff
        context["built_at"] = built_at
        return context
