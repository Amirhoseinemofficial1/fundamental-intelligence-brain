from __future__ import annotations
import json,os,time,urllib.parse,urllib.request
from dataclasses import dataclass
from typing import Any,Protocol
from ..config import Settings
from ..storage.sqlite import connect

@dataclass(frozen=True,slots=True)
class ReferenceQuote:
    asset_id:str;observed_at:float;available_at:float;price:float;volume:float|None;provider:str;quality:float
class ReferenceMarketProvider(Protocol):
    def get_quote(self,asset_id:str,at:float|None=None)->ReferenceQuote|None:...
    def get_history(self,asset_id:str,start:float,end:float,limit:int=5000)->list[ReferenceQuote]:...
    def get_ohlcv(self,asset_id:str,start:float,end:float,limit:int=5000)->list[ReferenceQuote]:...
    def get_volume(self,asset_id:str,at:float|None=None)->float|None:...
    def get_orderbook(self,asset_id:str)->dict[str,Any]|None:...
    def health(self)->dict[str,Any]:...
class BrainReferenceMarketProvider:
    def __init__(self,settings:Settings,provider:str|None=None):
        from .store import BrainStore
        BrainStore(settings)
        self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3';self.provider=provider
    def get_history(self,asset_id,start,end,limit=5000):
        where='(asset_id=? OR asset_id IN (SELECT asset_id FROM assets WHERE market_symbol=?)) AND observed_at>=? AND observed_at<=? AND available_at<=?';params=[asset_id,asset_id,start,end,end]
        if self.provider:where+=' AND provider=?';params.append(self.provider)
        with connect(self.path,self.settings) as con:rows=con.execute(f'SELECT * FROM reference_prices WHERE {where} ORDER BY observed_at LIMIT ?',(*params,limit)).fetchall()
        return [ReferenceQuote(str(r['asset_id']),float(r['observed_at']),float(r['available_at']),float(r['price']),float(r['volume']) if r['volume'] is not None else None,str(r['provider']),float(r['quality'])) for r in rows]
    def get_ohlcv(self,asset_id,start,end,limit=5000):return self.get_history(asset_id,start,end,limit)
    def get_quote(self,asset_id,at=None):
        at=float(at or time.time())
        with connect(self.path,self.settings) as con:
            row=con.execute('SELECT * FROM reference_prices WHERE asset_id=? AND observed_at<=? AND available_at<=? ORDER BY observed_at DESC LIMIT 1',(asset_id,at,at)).fetchone()
        return ReferenceQuote(str(row['asset_id']),float(row['observed_at']),float(row['available_at']),float(row['price']),float(row['volume']) if row['volume'] is not None else None,str(row['provider']),float(row['quality'])) if row else None
    def get_volume(self,asset_id,at=None):q=self.get_quote(asset_id,at);return q.volume if q else None
    def get_orderbook(self,asset_id):return None
    def health(self):
        with connect(self.path,self.settings) as con:row=con.execute('SELECT COUNT(*) count,MAX(available_at) latest FROM reference_prices').fetchone()
        return {'ok':int(row['count'])>0,'count':int(row['count']),'latest':row['latest'],'provider':self.provider or 'all'}

PUBLIC_SYMBOLS={'XAUUSD':'GC=F','XAGUSD':'SI=F','WTI':'CL=F','BRENT':'BZ=F','EURUSD':'EURUSD=X','GBPUSD':'GBPUSD=X','USDJPY':'JPY=X','USDCHF':'CHF=X','AUDUSD':'AUDUSD=X','NZDUSD':'NZDUSD=X','USDCAD':'CAD=X','DXY':'DX-Y.NYB','SPX':'^GSPC','NDX':'^NDX','DJI':'^DJI','DAX':'^GDAXI','FTSE':'^FTSE'}

