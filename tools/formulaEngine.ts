/**
 * A small Excel-style formula evaluator, for the non-technical tracks.
 *
 * Roles like financial-analyst, accountant and talent-acquisition-specialist do their work in spreadsheets,
 * not in Python. A SQL question would be a costume; SUMIF, XLOOKUP, IFERROR and absolute references are what
 * the job actually uses, so those are what the code questions should ask for.
 *
 * ## Why this is hand-written rather than a dependency
 *
 * The obvious route is HyperFormula or formulajs. Both were rejected for concrete reasons found while
 * preparing this:
 *
 * * The project's `node_modules` on the VPS is npm-shaped but incomplete — `npm install` fails with EACCES
 *   trying to create a nested `@esbuild-kit/core-utils/node_modules/@esbuild/android-arm`, a platform package
 *   for the wrong architecture. Repairing it means `npm ci`, which deletes 253 MB and reinstalls from the
 *   network while a VS Code server and several other agents are active on the same box. Not a risk worth
 *   taking to add one language.
 * * `sqlWorker.js` already establishes the pattern: a self-hosted engine, no CDN, loaded inside a dedicated
 *   Worker. A self-contained evaluator matches it and keeps the image smaller than bundling a full
 *   spreadsheet engine that implements 400 functions we will never ask for.
 *
 * The trade is explicit: this implements the functions the content uses, and **throws on any other function
 * name** rather than returning a wrong answer. A missing function is a loud authoring error; a subtly wrong
 * SUMIF is a learner marked incorrect for a correct formula.
 *
 * ## Scope
 *
 * Operators: `+ - * / ^ % & = <> < > <= >=`, parentheses, unary minus, range references (`A1:B4`), absolute
 * and relative forms (`$A$1`, `$A1`, `A$1`), strings, numbers, booleans, and `TRUE`/`FALSE`.
 *
 * Functions are grouped by what each track needs. Every one is covered by a test asserting a value computed
 * by hand, so an implementation mistake is caught by `test` rather than by a learner.
 */

export interface FormulaValue {
  readonly kind: "number" | "string" | "boolean" | "blank" | "error";
  readonly value: number | string | boolean | null;
}

const num = (value: number): FormulaValue => ({ kind: "number", value });
const str = (value: string): FormulaValue => ({ kind: "string", value });
const bool = (value: boolean): FormulaValue => ({ kind: "boolean", value });
const blank = (): FormulaValue => ({ kind: "blank", value: null });
const err = (code: string): FormulaValue => ({ kind: "error", value: code });

/** A rectangular block of cells, as `setupSheet` supplies them. */
export type Sheet = ReadonlyArray<ReadonlyArray<string | number | boolean | null>>;

export class FormulaError extends Error {
  constructor(public readonly code: string, message?: string) {
    super(message ?? code);
    this.name = "FormulaError";
  }
}

// ---------------------------------------------------------------- lexer

type Token =
  | { t: "num"; v: number }
  | { t: "str"; v: string }
  | { t: "ref"; v: string }
  | { t: "func"; v: string }
  | { t: "op"; v: string }
  | { t: "lparen" }
  | { t: "rparen" }
  | { t: "comma" }
  | { t: "colon" };

// Order matters: `<=` must be tried before `<`, `<>` before `<=`.
const OPERATORS = ["<>", "<=", ">=", "+", "-", "*", "/", "^", "%", "&", "=", "<", ">"];

function tokenize(source: string): Token[] {
  const tokens: Token[] = [];
  let i = 0;
  while (i < source.length) {
    const c = source[i];
    if (c === " " || c === "\t" || c === "\n" || c === "\r") {
      i += 1;
      continue;
    }
    if (c === "(") { tokens.push({ t: "lparen" }); i += 1; continue; }
    if (c === ")") { tokens.push({ t: "rparen" }); i += 1; continue; }
    if (c === ",") { tokens.push({ t: "comma" }); i += 1; continue; }
    if (c === ":") { tokens.push({ t: "colon" }); i += 1; continue; }

    if (c === '"') {
      let out = "";
      i += 1;
      while (i < source.length && source[i] !== '"') {
        // Excel escapes a literal quote by doubling it.
        if (source[i] === '"' && source[i + 1] === '"') { out += '"'; i += 2; continue; }
        out += source[i];
        i += 1;
      }
      if (i >= source.length) throw new FormulaError("#ERROR!", "unterminated string");
      i += 1;
      tokens.push({ t: "str", v: out });
      continue;
    }

    const two = source.slice(i, i + 2);
    if (two === "<>") { tokens.push({ t: "op", v: "<>" }); i += 2; continue; }

    if (/[0-9]/.test(c) || (c === "." && /[0-9]/.test(source[i + 1] ?? ""))) {
      let out = "";
      while (i < source.length && /[0-9.]/.test(source[i])) { out += source[i]; i += 1; }
      tokens.push({ t: "num", v: Number(out) });
      continue;
    }

    // A reference or a function name: letters, digits, `$`, `_`, `.`. A dangling `(` makes it a function.
    if (/[A-Za-z_$]/.test(c)) {
      let out = "";
      while (i < source.length && /[A-Za-z0-9_$.]/.test(source[i])) { out += source[i]; i += 1; }
      let lookahead = i;
      while (lookahead < source.length && source[lookahead] === " ") lookahead += 1;
      if (source[lookahead] === "(") {
        tokens.push({ t: "func", v: out.toUpperCase() });
      } else if (/^\$?[A-Za-z]{1,3}\$?[0-9]+$/.test(out)) {
        tokens.push({ t: "ref", v: out.toUpperCase() });
      } else if (out.toUpperCase() === "TRUE") {
        tokens.push({ t: "num", v: 1 }); // handled as boolean by comparison, see coerce
        tokens[tokens.length - 1] = { t: "op", v: "TRUE" };
      } else if (out.toUpperCase() === "FALSE") {
        tokens.push({ t: "op", v: "FALSE" });
      } else {
        throw new FormulaError("#NAME?", `unknown name ${out}`);
      }
      continue;
    }

    const op = OPERATORS.find((candidate) => source.startsWith(candidate, i));
    if (op) { tokens.push({ t: "op", v: op }); i += op.length; continue; }

    throw new FormulaError("#ERROR!", `unexpected character ${c}`);
  }
  return tokens;
}

