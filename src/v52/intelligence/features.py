from __future__ import annotations
import hashlib,json,math,time
from typing import Any
from ..config import Settings
from ..storage.sqlite import connect

FEATURE_VERSION='features-59.0.0-impact-gated-structured'
ONCHAIN=('exchange_inflow','exchange_outflow','net_exchange_flow','whale_transfer_count','whale_transfer_value','large_transfer_count','large_transfer_value','whale_accumulation','whale_distribution','holder_concentration','new_holder_rate','smart_money_flow','stablecoin_flow','dex_volume','dex_liquidity','lp_delta','lp_change','bridge_flow','mint','mint_amount','burn','burn_amount','treasury_flow')
DERIVATIVES=('open_interest','oi_change','oi_acceleration','funding','funding_rate','funding_zscore','basis','long_short_ratio','long_liquidation','short_liquidation','liquidation_long','liquidation_short','liquidation_imbalance','futures_spot_divergence','squeeze_score','squeeze_probability')
MICROSTRUCTURE=('spread','depth','bid_ask_imbalance','orderbook_slope','aggressive_buy','aggressive_sell','aggressive_buy_volume','aggressive_sell_volume','trade_delta','cvd','large_trade_rate','depth_depletion','depth_replenishment','liquidity_vacuum','absorption','exhaustion')
FUNDAMENTAL=('cpi','ppi','gdp','pmi','employment','unemployment_rate','nonfarm_payrolls','average_hourly_earnings','jolts_job_openings','policy_rate','effective_fed_funds_rate','sofr','obfr','tgcr','bgcr','nominal_yield','real_yield','treasury_2y','treasury_5y','treasury_10y','treasury_30y','real_yield_5y','real_yield_10y','real_yield_30y','yield_curve_2s10s','yield_curve_5s30s','rate_differential','inventory','oil_inventory','gasoline_inventory','distillate_inventory','production','oil_production','refinery_utilization','positioning_zscore','positioning_net','positioning_weekly_delta','positioning_four_week_delta','earnings','eps','revenue','guidance','margin','etf_flow','gold_etf_holdings','central_bank_gold_reserves','central_bank_gold_purchases','risk_sentiment')
ALL_DYNAMIC=ONCHAIN+DERIVATIVES+MICROSTRUCTURE+FUNDAMENTAL
CROSS_ASSETS=('DXY','SPX','NDX','BTC','XAUUSD','WTI')
FEATURE_TTLS={
    'spread':60,'depth':60,'bid_ask_imbalance':60,'orderbook_slope':60,'aggressive_buy':90,'aggressive_sell':90,
    'open_interest':900,'oi_change':900,'oi_acceleration':900,'funding':1800,'funding_rate':1800,'funding_zscore':1800,
    'basis':900,'long_short_ratio':900,'liquidation_imbalance':300,'dex_liquidity':300,'dex_volume':900,
    'cpi':172800,'ppi':172800,'gdp':604800,'pmi':172800,'employment':172800,'inventory':86400,
    'production':86400,'oil_production':86400,'refinery_utilization':86400,
    'policy_rate':172800,'effective_fed_funds_rate':172800,'sofr':172800,'obfr':172800,'tgcr':172800,'bgcr':172800,
    'nominal_yield':172800,'real_yield':172800,'treasury_2y':172800,'treasury_5y':172800,
    'treasury_10y':172800,'treasury_30y':172800,'real_yield_5y':172800,'real_yield_10y':172800,
    'real_yield_30y':172800,'yield_curve_2s10s':172800,'yield_curve_5s30s':172800,
    'positioning_zscore':1209600,'positioning_net':1209600,'positioning_weekly_delta':1209600,
    'positioning_four_week_delta':1209600,'gold_etf_holdings':259200,'etf_flow':259200,
    'central_bank_gold_reserves':7776000,'central_bank_gold_purchases':7776000,
    'earnings':604800,'eps':604800,'revenue':604800,'guidance':604800,'margin':604800,
}

