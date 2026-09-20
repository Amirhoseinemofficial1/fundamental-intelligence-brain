"""Auditable 1:1 barrier outcomes and delayed contextual-bandit feedback.

Candles are evidence for labels, never backdated prediction-time features.
An ambiguous candle or missing minute is not a win or loss for training.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import time
import urllib.parse
from dataclasses import asdict, dataclass

from ..storage.sqlite import connect, transaction

LEVELS = (.001, .003, .005, .01, .03, .05, .10)


@dataclass(frozen=True)
class Candle:
    start: float
    end: float
    open: float
    high: float
    low: float
    close: float
    volume: float | None = None

    def valid(self):
        prices=(self.open,self.high,self.low,self.close)
        return (all(math.isfinite(x) and x>0 for x in prices)
                and math.isfinite(self.start) and math.isfinite(self.end)
                and self.end>self.start and self.low<=min(self.open,self.close)
                and self.high>=max(self.open,self.close))


def evaluate(candles, decision_at, due_at, side, cost_bps=10.0):
    """Enter at first complete minute open after decision; stop on first touch.

    Labels preserve raw MFE/MAE beyond the first touch over the full horizon.
    A gap makes the whole path unobservable. A candle touching both barriers
    is ambiguous because OHLC does not contain intrabar order. Costs are an
    explicit round-trip estimate, not a claim of actual execution.
    """
    if side not in (-1,1) or not math.isfinite(cost_bps) or cost_bps<0:
        raise ValueError('invalid side or costs')
    window=sorted((c for c in candles if c.start>=decision_at and c.end<=due_at),key=lambda c:c.start)
    if not window or any(not c.valid() for c in window):return None
    if window[0].start-decision_at>60.001 or due_at-window[-1].end>60.001:return None
    if any(abs(a.end-b.start)>.01 for a,b in zip(window,window[1:])):return None
    if any(abs(c.end-c.start-60)>.01 for c in window):return None
    entry=window[0].open
    favorable=[(c.high/entry-1) if side==1 else (1-c.low/entry) for c in window]
    adverse=[(c.low/entry-1) if side==1 else (1-c.high/entry) for c in window]
    mfe=max(0.,max(favorable));mae=min(0.,min(adverse))
    results=[]
    for level in LEVELS:
        state='TIMEOUT';hit_at=None;gross=(window[-1].close/entry-1)*side
        for i,c in enumerate(window):
            opening=(c.open/entry-1)*side
            # A gap through a stop executes at the observed open, not at an invented fill.
            if opening<=-level+1e-12:state='LOSS';gross=opening;hit_at=c.start;break
            if opening>=level-1e-12:state='WIN';gross=level;hit_at=c.start;break
            win=favorable[i]>=level-1e-12;loss=adverse[i]<=-level+1e-12
            if win and loss:state='AMBIGUOUS';gross=None;hit_at=c.end;break
            if win or loss:
                state='WIN' if win else 'LOSS';gross=level if win else -level;hit_at=c.end;break
        net=None if gross is None else gross-cost_bps/10000
        results.append(dict(level=level,state=state,entry_price=entry,entry_at=window[0].start,
                            take_profit=entry*(1+side*level),stop_loss=entry*(1-side*level),
                            hit_at=hit_at,gross_return=gross,net_return=net,
                            reward_r=None if net is None else net/level,mfe=mfe,mae=mae,
                            time_to_mfe=window[favorable.index(max(favorable))].end-window[0].start,
                            time_to_mae=window[adverse.index(min(adverse))].end-window[0].start,
                            samples=len(window),cost_bps=cost_bps))
    return results


SCHEMA = '''
CREATE TABLE IF NOT EXISTS signal_outcomes(
 id INTEGER PRIMARY KEY,feature_id TEXT NOT NULL UNIQUE,prediction_id INTEGER NOT NULL,
 event_id INTEGER NOT NULL,asset_id TEXT NOT NULL,horizon_seconds INTEGER NOT NULL,
 return_value REAL NOT NULL,mfe REAL NOT NULL,mae REAL NOT NULL,volatility REAL NOT NULL,
 time_to_peak_seconds REAL NOT NULL,time_to_mfe_seconds REAL NOT NULL,time_to_mae_seconds REAL NOT NULL,
 measured_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS neural_bandit_state(id INTEGER PRIMARY KEY CHECK(id=1),state_json TEXT NOT NULL,updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS neural_bandit_updates(signal_id INTEGER NOT NULL,level REAL NOT NULL,version INTEGER NOT NULL,reward REAL NOT NULL,feature_id TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(signal_id,level));
CREATE TABLE IF NOT EXISTS evaluation_candles(
 asset_id TEXT NOT NULL,provider TEXT NOT NULL,start REAL NOT NULL,end REAL NOT NULL,
 open REAL NOT NULL,high REAL NOT NULL,low REAL NOT NULL,close REAL NOT NULL,volume REAL,
 fetched_at REAL NOT NULL,PRIMARY KEY(asset_id,provider,start));
CREATE TABLE IF NOT EXISTS signal_evaluations(
 signal_id INTEGER PRIMARY KEY REFERENCES signals(id),state TEXT NOT NULL DEFAULT 'PENDING',
 attempts INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0,
 last_error TEXT,completed_at REAL,provider TEXT,path_sha256 TEXT);
CREATE INDEX IF NOT EXISTS idx_evaluations_due ON signal_evaluations(state,next_attempt);
CREATE TABLE IF NOT EXISTS signal_barriers(
 signal_id INTEGER NOT NULL REFERENCES signals(id),level REAL NOT NULL,
 state TEXT NOT NULL,net_return REAL,reward_r REAL,detail_json TEXT NOT NULL,
 learned_at REAL,PRIMARY KEY(signal_id,level));
CREATE TABLE IF NOT EXISTS bandit_feedback(
 event_id INTEGER NOT NULL,asset_id TEXT NOT NULL,horizon INTEGER NOT NULL,
 side INTEGER NOT NULL,level REAL NOT NULL,signal_id INTEGER NOT NULL,
 PRIMARY KEY(event_id,asset_id,horizon,side,level));
CREATE TABLE IF NOT EXISTS contextual_bandit(
 context TEXT NOT NULL,level REAL NOT NULL,samples INTEGER NOT NULL,wins INTEGER NOT NULL,
 losses INTEGER NOT NULL,timeouts INTEGER NOT NULL,reward_mean REAL NOT NULL,
 reward_ewma REAL NOT NULL,updated_at REAL NOT NULL,PRIMARY KEY(context,level));
'''


class CandleEvidence:
    def __init__(self,settings,path):
        from .reference_market import CexUniverseMarketProvider,PublicChartMarketProvider
        self.settings=settings;self.path=path
        self.cex=CexUniverseMarketProvider(settings);self.public=PublicChartMarketProvider(settings)

    def history(self,asset_id,start,end):
        # Read cache by venue: never mix prices from different instruments/venues.
        with connect(self.path,self.settings) as con:
            rows=con.execute('SELECT * FROM evaluation_candles WHERE asset_id=? AND start>=? AND end<=? ORDER BY provider,start',(asset_id,start,end)).fetchall()
        providers={r['provider'] for r in rows}
        for provider in providers:
            cached=[Candle(*(r[k] for k in ('start','end','open','high','low','close','volume'))) for r in rows if r['provider']==provider]
            if evaluate(cached,start,end,1,0) is not None:return cached,provider
        symbols=self.cex._symbols(asset_id)
        if asset_id in {'BTC','ETH','SOL','SUI'}:symbols.setdefault('binance_spot',asset_id+'USDT')
        candles=[]
        if symbols.get('binance_spot'):
            provider='binance_spot';cursor=int(math.ceil(start/60)*60*1000)
            for _ in range(12):
                url=os.getenv('BINANCE_PUBLIC_DATA_REST','https://data-api.binance.vision').rstrip('/')+'/api/v3/klines?'+urllib.parse.urlencode(dict(symbol=symbols['binance_spot'],interval='1m',startTime=cursor,endTime=int(end*1000)-1,limit=1000))
                payload=self.cex._download(url)
                if not isinstance(payload,list) or not payload:break
                for r in payload:
                    candle=Candle(float(r[0])/1000,(float(r[6])+1)/1000,*map(float,r[1:5]),float(r[5]))
                    if candle.valid() and candle.start>=start and candle.end<=end:candles.append(candle)
                next_cursor=int(payload[-1][0])+60000
                if next_cursor<=cursor or next_cursor>=end*1000:break
                cursor=next_cursor
        else:
            with connect(self.path,self.settings) as con:
                asset=con.execute('SELECT asset_class FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
            if not asset or asset['asset_class'] not in {'EQUITY','FOREX','COMMODITY','INDEX'}:return [],'unavailable'
            symbol=self.public._symbol(asset_id);provider='public_chart:'+symbol
            url='https://query1.finance.yahoo.com/v8/finance/chart/'+urllib.parse.quote(symbol,safe='')+'?'+urllib.parse.urlencode(dict(period1=int(start),period2=int(end)+1,interval='1m'))
            payload=self.public._download(url)
            result=((payload.get('chart') or {}).get('result') or [])[0]
            quotes=result['indicators']['quote'][0]
            for i,epoch in enumerate(result.get('timestamp') or []):
                vals=[(quotes.get(k) or [])[i] for k in ('open','high','low','close')]
                if any(v is None for v in vals):continue
                candle=Candle(float(epoch),float(epoch)+60,*map(float,vals),(quotes.get('volume') or [None]*len(result['timestamp']))[i])
                if candle.valid() and candle.start>=start and candle.end<=end:candles.append(candle)
        unique={c.start:c for c in candles};candles=sorted(unique.values(),key=lambda c:c.start)
        with connect(self.path,self.settings) as con:
            transaction(con)
            con.executemany('INSERT OR IGNORE INTO evaluation_candles VALUES(?,?,?,?,?,?,?,?,?,?)',[(asset_id,provider,c.start,c.end,c.open,c.high,c.low,c.close,c.volume,time.time()) for c in candles])
        return candles,provider


class SignalEvaluator:
    def __init__(self,settings):
        from .store import BrainStore
        self.settings=settings;self.path=BrainStore(settings).path
        with connect(self.path,settings) as con:con.executescript(SCHEMA)
        self.reader=CandleEvidence(settings,self.path)

    @staticmethod
    def context(row):
        return json.dumps([row['asset_id'],row['event_type'],row['horizon_seconds'],row['side']],separators=(',',':'))

    def cycle(self,limit=4,now=None):
        now=time.time() if now is None else now
        with connect(self.path,self.settings) as con:
            # Persist pending jobs, including internally observed directional signals.
            con.execute('''INSERT OR IGNORE INTO signal_evaluations(signal_id,state)
                SELECT s.id,'PENDING' FROM signals s JOIN predictions p ON p.id=s.prediction_id
                JOIN events e ON e.id=p.event_id
                WHERE p.mode!='REPLAY' AND p.direction_probability!=0.5
                  AND e.title NOT LIKE 'PREDICTION MARKET %'
                  AND e.title!='SEC CURRENT FILING' ''')
            rows=con.execute('''SELECT se.*,s.action,p.event_id,p.asset_id,p.created_at,p.horizon_seconds,
                e.event_type,p.id prediction_id,p.direction_probability,p.mode,p.evidence_json
                FROM signal_evaluations se JOIN signals s ON s.id=se.signal_id
                JOIN predictions p ON p.id=s.prediction_id JOIN events e ON e.id=p.event_id
                WHERE se.state='PENDING' AND se.next_attempt<=? AND p.created_at+p.horizon_seconds+60<=?
                ORDER BY se.next_attempt,p.created_at LIMIT ?''',(now,now,limit)).fetchall()
        completed=0
        for raw in rows:
            row=dict(raw);row['side']=1 if row['direction_probability']>.5 else -1
            try:
                candles,provider=self.reader.history(row['asset_id'],row['created_at'],row['created_at']+row['horizon_seconds'])
                result=evaluate(candles,row['created_at'],row['created_at']+row['horizon_seconds'],row['side'],float(os.getenv('SIGNAL_ROUND_TRIP_COST_BPS','10')))
                if result is None:raise ValueError('incomplete_or_gapped_OHLC_path')
                self.complete(row,result,candles,provider,now);completed+=1
            except Exception as exc:
                attempts=row['attempts']+1
                with connect(self.path,self.settings) as con:
                    con.execute('UPDATE signal_evaluations SET attempts=?,next_attempt=?,state=?,last_error=? WHERE signal_id=?',
                        (attempts,now+min(3600,60*2**min(attempts,6)),'UNOBSERVABLE' if attempts>=12 else 'PENDING',type(exc).__name__+':'+str(exc)[:160],row['signal_id']))
        return dict(attempted=len(rows),completed=completed)

    def complete(self,row,results,candles,provider,now):
        digest=hashlib.sha256(json.dumps([asdict(c) for c in candles],sort_keys=True).encode()).hexdigest()
        with connect(self.path,self.settings) as con:
            transaction(con)
            from .neural_bandit import NeuralBandit,vector
            stored=con.execute('SELECT state_json FROM neural_bandit_state WHERE id=1').fetchone()
            neural=NeuralBandit(json.loads(stored['state_json']) if stored else None)
            evidence=json.loads(row.get('evidence_json') or '{}')
            feature_id=evidence.get('feature_id')
            feature=con.execute('SELECT values_json,prediction_time,available_cutoff FROM feature_vectors WHERE feature_id=?',(feature_id,)).fetchone() if feature_id else None
            x=vector(json.loads(feature['values_json']),row['side'],row['horizon_seconds']) if feature and feature['available_cutoff']<=row['created_at'] and feature['prediction_time']<=row['created_at'] else None
            if feature and x is not None:
                import statistics
                usable=[c for c in candles if c.start>=row['created_at'] and c.end<=row['created_at']+row['horizon_seconds']]
                first=usable[0].open;high=max(usable,key=lambda c:c.high);low=min(usable,key=lambda c:c.low)
                logs=[math.log(b.close/a.close) for a,b in zip(usable,usable[1:])]
                peak=high if high.high/first-1>=1-low.low/first else low
                con.execute('INSERT OR IGNORE INTO signal_outcomes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                    (row['signal_id'],feature_id,row['prediction_id'],row['event_id'],row['asset_id'],row['horizon_seconds'],
                     usable[-1].close/first-1,max(0,high.high/first-1),min(0,low.low/first-1),statistics.pstdev(logs) if logs else 0.,
                     peak.end-usable[0].start,high.end-usable[0].start,low.end-usable[0].start,now))
            for result in results:
                # Once-only updates survive crashes/retries. Correlated event revisions
                # contribute at most one reward per asset, horizon, direction and arm.
                cur=con.execute('INSERT OR IGNORE INTO signal_barriers VALUES(?,?,?,?,?,?,?)',
                    (row['signal_id'],result['level'],result['state'],result['net_return'],result['reward_r'],json.dumps(result,sort_keys=True),None))
                if cur.rowcount!=1 or result['state']=='AMBIGUOUS':continue
                fresh=con.execute('INSERT OR IGNORE INTO bandit_feedback VALUES(?,?,?,?,?,?)',
                    (row['event_id'],row['asset_id'],row['horizon_seconds'],row['side'],result['level'],row['signal_id']))
                if fresh.rowcount!=1:continue
                state=result['state'];reward=result['reward_r'];context=self.context(row)
                if x is not None:
                    neural.update(x,LEVELS.index(result['level']),reward)
                    con.execute('INSERT OR IGNORE INTO neural_bandit_updates VALUES(?,?,?,?,?,?)',(row['signal_id'],result['level'],neural.state['version'],reward,feature_id,now))
                previous=con.execute('SELECT * FROM contextual_bandit WHERE context=? AND level=?',(context,result['level'])).fetchone()
                n=int(previous['samples']) if previous else 0
                mean=float(previous['reward_mean']) if previous else 0.
                ewma=float(previous['reward_ewma']) if previous else reward
                con.execute('INSERT OR REPLACE INTO contextual_bandit VALUES(?,?,?,?,?,?,?,?,?)',
                    (context,result['level'],n+1,(previous['wins'] if previous else 0)+int(state=='WIN'),
                     (previous['losses'] if previous else 0)+int(state=='LOSS'),(previous['timeouts'] if previous else 0)+int(state=='TIMEOUT'),
                     mean+(reward-mean)/(n+1),.95*ewma+.05*reward,now))
                con.execute('UPDATE signal_barriers SET learned_at=? WHERE signal_id=? AND level=?',(now,row['signal_id'],result['level']))
            if x is not None:
                con.execute('INSERT OR REPLACE INTO neural_bandit_state VALUES(1,?,?)',(json.dumps(neural.state,separators=(',',':')),now))
            con.execute("UPDATE signal_evaluations SET state='COMPLETE',completed_at=?,provider=?,path_sha256=?,last_error=NULL WHERE signal_id=?",(now,provider,digest,row['signal_id']))

    def policy(self,asset_id,event_type,horizon,side,values=None):
        context=self.context(dict(asset_id=asset_id,event_type=event_type,horizon_seconds=horizon,side=side))
        with connect(self.path,self.settings) as con:
            rows=con.execute('SELECT * FROM contextual_bandit WHERE context=? ORDER BY level',(context,)).fetchall()
            stored=con.execute('SELECT state_json FROM neural_bandit_state WHERE id=1').fetchone()
        arms=[]
        for row in rows:
            n=row['samples'];p=row['wins']/n;z=1.96
            margin=z*math.sqrt(p*(1-p)/n+z*z/(4*n*n))/(1+z*z/n)
            center=(p+z*z/(2*n))/(1+z*z/n)
            arms.append(dict(level=row['level'],samples=n,success_rate=p,lower95=max(0,center-margin),upper95=min(1,center+margin),reward_r=row['reward_mean'],recent_reward_r=row['reward_ewma']))
        eligible=[a for a in arms if a['samples']>=30 and a['lower95']>.5 and a['recent_reward_r']>0]
        from .neural_bandit import NeuralBandit,vector
        neural=NeuralBandit(json.loads(stored['state_json']) if stored else None)
        return dict(state='SHADOW',neural_version=neural.state['version'],neural_expected_r=neural.forward(vector(values or {},side,horizon))[1],arms=arms,suggested_level=max(eligible,key=lambda a:a['recent_reward_r'])['level'] if eligible else None,
                    reason='delayed_full_information_contextual_bandit; no automatic live promotion')
