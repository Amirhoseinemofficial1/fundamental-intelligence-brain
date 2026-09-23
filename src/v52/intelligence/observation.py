from __future__ import annotations
import asyncio, hashlib, json, re, time, urllib.parse, urllib.request
from dataclasses import dataclass
from typing import Any
from ..config import Settings
from ..storage.sqlite import connect
from ..storage.structured import StructuredStore
from ..timeutil import parse_datetime
from .semantic_routing import route_observation

@dataclass(frozen=True, slots=True)
class CanonicalObservation:
    observation_id: str; event_time: float; available_time: float; ingestion_time: float; source_id: str
    source_quality: float; asset_ids: tuple[str,...]; entity_ids: tuple[str,...]; observation_type: str
    value: float|None; unit: str; direction: str; confidence: float; freshness_seconds: float; quality: float
    raw_reference: str; metadata: dict[str,Any]; replay: bool=False
    provider_id: str=""; market: str="MULTI"; observation_family: str="GENERAL"

    def __post_init__(self)->None:
        if not self.event_time <= self.available_time <= self.ingestion_time:
            raise ValueError("canonical timestamp contract requires event_time <= available_time <= ingestion_time")

SEED_ASSETS: tuple[tuple[str,str,str,str,str],...] = (
    ('XAUUSD','XAUUSD','Gold','COMMODITY','XAUUSD'),('XAGUSD','XAGUSD','Silver','COMMODITY','XAGUSD'),
    ('COPPER','COPPER','Copper','COMMODITY','HG'),('WTI','WTI','WTI Crude','COMMODITY','CL'),('BRENT','BRENT','Brent Crude','COMMODITY','BRN'),
    ('EURUSD','EURUSD','EUR/USD','FOREX','EURUSD'),('GBPUSD','GBPUSD','GBP/USD','FOREX','GBPUSD'),
    ('USDJPY','USDJPY','USD/JPY','FOREX','USDJPY'),('USDCHF','USDCHF','USD/CHF','FOREX','USDCHF'),
    ('AUDUSD','AUDUSD','AUD/USD','FOREX','AUDUSD'),('NZDUSD','NZDUSD','NZD/USD','FOREX','NZDUSD'),('USDCAD','USDCAD','USD/CAD','FOREX','USDCAD'),
    ('DXY','DXY','US Dollar Index','INDEX','DXY'),('SPX','SPX','S&P 500','INDEX','SPX'),('NDX','NDX','Nasdaq 100','INDEX','NDX'),
    ('DJI','DJI','Dow Jones Industrial Average','INDEX','DJI'),('DAX','DAX','DAX 40','INDEX','DAX'),('FTSE','FTSE','FTSE 100','INDEX','FTSE'),
    ('equity:XNAS:AAPL','AAPL','Apple','EQUITY','AAPL'),('equity:XNAS:NVDA','NVDA','NVIDIA','EQUITY','NVDA'),('equity:XNAS:TSLA','TSLA','Tesla','EQUITY','TSLA'),('equity:XNAS:MSFT','MSFT','Microsoft','EQUITY','MSFT'),('equity:XNAS:PLTR','PLTR','Palantir','EQUITY','PLTR'),
    ('BTC','BTC','Bitcoin','CRYPTO','BTCUSDT'),('ETH','ETH','Ethereum','CRYPTO','ETHUSDT'),('SOL','SOL','Solana','TOKEN','SOLUSDT'),('SUI','SUI','Sui','TOKEN','SUIUSDT'),
)
ALIASES={'APPLE':'equity:XNAS:AAPL','NVIDIA':'equity:XNAS:NVDA','TESLA':'equity:XNAS:TSLA','MICROSOFT':'equity:XNAS:MSFT','PALANTIR':'equity:XNAS:PLTR','BITCOIN':'BTC','ETHEREUM':'ETH','SOLANA':'SOL','SUI NETWORK':'SUI','COINBASE WILL LIST SUI':'SUI','GOLD':'XAUUSD','SILVER':'XAGUSD','CRUDE':'WTI'}
EXCHANGE_ALIASES={'NYSE':'XNYS','NASDAQ':'XNAS','LSE':'XLON','LONDON':'XLON','TSX':'XTSE','TSE':'XTKS','TOKYO':'XTKS','HKEX':'XHKG','ASX':'XASX','XETRA':'XETR','EURONEXT PARIS':'XPAR','EURONEXT AMSTERDAM':'XAMS','BORSA ITALIANA':'XMIL','BME':'XMAD','SIX':'XSWX','NSE':'XNSE','BSE':'XBOM','SGX':'XSES','KRX':'XKRX','TAIWAN':'XTAI','B3':'BVMF','JSE':'XJSE','NZX':'XNZE'}
PUBLIC_CHART_SUFFIXES={'XLON':'.L','XTSE':'.TO','XTSX':'.V','XTKS':'.T','XHKG':'.HK','XASX':'.AX','XETR':'.DE','XFRA':'.F','XPAR':'.PA','XAMS':'.AS','XBRU':'.BR','XMIL':'.MI','XMAD':'.MC','XLIS':'.LS','XSWX':'.SW','XSTO':'.ST','XCSE':'.CO','XOSL':'.OL','XHEL':'.HE','XWAR':'.WA','XPRA':'.PR','XWBO':'.VI','XNSE':'.NS','XBOM':'.BO','XKRX':'.KS','XKOS':'.KQ','XSES':'.SI','XTAI':'.TW','ROCO':'.TWO','BVMF':'.SA','XMEX':'.MX','XJSE':'.JO','XNZE':'.NZ'}

