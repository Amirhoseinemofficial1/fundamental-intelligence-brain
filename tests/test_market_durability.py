from __future__ import annotations

import asyncio
from v52.metrics import Metrics
from v52.market.writer import RawWireJournal, SemanticWriter
from v52.models import MarketEvent
from v52.timeutil import iso_now


def test_raw_and_semantic_exact_shutdown_invariants(settings) -> None:
    async def run():
        metrics=Metrics('market_test',settings.state_dir)
        raw=RawWireJournal(settings,metrics,'market_test')
        sem=SemanticWriter(settings,metrics,'market_test')
        await raw.start(); await sem.start()
        for i in range(100):
            await raw.append('TEST','trade','BTCUSDT',f'{{"i":{i}}}')
            await sem.append(MarketEvent('trade','TEST','BTCUSDT',None,iso_now(),'FLOW',{'i':i}))
        await sem.stop(); await raw.stop()
        snap=metrics.snapshot()
        c=snap['counters']; g=snap['gauges']
        assert c['market_raw_enqueued']==100
        assert c['market_raw_written']==100
        assert g['market_raw_synced']==100
        assert g['market_raw_invariant_ok']==1
        assert c['market_semantic_enqueued']==100
        assert c['market_semantic_persisted']==100
        assert g['market_semantic_invariant_ok']==1
        assert g['market_raw_qsize']==0
        assert g['market_semantic_qsize']==0
    asyncio.run(run())
