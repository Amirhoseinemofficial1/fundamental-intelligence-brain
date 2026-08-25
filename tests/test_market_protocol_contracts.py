from __future__ import annotations

import pytest

from v52.config import Settings
from v52.market.service import mexc_depth_side
from v52.storage.market import MarketSemanticStore


def test_mexc_depth_quantity_is_second_column() -> None:
    # MEXC contract depth rows are [price, quantity, order_count].
    assert mexc_depth_side([[100.0, 7.0, 2], [101.0, "3.5", 99]]) == {100.0: 7.0, 101.0: 3.5}
    assert mexc_depth_side([[100.0, 0.0, 77]]) == {}


def test_bybit_depth_contract_rejects_non_protocol_depth(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BYBIT_ORDERBOOK_DEPTH", "20")
    with pytest.raises(ValueError, match="BYBIT_ORDERBOOK_DEPTH"):
        Settings.from_env()
    monkeypatch.setenv("BYBIT_ORDERBOOK_DEPTH", "50")
    assert Settings.from_env().bybit_orderbook_depth == 50


def test_current_market_endpoint_defaults_validate(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    # Binance USD-M Futures public routing changed in Aug-2026. Keep the new split explicit.
    monkeypatch.delenv("BINANCE_FUTURES_PUBLIC_WS", raising=False)
    monkeypatch.delenv("BINANCE_FUTURES_MARKET_WS", raising=False)
    monkeypatch.setenv("DERIBIT_TICK_INTERVAL", "agg2")
    s = Settings.from_env()
    s.validate()


def test_market_sequence_checkpoint_survives_store_reopen(settings) -> None:
    store = MarketSemanticStore(settings, "market_seq_test")
    store.checkpoint_sequences({"BINANCE:BTCUSDT:aggTrade": (12345, True)})
    store.close()
    reopened = MarketSemanticStore(settings, "market_seq_test")
    state = reopened.load_sequence_states()["BINANCE:BTCUSDT:aggTrade"]
    assert state["last_sequence"] == 12345
    assert state["recovery_pending"] is True
    reopened.checkpoint_sequences({"BINANCE:BTCUSDT:aggTrade": (12399, False)})
    assert reopened.pending_recovery_count() == 0
    reopened.close()