def semantic_direction(family:str,payload:dict[str,Any],value:float|None)->str:
    """Interpret change/surprise semantics; an absolute positive level is never directional."""
    family=str(family or 'GENERAL').upper()
    def number(*keys:str)->float|None:
        for key in keys:
            if payload.get(key) is not None:
                try:return float(str(payload[key]).replace(',','').replace('%',''))
                except (TypeError,ValueError):continue
        return None
    actual=number('actual');consensus=number('consensus','forecast');previous=number('previous')
    change=number('change','delta','revision','guidance_delta','rate_change','z_score','zscore')
    delta=(actual-consensus) if actual is not None and consensus is not None else change
    if family=='MACRO':
        return 'UP' if delta is not None and delta>0 else 'DOWN' if delta is not None and delta<0 else 'NEUTRAL'
    if family=='DERIVATIVES':
        return 'UP' if change is not None and change>0 else 'DOWN' if change is not None and change<0 else 'NEUTRAL'
    if family=='ONCHAIN':
        flow=number('net_flow','netflow','flow_delta','accumulation_delta')
        counterparty=str(payload.get('counterparty') or payload.get('destination') or '').lower()
        if flow is None:return 'NEUTRAL'
        if 'exchange' in counterparty:return 'DOWN' if flow>0 else 'UP'
        return 'UP' if flow>0 else 'DOWN' if flow<0 else 'NEUTRAL'
    if family=='CORPORATE':
        return 'UP' if delta is not None and delta>0 else 'DOWN' if delta is not None and delta<0 else 'NEUTRAL'
    if family=='ENERGY':
        inventory_surprise=number('inventory_surprise')
        production_change=number('production_change')
        driver=inventory_surprise if inventory_surprise is not None else production_change
        return 'DOWN' if driver is not None and driver>0 else 'UP' if driver is not None and driver<0 else 'NEUTRAL'
    return 'NEUTRAL'

