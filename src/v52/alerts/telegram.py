from __future__ import annotations

import html
import json
from urllib.parse import urlsplit
from typing import Any


def _esc(value: Any) -> str:
    return html.escape(str(value or ""), quote=False)


def render_story(story: dict[str, Any], payload: dict[str, Any]) -> str:
    title = str(story.get("english_title") or payload.get("title") or story.get("title") or "Untitled")[:240]
    summary = str(story.get("english_summary") or payload.get("summary") or story.get("summary") or "")[:700]
    source = str(payload.get("source") or story.get("source_name") or "")[:120]
    category = str(payload.get("category") or story.get("category") or "")[:80]
    materiality = payload.get("materiality") or story.get("materiality") or 0
    age = payload.get("detection_age_seconds")
    url = str(payload.get("url") or story.get("canonical_url") or "")[:900]
    age_text = "n/a" if age is None else f"{float(age):.1f}s"
    parts = [
        f"<b>📰 {_esc(title)}</b>",
        f"<b>Source:</b> {_esc(source)} | <b>Category:</b> {_esc(category)}",
        f"<b>Materiality:</b> {_esc(materiality)}/10 | <b>Detected age:</b> {_esc(age_text)}",
    ]
    if summary:
        parts.append(_esc(summary))
    if url:
        parts.append(f'<a href="{html.escape(str(url), quote=True)}">Open source</a>')
    return "\n".join(parts)


def render_market(payload: dict[str, Any]) -> str:
    kind = str(payload.get("event_type") or payload.get("kind") or "MARKET EVENT")
    venue = payload.get("venue") or ""
    symbol = payload.get("symbol") or "GLOBAL"
    headline = payload.get("headline") or f"{venue} {symbol} {kind}"
    parts = [f"<b>⚡ {_esc(headline)}</b>", f"<b>Venue:</b> {_esc(venue)} | <b>Symbol:</b> {_esc(symbol)}"]
    details = payload.get("details")
    if isinstance(details, dict):
        for k, v in list(details.items())[:12]:
            parts.append(f"<b>{_esc(k)}:</b> {_esc(v)}")
    return "\n".join(parts)


