# `lexer.py` — Tokenizer Documentation

This document is a hacking guide to `lexer.py`, the PLY (Python Lex-Yacc) lexer that feeds `parser.py`. It explains every token category, how PLY resolves competing patterns, and how to extend the lexer without introducing silent bugs.

---

## 1. Overview

`lexer.py` uses **PLY's lexer** (`ply.lex`). It defines:

1. A `tokens` tuple (the canonical list of token *names*).
2. A `reserved` dict mapping keyword *strings* to token names.
3. A set of `t_<NAME>` regex variables and functions.
4. `literals` — single-character tokens handled without explicit rules.
5. `t_ignore` / `t_ignore_COMMENT` — silently consumed text.
6. `t_error` — illegal character handling.

At the end, `lexer = lex.lex()` builds the tokenizer. `parser.py` imports `lexer`, `tokens`, and `reserved`.

**Design principle:** the lexer emits **raw token text**. It does not decode strings, does not parse numbers, and does not distinguish between `i32` as a type vs. an identifier. Everything downstream (`parser.py`, `codegen.py`) is responsible for interpretation. Keep it that way — a "smart" lexer makes debugging much harder.

---

## 2. Token List

```python
tokens = (
    'COMMENT', 'FLOAT_NUMBER', 'HEX_NUMBER', 'NUMBER',
    'STRING', 'IDENT', 'LOGICAL_AND', 'LOGICAL_OR', 'LOGICAL_NOT',
    'SHL', 'SHR', 'BIT_AND', 'BIT_OR', 'BIT_XOR', 'BIT_NOT',
    'EQ', 'NEQ', 'LE', 'GE', 'LT', 'GT',
    'INC', 'DEC', 'ADD_ASSIGN', 'SUB_ASSIGN', 'MUL_ASSIGN', 'DIV_ASSIGN',
    'PLUS', 'MINUS', 'MUL', 'DIV', 'MOD',
    'ARROW', 'ELLIPSIS', 'DOT', 'ASSIGN', 'COMMA', 'COLON', 'SEMI',
)
```

Then extended with keyword token names:

```python
tokens = tokens + tuple(set(reserved.values()))
```

**Important:** this rebuild is a **tuple of strings**, appended to `tokens` at module load. If you add a new reserved word, its token name is automatically added to `tokens`. If you add a new non-reserved token (like a new operator), you must add it to the *first* tuple explicitly.

**Duplicate names:** if a token name appears in both the base tuple and `reserved.values()`, `tokens` will contain it twice. PLY tolerates this, but it's noise — keep names unique.

---

## 3. Reserved Words

```python
reserved = {
    'struct': 'KEYWORD_STRUCT', 'union': 'KEYWORD_UNION', 'enum': 'KEYWORD_ENUM',
    'typedef': 'KEYWORD_TYPEDEF', 'const': 'KEYWORD_CONST', 'volatile': 'KEYWORD_VOLATILE',
    'restrict': 'KEYWORD_RESTRICT',
    'float': 'TYPE_FLOAT', 'double': 'TYPE_DOUBLE', 'void': 'TYPE_VOID',
    'let': 'LET', 'if': 'IF', 'elif': 'ELIF', 'else': 'ELSE',
    'while': 'WHILE', 'for': 'FOR', 'def': 'DEF', 'fn': 'DEF',
    'return': 'RETURN', 'break': 'BREAK', 'continue': 'CONTINUE',
    'asm': 'KEYWORD_ASM',
    'template': 'KEYWORD_TEMPLATE',
    'outputs': 'KEYWORD_OUTPUTS',
    'inputs': 'KEYWORD_INPUTS',
    'clobbers': 'KEYWORD_CLOBBERS',
}
```

Reserved words are matched by `t_IDENT` and re-typed via `t.type = reserved.get(t.value, 'IDENT')`.

**Key rule:** every value in `reserved` must be a valid token name. Since `tokens` is extended from `reserved.values()`, this is automatic — but it means adding a keyword here *creates* its token name. You still need to *use* that token name somewhere in `parser.py`, or PLY will accept it but the parser will treat it as a syntax error.

