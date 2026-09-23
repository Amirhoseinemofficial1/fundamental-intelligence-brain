from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

import aiohttp

from ..config import Settings
from ..storage.sqlite import connect

logger = logging.getLogger(__name__)

QUOTE_PRIORITY = {"USDT": 0, "USD": 1, "USDC": 2, "BTC": 3, "ETH": 4, "EUR": 5}


def _f(value: Any) -> float:
    try:
        result=float(value)
        return result if math.isfinite(result) else 0.0
    except (TypeError,ValueError):
        return 0.0


def base_symbol_from_market(symbol: str) -> str:
    raw=str(symbol or '').upper().strip()
    if raw.startswith('PI_'):
        raw=raw[3:]
    if '-' in raw:
        parts=raw.split('-')
        if parts and parts[0]:
            return 'BTC' if parts[0]=='XBT' else parts[0]
    if '_' in raw:
        parts=raw.split('_')
        if parts and parts[0]:
            return 'BTC' if parts[0]=='XBT' else parts[0]
    for quote in ('USDT','USDC','USD','EUR','BTC','ETH'):
        if raw.endswith(quote) and len(raw)>len(quote):
            base=raw[:-len(quote)]
            return 'BTC' if base=='XBT' else base
    return 'BTC' if raw=='XBT' else raw


@dataclass(frozen=True, slots=True)
class CryptoInstrument:
    venue: str
    symbol: str
    base: str
    quote: str
    kind: str
    active: bool = True
    price: float = 0.0
    quote_volume_24h: float = 0.0
    change_pct_24h: float = 0.0

    def rank_key(self) -> tuple[float, int, str]:
        return (-max(0.0,self.quote_volume_24h), QUOTE_PRIORITY.get(self.quote,99), self.symbol)


