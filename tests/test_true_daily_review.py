from __future__ import annotations
import asyncio
from v52.intelligence.research import ResearchService
from v52.metrics import Metrics

def test_daily_report_contains_research_not_only_counters(settings):
    async def run():
        report=await ResearchService(settings,Metrics('daily-review-test',settings.state_dir)).daily_report()
        required={'performance','per_expert','per_asset','per_horizon','per_regime','entry_quality','exit_quality','missed_opportunities','failure_reasons','hypotheses','model_governance','before_after_improvements','drift','qualification'}
        assert required.issubset(report)
        assert 'brier' in report['performance'] and 'ece' in report['performance']
    asyncio.run(run())