class PublicChartMarketProvider:
    """No-key runtime adapter for metals, energy, FX, equities and indices."""
    def __init__(self,settings:Settings):self.settings=settings;self.brain=BrainReferenceMarketProvider(settings);self.last_error='';self.last_success=0.0
    def _symbol(self,asset_id):
        key=str(asset_id).upper()
        if key in PUBLIC_SYMBOLS:return PUBLIC_SYMBOLS[key]
        with connect(self.brain.path,self.settings) as con:
            row=con.execute("SELECT provider_symbol FROM provider_symbols WHERE asset_id=? AND provider='public_chart'",(asset_id,)).fetchone()
            if row:return str(row['provider_symbol'])
            asset=con.execute("SELECT market_symbol FROM assets WHERE asset_id=?",(asset_id,)).fetchone()
        return str(asset['market_symbol']) if asset else key
    def _download(self,url):
        request=urllib.request.Request(url,headers={'User-Agent':self.settings.user_agent,'Accept':'application/json'})
        with urllib.request.urlopen(request,timeout=6) as response:return json.loads(response.read(8_000_000))
    def get_history(self,asset_id,start,end,limit=5000):
        symbol=self._symbol(asset_id);period1=max(0,int(start)-120);period2=max(period1+60,int(end)+120);url=f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(symbol,safe='')}?period1={period1}&period2={period2}&interval=1m&events=history"
        try:
            data=self._download(url);result=((data.get('chart') or {}).get('result') or [])[0];timestamps=result.get('timestamp') or [];quote=(((result.get('indicators') or {}).get('quote') or [{}])[0]);closes=quote.get('close') or [];volumes=quote.get('volume') or [];rows=[]
            with connect(self.brain.path,self.settings) as con:
                ordered=sorted(enumerate(timestamps),key=lambda item:float(item[1]))
                for index,epoch in ordered[-max(1,int(limit)):]:
                    price=closes[index] if index<len(closes) else None
                    if price is None:continue
                    volume=volumes[index] if index<len(volumes) else None;epoch=float(epoch)+60
                    if epoch>min(end,time.time()):continue
                    fetched_at=time.time();item=ReferenceQuote(str(asset_id),epoch,fetched_at,float(price),float(volume) if volume is not None else None,'public_chart',.72);rows.append(item)
                    con.execute("INSERT OR IGNORE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('public_chart',?,?,?,?,?,.72,?)",(asset_id,epoch,fetched_at,item.price,item.volume,json.dumps({'symbol':symbol})))
            rows.sort(key=lambda item:item.observed_at);self.last_success=time.time();self.last_error='';return rows
        except Exception as exc:self.last_error=f'{type(exc).__name__}:{exc}'[:500];return []
    def get_quote(self,asset_id,at=None):
        end=float(at or time.time());rows=self.get_history(asset_id,end-900,end,20);return max(rows,key=lambda item:item.observed_at) if rows else self.brain.get_quote(asset_id,at)
    def get_ohlcv(self,asset_id,start,end,limit=5000):return self.get_history(asset_id,start,end,limit)
    def get_volume(self,asset_id,at=None):q=self.get_quote(asset_id,at);return q.volume if q else None
    def get_orderbook(self,asset_id):return None
    def health(self):return {'provider':'public_chart','ok':not self.last_error,'last_success':self.last_success,'last_error':self.last_error,'asset_classes':['COMMODITY','FOREX','EQUITY','INDEX']}