class DynamicAssetUniverse:
    def __init__(self, settings: Settings): self.settings=settings; self.path=settings.data_dir/'brain'/'intelligence.sqlite3'; self.bootstrap()
    def bootstrap(self)->None:
        with connect(self.path,self.settings) as con:
            now=time.time()
            for aid,symbol,name,klass,market in SEED_ASSETS:
                metadata={}
                if klass=='EQUITY':
                    exchange=aid.split(':',2)[1]
                    metadata={'exchange':exchange,'provider_symbols':{'public_chart':f"{symbol}{PUBLIC_CHART_SUFFIXES.get(exchange,'')}",'canonical':f'{exchange}:{symbol}'},'resolution':'CANONICAL_SEED'}
                con.execute("INSERT INTO assets(asset_id,symbol,name,asset_class,market_symbol,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET symbol=excluded.symbol,name=excluded.name,asset_class=excluded.asset_class,market_symbol=excluded.market_symbol,metadata_json=CASE WHEN assets.metadata_json='{}' THEN excluded.metadata_json ELSE assets.metadata_json END,updated_at=excluded.updated_at",(aid,symbol,name,klass,market,json.dumps(metadata),now))
                if klass=='EQUITY':
                    exchange=aid.split(':',2)[1];provider_symbol=(metadata.get('provider_symbols') or {}).get('public_chart',symbol)
                    con.execute("INSERT OR REPLACE INTO provider_symbols(asset_id,provider,provider_symbol,exchange,updated_at) VALUES(?,?,?,?,?)",(aid,'public_chart',provider_symbol,exchange,now))
                    for alias in {symbol,name.upper()}:
                        con.execute("INSERT OR REPLACE INTO asset_aliases(alias,asset_id,source,confidence,updated_at) VALUES(?,?,'canonical_seed',1.0,?)",(alias,aid,now))
            # Keep legacy ticker-only equity rows readable but never selectable for new inference.
            # A fresh V57 database contains only canonical exchange-aware seed identities.
            legacy={'AAPL':'equity:XNAS:AAPL','NVDA':'equity:XNAS:NVDA','TSLA':'equity:XNAS:TSLA','MSFT':'equity:XNAS:MSFT','PLTR':'equity:XNAS:PLTR'}
            for old_id,new_id in legacy.items():
                row=con.execute("SELECT metadata_json FROM assets WHERE asset_id=?",(old_id,)).fetchone()
                if row:
                    try:meta=json.loads(row['metadata_json'] or '{}')
                    except (TypeError,ValueError):meta={}
                    meta['legacy_redirect']=new_id
                    con.execute("UPDATE assets SET metadata_json=?,updated_at=? WHERE asset_id=?",(json.dumps(meta),now,old_id))
                    con.execute("INSERT OR REPLACE INTO asset_aliases(alias,asset_id,source,confidence,updated_at) VALUES(?,?,'legacy_redirect',1.0,?)",(old_id,new_id,now))
    @staticmethod
    def _candidate_asset_id(candidate:dict[str,Any])->tuple[str,str,str,str,str,dict[str,Any]]|None:
        symbol=str(candidate.get('ticker') or candidate.get('token_symbol') or candidate.get('symbol') or '').upper().strip('$ ')
        if not re.fullmatch(r'[A-Z][A-Z0-9.-]{0,19}',symbol):return None
        chain=str(candidate.get('chain') or '').strip().lower();contract=str(candidate.get('contract') or candidate.get('contract_address') or '').strip()
        exchange=str(candidate.get('exchange') or '').strip()
        if chain or contract:
            if not chain or not contract:return None
            asset_id=f'{chain}:{contract.lower() if chain not in {"solana"} else contract}'
            klass='TOKEN';market=str((candidate.get('market_pairs') or [symbol])[0] if isinstance(candidate.get('market_pairs'),list) else candidate.get('market_pair') or symbol)
            name=str(candidate.get('project') or candidate.get('token_name') or candidate.get('name') or symbol)
        elif exchange:
            normalized_exchange=EXCHANGE_ALIASES.get(exchange.upper(),exchange.upper())
            # Canonical equity identity is exchange-aware from the first listing;
            # a ticker is only an alias and can never overwrite another venue.
            asset_id=f'equity:{normalized_exchange}:{symbol}';exchange=normalized_exchange;klass='EQUITY';market=symbol;name=str(candidate.get('company') or candidate.get('issuer') or candidate.get('name') or symbol)
        else:return None
        metadata={k:candidate.get(k) for k in ('exchange','sector','industry','chain','contract','contract_address','project','market_pairs','aliases') if candidate.get(k) is not None}
        if klass=='EQUITY':
            suffix=PUBLIC_CHART_SUFFIXES.get(exchange,'')
            metadata['exchange']=exchange;metadata['country']=str(candidate.get('country') or '')
            supplied=candidate.get('provider_symbols') if isinstance(candidate.get('provider_symbols'),dict) else {}
            metadata['provider_symbols']={**{'public_chart':f'{symbol}{suffix}','canonical':f'{exchange}:{symbol}'},**{str(k):str(v) for k,v in supplied.items()}}
        metadata['resolution']='DETERMINISTIC_VALIDATED_DYNAMIC'
        return asset_id,symbol,name,klass,market,metadata

    @staticmethod
    def _candidates(payload:dict[str,Any])->list[dict[str,Any]]:
        candidates=[]
        for key in ('asset_candidates','entity_candidates','token_candidates'):
            value=payload.get(key)
            if isinstance(value,list):candidates.extend(x for x in value if isinstance(x,dict))
        if any(key in payload for key in ('ticker','token_symbol','symbol','contract','contract_address')):candidates.append(payload)
        return candidates

    def discover(self,payload:Any)->tuple[str,...]:
        # Chain/contract metadata is intentionally excluded from affected-asset text inference.
        def strip_identity_metadata(value:Any)->Any:
            if isinstance(value,dict):return {k:strip_identity_metadata(v) for k,v in value.items() if k not in {'chain','contract','contract_address','market_pairs'}}
            if isinstance(value,list):return [strip_identity_metadata(x) for x in value]
            return value
        searchable=strip_identity_metadata(payload)
        raw_text=json.dumps(searchable,ensure_ascii=False)
        text=raw_text.upper(); found=set()
        cashtags={x.upper() for x in re.findall(r'\$([A-Z][A-Z0-9.-]{0,19})',text)}
        crypto_context=bool(re.search(r'\b(CRYPTO|TOKEN|COIN|BLOCKCHAIN|PROTOCOL|AIRDROP|DEX|CEX|FUNDING|LIQUIDATION|LISTING|DELISTING|PERPETUAL)\b',text))
        uppercase_tickers={x.upper() for x in re.findall(r'(?<![A-Za-z0-9])([A-Z][A-Z0-9.-]{1,19})(?![A-Za-z0-9])',raw_text)}
        explicit_tickers=uppercase_tickers if crypto_context else set()
        equity_context=bool(re.search(r'\b(EARNINGS|REVENUE|EPS|GUIDANCE|SHARES|STOCK|NASDAQ|NYSE|SEC FILING|DIVIDEND|BUYBACK)\b',text))
        generic_token_terms={'TOKEN','COIN','CRYPTO','BLOCKCHAIN','PROTOCOL','AIRDROP','DEX','CEX','FUNDING','LIQUIDATION','LISTING','DELISTING','PERPETUAL','UPDATE','NEWS'}
        generic_equity_terms={'HELP','DATA','NEWS','MARKET','GLOBAL','INDEX','UPDATE','REPORT','GROUP','HOLDINGS','SHARES','STOCK'}
        def mentioned(value:Any)->bool:
            phrase=str(value or '').strip().upper()
            return bool(phrase and re.search(rf'(?<![A-Z0-9]){re.escape(phrase)}(?![A-Z0-9])',text))
        with connect(self.path,self.settings) as con:
            has_candidates=isinstance(payload,dict) and bool(self._candidates(payload))
            candidate_symbols={str(x.get('ticker') or x.get('token_symbol') or x.get('symbol') or '').upper() for x in self._candidates(payload)} if isinstance(payload,dict) else set()
            # Contract/equity/seed assets remain a small semantic set. CEX-wide
            # aggregate assets use SQL token lookup below, avoiding an O(N_tokens)
            # regex scan for every story after the universe grows to thousands.
            rows=con.execute("SELECT asset_id,symbol,name,asset_class,metadata_json FROM assets WHERE asset_id NOT LIKE 'crypto:%'").fetchall()
            for row in rows:
                try: row_meta=json.loads(row['metadata_json'] or '{}')
                except (TypeError,ValueError): row_meta={}
                if row_meta.get('legacy_redirect'): continue
                if has_candidates and str(row['asset_class'])=='TOKEN':continue
                if has_candidates and str(row['symbol']).upper() in candidate_symbols:continue
                if str(row['asset_class'])=='TOKEN':
                    symbol=str(row['symbol']).upper();name=str(row['name']).strip().upper()
                    project_name_match=name!=symbol and len(name)>=4 and name not in generic_token_terms and mentioned(name)
                    if symbol in cashtags or (crypto_context and (symbol in explicit_tickers or project_name_match)):found.add(str(row['asset_id']))
                    continue
                if str(row['asset_class'])=='EQUITY':
                    symbol=str(row['symbol']).upper();name=str(row['name']).strip().upper()
                    symbol_match=symbol in cashtags or (equity_context and symbol in uppercase_tickers)
                    name_match=name!=symbol and len(name)>=4 and name not in generic_equity_terms and mentioned(name)
                    if symbol_match or name_match:found.add(str(row['asset_id']))
                    continue
                if mentioned(row['symbol']) or mentioned(row['name']):found.add(str(row['asset_id']))
            for alias,aid in ALIASES.items():
                if mentioned(alias):found.add(aid)
            # Fast exact lookup for the large CEX aggregate universe. Restrict plain
            # ticker inference to crypto/token contexts; cashtags are always explicit.
            cex_tokens=sorted(cashtags|(explicit_tickers-generic_token_terms))[:128]
            if cex_tokens:
                bind_marks=','.join('?' for _ in cex_tokens)
                for cex_row in con.execute(f"SELECT asset_id FROM assets WHERE asset_id LIKE 'crypto:%' AND UPPER(symbol) IN ({bind_marks})",tuple(cex_tokens)).fetchall():
                    found.add(str(cex_row['asset_id']))
            for alias_row in con.execute('SELECT aa.alias,aa.asset_id,a.asset_class,a.symbol FROM asset_aliases aa JOIN assets a ON a.asset_id=aa.asset_id').fetchall():
                alias=str(alias_row['alias']).upper()
                if has_candidates and alias in candidate_symbols:continue
                if not alias:continue
                if str(alias_row['asset_class'])=='TOKEN':
                    symbol=str(alias_row['symbol']).upper()
                    explicit_symbol=alias==symbol and alias in explicit_tickers
                    project_alias=alias!=symbol and len(alias)>=4 and alias not in generic_token_terms and mentioned(alias)
                    if alias in cashtags or (crypto_context and (explicit_symbol or project_alias)):found.add(str(alias_row['asset_id']))
                    continue
                if str(alias_row['asset_class'])=='EQUITY':
                    symbol=str(alias_row['symbol']).upper()
                    explicit_symbol=alias==symbol and (symbol in cashtags or (equity_context and symbol in uppercase_tickers))
                    company_alias=alias!=symbol and len(alias)>=4 and alias not in generic_equity_terms and mentioned(alias)
                    if explicit_symbol or company_alias:found.add(str(alias_row['asset_id']))
                    continue
                if re.search(rf'(?<![A-Z0-9]){re.escape(alias)}(?![A-Z0-9])',text): found.add(str(alias_row['asset_id']))
            if isinstance(payload,dict):
                for candidate in self._candidates(payload):
                    resolved=self._candidate_asset_id(candidate)
                    if not resolved:
                        symbol=str(candidate.get('ticker') or candidate.get('token_symbol') or candidate.get('symbol') or '').upper().strip('$ ')
                        if symbol:
                            matches=[]
                            for existing in con.execute('SELECT asset_id,metadata_json FROM assets WHERE UPPER(symbol)=?',(symbol,)).fetchall():
                                try:meta=json.loads(existing['metadata_json'] or '{}')
                                except (TypeError,ValueError):meta={}
                                if not meta.get('legacy_redirect'):matches.append(str(existing['asset_id']))
                            if len(matches)==1:found.add(matches[0])
                        continue
                    asset_id,symbol,name,klass,market,metadata=resolved;now=time.time()
                    con.execute("INSERT INTO assets(asset_id,symbol,name,asset_class,market_symbol,metadata_json,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(asset_id) DO UPDATE SET symbol=excluded.symbol,name=excluded.name,asset_class=excluded.asset_class,market_symbol=excluded.market_symbol,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at",(asset_id,symbol,name,klass,market,json.dumps(metadata),now))
                    for provider,provider_symbol in (metadata.get('provider_symbols') or {}).items():
                        con.execute("INSERT OR REPLACE INTO provider_symbols(asset_id,provider,provider_symbol,exchange,updated_at) VALUES(?,?,?,?,?)",(asset_id,provider,str(provider_symbol),str(metadata.get('exchange') or ''),now))
                    aliases=set(str(x).strip().upper() for x in (candidate.get('aliases') or []) if str(x).strip())|{symbol,name.upper()}
                    for alias in aliases:con.execute("INSERT OR REPLACE INTO asset_aliases(alias,asset_id,source,confidence,updated_at) VALUES(?,?,'dynamic_resolver',.95,?)",(alias,asset_id,now))
                    found.add(asset_id)
        return tuple(sorted(found))

    def resolve_directory(self,query:str,provider:Any)->tuple[str,...]:
        """Resolve an unknown issuer/token through a deterministic directory API.

        ``provider`` is an injected registry adapter, not an LLM.  Its response is
        passed through exactly the same validation and registration path.
        """
        response=provider.lookup(query)
        candidates=response if isinstance(response,list) else [response]
        return self.discover({'asset_candidates':[x for x in candidates if isinstance(x,dict)]})

    def resolve_public_directory(self,query:str)->tuple[str,...]:return self.resolve_directory(query,PublicEquityDirectoryProvider(self.settings))

    def provider_symbol(self,asset_id:str,provider:str='public_chart')->str|None:
        with connect(self.path,self.settings) as con:
            row=con.execute("SELECT provider_symbol FROM provider_symbols WHERE asset_id=? AND provider=?",(asset_id,provider)).fetchone()
        return str(row['provider_symbol']) if row else None