class CryptoUniverseScanner:
    """Discover active crypto markets without a hard-coded token list.

    The scanner is deliberately low-frequency. It discovers the complete venue
    directory, while WebSocket collectors may deep-stream only a bounded hot tier.
    This prevents an all-token universe from turning into tens of thousands of
    order-book subscriptions.
    """
    def __init__(self, settings: Settings, session: aiohttp.ClientSession):
        self.settings=settings
        self.session=session
        self.timeout=aiohttp.ClientTimeout(total=20,connect=5,sock_read=15)

    async def _json(self, method:str, url:str, **kwargs:Any)->Any:
        async with self.session.request(method,url,timeout=self.timeout,**kwargs) as r:
            text=await r.text(errors='replace')
            if r.status!=200:
                raise RuntimeError(f'HTTP_{r.status}:{url}')
            return json.loads(text)

    @staticmethod
    def _rank(rows:list[CryptoInstrument])->list[CryptoInstrument]:
        uniq:dict[str,CryptoInstrument]={}
        for row in rows:
            if not row.active or not row.symbol or not row.base:
                continue
            old=uniq.get(row.symbol)
            if old is None or row.quote_volume_24h>old.quote_volume_24h:
                uniq[row.symbol]=row
        return sorted(uniq.values(),key=lambda x:x.rank_key())

    async def binance(self)->list[CryptoInstrument]:
        base=os.getenv('BINANCE_FUTURES_REST','https://fapi.binance.com').rstrip('/')
        info,tickers=await asyncio.gather(self._json('GET',f'{base}/fapi/v1/exchangeInfo'),self._json('GET',f'{base}/fapi/v1/ticker/24hr'))
        tmap={str(x.get('symbol')):x for x in tickers if isinstance(x,dict)} if isinstance(tickers,list) else {}
        rows=[]
        for x in (info.get('symbols') or []) if isinstance(info,dict) else []:
            if not isinstance(x,dict) or str(x.get('status'))!='TRADING':continue
            if str(x.get('contractType') or '').upper() not in {'PERPETUAL',''}:continue
            sym=str(x.get('symbol') or '');base_asset=str(x.get('baseAsset') or base_symbol_from_market(sym)).upper();quote=str(x.get('quoteAsset') or '').upper();t=tmap.get(sym,{})
            rows.append(CryptoInstrument('binance',sym,base_asset,quote,'PERPETUAL',True,_f(t.get('lastPrice')),_f(t.get('quoteVolume')),_f(t.get('priceChangePercent'))))
        return self._rank(rows)

    async def binance_spot(self)->list[CryptoInstrument]:
        base=os.getenv('BINANCE_SPOT_REST','https://api.binance.com').rstrip('/')
        info,tickers=await asyncio.gather(self._json('GET',f'{base}/api/v3/exchangeInfo'),self._json('GET',f'{base}/api/v3/ticker/24hr'))
        tmap={str(x.get('symbol')):x for x in tickers if isinstance(x,dict)} if isinstance(tickers,list) else {}
        rows=[]
        for x in (info.get('symbols') or []) if isinstance(info,dict) else []:
            if not isinstance(x,dict) or str(x.get('status') or '').upper()!='TRADING':continue
            if x.get('isSpotTradingAllowed') is False:continue
            sym=str(x.get('symbol') or '');base_asset=str(x.get('baseAsset') or base_symbol_from_market(sym)).upper();quote=str(x.get('quoteAsset') or '').upper();t=tmap.get(sym,{})
            rows.append(CryptoInstrument('binance_spot',sym,base_asset,quote,'SPOT',True,_f(t.get('lastPrice')),_f(t.get('quoteVolume')),_f(t.get('priceChangePercent'))))
        return self._rank(rows)

    async def bybit(self)->list[CryptoInstrument]:
        base=os.getenv('BYBIT_REST','https://api.bybit.com').rstrip('/');cursor='';items=[]
        for _ in range(20):
            params={'category':'linear','status':'Trading','limit':'1000'}
            if cursor:params['cursor']=cursor
            obj=await self._json('GET',f'{base}/v5/market/instruments-info',params=params);result=obj.get('result') or {};items.extend(result.get('list') or []);cursor=str(result.get('nextPageCursor') or '')
            if not cursor:break
        ticks=await self._json('GET',f'{base}/v5/market/tickers',params={'category':'linear'});tmap={str(x.get('symbol')):x for x in ((ticks.get('result') or {}).get('list') or []) if isinstance(x,dict)}
        rows=[]
        for x in items:
            if not isinstance(x,dict) or str(x.get('status'))!='Trading':continue
            if str(x.get('contractType') or '').lower() not in {'linearperpetual',''}:continue
            sym=str(x.get('symbol') or '');t=tmap.get(sym,{})
            rows.append(CryptoInstrument('bybit',sym,str(x.get('baseCoin') or base_symbol_from_market(sym)).upper(),str(x.get('quoteCoin') or '').upper(),'PERPETUAL',True,_f(t.get('lastPrice')),_f(t.get('turnover24h')),_f(t.get('price24hPcnt'))*100.0))
        return self._rank(rows)

    async def bybit_spot(self)->list[CryptoInstrument]:
        base=os.getenv('BYBIT_REST','https://api.bybit.com').rstrip('/')
        info,ticks=await asyncio.gather(self._json('GET',f'{base}/v5/market/instruments-info',params={'category':'spot'}),self._json('GET',f'{base}/v5/market/tickers',params={'category':'spot'}))
        items=((info.get('result') or {}).get('list') or []) if isinstance(info,dict) else []
        tmap={str(x.get('symbol')):x for x in ((ticks.get('result') or {}).get('list') or []) if isinstance(x,dict)} if isinstance(ticks,dict) else {}
        rows=[]
        for x in items:
            if not isinstance(x,dict) or str(x.get('status') or '').lower()!='trading':continue
            sym=str(x.get('symbol') or '');t=tmap.get(sym,{})
            rows.append(CryptoInstrument('bybit_spot',sym,str(x.get('baseCoin') or base_symbol_from_market(sym)).upper(),str(x.get('quoteCoin') or '').upper(),'SPOT',True,_f(t.get('lastPrice')),_f(t.get('turnover24h')),_f(t.get('price24hPcnt'))*100.0))
        return self._rank(rows)

    async def okx(self)->list[CryptoInstrument]:
        base=os.getenv('OKX_REST','https://www.okx.com').rstrip('/')
        info,ticks=await asyncio.gather(self._json('GET',f'{base}/api/v5/public/instruments',params={'instType':'SWAP'}),self._json('GET',f'{base}/api/v5/market/tickers',params={'instType':'SWAP'}))
        tmap={str(x.get('instId')):x for x in (ticks.get('data') or []) if isinstance(x,dict)}
        rows=[]
        for x in info.get('data') or []:
            if not isinstance(x,dict) or str(x.get('state') or '').lower()!='live':continue
            sym=str(x.get('instId') or '');t=tmap.get(sym,{});last=_f(t.get('last'));open24=_f(t.get('open24h'));change=((last/open24-1)*100) if last>0 and open24>0 else 0.0
            rows.append(CryptoInstrument('okx',sym,str(x.get('ctValCcy') or x.get('uly') or base_symbol_from_market(sym)).split('-')[0].upper(),str(x.get('settleCcy') or x.get('quoteCcy') or '').upper(),'SWAP',True,last,_f(t.get('volCcy24h'))*last,change))
        return self._rank(rows)

    async def okx_spot(self)->list[CryptoInstrument]:
        base=os.getenv('OKX_REST','https://www.okx.com').rstrip('/')
        info,ticks=await asyncio.gather(self._json('GET',f'{base}/api/v5/public/instruments',params={'instType':'SPOT'}),self._json('GET',f'{base}/api/v5/market/tickers',params={'instType':'SPOT'}))
        tmap={str(x.get('instId')):x for x in (ticks.get('data') or []) if isinstance(x,dict)} if isinstance(ticks,dict) else {}
        rows=[]
        for x in (info.get('data') or []) if isinstance(info,dict) else []:
            if not isinstance(x,dict) or str(x.get('state') or '').lower()!='live':continue
            sym=str(x.get('instId') or '');t=tmap.get(sym,{});last=_f(t.get('last'));open24=_f(t.get('open24h'));change=((last/open24-1)*100) if last>0 and open24>0 else 0.0
            rows.append(CryptoInstrument('okx_spot',sym,str(x.get('baseCcy') or base_symbol_from_market(sym)).upper(),str(x.get('quoteCcy') or '').upper(),'SPOT',True,last,_f(t.get('volCcy24h'))*last,change))
        return self._rank(rows)

    async def mexc(self)->list[CryptoInstrument]:
        base=os.getenv('MEXC_FUTURES_REST','https://contract.mexc.com').rstrip('/')
        obj=await self._json('GET',f'{base}/api/v1/contract/detail');rows=[]
        for x in (obj.get('data') or []) if isinstance(obj,dict) else []:
            if not isinstance(x,dict):continue
            sym=str(x.get('symbol') or '');state=x.get('state')
            if state not in (None,0,'0','ENABLED','TRADING'):continue
            rows.append(CryptoInstrument('mexc',sym,str(x.get('baseCoin') or base_symbol_from_market(sym)).upper(),str(x.get('quoteCoin') or '').upper(),'PERPETUAL'))
        return self._rank(rows)

    async def mexc_spot(self)->list[CryptoInstrument]:
        base=os.getenv('MEXC_SPOT_REST','https://api.mexc.com').rstrip('/')
        info,tickers=await asyncio.gather(self._json('GET',f'{base}/api/v3/exchangeInfo'),self._json('GET',f'{base}/api/v3/ticker/24hr'))
        tmap={str(x.get('symbol')):x for x in tickers if isinstance(x,dict)} if isinstance(tickers,list) else {}
        rows=[]
        for x in (info.get('symbols') or []) if isinstance(info,dict) else []:
            if not isinstance(x,dict):continue
            status=str(x.get('status') or '').upper()
            if status not in {'1','ENABLED','TRADING'} or x.get('isSpotTradingAllowed') is False:continue
            sym=str(x.get('symbol') or '');t=tmap.get(sym,{})
            rows.append(CryptoInstrument('mexc_spot',sym,str(x.get('baseAsset') or base_symbol_from_market(sym)).upper(),str(x.get('quoteAsset') or '').upper(),'SPOT',True,_f(t.get('lastPrice')),_f(t.get('quoteVolume')),_f(t.get('priceChangePercent'))))
        return self._rank(rows)

    async def dydx(self)->list[CryptoInstrument]:
        base=os.getenv('DYDX_REST','https://indexer.dydx.trade').rstrip('/')
        obj=await self._json('GET',f'{base}/v4/perpetualMarkets');markets=obj.get('markets') or {};rows=[]
        for sym,x in markets.items() if isinstance(markets,dict) else []:
            if not isinstance(x,dict) or str(x.get('status') or '').upper() not in {'ACTIVE',''}:continue
            rows.append(CryptoInstrument('dydx',str(sym),base_symbol_from_market(str(sym)),str(x.get('quoteAsset') or 'USD').upper(),'PERPETUAL',True,_f(x.get('oraclePrice')),_f(x.get('volume24H')),_f(x.get('priceChange24H'))))
        return self._rank(rows)

    async def coinbase(self)->list[CryptoInstrument]:
        base=os.getenv('COINBASE_EXCHANGE_REST','https://api.exchange.coinbase.com').rstrip('/')
        obj=await self._json('GET',f'{base}/products');rows=[]
        for x in obj if isinstance(obj,list) else []:
            if not isinstance(x,dict) or str(x.get('status') or '').lower()!='online':continue
            sym=str(x.get('id') or '');base_asset=str(x.get('base_currency') or base_symbol_from_market(sym)).upper();quote=str(x.get('quote_currency') or '').upper()
            if not base_asset or base_asset in {'USD','USDC','USDT','EUR','GBP'}:continue
            rows.append(CryptoInstrument('coinbase',sym,base_asset,quote,'SPOT'))
        return self._rank(rows)

    async def kraken(self)->list[CryptoInstrument]:
        base=os.getenv('KRAKEN_FUTURES_REST','https://futures.kraken.com').rstrip('/')
        obj=await self._json('GET',f'{base}/derivatives/api/v3/instruments');rows=[]
        for x in (obj.get('instruments') or []) if isinstance(obj,dict) else []:
            if not isinstance(x,dict) or x.get('tradeable') is False:continue
            sym=str(x.get('symbol') or '');rows.append(CryptoInstrument('kraken',sym,base_symbol_from_market(sym),str(x.get('quote') or 'USD').upper(),'FUTURES'))
        return self._rank(rows)

    async def kraken_spot(self)->list[CryptoInstrument]:
        base=os.getenv('KRAKEN_SPOT_REST','https://api.kraken.com').rstrip('/')
        obj=await self._json('GET',f'{base}/0/public/AssetPairs');pairs=(obj.get('result') or {}) if isinstance(obj,dict) else {};rows=[]
        for key,x in pairs.items() if isinstance(pairs,dict) else []:
            if not isinstance(x,dict) or str(x.get('status') or 'online').lower() not in {'online',''}:continue
            ws=str(x.get('wsname') or '');parts=ws.split('/') if '/' in ws else []
            base_asset=(parts[0] if parts else str(x.get('base') or '')).upper();quote=(parts[1] if len(parts)>1 else str(x.get('quote') or '')).upper()
            base_asset='BTC' if base_asset in {'XBT','XXBT'} else base_asset.lstrip('XZ') if len(base_asset)>3 and base_asset[:1] in {'X','Z'} else base_asset
            quote='BTC' if quote in {'XBT','XXBT'} else quote.lstrip('XZ') if len(quote)>3 and quote[:1] in {'X','Z'} else quote
            sym=str(x.get('altname') or key)
            if base_asset and quote:rows.append(CryptoInstrument('kraken_spot',sym,base_asset,quote,'SPOT'))
        return self._rank(rows)

    async def hyperliquid(self)->list[CryptoInstrument]:
        base=os.getenv('HYPERLIQUID_REST','https://api.hyperliquid.xyz').rstrip('/')
        obj=await self._json('POST',f'{base}/info',json={'type':'metaAndAssetCtxs'});meta=(obj[0] if isinstance(obj,list) and obj else {}) or {};ctx=(obj[1] if isinstance(obj,list) and len(obj)>1 else []) or [];rows=[]
        universe=meta.get('universe') or []
        for i,x in enumerate(universe):
            if not isinstance(x,dict) or bool(x.get('isDelisted')):continue
            coin=str(x.get('name') or '');c=ctx[i] if i<len(ctx) and isinstance(ctx[i],dict) else {};mark=_f(c.get('markPx'));prev=_f(c.get('prevDayPx'));change=((mark/prev-1)*100) if mark>0 and prev>0 else 0.0
            rows.append(CryptoInstrument('hyperliquid',coin,coin.upper(),'USD','PERPETUAL',True,mark,_f(c.get('dayNtlVlm')),change))
        return self._rank(rows)

    async def scan_venue(self, venue:str)->list[CryptoInstrument]:
        method=getattr(self,str(venue).lower(),None)
        if method is None:return []
        try:return await method()
        except asyncio.CancelledError:raise
        except Exception as exc:
            logger.warning('crypto universe scan failed venue=%s error=%s',venue,type(exc).__name__)
            return []

    async def scan_all(self, venues:tuple[str,...])->dict[str,list[CryptoInstrument]]:
        sem=asyncio.Semaphore(4)
        async def one(v:str):
            async with sem:return v,await self.scan_venue(v)
        pairs=await asyncio.gather(*(one(v) for v in venues))
        return {v:rows for v,rows in pairs}

    async def stream_symbols(self,venue:str,fallback:tuple[str,...],limit:int)->tuple[str,...]:
        rows=await self.scan_venue(venue)
        if not rows:return fallback
        symbols=tuple(x.symbol for x in (rows if limit<=0 else rows[:limit]))
        return tuple(dict.fromkeys((*fallback,*symbols))) if limit<=0 else tuple(dict.fromkeys((*fallback,*symbols)))[:max(len(fallback),limit)]

    @staticmethod
    def screen_all_tokens(result:dict[str,list[CryptoInstrument]],hot_limit:int)->list[dict[str,Any]]:
        """Lightweight analysis for every active token in the discovered universe.

        This intentionally does not run the expensive 20-expert Brain for every
        symbol every cycle.  It still evaluates every discovered token for
        liquidity, cross-venue coverage, momentum and price dispersion.  Event,
        anomaly and news-selected assets are then promoted to the full Brain.
        """
        grouped:dict[str,list[CryptoInstrument]]={}
        for rows in result.values():
            for row in rows:
                if row.active and row.base:
                    grouped.setdefault(row.base.upper(),[]).append(row)
        ranked=sorted(grouped.items(),key=lambda kv:-max((r.quote_volume_24h for r in kv[1]),default=0.0))
        hot={base for base,_ in ranked[:max(0,hot_limit)]} if hot_limit>0 else set(grouped)
        out=[]
        for base,rows in ranked:
            best=sorted(rows,key=lambda r:r.rank_key())[0]
            prices=[r.price for r in rows if r.price>0 and r.quote in {'USD','USDT','USDC'}]
            changes=[r.change_pct_24h for r in rows if math.isfinite(r.change_pct_24h)]
            max_vol=max((r.quote_volume_24h for r in rows),default=0.0)
            max_abs=max((abs(x) for x in changes),default=0.0)
            avg_change=(sum(changes)/len(changes)) if changes else 0.0
            dispersion=((max(prices)-min(prices))/((sum(prices)/len(prices)) or 1.0)*100.0) if len(prices)>=2 else 0.0
            venue_count=len({r.venue for r in rows})
            liquidity_score=math.log10(1.0+max(0.0,max_vol))
            attention=max_abs*(1.0+0.12*max(0,venue_count-1))*(1.0+0.08*liquidity_score)
            out.append({
                'base':base,'tier':'HOT' if base in hot else 'WARM','venue_count':venue_count,
                'market_count':len(rows),'primary_venue':best.venue,'primary_symbol':best.symbol,
                'price':best.price,'max_quote_volume_24h':max_vol,'avg_change_pct_24h':avg_change,
                'max_abs_change_pct_24h':max_abs,'cross_venue_price_dispersion_pct':dispersion,
                'attention_score':attention,
            })
        return out

    def persist_snapshot(self, result:dict[str,list[CryptoInstrument]], screen:list[dict[str,Any]]|None=None)->Path:
        target=self.settings.data_dir/'market'/'crypto_universe.json';target.parent.mkdir(parents=True,exist_ok=True);tmp=target.with_suffix('.json.tmp')
        payload={'updated_at':time.time(),'venues':{v:[asdict(x) for x in rows] for v,rows in result.items()},'token_screen':screen or []}
        tmp.write_text(json.dumps(payload,separators=(',',':'),ensure_ascii=False));os.replace(tmp,target);return target

    def register_assets(self,result:dict[str,list[CryptoInstrument]])->dict[str,int]:
        # BrainStore is imported lazily to avoid coupling exchange hot paths to
        # intelligence initialization unless the low-frequency universe service runs.
        from ..intelligence.store import BrainStore
        BrainStore(self.settings)
        path=self.settings.data_dir/'brain'/'intelligence.sqlite3';now=time.time();by_base:dict[str,list[CryptoInstrument]]={}
        for rows in result.values():
            for row in rows:
                if row.base:by_base.setdefault(row.base.upper(),[]).append(row)
        inserted=updated=0
        with connect(path,self.settings) as con:
            for base,rows in by_base.items():
                existing=con.execute("SELECT asset_id,asset_class,metadata_json FROM assets WHERE UPPER(symbol)=? ORDER BY CASE WHEN asset_id IN ('BTC','ETH','SOL','SUI') THEN 0 ELSE 1 END,asset_id",(base,)).fetchall()
                seed=next((r for r in existing if str(r['asset_id']) in {'BTC','ETH','SOL','SUI'}),None)
                aggregate=next((r for r in existing if str(r['asset_id']).startswith('crypto:')),None)
                asset_id=str(seed['asset_id']) if seed else str(aggregate['asset_id']) if aggregate else f'crypto:{base}'
                best=sorted(rows,key=lambda x:x.rank_key())[0]
                old=con.execute('SELECT metadata_json FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
                try:meta=json.loads(old['metadata_json'] or '{}') if old else {}
                except (TypeError,ValueError):meta={}
                venue_symbols={r.venue:r.symbol for r in sorted(rows,key=lambda x:x.rank_key())}
                meta.update({'cex_universe':True,'venues':sorted(venue_symbols),'market_symbols':venue_symbols,'quote_assets':sorted({r.quote for r in rows if r.quote}),'quote_volume_24h':max((r.quote_volume_24h for r in rows),default=0.0),'change_pct_24h':best.change_pct_24h,'universe_updated_at':now})
                cur=con.execute("INSERT INTO assets(asset_id,symbol,name,asset_class,market_symbol,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET symbol=excluded.symbol,name=CASE WHEN assets.name=assets.symbol THEN excluded.name ELSE assets.name END,market_symbol=excluded.market_symbol,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",(asset_id,base,base,'CRYPTO' if not seed else str(seed['asset_class']),best.symbol,json.dumps(meta,sort_keys=True),now))
                inserted+=int(old is None);updated+=int(old is not None)
                for r in rows:
                    con.execute("INSERT OR REPLACE INTO provider_symbols(asset_id,provider,provider_symbol,exchange,updated_at) VALUES(?,?,?,?,?)",(asset_id,f'cex:{r.venue}',r.symbol,r.venue.upper(),now))
                # Symbol aliases are only installed when they are not already owned
                # by a different canonical asset. Ambiguous contract tickers remain
                # resolvable only through chain+contract identity.
                for alias in (base,f'${base}'):
                    current=con.execute('SELECT asset_id FROM asset_aliases WHERE alias=?',(alias,)).fetchone()
                    if current is None or str(current['asset_id'])==asset_id:
                        con.execute("INSERT OR REPLACE INTO asset_aliases(alias,asset_id,source,confidence,updated_at) VALUES(?,?,'cex_universe',.92,?)",(alias,asset_id,now))
        return {'assets':len(by_base),'inserted':inserted,'updated':updated}

    @staticmethod
    def anomalies(result:dict[str,list[CryptoInstrument]],change_pct:float,min_volume:float,limit:int)->list[CryptoInstrument]:
        best:dict[str,CryptoInstrument]={}
        for rows in result.values():
            for row in rows:
                if abs(row.change_pct_24h)<change_pct or row.quote_volume_24h<min_volume:continue
                old=best.get(row.base)
                if old is None or row.quote_volume_24h>old.quote_volume_24h:best[row.base]=row
        return sorted(best.values(),key=lambda x:(-abs(x.change_pct_24h),-x.quote_volume_24h,x.base))[:max(0,limit)]