class DexContractMarketProvider:
    """Contract-address DEX discovery and quote adapter (Dexscreener-compatible)."""
    def __init__(self,settings:Settings):self.settings=settings;self.brain=BrainReferenceMarketProvider(settings);self.last_error='';self.last_success=0.0
    def _identity(self,asset_id:str)->tuple[str,str]|None:
        with connect(self.brain.path,self.settings) as con:row=con.execute("SELECT metadata_json FROM assets WHERE asset_id=? AND asset_class='TOKEN'",(asset_id,)).fetchone()
        if not row:return None
        meta=json.loads(row['metadata_json'] or '{}');chain=str(meta.get('chain') or '').lower();contract=str(meta.get('contract') or meta.get('contract_address') or '')
        return (chain,contract) if chain and contract else None
    def _download(self,url:str)->dict[str,Any]:
        request=urllib.request.Request(url,headers={'User-Agent':self.settings.user_agent,'Accept':'application/json'})
        with urllib.request.urlopen(request,timeout=6) as response:return json.loads(response.read(8_000_000))
    def _pairs(self,asset_id:str)->list[dict[str,Any]]:
        identity=self._identity(asset_id)
        if not identity:return []
        chain,contract=identity;url=f"https://api.dexscreener.com/latest/dex/tokens/{urllib.parse.quote(contract,safe='')}"
        try:
            data=self._download(url);pairs=[p for p in (data.get('pairs') or []) if str(p.get('chainId') or '').lower()==chain and str((p.get('baseToken') or {}).get('address') or '').lower()==contract.lower()]
            pairs.sort(key=lambda p:float((p.get('liquidity') or {}).get('usd') or 0),reverse=True);now=time.time()
            with connect(self.brain.path,self.settings) as con:
                for pair in pairs:
                    con.execute("""INSERT OR REPLACE INTO dex_pairs(asset_id,provider,chain,contract,pair_address,quote_symbol,liquidity_usd,volume_24h,url,metadata_json,updated_at)
                        VALUES(?,'dexscreener',?,?,?,?,?,?,?,?,?)""",(asset_id,chain,contract,str(pair.get('pairAddress') or ''),str((pair.get('quoteToken') or {}).get('symbol') or ''),float((pair.get('liquidity') or {}).get('usd') or 0),float((pair.get('volume') or {}).get('h24') or 0),str(pair.get('url') or ''),json.dumps(pair,sort_keys=True),now))
            self.last_success=now;self.last_error='';return pairs
        except Exception as exc:self.last_error=f'{type(exc).__name__}:{exc}'[:500];return []
    def get_quote(self,asset_id,at=None):
        if not self._identity(str(asset_id)):return None
        pairs=self._pairs(str(asset_id));now=time.time()
        if pairs:
            pair=pairs[0]
            try:price=float(pair.get('priceUsd'));volume=float((pair.get('volume') or {}).get('h24') or 0)
            except (TypeError,ValueError):price=0.0;volume=0.0
            if price>0:
                quote=ReferenceQuote(str(asset_id),now,now,price,volume,'dexscreener',.75)
                with connect(self.brain.path,self.settings) as con:con.execute("INSERT OR REPLACE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('dexscreener',?,?,?,?,?,.75,?)",(asset_id,now,now,price,volume,json.dumps({'pair_address':pair.get('pairAddress'),'contract_identity':asset_id})))
                return quote
        return self.brain.get_quote(str(asset_id),at)
    def get_history(self,asset_id,start,end,limit=5000):return self.brain.get_history(str(asset_id),start,end,limit) if self._identity(str(asset_id)) else []
    def get_ohlcv(self,asset_id,start,end,limit=5000):return self.get_history(asset_id,start,end,limit)
    def get_volume(self,asset_id,at=None):q=self.get_quote(asset_id,at);return q.volume if q else None
    def get_orderbook(self,asset_id):return None
    def health(self):return {'provider':'dexscreener','ok':not self.last_error,'last_success':self.last_success,'last_error':self.last_error,'identity':'chain+contract'}

