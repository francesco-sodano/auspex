import argparse
from bisect import bisect_left
from collections import defaultdict
from datetime import date
import json
import math
import os
import random
from statistics import fmean, median

from mssql_python import connect


HORIZONS = (63, 126)


def _forward_return(series, as_of, horizon):
    dates = [point[0] for point in series]
    index = bisect_left(dates, as_of)
    if index >= len(series) or series[index][0] != as_of:
        return None
    forward_index = index + horizon
    if forward_index >= len(series):
        return None
    entry = series[index][1]
    exit_value = series[forward_index][1]
    if entry <= 0 or exit_value <= 0:
        return None
    return exit_value / entry - 1.0


def _bootstrap_interval(values, *, iterations=2000, seed=42):
    if len(values) < 2:
        return None
    rng = random.Random(seed)
    estimates = sorted(
        fmean(rng.choice(values) for _ in values)
        for _ in range(iterations)
    )
    lower = estimates[math.floor(0.025 * (iterations - 1))]
    upper = estimates[math.ceil(0.975 * (iterations - 1))]
    return [lower, upper]


def evaluate(score_rows, price_rows, *, horizons=HORIZONS, bootstrap_iterations=2000):
    prices = defaultdict(list)
    for row in price_rows:
        prices[str(row["ticker"]).upper()].append(
            (date.fromisoformat(str(row["event_date"])), float(row["close"]))
        )
    for ticker in prices:
        prices[ticker].sort()

    cohorts = defaultdict(list)
    for row in score_rows:
        if row.get("opportunity_score_raw") is None or row.get("beta_252d") is None:
            continue
        cohorts[(str(row["as_of"]), str(row["theme_id"]))].append(row)

    output = {}
    for horizon in horizons:
        cohort_spreads = []
        eligible_rows = 0
        top_rows = 0
        for (as_of_text, _theme_id), rows in sorted(cohorts.items()):
            as_of = date.fromisoformat(as_of_text)
            outcomes = []
            for row in rows:
                security_return = _forward_return(
                    prices[str(row["ticker"]).upper()], as_of, horizon
                )
                benchmark_return = _forward_return(
                    prices[str(row["benchmark_symbol"]).upper()], as_of, horizon
                )
                if security_return is None or benchmark_return is None:
                    continue
                adjusted = security_return - float(row["beta_252d"]) * benchmark_return
                outcomes.append((float(row["opportunity_score_raw"]), adjusted))
            if len(outcomes) < 5:
                continue
            outcomes.sort(key=lambda item: item[0], reverse=True)
            top_count = max(1, math.ceil(len(outcomes) * 0.20))
            top_return = fmean(outcome for _raw, outcome in outcomes[:top_count])
            cohort_median = median(outcome for _raw, outcome in outcomes)
            cohort_spreads.append(top_return - cohort_median)
            eligible_rows += len(outcomes)
            top_rows += top_count
        interval = _bootstrap_interval(
            cohort_spreads,
            iterations=bootstrap_iterations,
            seed=42 + horizon,
        )
        output[str(horizon)] = {
            "status": "evaluated" if cohort_spreads else "insufficient_history",
            "cohort_dates": len(cohort_spreads),
            "eligible_security_rows": eligible_rows,
            "top_quintile_rows": top_rows,
            "mean_beta_adjusted_spread": (
                fmean(cohort_spreads) if cohort_spreads else None
            ),
            "bootstrap_95pct_interval": interval,
        }
    return output


def load_warehouse_rows(server, database):
    connection = connect(
        f"Server={server};Database={database};"
        "Authentication=ActiveDirectoryDefault;Encrypt=yes;TrustServerCertificate=no;"
    )
    connection.autocommit = True
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT s.as_of, s.theme_id, s.security_sk, d.ticker,
                   s.opportunity_score_raw, f.beta_252d, t.benchmark_symbol
            FROM dbo.fact_theme_opportunity_score s
            JOIN dbo.dim_security d ON d.security_sk = s.security_sk
            JOIN dbo.dim_theme t ON t.theme_id = s.theme_id
            LEFT JOIN dbo.security_daily_features f
              ON f.security_sk = s.security_sk AND f.date_sk = s.date_sk
            WHERE s.model_version = 'opportunity_v1'
              AND s.weight_version = 'balanced_v1'
              AND s.opportunity_score_raw IS NOT NULL
            """
        )
        score_rows = [
            {
                "as_of": str(row[0]),
                "theme_id": row[1],
                "security_sk": int(row[2]),
                "ticker": row[3],
                "opportunity_score_raw": row[4],
                "beta_252d": row[5],
                "benchmark_symbol": row[6],
            }
            for row in cursor.fetchall()
        ]
        cursor.execute(
            """
            SELECT d.ticker, m.event_date, m.[close]
            FROM dbo.fact_market_daily m
            JOIN dbo.dim_security d ON d.security_sk = m.security_sk
            WHERE m.[close] > 0
            ORDER BY d.ticker, m.event_date
            """
        )
        price_rows = [
            {"ticker": row[0], "event_date": str(row[1]), "close": row[2]}
            for row in cursor.fetchall()
        ]
        return score_rows, price_rows
    finally:
        connection.close()


def main():
    parser = argparse.ArgumentParser(
        description="Backtest top-quintile Opportunity Score raw composites"
    )
    parser.add_argument(
        "--server", default=os.environ.get("FABRIC_WAREHOUSE_SERVER", "")
    )
    parser.add_argument(
        "--database", default=os.environ.get("FABRIC_WAREHOUSE_DATABASE", "auspex_gold")
    )
    parser.add_argument("--bootstrap-iterations", type=int, default=2000)
    args = parser.parse_args()
    if not args.server:
        parser.error("--server or FABRIC_WAREHOUSE_SERVER is required")
    score_rows, price_rows = load_warehouse_rows(args.server, args.database)
    result = {
        "model_version": "opportunity_v1",
        "weight_version": "balanced_v1",
        "method": "top quintile raw composite minus cohort median, beta adjusted",
        "score_rows": len(score_rows),
        "results": evaluate(
            score_rows,
            price_rows,
            bootstrap_iterations=args.bootstrap_iterations,
        ),
    }
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
