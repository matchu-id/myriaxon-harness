"""One-off: give the bare-code options in level 02 Indonesian counterparts.

Thirteen options in this level are nothing but a code snippet, so a faithful translation is byte-identical
to the English and `guard_localisation.py` reports them as `unusable: ... (identical to English)`. Each is
differentiated with a short Indonesian `--` comment inside the code span: the SQL stays valid and visible,
and the option is no longer a copy of the English.

    python tools/fix02.py
"""

from pathlib import Path

WORK = Path(__file__).resolve().parent.parent / "localisation" / "work" / "02.id.tsv"

BT = chr(0x60)
DQ = chr(0x22)
SQ = chr(0x27)

FIXES = {
    "questions.2.options.0.text":
        BT + "SELECT * FROM products IF price < 50" + BT + " -- IF bukan klausa SQL yang sah",
    "questions.2.options.1.text":
        BT + "SELECT price < 50 FROM products" + BT + " -- memilih hasil perbandingannya, bukan kolomnya",
    "questions.2.options.2.text":
        BT + "SELECT * FROM products WHERE price < 50" + BT + " -- WHERE menyaring baris",
    "questions.2.options.3.text":
        BT + "SELECT * FROM products HAVING price < 50" + BT + " -- HAVING menyaring grup, bukan baris",
    "questions.4.options.0.text":
        BT + "CREATE TABLE products (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, price REAL)" + BT
        + " -- id diisi database sendiri",
    "questions.5.options.1.text":
        BT + "UPDATE products SET price = 9.99 WHERE name = " + SQ + "Mug" + SQ + BT
        + " -- satu baris saja yang berubah",
    "questions.8.options.0.text":
        BT + "products.map((product) => product.name)" + BT + " -- satu elemen per produk",
    "questions.9.options.0.text":
        BT + "products.map((product) => product.inStock)" + BT + " -- hasilnya boolean, bukan produknya",
    "questions.9.options.1.text":
        BT + "products.filter((product) => product.inStock)" + BT + " -- produknya yang dikembalikan",
    "questions.13.options.1.text":
        BT + "products.find((product) => product.name === " + DQ + "Mug" + DQ + ")" + BT
        + " -- berhenti di kecocokan pertama",
    "questions.13.options.3.text":
        BT + "products.map((product) => product.name === " + DQ + "Mug" + DQ + ")" + BT
        + " -- menghasilkan boolean, bukan produknya",
}


def main() -> int:
    lines = WORK.read_text(encoding="utf-8").split("\n")
    patched: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if "\t" in line:
            key = line.split("\t", 1)[0]
            if key in FIXES:
                line = key + "\t" + FIXES[key]
                seen.add(key)
        patched.append(line)
    missing = sorted(set(FIXES) - seen)
    if missing:
        print(f"  REFUSING: {len(missing)} key(s) not found in the work file:")
        for key in missing:
            print(f"    {key}")
        return 1
    WORK.write_text("\n".join(patched), encoding="utf-8", newline="\n")
    print(f"  patched {len(seen)} value(s) in {WORK.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