class CexUniverseMarketProvider:
    """On-demand public CEX quote/history for dynamically discovered CEX assets.

    Only active/watchlisted assets are polled, so the complete crypto directory can
    stay registered without creating a request storm.
    """
    def __init__(self,settings:Settings):self.settings=settings;self.brain=BrainReferenceMarketProvider(settings);self.last_error='';self.last_success=0.0
    def _symbols(self,asset_id:str)->dict[str,str]:
        with connect(self.brain.path,self.settings) as con:
            rows=con.execute("SELECT provider,provider_symbol FROM provider_symbols WHERE asset_id=? AND provider LIKE 'cex:%'",(asset_id,)).fetchall()
        return {str(r['provider']).split(':',1)[1]:str(r['provider_symbol']) for r in rows}
    def _download(self,url:str,body:bytes|None=None)->Any:
        headers={'User-Agent':self.settings.user_agent,'Accept':'application/json'}
        if body is not None:headers['Content-Type']='application/json'
        req=urllib.request.Request(url,data=body,headers=headers,method='POST' if body is not None else 'GET')
        with urllib.request.urlopen(req,timeout=6) as response:return json.loads(response.read(8_000_000))
    def _persist(self,asset_id:str,price:float,volume:float|None,provider:str,ts:float|None=None,meta:dict[str,Any]|None=None)->ReferenceQuote|None:
        if not price or price<=0:return None
        now=float(ts or time.time());q=ReferenceQuote(asset_id,now,now,float(price),float(volume) if volume is not None else None,provider,.82)
        with connect(self.brain.path,self.settings) as con:con.execute("INSERT OR REPLACE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES(?,?,?,?,?,?,.82,?)",(provider,asset_id,now,now,q.price,q.volume,json.dumps(meta or {})))
        self.last_success=time.time();self.last_error='';return q
    def get_quote(self,asset_id,at=None):
        symbols=self._symbols(str(asset_id))
        if str(asset_id) in {'BTC','ETH','SOL','SUI'}:symbols.setdefault('binance_spot',str(asset_id)+'USDT')
        if not symbols:return None
        try:
            if symbols.get('binance_spot'):
                sym=urllib.parse.quote(symbols['binance_spot']);obj=self._download(f"{os.getenv('BINANCE_SPOT_REST','https://api.binance.com').rstrip('/')}/api/v3/ticker/24hr?symbol={sym}")
                return self._persist(str(asset_id),float(obj.get('lastPrice') or 0),float(obj.get('quoteVolume') or 0),'cex:binance_spot',meta={'symbol':symbols['binance_spot']})
            if symbols.get('bybit_spot'):
                sym=urllib.parse.quote(symbols['bybit_spot']);obj=self._download(f"{os.getenv('BYBIT_REST','https://api.bybit.com').rstrip('/')}/v5/market/tickers?category=spot&symbol={sym}");row=(((obj.get('result') or {}).get('list') or [{}])[0])
                return self._persist(str(asset_id),float(row.get('lastPrice') or 0),float(row.get('turnover24h') or 0),'cex:bybit_spot',meta={'symbol':symbols['bybit_spot']})
            if symbols.get('okx_spot'):
                sym=urllib.parse.quote(symbols['okx_spot']);obj=self._download(f"{os.getenv('OKX_REST','https://www.okx.com').rstrip('/')}/api/v5/market/ticker?instId={sym}");row=((obj.get('data') or [{}])[0])
                return self._persist(str(asset_id),float(row.get('last') or 0),None,'cex:okx_spot',meta={'symbol':symbols['okx_spot']})
            if symbols.get('mexc_spot'):
                sym=urllib.parse.quote(symbols['mexc_spot']);obj=self._download(f"{os.getenv('MEXC_SPOT_REST','https://api.mexc.com').rstrip('/')}/api/v3/ticker/24hr?symbol={sym}")
                return self._persist(str(asset_id),float(obj.get('lastPrice') or 0),float(obj.get('quoteVolume') or 0),'cex:mexc_spot',meta={'symbol':symbols['mexc_spot']})
            if symbols.get('coinbase'):
                sym=urllib.parse.quote(symbols['coinbase'],safe='');obj=self._download(f"{os.getenv('COINBASE_EXCHANGE_REST','https://api.exchange.coinbase.com').rstrip('/')}/products/{sym}/ticker")
                return self._persist(str(asset_id),float(obj.get('price') or 0),float(obj.get('volume') or 0),'cex:coinbase',meta={'symbol':symbols['coinbase']})
            if symbols.get('kraken_spot'):
                sym=urllib.parse.quote(symbols['kraken_spot']);obj=self._download(f"{os.getenv('KRAKEN_SPOT_REST','https://api.kraken.com').rstrip('/')}/0/public/Ticker?pair={sym}");result=obj.get('result') or {};row=next(iter(result.values()),{}) if isinstance(result,dict) else {};close=row.get('c') or [];vol=row.get('v') or []
                return self._persist(str(asset_id),float(close[0] if close else 0),float(vol[-1] if vol else 0),'cex:kraken_spot',meta={'symbol':symbols['kraken_spot']})
            if symbols.get('binance'):
                sym=urllib.parse.quote(symbols['binance']);obj=self._download(f"{os.getenv('BINANCE_FUTURES_REST','https://fapi.binance.com').rstrip('/')}/fapi/v1/ticker/24hr?symbol={sym}")
                return self._persist(str(asset_id),float(obj.get('lastPrice') or 0),float(obj.get('quoteVolume') or 0),'cex:binance',meta={'symbol':symbols['binance']})
            if symbols.get('bybit'):
                sym=urllib.parse.quote(symbols['bybit']);obj=self._download(f"{os.getenv('BYBIT_REST','https://api.bybit.com').rstrip('/')}/v5/market/tickers?category=linear&symbol={sym}");row=(((obj.get('result') or {}).get('list') or [{}])[0])
                return self._persist(str(asset_id),float(row.get('lastPrice') or 0),float(row.get('turnover24h') or 0),'cex:bybit',meta={'symbol':symbols['bybit']})
            if symbols.get('okx'):
                sym=urllib.parse.quote(symbols['okx']);obj=self._download(f"{os.getenv('OKX_REST','https://www.okx.com').rstrip('/')}/api/v5/market/ticker?instId={sym}");row=((obj.get('data') or [{}])[0])
                return self._persist(str(asset_id),float(row.get('last') or 0),None,'cex:okx',meta={'symbol':symbols['okx']})
        except Exception as exc:self.last_error=f'{type(exc).__name__}:{exc}'[:500]
        return self.brain.get_quote(str(asset_id),at)
    def get_history(self,asset_id,start,end,limit=5000):
        cached=self.brain.get_history(str(asset_id),start,end,limit)
        if len(cached)>=2:return cached
        symbols=self._symbols(str(asset_id));rows=[]
        try:
            if symbols.get('binance_spot'):
                sym=urllib.parse.quote(symbols['binance_spot']);url=f"{os.getenv('BINANCE_SPOT_REST','https://api.binance.com').rstrip('/')}/api/v3/klines?symbol={sym}&interval=1m&startTime={int(start*1000)}&endTime={int(end*1000)}&limit={min(1000,max(2,limit))}";obj=self._download(url)
                with connect(self.brain.path,self.settings) as con:
                    for x in obj if isinstance(obj,list) else []:
                        if not isinstance(x,list) or len(x)<7:continue
                        ts=float(x[6])/1000.0;price=float(x[4]);vol=float(x[7]) if len(x)>7 else float(x[5]);q=ReferenceQuote(str(asset_id),ts,ts,price,vol,'cex:binance_spot',.86);rows.append(q);con.execute("INSERT OR IGNORE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('cex:binance_spot',?,?,?,?,?,.86,?)",(asset_id,ts,ts,price,vol,json.dumps({'symbol':symbols['binance_spot'],'interval':'1m'})))
            elif symbols.get('bybit_spot'):
                sym=urllib.parse.quote(symbols['bybit_spot']);url=f"{os.getenv('BYBIT_REST','https://api.bybit.com').rstrip('/')}/v5/market/kline?category=spot&symbol={sym}&interval=1&start={int(start*1000)}&end={int(end*1000)}&limit={min(1000,max(2,limit))}";obj=self._download(url)
                with connect(self.brain.path,self.settings) as con:
                    for x in ((obj.get('result') or {}).get('list') or []):
                        if not isinstance(x,list) or len(x)<6:continue
                        ts=float(x[0])/1000.0+60.0;price=float(x[4]);vol=float(x[6]) if len(x)>6 else float(x[5]);q=ReferenceQuote(str(asset_id),ts,ts,price,vol,'cex:bybit_spot',.84);rows.append(q);con.execute("INSERT OR IGNORE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('cex:bybit_spot',?,?,?,?,?,.84,?)",(asset_id,ts,ts,price,vol,json.dumps({'symbol':symbols['bybit_spot'],'interval':'1m'})))
            elif symbols.get('mexc_spot'):
                sym=urllib.parse.quote(symbols['mexc_spot']);url=f"{os.getenv('MEXC_SPOT_REST','https://api.mexc.com').rstrip('/')}/api/v3/klines?symbol={sym}&interval=1m&startTime={int(start*1000)}&endTime={int(end*1000)}&limit={min(1000,max(2,limit))}";obj=self._download(url)
                with connect(self.brain.path,self.settings) as con:
                    for x in obj if isinstance(obj,list) else []:
                        if not isinstance(x,list) or len(x)<7:continue
                        ts=float(x[6])/1000.0;price=float(x[4]);vol=float(x[7]) if len(x)>7 else float(x[5]);q=ReferenceQuote(str(asset_id),ts,ts,price,vol,'cex:mexc_spot',.80);rows.append(q);con.execute("INSERT OR IGNORE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('cex:mexc_spot',?,?,?,?,?,.80,?)",(asset_id,ts,ts,price,vol,json.dumps({'symbol':symbols['mexc_spot'],'interval':'1m'})))
            elif symbols.get('binance'):
                sym=urllib.parse.quote(symbols['binance']);url=f"{os.getenv('BINANCE_FUTURES_REST','https://fapi.binance.com').rstrip('/')}/fapi/v1/klines?symbol={sym}&interval=1m&startTime={int(start*1000)}&endTime={int(end*1000)}&limit={min(1500,max(2,limit))}";obj=self._download(url)
                with connect(self.brain.path,self.settings) as con:
                    for x in obj if isinstance(obj,list) else []:
                        if not isinstance(x,list) or len(x)<7:continue
                        ts=float(x[6])/1000.0;price=float(x[4]);vol=float(x[7]) if len(x)>7 else float(x[5]);q=ReferenceQuote(str(asset_id),ts,ts,price,vol,'cex:binance',.84);rows.append(q);con.execute("INSERT OR IGNORE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('cex:binance',?,?,?,?,?,.84,?)",(asset_id,ts,ts,price,vol,json.dumps({'symbol':symbols['binance'],'interval':'1m'})))
            elif symbols.get('bybit'):
                sym=urllib.parse.quote(symbols['bybit']);url=f"{os.getenv('BYBIT_REST','https://api.bybit.com').rstrip('/')}/v5/market/kline?category=linear&symbol={sym}&interval=1&start={int(start*1000)}&end={int(end*1000)}&limit={min(1000,max(2,limit))}";obj=self._download(url)
                with connect(self.brain.path,self.settings) as con:
                    for x in ((obj.get('result') or {}).get('list') or []):
                        if not isinstance(x,list) or len(x)<6:continue
                        ts=float(x[0])/1000.0+60.0;price=float(x[4]);vol=float(x[6]) if len(x)>6 else float(x[5]);q=ReferenceQuote(str(asset_id),ts,ts,price,vol,'cex:bybit',.82);rows.append(q);con.execute("INSERT OR IGNORE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES('cex:bybit',?,?,?,?,?,.82,?)",(asset_id,ts,ts,price,vol,json.dumps({'symbol':symbols['bybit'],'interval':'1m'})))
            rows.sort(key=lambda x:x.observed_at);self.last_success=time.time();self.last_error=''
        except Exception as exc:self.last_error=f'{type(exc).__name__}:{exc}'[:500]
        return rows or cached
    def get_ohlcv(self,asset_id,start,end,limit=5000):return self.get_history(asset_id,start,end,limit)
    def get_volume(self,asset_id,at=None):q=self.get_quote(asset_id,at);return q.volume if q else None
    def get_orderbook(self,asset_id):return None
    def health(self):return {'provider':'cex_universe','ok':not self.last_error,'last_success':self.last_success,'last_error':self.last_error}

