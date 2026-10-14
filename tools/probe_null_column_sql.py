#!/usr/bin/env python3
"""Find a form of the entirely-NULL-column check that this SQLite actually answers.

Four attempts have returned nothing, and rather than keep reasoning about SQLite's aggregate edge cases
this walks up from the simplest possible query until one produces output. The fixture has four rows with
`num_past_orders` NULL in all of them, so `SUM(CASE WHEN num_past_orders IS NULL THEN 1 ELSE 0 END)`
should be 4 and `COUNT(*)` should be 4.
"""

from __future__ import annotations

import sqlite3

SETUP = (
    "CREATE TABLE features (order_id TEXT, order_value REAL, num_past_orders INTEGER);\n"
    "INSERT INTO features VALUES ('o1',120.0,3),('o2',80.0,NULL),('o3',45.0,NULL),('o4',200.0,7);"
)

STEPS = [
    ("1 rows", "SELECT COUNT(*) AS k FROM features"),
    ("2 null count in the column",
     "SELECT SUM(CASE WHEN num_past_orders IS NULL THEN 1 ELSE 0 END) AS k FROM features"),
    ("3 both in one aggregate row",
     "SELECT COUNT(*) AS n_rows, SUM(CASE WHEN num_past_orders IS NULL THEN 1 ELSE 0 END) AS n_null "
     "FROM features"),
    ("4 same with a GROUP BY on a constant",
     "SELECT COUNT(*) AS n_rows, SUM(CASE WHEN num_past_orders IS NULL THEN 1 ELSE 0 END) AS n_null "
     "FROM features GROUP BY 1=1"),
    ("5 the union of both columns' checks, filtered in a wrapper",
     "SELECT feature_name, n_rows FROM ("
     "SELECT 'num_past_orders' AS feature_name, COUNT(*) AS n_rows, "
     "SUM(CASE WHEN num_past_orders IS NULL THEN 1 ELSE 0 END) AS n_null FROM features "
     "UNION ALL "
     "SELECT 'order_value', COUNT(*), SUM(CASE WHEN order_value IS NULL THEN 1 ELSE 0 END) FROM features) "
     "WHERE n_null = n_rows ORDER BY feature_name ASC"),
]


def main() -> int:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    for name, query in STEPS:
        try:
            print(f"  {name}: {connection.execute(query).fetchall()}")
        except sqlite3.Error as error:
            print(f"  {name}: ERROR {error}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