def _render_intelligence_legacy(payload: dict[str, Any]) -> str:
    action = str(payload.get("action") or "NO_TRADE")[:40]
    asset = str(payload.get("asset_name") or payload.get("asset_id") or "Unknown")[:100]
    event = str(payload.get("event_title") or payload.get("event_type") or "Market event")[:300]
    confidence = float(payload.get("calibrated_confidence") or 0.0)
    probability = float(payload.get("probability", .5))
    uncertainty = float(payload.get("uncertainty", 1.0))
    risk = float(payload.get("risk_score", 1.0))
    horizon = int(payload.get("horizon_seconds") or 0)
    provisional=bool(payload.get('provisional_gate'))
    heading="V59 PROVISIONAL RESEARCH SIGNAL" if provisional else "V59 QUALIFIED INTELLIGENCE"
    parts = [
        f"<b>🧠 {heading} — {_esc(action)}</b>",
        f"<b>Asset:</b> {_esc(asset)} | <b>Horizon:</b> {_esc(horizon)}s",
        f"<b>Market:</b> {_esc(payload.get('market'))} | <b>Direction:</b> {_esc(payload.get('direction'))} | <b>Type:</b> {_esc(payload.get('signal_type'))}",
        f"<b>Event:</b> {_esc(event)}",
        f"<b>Directional probability:</b> {_esc(f'{probability:.1%}')} | <b>Calibrated confidence:</b> {_esc(f'{confidence:.1%}')}",
        f"<b>Uncertainty:</b> {_esc(f'{uncertainty:.1%}')} | <b>Risk:</b> {_esc(f'{risk:.1%}')}",
        f"<b>Quality:</b> {_esc(payload.get('quality_score'))} | <b>Regime:</b> {_esc(payload.get('current_regime'))}",
        f"<b>Entry:</b> {_esc(str(payload.get('entry_logic') or '')[:180])}",
        f"<b>Invalidation:</b> {_esc(str(payload.get('invalidation_logic') or '')[:180])}",
        f"<b>Target:</b> {_esc(str(payload.get('target_logic') or '')[:180])}",
        f"<b>Exit:</b> {_esc(str(payload.get('exit_logic') or '')[:180])}",
        f"<b>Drivers:</b> {_esc(', '.join(str(x) for x in (payload.get('main_drivers') or []))[:180])}",
        f"<b>Champion:</b> {_esc(payload.get('champion_model'))} | <b>Signal:</b> {_esc(payload.get('signal_id'))} | <b>Time:</b> {_esc(payload.get('timestamp'))}",
        f"<b>Qualification:</b> {_esc(payload.get('qualification_status'))}",
        f"<i>{_esc(str(payload.get('risk_disclaimer') or 'Probabilistic intelligence; no guaranteed profit.')[:250])}</i>",
    ]
    if payload.get('reference_price') is not None:
        age_text=f"{float(payload.get('reference_price_age_seconds') or 0):.1f}s"
        parts.extend([
            f"<b>Reference price:</b> {_esc(payload.get('reference_price'))} | <b>Provider:</b> {_esc(payload.get('reference_price_provider'))}",
            f"<b>Observed:</b> {_esc(payload.get('reference_price_observed_at'))} | <b>Age:</b> {_esc(age_text)}",
            f"<b>1:1 inspection scenario:</b> entry {_esc(payload.get('reference_entry'))} | target {_esc(payload.get('reference_target'))} | invalidation {_esc(payload.get('reference_invalidation'))} ({_esc(payload.get('reference_move_pct'))}%)",
        ])
    if provisional and payload.get('provisional_basis'):
        parts.extend([
            f"<b>Provisional basis:</b> {_esc(payload.get('provisional_basis'))}",
            f"<b>Unqualified model probability (shown for audit only):</b> {_esc(payload.get('model_probability_unqualified'))}",
        ])
    parts.append("<b>Evaluation:</b> ±0.1 / 0.3 / 0.5 / 1 / 3 / 5 / 10% · R:R 1:1")
    parts.append("<i>All reasons, news and source links are in the accompanying full report.</i>")
    for source in (payload.get('sources') or [])[:2]:
        url=str(source.get('source_url') or '')
        if urlsplit(url).scheme in {'http','https'} and len(url)<=500:
            parts.append('<a href="'+html.escape(url,quote=True)+'">'+_esc(str(source.get('source_name') or 'Source')[:80])+'</a>')
    return "\n".join(parts)


def _human_horizon(seconds: int) -> str:
    if seconds >= 86400 and seconds % 86400 == 0:return f"{seconds//86400} روز"
    if seconds >= 3600 and seconds % 3600 == 0:return f"{seconds//3600} ساعت"
    if seconds >= 60:return f"{seconds//60} دقیقه"
    return f"{seconds} ثانیه"


def _readable_summary(value: Any) -> str:
    text=str(value or '').strip()
    if not text.startswith('{'):return text
    try:obj=json.loads(text)
    except (TypeError,ValueError,json.JSONDecodeError):return ''
    if not isinstance(obj,dict):return ''
    parts=[]
    for key,label in (('headline','خبر'),('description','شرح'),('actual','مقدار واقعی'),('forecast','پیش‌بینی'),
                      ('consensus','اجماع'),('previous','مقدار قبلی'),('revision','بازبینی'),('unit','واحد')):
        if obj.get(key) not in (None,''):parts.append(f"{label}: {obj[key]}")
    return ' | '.join(parts)