// ---------------------------------------------------------------- references

/** `$B$7` -> `{ col: 1, row: 6, absCol: true, absRow: true }`. */
function parseRef(ref: string): { col: number; row: number } {
  const match = /^\$?([A-Za-z]{1,3})\$?([0-9]+)$/.exec(ref);
  if (!match) throw new FormulaError("#REF!", `bad reference ${ref}`);
  const letters = match[1].toUpperCase();
  let col = 0;
  for (const ch of letters) col = col * 26 + (ch.charCodeAt(0) - 64);
  return { col: col - 1, row: Number(match[2]) - 1 };
}

function cellAt(sheet: Sheet, ref: string): FormulaValue {
  const { col, row } = parseRef(ref);
  const rowValues = sheet[row];
  if (!rowValues || col >= rowValues.length || col < 0 || row < 0) return blank();
  const raw = rowValues[col];
  if (raw === null || raw === undefined || raw === "") return blank();
  if (typeof raw === "number") return num(raw);
  if (typeof raw === "boolean") return bool(raw);
  return str(String(raw));
}

function rangeRefs(sheet: Sheet, from: string, to: string): string[] {
  const a = parseRef(from);
  const b = parseRef(to);
  const refs: string[] = [];
  for (let row = Math.min(a.row, b.row); row <= Math.max(a.row, b.row); row += 1) {
    for (let col = Math.min(a.col, b.col); col <= Math.max(a.col, b.col); col += 1) {
      let letters = "";
      let n = col + 1;
      while (n > 0) {
        const rem = (n - 1) % 26;
        letters = String.fromCharCode(65 + rem) + letters;
        n = Math.floor((n - 1) / 26);
      }
      refs.push(`${letters}${row + 1}`);
    }
  }
  return refs;
}

export function columnLetter(index: number): string {
  let letters = "";
  let n = index + 1;
  while (n > 0) {
    const rem = (n - 1) % 26;
    letters = String.fromCharCode(65 + rem) + letters;
    n = Math.floor((n - 1) / 26);
  }
  return letters;
}

// ---------------------------------------------------------------- values

function toNumber(value: FormulaValue): number {
  if (value.kind === "number") return value.value as number;
  if (value.kind === "boolean") return value.value ? 1 : 0;
  if (value.kind === "blank") return 0;
  if (value.kind === "string") {
    const parsed = Number(value.value);
    if (Number.isFinite(parsed)) return parsed;
    throw new FormulaError("#VALUE!", `not numeric: ${String(value.value)}`);
  }
  throw new FormulaError(String(value.value));
}

function toText(value: FormulaValue): string {
  if (value.kind === "string") return String(value.value);
  if (value.kind === "blank") return "";
  if (value.kind === "boolean") return value.value ? "TRUE" : "FALSE";
  return String(value.value);
}

function toBoolean(value: FormulaValue): boolean {
  if (value.kind === "boolean") return value.value as boolean;
  if (value.kind === "number") return (value.value as number) !== 0;
  if (value.kind === "blank") return false;
  if (value.kind === "string") {
    const upper = String(value.value).toUpperCase();
    if (upper === "TRUE") return true;
    if (upper === "FALSE") return false;
    throw new FormulaError("#VALUE!", `not boolean: ${String(value.value)}`);
  }
  throw new FormulaError(String(value.value));
}

/** A range flattened to the values a function sees, with blanks dropped — matching Excel's SUM/COUNT/AVERAGE. */
function rangeValues(sheet: Sheet, refs: string[]): FormulaValue[] {
  return refs.map((ref) => cellAt(sheet, ref));
}

export function normalize(value: FormulaValue): number | string | boolean | null {
  if (value.kind === "blank") return null;
  return value.value as number | string | boolean | null;
}

// ---------------------------------------------------------------- evaluator

interface Context {
  sheet: Sheet;
}

type Node =
  | { n: "value"; value: FormulaValue }
  | { n: "ref"; ref: string }
  | { n: "range"; refs: string[] }
  | { n: "call"; name: string; args: Node[] }
  | { n: "binary"; op: string; left: Node; right: Node }
  | { n: "unary"; op: string; operand: Node };