**Why sized integer types (`i32`, `i8`, ...) are absent:**
The comment explains this: `i32`, `i8`, `ptr`, `string`, `half`, `bfloat`, etc. are **not** reserved. They lex as `IDENT` and are resolved by `codegen._get_llvm_type`. This is intentional — it lets the language add integer widths without touching the lexer. Only `float`, `double`, and `void` remain reserved because they're grammatical keywords in `p_type`.

**If you add a type keyword here** (e.g. `bool`), remember you must also:
1. Use its token name in `parser.p_type`'s alternatives.
2. Handle it in `codegen._get_llvm_type`.

**If you add a keyword that collides with an existing identifier name** in user code (e.g. someone named a variable `template` before this keyword existed), their code will suddenly break. Keyword additions are effectively breaking changes.

---

## 4. Literals

```python
literals = ['(', ')', '[', ']', '{', '}']
```

These are single characters that PLY emits as one-char tokens **without needing `t_*` rules**. Each character is its own token whose *name equals its value*.

**To add a new single-char literal** (e.g. `@`): add it here. In `parser.py`, refer to it in rule docstrings as `'@'` (quoted). No `t_@` rule is needed — but you *also* can't define a `t_@` function (the name isn't a valid identifier).

**What *not* to add here:** characters that are also prefixes of multi-char operators (`<`, `>`, `&`, `|`, `=`, `!`, `*`, `+`, `-`, `/`, `.`, `:`). Those need regex rules so PLY's longest-match preference handles `<=` before `<`. PLY would try literals first and get `<=` wrong.

**Note:** `'['` and `']'` are literals. This is why `parser.p_type_array` can use `'[' NUMBER IDENT type ']'` without any `t_LBRACKET` rule. The same literals are used in asm field lists.

**Note:** `','`, `';'`, `':'`, `'.'` are **not** in `literals` — they have explicit `t_*` rules. That's fine, but keep in mind that if you ever need them in rule docstrings, use the same token names (`COMMA`, `SEMI`, `COLON`, `DOT`).

---

## 5. Regex Rules

PLY classifies `t_*` definitions into two kinds:

- **Variables** (`t_NAME = r'...'`): simple regexes with no action. PLY collects them and compiles one master regex, ordered by **decreasing regex length**. Longer patterns win.
- **Functions** (`def t_NAME(t): ...`): called with the matched token. Can modify `t.type`, `t.value`, or return `None` to discard.

**Order rule:** Functions are always tried **before** variables, in the order they appear in the file. Variables are tried in decreasing length order. Mixing the two requires care (see `t_IDENT` below).

### 5.1 Numeric tokens

```python
t_FLOAT_NUMBER = r'\b\d+\.\d+([eE][+-]?\d+)?\b'
t_HEX_NUMBER   = r'\b0[xX][0-9a-fA-F]+\b'
t_NUMBER       = r'\b\d+\b'
```

**Order matters** for variables with the same length? Actually PLY sorts by regex-pattern length in *characters*, not by specificity. Here:
- `t_FLOAT_NUMBER`'s pattern is longest (63 chars).
- `t_HEX_NUMBER` next (~33 chars).
- `t_NUMBER` shortest (~11 chars).

So `3.14` matches `FLOAT_NUMBER` before `NUMBER`, and `0xFF` matches `HEX_NUMBER` before `NUMBER`. But **this is fragile** — it depends on total pattern length, not on how specific the pattern is.

**Safer pattern:** if you add more numeric forms (binary `0b1010`, octal `0o777`, digit separators `1_000`, suffixes `42u`), consider replacing all three variables with a **single `t_NUMBER` function** that inspects `t.value` and sets `t.type` accordingly. That removes ambiguity entirely.

**Current limitations:**
- No binary/octal literals (only decimal and hex).
- No digit separators.
- No type suffixes (`42u`, `3.14f`).
- Exponent-only floats (`1e10`) are **not** matched — they'd lex as `NUMBER` (`1`) followed by `IDENT` (`e10`). Add `\d+[eE][+-]?\d+` if you want them.
- The `\b` boundaries are good; they prevent `0x1.2` from being split weirdly.

**To add binary literals:** insert `t_BINARY_NUMBER = r'\b0[bB][01]+\b'` **above** `t_HEX_NUMBER` (or convert to a function, which is cleaner).