def render_intelligence(payload: dict[str, Any]) -> str:
    provisional=bool(payload.get('provisional_gate'))
    action=str(payload.get('action') or 'NO_TRADE').upper();direction=str(payload.get('direction') or '').upper()
    action_fa={'WATCH_BUY':'سناریوی صعودی؛ زیرنظر برای خرید','BUY':'خرید','ENTER_ON_CONFIRMATION':'ورود پس از تأیید',
               'WATCH_SELL':'سناریوی نزولی؛ زیرنظر برای فروش','SELL':'فروش','SELL_SHORT':'سناریوی نزولی / فروش',
               'AVOID_LONG':'اجتناب از خرید'}.get(action,action)
    direction_fa={'UP':'صعودی','DOWN':'نزولی'}.get(direction,direction or 'نامشخص')
    event_types={'CENTRAL_BANK_DECISION':'تصمیم بانک مرکزی','INFLATION_RELEASE':'انتشار تورم','EMPLOYMENT_RELEASE':'گزارش اشتغال',
                 'OPEC_DECISION':'تصمیم اوپک','OIL_INVENTORY':'ذخایر نفت','ENERGY_SUPPLY_DISRUPTION':'اختلال عرضه انرژی',
                 'EARNINGS':'گزارش مالی شرکت','GUIDANCE':'راهنمای سود شرکت','REGULATORY_ACTION':'اقدام نظارتی',
                 'EXCHANGE_LISTING':'لیست‌شدن در صرافی','EXCHANGE_DELISTING':'حذف از صرافی','PROTOCOL_EXPLOIT':'رخداد امنیتی',
                 'ETF_DECISION':'تصمیم ETF','TOKEN_UNLOCK':'آزادسازی توکن','PROTOCOL_UPGRADE':'ارتقای شبکه'}
    event_type=str(payload.get('event_type') or '');asset=str(payload.get('asset_name') or payload.get('asset_id') or 'نامشخص')[:100]
    quality=float(payload.get('quality_score') or 0);confidence=float(payload.get('calibrated_confidence') or 0)
    uncertainty=float(payload.get('uncertainty') or 1);risk=float(payload.get('risk_score') or 1)
    status='تحقیقاتی پس از دوره ۴۸ ساعته؛ هنوز اعتبارسنجی نهایی نشده' if provisional else 'اعتبارسنجی‌شده برای ارسال'
    parts=[f"<b>📊 سیگنال فاندامنتال | {_esc(asset)}</b>",f"<b>وضعیت:</b> {_esc(status)}",
           f"<b>نتیجه:</b> {_esc(action_fa)} | <b>جهت:</b> {_esc(direction_fa)} | <b>افق:</b> {_esc(_human_horizon(int(payload.get('horizon_seconds') or 0)))}",
           f"<b>بازار:</b> {_esc(payload.get('market'))} | <b>نوع رویداد:</b> {_esc(event_types.get(event_type,event_type))}","",
           "<b>خبر مبنای سیگنال</b>",f"• {_esc(str(payload.get('event_title') or event_type)[:350])}"]
    summary=_readable_summary(payload.get('event_summary'))
    if summary:parts.append(f"• خلاصه: {_esc(summary[:500])}")
    if payload.get('reference_price') is not None:
        parts.extend(["","<b>قیمت و سناریوی بررسی ۱:۱</b>",
            f"• قیمت مرجع: <code>{_esc(payload.get('reference_price'))}</code> | منبع: {_esc(payload.get('reference_price_provider'))}",
            f"• ورود مرجع: <code>{_esc(payload.get('reference_entry'))}</code>",
            f"• هدف: <code>{_esc(payload.get('reference_target'))}</code> | حد ابطال: <code>{_esc(payload.get('reference_invalidation'))}</code>",
            f"• عمر قیمت: {float(payload.get('reference_price_age_seconds') or 0):.0f} ثانیه | دامنه: ±{_esc(payload.get('reference_move_pct'))}%"])
    parts.extend(["","<b>شرایط و منطق سیگنال</b>",
        f"• شرط ورود: {_esc(str(payload.get('entry_logic') or 'پس از تأیید واکنش قیمت و نقدشوندگی')[:260])}",
        f"• شرط ابطال: {_esc(str(payload.get('invalidation_logic') or 'نقض جهت خبر یا حد ابطال قیمت')[:260])}",
        f"• منطق هدف: {_esc(str(payload.get('target_logic') or '')[:220])}",
        f"• خروج: {_esc(str(payload.get('exit_logic') or '')[:220])}"])
    drivers=[str(x) for x in (payload.get('main_drivers') or payload.get('reasons') or []) if str(x).strip()]
    if drivers:parts.extend(["","<b>دلایل اصلی</b>"]+[f"• {_esc(x[:260])}" for x in drivers[:4]])
    parts.extend(["","<b>کیفیت و ریسک</b>",
        f"• کیفیت داده: {quality:.0%} | اطمینان کالیبره: {confidence:.0%} | عدم‌قطعیت: {uncertainty:.0%} | ریسک: {risk:.0%}"])
    if provisional:parts.append("• جهت این پیام از سازوکار روشن خبر استخراج شده؛ احتمال مدل هنوز تأیید نهایی نیست و فقط برای حسابرسی ثبت می‌شود.")
    raw_sources=[x for x in (payload.get('sources') or []) if isinstance(x,dict)]
    sources=[];seen=set()
    for source in sorted(raw_sources,key=lambda x:0 if urlsplit(str(x.get('source_url') or '')).scheme in {'http','https'} else 1):
        key=str(source.get('source_url') or '').strip() or str(source.get('title') or source.get('source_name') or '').strip().lower()
        if not key or key in seen:continue
        seen.add(key);sources.append(source)
    if sources:
        parts.extend(["","<b>منابع و لینک بررسی</b>"])
        for i,source in enumerate(sources[:4],1):
            url=str(source.get('source_url') or '');label=str(source.get('title') or source.get('source_name') or f'منبع {i}')[:170]
            observed=str(source.get('observed_at') or '')[:40]
            line=(f'{i}. <a href="{html.escape(url,quote=True)}">{_esc(label)}</a>' if urlsplit(url).scheme in {'http','https'} and len(url)<=500 else f'{i}. {_esc(label)}')
            if observed:line+=f" | {_esc(observed)}"
            parts.append(line)
    parts.extend(["",f"<b>شناسه:</b> {_esc(payload.get('signal_id'))} | <b>زمان:</b> {_esc(payload.get('timestamp'))}",
                  "<i>این پیام سفارش خودکار نیست؛ قبل از اقدام، خبر و قیمت لینک‌شده را بررسی کنید.</i>"])
    kept=[];size=0
    for line in parts:
        addition=len(line)+(1 if kept else 0)
        if size+addition>3980:break
        kept.append(line);size+=addition
    return "\n".join(kept)


