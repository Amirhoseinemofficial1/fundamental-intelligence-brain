from __future__ import annotations

from v52.news.relevance import materiality_score, is_fundamental, story_fingerprint


def test_material_news_beats_noise() -> None:
    high,_=materiality_score('Federal Reserve announces interest rate cut','The central bank cut rates','MONETARY_POLICY',True)
    noise,_=materiality_score('Football celebrity fashion update','sports music movie','SPORTS',False)
    assert high >= 8
    assert noise == 0
    assert is_fundamental(high,'Federal Reserve announces interest rate cut','MONETARY_POLICY')
    assert not is_fundamental(noise,'football','SPORTS')


def test_fingerprint_uses_canonical_url() -> None:
    a=story_fingerprint('A','https://example.com/a?utm_source=x&id=1')
    b=story_fingerprint('Different','https://example.com/a?id=1&utm_medium=y')
    assert a==b