class PublicEquityDirectoryProvider:
    """No-key global issuer directory; it never depends on the semantic LLM."""
    def __init__(self,settings:Settings):self.settings=settings
    def lookup(self,query:str)->list[dict[str,Any]]:
        url=f"https://query1.finance.yahoo.com/v1/finance/search?q={urllib.parse.quote(query)}&quotesCount=10&newsCount=0"
        request=urllib.request.Request(url,headers={'User-Agent':self.settings.user_agent,'Accept':'application/json'})
        with urllib.request.urlopen(request,timeout=6) as response:data=json.loads(response.read(2_000_000))
        result=[]
        for quote in data.get('quotes') or []:
            if str(quote.get('quoteType') or '').upper() not in {'EQUITY','ETF'}:continue
            provider_symbol=str(quote.get('symbol') or '').upper();ticker=provider_symbol.split('.',1)[0]
            if not ticker:continue
            result.append({'ticker':ticker,'company':quote.get('longname') or quote.get('shortname') or ticker,'exchange':quote.get('exchange') or quote.get('exchDisp'),'country':quote.get('region') or '','sector':quote.get('sector') or '','industry':quote.get('industry') or '','provider_symbols':{'public_chart':provider_symbol},'aliases':[x for x in (quote.get('longname'),quote.get('shortname')) if x]})
        return result

