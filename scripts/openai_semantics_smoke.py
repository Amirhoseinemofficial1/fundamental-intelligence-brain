from __future__ import annotations
import asyncio, json, os
from v52.config import Settings
from v52.intelligence.openai_semantics import OpenAISemanticExtractor
from v52.intelligence.taxonomy import classify_story

async def run() -> int:
    settings=Settings.from_env()
    story={"title":"Federal Reserve cuts interest rates by 25 basis points","summary":"Official policy decision cites easing inflation",
           "category":"MONETARY_POLICY","materiality":10,"source_name":"Federal Reserve","source_url":"https://federalreserve.gov"}
    base=classify_story(story); client=OpenAISemanticExtractor(settings)
    try: result=await client.extract(story,base)
    finally: await client.close()
    print(json.dumps({"ok":bool(result),"event_type":(result or {}).get("event_type"),"asset_count":len((result or {}).get("asset_ids") or []),
                      "model":os.getenv("OPENAI_MODEL","gpt-5.4-mini"),"store":False}))
    return 0 if result else 2
if __name__=='__main__': raise SystemExit(asyncio.run(run()))