function parse(tokens: Token[], context: Context): Node {
  let pos = 0;

  const peek = (): Token | undefined => tokens[pos];
  const eat = (): Token => {
    const token = tokens[pos];
    if (!token) throw new FormulaError("#ERROR!", "unexpected end of formula");
    pos += 1;
    return token;
  };

  function parsePrimary(): Node {
    const token = eat();
    if (token.t === "num") return { n: "value", value: num(token.v) };
    if (token.t === "str") return { n: "value", value: str(token.v) };
    if (token.t === "op" && (token.v === "TRUE" || token.v === "FALSE")) {
      return { n: "value", value: bool(token.v === "TRUE") };
    }
    if (token.t === "ref") {
      if (peek()?.t === "colon") {
        eat();
        const end = eat();
        if (end.t !== "ref") throw new FormulaError("#REF!", "range must end in a reference");
        return { n: "range", refs: rangeRefs(context.sheet, token.v, end.v) };
      }
      return { n: "ref", ref: token.v };
    }
    if (token.t === "lparen") {
      const inner = parseExpr();
      if (eat().t !== "rparen") throw new FormulaError("#ERROR!", "missing )");
      return inner;
    }
    if (token.t === "func") {
      const args: Node[] = [];
      if (peek()?.t === "rparen") { eat(); return { n: "call", name: token.v, args }; }
      for (;;) {
        args.push(parseExpr());
        const next = eat();
        if (next.t === "rparen") break;
        if (next.t !== "comma") throw new FormulaError("#ERROR!", "expected , or )");
      }
      return { n: "call", name: token.v, args };
    }
    if (token.t === "op" && (token.v === "-" || token.v === "+")) {
      return { n: "unary", op: token.v, operand: parsePrimary() };
    }
    throw new FormulaError("#ERROR!", "unexpected token");
  }

  function parsePower(): Node {
    let left = parsePrimary();
    while (peek()?.t === "op" && peek()?.v === "^") {
      eat();
      left = { n: "binary", op: "^", left, right: parsePrimary() };
    }
    return left;
  }

  function parseTerm(): Node {
    let left = parsePower();
    while (peek()?.t === "op" && ["*", "/", "%"].includes((peek() as { v: string }).v)) {
      const op = (eat() as { v: string }).v;
      left = { n: "binary", op, left, right: parsePower() };
    }
    return left;
  }

  function parseConcat(): Node {
    let left = parseTerm();
    while (peek()?.t === "op" && peek()?.v === "&") {
      eat();
      left = { n: "binary", op: "&", left, right: parseTerm() };
    }
    return left;
  }

  function parseComparison(): Node {
    let left = parseConcat();
    while (peek()?.t === "op" && ["=", "<>", "<", ">", "<=", ">="].includes((peek() as { v: string }).v)) {
      const op = (eat() as { v: string }).v;
      left = { n: "binary", op, left, right: parseConcat() };
    }
    return left;
  }

  function parseExpr(): Node {
    let left = parseComparison();
    while (peek()?.t === "op" && (peek()?.v === "+" || peek()?.v === "-")) {
      const op = (eat() as { v: string }).v;
      left = { n: "binary", op, left, right: parseComparison() };
    }
    return left;
  }

  const root = parseExpr();
  if (pos !== tokens.length) throw new FormulaError("#ERROR!", "trailing tokens");
  return root;
}

function evaluate(node: Node, context: Context): FormulaValue {
  switch (node.n) {
    case "value":
      return node.value;
    case "ref":
      return cellAt(context.sheet, node.ref);
    case "range":
      throw new FormulaError("#VALUE!", "a range is only valid as a function argument");
    case "unary": {
      const operand = toNumber(evaluate(node.operand, context));
      return num(node.op === "-" ? -operand : operand);
    }
    case "binary": {
      const { op } = node;
      if (op === "&") {
        return str(toText(evaluate(node.left, context)) + toText(evaluate(node.right, context)));
      }
      const left = evaluate(node.left, context);
      const right = evaluate(node.right, context);
      if (["=", "<>", "<", ">", "<=", ">="].includes(op)) return compare(op, left, right);
      const a = toNumber(left);
      const b = toNumber(right);
      switch (op) {
        case "+": return num(a + b);
        case "-": return num(a - b);
        case "*": return num(a * b);
        case "/":
          if (b === 0) throw new FormulaError("#DIV/0!");
          return num(a / b);
        case "%": return num(a % b);
        case "^": return num(a ** b);
        default: throw new FormulaError("#ERROR!", `unknown operator ${op}`);
      }
    }
    case "call":
      return callFunction(node.name, node.args, context);
    default:
      throw new FormulaError("#ERROR!", "unhandled node");
  }
}

/** Excel compares text case-insensitively and treats blanks as 0 or "" depending on the other side. */
function compare(op: string, left: FormulaValue, right: FormulaValue): FormulaValue {
  let a: number | string | boolean;
  let b: number | string | boolean;
  if (left.kind === "string" || right.kind === "string") {
    a = toText(left).toUpperCase();
    b = toText(right).toUpperCase();
  } else {
    a = toNumber(left);
    b = toNumber(right);
  }
  switch (op) {
    case "=": return bool(a === b);
    case "<>": return bool(a !== b);
    case "<": return bool(a < b);
    case ">": return bool(a > b);
    case "<=": return bool(a <= b);
    case ">=": return bool(a >= b);
    default: throw new FormulaError("#ERROR!", `unknown comparison ${op}`);
  }
}

// ---------------------------------------------------------------- functions

/**
 * Criteria matching for `SUMIF`/`COUNTIF`/`AVERAGEIF`: `">100"`, `"<=5"`, `"north"`, `"<>north"`, `100`.
 * Text comparison is case-insensitive, as in Excel.
 */