class PublicTokenDirectoryProvider:
    """Deterministic no-key token/pool directory backed by DEX Screener.

    Returned candidates are still validated by ``DynamicAssetUniverse``; this
    adapter cannot directly create an asset.
    """
    def __init__(self,settings:Settings):self.settings=settings
    def lookup(self,query:str)->list[dict[str,Any]]:
        url=f"https://api.dexscreener.com/latest/dex/search?q={urllib.parse.quote(query)}"
        request=urllib.request.Request(url,headers={'User-Agent':self.settings.user_agent,'Accept':'application/json'})
        with urllib.request.urlopen(request,timeout=6) as response:data=json.loads(response.read(3_000_000))
        result=[];seen=set()
        for pair in data.get('pairs') or []:
            base=pair.get('baseToken') or {};chain=str(pair.get('chainId') or '').lower();contract=str(base.get('address') or '')
            key=(chain,contract)
            if not chain or not contract or key in seen:continue
            seen.add(key);symbol=str(base.get('symbol') or '').upper()
            result.append({'token_symbol':symbol,'token_name':base.get('name') or symbol,'project':base.get('name') or symbol,
                'chain':chain,'contract':contract,'market_pairs':[pair.get('pairAddress') or f"{symbol}/{(pair.get('quoteToken') or {}).get('symbol','QUOTE')}"],
                'aliases':[x for x in (base.get('name'),symbol) if x]})
        return result

