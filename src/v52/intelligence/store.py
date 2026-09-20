from __future__ import annotations

import asyncio
import json
import math
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Iterable

from ..config import Settings
from ..storage.sqlite import connect, serialized_initialization, transaction
from ..timeutil import parse_datetime
from .types import EventUnderstanding, ExpertVote, SignalDecision


SCHEMA_VERSION = 10


class BrainStore:
    """Versioned persistent memory for intelligence, learning and governance.

    The database is physically separate from collector databases. Every write is
    idempotent so process restart can replay the story ledger without duplicate
    learning, predictions or user-facing signals.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self.path = settings.data_dir / "brain" / "intelligence.sqlite3"
        self._init_sync()

    @serialized_initialization
    def _init_sync(self) -> None:
        with connect(self.path, self.settings) as con:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations(
                    version INTEGER PRIMARY KEY,
                    applied_at REAL NOT NULL,
                    checksum TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS metadata(
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS assets(
                    asset_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    name TEXT NOT NULL,
                    asset_class TEXT NOT NULL,
                    market_symbol TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS entities(
                    entity_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cluster_key TEXT NOT NULL UNIQUE,
                    event_type TEXT NOT NULL,
                    domain TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    first_observed_at TEXT NOT NULL,
                    last_observed_at TEXT NOT NULL,
                    importance REAL NOT NULL,
                    novelty REAL NOT NULL,
                    surprise REAL,
                    source_credibility REAL NOT NULL,
                    priority TEXT NOT NULL,
                    state TEXT NOT NULL,
                    update_count INTEGER NOT NULL DEFAULT 1,
                    extraction_method TEXT NOT NULL,
                    extraction_version TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_time ON events(first_observed_at,id);
                CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type,domain,id);
                CREATE TABLE IF NOT EXISTS event_updates(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    story_id INTEGER NOT NULL,
                    source_change_id INTEGER NOT NULL UNIQUE,
                    update_key TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT NOT NULL,
                    source_name TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_event_updates_event ON event_updates(event_id,id);
                CREATE TABLE IF NOT EXISTS event_source_lineage(
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    story_id INTEGER NOT NULL,
                    source_change_id INTEGER NOT NULL,
                    source_id TEXT NOT NULL,
                    provider_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(event_id,source_change_id,source_id,provider_id)
                );
                CREATE INDEX IF NOT EXISTS idx_event_source_lineage ON event_source_lineage(event_id,source_id);
                CREATE TABLE IF NOT EXISTS event_semantic_context(
                    event_id INTEGER PRIMARY KEY REFERENCES events(id) ON DELETE CASCADE,
                    context_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS semantic_memories(
                    content_hash TEXT PRIMARY KEY,
                    embedding_model TEXT NOT NULL,
                    dimensions INTEGER NOT NULL,
                    embedding_json TEXT NOT NULL,
                    analysis_json TEXT NOT NULL,
                    text_preview TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS event_semantic_links(
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    content_hash TEXT NOT NULL REFERENCES semantic_memories(content_hash) ON DELETE CASCADE,
                    created_at REAL NOT NULL,
                    PRIMARY KEY(event_id,content_hash)
                );
                CREATE TABLE IF NOT EXISTS event_entities(
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    entity_id TEXT NOT NULL REFERENCES entities(entity_id),
                    relation TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    PRIMARY KEY(event_id,entity_id,relation)
                );
                CREATE TABLE IF NOT EXISTS event_assets(
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id),
                    impact_order INTEGER NOT NULL,
                    directional_bias TEXT NOT NULL,
                    mapping_confidence REAL NOT NULL,
                    mapping_reason TEXT NOT NULL,
                    PRIMARY KEY(event_id,asset_id)
                );
                CREATE INDEX IF NOT EXISTS idx_event_assets_asset ON event_assets(asset_id,event_id);
                CREATE TABLE IF NOT EXISTS relations(
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    source_id TEXT NOT NULL,
                    relation TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    provenance_json TEXT NOT NULL DEFAULT '{}',
                    PRIMARY KEY(event_id,source_id,relation,target_id)
                );
                CREATE TABLE IF NOT EXISTS source_profiles(
                    source_id TEXT PRIMARY KEY,
                    observations INTEGER NOT NULL DEFAULT 0,
                    primary_count INTEGER NOT NULL DEFAULT 0,
                    correction_count INTEGER NOT NULL DEFAULT 0,
                    duplicate_count INTEGER NOT NULL DEFAULT 0,
                    latency_sum REAL NOT NULL DEFAULT 0,
                    latency_samples INTEGER NOT NULL DEFAULT 0,
                    reliability REAL NOT NULL DEFAULT .5,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS regimes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    asset_id TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    regime TEXT NOT NULL,
                    trend_score REAL NOT NULL,
                    volatility_score REAL NOT NULL,
                    change_score REAL NOT NULL,
                    liquidity_score REAL NOT NULL,
                    confidence REAL NOT NULL,
                    features_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_regime_asset_time ON regimes(asset_id,observed_at DESC);
                CREATE TABLE IF NOT EXISTS expert_votes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    revision INTEGER NOT NULL,
                    expert TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    horizon_seconds INTEGER NOT NULL,
                    probability REAL NOT NULL,
                    expected_magnitude REAL NOT NULL,
                    uncertainty REAL NOT NULL,
                    weight REAL NOT NULL,
                    rationale_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(event_id,revision,expert,asset_id,horizon_seconds)
                );
                CREATE TABLE IF NOT EXISTS predictions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    prediction_key TEXT NOT NULL UNIQUE,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    asset_id TEXT NOT NULL,
                    horizon_seconds INTEGER NOT NULL,
                    direction_probability REAL NOT NULL,
                    calibrated_confidence REAL NOT NULL,
                    expected_magnitude REAL NOT NULL,
                    uncertainty REAL NOT NULL,
                    expected_value REAL NOT NULL,
                    risk_score REAL NOT NULL,
                    action TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    model_version_id TEXT NOT NULL,
                    feature_version TEXT NOT NULL,
                    feature_cutoff_at TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_predictions_event ON predictions(event_id,asset_id,horizon_seconds);
                CREATE INDEX IF NOT EXISTS idx_predictions_event_created ON predictions(event_id,created_at DESC);
                CREATE TABLE IF NOT EXISTS signals(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_key TEXT NOT NULL UNIQUE,
                    prediction_id INTEGER NOT NULL REFERENCES predictions(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    action TEXT NOT NULL,
                    entry_logic TEXT NOT NULL,
                    invalidation_logic TEXT NOT NULL,
                    target_logic TEXT NOT NULL,
                    exit_logic TEXT NOT NULL,
                    opened_at REAL NOT NULL,
                    closed_at REAL,
                    close_reason TEXT,
                    telegram_state TEXT NOT NULL,
                    last_updated_at REAL NOT NULL,
                    position_fraction REAL NOT NULL DEFAULT 1.0,
                    partial_exit_count INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS signal_lifecycle(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
                    state TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    market_context_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS response_targets(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id INTEGER NOT NULL REFERENCES events(id) ON DELETE CASCADE,
                    asset_id TEXT NOT NULL,
                    horizon_seconds INTEGER NOT NULL,
                    event_epoch REAL NOT NULL,
                    due_at REAL NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    next_attempt REAL NOT NULL DEFAULT 0,
                    last_error TEXT,
                    UNIQUE(event_id,asset_id,horizon_seconds)
                );
                CREATE INDEX IF NOT EXISTS idx_response_due ON response_targets(state,due_at,next_attempt,id);
                CREATE TABLE IF NOT EXISTS market_responses(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_id INTEGER NOT NULL UNIQUE REFERENCES response_targets(id) ON DELETE CASCADE,
                    event_id INTEGER NOT NULL,
                    asset_id TEXT NOT NULL,
                    horizon_seconds INTEGER NOT NULL,
                    start_price REAL NOT NULL,
                    end_price REAL NOT NULL,
                    return_value REAL NOT NULL,
                    log_return REAL NOT NULL DEFAULT 0,
                    volatility REAL NOT NULL,
                    volume REAL,
                    mfe REAL NOT NULL,
                    mae REAL NOT NULL,
                    direction TEXT NOT NULL,
                    reversal INTEGER NOT NULL,
                    continuation INTEGER NOT NULL,
                    time_to_peak_seconds REAL,
                    time_to_mfe_seconds REAL,
                    time_to_mae_seconds REAL,
                    volume_change REAL,
                    peak_impact REAL,
                    impact_decay REAL,
                    observed_samples INTEGER NOT NULL,
                    measured_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_responses_learning ON market_responses(asset_id,horizon_seconds,event_id);
                CREATE TABLE IF NOT EXISTS model_versions(
                    model_version_id TEXT PRIMARY KEY,
                    model_name TEXT NOT NULL,
                    role TEXT NOT NULL,
                    status TEXT NOT NULL,
                    parent_version_id TEXT,
                    code_version TEXT NOT NULL,
                    data_version TEXT NOT NULL,
                    feature_version TEXT NOT NULL,
                    train_start REAL,
                    train_end REAL,
                    validation_start REAL,
                    validation_end REAL,
                    hyperparameters_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    artifact_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    promoted_at REAL,
                    retired_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_one_champion ON model_versions(model_name) WHERE status='CHAMPION';
                CREATE TABLE IF NOT EXISTS experiments(
                    experiment_id TEXT PRIMARY KEY,
                    hypothesis TEXT NOT NULL,
                    status TEXT NOT NULL,
                    method TEXT NOT NULL,
                    specification_json TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE TABLE IF NOT EXISTS knowledge(
                    knowledge_key TEXT PRIMARY KEY,
                    subject TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object TEXT NOT NULL,
                    level TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    effect_mean REAL NOT NULL,
                    uncertainty REAL NOT NULL,
                    evidence_json TEXT NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS asset_profiles(
                    asset_id TEXT PRIMARY KEY,
                    profile_json TEXT NOT NULL,
                    sample_count INTEGER NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drift_events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drift_type TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    score REAL NOT NULL,
                    threshold REAL NOT NULL,
                    action TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_reports(
                    report_date TEXT PRIMARY KEY,
                    mode TEXT NOT NULL,
                    report_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_log(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    actor TEXT NOT NULL,
                    action TEXT NOT NULL,
                    object_type TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    detail_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS canonical_observations(
                    observation_id TEXT PRIMARY KEY,
                    event_time REAL NOT NULL,
                    available_time REAL NOT NULL,
                    ingestion_time REAL NOT NULL,
                    source_id TEXT NOT NULL,
                    source_quality REAL NOT NULL,
                    asset_ids_json TEXT NOT NULL,
                    entity_ids_json TEXT NOT NULL,
                    observation_type TEXT NOT NULL,
                    value REAL,
                    unit TEXT NOT NULL,
                    direction TEXT NOT NULL,
                    confidence REAL NOT NULL,
                    freshness_seconds REAL NOT NULL,
                    quality REAL NOT NULL,
                    raw_reference TEXT NOT NULL,
                    metadata_json TEXT NOT NULL,
                    replay INTEGER NOT NULL DEFAULT 0,
                    provider_id TEXT NOT NULL DEFAULT '',
                    market TEXT NOT NULL DEFAULT 'MULTI',
                    observation_family TEXT NOT NULL DEFAULT 'GENERAL'
                );
                CREATE INDEX IF NOT EXISTS idx_observations_available ON canonical_observations(available_time,observation_type);
                CREATE INDEX IF NOT EXISTS idx_observations_source ON canonical_observations(source_id,ingestion_time);
                CREATE TABLE IF NOT EXISTS feature_vectors(
                    feature_id TEXT PRIMARY KEY,
                    event_id INTEGER REFERENCES events(id) ON DELETE CASCADE,
                    asset_id TEXT NOT NULL,
                    horizon_seconds INTEGER NOT NULL,
                    feature_version TEXT NOT NULL,
                    prediction_time REAL NOT NULL,
                    available_cutoff REAL NOT NULL,
                    quality REAL NOT NULL,
                    values_json TEXT NOT NULL,
                    provenance_json TEXT NOT NULL,
                    replay INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_features_learning ON feature_vectors(asset_id,horizon_seconds,prediction_time);
                CREATE TABLE IF NOT EXISTS datasets(
                    dataset_id TEXT PRIMARY KEY,
                    expert TEXT NOT NULL,
                    asset_scope TEXT NOT NULL,
                    horizon_seconds INTEGER NOT NULL,
                    start_at REAL NOT NULL,
                    end_at REAL NOT NULL,
                    row_count INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    feature_version TEXT NOT NULL,
                    label_version TEXT NOT NULL,
                    specification_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS model_artifacts(
                    artifact_id TEXT PRIMARY KEY,
                    expert TEXT NOT NULL,
                    model_family TEXT NOT NULL,
                    state TEXT NOT NULL,
                    artifact_path TEXT NOT NULL,
                    artifact_sha256 TEXT NOT NULL,
                    dataset_id TEXT NOT NULL REFERENCES datasets(dataset_id),
                    feature_version TEXT NOT NULL,
                    code_version TEXT NOT NULL,
                    seed INTEGER NOT NULL,
                    training_start REAL NOT NULL,
                    training_end REAL NOT NULL,
                    validation_start REAL NOT NULL,
                    validation_end REAL NOT NULL,
                    hyperparameters_json TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    calibration_json TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    promoted_at REAL,
                    retired_at REAL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS idx_artifact_champion ON model_artifacts(expert) WHERE state='CHAMPION';
                CREATE TABLE IF NOT EXISTS expert_outputs(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature_id TEXT NOT NULL REFERENCES feature_vectors(feature_id),
                    expert TEXT NOT NULL,
                    artifact_id TEXT NOT NULL,
                    model_state TEXT NOT NULL,
                    probability REAL NOT NULL,
                    uncertainty REAL NOT NULL,
                    created_at REAL NOT NULL,
                    UNIQUE(feature_id,expert,artifact_id)
                );
                CREATE TABLE IF NOT EXISTS shadow_predictions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    feature_id TEXT NOT NULL REFERENCES feature_vectors(feature_id),
                    artifact_id TEXT NOT NULL REFERENCES model_artifacts(artifact_id),
                    probability REAL NOT NULL,
                    prediction_time REAL NOT NULL,
                    outcome REAL,
                    label INTEGER,
                    resolved_at REAL,
                    UNIQUE(feature_id,artifact_id)
                );
                CREATE TABLE IF NOT EXISTS drift_actions(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drift_event_id INTEGER,
                    drift_type TEXT NOT NULL,
                    requested_action TEXT NOT NULL,
                    execution_status TEXT NOT NULL,
                    execution_time REAL,
                    affected_component TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS drift_remediation_queue(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drift_action_id INTEGER NOT NULL REFERENCES drift_actions(id) ON DELETE CASCADE,
                    drift_type TEXT NOT NULL,
                    remediation_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS idx_drift_remediation_state ON drift_remediation_queue(state,id);
                CREATE TABLE IF NOT EXISTS reference_prices(
                    provider TEXT NOT NULL,
                    asset_id TEXT NOT NULL,
                    observed_at REAL NOT NULL,
                    available_at REAL NOT NULL,
                    price REAL NOT NULL,
                    volume REAL,
                    quality REAL NOT NULL,
                    metadata_json TEXT NOT NULL,
                    PRIMARY KEY(provider,asset_id,observed_at)
                );
                CREATE INDEX IF NOT EXISTS idx_reference_price_lookup ON reference_prices(asset_id,observed_at,available_at);
                CREATE TABLE IF NOT EXISTS active_asset_watchlist(
                    asset_id TEXT PRIMARY KEY REFERENCES assets(asset_id),reason TEXT NOT NULL,event_id INTEGER,
                    activated_at REAL NOT NULL,expires_at REAL NOT NULL,last_polled_at REAL NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT 'ACTIVE'
                );
                CREATE TABLE IF NOT EXISTS hypotheses(
                    hypothesis_id TEXT PRIMARY KEY,
                    statement TEXT NOT NULL,
                    status TEXT NOT NULL,
                    scope_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    effect_size REAL,
                    confidence_low REAL,
                    confidence_high REAL,
                    sample_count INTEGER NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS failure_attribution(
                    prediction_id INTEGER PRIMARY KEY REFERENCES predictions(id),
                    category TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS missed_opportunities(
                    response_id INTEGER PRIMARY KEY REFERENCES market_responses(id),event_id INTEGER NOT NULL,asset_id TEXT NOT NULL,
                    return_value REAL NOT NULL,root_cause TEXT NOT NULL,evidence_json TEXT NOT NULL,created_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS post_exit(
                    signal_id INTEGER NOT NULL REFERENCES signals(id),horizon_seconds INTEGER NOT NULL,classification TEXT NOT NULL,
                    return_after_exit REAL NOT NULL,evidence_json TEXT NOT NULL,created_at REAL NOT NULL,PRIMARY KEY(signal_id,horizon_seconds)
                );
                CREATE TABLE IF NOT EXISTS active_signal_snapshots(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    signal_id INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
                    observed_at REAL NOT NULL, price REAL, return_since_entry REAL,
                    mfe_so_far REAL NOT NULL, mae_so_far REAL NOT NULL,
                    derivatives_json TEXT NOT NULL, onchain_json TEXT NOT NULL,
                    liquidity REAL, microstructure_json TEXT NOT NULL, volatility REAL,
                    regime TEXT, fundamental_json TEXT NOT NULL, remaining_ev REAL NOT NULL,
                    action TEXT NOT NULL, evidence_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_active_signal_snapshots ON active_signal_snapshots(signal_id,observed_at DESC);
                CREATE TABLE IF NOT EXISTS post_exit_targets(
                    signal_id INTEGER NOT NULL REFERENCES signals(id) ON DELETE CASCADE,
                    horizon_seconds INTEGER NOT NULL, due_at REAL NOT NULL, state TEXT NOT NULL DEFAULT 'PENDING',
                    attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT,
                    PRIMARY KEY(signal_id,horizon_seconds)
                );
                CREATE INDEX IF NOT EXISTS idx_post_exit_targets_due ON post_exit_targets(state,due_at);
                CREATE TABLE IF NOT EXISTS asset_aliases(
                    alias TEXT NOT NULL, asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    source TEXT NOT NULL, confidence REAL NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(alias,asset_id)
                );
                CREATE TABLE IF NOT EXISTS provider_symbols(
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, provider_symbol TEXT NOT NULL, exchange TEXT NOT NULL,
                    updated_at REAL NOT NULL, PRIMARY KEY(asset_id,provider)
                );
                CREATE TABLE IF NOT EXISTS dex_pairs(
                    asset_id TEXT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
                    provider TEXT NOT NULL, chain TEXT NOT NULL, contract TEXT NOT NULL,
                    pair_address TEXT NOT NULL, quote_symbol TEXT NOT NULL, liquidity_usd REAL,
                    volume_24h REAL, url TEXT, metadata_json TEXT NOT NULL, updated_at REAL NOT NULL,
                    PRIMARY KEY(asset_id,provider,pair_address)
                );
                CREATE TABLE IF NOT EXISTS improvement_records(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    metric TEXT NOT NULL,
                    before_value REAL NOT NULL,
                    after_value REAL NOT NULL,
                    sample_size INTEGER NOT NULL,
                    validation_window TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            con.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,applied_at,checksum) VALUES(?,?,?)",
                (SCHEMA_VERSION, time.time(), "v59-impact-router-structured-data-schema-10"),
            )
            response_columns={str(r['name']) for r in con.execute('PRAGMA table_info(market_responses)').fetchall()}
            for name,definition in (('log_return','REAL NOT NULL DEFAULT 0'),('time_to_mfe_seconds','REAL'),('time_to_mae_seconds','REAL'),('volume_change','REAL'),('peak_impact','REAL'),('impact_decay','REAL')):
                if name not in response_columns:con.execute(f'ALTER TABLE market_responses ADD COLUMN {name} {definition}')
            event_columns={str(r['name']) for r in con.execute('PRAGMA table_info(events)').fetchall()}
            for name,definition in (('subtype',"TEXT NOT NULL DEFAULT 'GENERAL'"),('country',"TEXT NOT NULL DEFAULT 'GLOBAL'"),('sector',"TEXT NOT NULL DEFAULT 'GENERAL'"),('priced_in_score','REAL NOT NULL DEFAULT .5'),('historical_analogues_json',"TEXT NOT NULL DEFAULT '[]'"),('raw_surprise','REAL'),('standardized_surprise','REAL'),('surprise_unit',"TEXT NOT NULL DEFAULT 'native'"),('surprise_family',"TEXT NOT NULL DEFAULT 'GENERAL'")):
                if name not in event_columns:con.execute(f'ALTER TABLE events ADD COLUMN {name} {definition}')
            relation_columns={str(r['name']) for r in con.execute('PRAGMA table_info(relations)').fetchall()}
            if 'provenance_json' not in relation_columns:con.execute("ALTER TABLE relations ADD COLUMN provenance_json TEXT NOT NULL DEFAULT '{}'")
            observation_columns={str(r['name']) for r in con.execute('PRAGMA table_info(canonical_observations)').fetchall()}
            for name,definition in (('provider_id',"TEXT NOT NULL DEFAULT ''"),('market',"TEXT NOT NULL DEFAULT 'MULTI'"),('observation_family',"TEXT NOT NULL DEFAULT 'GENERAL'")):
                if name not in observation_columns:con.execute(f'ALTER TABLE canonical_observations ADD COLUMN {name} {definition}')
            feature_columns={str(r['name']) for r in con.execute('PRAGMA table_info(feature_vectors)').fetchall()}
            if 'freshness_json' not in feature_columns:con.execute("ALTER TABLE feature_vectors ADD COLUMN freshness_json TEXT NOT NULL DEFAULT '{}'")
            post_exit_columns={str(r['name']) for r in con.execute('PRAGMA table_info(post_exit)').fetchall()}
            for name,definition in (('max_gain_after_exit','REAL NOT NULL DEFAULT 0'),('max_loss_after_exit','REAL NOT NULL DEFAULT 0'),('time_to_post_exit_peak','REAL'),('time_to_post_exit_trough','REAL')):
                if name not in post_exit_columns:con.execute(f'ALTER TABLE post_exit ADD COLUMN {name} {definition}')
            signal_columns={str(r['name']) for r in con.execute('PRAGMA table_info(signals)').fetchall()}
            for name,definition in (('position_fraction','REAL NOT NULL DEFAULT 1.0'),('partial_exit_count','INTEGER NOT NULL DEFAULT 0'),
                ('original_position_fraction','REAL NOT NULL DEFAULT 1.0'),('partial_exit_price','REAL'),('partial_exit_time','REAL'),
                ('partial_realized_result','REAL NOT NULL DEFAULT 0')):
                if name not in signal_columns:con.execute(f'ALTER TABLE signals ADD COLUMN {name} {definition}')
            shadow_columns={str(r['name']) for r in con.execute('PRAGMA table_info(shadow_predictions)').fetchall()}
            for name,definition in (('signal_id','INTEGER'),('target_type',"TEXT NOT NULL DEFAULT 'direction'"),('label_text','TEXT')):
                if name not in shadow_columns:con.execute(f'ALTER TABLE shadow_predictions ADD COLUMN {name} {definition}')
            snapshot_columns={str(r['name']) for r in con.execute('PRAGMA table_info(active_signal_snapshots)').fetchall()}
            for name,definition in (('feature_id','TEXT'),('inference_json',"TEXT NOT NULL DEFAULT '{}'")):
                if name not in snapshot_columns:con.execute(f'ALTER TABLE active_signal_snapshots ADD COLUMN {name} {definition}')
            self._set_meta_con(con, "deployment_started_at", str(time.time()), overwrite=False)
            self._set_meta_con(con, "story_cursor", "0", overwrite=False)
            self._set_meta_con(con, "signals_enabled", "0", overwrite=False)
            self._set_meta_con(con, "observation_runtime_seconds", "0", overwrite=False)
            self._set_meta_con(con, "shadow_runtime_seconds", "0", overwrite=False)
            self._set_meta_con(con, "last_runtime_tick", "0", overwrite=False)
            self._ensure_baseline_model(con)

    async def record_semantic_memory(self, event_id: int, content_hash: str, model: str, embedding: list[float], analysis: dict[str, Any] | None, text_preview: str) -> None:
        await asyncio.to_thread(self._record_semantic_memory_sync, event_id, content_hash, model, embedding, analysis or {}, text_preview)

    def _record_semantic_memory_sync(self, event_id: int, content_hash: str, model: str, embedding: list[float], analysis: dict[str, Any], text_preview: str) -> None:
        values=[float(value) for value in embedding]
        if not content_hash or not values or any(not math.isfinite(value) for value in values):
            raise ValueError("invalid semantic memory")
        with connect(self.path,self.settings) as con:
            transaction(con)
            try:
                con.execute("""INSERT OR IGNORE INTO semantic_memories(content_hash,embedding_model,dimensions,embedding_json,analysis_json,text_preview,created_at)
                    VALUES(?,?,?,?,?,?,?)""",(content_hash,model,len(values),json.dumps(values,separators=(',',':')),json.dumps(analysis,ensure_ascii=False,sort_keys=True,default=str),text_preview[:2000],time.time()))
                con.execute("INSERT OR IGNORE INTO event_semantic_links(event_id,content_hash,created_at) VALUES(?,?,?)",(event_id,content_hash,time.time()))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    @staticmethod
    def _set_meta_con(con: sqlite3.Connection, key: str, value: str, *, overwrite: bool = True) -> None:
        if overwrite:
            con.execute(
                "INSERT INTO metadata(key,value,updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, value, time.time()),
            )
        else:
            con.execute("INSERT OR IGNORE INTO metadata(key,value,updated_at) VALUES(?,?,?)", (key, value, time.time()))

    @staticmethod
    def _ensure_baseline_model(con: sqlite3.Connection) -> None:
        if con.execute("SELECT 1 FROM model_versions WHERE model_name='meta_ensemble' AND status='CHAMPION'").fetchone():
            return
        now = time.time()
        con.execute(
            """INSERT INTO model_versions(model_version_id,model_name,role,status,code_version,data_version,feature_version,
               hyperparameters_json,metrics_json,artifact_json,created_at,promoted_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                "meta-ensemble-56.0-bootstrap", "meta_ensemble", "META", "CHAMPION", "57.3.0",
                "bootstrap", "features-57.3.0", json.dumps({"prior_strength": 8, "min_samples": 30,"live_signal_eligible":False}),
                json.dumps({"status": "bootstrap_unvalidated"}), json.dumps({"expert_weights": {}}), now, now,
            ),
        )

    async def get_meta(self, key: str, default: str = "") -> str:
        return await asyncio.to_thread(self._get_meta_sync, key, default)

    def _get_meta_sync(self, key: str, default: str) -> str:
        with connect(self.path, self.settings) as con:
            row = con.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
            return str(row["value"]) if row else default

    async def set_meta(self, key: str, value: str) -> None:
        await asyncio.to_thread(self._set_meta_sync, key, value)

    def _set_meta_sync(self, key: str, value: str) -> None:
        with connect(self.path, self.settings) as con:
            self._set_meta_con(con, key, value)

    async def historical_similarity(self, event_type: str, title: str) -> float:
        return await asyncio.to_thread(self._historical_similarity_sync, event_type, title)

    def _historical_similarity_sync(self, event_type: str, title: str) -> float:
        tokens = set(x for x in title.lower().split() if len(x) > 2)
        if not tokens:
            return 0.0
        with connect(self.path, self.settings) as con:
            rows = con.execute("SELECT title FROM events WHERE event_type=? ORDER BY id DESC LIMIT 100", (event_type,)).fetchall()
        best = 0.0
        for row in rows:
            other = set(x for x in str(row["title"]).lower().split() if len(x) > 2)
            union = tokens | other
            if union:
                best = max(best, len(tokens & other) / len(union))
        return best

    async def ingest_event(self, understanding: EventUnderstanding, story: dict[str, Any]) -> tuple[int, bool, bool]:
        return await asyncio.to_thread(self._ingest_event_sync, understanding, story)

    def _ingest_event_sync(self, u: EventUnderstanding, story: dict[str, Any]) -> tuple[int, bool, bool]:
        story_id = int(story["id"])
        now = time.time()
        dt = parse_datetime(u.occurred_at) or parse_datetime(u.observed_at)
        event_epoch = dt.timestamp() if dt else now
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                source_change_id = int(story.get("_change_id") or story_id)
                existing_update = con.execute(
                    """SELECT event_id,source_change_id FROM event_updates
                       WHERE source_change_id=? OR update_key=?
                       ORDER BY CASE WHEN source_change_id=? THEN 0 ELSE 1 END LIMIT 1""",
                    (source_change_id, u.update_key, source_change_id),
                ).fetchone()
                if existing_update:
                    event_id = int(existing_update["event_id"])
                    # A semantically identical update can arrive from another
                    # story/source change.  update_key is intentionally unique;
                    # retain the extra provenance instead of retrying a doomed
                    # INSERT forever and pinning the FIFO cursor.
                    if int(existing_update["source_change_id"]) != source_change_id:
                        source_id = str(story.get("source_id") or story.get("source_name") or story.get("source_url") or "unknown")[:300]
                        provider_id = str(story.get("provider_id") or story.get("source_name") or "unknown")[:300]
                        lineage = con.execute(
                            """INSERT OR IGNORE INTO event_source_lineage(
                                   event_id,story_id,source_change_id,source_id,provider_id,observed_at,created_at
                               ) VALUES(?,?,?,?,?,?,?)""",
                            (event_id, story_id, source_change_id, source_id, provider_id, u.observed_at, now),
                        )
                        if lineage.rowcount:
                            con.execute(
                                """INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at)
                                   VALUES(?,?,?,?,?,?)""",
                                (
                                    "intelligence", "EVENT_UPDATE_DEDUPLICATED", "event", str(event_id),
                                    json.dumps({"story_id": story_id, "source_change_id": source_change_id}), now,
                                ),
                            )
                    con.execute("COMMIT")
                    return event_id, False, False
                row = con.execute("SELECT event_id AS id FROM event_updates WHERE story_id=? ORDER BY id DESC LIMIT 1", (story_id,)).fetchone()
                if row is None:
                    row = con.execute("SELECT id FROM events WHERE cluster_key=?", (u.cluster_key,)).fetchone()
                if row is None:
                    # Cross-source semantic clustering: compare only temporally adjacent events
                    # of the same canonical type. This prevents Reuters/AP syndication from
                    # creating duplicate learning while avoiding broad all-day merges.
                    candidates = con.execute(
                        "SELECT id,title FROM events WHERE event_type=? AND created_at>? ORDER BY id DESC LIMIT 50",
                        (u.event_type, now - 21600),
                    ).fetchall()
                    current_tokens = {x for x in u.title.lower().split() if len(x) > 2}
                    best_id, best_similarity = None, 0.0
                    for candidate in candidates:
                        other_tokens = {x for x in str(candidate["title"]).lower().split() if len(x) > 2}
                        union = current_tokens | other_tokens
                        similarity = len(current_tokens & other_tokens) / len(union) if union else 0.0
                        if similarity > best_similarity:
                            best_id, best_similarity = int(candidate["id"]), similarity
                    if best_id is not None and best_similarity >= .45:
                        row = {"id": best_id}
                is_new = row is None
                if is_new:
                    cur = con.execute(
                        """INSERT INTO events(cluster_key,event_type,domain,title,summary,occurred_at,first_observed_at,last_observed_at,
                           importance,novelty,surprise,source_credibility,priority,state,extraction_method,extraction_version,created_at,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (u.cluster_key, u.event_type, u.domain, u.title, u.summary, u.occurred_at, u.observed_at, u.observed_at,
                         u.importance, u.novelty, u.surprise, u.source_credibility, u.priority, u.event_state,
                         u.extraction_method, u.extraction_version, now, now),
                    )
                    event_id = int(cur.lastrowid)
                else:
                    event_id = int(row["id"])
                    con.execute(
                        """UPDATE events SET last_observed_at=?,importance=MAX(importance,?),novelty=MAX(novelty,?),
                           source_credibility=MAX(source_credibility,?),state=?,update_count=update_count+1,updated_at=? WHERE id=?""",
                        (u.observed_at, u.importance, u.novelty, u.source_credibility, u.event_state, now, event_id),
                    )
                con.execute(
                    """INSERT INTO event_updates(event_id,story_id,source_change_id,update_key,state,title,summary,source_name,source_url,observed_at,created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (event_id, story_id, source_change_id, u.update_key, u.event_state, u.title, u.summary, str(story.get("source_name") or ""),
                     str(story.get("source_url") or ""), u.observed_at, now),
                )
                source_id = str(story.get("source_id") or story.get("source_name") or story.get("source_url") or "unknown")[:300]
                provider_id = str(story.get("provider_id") or story.get("source_name") or "unknown")[:300]
                con.execute(
                    """INSERT OR IGNORE INTO event_source_lineage(event_id,story_id,source_change_id,source_id,provider_id,observed_at,created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (event_id,story_id,source_change_id,source_id,provider_id,u.observed_at,now),
                )
                for entity in u.entities:
                    con.execute(
                        """INSERT INTO entities(entity_id,name,kind,updated_at) VALUES(?,?,?,?)
                           ON CONFLICT(entity_id) DO UPDATE SET name=excluded.name,kind=excluded.kind,updated_at=excluded.updated_at""",
                        (entity["entity_id"], entity["name"], entity["kind"], now),
                    )
                    con.execute("INSERT OR IGNORE INTO event_entities(event_id,entity_id,relation,confidence) VALUES(?,?,?,?)", (event_id, entity["entity_id"], "SUBJECT", .9))
                for asset in u.assets:
                    con.execute(
                        """INSERT INTO assets(asset_id,symbol,name,asset_class,market_symbol,metadata_json,updated_at)
                           VALUES(?,?,?,?,?,'{}',?) ON CONFLICT(asset_id) DO UPDATE SET symbol=excluded.symbol,name=excluded.name,
                           asset_class=excluded.asset_class,market_symbol=excluded.market_symbol,updated_at=excluded.updated_at""",
                        (asset["asset_id"], asset["symbol"], asset["name"], asset["asset_class"], asset["market_symbol"], now),
                    )
                    con.execute(
                        """INSERT INTO event_assets(event_id,asset_id,impact_order,directional_bias,mapping_confidence,mapping_reason)
                           VALUES(?,?,?,?,?,?) ON CONFLICT(event_id,asset_id) DO UPDATE SET
                           mapping_confidence=MAX(mapping_confidence,excluded.mapping_confidence),mapping_reason=excluded.mapping_reason""",
                        (event_id, asset["asset_id"], asset["impact_order"], asset["directional_bias"], asset["mapping_confidence"], asset["mapping_reason"]),
                    )
                    if bool(story.get("_create_response_targets", True)):
                        for horizon in u.expected_horizons:
                            con.execute(
                                "INSERT OR IGNORE INTO response_targets(event_id,asset_id,horizon_seconds,event_epoch,due_at) VALUES(?,?,?,?,?)",
                                (event_id, asset["asset_id"], int(horizon), event_epoch, event_epoch + int(horizon)),
                            )
                for relation in u.relations:
                    provenance={'event_id':event_id,'story_id':story_id,'source_change_id':source_change_id,'source':str(story.get('source_name') or story.get('source_url') or 'unknown'),'observed_at':u.observed_at,'extraction_method':u.extraction_method,'extraction_version':u.extraction_version}
                    con.execute("INSERT OR IGNORE INTO relations(event_id,source_id,relation,target_id,provenance_json) VALUES(?,?,?,?,?)", (event_id, relation["source_id"], relation["relation"], relation["target_id"],json.dumps(provenance)))
                con.execute("UPDATE events SET subtype=?,country=?,sector=?,priced_in_score=?,historical_analogues_json=? WHERE id=?",(u.subtype,u.country,u.sector,u.priced_in_score,json.dumps(u.historical_analogues),event_id))
                source_id = str(story.get("source_id") or story.get("source_name") or story.get("source_url") or "unknown")[:300]
                primary = 1 if u.source_credibility >= .9 else 0
                correction = 1 if u.event_state == "CORRECTION" else 0
                duplicate = 0 if is_new else 1
                con.execute(
                    """INSERT INTO source_profiles(source_id,observations,primary_count,correction_count,duplicate_count,reliability,updated_at)
                       VALUES(?,1,?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET observations=observations+1,
                       primary_count=primary_count+excluded.primary_count,correction_count=correction_count+excluded.correction_count,
                       duplicate_count=duplicate_count+excluded.duplicate_count,
                       reliability=(reliability*observations+excluded.reliability)/(observations+1),updated_at=excluded.updated_at""",
                    (source_id, primary, correction, duplicate, u.source_credibility, now),
                )
                con.execute(
                    "INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                    ("intelligence", "EVENT_INGESTED" if is_new else "EVENT_UPDATED", "event", str(event_id), json.dumps({"story_id": story_id, "source_change_id": source_change_id, "state": u.event_state}), now),
                )
                con.execute("COMMIT")
                return event_id, is_new, not is_new
            except BaseException:
                con.execute("ROLLBACK")
                raise

    async def event_context(self, event_id: int) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._event_context_sync, event_id)

    async def latest_prediction_at(self, event_id: int) -> float | None:
        return await asyncio.to_thread(self._latest_prediction_at_sync, event_id)

    def _latest_prediction_at_sync(self, event_id: int) -> float | None:
        with connect(self.path, self.settings) as con:
            row = con.execute(
                "SELECT created_at FROM predictions WHERE event_id=? ORDER BY created_at DESC LIMIT 1",
                (event_id,),
            ).fetchone()
            return float(row['created_at']) if row else None

    def _event_context_sync(self, event_id: int) -> dict[str, Any] | None:
        with connect(self.path, self.settings) as con:
            event = con.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
            if not event:
                return None
            result = dict(event)
            result["assets"] = [dict(r) for r in con.execute("SELECT ea.*,a.symbol,a.name,a.asset_class,a.market_symbol FROM event_assets ea JOIN assets a USING(asset_id) WHERE ea.event_id=?", (event_id,)).fetchall()]
            result["sources"] = [dict(r) for r in con.execute("SELECT source_name,source_url,title,summary,observed_at FROM event_updates WHERE event_id=? ORDER BY id",(event_id,)).fetchall()]
            result["entities"] = [dict(r) for r in con.execute("SELECT e.* FROM event_entities ee JOIN entities e USING(entity_id) WHERE ee.event_id=?", (event_id,)).fetchall()]
            return result

    async def record_regime(self, asset_id: str, regime: dict[str, Any]) -> None:
        await asyncio.to_thread(self._record_regime_sync, asset_id, regime)

    def _record_regime_sync(self, asset_id: str, regime: dict[str, Any]) -> None:
        with connect(self.path, self.settings) as con:
            con.execute(
                """INSERT INTO regimes(asset_id,observed_at,regime,trend_score,volatility_score,change_score,liquidity_score,confidence,features_json)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (asset_id, time.time(), regime["regime"], regime["trend_score"], regime["volatility_score"], regime["change_score"],
                 regime["liquidity_score"], regime["confidence"], json.dumps({**regime.get("features", {}),"probabilities":regime.get('probabilities',{}),"uncertainty":regime.get('uncertainty'),"transition_probability":regime.get('transition_probability')}, separators=(",", ":"))),
            )

    async def latest_regime(self, asset_id: str) -> dict[str, Any] | None:
        return await asyncio.to_thread(self._latest_regime_sync, asset_id)

    def _latest_regime_sync(self, asset_id: str) -> dict[str, Any] | None:
        with connect(self.path, self.settings) as con:
            row = con.execute("SELECT * FROM regimes WHERE asset_id=? ORDER BY observed_at DESC LIMIT 1", (asset_id,)).fetchone()
            return dict(row) if row else None

    async def historical_outcomes(self, event_type: str, asset_id: str, horizon: int, limit: int = 500) -> list[float]:
        return await asyncio.to_thread(self._historical_outcomes_sync, event_type, asset_id, horizon, limit)

    def _historical_outcomes_sync(self, event_type: str, asset_id: str, horizon: int, limit: int) -> list[float]:
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT mr.return_value FROM market_responses mr JOIN events e ON e.id=mr.event_id
                   WHERE e.event_type=? AND mr.asset_id=? AND mr.horizon_seconds=? ORDER BY mr.id DESC LIMIT ?""",
                (event_type, asset_id, horizon, limit),
            ).fetchall()
            return [float(r["return_value"]) for r in rows]

    async def record_decision(self, event_id: int, votes: Iterable[ExpertVote], decision: SignalDecision, revision: int = 1, inference: dict[str, Any] | None = None, feature: dict[str, Any] | None = None) -> tuple[int, int]:
        return await asyncio.to_thread(self._record_decision_sync, event_id, list(votes), decision, revision, inference or {}, feature or {})

    def _record_decision_sync(self, event_id: int, votes: list[ExpertVote], d: SignalDecision, revision: int, inference: dict[str, Any], feature: dict[str, Any]) -> tuple[int, int]:
        now = time.time()
        feature_id = str(feature.get("feature_id") or "features-57.3.0")
        prediction_key = f"event:{event_id}:revision:{revision}:{d.asset_id}:{d.horizon_seconds}:{feature_id}"
        signal_key = f"signal:{prediction_key}"
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                for vote in votes:
                    con.execute(
                        """INSERT OR IGNORE INTO expert_votes(event_id,revision,expert,asset_id,horizon_seconds,probability,expected_magnitude,
                           uncertainty,weight,rationale_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (event_id, revision, vote.expert, vote.asset_id, vote.horizon_seconds, vote.direction_probability, vote.expected_magnitude,
                         vote.uncertainty, vote.weight, json.dumps(vote.rationale), now),
                    )
                artifact_ids = list(inference.get("artifact_ids") or [])
                model = con.execute("SELECT model_version_id FROM model_versions WHERE model_name='meta_ensemble' AND status='CHAMPION'").fetchone()
                model_id = str(inference.get('controlling_artifact_id') or '') or next((x for x in artifact_ids if ':meta:' in x), None) or (str(model["model_version_id"]) if model else "meta-ensemble-54.0-bootstrap")
                con.execute(
                    """INSERT OR IGNORE INTO predictions(prediction_key,event_id,asset_id,horizon_seconds,direction_probability,
                       calibrated_confidence,expected_magnitude,uncertainty,expected_value,risk_score,action,mode,model_version_id,
                       feature_version,feature_cutoff_at,evidence_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (prediction_key, event_id, d.asset_id, d.horizon_seconds, d.probability, d.calibrated_confidence, d.expected_magnitude,
                     d.uncertainty, d.expected_value, d.risk_score, d.action, d.mode, model_id, str(feature.get("feature_version") or "features-57.3.0"),
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(feature.get("available_cutoff") or now))), json.dumps({"reasons": d.reasons, "evidence_count": d.evidence_count, "inference": inference, "feature_id": feature_id}), now),
                )
                prediction = con.execute("SELECT id FROM predictions WHERE prediction_key=?", (prediction_key,)).fetchone()
                prediction_id = int(prediction["id"])
                live_qualified = bool(inference.get('live_qualified'))
                telegram_state = "ELIGIBLE" if d.mode == "LIVE" and live_qualified and d.action not in {"NO_TRADE", "WAIT"} else "SUPPRESSED"
                signal_state = "QUALIFIED" if d.mode == "LIVE" and live_qualified and d.action not in {"NO_TRADE", "WAIT"} else "CREATED" if d.action not in {"NO_TRADE", "WAIT"} else "INVALIDATED"
                con.execute(
                    """INSERT OR IGNORE INTO signals(signal_key,prediction_id,state,action,entry_logic,invalidation_logic,target_logic,
                       exit_logic,opened_at,telegram_state,last_updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (signal_key, prediction_id, signal_state, d.action, d.entry_logic, d.invalidation_logic, d.target_logic,
                     d.exit_logic, now, telegram_state, now),
                )
                signal = con.execute("SELECT id FROM signals WHERE signal_key=?", (signal_key,)).fetchone()
                signal_id = int(signal["id"])
                con.execute(
                    "INSERT OR IGNORE INTO signal_lifecycle(signal_id,state,reason,market_context_json,created_at) VALUES(?,?,?,?,?)",
                    (signal_id, signal_state, ";".join(d.reasons), json.dumps({"mode": d.mode, "risk": d.risk_score}), now),
                )
                con.execute("COMMIT")
                return prediction_id, signal_id
            except BaseException:
                con.execute("ROLLBACK")
                raise

    async def pending_responses(self, now: float, limit: int = 100) -> list[dict[str, Any]]:
        return await asyncio.to_thread(self._pending_responses_sync, now, limit)

    def _pending_responses_sync(self, now: float, limit: int) -> list[dict[str, Any]]:
        with connect(self.path, self.settings) as con:
            rows = con.execute(
                """SELECT rt.*,a.market_symbol,a.asset_class FROM response_targets rt JOIN assets a USING(asset_id)
                   WHERE rt.state='pending' AND rt.due_at<=? AND rt.next_attempt<=? ORDER BY rt.due_at,id LIMIT ?""",
                (now, now, limit),
            ).fetchall()
            return [dict(r) for r in rows]

    async def retry_response(self, target_id: int, error: str, delay: float, dead: bool = False) -> None:
        await asyncio.to_thread(self._retry_response_sync, target_id, error, delay, dead)

    def _retry_response_sync(self, target_id: int, error: str, delay: float, dead: bool) -> None:
        with connect(self.path, self.settings) as con:
            con.execute(
                "UPDATE response_targets SET state=?,attempts=attempts+1,next_attempt=?,last_error=? WHERE id=?",
                ("unobservable" if dead else "pending", time.time() + delay, error[:1000], target_id),
            )

    async def complete_response(self, target: dict[str, Any], response: dict[str, Any]) -> None:
        await asyncio.to_thread(self._complete_response_sync, target, response)

    def _complete_response_sync(self, target: dict[str, Any], response: dict[str, Any]) -> None:
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                con.execute(
                    """INSERT OR IGNORE INTO market_responses(target_id,event_id,asset_id,horizon_seconds,start_price,end_price,
                       return_value,log_return,volatility,volume,mfe,mae,direction,reversal,continuation,time_to_peak_seconds,time_to_mfe_seconds,
                       time_to_mae_seconds,volume_change,peak_impact,impact_decay,observed_samples,measured_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (target["id"], target["event_id"], target["asset_id"], target["horizon_seconds"], response["start_price"],
                     response["end_price"], response["return_value"], response.get("log_return",0.0), response["volatility"], response.get("volume"), response["mfe"],
                     response["mae"], response["direction"], int(response["reversal"]), int(response["continuation"]),
                     response.get("time_to_peak_seconds"),response.get("time_to_mfe_seconds"),response.get("time_to_mae_seconds"),response.get("volume_change"),
                     response.get("peak_impact"),response.get("impact_decay"),response["observed_samples"], time.time()),
                )
                con.execute("UPDATE response_targets SET state='complete',last_error=NULL WHERE id=?", (target["id"],))
                con.execute("COMMIT")
            except BaseException:
                con.execute("ROLLBACK")
                raise

    async def mode(self) -> str:
        from .warmup import RuntimeWarmup
        return str((await RuntimeWarmup(self.settings).status())["mode"])

    async def summary(self) -> dict[str, Any]:
        return await asyncio.to_thread(self._summary_sync)

    async def rollback_model(self, target_version: str | None = None) -> dict[str, Any]:
        """Atomically restore a previously promoted model and preserve the audit trail."""
        return await asyncio.to_thread(self._rollback_model_sync, target_version)

    def _rollback_model_sync(self, target_version: str | None) -> dict[str, Any]:
        with connect(self.path, self.settings) as con:
            transaction(con)
            try:
                current = con.execute(
                    "SELECT * FROM model_versions WHERE model_name='meta_ensemble' AND status='CHAMPION'"
                ).fetchone()
                if current is None:
                    raise RuntimeError("no current champion")
                if target_version:
                    target = con.execute(
                        "SELECT * FROM model_versions WHERE model_version_id=? AND model_name='meta_ensemble' AND status='RETIRED'",
                        (target_version,),
                    ).fetchone()
                else:
                    target = con.execute(
                        "SELECT * FROM model_versions WHERE model_name='meta_ensemble' AND status='RETIRED' ORDER BY COALESCE(retired_at,created_at) DESC LIMIT 1"
                    ).fetchone()
                if target is None:
                    con.execute("ROLLBACK")
                    return {"rolled_back": False, "reason": "no_eligible_retired_model", "current": current["model_version_id"]}
                now = time.time()
                con.execute(
                    "UPDATE model_versions SET status='RETIRED',retired_at=? WHERE model_version_id=?",
                    (now, current["model_version_id"]),
                )
                con.execute(
                    "UPDATE model_versions SET status='CHAMPION',promoted_at=?,retired_at=NULL WHERE model_version_id=?",
                    (now, target["model_version_id"]),
                )
                detail = {
                    "from": current["model_version_id"],
                    "to": target["model_version_id"],
                    "reason": "operator_or_drift_rollback",
                }
                con.execute(
                    "INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at) VALUES('governance','MODEL_ROLLBACK','model',?,?,?)",
                    (target["model_version_id"], json.dumps(detail), now),
                )
                con.execute("COMMIT")
                return {"rolled_back": True, **detail}
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise

    def _summary_sync(self) -> dict[str, Any]:
        tables = ("events", "event_updates", "canonical_observations", "feature_vectors", "predictions", "signals", "market_responses", "model_versions", "model_artifacts", "shadow_predictions", "datasets", "experiments", "hypotheses", "knowledge", "failure_attribution", "missed_opportunities", "post_exit", "drift_events")
        with connect(self.path, self.settings) as con:
            counts = {name: int(con.execute(f"SELECT COUNT(*) c FROM {name}").fetchone()["c"]) for name in tables}
            pending = int(con.execute("SELECT COUNT(*) c FROM response_targets WHERE state='pending'").fetchone()["c"])
            unobservable = int(con.execute("SELECT COUNT(*) c FROM response_targets WHERE state='unobservable'").fetchone()["c"])
            return {"schema_version": SCHEMA_VERSION, "counts": counts, "response_pending": pending, "response_unobservable": unobservable}

    async def audit(self, actor: str, action: str, object_type: str, object_id: str, detail: dict[str, Any]) -> None:
        await asyncio.to_thread(self._audit_sync, actor, action, object_type, object_id, detail)

    def _audit_sync(self, actor: str, action: str, object_type: str, object_id: str, detail: dict[str, Any]) -> None:
        with connect(self.path, self.settings) as con:
            con.execute(
                "INSERT INTO audit_log(actor,action,object_type,object_id,detail_json,created_at) VALUES(?,?,?,?,?,?)",
                (actor, action, object_type, object_id, json.dumps(detail, ensure_ascii=False, default=str), time.time()),
            )