function criteriaMatcher(criteria: FormulaValue): (value: FormulaValue) => boolean {
  const text = toText(criteria);
  const match = /^(<=|>=|<>|=|<|>)(.*)$/.exec(text);
  const op = match ? match[1] : "=";
  const raw = match ? match[2] : text;
  const numericTarget = raw.trim() !== "" ? Number(raw) : Number.NaN;

  return (value: FormulaValue): boolean => {
    const isNumericTarget = match !== null && Number.isFinite(numericTarget);
    if (isNumericTarget) {
      const candidate = value.kind === "blank" ? 0 : toNumber(value);
      switch (op) {
        case "=": return candidate === numericTarget;
        case "<>": return candidate !== numericTarget;
        case "<": return candidate < numericTarget;
        case ">": return candidate > numericTarget;
        case "<=": return candidate <= numericTarget;
        case ">=": return candidate >= numericTarget;
      }
    }
    const candidate = toText(value).toUpperCase();
    const target = raw.toUpperCase();
    switch (op) {
      case "=": return candidate === target;
      case "<>": return candidate !== target;
      // Excel's ordering for text in these operators is arbitrary but defined; refuse rather than guess.
      default:
        throw new FormulaError("#VALUE!", `text criteria only support = and <> (got ${op}${raw})`);
    }
  };
}

/** Flatten an argument into individual values, expanding a range into its cells. */
function flat(node: Node, context: Context): FormulaValue[] {
  if (node.n === "range") return rangeValues(context.sheet, node.refs);
  return [evaluate(node, context)];
}

/** Numbers only, blanks and text skipped — the behaviour of SUM/AVERAGE/COUNT over a column. */
function numbersOf(nodes: Node[], context: Context): number[] {
  const out: number[] = [];
  for (const node of nodes) {
    for (const value of flat(node, context)) {
      if (value.kind === "number") out.push(value.value as number);
      else if (value.kind === "boolean") out.push(value.value ? 1 : 0);
    }
  }
  return out;
}

const TEXT_FUNCTIONS = new Set([
  "CONCAT", "CONCATENATE", "TEXTJOIN", "LEFT", "RIGHT", "MID", "LEN", "TRIM", "UPPER", "LOWER", "TEXT",
]);
const MATH_FUNCTIONS = new Set([
  "SUM", "SUMIF", "SUMIFS", "SUMPRODUCT", "AVERAGE", "AVERAGEIF", "AVERAGEIFS", "COUNT", "COUNTA",
  "COUNTIF", "COUNTIFS", "COUNTBLANK", "MIN", "MAX", "ROUND", "ROUNDUP", "ROUNDDOWN", "ABS", "INT",
  "MEDIAN", "STDEV", "PRODUCT", "MOD", "SQRT", "POWER", "LARGE", "SMALL", "RANK", "PERCENTILE",
]);
const LOGIC_FUNCTIONS = new Set(["IF", "IFS", "IFERROR", "IFNA", "AND", "OR", "NOT", "SWITCH", "ISBLANK", "ISNUMBER", "ISTEXT"]);
const LOOKUP_FUNCTIONS = new Set(["VLOOKUP", "HLOOKUP", "INDEX", "MATCH", "XLOOKUP", "CHOOSE"]);
const DATE_FUNCTIONS = new Set(["YEAR", "MONTH", "DAY", "DATE", "DAYS", "EOMONTH", "EDATE"]);
const FINANCE_FUNCTIONS = new Set(["NPV", "IRR", "PMT", "FV", "PV", "RATE", "NPER"]);

export const SUPPORTED_FUNCTIONS: readonly string[] = [
  ...TEXT_FUNCTIONS, ...MATH_FUNCTIONS, ...LOGIC_FUNCTIONS, ...LOOKUP_FUNCTIONS,
  ...DATE_FUNCTIONS, ...FINANCE_FUNCTIONS,
].sort();