def full_signal_report(payload):
    parts=['<!doctype html><html lang="en"><meta charset="utf-8"><title>Signal report</title>',
           '<style>body{font:16px system-ui;max-width:960px;margin:32px auto;padding:20px;line-height:1.6}pre{white-space:pre-wrap;overflow-wrap:anywhere}li{margin-bottom:12px}</style>',
           '<h1>Signal '+_esc(payload.get('signal_id'))+' · '+_esc(payload.get('asset_id'))+'</h1>',
           '<p>'+_esc(payload.get('event_title'))+'</p>',
           '<h2>Analysis</h2><p>'+_esc(payload.get('event_summary'))+'</p><ul>']
    for reason in payload.get('reasons') or []:parts.append('<li>'+_esc(reason)+'</li>')
    parts.append('</ul><h2>News and sources</h2><ol>')
    for source in payload.get('sources') or []:
        url=str(source.get('source_url') or '')
        link=('<a href="'+html.escape(url,quote=True)+'">'+_esc(url)+'</a>') if urlsplit(url).scheme in {'http','https'} else _esc(url)
        parts.append('<li><b>'+_esc(source.get('source_name'))+'</b> · '+_esc(source.get('observed_at'))+'<br>'+_esc(source.get('title'))+'<p>'+_esc(source.get('summary'))+'</p>'+link+'</li>')
    parts.append('</ol><h2>Complete signal record</h2><pre>'+_esc(json.dumps(payload,ensure_ascii=False,indent=2,default=str))+'</pre>')
    parts.append('<p>Seven independent 1:1 evaluation arms: 0.1%, 0.3%, 0.5%, 1%, 3%, 5%, 10%. Paper entry is the first full minute open after prediction. Costs are estimates. Missing or ambiguous candles are excluded from win/loss training. Adaptive policy remains in shadow until independently validated.</p></html>')
    return '\n'.join(parts).encode('utf-8')


def render_signal_exit(payload: dict[str, Any]) -> str:
    observed_return = f"{float(payload.get('return_value') or 0):.3%}"
    mfe = f"{float(payload.get('mfe') or 0):.3%}"
    mae = f"{float(payload.get('mae') or 0):.3%}"
    return "\n".join([
        "<b>🛑 V52 SIGNAL EXIT / INVALIDATION</b>",
        f"<b>Asset:</b> {_esc(payload.get('asset_id'))} | <b>Prior action:</b> {_esc(payload.get('previous_action'))}",
        f"<b>Event:</b> {_esc(str(payload.get('event_title') or '')[:300])}",
        f"<b>Reason:</b> {_esc(payload.get('close_reason'))}",
        f"<b>Observed return:</b> {_esc(observed_return)} | <b>MFE:</b> {_esc(mfe)} | <b>MAE:</b> {_esc(mae)}",
        "<i>The lifecycle is closed and retained for post-exit learning.</i>",
    ])