def _epoch(value:Any, fallback:float)->float:
    if isinstance(value,(int,float)) and float(value)>0: return float(value)/1000 if float(value)>1e12 else float(value)
    if isinstance(value,str):
        parsed=parse_datetime(value)
        if parsed: return parsed.timestamp()
    return fallback
def _first_number(payload:Any)->float|None:
    keys=('value','actual','price','close','rate','amount','volume','open_interest','funding_rate','netflow','net_flow','balance')
    if isinstance(payload,dict):
        for key in keys:
            try:
                if key in payload and payload[key] is not None: return float(str(payload[key]).replace(',','').replace('%',''))
            except ValueError: continue
        for value in payload.values():
            result=_first_number(value)
            if result is not None:return result
    if isinstance(payload,list):
        for value in payload[:20]:
            result=_first_number(value)
            if result is not None:return result
    return None

class ObservationBridge:
    BATCH_KEYS=("data","results","items","rows","records","quotes","assets")
    # Retain these unverified provider snapshots in the structured source DB
    # for audit, without copying thousands of uninformative rows into the
    # canonical learning stream.
    RAW_ONLY_RECORD_TYPES=frozenset({
        'PREDICTION_MARKET_STATE', 'PREDICTION_MARKET_UPDATE',
        'PREDICTION_MARKET_REALTIME', 'SEC_CURRENT_FILING',
        'FTD_RECORD', 'FTD_DATASET_UPDATE',
        'POSITIONING_TFF', 'POSITIONING_DISAGGREGATED',
        'POSITIONING_LEGACY', 'POSITIONING_CIT',
    })
    def __init__(self,settings:Settings): self.settings=settings; self.structured=StructuredStore(settings); self.path=settings.data_dir/'brain'/'intelligence.sqlite3'; self.assets=DynamicAssetUniverse(settings)

    @classmethod
    def _expand_payload_rows(cls,payload:Any,depth:int=0,parent:dict[str,Any]|None=None)->list[dict[str,Any]]:
        """Expand provider batch envelopes into independent canonical rows.

        Only lists of mapping objects are exploded; scalar arrays such as candle
        timestamps/prices remain one record. Parent scalar metadata (timestamps,
        units, exchange, chain, etc.) is inherited by each child.
        """
        if depth>4:return [payload] if isinstance(payload,dict) else []
        if isinstance(payload,list):
            if payload and all(isinstance(x,dict) for x in payload):
                out=[]
                for item in payload:
                    merged={**(parent or {}),**item}
                    out.extend(cls._expand_payload_rows(merged,depth+1,None))
                return out
            return []
        if not isinstance(payload,dict):return []
        inherited={k:v for k,v in payload.items() if not isinstance(v,(list,dict))}
        if parent:inherited={**parent,**inherited}
        for key in cls.BATCH_KEYS:
            value=payload.get(key)
            if isinstance(value,list) and value and all(isinstance(x,dict) for x in value):
                out=[]
                for item in value:
                    out.extend(cls._expand_payload_rows({**inherited,**item},depth+1,None))
                return out
            if isinstance(value,dict):
                nested=cls._expand_payload_rows(value,depth+1,inherited)
                if nested:return nested
        return [{**inherited,**payload}]

    def _normalize_payload(self,row:dict[str,Any],payload:dict[str,Any],replay:bool,index:int,total:int)->CanonicalObservation:
        ingested=float(row['observed_at']);event=_epoch(payload.get('event_time') or payload.get('timestamp') or payload.get('time'),ingested)
        available=_epoch(payload.get('available_time'),ingested)
        # Provider event timestamps can legitimately be after the local snapshot clock
        # because of clock skew. Never move ingestion into the future; clamp only small
        # provider skew, otherwise reject the impossible record.
        if event>ingested and event-ingested<=5:event=ingested
        if available>ingested and available-ingested<=5:available=ingested
        if not event <= available <= ingested:raise ValueError("structured observation violates event<=available<=ingestion contract")
        provider=str(row['provider']);quality=.92 if any(x in provider.lower() for x in ('fed','sec','eia','opec','official','bls','cftc','treasury','newyorkfed','ishares','world_gold')) else .72
        value=_first_number(payload);assets=self.assets.discover(payload);typ=str(payload.get('record_type') or row['record_type']).upper()
        route=route_observation(provider,str(row['lane']),typ,str(payload.get('unit') or ''),payload)
        child_hash=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()[:16]
        oid=f"structured:{row['id']}:{row['payload_hash']}:{index}:{child_hash}" if total>1 else 'structured:'+str(row['id'])+':'+str(row['payload_hash'])
        direction=semantic_direction(route.observation_family,payload,value)
        metadata={'lane':row['lane'],'payload':payload,'batch_index':index,'batch_size':total,'_structured_snapshot_id':int(row['id']),'semantic_route':{'event_type':route.event_type,'domain':route.domain,'subtype':route.subtype,'experts':route.experts,'horizons':route.horizons}}
        return CanonicalObservation(oid,event,available,ingested,provider,quality,assets,(),typ,value,str(payload.get('unit') or ''),direction,.85,max(0,ingested-event),quality,f"structured:{row['id']}:{index}",metadata,replay,provider,','.join(assets) or 'MULTI',route.observation_family)

    def normalize_many(self,row:dict[str,Any],replay:bool=False)->list[CanonicalObservation]:
        payload=json.loads(row['payload_json'])
        expanded=self._expand_payload_rows(payload)
        if not expanded and isinstance(payload,dict):expanded=[payload]
        if not expanded:return []
        return [self._normalize_payload(row,item,replay,index,len(expanded)) for index,item in enumerate(expanded)]

    def normalize(self,row:dict[str,Any],replay:bool=False)->CanonicalObservation:
        observations=self.normalize_many(row,replay)
        if not observations:raise ValueError('structured payload did not contain a canonicalizable record')
        if len(observations)>1:raise ValueError('structured batch payload requires normalize_many(); refusing silent row collapse')
        return observations[0]

    def persist_sync(self,o:CanonicalObservation)->bool:
        with connect(self.path,self.settings) as con:
            cur=con.execute("""INSERT OR IGNORE INTO canonical_observations(observation_id,event_time,available_time,ingestion_time,source_id,source_quality,asset_ids_json,entity_ids_json,observation_type,value,unit,direction,confidence,freshness_seconds,quality,raw_reference,metadata_json,replay,provider_id,market,observation_family) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
              (o.observation_id,o.event_time,o.available_time,o.ingestion_time,o.source_id,o.source_quality,json.dumps(o.asset_ids),json.dumps(o.entity_ids),o.observation_type,o.value,o.unit,o.direction,o.confidence,o.freshness_seconds,o.quality,o.raw_reference,json.dumps(o.metadata,default=str),int(o.replay),o.provider_id or o.source_id,o.market,o.observation_family))
            inserted=cur.rowcount>0
            if inserted and o.value is not None and any(x in o.observation_type for x in ('PRICE','QUOTE','OHLC','TRADE','CLOSE')):
                for asset_id in o.asset_ids:
                    con.execute("INSERT OR IGNORE INTO reference_prices(provider,asset_id,observed_at,available_at,price,volume,quality,metadata_json) VALUES(?,?,?,?,?,NULL,?,?)",(o.source_id,asset_id,o.event_time,o.available_time,o.value,o.quality,json.dumps({'observation_id':o.observation_id})))
            return inserted
    async def drain(self,cursor:int,limit:int=500,replay:bool=False)->tuple[int,list[CanonicalObservation]]:
        rows=await self.structured.snapshots_after(cursor,limit);out=[]
        for row in rows:
            if str(row['record_type']).upper() in self.RAW_ONLY_RECORD_TYPES:
                cursor=int(row['id'])
                continue
            for obs in self.normalize_many(row,replay):
                # Persistence is idempotent. The structured cursor is responsible
                # for processing progress, therefore already-persisted observations
                # must still be returned after an interrupted shutdown/restart.
                await asyncio.to_thread(self.persist_sync,obs)
                out.append(obs)
            cursor=int(row['id'])
        return cursor,out