function callFunction(name: string, argNodes: Node[], context: Context): FormulaValue {
  const arg = (index: number): FormulaValue => {
    const node = argNodes[index];
    if (!node) throw new FormulaError("#VALUE!", `${name} needs argument ${index + 1}`);
    return evaluate(node, context);
  };
  const need = (count: number): void => {
    if (argNodes.length < count) {
      throw new FormulaError("#VALUE!", `${name} needs at least ${count} argument(s)`);
    }
  };
  /** Range-aware argument: a range becomes a flat list, a scalar becomes one value. */
  const list = (index: number): FormulaValue[] => {
    const node = argNodes[index];
    if (!node) throw new FormulaError("#VALUE!", `${name} needs argument ${index + 1}`);
    return flat(node, context);
  };

  switch (name) {
    // ---- math
    case "SUM": return num(numbersOf(argNodes, context).reduce((a, b) => a + b, 0));
    case "PRODUCT": {
      const values = numbersOf(argNodes, context);
      return num(values.length === 0 ? 0 : values.reduce((a, b) => a * b, 1));
    }
    case "AVERAGE": {
      const values = numbersOf(argNodes, context);
      if (values.length === 0) throw new FormulaError("#DIV/0!");
      return num(values.reduce((a, b) => a + b, 0) / values.length);
    }
    case "COUNT": return num(numbersOf(argNodes, context).length);
    case "COUNTA": {
      let count = 0;
      for (const node of argNodes) for (const value of flat(node, context)) if (value.kind !== "blank") count += 1;
      return num(count);
    }
    case "COUNTBLANK": {
      let count = 0;
      for (const node of argNodes) for (const value of flat(node, context)) if (value.kind === "blank") count += 1;
      return num(count);
    }
    case "MIN": {
      const values = numbersOf(argNodes, context);
      return num(values.length === 0 ? 0 : Math.min(...values));
    }
    case "MAX": {
      const values = numbersOf(argNodes, context);
      return num(values.length === 0 ? 0 : Math.max(...values));
    }
    case "MEDIAN": {
      const values = numbersOf(argNodes, context).slice().sort((a, b) => a - b);
      if (values.length === 0) throw new FormulaError("#NUM!");
      const mid = Math.floor(values.length / 2);
      return num(values.length % 2 ? values[mid] : (values[mid - 1] + values[mid]) / 2);
    }
    case "STDEV": {
      const values = numbersOf(argNodes, context);
      // Sample standard deviation (STDEV.S), which is what Excel's STDEV returns.
      if (values.length < 2) throw new FormulaError("#DIV/0!");
      const mean = values.reduce((a, b) => a + b, 0) / values.length;
      const variance = values.reduce((a, b) => a + (b - mean) ** 2, 0) / (values.length - 1);
      return num(Math.sqrt(variance));
    }
    case "ABS": need(1); return num(Math.abs(toNumber(arg(0))));
    case "INT": need(1); return num(Math.floor(toNumber(arg(0))));
    case "SQRT": need(1); return num(Math.sqrt(toNumber(arg(0))));
    case "MOD": need(2); return num(toNumber(arg(0)) % toNumber(arg(1)));
    case "POWER": need(2); return num(toNumber(arg(0)) ** toNumber(arg(1)));
    case "ROUND": {
      need(2);
      const factor = 10 ** toNumber(arg(1));
      // Excel rounds half away from zero, not half-to-even as Math.round does for negatives.
      const scaled = toNumber(arg(0)) * factor;
      return num((scaled < 0 ? -Math.round(-scaled) : Math.round(scaled)) / factor);
    }
    case "ROUNDUP": {
      need(2);
      const factor = 10 ** toNumber(arg(1));
      const scaled = toNumber(arg(0)) * factor;
      return num((scaled < 0 ? -Math.ceil(-scaled) : Math.ceil(scaled)) / factor);
    }
    case "ROUNDDOWN": {
      need(2);
      const factor = 10 ** toNumber(arg(1));
      const scaled = toNumber(arg(0)) * factor;
      return num((scaled < 0 ? -Math.floor(-scaled) : Math.floor(scaled)) / factor);
    }
    case "SUMPRODUCT": {
      const columns = argNodes.map((node) => flat(node, context));
      const length = columns[0]?.length ?? 0;
      if (columns.some((column) => column.length !== length)) {
        throw new FormulaError("#VALUE!", "SUMPRODUCT ranges must be the same size");
      }
      let total = 0;
      for (let i = 0; i < length; i += 1) {
        let product = 1;
        for (const column of columns) product *= toNumber(column[i]);
        total += product;
      }
      return num(total);
    }
    case "SUMIF":
    case "AVERAGEIF": {
      need(2);
      const criteria = criteriaMatcher(arg(1));
      const range = list(0);
      const sumRange = argNodes.length > 2 ? list(2) : range;
      const picked: number[] = [];
      range.forEach((value, index) => {
        if (!criteria(value)) return;
        const target = sumRange[index];
        if (target && target.kind === "number") picked.push(target.value as number);
        else if (target && target.kind === "boolean") picked.push(target.value ? 1 : 0);
      });
      if (name === "SUMIF") return num(picked.reduce((a, b) => a + b, 0));
      if (picked.length === 0) throw new FormulaError("#DIV/0!");
      return num(picked.reduce((a, b) => a + b, 0) / picked.length);
    }
    case "COUNTIF": {
      need(2);
      const criteria = criteriaMatcher(arg(1));
      return num(list(0).filter(criteria).length);
    }
    case "SUMIFS":
    case "COUNTIFS":
    case "AVERAGEIFS": {
      if (argNodes.length < 3 || argNodes.length % 2 === 0) {
        throw new FormulaError("#VALUE!", `${name} takes a target then range/criteria pairs`);
      }
      const isCount = name === "COUNTIFS";
      const target = isCount ? null : list(0);
      const firstPair = isCount ? 0 : 1;
      const pairs: { values: FormulaValue[]; criteria: (value: FormulaValue) => boolean }[] = [];
      for (let i = firstPair; i + 1 < argNodes.length; i += 2) {
        pairs.push({ values: list(i), criteria: criteriaMatcher(arg(i + 1)) });
      }
      const length = pairs[0].values.length;
      if (pairs.some((pair) => pair.values.length !== length) || (target && target.length !== length)) {
        throw new FormulaError("#VALUE!", `${name} ranges must be the same size`);
      }
      const picked: number[] = [];
      let count = 0;
      for (let i = 0; i < length; i += 1) {
        if (!pairs.every((pair) => pair.criteria(pair.values[i]))) continue;
        count += 1;
        const value = target?.[i];
        if (value && value.kind === "number") picked.push(value.value as number);
      }
      if (name === "COUNTIFS") return num(count);
      if (name === "SUMIFS") return num(picked.reduce((a, b) => a + b, 0));
      if (picked.length === 0) throw new FormulaError("#DIV/0!");
      return num(picked.reduce((a, b) => a + b, 0) / picked.length);
    }
    case "LARGE":
    case "SMALL": {
      need(2);
      const values = numbersOf([argNodes[0]], context).slice().sort((a, b) => a - b);
      const k = toNumber(arg(1));
      if (k < 1 || k > values.length) throw new FormulaError("#NUM!");
      return num(name === "LARGE" ? values[values.length - k] : values[k - 1]);
    }
    case "RANK": {
      need(2);
      const values = numbersOf([argNodes[1]], context);
      const target = toNumber(arg(0));
      const sorted = values.slice().sort((a, b) => b - a);
      const index = sorted.indexOf(target);
      if (index === -1) throw new FormulaError("#N/A");
      return num(index + 1);
    }
    case "PERCENTILE": {
      need(2);
      const values = numbersOf([argNodes[0]], context).slice().sort((a, b) => a - b);
      const p = toNumber(arg(1));
      if (values.length === 0 || p < 0 || p > 1) throw new FormulaError("#NUM!");
      const position = (values.length - 1) * p;
      const lower = Math.floor(position);
      const upper = Math.ceil(position);
      if (lower === upper) return num(values[lower]);
      return num(values[lower] + (values[upper] - values[lower]) * (position - lower));
    }

    // ---- logic
    case "IF": {
      need(2);
      return toBoolean(arg(0)) ? arg(1) : argNodes.length > 2 ? arg(2) : bool(false);
    }
    case "IFS": {
      if (argNodes.length % 2 !== 0) throw new FormulaError("#VALUE!", "IFS takes condition/value pairs");
      for (let i = 0; i + 1 < argNodes.length; i += 2) {
        if (toBoolean(arg(i))) return arg(i + 1);
      }
      throw new FormulaError("#N/A", "no IFS condition matched");
    }
    case "IFERROR": {
      need(2);
      try {
        return arg(0);
      } catch (error) {
        if (error instanceof FormulaError) return arg(1);
        throw error;
      }
    }
    case "IFNA": {
      need(2);
      try {
        return arg(0);
      } catch (error) {
        if (error instanceof FormulaError && error.code === "#N/A") return arg(1);
        throw error;
      }
    }
    case "AND":
    case "OR": {
      const values = argNodes.flatMap((node) => flat(node, context));
      const bools = values.map(toBoolean);
      return bool(name === "AND" ? bools.every(Boolean) : bools.some(Boolean));
    }
    case "NOT": need(1); return bool(!toBoolean(arg(0)));
    case "ISBLANK": need(1); return bool(arg(0).kind === "blank");
    case "ISNUMBER": need(1); return bool(arg(0).kind === "number");
    case "ISTEXT": need(1); return bool(arg(0).kind === "string");
    case "SWITCH": {
      need(3);
      const subject = toText(arg(0));
      for (let i = 1; i + 1 < argNodes.length; i += 2) {
        if (toText(arg(i)).toUpperCase() === subject.toUpperCase()) return arg(i + 1);
      }
      return argNodes.length % 2 === 0 ? arg(argNodes.length - 1) : err("#N/A");
    }

    // ---- text
    case "CONCAT":
    case "CONCATENATE": {
      let out = "";
      for (const node of argNodes) for (const value of flat(node, context)) out += toText(value);
      return str(out);
    }
    case "TEXTJOIN": {
      need(3);
      const separator = toText(arg(0));
      const skipEmpty = toBoolean(arg(1));
      const parts: string[] = [];
      for (let i = 2; i < argNodes.length; i += 1) {
        for (const value of flat(argNodes[i], context)) {
          const text = toText(value);
          if (skipEmpty && text === "") continue;
          parts.push(text);
        }
      }
      return str(parts.join(separator));
    }
    case "LEFT":
    case "RIGHT": {
      need(1);
      const text = toText(arg(0));
      const count = argNodes.length > 1 ? toNumber(arg(1)) : 1;
      return str(name === "LEFT" ? text.slice(0, count) : text.slice(text.length - count));
    }
    case "MID": {
      need(3);
      const text = toText(arg(0));
      const start = toNumber(arg(1));
      const count = toNumber(arg(2));
      if (start < 1 || count < 0) throw new FormulaError("#VALUE!");
      return str(text.slice(start - 1, start - 1 + count));
    }
    case "LEN": need(1); return num(toText(arg(0)).length);
    case "TRIM": need(1); return str(toText(arg(0)).replace(/\s+/g, " ").trim());
    case "UPPER": need(1); return str(toText(arg(0)).toUpperCase());
    case "LOWER": need(1); return str(toText(arg(0)).toLowerCase());
    case "TEXT": {
      need(2);
      const value = toNumber(arg(0));
      const format = toText(arg(1));
      if (format.includes("%")) {
        const decimals = (format.split(".")[1] ?? "").replace(/[^0]/g, "").length;
        return str(`${(value * 100).toFixed(decimals)}%`);
      }
      const decimals = (format.split(".")[1] ?? "").replace(/[^0]/g, "").length;
      const formatted = Math.abs(value).toFixed(decimals);
      const [whole, fraction] = formatted.split(".");
      const grouped = whole.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
      const body = fraction ? `${grouped}.${fraction}` : grouped;
      return str(value < 0 ? `(${body})` : body);
    }

    // ---- lookup
    case "VLOOKUP": {
      need(3);
      const needle = arg(0);
      const table = rangeRefsOf(argNodes[1], context);
      const columnIndex = toNumber(arg(2));
      const approximate = argNodes.length > 3 ? toBoolean(arg(3)) : true;
      if (table.length === 0) throw new FormulaError("#N/A");
      const width = table[0].length;
      if (columnIndex < 1 || columnIndex > width) throw new FormulaError("#REF!");
      if (!approximate) {
        const row = table.find((cells) => toText(cells[0]).toUpperCase() === toText(needle).toUpperCase());
        if (!row) throw new FormulaError("#N/A");
        return row[columnIndex - 1];
      }
      // Approximate match: the last row whose first column is <= the needle, assuming ascending order.
      let found: FormulaValue[] | null = null;
      for (const cells of table) {
        const head = cells[0];
        const isText = head.kind === "string" || needle.kind === "string";
        const reached = isText
          ? toText(head).toUpperCase() <= toText(needle).toUpperCase()
          : toNumber(head) <= toNumber(needle);
        if (reached) found = cells;
        else break;
      }
      if (!found) throw new FormulaError("#N/A");
      return found[columnIndex - 1];
    }
    case "HLOOKUP": {
      need(3);
      const needle = arg(0);
      const table = rangeRefsOf(argNodes[1], context);
      const rowIndex = toNumber(arg(2));
      if (table.length === 0) throw new FormulaError("#N/A");
      const width = table[0].length;
      if (rowIndex < 1 || rowIndex > table.length) throw new FormulaError("#REF!");
      let matchedColumn = -1;
      for (let col = 0; col < width; col += 1) {
        const head = table[0][col];
        const isText = head.kind === "string" || needle.kind === "string";
        const equal = isText
          ? toText(head).toUpperCase() === toText(needle).toUpperCase()
          : toNumber(head) === toNumber(needle);
        if (equal) { matchedColumn = col; break; }
      }
      if (matchedColumn === -1) throw new FormulaError("#N/A");
      return table[rowIndex - 1][matchedColumn];
    }
    case "INDEX": {
      need(2);
      const table = rangeRefsOf(argNodes[0], context);
      const rowIndex = toNumber(arg(1));
      const columnIndex = argNodes.length > 2 ? toNumber(arg(2)) : 1;
      if (rowIndex < 1 || rowIndex > table.length) throw new FormulaError("#REF!");
      const width = table[0]?.length ?? 0;
      if (columnIndex < 1 || columnIndex > width) throw new FormulaError("#REF!");
      return table[rowIndex - 1][columnIndex - 1];
    }
    case "MATCH": {
      need(2);
      const needle = arg(0);
      const values = list(1);
      const matchType = argNodes.length > 2 ? toNumber(arg(2)) : 1;
      if (matchType === 0) {
        const index = values.findIndex(
          (value) => toText(value).toUpperCase() === toText(needle).toUpperCase(),
        );
        if (index === -1) throw new FormulaError("#N/A");
        return num(index + 1);
      }
      if (matchType === 1) {
        let found = -1;
        for (let i = 0; i < values.length; i += 1) {
          if (toNumber(values[i]) <= toNumber(needle)) found = i;
          else break;
        }
        if (found === -1) throw new FormulaError("#N/A");
        return num(found + 1);
      }
      throw new FormulaError("#N/A", "MATCH with match_type -1 is not supported");
    }
    case "XLOOKUP": {
      need(3);
      const needle = arg(0);
      const lookup = list(1);
      const result = list(2);
      const index = lookup.findIndex(
        (value) => toText(value).toUpperCase() === toText(needle).toUpperCase(),
      );
      if (index === -1) {
        if (argNodes.length > 3) return arg(3);
        throw new FormulaError("#N/A");
      }
      if (index >= result.length) throw new FormulaError("#VALUE!", "XLOOKUP result range is shorter");
      return result[index];
    }
    case "CHOOSE": {
      need(2);
      const index = toNumber(arg(0));
      if (index < 1 || index > argNodes.length - 1) throw new FormulaError("#VALUE!");
      return arg(index);
    }

    // ---- date (serial numbers, 1900 system, as Excel stores them)
    case "DATE": {
      need(3);
      const year = toNumber(arg(0));
      const month = toNumber(arg(1));
      const day = toNumber(arg(2));
      // UTC throughout so the answer does not depend on the machine's timezone.
      const ms = Date.UTC(year, month - 1, day);
      const epoch = Date.UTC(1899, 11, 30);
      return num(Math.round((ms - epoch) / 86400000));
    }
    case "YEAR":
    case "MONTH":
    case "DAY": {
      need(1);
      const serial = toNumber(arg(0));
      const date = new Date(Date.UTC(1899, 11, 30) + serial * 86400000);
      if (name === "YEAR") return num(date.getUTCFullYear());
      if (name === "MONTH") return num(date.getUTCMonth() + 1);
      return num(date.getUTCDate());
    }
    case "DAYS": {
      need(2);
      return num(toNumber(arg(0)) - toNumber(arg(1)));
    }
    case "EDATE":
    case "EOMONTH": {
      need(2);
      const serial = toNumber(arg(0));
      const months = toNumber(arg(1));
      const base = new Date(Date.UTC(1899, 11, 30) + serial * 86400000);
      const year = base.getUTCFullYear();
      const month = base.getUTCMonth() + months;
      const day = name === "EOMONTH" ? 0 : base.getUTCDate();
      const target = name === "EOMONTH"
        ? new Date(Date.UTC(year, month + 1, 0))
        : new Date(Date.UTC(year, month, day));
      if (name === "EDATE" && target.getUTCDate() !== day) {
        // Excel clamps to the last day of the target month.
        return num(Math.round((Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)
          - Date.UTC(1899, 11, 30)) / 86400000));
      }
      return num(Math.round((target.getTime() - Date.UTC(1899, 11, 30)) / 86400000));
    }

    // ---- finance
    case "NPV": {
      need(2);
      const rate = toNumber(arg(0));
      const flows = numbersOf(argNodes.slice(1), context);
      // Excel discounts the first value by one period, not zero.
      return num(flows.reduce((total, flow, index) => total + flow / (1 + rate) ** (index + 1), 0));
    }
    case "PMT": {
      need(3);
      const rate = toNumber(arg(0));
      const periods = toNumber(arg(1));
      const present = toNumber(arg(2));
      const future = argNodes.length > 3 ? toNumber(arg(3)) : 0;
      const type = argNodes.length > 4 ? toNumber(arg(4)) : 0;
      if (rate === 0) return num(-(present + future) / periods);
      const factor = (1 + rate) ** periods;
      return num(-(present * factor + future) * rate / ((factor - 1) * (1 + rate * type)));
    }
    case "FV": {
      need(3);
      const rate = toNumber(arg(0));
      const periods = toNumber(arg(1));
      const payment = toNumber(arg(2));
      const present = argNodes.length > 3 ? toNumber(arg(3)) : 0;
      const type = argNodes.length > 4 ? toNumber(arg(4)) : 0;
      if (rate === 0) return num(-(present + payment * periods));
      const factor = (1 + rate) ** periods;
      return num(-(present * factor + payment * (1 + rate * type) * (factor - 1) / rate));
    }
    case "PV": {
      need(3);
      const rate = toNumber(arg(0));
      const periods = toNumber(arg(1));
      const payment = toNumber(arg(2));
      const future = argNodes.length > 3 ? toNumber(arg(3)) : 0;
      const type = argNodes.length > 4 ? toNumber(arg(4)) : 0;
      if (rate === 0) return num(-(future + payment * periods));
      const factor = (1 + rate) ** periods;
      return num(-(future + payment * (1 + rate * type) * (factor - 1) / rate) / factor);
    }
    case "NPER": {
      need(3);
      const rate = toNumber(arg(0));
      const payment = toNumber(arg(1));
      const present = toNumber(arg(2));
      const future = argNodes.length > 3 ? toNumber(arg(3)) : 0;
      if (rate === 0) return num(-(present + future) / payment);
      if (payment / rate <= -present * 0 + Number.NEGATIVE_INFINITY) throw new FormulaError("#NUM!");
      return num(Math.log((payment - future * rate) / (payment + present * rate)) / Math.log(1 + rate));
    }
    case "IRR": {
      need(1);
      const flows = numbersOf([argNodes[0]], context);
      if (flows.length < 2) throw new FormulaError("#NUM!");
      const npv = (rate: number): number =>
        flows.reduce((total, flow, index) => total + flow / (1 + rate) ** index, 0);
      // Bisection over a wide bracket: predictable and always converges for a conventional sign pattern,
      // where Newton's method can wander. Excel's own default guess does not affect the root found here.
      let low = -0.999999;
      let high = 10;
      let fLow = npv(low);
      let fHigh = npv(high);
      if (fLow * fHigh > 0) throw new FormulaError("#NUM!", "IRR did not bracket a root");
      for (let i = 0; i < 200; i += 1) {
        const mid = (low + high) / 2;
        const fMid = npv(mid);
        if (Math.abs(fMid) < 1e-12 || high - low < 1e-12) return num(mid);
        if (fLow * fMid <= 0) { high = mid; fHigh = fMid; } else { low = mid; fLow = fMid; }
      }
      return num((low + high) / 2);
    }
    case "RATE": {
      throw new FormulaError("#N/A", "RATE is not implemented; use IRR on explicit cash flows");
    }
    default:
      throw new FormulaError(
        "#NAME?",
        `unsupported function ${name}. Supported: ${SUPPORTED_FUNCTIONS.join(", ")}`,
      );
  }
}

