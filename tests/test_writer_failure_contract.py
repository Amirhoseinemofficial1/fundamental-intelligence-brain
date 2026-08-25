from __future__ import annotations

import asyncio
import pytest
from v52.metrics import Metrics
from v52.market.writer import RawWireJournal


def test_raw_writer_failure_becomes_fatal(settings, monkeypatch) -> None:
    async def run():
        metrics=Metrics('market_fail',settings.state_dir)
        raw=RawWireJournal(settings,metrics,'market_fail')
        def boom(batch, force_sync=False):
            raise OSError('disk failed')
        monkeypatch.setattr(raw,'_write_batch_sync',boom)
        await raw.start()
        await raw.append('X','wire','BTC','{}')
        await asyncio.sleep(0.05)
        assert raw.task and raw.task.done()
        with pytest.raises(RuntimeError):
            await raw.append('X','wire','BTC','{}')
    asyncio.run(run())