### 5.2 Strings

```python
t_STRING = r'"[^"]*"'
```

Matches any double-quoted string with no escapes. The raw token text (including quotes) is stored in `t.value`.

**Crucial:** `codegen.visit_LiteralNode` and `codegen._decode_string_literal` decode the token using Python's `unicode_escape`, which understands `\n`, `\t`, `\\`, `\x41`, etc. The lexer does **not** interpret escapes; it just passes the raw text.

**Consequences:**
- `"\\"` (raw token = `"\\"`) decodes to `\`. Fine.
- `"\""` (raw token = `"\"`) — but the regex `[^"]*` stops at the escaped quote, so this fails to parse. **Escaped quotes are not supported.**
- `"\n"` (raw token = `"\n"` — two chars, backslash and `n`) decodes to a newline via `unicode_escape`. Works, but relies on codegen.

**To support escaped quotes**, change the regex to `r'"([^"\\]|\\.)*"'`. Then `codegen._decode_string_literal` already handles the escapes.

**To support raw strings** (`r"..."`): add a rule that strips the prefix, or a dedicated `t_RAW_STRING` with value containing no backslashes. Coordinate with the parser — `LiteralNode.value` should indicate the string is raw.

**To support single-quoted strings** (or char literals): add a separate `t_CHAR` or extend `t_STRING`. Char literals would need a distinct token since they semantically produce an integer, not a pointer.

### 5.3 Operators

```python
t_LOGICAL_AND = r'&&'
t_LOGICAL_OR  = r'\|\|'
t_LOGICAL_NOT = r'!'
t_SHL         = r'<<'
t_SHR         = r'>>'
t_BIT_AND     = r'&'
t_BIT_OR      = r'\|'
t_BIT_XOR     = r'\^'
t_BIT_NOT     = r'~'
t_EQ          = r'=='
t_NEQ         = r'!='
t_LE          = r'<='
t_GE          = r'>='
t_LT          = r'<'
t_GT          = r'>'
t_INC         = r'\+\+'
t_DEC         = r'--'
t_ADD_ASSIGN  = r'\+='
t_SUB_ASSIGN  = r'-='
t_MUL_ASSIGN  = r'\*='
t_DIV_ASSIGN  = r'/='
t_PLUS        = r'\+'
t_MINUS       = r'-'
t_MUL         = r'\*'
t_DIV         = r'/'
t_MOD         = r'%'
t_ARROW       = r'->'
t_ELLIPSIS    = r'\.\.\.'
t_DOT         = r'\.'
t_ASSIGN      = r'='
t_COMMA       = r','
t_COLON       = r':'
t_SEMI        = r';'
```

**How PLY resolves ambiguity:** because these are variables (not functions), PLY sorts them by **pattern length descending** and tries them in that order. So `<=` (2 chars) is tried before `<` (1 char). This is why `<=` lexes as `LE`, not `LT` followed by `ASSIGN`.

**Danger zone:** the sort is by *total pattern character count*, not by "specificity". For example, `t_ELLIPSIS = r'\.\.\.'` is longer than `t_DOT = r'\.'`, so `...` correctly wins over `.` `.` `.`. But if you had two patterns of equal length, PLY's order would be file order (undefined for variables). Keep new multi-char operators strictly longer than their single-char prefixes.

**To add a new operator:**
1. Add its name to the `tokens` tuple (first tuple — the manually-maintained one).
2. Add `t_YOURNAME = r'...'` in the operator block.
3. Add it to `precedence` in `parser.py` if it's an operator.
4. Add productions to `parser.py`.
5. Handle it in `codegen.py`.

**Note on unused tokens:** the lexer defines `INC`, `DEC`, `ADD_ASSIGN`, `SUB_ASSIGN`, `MUL_ASSIGN`, `DIV_ASSIGN`, `ELLIPSIS`, and `COMMENT` — but `parser.py` doesn't use most of them. PLY accepts unused tokens without complaint; they're just never produced in a way the parser cares about. Removing them from `tokens` would break the `t_*` definitions (PLY refuses to define `t_X` unless `X` is in `tokens`), so either keep them or remove both sides.

**`t_ignore_COMMENT`** (below) means `COMMENT` is never actually returned as a token — the `tokens` entry is vestigial.

### 5.4 Identifiers / keywords

```python
def t_IDENT(t):
    r'[a-zA-Z_][a-zA-Z0-9_]*'
    t.type = reserved.get(t.value, 'IDENT')
    return t
