from __future__ import annotations
import importlib,os
from typing import Any
from ..config import Settings
from ..news.registry import SourceRegistry

EXPLICIT=(
 {'provider_id':'brain_reference','category':'market_reference','asset_class':'ALL','cost':'free','key_required':False,'env_var':'','adapter':'v52.intelligence.reference_market:BrainReferenceMarketProvider','fixture_verified':True},
 {'provider_id':'public_chart','category':'market_reference','asset_class':'COMMODITY_FOREX_EQUITY_INDEX','cost':'free','key_required':False,'env_var':'','adapter':'v52.intelligence.reference_market:PublicChartMarketProvider','fixture_verified':True},
 {'provider_id':'dexscreener','category':'dex_market_reference','asset_class':'TOKEN','cost':'free','key_required':False,'env_var':'','adapter':'v52.intelligence.reference_market:DexContractMarketProvider','fixture_verified':True},
 {'provider_id':'public_equity_directory','category':'asset_directory','asset_class':'EQUITY_ETF','cost':'free','key_required':False,'env_var':'','adapter':'v52.intelligence.observation:PublicEquityDirectoryProvider','fixture_verified':True},
 {'provider_id':'nansen','category':'onchain','asset_class':'CRYPTO_TOKEN','cost':'paid','key_required':True,'env_var':'NANSEN_API_KEY','adapter':'v52.structured.provider_flows:ProviderFlowService','fixture_verified':True},
 {'provider_id':'cryptoquant','category':'onchain','asset_class':'CRYPTO','cost':'paid','key_required':True,'env_var':'CRYPTOQUANT_TOKEN','adapter':'v52.structured.provider_flows:ProviderFlowService','fixture_verified':True},
 {'provider_id':'coinglass','category':'derivatives','asset_class':'CRYPTO','cost':'paid','key_required':True,'env_var':'COINGLASS_API_KEY','adapter':'v52.structured.provider_flows:ProviderFlowService','fixture_verified':True},
 {'provider_id':'glassnode','category':'onchain','asset_class':'CRYPTO','cost':'paid','key_required':True,'env_var':'GLASSNODE_API_KEY','adapter':'v52.structured.provider_flows:ProviderFlowService','fixture_verified':True},
 {'provider_id':'native_official','category':'macro_fundamental','asset_class':'ALL','cost':'free','key_required':False,'env_var':'','adapter':'v52.structured.native:NativePublicService','fixture_verified':True},
)
def _callable(path:str)->bool:
    module,name=path.split(':',1)
    try:return hasattr(importlib.import_module(module),name)
    except ImportError:return False
def build_manifest(settings:Settings)->list[dict[str,Any]]:
    rows=[]
    for item in EXPLICIT:
        credential=not item['key_required'] or bool(os.getenv(item['env_var'],''));rows.append({**item,'adapter_implemented':_callable(item['adapter']),'enabled':credential,'live_verified':False,'status':'IMPLEMENTED_FIXTURE_VERIFIED' if not credential else 'IMPLEMENTED_LIVE_VALIDATION_PENDING'})
    for provider in SourceRegistry(settings).structured_providers():
        pid=str(provider.get('slug') or provider.get('name'));env=str(provider.get('key_env') or '');key_required=bool(env);adapter='v52.structured.service:StructuredProviderRunner';fixture=bool(provider.get('request'))
        rows.append({'provider_id':pid,'category':str(provider.get('category') or 'structured'),'asset_class':str(provider.get('asset_class') or 'ALL'),'cost':'paid' if key_required else 'free','key_required':key_required,'env_var':env,'adapter':adapter,'adapter_implemented':_callable(adapter),'fixture_verified':fixture,'live_verified':False,'enabled':fixture and (not key_required or bool(os.getenv(env,''))),'status':'IMPLEMENTED_FIXTURE_VERIFIED' if fixture else 'NOT_APPLICABLE_WITH_REASON'})
    return rows
def validate_manifest(settings:Settings)->dict[str,Any]:
    rows=build_manifest(settings);fail=[x['provider_id'] for x in rows if x['enabled'] and not x['adapter_implemented']]
    return {'ok':not fail,'providers':len(rows),'enabled':sum(x['enabled'] for x in rows),'fixture_verified':sum(x['fixture_verified'] for x in rows),'invalid_enabled':fail}
