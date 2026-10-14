#!/usr/bin/env python3
"""Probe the SQLite pragma forms for listing a table's foreign keys.

The first attempt returned no rows, and this prints the candidates side by side so the working form is
chosen from output rather than from reading the documentation.
"""

from __future__ import annotations

import sqlite3

SETUP = (
    "PRAGMA foreign_keys = ON;\n"
    "CREATE TABLE categories (id INTEGER PRIMARY KEY, name TEXT);\n"
    "CREATE TABLE suppliers (id INTEGER PRIMARY KEY, name TEXT);\n"
    "CREATE TABLE products (id INTEGER PRIMARY KEY, name TEXT, "
    "category_id INTEGER REFERENCES categories(id), "
    "supplier_id INTEGER REFERENCES suppliers(id));\n"
    "CREATE TABLE orders (id INTEGER PRIMARY KEY, product_id INTEGER REFERENCES products(id));"
)

CANDIDATES = {
    "A pragma_table_list exists": "SELECT name FROM pragma_table_list ORDER BY name",
    "B foreign_key_list for products": "PRAGMA foreign_key_list(products)",
    "C pragma_foreign_key_list whole": "SELECT \"table\" FROM pragma_foreign_key_list",
    "D pragma_foreign_key_list with arg":
        "SELECT \"table\", \"from\" FROM pragma_foreign_key_list('products')",
    "E sqlite_master DDL for foreign keys":
        "SELECT name, sql FROM sqlite_master WHERE type = 'table' AND sql LIKE '%REFERENCES%'",
}


def main() -> int:
    connection = sqlite3.connect(":memory:")
    connection.executescript(SETUP)
    for name, query in CANDIDATES.items():
        try:
            rows = connection.execute(query).fetchall()
            print(f"  {name}: {rows[:6]}")
        except sqlite3.Error as error:
            print(f"  {name}: ERROR {error}")
    connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