/** A range argument as rows of values, for the lookup functions. */
function rangeRefsOf(node: Node, context: Context): FormulaValue[][] {
  if (node.n !== "range") {
    throw new FormulaError("#VALUE!", "expected a range (e.g. A1:C4) as the table argument");
  }
  const cells = node.refs.map((ref) => parseRef(ref));
  const rows = Math.max(...cells.map((c) => c.row)) - Math.min(...cells.map((c) => c.row)) + 1;
  const cols = Math.max(...cells.map((c) => c.col)) - Math.min(...cells.map((c) => c.col)) + 1;
  const grid: FormulaValue[][] = [];
  for (let r = 0; r < rows; r += 1) {
    const row: FormulaValue[] = [];
    for (let c = 0; c < cols; c += 1) {
      row.push(cellAt(context.sheet, `${columnLetter(Math.min(...cells.map((x) => x.col)) + c)}`
        + `${Math.min(...cells.map((x) => x.row)) + r + 1}`));
    }
    grid.push(row);
  }
  return grid;
}

// ---------------------------------------------------------------- entry point

/**
 * Evaluate one formula against one sheet.
 *
 * `sheet` is row-major and may be ragged; a cell outside it reads as blank, which is what a learner sees when
 * they reference an empty cell in Excel. The leading `=` is optional so a question can present either form.
 */
export function evaluateFormula(formula: string, sheet: Sheet = []): FormulaValue {
  const source = formula.trim().replace(/^=/, "");
  if (source === "") return blank();
  try {
    return evaluate(parse(tokenize(source), { sheet }), { sheet });
  } catch (error) {
    if (error instanceof FormulaError) return error;
    throw error;
  }
}

/** The plain JS value a test's `expected` should hold: number, string, boolean, or null. */
export function evaluateFormulaValue(formula: string, sheet: Sheet = []): number | string | boolean | null {
  return normalize(evaluateFormula(formula, sheet));
}