def render_daily_report(payload: dict[str, Any]) -> str:
    summary = payload.get("summary") or {}
    if not isinstance(summary,dict):summary={"message":str(summary)}
    counts = summary.get("counts") or {}
    lifecycle = payload.get("lifecycle") or {}
    qualification = payload.get("qualification") or {}
    drift = payload.get("drift") or {}
    return "\n".join([
        f"<b>📊 V52 DAILY AI LEARNING REPORT — {_esc(payload.get('date'))}</b>",
        f"<b>Mode:</b> {_esc(payload.get('mode'))} | <b>Qualified:</b> {_esc(qualification.get('qualified', False))}",
        f"<b>Events:</b> {_esc(counts.get('events', 0))} | <b>Responses:</b> {_esc(counts.get('market_responses', 0))} | <b>Knowledge:</b> {_esc(counts.get('knowledge', 0))}",
        f"<b>Signals:</b> {_esc(counts.get('signals', 0))} | <b>Closed:</b> {_esc(lifecycle.get('closed', 0))} | <b>Missed reviewed:</b> {_esc(lifecycle.get('missed', 0))}",
        f"<b>Drift:</b> {_esc(drift.get('detected', False))} | <b>Brier:</b> {_esc(qualification.get('brier', 'n/a'))} | <b>ECE:</b> {_esc(qualification.get('calibration_error', 'n/a'))}",
        "<i>Internal evidence is gated; no signal is released during warm-up or failed qualification.</i>",
    ])

def render_typed(payload:dict[str,Any])->str:
    kind=str(payload.get('kind') or '').upper().replace('_',' ')
    icon={'ACTIVE UPDATE':'🔄','INVALIDATION':'⛔','PUMP WARNING':'🚀','DUMP WARNING':'📉','FUNDAMENTAL INTELLIGENCE':'🌐','VALIDATED LEARNING UPDATE':'✅','ADAPTIVE LEARNING REPORT':'🧬','CRITICAL SYSTEM WARNING':'🚨'}.get(kind,'ℹ️')
    fields=[f"<b>{icon} V52 {kind}</b>",f"<b>Asset:</b> {_esc(payload.get('asset_id') or payload.get('asset'))}",f"<b>Summary:</b> {_esc(str(payload.get('summary') or payload.get('message') or payload.get('event_title') or '')[:1200])}"]
    if payload.get('confidence') is not None:fields.append(f"<b>Confidence:</b> {_esc(payload.get('confidence'))}")
    if payload.get('action'):fields.append(f"<b>Action:</b> {_esc(payload.get('action'))}")
    fields.append('<i>Probabilistic intelligence; no guaranteed profit.</i>');return '\n'.join(fields)


def render_batch(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> str:
    chunks = []
    for story, payload in items:
        kind = str(payload.get("kind") or "")
        if kind in {"intelligence_signal","provisional_signal"}:
            chunks.append(render_intelligence(payload))
        elif kind == "signal_exit":
            chunks.append(render_signal_exit(payload))
        elif kind == "daily_learning_report":
            chunks.append(render_daily_report(payload))
        elif kind in {'active_update','invalidation','pump_warning','dump_warning','fundamental_intelligence','validated_learning_update','adaptive_learning_report','critical_system_warning'}:
            chunks.append(render_typed(payload))
        elif str(payload.get("kind") or payload.get("event_type") or "").lower().startswith("market") or payload.get("venue"):
            chunks.append(render_market(payload))
        else:
            chunks.append(render_story(story, payload))
    text = "\n\n────────────\n\n".join(chunks)
    if len(text) > 4000:
        # Batch sizing is a caller concern. Failing closed prevents malformed HTML and
        # prevents marking unseen items as delivered.
        raise ValueError("rendered Telegram batch exceeds 4000 characters")
    return text
