from __future__ import annotations

import dataclasses
import asyncio
import json
from pathlib import Path

from v52.config import Settings
from v52.market.crypto_universe import CryptoInstrument,CryptoUniverseScanner,base_symbol_from_market
from v52.intelligence.observation import DynamicAssetUniverse
from v52.intelligence.store import BrainStore
from v52.storage.sqlite import connect


def test_base_symbol_formats():
    assert base_symbol_from_market('BTCUSDT')=='BTC'
    assert base_symbol_from_market('SUI-USDT-SWAP')=='SUI'
    assert base_symbol_from_market('PEPE_USDT')=='PEPE'
    assert base_symbol_from_market('PI_XBTUSD')=='BTC'


def test_anomaly_dedup_prefers_liquid_venue():
    result={'a':[CryptoInstrument('a','AAAUSDT','AAA','USDT','PERP',True,1,2_000_000,15)],'b':[CryptoInstrument('b','AAA-USD','AAA','USD','PERP',True,1,5_000_000,16)],'c':[CryptoInstrument('c','BBBUSDT','BBB','USDT','PERP',True,1,9_000_000,3)]}
    rows=CryptoUniverseScanner.anomalies(result,12,1_000_000,10)
    assert len(rows)==1 and rows[0].venue=='b' and rows[0].base=='AAA'


def test_cex_registration_and_fast_discovery(settings:Settings,tmp_path:Path):
    s=dataclasses.replace(settings,data_dir=tmp_path/'data')
    BrainStore(s)
    universe=DynamicAssetUniverse(s)
    class Dummy:
        register_assets=CryptoUniverseScanner.register_assets
    # call unbound registrar with only settings; method does not require session
    d=Dummy();d.settings=s
    stats=d.register_assets({'binance':[CryptoInstrument('binance','ARBUSDT','ARB','USDT','PERPETUAL',True,.5,10_000_000,4.0)]})
    assert stats['assets']>=1
    assert universe.discover({'title':'Crypto update: $ARB funding changes','category':'CRYPTO'})==('crypto:ARB',)
    # A normal English occurrence must not trigger the large CEX symbol universe.
    assert 'crypto:ARB' not in universe.discover({'title':'The arbiter issued a report','category':'GENERAL'})


def test_provisional_direction_uses_event_mechanism_not_cold_probability():
    from types import SimpleNamespace
    from v52.intelligence.service import IntelligenceService
    decision=SimpleNamespace(probability=.20,calibrated_confidence=.70)
    event={'event_type':'EXCHANGE_LISTING','title':'MEXC will list APT','summary':'Spot listing',
           'sources':[{'source_url':'https://www.mexc.com/support/articles/apt'}]}
    asset={'asset_id':'crypto:APT','mapping_confidence':.88,'mapping_reason':'dynamic_universe','directional_bias':'MIXED'}
    result=IntelligenceService._provisional_action(decision,{'quality':.5},'CONSERVATIVE',True,{'price':5},event,asset)
    assert result['action']=='WATCH_BUY' and result['direction']=='UP'
    assert result['basis'].startswith('DETERMINISTIC_EVENT_MECHANISM')
    noisy={**event,'event_type':'YIELD_CURVE_UPDATE','title':'DATASET SURFACE REVISION'}
    assert IntelligenceService._provisional_action(decision,{'quality':.5},'CONSERVATIVE',True,{'price':5},noisy,asset) is None


def test_dynamic_asset_name_requires_a_complete_phrase(settings:Settings,tmp_path:Path):
    s=dataclasses.replace(settings,data_dir=tmp_path/'data')
    BrainStore(s);universe=DynamicAssetUniverse(s)
    asset=universe.discover({'token_symbol':'STANDARD','token_name':'STANDARD','chain':'base','contract':'0x123abc'})
    assert asset==('base:0x123abc',)
    assert asset[0] not in universe.discover({'title':'Policy update','standardized_surprise':.7})
    assert asset[0] in universe.discover({'title':'STANDARD token update'})
    short=universe.discover({'token_symbol':'F','token_name':'SynFutures','chain':'base','contract':'0x456def'})
    assert short==('base:0x456def',)
    assert short[0] not in universe.discover({'title':'Telephone and Data Systems files results'})
    assert short[0] in universe.discover({'title':'$F token update'})
    generic=universe.discover({'token_symbol':'FOR','token_name':'Federal Oil Reserve','chain':'solana','contract':'12vc7'})
    assert generic==('solana:12vc7',)
    assert generic[0] not in universe.discover({'title':'Message to RBA as rate decision looms: do not punish us for inflation'})
    assert universe.discover({'title':'Federal Oil Reserve token update'})==generic


def test_common_equity_ticker_is_not_inferred_from_navigation_text(settings:Settings,tmp_path:Path):
    s=dataclasses.replace(settings,data_dir=tmp_path/'data');BrainStore(s);universe=DynamicAssetUniverse(s)
    with connect(s.data_dir/'brain'/'intelligence.sqlite3',s) as con:
        con.execute("INSERT INTO assets(asset_id,symbol,name,asset_class,market_symbol,metadata_json,updated_at) VALUES('equity:XNAS:HELP','HELP','Cybin D/B/A Helus Pharma','EQUITY','HELP','{}',1)")
        con.execute("INSERT INTO asset_aliases(alias,asset_id,source,confidence,updated_at) VALUES('HELP','equity:XNAS:HELP','fixture',1,1)")
    assert 'equity:XNAS:HELP' not in universe.discover({'title':'DATASET SURFACE REVISION','summary':'View Data | Help | Datasets','category':'ECONOMY'})
    assert universe.discover({'title':'NASDAQ earnings: HELP beats estimates','category':'EQUITIES'})==('equity:XNAS:HELP',)
    assert universe.discover({'title':'Cybin D/B/A Helus Pharma reports earnings','category':'EQUITIES'})==('equity:XNAS:HELP',)