```

This is the **only function rule**, so PLY tries it before any variable. That means identifiers are always recognized before operators — but that's fine because operators can't start with a letter or underscore.

**The `reserved` lookup is the keyword mechanism.** Any word not in `reserved` stays `IDENT`.

**`t.type` vs `t.value`:** `t.value` remains the original string (e.g. `"let"`); `t.type` becomes `"LET"`. The parser dispatches on `t.type`.

**To add a keyword:** add `'yourword': 'YOUR_TOKEN'` to `reserved`. Done. The token is automatically in `tokens`.

**To add a keyword that starts with a digit:** impossible — identifiers must start with a letter or `_`. If you want `0x`-style keywords, they need a dedicated regex before `t_NUMBER`.

**To add a keyword that's also an operator spelling** (e.g. `as`, `is`): fine, they're alphanumeric.

**To make keywords context-sensitive** (e.g. `type` is a keyword in some contexts but an identifier in others): the standard trick is to *not* put it in `reserved`, keep it as `IDENT`, and let the parser decide. This is how `i32` etc. work today.

**Case sensitivity:** `t_IDENT` is case-sensitive. `Let` and `let` are different tokens. Add `re.IGNORECASE` (via the `re` module) if you want case-insensitive keywords — but then identifiers also become case-insensitive, which is usually undesirable.

### 5.5 Whitespace and comments

```python
t_ignore = ' \t\n'
t_ignore_COMMENT = r'//.*'
```

`t_ignore` is PLY's mechanism for characters that are silently dropped. They cannot be token names. `\n` is included here — **the lexer does not track line numbers via `t_newline`**. This means `t.lineno` never increments. If you want line numbers in error messages, remove `\n` from `t_ignore` and add:

```python
def t_newline(t):
    r'\n+'
    t.lexer.lineno += len(t.value)
```

`t_ignore_COMMENT = r'//.*'` matches a single-line comment. `.` doesn't match `\n` by default, so `//` comments terminate at newline. The `tokens` entry `'COMMENT'` is unused — PLY doesn't require a token name for `t_ignore_*` rules; the name is just a PLY convention.

**To add block comments** (`/* ... */`):

```python
def t_COMMENT_BLOCK(t):
    r'/\*[^*]*\*+(?:[^/*][^*]*\*+)*/'
    t.lexer.lineno += t.value.count('\n')
    # Return None to discard the token entirely.
```

**Important:** use a function, not a variable. Variable `t_COMMENT_BLOCK` would *return* the token, which the parser doesn't expect. Return `None` (or don't return at all) to discard.

**To add Python-style `#` comments:** `t_ignore_HASH = r'#[^\n]*'`.

**To add nested block comments** (Rust-style): you need a stateful lexer with a state stack. PLY supports states via `states = (('comment', 'exclusive'),)` and `t_comment_*` rules. This is a bigger change; plan accordingly.

### 5.6 Error handling

```python
def t_error(t):
    print(f"Illegal character '{t.value[0]}'")
    t.lexer.skip(1)
```

Prints a message and skips one character. **This is a recover-and-continue strategy** — bad characters don't stop lexing, which means the parser may then produce cascading errors. For a REPL this is fine; for a compiler front end it's usually wrong.

**To fail fast:** raise `SyntaxError` from `t_error`. PLY will propagate it.

**To add line numbers to the message:** first make the lexer track lines (§5.5).

**To report a range instead of one char:** `t.value` is the remaining input from the illegal char to the end of the current match attempt; `t.value[0]` is just the first char. Print `t.value[:20]` or similar for context.

---

## 6. Lexer Construction

```python
lexer = lex.lex()
```

Built at import time.

**Options worth knowing:**
- `lex.lex(debug=True)` — prints every rule and its regex; useful when a rule "never fires".
- `lex.lex(reflags=re.UNICODE)` — enable Unicode in identifiers (identifiers would still be `[a-zA-Z_]` unless you also change `t_IDENT`).
- `lex.lex(optimize=True)` — writes `lextab.py`. Combined with `yacc.yacc(optimize=True)`, this speeds startup considerably in deployments.
- `lex.lex(module=obj)` — build against an object instead of the module namespace; useful for multiple lexer instances.

