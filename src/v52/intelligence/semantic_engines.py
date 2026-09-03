from __future__ import annotations

import math
import statistics
from typing import Any

class SurpriseEngine:
    @staticmethod
    def evaluate(payload:dict[str,Any],history:list[float]|None=None)->dict[str,Any]:
        def num(name:str)->float|None:
            try:return float(payload[name]) if payload.get(name) is not None else None
            except (TypeError,ValueError):return None
        actual=num('actual');forecast=num('forecast');consensus=num('consensus');previous=num('previous');revision=num('revision') or 0.0
        reference=consensus if consensus is not None else forecast if forecast is not None else previous
        raw=(actual-reference+revision) if actual is not None and reference is not None else None
        distribution=[float(x) for x in (history or []) if math.isfinite(float(x))]
        scale=statistics.pstdev(distribution) if len(distribution)>1 else None
        standardized=raw/max(scale,1e-9) if raw is not None and scale is not None else None
        return {'actual':actual,'forecast':forecast,'consensus':consensus,'previous':previous,'revision':revision,'historical_distribution':distribution,'raw_surprise':raw,'standardized_surprise':standardized,'valid':raw is not None}

class PricedInEngine:
    @staticmethod
    def evaluate(payload:dict[str,Any],analogues:list[dict[str,Any]]|None=None)->dict[str,Any]:
        components={'pre_event_price_move':abs(float(payload.get('pre_event_price_move') or 0)),'prior_related_stories':min(1.0,float(payload.get('prior_related_stories') or 0)/5),'rumors':float(bool(payload.get('rumors'))),'consensus':float(bool(payload.get('consensus') is not None)),'positioning':abs(float(payload.get('positioning_zscore') or 0))/3,'anticipation':float(payload.get('anticipation_score') or 0),'historical_analogue':max((float(x.get('priced_in_score') or 0) for x in (analogues or [])),default=0.0)}
        score=max(0.0,min(1.0,sum(min(1.0,value) for value in components.values())/len(components)))
        return {'score':score,'components':components,'valid_components':sum(value>0 for value in components.values())}
