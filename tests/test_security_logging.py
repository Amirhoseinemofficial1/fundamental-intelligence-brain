from __future__ import annotations

import logging
from v52.loggingx import redact_text, RedactingFormatter


def test_telegram_token_redacted_everywhere() -> None:
    secret='123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi'
    text=f'https://api.telegram.org/bot{secret}/sendMessage sk-abcdefghijklmnopqrstuv'
    out=redact_text(text)
    assert secret not in out
    assert 'sk-abcdefghijklmnopqrstuv' not in out
    rec=logging.LogRecord('x',logging.ERROR,'',0,text,(),None)
    formatted=RedactingFormatter('%(message)s').format(rec)
    assert secret not in formatted
