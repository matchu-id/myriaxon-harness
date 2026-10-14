#!/usr/bin/env python3
"""Verify every SQL code question's expected values by actually running the reference queries.

An unverified `expected` value is the worst kind of content bug: the learner writes a correct query, the
test disagrees, and the exam marks them wrong with no way to tell why. Guessing at expected output is not
acceptable, so this executes each reference solution against each question's own setupSql in SQLite and
compares the result to what the file claims.

SQLite is not sql.js, but for plain SELECT/GROUP BY/ORDER BY/LIMIT the semantics match, which is all
these questions use.

Usage:  python3 verify_code_questions.py <level.json>
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

# A reference solution per code question. The author writes these; they are the queries a competent
# learner would submit. Matching is by a distinctive phrase in the prompt rather than by position, so
# reordering a file cannot silently pair a question with the wrong reference.
REFERENCE_SOLUTIONS = {
    "top_n_above_threshold": (
        "SELECT name FROM products WHERE price > ? ORDER BY price DESC LIMIT ?"
    ),
    "categories_above_count": (
        "SELECT category, COUNT(*) AS n FROM products GROUP BY category "
        "HAVING COUNT(*) > ? ORDER BY n DESC"
    ),
    "distinct_excluding": (
        "SELECT DISTINCT category FROM products WHERE category <> ? ORDER BY category"
    ),
    "price_between": (
        "SELECT name, price FROM products WHERE price BETWEEN ? AND ? ORDER BY price ASC"
    ),
    # Level 02 -- data cleaning.
    "null_emails": (
        "SELECT order_id FROM orders WHERE customer_email IS NULL ORDER BY placed_at DESC"
    ),
    "lowercase_group_count": (
        # The secondary sort key is part of the reference, not decoration: two customers can share the
        # same order count, and without a tiebreaker SQLite is free to return them in either order, so
        # the "expected" value would be unfalsifiable.
        "SELECT LOWER(customer_email) AS customer_email, COUNT(*) AS n FROM orders "
        "GROUP BY LOWER(customer_email) HAVING COUNT(*) > 1 ORDER BY n DESC, customer_email ASC"
    ),
    "mean_max_excluding_negative": (
        "SELECT AVG(order_value) AS mean_value, MAX(order_value) AS max_value "
        "FROM orders WHERE order_value >= 0"
    ),
    "top_three_valid": (
        "SELECT order_id, order_value FROM orders WHERE order_value >= 0 "
        "ORDER BY order_value DESC LIMIT 3"
    ),
    # Level 03 -- baselines and reproducible splits.
    "majority_baseline_pct": (
        "SELECT ROUND(100.0 * SUM(CASE WHEN returned = 0 THEN 1 ELSE 0 END) / COUNT(*), 2) "
        "AS baseline_pct FROM orders"
    ),
    "reproducible_split": (
        "WITH numbered AS (SELECT returned, ROW_NUMBER() OVER (ORDER BY order_id) AS rn FROM orders) "
        "SELECT split, COUNT(*) AS n, ROUND(100.0 * SUM(returned) / COUNT(*), 2) AS return_pct FROM ("
        "SELECT CASE WHEN rn % 2 = 0 THEN 'train' ELSE 'test' END AS split, returned FROM numbered"
        ") GROUP BY split ORDER BY split ASC"
    ),
    "reason_shares": (
        "SELECT return_reason, COUNT(*) AS n, "
        "ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM returns), 2) AS share_pct "
        "FROM returns GROUP BY return_reason HAVING COUNT(*) > 1 "
        "ORDER BY n DESC, return_reason ASC"
    ),
    "pipeline_integrity_counts": (
        "SELECT COUNT(*) AS n_rows, SUM(CASE WHEN returned = 1 THEN 1 ELSE 0 END) AS n_returned, "
        "COUNT(DISTINCT order_id) AS distinct_orders FROM orders"
    ),
    # Level 04 -- framing, leakage and feature building.
    "join_customer_feature": (
        "SELECT o.order_id, o.category, c.lifetime_orders FROM orders o "
        "JOIN customers c ON c.customer_id = o.customer_id "
        "WHERE o.placed_at >= '2026-01-01' ORDER BY o.placed_at DESC, o.order_id ASC"
    ),
    "positive_category_stats": (
        "SELECT category, COUNT(*) AS n, AVG(order_value) AS avg_value FROM orders "
        "WHERE order_value > 0 GROUP BY category HAVING COUNT(*) >= 2 "
        "ORDER BY avg_value DESC, category ASC"
    ),
    "cutoff_training_subset": (
        "SELECT COUNT(*) AS n_before, "
        "COALESCE(SUM(CASE WHEN returned = 1 THEN 1 ELSE 0 END), 0) AS n_returned_before "
        "FROM orders WHERE placed_at <= ?"
    ),
    "value_band_case": (
        "SELECT order_id, category, CASE WHEN order_value < 50 THEN 'low' "
        "WHEN order_value < 150 THEN 'mid' ELSE 'high' END AS value_band "
        "FROM orders ORDER BY order_id ASC"
    ),
    # Level 05 -- confusion matrix and the precision/recall tradeoff.
    "confusion_cells": (
        "SELECT SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) AS tp, "
        "SUM(CASE WHEN predicted = 1 AND actual = 0 THEN 1 ELSE 0 END) AS fp, "
        "SUM(CASE WHEN predicted = 0 AND actual = 1 THEN 1 ELSE 0 END) AS fn, "
        "SUM(CASE WHEN predicted = 0 AND actual = 0 THEN 1 ELSE 0 END) AS tn FROM predictions"
    ),
    "precision_recall": (
        "SELECT ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END), 0), 2) AS precision_pct, "
        "ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END), 0), 2) AS recall_pct FROM predictions"
    ),
    "accuracy_beside_counts": (
        "SELECT SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END) AS n_flagged, "
        "SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END) AS n_actual_returns, "
        "ROUND(100.0 * SUM(CASE WHEN predicted = actual THEN 1 ELSE 0 END) / COUNT(*), 2) "
        "AS accuracy_pct FROM predictions"
    ),
    "threshold_sweep": (
        "WITH t(threshold) AS (SELECT 0.2 UNION ALL SELECT 0.5 UNION ALL SELECT 0.8) "
        "SELECT t.threshold AS threshold, "
        "SUM(CASE WHEN s.probability >= t.threshold THEN 1 ELSE 0 END) AS n_flagged, "
        "COALESCE(ROUND(100.0 * SUM(CASE WHEN s.probability >= t.threshold AND s.actual = 1 "
        "THEN 1 ELSE 0 END) / NULLIF(SUM(CASE WHEN s.probability >= t.threshold THEN 1 ELSE 0 END), 0), 2), 0) "
        "AS precision_pct, "
        "COALESCE(ROUND(100.0 * SUM(CASE WHEN s.probability >= t.threshold AND s.actual = 1 "
        "THEN 1 ELSE 0 END) / NULLIF(SUM(CASE WHEN s.actual = 1 THEN 1 ELSE 0 END), 0), 2), 0) "
        "AS recall_pct FROM t CROSS JOIN scores s GROUP BY t.threshold ORDER BY t.threshold ASC"
    ),
    # Level 06 -- fairness audit and data-quality measurement.
    "approval_rate_by_group": (
        "SELECT region, COUNT(*) AS n, ROUND(100.0 * SUM(approved) / COUNT(*), 2) AS approval_pct "
        "FROM approvals GROUP BY region ORDER BY approval_pct DESC, region ASC"
    ),
    "missing_rate_by_group": (
        "SELECT group_name, COUNT(*) AS n_rows, "
        "ROUND(100.0 * SUM(CASE WHEN age IS NULL THEN 1 ELSE 0 END) / COUNT(*), 2) AS missing_age_pct "
        "FROM customers GROUP BY group_name ORDER BY group_name ASC"
    ),
    "interaction_return_rate": (
        "SELECT category, season, COUNT(*) AS n, "
        "ROUND(100.0 * SUM(returned) / COUNT(*), 2) AS return_pct FROM orders "
        "GROUP BY category, season HAVING COUNT(*) >= 2 ORDER BY category ASC, season ASC"
    ),
    "constant_column_count": (
        "SELECT (CASE WHEN MIN(order_id) = MAX(order_id) THEN 1 ELSE 0 END) + "
        "(CASE WHEN MIN(region) = MAX(region) THEN 1 ELSE 0 END) + "
        "(CASE WHEN MIN(channel) = MAX(channel) THEN 1 ELSE 0 END) + "
        "(CASE WHEN MIN(currency) = MAX(currency) THEN 1 ELSE 0 END) AS constant_columns FROM orders"
    ),
    # Level 07 -- threshold, cost and calibration.
    "f1_at_threshold": (
        "WITH m AS (SELECT "
        "SUM(CASE WHEN probability >= 0.5 AND actual = 1 THEN 1 ELSE 0 END) AS tp, "
        "SUM(CASE WHEN probability >= 0.5 AND actual = 0 THEN 1 ELSE 0 END) AS fp, "
        "SUM(CASE WHEN probability < 0.5 AND actual = 1 THEN 1 ELSE 0 END) AS fn FROM scores) "
        "SELECT ROUND(100.0 * tp / NULLIF(tp + fp, 0), 2) AS precision_pct, "
        "ROUND(100.0 * tp / NULLIF(tp + fn, 0), 2) AS recall_pct, "
        "ROUND(2.0 * (1.0 * tp / NULLIF(tp + fp, 0)) * (1.0 * tp / NULLIF(tp + fn, 0)) / "
        "NULLIF((1.0 * tp / NULLIF(tp + fp, 0)) + (1.0 * tp / NULLIF(tp + fn, 0)), 0), 3) AS f1 FROM m"
    ),
    "false_alarm_cost": (
        "SELECT SUM(CASE WHEN probability >= 0.5 AND actual = 0 THEN 1 ELSE 0 END) AS fp, "
        "5 * SUM(CASE WHEN probability >= 0.5 AND actual = 0 THEN 1 ELSE 0 END) AS total_cost FROM scores"
    ),
    "confusion_at_threshold": (
        "SELECT SUM(CASE WHEN probability >= 0.5 AND actual = 1 THEN 1 ELSE 0 END) AS tp, "
        "SUM(CASE WHEN probability >= 0.5 AND actual = 0 THEN 1 ELSE 0 END) AS fp, "
        "SUM(CASE WHEN probability < 0.5 AND actual = 1 THEN 1 ELSE 0 END) AS fn, "
        "SUM(CASE WHEN probability < 0.5 AND actual = 0 THEN 1 ELSE 0 END) AS tn FROM scores"
    ),
    "calibration_bands": (
        "SELECT band, COUNT(*) AS n, ROUND(100.0 * SUM(actual) / COUNT(*), 2) AS actual_return_pct FROM ("
        "SELECT actual, CASE WHEN probability < 0.5 THEN 'low' WHEN probability < 0.8 THEN 'mid' "
        "ELSE 'high' END AS band FROM scores) GROUP BY band ORDER BY band ASC"
    ),
    "reliability_table": (
        "SELECT band, COUNT(*) AS n, ROUND(100.0 * AVG(probability), 2) AS mean_predicted_pct, "
        "ROUND(100.0 * SUM(actual) / COUNT(*), 2) AS actual_pct FROM ("
        "SELECT probability, actual, CASE WHEN probability < 0.5 THEN 'low' "
        "WHEN probability < 0.8 THEN 'mid' ELSE 'high' END AS band FROM scores) "
        "GROUP BY band ORDER BY band ASC"
    ),
    # Level 08 -- trees, combination effects and comparison from a log.
    "combination_return_rate": (
        "SELECT category, price_band, COUNT(*) AS n, "
        "ROUND(100.0 * SUM(returned) / COUNT(*), 2) AS return_pct FROM orders "
        "GROUP BY category, price_band HAVING COUNT(*) >= 3 "
        "ORDER BY return_pct DESC, category ASC, price_band ASC"
    ),
    "extreme_distances": (
        "SELECT order_id, shipping_distance_km FROM shipments "
        "ORDER BY shipping_distance_km DESC LIMIT 5"
    ),
    "column_defect_counts": (
        "SELECT SUM(CASE WHEN shipping_distance_km IS NULL THEN 1 ELSE 0 END) AS n_null, "
        "SUM(CASE WHEN shipping_distance_km < 0 THEN 1 ELSE 0 END) AS n_negative, "
        "SUM(CASE WHEN shipping_distance_km > 1000000 THEN 1 ELSE 0 END) AS n_huge FROM shipments"
    ),
    "best_run_per_model": (
        "SELECT model_name, COUNT(*) AS n_runs, MAX(recall) AS best_recall FROM runs "
        "GROUP BY model_name ORDER BY best_recall DESC, model_name ASC"
    ),
    # Level 09 -- overfitting, folds and spread.
    "fold_return_balance": (
        "WITH numbered AS (SELECT returned, "
        "((ROW_NUMBER() OVER (ORDER BY order_id) - 1) % 5) + 1 AS fold FROM orders) "
        "SELECT fold, COUNT(*) AS n, ROUND(100.0 * SUM(returned) / COUNT(*), 2) AS return_pct "
        "FROM numbered GROUP BY fold ORDER BY fold ASC"
    ),
    "fold_spread": (
        "SELECT ROUND(AVG(recall), 3) AS mean_recall, MIN(recall) AS min_recall, "
        "MAX(recall) AS max_recall FROM folds"
    ),
    "overfitting_gap": (
        "SELECT run_id, train_score, test_score, train_score - test_score AS gap FROM runs "
        "WHERE train_score - test_score > 0.15 ORDER BY gap DESC"
    ),
    "fold_class_counts": (
        "WITH numbered AS (SELECT returned, "
        "((ROW_NUMBER() OVER (ORDER BY order_id) - 1) % 5) + 1 AS fold FROM orders) "
        "SELECT fold, SUM(CASE WHEN returned = 1 THEN 1 ELSE 0 END) AS n_pos, "
        "SUM(CASE WHEN returned = 0 THEN 1 ELSE 0 END) AS n_neg "
        "FROM numbered GROUP BY fold ORDER BY fold ASC"
    ),
    # Level 10 -- deployment, skew and drift.
    "canary_precision": (
        "SELECT model_version, COUNT(*) AS n, "
        "ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END), 0), 2) AS precision_pct "
        "FROM served GROUP BY model_version ORDER BY model_version ASC"
    ),
    "feature_skew": (
        "SELECT feature_name, ROUND(train_mean, 2) AS train_mean, ROUND(serve_mean, 2) AS serve_mean, "
        "ROUND(ABS(train_mean - serve_mean), 2) AS drift FROM features "
        "ORDER BY drift DESC, feature_name ASC"
    ),
    "model_serving_window": (
        "SELECT model_version, COUNT(*) AS n, MIN(predicted_at) AS first_seen, "
        "MAX(predicted_at) AS last_seen FROM prediction_log "
        "GROUP BY model_version ORDER BY model_version ASC"
    ),
    "outcome_rate_trend": (
        "SELECT served_date, COUNT(*) AS n, ROUND(100.0 * SUM(actual) / COUNT(*), 2) AS return_pct "
        "FROM outcomes GROUP BY served_date ORDER BY served_date ASC"
    ),
    # Level 11 -- fairness audit: false-positive rate, per-group metrics, selection rate, disparity.
    "regional_fpr": (
        "SELECT region, SUM(CASE WHEN actual = 0 THEN 1 ELSE 0 END) AS n_negative, "
        "ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 0 THEN 1 ELSE 0 END) / "
        "SUM(CASE WHEN actual = 0 THEN 1 ELSE 0 END), 2) AS fpr_pct FROM audit "
        "GROUP BY region HAVING SUM(CASE WHEN actual = 0 THEN 1 ELSE 0 END) >= 3 "
        "ORDER BY fpr_pct DESC, region ASC"
    ),
    "regional_recall_precision": (
        "SELECT region, "
        "ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END), 0), 2) AS recall_pct, "
        "COALESCE(ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END), 0), 2), 0) AS precision_pct FROM audit "
        "GROUP BY region HAVING SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END) >= 2 ORDER BY region ASC"
    ),
    "disparity_from_best": (
        "WITH rates AS (SELECT region, 100.0 * SUM(returned) / COUNT(*) AS rate FROM orders "
        "GROUP BY region) "
        "SELECT region, ROUND((SELECT MAX(rate) FROM rates) - rate, 2) AS gap_from_best FROM rates "
        "ORDER BY gap_from_best DESC, region ASC"
    ),
    "regional_selection_rate": (
        "SELECT region, COUNT(*) AS n, "
        "ROUND(100.0 * SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END) / COUNT(*), 2) AS flagged_pct "
        "FROM audit GROUP BY region HAVING COUNT(*) >= 5 "
        "ORDER BY flagged_pct DESC, region ASC"
    ),
    # Level 12 -- drift, covariate shift and champion/challenger.
    "band_distribution": (
        "SELECT value_band, COUNT(*) AS n, ROUND(100.0 * COUNT(*) / (SELECT COUNT(*) FROM production), 2) "
        "AS pct FROM production GROUP BY value_band ORDER BY value_band ASC"
    ),
    "category_shift": (
        "SELECT category, train_pct, live_pct, "
        "CASE WHEN train_pct IS NULL THEN NULL ELSE ROUND(ABS(live_pct - train_pct), 2) END AS shift "
        "FROM mix WHERE train_pct IS NULL OR ABS(live_pct - train_pct) > 5 "
        "ORDER BY shift DESC, category ASC"
    ),
    "period_return_rate": (
        "SELECT period, COUNT(*) AS n, ROUND(100.0 * SUM(returned) / COUNT(*), 2) AS return_pct "
        "FROM periods GROUP BY period ORDER BY period ASC"
    ),
    "champion_challenger": (
        "SELECT model_version, COUNT(*) AS n, ROUND(100.0 * SUM(actual) / COUNT(*), 2) AS return_pct "
        "FROM served WHERE served_at >= '2026-04-01' "
        "GROUP BY model_version ORDER BY model_version ASC"
    ),
    # Level 13 -- pipeline tests and boosting.
    # The wrapper form is required: this SQLite returns no rows for `SELECT ... FROM features HAVING
    # COUNT(col) = 0` even when the column is entirely NULL, so the per-column aggregates are computed
    # first and filtered afterwards. Verified against the fixture, which has shipping_zone NULL throughout.
    "null_column_check": (
        "SELECT feature_name, n_rows FROM ("
        "SELECT 'order_value' AS feature_name, COUNT(*) AS n_rows, "
        "SUM(CASE WHEN order_value IS NULL THEN 1 ELSE 0 END) AS n_null FROM features "
        "UNION ALL "
        "SELECT 'shipping_zone', COUNT(*), SUM(CASE WHEN shipping_zone IS NULL THEN 1 ELSE 0 END) "
        "FROM features) WHERE n_null = n_rows ORDER BY feature_name ASC"
    ),
    "duplicate_key_check": (
        "SELECT order_id, COUNT(*) AS n FROM features GROUP BY order_id HAVING COUNT(*) > 1 "
        "ORDER BY n DESC, order_id ASC"
    ),
    "run_delta": (
        "SELECT model_version, score, ROUND(score - LAG(score) OVER (ORDER BY model_version ASC), 2) "
        "AS delta FROM runs ORDER BY model_version ASC"
    ),
    "latency_budget": (
        "SELECT endpoint, COUNT(*) AS n, SUM(CASE WHEN latency_ms > 100 THEN 1 ELSE 0 END) AS n_slow, "
        "ROUND(100.0 * SUM(CASE WHEN latency_ms > 100 THEN 1 ELSE 0 END) / COUNT(*), 2) AS slow_pct "
        "FROM requests GROUP BY endpoint ORDER BY slow_pct DESC, endpoint ASC"
    ),
    # Level 14 -- model card and datasheet evidence.
    "subgroup_card_metrics": (
        "SELECT region, COUNT(*) AS n, "
        "COALESCE(ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END), 0), 2), 0) AS recall_pct, "
        "COALESCE(ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 0 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 0 THEN 1 ELSE 0 END), 0), 2), 0) AS fpr_pct FROM evaluation "
        "GROUP BY region HAVING COUNT(*) >= 3 ORDER BY region ASC"
    ),
    "training_window": (
        "SELECT COUNT(*) AS n_rows, MIN(placed_at) AS first_order, MAX(placed_at) AS last_order "
        "FROM training_orders"
    ),
    "quarter_class_balance": (
        "SELECT substr(placed_at, 1, 7) AS quarter, COUNT(*) AS n, "
        "ROUND(100.0 * SUM(returned) / COUNT(*), 2) AS return_pct FROM training_orders "
        "GROUP BY substr(placed_at, 1, 7) ORDER BY quarter ASC"
    ),
    "provenance_inventory": (
        "SELECT source_system, COUNT(*) AS n_columns, "
        "SUM(CASE WHEN last_refresh IS NULL THEN 1 ELSE 0 END) AS n_missing, "
        "SUM(CASE WHEN last_refresh IS NOT NULL AND last_refresh < '2025-12-02' THEN 1 ELSE 0 END) "
        "AS n_stale FROM provenance GROUP BY source_system ORDER BY source_system ASC"
    ),
    # Level 15 -- feature selection, imbalance, calibration, learning curves.
    "importance_cumulative": (
        "WITH ranked AS (SELECT feature_name, importance, "
        "100.0 * importance / (SELECT SUM(importance) FROM importance) AS share_pct FROM importance) "
        "SELECT feature_name, importance, ROUND(share_pct, 2) AS share_pct, "
        "ROUND(SUM(share_pct) OVER (ORDER BY importance DESC, feature_name ASC "
        "ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW), 2) AS cumulative_pct "
        "FROM ranked ORDER BY importance DESC, feature_name ASC"
    ),
    "split_balance": (
        "SELECT split, COUNT(*) AS n, SUM(CASE WHEN returned = 1 THEN 1 ELSE 0 END) AS n_pos, "
        "SUM(CASE WHEN returned = 0 THEN 1 ELSE 0 END) AS n_neg, "
        "ROUND(100.0 * SUM(CASE WHEN returned = 1 THEN 1 ELSE 0 END) / COUNT(*), 2) AS pos_pct "
        "FROM dataset GROUP BY split ORDER BY split ASC"
    ),
    "learning_curve_average": (
        "SELECT train_size, ROUND(AVG(train_err), 4) AS train_err, ROUND(AVG(val_err), 4) AS val_err "
        "FROM curve GROUP BY train_size ORDER BY train_size ASC"
    ),
    # Level 16 -- peak load, latency percentiles, rollback and A/B latency.
    "peak_load_projection": (
        "SELECT hour_utc, n, n * ? AS projected_n FROM baseline ORDER BY hour_utc ASC"
    ),
    "latency_percentiles": (
        "WITH ranked AS (SELECT endpoint, latency_ms, "
        "ROW_NUMBER() OVER (PARTITION BY endpoint ORDER BY latency_ms ASC) AS rn, "
        "COUNT(*) OVER (PARTITION BY endpoint) AS n FROM requests) "
        "SELECT endpoint, MAX(n) AS n, "
        "MAX(CASE WHEN rn = CAST(CEIL(0.50 * n) AS INTEGER) THEN latency_ms END) AS p50_ms, "
        "MAX(CASE WHEN rn = CAST(CEIL(0.95 * n) AS INTEGER) THEN latency_ms END) AS p95_ms, "
        "MAX(CASE WHEN rn = CAST(CEIL(0.99 * n) AS INTEGER) THEN latency_ms END) AS p99_ms "
        "FROM ranked GROUP BY endpoint ORDER BY endpoint ASC"
    ),
    "rollback_breach_series": (
        "SELECT minute_utc, ROUND(100.0 * n_errors / n_requests, 2) AS error_pct, "
        "CASE WHEN 100.0 * n_errors / n_requests > ? THEN 1 ELSE 0 END AS breached "
        "FROM health ORDER BY minute_utc ASC"
    ),
    "ab_latency_comparison": (
        "WITH ranked AS (SELECT model_version, latency_ms, "
        "ROW_NUMBER() OVER (PARTITION BY model_version ORDER BY latency_ms ASC) AS rn, "
        "COUNT(*) OVER (PARTITION BY model_version) AS n FROM ab_test) "
        "SELECT model_version, MAX(n) AS n, ROUND(AVG(latency_ms), 1) AS avg_latency_ms, "
        "MAX(CASE WHEN rn = CAST(CEIL(0.95 * n) AS INTEGER) THEN latency_ms END) AS p95_latency_ms "
        "FROM ranked GROUP BY model_version ORDER BY model_version ASC"
    ),
    # Level 17 -- incident window, alert threshold replay, follow-ups and response times.
    "incident_fpr_windows": (
        "SELECT window_start, COUNT(*) AS n, "
        "COALESCE(ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 0 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 0 THEN 1 ELSE 0 END), 0), 2), 0) AS fpr_pct FROM served "
        "GROUP BY window_start ORDER BY window_start ASC"
    ),
    "alert_threshold_replay": (
        "SELECT minute_utc, ROUND(100.0 * n_fp / n_negative, 2) AS fpr_pct, "
        "CASE WHEN 100.0 * n_fp / n_negative > ? THEN 1 ELSE 0 END AS would_alert "
        "FROM history ORDER BY minute_utc ASC"
    ),
    "followup_actions": (
        "SELECT action_id, description, COALESCE(owner, 'UNASSIGNED') AS owner, "
        "CAST(julianday(due_date) - julianday('2026-04-01') AS INTEGER) AS days_remaining "
        "FROM actions ORDER BY days_remaining ASC"
    ),
    "incident_response_times": (
        "SELECT severity, COUNT(*) AS n, ROUND(AVG(detect_mins), 1) AS avg_detect_mins, "
        "ROUND(AVG(resolve_mins), 1) AS avg_resolve_mins FROM incidents "
        "GROUP BY severity ORDER BY severity ASC"
    ),
    # Level 18 -- sign-off controls: tolerance, evidence freshness, golden set, four eyes.
    "fairness_tolerance_check": (
        "SELECT region, COUNT(*) AS n, "
        "ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 0 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 0 THEN 1 ELSE 0 END), 0), 2) AS fpr_pct, "
        "CASE WHEN ABS(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 0 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 0 THEN 1 ELSE 0 END), 0) - "
        "(SELECT 100.0 * SUM(CASE WHEN predicted = 1 AND actual = 0 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 0 THEN 1 ELSE 0 END), 0) FROM eval)) <= ? "
        "THEN 1 ELSE 0 END AS within_tolerance FROM eval "
        "GROUP BY region ORDER BY fpr_pct DESC, region ASC"
    ),
    "evidence_freshness": (
        "SELECT evidence_type, COUNT(*) AS n_items, "
        "SUM(CASE WHEN as_of < ? THEN 1 ELSE 0 END) AS n_stale FROM evidence "
        "GROUP BY evidence_type ORDER BY n_stale DESC, evidence_type ASC"
    ),
    "golden_regression": (
        "SELECT model_version, COUNT(*) AS n, SUM(CASE WHEN produced <> expected THEN 1 ELSE 0 END) "
        "AS n_mismatch, ROUND(100.0 * SUM(CASE WHEN produced = expected THEN 1 ELSE 0 END) / COUNT(*), 2) "
        "AS match_pct FROM golden GROUP BY model_version "
        "ORDER BY match_pct ASC, model_version ASC"
    ),
    "four_eyes_review": (
        "SELECT change_id, author, reviewer, "
        "CASE WHEN reviewer IS NOT NULL AND reviewer <> author THEN 1 ELSE 0 END AS four_eyes "
        "FROM changes ORDER BY four_eyes ASC, change_id ASC"
    ),
    # Level 19 -- registry, guardrail coverage, ownership and review cadence.
    "registry_inventory": (
        "SELECT model_name, COALESCE(owner, 'UNASSIGNED') AS owner, MAX(deployed_at) AS latest_deployed, "
        "COUNT(*) AS n_versions, "
        "CAST(julianday('2026-04-01') - julianday(MAX(last_reviewed)) AS INTEGER) AS days_since_review "
        "FROM registry GROUP BY model_name ORDER BY days_since_review DESC, model_name ASC"
    ),
    "guardrail_coverage": (
        "SELECT project_id, "
        "(has_versioned_data + has_baseline + has_holdout + has_owner) AS passed_checks, "
        "CASE WHEN (has_versioned_data + has_baseline + has_holdout + has_owner) = 4 "
        "THEN 'ready' ELSE 'incomplete' END AS verdict FROM projects "
        "ORDER BY passed_checks ASC, project_id ASC"
    ),
    "ownership_coverage": (
        "SELECT COUNT(*) AS n_models, SUM(CASE WHEN owner IS NULL THEN 1 ELSE 0 END) AS n_unowned, "
        "ROUND(100.0 * SUM(CASE WHEN owner IS NULL THEN 1 ELSE 0 END) / COUNT(*), 2) AS pct_unowned "
        "FROM models"
    ),
    "review_cadence_check": (
        "SELECT model_name, "
        "CAST(julianday('2026-04-01') - julianday(last_reviewed) AS INTEGER) AS days_since_review, "
        "CASE WHEN CAST(julianday('2026-04-01') - julianday(last_reviewed) AS INTEGER) > ? "
        "THEN 1 ELSE 0 END AS overdue FROM reviews "
        "ORDER BY days_since_review DESC, model_name ASC"
    ),
    # Level 20 -- capstone: four-fifths ratio, baseline comparison, audit trail, disclosures.
    "selection_ratio_check": (
        "WITH rates AS (SELECT group_name, COUNT(*) AS n, "
        "100.0 * SUM(flagged) / COUNT(*) AS flag_pct FROM decisions GROUP BY group_name) "
        "SELECT group_name, n, ROUND(flag_pct, 2) AS flag_pct, "
        "ROUND(flag_pct / (SELECT MAX(flag_pct) FROM rates), 3) AS selection_ratio FROM rates "
        "ORDER BY selection_ratio ASC, group_name ASC"
    ),
    "baseline_comparison": (
        "SELECT model_name, "
        "COALESCE(ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END), 0), 2), 0) AS precision_pct, "
        "COALESCE(ROUND(100.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END), 0), 2), 0) AS recall_pct, "
        "COALESCE(ROUND(2.0 * (1.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END), 0)) * "
        "(1.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END), 0)) / "
        "NULLIF((1.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN predicted = 1 THEN 1 ELSE 0 END), 0)) + "
        "(1.0 * SUM(CASE WHEN predicted = 1 AND actual = 1 THEN 1 ELSE 0 END) / "
        "NULLIF(SUM(CASE WHEN actual = 1 THEN 1 ELSE 0 END), 0)), 0), 3), 0) AS f1 "
        "FROM evaluations GROUP BY model_name ORDER BY f1 DESC, model_name ASC"
    ),
    "audit_trail_summary": (
        "SELECT model_version, COUNT(*) AS n_changes, MAX(changed_at) AS last_change, "
        "COUNT(DISTINCT author) AS distinct_authors FROM audit_log "
        "GROUP BY model_version ORDER BY n_changes DESC, model_version ASC"
    ),
    "disclosure_compliance": (
        "SELECT model_name, "
        "(CASE WHEN intended_use IS NULL OR intended_use = '' THEN 1 ELSE 0 END) + "
        "(CASE WHEN limitations IS NULL OR limitations = '' THEN 1 ELSE 0 END) + "
        "(CASE WHEN owner IS NULL OR owner = '' THEN 1 ELSE 0 END) + "
        "(CASE WHEN eval_conditions IS NULL OR eval_conditions = '' THEN 1 ELSE 0 END) AS n_missing, "
        "CASE WHEN (CASE WHEN intended_use IS NULL OR intended_use = '' THEN 1 ELSE 0 END) + "
        "(CASE WHEN limitations IS NULL OR limitations = '' THEN 1 ELSE 0 END) + "
        "(CASE WHEN owner IS NULL OR owner = '' THEN 1 ELSE 0 END) + "
        "(CASE WHEN eval_conditions IS NULL OR eval_conditions = '' THEN 1 ELSE 0 END) = 0 "
        "THEN 1 ELSE 0 END AS compliant FROM disclosures "
        "ORDER BY n_missing DESC, model_name ASC"
    ),
    # Fullstack Developer -- level 02 SQL questions.
    "products_under_price": (
        "SELECT name, price FROM products WHERE price < 50 ORDER BY price ASC, name ASC"
    ),
    "catalogue_by_category": (
        "SELECT category, COUNT(*) AS n_products, ROUND(AVG(price), 2) AS avg_price FROM products "
        "GROUP BY category ORDER BY n_products DESC, category ASC"
    ),
    # Fullstack Developer -- level 04 SQL question.
    "products_with_category": (
        "SELECT p.name AS product_name, c.name AS category_name FROM products p "
        "LEFT JOIN categories c ON c.id = p.category_id ORDER BY p.name ASC"
    ),
    # Fullstack Developer -- level 06 stock-safety questions.
    #
    # Each is a single statement, because the browser runner does `db.prepare(code)` and `prepare`
    # compiles one statement. An earlier version of this level asked for `BEGIN; ...; ROLLBACK;`, which
    # would have passed here -- this verifier uses Python's sqlite3 and can run a script -- while failing
    # for every learner. The transaction concept now lives in the multiple-choice questions, where it
    # belongs, and the code questions ask for the guards a learner actually writes.
    "oversold_guard": (
        "SELECT stock AS stock_before, stock, CASE WHEN stock > 0 THEN 1 ELSE 0 END AS would_decrement "
        "FROM inventory WHERE product_id = 1"
    ),
    "atomic_decrement": (
        "UPDATE inventory SET stock = stock - 1 WHERE product_id = 1 AND stock > 0 "
        "RETURNING stock AS stock_after"
    ),
    "restock_increment": (
        "UPDATE inventory SET stock = stock + 2 WHERE product_id = 1 RETURNING stock AS stock_after"
    ),
    # Fullstack Developer -- level 08 N+1 and catalogue shape.
    "products_join_categories": (
        "SELECT p.name AS product_name, c.name AS category_name FROM products p "
        "JOIN categories c ON c.id = p.category_id ORDER BY p.name ASC"
    ),
    "category_product_counts": (
        "SELECT c.name AS category_name, COUNT(*) AS n FROM products p "
        "JOIN categories c ON c.id = p.category_id GROUP BY c.name "
        "ORDER BY n DESC, category_name ASC"
    ),
    # Fullstack Developer -- level 10 schema-inspection questions.
    "list_tables": (
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' "
        "ORDER BY name ASC"
    ),
    "foreign_key_report": (
        "SELECT t.name AS from_table, f.\"table\" AS to_table FROM pragma_table_list t "
        "JOIN pragma_foreign_key_list(t.name) f "
        "WHERE t.name NOT LIKE 'sqlite_%' "
        "ORDER BY from_table ASC, to_table ASC"
    ),
    # Fullstack Developer -- level 11 file-size and layer-shape questions.
    "file_sizes": (
        "SELECT path, layer, lines FROM source_files ORDER BY lines DESC, path ASC"
    ),
    "layer_shape": (
        "SELECT layer, COUNT(*) AS n_files, SUM(lines) AS total_lines FROM source_files "
        "GROUP BY layer ORDER BY total_lines DESC, layer ASC"
    ),
    # Fullstack Developer -- level 12 rate-limit forensics.
    "requests_per_caller": (
        "SELECT caller_id, COUNT(*) AS n FROM requests WHERE created_at >= ? "
        "GROUP BY caller_id ORDER BY n DESC, caller_id ASC"
    ),
    "caller_burst_span": (
        "SELECT caller_id, MIN(created_at) AS first_at, MAX(created_at) AS last_at, "
        "CAST((julianday(MAX(created_at)) - julianday(MIN(created_at))) * 86400 AS INTEGER) "
        "AS span_seconds FROM requests GROUP BY caller_id "
        "ORDER BY span_seconds ASC, caller_id ASC"
    ),
    # Fullstack Developer -- level 13 response-shape review.
    "column_exposure": (
        "SELECT column_name, CASE WHEN sensitive = 1 THEN 'no' ELSE 'yes' END AS exposable "
        "FROM user_columns ORDER BY sensitive DESC, column_name ASC"
    ),
    # Fullstack Developer -- level 14 replica lag report.
    "latest_replica_lag": (
        "SELECT l.replica_name, l.lag_seconds AS latest_lag FROM replica_lag l "
        "JOIN (SELECT replica_name, MAX(sampled_at) AS m FROM replica_lag GROUP BY replica_name) x "
        "ON x.replica_name = l.replica_name AND x.m = l.sampled_at "
        "ORDER BY latest_lag DESC, l.replica_name ASC"
    ),
    # Fullstack Developer -- level 15 slow-query log ranking.
    "slow_query_totals": (
        "SELECT pattern, COUNT(*) AS n, SUM(duration_ms) AS total_ms FROM slow_queries "
        "GROUP BY pattern ORDER BY total_ms DESC, pattern ASC"
    ),
    # Fullstack Developer -- level 16 unhandled webhook event types.
    "unhandled_event_types": (
        "SELECT DISTINCT r.event_type FROM received_events r "
        "WHERE NOT EXISTS (SELECT 1 FROM handled_types h WHERE h.event_type = r.event_type) "
        "ORDER BY r.event_type ASC"
    ),
    # Fullstack Developer -- level 17 connection-demand planning.
    "connection_demand": (
        "SELECT name, instances * pool_per_instance AS demand, "
        "CASE WHEN instances * pool_per_instance > server_connection_limit THEN 'yes' ELSE 'no' END "
        "AS exceeds FROM environments ORDER BY demand DESC, name ASC"
    ),
    # Fullstack Developer -- level 18 dependency-share trace analysis.
    "dependency_share": (
        "SELECT request_id, SUM(duration_ms) AS total_ms, "
        "SUM(CASE WHEN is_non_critical = 1 THEN duration_ms ELSE 0 END) AS slow_dep_ms, "
        "CAST(100 * SUM(CASE WHEN is_non_critical = 1 THEN duration_ms ELSE 0 END) "
        "/ SUM(duration_ms) AS INTEGER) AS dep_share_pct "
        "FROM spans GROUP BY request_id "
        "HAVING dep_share_pct >= 70 "
        "ORDER BY dep_share_pct DESC, request_id ASC"
    ),
    "span_totals_by_service": (
        "SELECT service, COUNT(*) AS n, SUM(duration_ms) AS total_ms, "
        "CAST(AVG(duration_ms) AS INTEGER) AS mean_ms FROM spans "
        "GROUP BY service ORDER BY total_ms DESC, service ASC"
    ),
    # Fullstack Developer -- level 19 API version usage before a sunset.
    "version_usage": (
        "SELECT version, COUNT(DISTINCT caller_id) AS callers, COUNT(*) AS requests "
        "FROM api_calls WHERE called_at >= ? "
        "GROUP BY version ORDER BY requests DESC, version ASC"
    ),
    # Fullstack Developer -- level 20 irreversible plan steps.
    "irreversible_steps": (
        "SELECT step_no, description FROM plan_steps WHERE reversible = 0 ORDER BY step_no ASC"
    ),
    # Fullstack Developer -- level 20 boundary-crossing imports.
    "boundary_crossings": (
        "SELECT DISTINCT i.from_module, i.to_module FROM imports i "
        "JOIN boundaries f ON f.module = i.from_module "
        "JOIN boundaries t ON t.module = i.to_module "
        "WHERE f.domain <> t.domain AND t.is_shared = 0 "
        "ORDER BY i.from_module ASC, i.to_module ASC"
    ),
}

# Prompt phrase -> reference key. Checked in order, so a more specific phrase must come first.
#
# Order matters and was wrong once: the level-02 NULL question says "lower case" in its *test name*, and
# the group-count question says "lower case" in its prompt, so the group-count matcher claimed both. The
# symptom was a mismatch reporting the wrong reference query, which is a confusing failure for a matcher
# bug. Phrase-specific matchers therefore precede generic ones.
PROMPT_MATCHERS: tuple[tuple[str, str], ...] = (
    ("steps of a migration plan that cannot be reversed", "irreversible_steps"),
    ("outbound dependencies that cross a boundary", "boundary_crossings"),
    ("each API version's caller count and total requests since a cutoff", "version_usage"),
    ("summarising the trace spans by service", "span_totals_by_service"),
    ("total time was spent almost entirely in the recommendations service", "dependency_share"),
    ("how many database connections the application fleet will demand", "connection_demand"),
    ("webhook event types that have been received but have no handler", "unhandled_event_types"),
    ("total time spent per query pattern from a slow-query log", "slow_query_totals"),
    ("each read replica's latest lag sample", "latest_replica_lag"),
    ("together with a flag marking the ones the account-menu endpoint", "column_exposure"),
    ("how many requests each caller made in a window", "requests_per_caller"),
    ("the first and last request in its window", "caller_burst_span"),
    ("reporting each file's size in lines", "file_sizes"),
    ("reporting the total lines and file count per layer", "layer_shape"),
    ("listing the tables in the current database", "list_tables"),
    ("listing every foreign-key relationship", "foreign_key_report"),
    ("replaces the page's 501 round trips", "products_join_categories"),
    ("reporting each category's product count", "category_product_counts"),
    ("reporting the outcome of an oversold attempt", "oversold_guard"),
    ("Write the guarded decrement checkout needs as one statement", "atomic_decrement"),
    ("restocking a returned item", "restock_increment"),
    ("its category's `name`, keeping products whose category is NULL", "products_with_category"),
    ("priced strictly under 50, cheapest first", "products_under_price"),
    ("summarising the catalogue by category", "catalogue_by_category"),
    ("measuring disparities for a high-stakes review", "selection_ratio_check"),
    ("comparing the deployed model against a documented baseline", "baseline_comparison"),
    ("checking the audit trail for a deployed model version", "audit_trail_summary"),
    ("carries the disclosures the standard requires", "disclosure_compliance"),
    ("a model registry inventory", "registry_inventory"),
    ("checking the mandatory guardrails across projects", "guardrail_coverage"),
    ("summarising ownership coverage across the registry", "ownership_coverage"),
    ("comparing model review ages against a policy", "review_cadence_check"),
    ("checking the fairness tolerance", "fairness_tolerance_check"),
    ("checking the sign-off evidence inventory", "evidence_freshness"),
    ("the golden-dataset regression check", "golden_regression"),
    ("supporting the four-eyes access review", "four_eyes_review"),
    ("locating the forty-minute spike", "incident_fpr_windows"),
    ("which minutes would have breached a candidate alert threshold", "alert_threshold_replay"),
    ("checking the postmortem's follow-up actions", "followup_actions"),
    ("summarising incident response", "incident_response_times"),
    ("projecting peak-day load", "peak_load_projection"),
    ("measuring the latency tail", "latency_percentiles"),
    ("auditing an automatic rollback rule", "rollback_breach_series"),
    ("comparing the two candidate models on the A/B test", "ab_latency_comparison"),
    ("ranking features by importance and computing each one's share", "importance_cumulative"),
    ("quantifying the class imbalance in a split", "split_balance"),
    # Both level 07 and level 15 ask for a "reliability table" over the same bands, and both open with
    # "one row per probability band with `band`". Matching on that shared wording sent level 07 to level
    # 15's reference AND took level 07's own tests down, because its matcher never ran -- so the
    # discriminator has to be the sentence each prompt does not share. Level 15's reason clause and level
    # 07's reason clause are those sentences. check_matchers() now refuses a fragment that is a substring
    # of another, which is the shape that made this silent.
    ("so an overconfident model can be seen", "reliability_table"),
    ("so predicted probabilities can be compared against observed rates", "calibration_bands"),
    ("summarising a learning curve", "learning_curve_average"),
    ("the subgroup performance table that belongs in the model card", "subgroup_card_metrics"),
    ("the training data's coverage as a single row", "training_window"),
    ("so the card can show how the class balance moved", "quarter_class_balance"),
    ("the provenance inventory the datasheet needs", "provenance_inventory"),
    ("whether any feature column in the assembled training set is entirely NULL", "null_column_check"),
    ("detects duplicate rows in the assembled feature table", "duplicate_key_check"),
    ("regression between runs becomes visible", "run_delta"),
    ("checking a latency budget", "latency_budget"),
    ("a shift in the shape can be seen", "band_distribution"),
    ("a category absent from training entirely should appear", "category_shift"),
    ("the test for concept drift", "period_return_rate"),
    ("a champion and a challenger can be compared", "champion_challenger"),
    ("the false-positive rate as a percentage", "regional_fpr"),
    ("which fairness criterion is being met", "regional_recall_precision"),
    ("the size of the disparity the audit has to judge", "disparity_from_best"),
    ("so selection rates can be compared across groups", "regional_selection_rate"),
    ("judged against the incumbent on live traffic", "canary_precision"),
    ("This is the training-serving skew check", "feature_skew"),
    ("a version that stopped serving can be spotted", "model_serving_window"),
    ("a change in the outcome rate over time", "outcome_rate_trend"),
    ("class balance of each fold can be inspected", "fold_return_balance"),
    ("gap between minimum and maximum", "fold_spread"),
    ("This is the overfitting detector", "overfitting_gap"),
    ("why an unstratified split is a problem", "fold_class_counts"),
    ("combination effect the level describes", "combination_return_rate"),
    ("most extreme `shipping_distance_km`", "extreme_distances"),
    ("how many rows have a NULL `shipping_distance_km`", "column_defect_counts"),
    ("can be compared from the experiment log", "best_run_per_model"),
    ("`f1` to three decimals", "f1_at_threshold"),
    ("each false alarm costs 5", "false_alarm_cost"),
    ("giving the full confusion matrix at one operating point", "confusion_at_threshold"),
    ("comparing approval rates across `region`", "approval_rate_by_group"),
    ("how often `age` is missing can be seen", "missing_rate_by_group"),
    ("this is the interaction feature the level describes", "interaction_return_rate"),
    ("identical for every row", "constant_column_count"),
    ("counting each cell of the confusion matrix", "confusion_cells"),
    # Must precede the plain precision/recall matcher: the sweep question's prompt also names
    # `precision_pct` and `recall_pct`, so a generic pattern claimed it and ran the wrong reference
    # against the wrong fixture -- surfacing as "no such table: predictions" rather than as a mismatch.
    ("one row per threshold in the list", "threshold_sweep"),
    ("so the 88%-accuracy trap can be seen", "accuracy_beside_counts"),
    ("precision_pct` and `recall_pct`", "precision_recall"),
    ("joins each order to its customer", "join_customer_feature"),
    ("for categories with at least two such orders", "positive_category_stats"),
    ("cutoff timestamp given as the first parameter", "cutoff_training_subset"),
    ("value_band", "value_band_case"),
    ("majority-class baseline accuracy", "majority_baseline_pct"),
    ("deterministically into a training set", "reproducible_split"),
    ("single most common reason", "reason_shares"),
    ("so a pipeline can assert the file loaded completely", "pipeline_integrity_counts"),
    ("is **null**", "null_emails"),
    ("is null", "null_emails"),
    ("mean and the maximum", "mean_max_excluding_negative"),
    ("three largest", "top_three_valid"),
    ("together with that customer's order count", "lowercase_group_count"),
    ("distinct `category`", "distinct_excluding"),
    ("together with `n`", "categories_above_count"),
    ("within the inclusive range", "price_between"),
    ("within an inclusive range", "price_between"),
    ("most expensive first", "top_n_above_threshold"),
    ("priced strictly above", "top_n_above_threshold"),
)


def match_reference(prompt: str) -> str | None:
    lowered = prompt.lower()
    for needle, key in PROMPT_MATCHERS:
        if needle.lower() in lowered:
            return key
    return None


def run(setup_sql: str, query: str, args: list) -> list[dict]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(setup_sql)
        cursor = connection.execute(query, args)
        columns = [d[0] for d in cursor.description]
        return [dict(zip(columns, row)) for row in cursor.fetchall()]
    finally:
        connection.close()


def normalise(rows: list) -> list:
    """Compare as ordered lists of dicts with values coerced to a common numeric type.

    SQLite returns 40 for a REAL column where JSON holds 40, and languages differ on int/float
    equality. Coercing numbers to float removes a whole class of false mismatch without hiding a real
    one: an off-by-one threshold still differs.
    """
    out = []
    for row in rows:
        if isinstance(row, dict):
            out.append({k: (float(v) if isinstance(v, (int, float)) else v) for k, v in row.items()})
        else:
            out.append(row)
    return out


def check_matchers() -> list[str]:
    """Report prompt fragments claimed by more than one matcher, and duplicate reference keys.

    This guard exists because the failure it detects is invisible. A matcher is a substring test, so a
    generic fragment like "a reliability table" matches every later question that uses the same phrase —
    and the first matcher in file order silently wins. The question then runs a reference written for a
    different fixture, which either errors with a missing table or, worse, returns plausible values for
    the wrong question.

    It has happened three times: a threshold sweep claimed the plain precision/recall reference, a
    category-shift question claimed the reliability reference, and level 15's reliability table claimed
    level 07's — which took level 07's tests down at the same time, since its own matcher never ran.

    Returns a list of problems; empty means the table is sound.
    """
    problems: list[str] = []
    seen: dict[str, str] = {}
    references = set(REFERENCE_SOLUTIONS)

    for fragment, key in PROMPT_MATCHERS:
        if key not in references:
            problems.append(f"matcher {fragment!r} points at unregistered reference {key!r}")
        if fragment in seen:
            problems.append(f"fragment {fragment!r} is registered twice, most recently for {key!r}")
        seen[fragment] = key

    # A fragment that is a substring of another fragment will shadow it depending on order.
    fragments = [fragment for fragment, _ in PROMPT_MATCHERS]
    for i, outer in enumerate(fragments):
        for j, inner in enumerate(fragments):
            if i != j and inner in outer and PROMPT_MATCHERS[i][1] != PROMPT_MATCHERS[j][1]:
                problems.append(
                    f"{inner!r} is a substring of {outer!r} but they map to different references "
                    f"({PROMPT_MATCHERS[j][1]!r} and {PROMPT_MATCHERS[i][1]!r})"
                )

    return problems


def main() -> int:
    path = Path(sys.argv[1])
    data = json.loads(path.read_text(encoding="utf-8"))

    total = passed = 0
    failures: list[str] = []

    for index, question in enumerate(data.get("questions") or [], 1):
        spec = question.get("code")
        if not spec or spec.get("language") != "sql":
            continue

        setup = spec.get("setupSql") or ""
        key = match_reference(question.get("prompt", ""))
        if key is None:
            failures.append(f"q{index}: no reference solution mapped to this prompt")
            continue
        if key not in REFERENCE_SOLUTIONS:
            failures.append(f"q{index}: matched '{key}' but that reference is not defined")
            continue

        query = REFERENCE_SOLUTIONS[key]
        print(f"  q{index} [{key}]")
        for test in spec.get("tests") or []:
            total += 1
            label = f"q{index}/{test.get('id')}{' (hidden)' if test.get('hidden') else ''}"
            try:
                actual = run(setup, query, list(test.get("args") or []))
            except Exception as exc:  # noqa: BLE001
                failures.append(f"{label}: the reference query itself failed: {exc}")
                print(f"    {label}: QUERY ERROR {exc}")
                continue
            expected = test.get("expected")
            if normalise(actual) == normalise(expected):
                passed += 1
                print(f"    {label}: ok  {actual}")
            else:
                failures.append(f"{label}: expected {expected} but the reference query returns {actual}")
                print(f"    {label}: MISMATCH")
                print(f"       expected: {expected}")
                print(f"       actual  : {actual}")

    print()
    print(f"  {passed}/{total} test expectation(s) confirmed by running the reference query")
    if failures:
        print(f"  {len(failures)} FAILURE(S):")
        for item in failures:
            print(f"    - {item}")
        return 1
    print("  every expected value is reproducible")
    return 0


if __name__ == "__main__":
    sys.exit(main())