**After editing the file:** if `lextab.py` exists and you used `optimize=True`, delete it (or use `optimize=False` during development) to force regeneration. Stale tables are a classic source of "I changed the regex and nothing happened".

**Threading:** the `lexer` object is not thread-safe. `lex.lex()` returns a single instance that carries `.lexpos`, `.lineno`, and current state. If you compile in parallel, call `lexer.clone()` per thread.

---

## 7. Common Modification Recipes

### 7.1 Add a new operator

1. Add its name to the first `tokens` tuple (e.g. `'POW'`).
2. Add `t_POW = r'\*\*'` **before** `t_MUL = r'\*'` — actually, order among variables is by pattern length, so `**` (2 chars) beats `*` (1 char) regardless of position. But keeping the source order logical helps readers.
3. Add to `parser.precedence`.
4. Add to `parser.p_expression_binop` or a new production.
5. Add a codegen branch.

**Watch out:** if your operator's regex is a prefix of an existing one *and* the same length, ambiguity is real. E.g. adding `t_AT = r'@'` and `t_AT2 = r'@'` would be a conflict.

### 7.2 Add a new keyword

1. Add `'yourword': 'YOUR_TOKEN'` to `reserved`.
2. Add productions in `parser.py` using `YOUR_TOKEN`.
3. Handle the new AST node in `codegen.py`.

That's it — no changes to `tokens` needed (it's rebuilt from `reserved.values()`).

### 7.3 Add a new numeric literal form

**Recommended approach: convert to functions.**

Replace the three variables with:

```python
def t_FLOAT_NUMBER(t):
    r'\b\d+\.\d+([eE][+-]?\d+)?\b|\b\d+[eE][+-]?\d+\b'
    return t

def t_BINARY_NUMBER(t):
    r'\b0[bB][01]+\b'
    return t

def t_OCTAL_NUMBER(t):
    r'\b0[oO][0-7]+\b'
    return t

def t_HEX_NUMBER(t):
    r'\b0[xX][0-9a-fA-F]+\b'
    return t

def t_NUMBER(t):
    r'\b\d+\b'
    return t
```

Functions are tried in file order, so put the more specific ones first. This removes all ambiguity and makes each form explicit.

