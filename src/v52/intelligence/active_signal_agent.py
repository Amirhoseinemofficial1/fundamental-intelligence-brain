from __future__ import annotations

import json
import time
from typing import Any

from ..config import Settings
from ..storage.sqlite import connect, transaction


class ActiveSignalAgent:
    """Re-evaluates open positions whenever materially adverse evidence arrives."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_dir / "brain" / "intelligence.sqlite3"

    def reevaluate(self, asset_id: str, probability: float, risk: float, exit_probability: float | None, evidence: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT s.id,s.state,s.action,p.direction_probability FROM signals s JOIN predictions p ON p.id=s.prediction_id
                   WHERE p.asset_id=? AND s.state IN ('ACTIVE','UPDATED')""",
                (asset_id,),
            ).fetchall()
            for row in rows:
                long_signal = str(row["action"]) in {"BUY", "ENTER_ON_CONFIRMATION", "ENTER_ON_PULLBACK"}
                reversal = (long_signal and probability <= .38) or (not long_signal and probability >= .62)
                learned_exit = exit_probability is not None and float(exit_probability) >= .62
                if not (reversal or learned_exit or risk >= .72):
                    continue
                reason = "LEARNED_EXIT_POLICY" if learned_exit else "ADVERSE_NEW_EVIDENCE" if reversal else "RISK_BOUNDARY_BREACH"
                now = time.time()
                transaction(con)
                try:
                    if row["state"] == "ACTIVE":
                        con.execute("UPDATE signals SET state='UPDATED',last_updated_at=? WHERE id=?", (now, row["id"]))
                        con.execute("INSERT INTO signal_lifecycle(signal_id,state,reason,market_context_json,created_at) VALUES(?,?,?,?,?)", (row["id"], "UPDATED", reason, json.dumps(evidence, default=str), now))
                    con.execute("UPDATE signals SET state='EXIT_PENDING',close_reason=?,last_updated_at=? WHERE id=?", (reason, now, row["id"]))
                    con.execute("INSERT INTO signal_lifecycle(signal_id,state,reason,market_context_json,created_at) VALUES(?,?,?,?,?)", (row["id"], "EXIT_PENDING", reason, json.dumps(evidence, default=str), now))
                    con.execute("COMMIT")
                except BaseException:
                    con.execute("ROLLBACK")
                    raise
                actions.append({"signal_id": int(row["id"]), "asset_id": asset_id, "action": "EXIT", "reason": reason, "probability": probability, "risk_score": risk})
        return actions