def feature_ttl(name:str)->int:
    if name in FEATURE_TTLS:return FEATURE_TTLS[name]
    if name in MICROSTRUCTURE:return 120
    if name in DERIVATIVES:return 900
    if name in ONCHAIN:return 3600
    if name in FUNDAMENTAL:return 86400
    return 3600

def canonical_feature_name(observation_type:str)->str:
    text=observation_type.lower();normalized=re_sub(text)
    aliases={'netflow':'net_exchange_flow','net_flow':'net_exchange_flow','whale_transfer_count':'whale_transfer_count','whale_transfer':'whale_transfer_value','large_transfer_count':'whale_transfer_count','large_transfer_value':'whale_transfer_value','open_interest_aggregate':'open_interest','funding_universe':'funding','consumer_price':'cpi','earnings':'earnings','long_liquidation':'long_liquidation','short_liquidation':'short_liquidation','liquidation':'liquidation_imbalance','lp_change':'lp_delta','squeeze_probability':'squeeze_score','orderbook_depth':'depth',
        'treasury_nominal_yield_2y':'treasury_2y','treasury_nominal_yield_5y':'treasury_5y','treasury_nominal_yield_10y':'treasury_10y','treasury_nominal_yield_30y':'treasury_30y',
        'treasury_real_yield_5y':'real_yield_5y','treasury_real_yield_10y':'real_yield_10y','treasury_real_yield_30y':'real_yield_30y',
        'fed_target_rate_midpoint':'policy_rate','gold_etf_flow':'etf_flow','oil_inventory':'oil_inventory'}
    if normalized in ALL_DYNAMIC:return normalized
    if normalized in aliases:return aliases[normalized]
    for key,value in aliases.items():
        if key in text:return value
    return next((name for name in sorted(ALL_DYNAMIC,key=len,reverse=True) if name in normalized), normalized)
def re_sub(text:str)->str:
    import re
    return re.sub(r'[^a-z0-9]+','_',text).strip('_')[:80] or 'unknown'

def evidence_quality(event:dict[str,Any],asset:dict[str,Any],modality_qualities:list[float],conflict:float=0.0)->float:
    """Score the evidence actually available for this event/asset pair.

    V59 previously multiplied only optional dynamic-modality quality by mapping
    confidence. With no fresh dynamic observation it substituted .5, making
    every production feature vector score below the .45 decision floor even
    when the event was direct, timely and mapped with high confidence.
    """
    source=max(0.0,min(1.0,float(event.get('source_credibility') or .5)))
    mapping=max(0.0,min(1.0,float(asset.get('mapping_confidence') or .5)))
    event_evidence=source*mapping
    if modality_qualities:
        modality=max(0.0,min(1.0,sum(modality_qualities)/len(modality_qualities)))*mapping
        score=.75*event_evidence+.25*modality
    else:score=event_evidence
    return max(0.0,min(1.0,score*(1.0-.25*max(0.0,min(1.0,float(conflict))))))