**Then**, in `codegen.visit_LiteralNode`, make sure `int(value, 0)` handles the new prefixes (`0b`, `0o` are supported by Python's base-0 int parse).

### 7.4 Add escapes to strings

1. Change `t_STRING` to `r'"([^"\\]|\\.)*"'`.
2. Coordinate with `codegen._decode_string_literal` — it uses `unicode_escape`, which handles most C-style escapes but interprets `\x` as Latin-1 bytes, not UTF-8. For full correctness, decode manually (hex escapes, octal escapes, `\uXXXX`).

### 7.5 Add line/column tracking

1. Remove `\n` from `t_ignore`.
2. Add:
   ```python
   def t_newline(t):
       r'\n+'
       t.lexer.lineno += len(t.value)
   ```
3. In `t_error`, print `t.lineno`.
4. In `parser.p_error`, `p.lineno` will now be meaningful.

If you want columns, keep a `lexpos`-based computation: `t.lexpos - t.lexer.latest_newline_pos`.

### 7.6 Support block comments

```python
def t_BLOCK_COMMENT(t):
    r'/\*[^*]*\*+(?:[^/*][^*]*\*+)*/'
    t.lexer.lineno += t.value.count('\n')
    # No return -> discarded.
```

Place **before** `t_IDENT`? Function order matters. Actually, block comments start with `/`, which is also `t_DIV`. Functions are tried before variables, so defining `t_BLOCK_COMMENT` as a function guarantees it wins over `t_DIV`. **Yes — it must be a function, not a variable.**

### 7.7 Add a `#` preprocessor-style directive

If you want `#include "foo.h"` or `#define`:

```python
def t_DIRECTIVE(t):
    r'\#[^\n]*'
    # Parse t.value here or emit a dedicated token.
```

If you emit a token, add its name to `tokens` and produce `DIRECTIVE` tokens that the parser handles. If you discard, the preprocessor is being driven entirely outside the grammar.

### 7.8 Change identifier rules (Unicode)

```python
def t_IDENT(t):
    r'[^\W\d]\w*'
    t.type = reserved.get(t.value, 'IDENT')
    return t
```

This allows Unicode letters. Combine with `lex.lex(reflags=re.UNICODE)`. Note that `reserved` lookups are still ASCII-only, so Unicode keywords require Unicode keys.

---

## 8. Debugging Tips

- **See the token stream:**
  ```python
  from lexer import lexer
  lexer.input("let x: i32 = 42;")
  for tok in lexer:
      print(tok.type, repr(tok.value), tok.lineno, tok.lexpos)
  ```
- **Find which rule matched:** `lex.lex(debug=True)` prints every regex as it's compiled.
- **A rule "never fires":** usually a *function* rule earlier in the file matches first, or a *variable* with a longer pattern wins. Temporarily disable rules to bisect.
- **A rule fires too eagerly:** e.g. `t_LT` matching before `t_LE`. Since variables are sorted by pattern length, this means the two patterns have equal length — an impossible case for `LT` vs `LE`. Check that `t_LE`'s regex is `<=` and not `<` by accident.
- **Stale `lextab.py`:** delete it if changes don't take effect.
- **Silent token drops:** if `t_error` runs on a character you expected to match, the rule for that character isn't defined or isn't reachable (e.g. a literal `'<'` in `literals` would shadow `t_LT`).

---

## 9. Invariants To Preserve

1. **`tokens` must contain every name referenced by `t_*` and by `parser.py` productions.** PLY errors at build time if a token is missing.
2. **Reserved words are only matched by `t_IDENT`.** No other rule should produce keyword tokens.
3. **`t_IDENT` must be a function**, since it does the reserved lookup. Don't convert it to a variable.
4. **Longer operators must be defined with strictly longer regexes than their prefixes** (`<=` before `<`, `<<=` before `<<` and `<=`). Because PLY orders by pattern length, equal-length overlapping patterns are undefined behavior.
5. **`t_ignore` cannot contain a character also matched by a `t_*` rule.** PLY silently drops it before any rule sees it.
6. **Comments that should be discarded must be functions returning nothing**, or `t_ignore_*` variables. A `t_COMMENT = r'...'` variable would return tokens the parser doesn't expect.
7. **String tokens include their quotes.** This is relied on by `codegen._decode_string_literal`. Don't strip quotes here.
8. **Number tokens are strings.** `int(...)`/`float(...)` conversion is the parser/codegen's job.
9. **No line tracking unless `t_newline` exists.** `t.lineno` is only useful if `\n` is not in `t_ignore`.
10. **The lexer does not resolve types.** `i32`, `u64`, etc. are identifiers here.

---

## 10. Known Limitations / Areas for Improvement

- No line-number tracking — `t.lineno` never increments, so error messages lack line info.
- No column tracking.
- No block comments.
- No escape sequences inside strings (though codegen decodes them).
- No raw strings, no char literals.
- No binary/octal literals.
- No exponent-only floats (`1e10`).
- No digit separators (`1_000_000`).
- No numeric type suffixes (`42u`, `42i64`, `3.14f32`).
- No multi-line strings.
- Unused tokens defined (`INC`, `DEC`, `*_ASSIGN`, `ELLIPSIS`, `COMMENT`).
- `t_error` doesn't fail fast — it prints and continues, which can cascade.
- No support for Unicode identifiers or keywords.
- No lexer states, so context-sensitive tokens (e.g. keywords that only apply inside asm) aren't possible without parser-level handling.

If you extend the lexer, prefer:

1. **Functions over variables** for any rule with ambiguity, so order is explicit.
2. **Narrow regexes with `\b` boundaries** to avoid accidental prefix matches.
3. **Keeping the lexer dumb.** Semantic decisions belong to the parser or codegen.
4. **Preserving raw token text.** Downstream code already relies on it (`AsmOperandNode.constraint`, `LiteralNode.value`, string tokens).
5. **Adding new tokens to `tokens` only when they aren't reserved words.** Reserved words get their token names for free.