def test_all_token_screen_covers_every_base():
    result={
        'a':[CryptoInstrument('a','AAAUSDT','AAA','USDT','PERP',True,1.0,10_000_000,5.0),CryptoInstrument('a','BBBUSDT','BBB','USDT','PERP',True,2.0,1_000_000,-2.0)],
        'b':[CryptoInstrument('b','AAA-USD','AAA','USD','PERP',True,1.01,8_000_000,4.0),CryptoInstrument('b','CCC-USD','CCC','USD','SPOT',True,3.0,500_000,1.0)],
    }
    screen=CryptoUniverseScanner.screen_all_tokens(result,1)
    assert {x['base'] for x in screen}=={'AAA','BBB','CCC'}
    assert sum(x['tier']=='HOT' for x in screen)==1
    aaa=next(x for x in screen if x['base']=='AAA')
    assert aaa['venue_count']==2 and aaa['cross_venue_price_dispersion_pct']>0

import pytest

@pytest.mark.asyncio
async def test_spot_directory_parsers(settings):
    class FakeScanner(CryptoUniverseScanner):
        async def _json(self,method,url,**kwargs):
            params=kwargs.get('params') or {}
            if 'api.binance.com' in url and url.endswith('/exchangeInfo'):
                return {'symbols':[{'symbol':'ARBUSDT','status':'TRADING','baseAsset':'ARB','quoteAsset':'USDT','isSpotTradingAllowed':True}]}
            if 'api.binance.com' in url and url.endswith('/ticker/24hr'):
                return [{'symbol':'ARBUSDT','lastPrice':'1.2','quoteVolume':'25000000','priceChangePercent':'7.5'}]
            if 'bybit.com' in url and 'instruments-info' in url and params.get('category')=='spot':
                return {'result':{'list':[{'symbol':'TIAUSDT','status':'Trading','baseCoin':'TIA','quoteCoin':'USDT'}]}}
            if 'bybit.com' in url and url.endswith('/v5/market/tickers') and params.get('category')=='spot':
                return {'result':{'list':[{'symbol':'TIAUSDT','lastPrice':'5','turnover24h':'10000000','price24hPcnt':'0.03'}]}}
            if 'okx.com' in url and 'instruments' in url and params.get('instType')=='SPOT':
                return {'data':[{'instId':'OP-USDT','state':'live','baseCcy':'OP','quoteCcy':'USDT'}]}
            if 'okx.com' in url and 'tickers' in url and params.get('instType')=='SPOT':
                return {'data':[{'instId':'OP-USDT','last':'2.1','open24h':'2','volCcy24h':'3000000'}]}
            if 'api.mexc.com' in url and url.endswith('/exchangeInfo'):
                return {'symbols':[{'symbol':'ENAUSDT','status':'1','baseAsset':'ENA','quoteAsset':'USDT','isSpotTradingAllowed':True}]}
            if 'api.mexc.com' in url and url.endswith('/ticker/24hr'):
                return [{'symbol':'ENAUSDT','lastPrice':'0.5','quoteVolume':'9000000','priceChangePercent':'-4'}]
            if 'api.kraken.com' in url and url.endswith('/AssetPairs'):
                return {'result':{'XXBTZUSD':{'altname':'XBTUSD','wsname':'XBT/USD','status':'online'},'SUIUSD':{'altname':'SUIUSD','wsname':'SUI/USD','status':'online'}}}
            raise AssertionError((method,url,params))
    import aiohttp
    async with aiohttp.ClientSession() as session:
        scan=FakeScanner(settings,session)
        assert (await scan.binance_spot())[0].base=='ARB'
        assert (await scan.bybit_spot())[0].base=='TIA'
        assert (await scan.okx_spot())[0].base=='OP'
        assert (await scan.mexc_spot())[0].base=='ENA'
        kr=await scan.kraken_spot()
        assert {x.base for x in kr}=={'BTC','SUI'}

@pytest.mark.asyncio
async def test_planned_hot_stream_rotation(settings):
    from types import SimpleNamespace
    from v52.market.service import MarketRuntime
    class Metrics:
        def __init__(self): self.values={}
        def inc(self,key,*args): self.values[key]=self.values.get(key,0)+1
    fake=SimpleNamespace(settings=dataclasses.replace(settings,market_crypto_universe_enabled=True,market_crypto_stream_rotate_seconds=.01),metrics=Metrics(),service='test-market')
    stop=asyncio.Event()
    async def runner():
        await asyncio.Event().wait()
    rotated=await MarketRuntime._run_stream_with_rotation(fake,'BYBIT',runner,stop)
    assert rotated is True and fake.metrics.values.get('crypto_stream_planned_rotation')==1