class ActiveAssetWatchlist:
    def __init__(self,settings:Settings):
        from .store import BrainStore
        from .observation import DynamicAssetUniverse
        self.settings=settings;self.path=BrainStore(settings).path
        DynamicAssetUniverse(settings)
    def activate(self,asset_id,reason,event_id=None,ttl_seconds=172800):
        now=time.time()
        with connect(self.path,self.settings) as con:con.execute("INSERT INTO active_asset_watchlist(asset_id,reason,event_id,activated_at,expires_at,last_polled_at,state) VALUES(?,?,?,?,?,0,'ACTIVE') ON CONFLICT(asset_id) DO UPDATE SET reason=excluded.reason,event_id=excluded.event_id,expires_at=MAX(expires_at,excluded.expires_at),state='ACTIVE'",(asset_id,reason,event_id,now,now+ttl_seconds))
    def active(self,now=None):
        with connect(self.path,self.settings) as con:return [dict(r) for r in con.execute("SELECT * FROM active_asset_watchlist WHERE state='ACTIVE' AND expires_at>? ORDER BY activated_at",(float(now or time.time()),)).fetchall()]
    def poll(self,provider:ReferenceMarketProvider,minimum_interval:float=60.0,limit:int=100)->dict[str,Any]:
        now=time.time();attempted=successful=0
        with connect(self.path,self.settings) as con:
            con.execute("UPDATE active_asset_watchlist SET state='EXPIRED' WHERE state='ACTIVE' AND expires_at<=?",(now,))
            rows=con.execute("SELECT * FROM active_asset_watchlist WHERE state='ACTIVE' AND expires_at>? AND last_polled_at<=? ORDER BY last_polled_at LIMIT ?",(now,now-max(1.0,minimum_interval),limit)).fetchall()
        for row in rows:
            attempted+=1;quote=provider.get_quote(str(row['asset_id']),now);successful+=int(quote is not None)
            with connect(self.path,self.settings) as con:con.execute("UPDATE active_asset_watchlist SET last_polled_at=? WHERE asset_id=?",(now,row['asset_id']))
        return {'attempted':attempted,'successful':successful,'polled_at':now}
class ReferenceProviderChain:
    def __init__(self,providers):self.providers=providers
    def get_history(self,asset_id,start,end,limit=5000):
        for provider in self.providers:
            rows=provider.get_history(asset_id,start,end,limit)
            if len(rows)>=2:return rows
        return []
    def get_quote(self,asset_id,at=None):
        for provider in self.providers:
            quote=provider.get_quote(asset_id,at)
            if quote:return quote
        return None
    def get_ohlcv(self,asset_id,start,end,limit=5000):return self.get_history(asset_id,start,end,limit)
    def get_volume(self,asset_id,at=None):q=self.get_quote(asset_id,at);return q.volume if q else None
    def get_orderbook(self,asset_id):
        for provider in self.providers:
            book=provider.get_orderbook(asset_id)
            if book:return book
        return None
    def health(self):return {'providers':[p.health() for p in self.providers]}