class FeatureStore:
    def __init__(self,settings:Settings):self.settings=settings;self.path=settings.data_dir/'brain'/'intelligence.sqlite3'
    def build_sync(self,event:dict[str,Any],asset:dict[str,Any],horizon:int,prediction_time:float|None=None,replay:bool=False)->dict[str,Any]:
        cutoff=float(prediction_time or time.time()); raw_values:dict[str,float|None]={name:None for name in ALL_DYNAMIC}; freshness:dict[str,dict[str,Any]]={}; provenance=[]; qualities=[]
        values:dict[str,float] = {}
        values.update({'event_importance':float(event.get('importance') or 0),'event_novelty':float(event.get('novelty') or 0),'source_quality':float(event.get('source_credibility') or .5),'surprise':float(event.get('surprise') or 0),'priced_in_score':float(event.get('priced_in_score') or .5),'horizon_log':math.log1p(horizon)/12.0,'source_conflict':0.0})
        with connect(self.path,self.settings) as con:
            semantic=con.execute('''SELECT sm.content_hash,sm.embedding_model,sm.embedding_json
                FROM event_semantic_links esl JOIN semantic_memories sm ON sm.content_hash=esl.content_hash
                WHERE esl.event_id=? ORDER BY esl.created_at DESC LIMIT 1''',(event.get('id'),)).fetchone()
            if semantic:
                try:embedding=[float(value) for value in json.loads(semantic['embedding_json'])]
                except (TypeError,ValueError,json.JSONDecodeError):embedding=[]
                for index,value in enumerate(embedding[:64]):
                    if math.isfinite(value):values[f'semantic_{index:02d}']=value
                values['semantic_available']=1.0 if len(embedding)>=32 else 0.0
                provenance.append(f"semantic:{semantic['embedding_model']}:{semantic['content_hash']}")
            else:
                values['semantic_available']=0.0
            for index in range(64):values.setdefault(f'semantic_{index:02d}',0.0)
            rows=con.execute('SELECT * FROM canonical_observations WHERE available_time<=? AND available_time>? ORDER BY available_time DESC LIMIT 5000',(cutoff,cutoff-30*86400)).fetchall()
            reliability={str(r['source_id']):float(r['reliability']) for r in con.execute('SELECT source_id,reliability FROM source_profiles').fetchall()}
            asset_id=str(asset['asset_id'])
            universe_row=con.execute('SELECT metadata_json,updated_at FROM assets WHERE asset_id=?',(asset_id,)).fetchone()
            if universe_row and float(universe_row['updated_at'] or 0)<=cutoff:
                try:universe_meta=json.loads(universe_row['metadata_json'] or '{}')
                except (TypeError,ValueError):universe_meta={}
                if universe_meta.get('cex_universe'):
                    values['asset_universe_venue_count']=float(len(universe_meta.get('venues') or []))
                    values['asset_universe_quote_volume_24h_log']=math.log1p(max(0.0,float(universe_meta.get('quote_volume_24h') or 0.0)))/25.0
                    values['asset_universe_change_24h']=max(-5.0,min(5.0,float(universe_meta.get('change_pct_24h') or 0.0)/100.0))
                    provenance.append(f"cex_universe:{asset_id}:{int(universe_row['updated_at'])}")
            values.setdefault('asset_universe_venue_count',0.0);values.setdefault('asset_universe_quote_volume_24h_log',0.0);values.setdefault('asset_universe_change_24h',0.0)
            profile=con.execute('SELECT profile_json,sample_count,updated_at FROM asset_profiles WHERE asset_id=?',(asset_id,)).fetchone()
            if profile:
                for key,value in json.loads(profile['profile_json']).items():
                    if isinstance(value,(int,float)):values[f'asset_profile_{re_sub(str(key))}']=float(value)
                values['asset_profile_sample_count']=float(profile['sample_count'])
                values['asset_profile_age_days']=max(0.0,(cutoff-float(profile['updated_at']))/86400.0)
                provenance.append(f'asset_profile:{asset_id}:{int(profile["updated_at"])}')
            else:
                values.update({'asset_profile_average_absolute_response':0.0,'asset_profile_reversal_probability':0.0,'asset_profile_sample_count':0.0,'asset_profile_age_days':0.0})
            observed_by_feature:dict[str,list[tuple[str,float,float]]]={}
            for row in rows:
                ids=json.loads(row['asset_ids_json'])
                if ids and asset_id not in ids:continue
                name=canonical_feature_name(str(row['observation_type'])); value=row['value']
                # Unknown provider record types are retained for audit, but a
                # numeric field such as prediction-market volume must not
                # raise the quality score of every unrelated asset.
                if name not in ALL_DYNAMIC:continue
                if value is not None:
                    source=str(row['source_id']); weight=float(row['quality'])*reliability.get(source,float(row['source_quality']))
                    ttl=feature_ttl(name);available=float(row['available_time']);expires=available+ttl;valid=cutoff<=expires
                    if name not in freshness:
                        freshness[name]={'value':float(value),'observed_at':float(row['event_time']),'available_at':available,'expires_at':expires,'source':source,'valid':valid,'ttl_seconds':ttl}
                        if valid:
                            raw_values[name]=float(value)
                    if valid:
                        provenance.append(row['observation_id']);qualities.append(weight)
                        observed_by_feature.setdefault(name,[]).append((source,float(value),weight))
            conflicts=[]
            for seen in observed_by_feature.values():
                if len({source for source,_,_ in seen})<2:continue
                weighted=sum(value*weight for _,value,weight in seen)/max(sum(weight for _,_,weight in seen),1e-12)
                scale=max(abs(weighted),max(abs(value) for _,value,_ in seen),1e-9)
                conflicts.append(min(1.0,max(abs(value-weighted) for _,value,_ in seen)/scale))
            holdings=observed_by_feature.get('gold_etf_holdings') or []
            if len(holdings)>=2:
                # Rows are read newest-first. The change in physical tonnes is a
                # deterministic flow proxy and retains the source provenance.
                raw_values['etf_flow']=holdings[0][1]-holdings[1][1]
            for name,value in raw_values.items():
                values[name]=0.0 if value is None else float(value)
                values[f'{name}__missing']=1.0 if value is None else 0.0
                values[f'{name}__valid']=1.0 if value is not None else 0.0
            values['source_conflict']=sum(conflicts)/len(conflicts) if conflicts else 0.0
            def prefer(primary:str,fallback:str)->float:
                current=raw_values.get(primary)
                return float(current) if current is not None else float(raw_values.get(fallback) or 0.0)
            values['oi_acceleration']=prefer('oi_acceleration','oi_change')
            values['squeeze_score']=prefer('squeeze_score','squeeze_probability')
            values['lp_delta']=prefer('lp_delta','lp_change')
            values['whale_transfer_count']=prefer('whale_transfer_count','large_transfer_count')
            values['whale_transfer_value']=prefer('whale_transfer_value','large_transfer_value')
            for cross_asset in CROSS_ASSETS:
                prices=con.execute('SELECT price FROM reference_prices WHERE asset_id=? AND available_at<=? AND available_at>? ORDER BY available_at DESC LIMIT 2',(cross_asset,cutoff,cutoff-7*86400)).fetchall()
                values[f'cross_{cross_asset.lower()}_return']=float(prices[0]['price'])/float(prices[1]['price'])-1.0 if len(prices)==2 and float(prices[1]['price']) else 0.0
            quality=evidence_quality(event,asset,qualities,values['source_conflict'])
            values['event_evidence_quality']=quality
            values['dynamic_modality_quality']=(sum(qualities)/len(qualities) if qualities else 0.0)
            feature_id=hashlib.sha256(f"{event.get('id')}|{asset_id}|{horizon}|{cutoff:.6f}|{FEATURE_VERSION}".encode()).hexdigest()
            # Missing features read as zero throughout inference, so persisting
            # thousands of explicit zeroes only inflates SQLite pages.  Keep the
            # live dense mapping for inference and a sparse, bounded record for
            # replay/audit.
            persisted_values = {
                key: value for key, value in values.items()
                if not isinstance(value, (int, float)) or float(value) != 0.0
            }
            persisted_provenance = list(dict.fromkeys(str(item) for item in provenance))[:128]
            con.execute("""INSERT OR IGNORE INTO feature_vectors(feature_id,event_id,asset_id,horizon_seconds,feature_version,prediction_time,available_cutoff,quality,values_json,provenance_json,replay,created_at,freshness_json) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
             (feature_id,event.get('id'),asset_id,horizon,FEATURE_VERSION,cutoff,cutoff,quality,json.dumps(persisted_values,sort_keys=True),json.dumps(persisted_provenance),int(replay),time.time(),json.dumps(freshness,sort_keys=True)))
        return {'feature_id':feature_id,'feature_version':FEATURE_VERSION,'prediction_time':cutoff,'available_cutoff':cutoff,'horizon_seconds':horizon,'quality':quality,'values':values,'freshness':freshness,'provenance':provenance,'replay':replay}
    async def build(self,*args,**kwargs):
        import asyncio
        return await asyncio.to_thread(self.build_sync,*args,**kwargs)
