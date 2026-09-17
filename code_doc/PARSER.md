# `parser.py` — Grammar & AST Documentation

This document is a hacking guide to `parser.py`, the PLY (Python Lex-Yacc) parser that turns a token stream from `lexer.py` into the AST consumed by `codegen.py`. It explains the grammar, the AST node shapes, and — most importantly — how to modify each piece safely.

---

## 1. Overview

`parser.py` uses **PLY's LR parser** (`ply.yacc`). It imports `lexer`, `tokens`, and `reserved` from `lexer.py` and builds the parser at import time via `yacc.yacc()`.

The file has three logical sections:

1. **AST node class definitions** (`ASTNode` and its subclasses).
2. **Operator precedence** (`precedence` tuple).
3. **Grammar rules** (`p_*` functions) — one function per production group.

The parser produces `ProgramNode` as the root, which `codegen.py` consumes.

**Design principle:** the parser builds *structural* trees only. It does not resolve types, validate names, or decide widths. That work belongs to `codegen.py`. This keeps grammar rules small and lets the backend evolve independently.

---

## 2. AST Node Classes

Each node is a thin data holder. If you add a field, you must update:

1. The `__init__` of the node.
2. Every grammar rule that constructs it.
3. Every visitor in `codegen.py` that consumes it.

### 2.1 Top-level & declaration nodes

| Node | Fields | Notes |
|---|---|---|
| `ProgramNode` | `statements: list` | Root. |
| `FunctionDeclNode` | `name, params, return_type, body` | `params` is a list of `(name, type_str)`. `return_type` is a string (`"void"` when absent). |
| `VarDeclNode` | `is_mutable, name, var_type, value` | `is_mutable` is currently always `False` (see §6.1). `var_type` may be `None` for inferred types. |
| `TypedefNode` | `name, target_type` | `target_type` is the raw type string the alias points to; resolved lazily by codegen. |
| `StructDeclNode` | `name, fields` | `fields` is a list of `(name, type_str)`. |
| `EnumDeclNode` | `name, variants, underlying_type` | `variants` is a list of `(name, int_or_None)`. `underlying_type` is `None` or a type string. |

### 2.2 Statement nodes

| Node | Fields |
|---|---|
| `BlockNode` | `statements` |
| `IfNode` | `condition, then_branch, else_branch` (else may be `BlockNode`, `IfNode`, or `None`) |
| `WhileNode` | `condition, body` |
| `ForNode` | `init, condition, update, body` (each may be `None`) |
| `ReturnNode` | `expression` (may be `None`) |
| `BreakNode` / `ContinueNode` | — |

### 2.3 Inline assembly nodes

| Node | Fields |
|---|---|
| `AsmOperandNode` | `constraint` (raw string token, **still quoted**), `expression` (AST node) |
| `AsmBlockNode` | `is_volatile, template (str), outputs, inputs, clobbers` |

`AsmOperandNode.constraint` keeps its surrounding quotes because the lexer emits the raw token. `codegen.py` decodes it with `_decode_string_literal`. If you change that convention, update both files.

### 2.4 Expression nodes

| Node | Fields |
|---|---|
| `BinaryOpNode` | `left, op (str), right` |
| `UnaryOpNode` | `op (str), operand` |
| `LiteralNode` | `value (str), type ("literal")` |
| `IdentifierNode` | `name` |
| `FnCallNode` | `name, args` |
| `MemberAccessNode` | `value, member, through_pointer (bool)` |
| `CastNode` | `expression, target_type` |

**Note:** `LiteralNode.value` is always a **string** — the raw token text. Conversion to int/float/string happens in `codegen.visit_LiteralNode`. Keep that in mind when adding literal types.

**Note:** `FnCallNode.name` is a plain string, not an expression. This means method calls and function-pointer calls aren't representable. See §6.4.

**Note:** `CastNode` is produced by `expression AS cast_type` (see §4.11). It carries only `expression` and a raw type string; codegen resolves the target and dispatches to `_coerce`.

---

## 3. Operator Precedence

```python
precedence = (
    ('right', 'ASSIGN', 'ADD_ASSIGN', 'SUB_ASSIGN', 'MUL_ASSIGN', 'DIV_ASSIGN'),
    ('left', 'LOGICAL_OR'),
    ('left', 'LOGICAL_AND'),
    ('left', 'BIT_OR'),
    ('left', 'BIT_XOR'),
    ('left', 'BIT_AND'),
    ('left', 'EQ', 'NEQ'),
    ('left', 'LT', 'LE', 'GT', 'GE'),
    ('left', 'SHL', 'SHR'),
    ('left', 'PLUS', 'MINUS'),
    ('left', 'MUL', 'DIV', 'MOD'),
    ('left', 'AS'),
    ('right', 'UNARY', 'LOGICAL_NOT', 'BIT_NOT'),
    ('left', 'DOT', 'ARROW'),
)
```

**PLY orders precedence from lowest (first) to highest (last).** So `DOT`/`ARROW` bind tightest; assignment binds loosest. `AS` sits just above multiplicative operators, so `a * b as T` parses as `a * (b as T)` and `a as T * b` parses as `(a as T) * b` only via the cast_type ambiguity note in §4.11.

**To add a new operator:**
1. Add its token to `lexer.py` and `tokens`.
2. Add it to the appropriate row in `precedence`. If it's a new tier, insert a new tuple in the right position.
3. Add a production to `p_expression_binop` or `p_expression_unary`.
4. Handle it in `codegen.visit_BinaryOpNode` / `visit_UnaryOpNode`.

**Common mistake:** adding the token to `precedence` but not to any grammar rule (or vice versa). PLY silently accepts unused tokens; the operator will just never parse.

**Common mistake:** mis-ordering a new tier. For example, inserting `('left', 'POW')` *before* `MUL` gives it lower precedence than multiplication (wrong if you want math convention). Insert *after* `MUL` for typical `^` semantics.

---

## 4. Grammar Rules

Each `p_<name>` function is a production group. `p[0]` is the result; `p[1..n]` are the RHS symbols in order.

### 4.1 Program & statements

- `p_program`: wraps `statement_list` in `ProgramNode`. **Root production.**
- `p_statement_list`: right-growing list. `empty` produces `[]`.
- `p_statement`: the union of all statement forms. **To add a new statement form**, add its non-terminal to this list.

`p_statement` currently includes:

```
function_decl
var_decl SEMI
typedef_decl SEMI
struct_decl
enum_decl
if_statement
while_statement
for_statement
return_statement SEMI
break_statement SEMI
continue_statement SEMI
asm_statement SEMI
expr_statement SEMI
block
```

**Tip:** `p_statement` is where you declare new top-level-or-nested statement kinds. If your new construct is only valid at the top level (e.g. `import`), you'd instead add a dedicated rule to `p_program` or introduce a `top_level_statement` non-terminal.

### 4.2 Functions

```
function_decl : DEF IDENT '(' parameter_list ')' ARROW type block
              | DEF IDENT '(' parameter_list ')' block
```

**To add modifiers** (`pub`, `extern`, `inline`): extend the LHS with optional non-terminals (e.g. `function_decl : visibility_opt DEF IDENT ...`). Store the modifier on `FunctionDeclNode` as a new field and consume it in `codegen`.

**To add generics:** this grammar has no `<...>` parameter syntax. You'd introduce a `type_params` non-terminal and carry a `generics` field. Codegen would then need monomorphization or erase to pointer-passing — significant work.

### 4.3 Parameters

`p_parameter_list` / `p_parameter_list_nonempty` build a list of `(name, type_str)` tuples. `p_parameter` is `IDENT COLON type`.

**To add default values:** extend `p_parameter` to `IDENT COLON type ASSIGN expression` and store a 3-tuple or a small record. Codegen's `_declare_function` uses `zip(func.args, node.params)` and destructures into `(name, ptype)`, so it must be updated too.

**To add variadics** (`...`): add a token in the lexer and a production here; then decide how codegen lowers it (C varargs ABI or explicit slice pointer).

### 4.4 Typedefs

```
typedef_decl : KEYWORD_TYPEDEF IDENT ASSIGN type
```

Produces `TypedefNode(name, target_type)`. The target is a raw type string, resolved lazily by `codegen._get_llvm_type` whenever the alias name is used. This means a typedef may point to a struct/enum declared later in the source, and forward references through aliases work naturally.

**To add typedef validation at parse time:** add a check in `p_typedef_decl`. Codegen already raises on duplicate and circular typedefs, so parser-side checks are only for early diagnostics.

**To add parameterized aliases** (`typedef Vec<T> = ...`): would require a `type_params` non-terminal and substitution in codegen — significant work.

### 4.5 Variable declarations

```
var_decl : LET IDENT COLON type ASSIGN expression
         | LET IDENT ASSIGN expression
         | LET IDENT COLON type
```

**Note on the middle case:** the guard `p[3] == '='` tests the *token value*, not its type. This works because the lexer's `=` token has value `'='`. If you ever change the lexer's `ASSIGN` value, this rule breaks.

**To add `let mut`:** the lexer would need a `MUT` token, and this rule would gain alternatives. `VarDeclNode.is_mutable` already exists — set it to `True` there. Codegen currently ignores `is_mutable`; enforce it in `visit_VarDeclNode` / `visit_BinaryOpNode` (assignment) if you want it.

**To add `const`:** same approach — new token, new alternatives, new AST field.

### 4.6 Structs

```
struct_decl : KEYWORD_STRUCT IDENT '{' struct_field_list '}'
```

`struct_field_list` allows trailing empties (via the `empty` alternative) but **not** trailing commas. If you want trailing commas, add:

```
struct_field_list : struct_field_list COMMA struct_field
                  | struct_field_list COMMA
                  | struct_field
                  | empty
```

Do the same for `enum_variant_list`, `parameter_list`, and `arg_list` if you want uniform trailing-comma support.

**To add struct methods:** you'd need a `function_decl` alternative nested inside `struct_field_list`, storing methods on `StructDeclNode`. Codegen would need name mangling (`Struct.method`).

**To add generics:** `struct Foo<T> { ... }` requires a `type_params` non-terminal and monomorphization in codegen (or pointer erasure).

### 4.7 Enums

```
enum_decl : KEYWORD_ENUM IDENT '{' enum_variant_list '}'
          | KEYWORD_ENUM IDENT COLON type '{' enum_variant_list '}'
```

`p_enum_variant` accepts `IDENT` or `IDENT ASSIGN NUMBER`. The explicit value is parsed with `int(p[3], 0)` (base-0, so `0x`, `0o`, `0b` work).

**Note:** `NUMBER` is used here; if you want `Enum.A = 1 + 2`, you'd switch to `expression` and evaluate it in codegen (or require constant folding).

**To add payload variants** (Rust-style `Some(int)`): change `p_enum_variant` to accept an optional `'(' type_list ')'` and store `(name, explicit_value, payload_types)`. Codegen's enum handling currently only emits integers — you'd need to switch to a tagged union (struct with tag + union). This is a significant change touching `visit_EnumDeclNode`, `_get_llvm_type`, `visit_IdentifierNode`, and `_resolve_enum_member`.

### 4.8 Control flow

#### If / else

```
if_statement : IF expression block ELSE block
             | IF expression block ELSE if_statement
             | IF expression block
```

**Key trick:** `ELSE if_statement` produces an `IfNode` as the else-branch of the outer `IfNode`. Codegen's `visit_IfNode` handles this naturally by recursively calling `self.generate(node.else_branch)` — which dispatches to `visit_IfNode` again. **If you refactor `visit_IfNode` to assume `else_branch` is always a `BlockNode`, you break `else if` chains.**

**To add `elsif` as a distinct keyword:** add the token, add a production, and either desugar to nested `IfNode` in the parser or add a new node type.

#### While

```
while_statement : WHILE expression block
```

No `do-while` currently. To add one, add a `DO ... WHILE ...;` production and a new `DoWhileNode`, then a matching codegen visitor (body runs before condition).

#### For

```
for_statement : FOR '(' for_init SEMI for_condition SEMI for_update ')' block
```

Each of `for_init`, `for_condition`, `for_update` accepts `empty`, so `for(;;)` is valid. `for_init` also accepts `var_decl` (notably **without** its trailing `SEMI`, since the `SEMI` in the `for` production serves that role).

**Watch out:** if you add a new statement that requires a trailing `SEMI` in `p_statement`, also add it explicitly to `for_init` (without `SEMI`) if you want it usable as a `for` initializer.

#### Break / continue / return

Simple. `p_return_statement` allows bare `return;` (which produces `ReturnNode(None)`).

### 4.9 Inline assembly

The asm grammar builds a **dict** in `p_asm_field` (`{'template': ...}`, `{'outputs': [...]}`, etc.), which `p_asm_field_list` merges via `dict.update`. `p_asm_statement` then reads the dict.

**Key gotcha:** field order does not matter, but duplicate keys silently overwrite. If you want to reject duplicate `template:` etc., change `p_asm_field_list` from `update` to an explicit merge that raises on collision.

**Constraint strings keep their quotes.** `p_asm_operand` stores `p[1]` verbatim (a quoted string token). `codegen.visit_AsmOperandNode` decodes it. If you change lexer string handling, revisit both sides.

**To add new asm fields** (e.g. `options:`): add the keyword to the lexer, add a `KEYWORD_OPTIONS COLON ...` alternative to `p_asm_field`, add the key to the dict, and read it in `p_asm_statement` → `AsmBlockNode`. Then update `codegen.visit_AsmBlockNode` to consume it.

**To add memory operands** (`"=m"(x)`): the operand production already accepts any expression, so `x` parses fine. Codegen would need to pass the *address* instead of the *value* when the constraint is `m`-family. That's a codegen-only change.

### 4.10 Blocks and expression statements

- `p_block`: `'{' statement_list '}'` → `BlockNode`.
- `p_expr_statement`: any expression followed by a `SEMI` (from `p_statement`) becomes a statement. Its value is discarded.

**Note:** `p_expr_statement` has no dedicated node — it just returns the expression. Codegen's `visit_BinaryOpNode` (assignment), `visit_FnCallNode`, etc. handle it. If you want to reject bare expressions that aren't calls/assignments (like C compilers warn about), add a validation pass or wrap in a new `ExprStmtNode`.

### 4.11 Types

```
type : TYPE_FLOAT | TYPE_DOUBLE | TYPE_VOID | IDENT | IDENT MUL
type : '[' NUMBER IDENT type ']'    # array types
type : LT NUMBER IDENT type GT      # vector types
```

**Important:** `i32`, `i8`, `ptr`, `string`, `half`, `bfloat`, `fp128`, etc. are **not reserved words**. They lex as `IDENT` and are resolved by `codegen._get_llvm_type` (via `type_map` or the `iN` regex). This is deliberate: it lets the language add integer widths without touching the lexer.

**To add a new named type:** just add it to `codegen.type_map`. **No parser change needed.**

**To add a new *syntactic* type form** (e.g. `fn(int) -> int`, `&T` reference, `[T]` slice, `(A, B)` tuple):
1. Add a production to `p_type`.
2. Choose the string encoding (e.g. `"fn(i32)->i32"`).
3. Teach `codegen._get_llvm_type` to parse it.

**Array rule:** `'[' NUMBER IDENT type ']'` — the `x` in `[512 x i8]` lexes as `IDENT` because `x` isn't reserved. That's a hack; if you add `x` as a keyword elsewhere, this rule breaks. Consider replacing `IDENT` with a dedicated token or accepting `'x'` literal.

**Vector rule:** `<N x T>` mirrors the array rule, reusing `LT`/`GT`. Because `type` is only entered from fixed points (after `COLON`, after `ARROW`, or recursively as an array/vector element type) and never from inside `expression`, this doesn't create shift/reduce ambiguity with `LT`/`GT`'s use as comparison operators.

**Nested arrays and vectors:** `[4 x [4 x i32]]` and `<4 x <4 x float>>` parse because `type` is recursive on both branches. Codegen's `_get_llvm_type` already handles both via its `array_match` and `vector_match` regexes.

**Pointer types:** only one level of `*` is supported (`IDENT MUL`). `T**` does not parse. If you need multi-level pointers, either extend to `IDENT MUL MUL` or make a dedicated pointer production: `type : type MUL`. The latter is cleaner but interacts with the array/vector productions' ambiguity.

### 4.12 Cast expressions

```
expression : expression AS cast_type %prec AS
```

`cast_type` is a **self-contained duplicate of `type`** used only after `AS`. Reusing `type` itself here would make it reachable from inside `expression`, enlarging `FOLLOW(type)` to include every binary-operator token; since LALR states merge by grammar position, that would make `type -> IDENT .` also treat `MUL` as a possible lookahead in the plain declaration contexts (var decl, params, struct fields, …), where `MUL` is not actually meaningful.

Keeping `cast_type` separate confines the one resulting ambiguity to casts alone: with `x as T .` and a `MUL` lookahead, the parser can't tell whether `MUL` starts a pointer type (`x as T*`) or is multiplication following a finished cast (`x as T * y`); it defaults to shift (pointer type wins), so wrap the cast in parens when multiplication is intended: `(x as T) * y`.

`p_expression_cast` uses `%prec AS` because the rule's rightmost symbol is the nonterminal `cast_type`, which carries no precedence of its own; without `%prec`, PLY would fall back to no precedence for this production and could not resolve its shift/reduce interaction with the surrounding binary-operator rules the same way the `AS` entry in the precedence table intends.

`cast_type` supports the same shapes as `type`:

```
cast_type : TYPE_FLOAT | TYPE_DOUBLE | TYPE_VOID | IDENT | IDENT MUL
cast_type : '[' NUMBER IDENT cast_type ']'   # array
cast_type : LT NUMBER IDENT cast_type GT      # vector
```

**To add a new cast target shape:** extend both `p_type` and `p_cast_type` (and any recursive array/vector variants). They are intentionally kept in sync by duplication, not by sharing a non-terminal.

### 4.13 Expressions

`p_expression_binop` covers all binary operators in one production group — PLY uses `precedence` to resolve. **To add a new binary operator**, just add it to this list and to `precedence`.

`p_expression_unary` covers prefix operators. `%prec UNARY` forces the production to use the `UNARY` precedence level regardless of the actual token's precedence. `BIT_AND` (address-of) and `MUL` (deref) get `%prec UNARY` because those same tokens are also binary. `LOGICAL_NOT` and `BIT_NOT` are unambiguously prefix, so they don't need the override.

**To add postfix operators** (`++`, `--`, `?`):
- Add a production like `expression : expression PLUS_PLUS`.
- Decide precedence (usually higher than `MUL`/`DIV`).
- Add a new AST node or reuse `UnaryOpNode` with a flag.

**Note on `p_expression_group`:** parenthesized expressions are transparent — the AST contains no `ParenNode`. This means you can't recover the source's parenthesization in the AST. Fine for codegen; a problem if you want to pretty-print.

**Note on `p_expression_member`:** `DOT` and `ARROW` are collapsed into one production that sets `through_pointer`. Both left-associate, so `a.b.c` and `a->b->c` work, but mixed chains like `a.b->c` are also accepted (parsed left-to-right). Codegen's `_member_address` handles the `through_pointer` at each step.

### 4.14 Argument lists

`p_arg_list` / `p_arg_list_nonempty` — same pattern as parameter lists. `empty` for zero-arg calls.

### 4.15 `empty` and error handling

`p_empty` defines the `empty` non-terminal used everywhere. It has no value.

`p_error` prints the offending token and line number. It does **not** raise, so parsing continues after an error, which can produce cascading messages. To fail fast, raise `SyntaxError` inside `p_error`.

**To add error recovery** (e.g. resync at `;`): call `parser.errok()` and return from `p_error`. This requires access to the `parser` object, which exists at module scope after `yacc.yacc()` — but `p_error` is called during parser construction/run, so you'd typically reference a global set by a factory.

---

## 5. Parser Construction

```python
parser = yacc.yacc()
```

Built at import time. Options you might add:

- `yacc.yacc(debug=True)` — writes `parser.out` and `parsetab.py`. Use during development to see shift/reduce conflicts.
- `yacc.yacc(write_tables=False, debug=False)` — safer for read-only filesystems / packaged deployments.
- `yacc.yacc(module=...)` — build against a class instance for stateful parsers. Currently unused.

**Tip:** after editing the grammar, delete `parsetab.py` if PLY caches it, or pass `write_tables=False` to force regeneration. Stale tables cause mysterious behavior.

**Watching for conflicts:** run with `debug=True` and check `parser.out` for "conflict" messages. Shift/reduce conflicts usually indicate an ambiguous precedence. Reduce/reduce conflicts are almost always a real grammar bug.

**The `cast_type` split was introduced specifically to avoid one such conflict.** If you later see new conflicts after touching `p_type`, `p_cast_type`, or the `AS` precedence row, that's the first place to look.

---

## 6. Common Modification Recipes

### 6.1 Enable `let mut`

1. `lexer.py`: add `MUT` to `reserved` with value `'mut'`.
2. `parser.py`:
   ```
   var_decl : LET MUT IDENT COLON type ASSIGN expression
            | LET MUT IDENT ASSIGN expression
            | LET MUT IDENT COLON type
            | ...existing...
   ```
   Set `is_mutable=True` in the new alternatives.
3. `codegen.py`:
   - Track mutability per symbol (new dict `self.symbol_mutable`).
   - Reject assignment in `visit_BinaryOpNode` when the target is immutable.
   - Reset the dict in `_define_function`.

### 6.2 Add a new statement keyword

Example: `loop { ... }` (infinite loop).

1. Lexer: add `KEYWORD_LOOP` to `reserved`.
2. Parser:
   - New node `LoopNode(body)`.
   - Production `loop_statement : KEYWORD_LOOP block`.
   - Add `loop_statement` to `p_statement`'s list.
3. Codegen:
   - `visit_LoopNode`: create `body_bb` and `end_bb`, push both onto the loop stacks, branch to `body_bb`, generate body, branch back to `body_bb` (unless terminated), position at `end_bb`.

### 6.3 Add a new binary operator

Example: integer unsigned division `/u`.

1. Lexer: add the token (if it's a new symbol like `/u`, this is awkward; a cleaner design is a separate unsigned type system).
2. Parser: add to `p_expression_binop`'s list and to `precedence`.
3. Codegen: in `visit_BinaryOpNode`, add a branch before the signed arithmetic dispatch that uses `udiv`/`urem`/`lshr`/`icmp_unsigned`.

If signedness is meant to come from the *types*, no parser change is needed — just route based on a type tag in codegen.

### 6.4 Function-pointer calls

Currently `FnCallNode.name` is a string. To support `f(x)` where `f` is a variable:

1. Parser: change `p_expression_call` to accept `expression '(' arg_list ')'` and store either a string or an expression on `FnCallNode` (e.g. add a `callee` field; keep `name` for the common case).
2. Codegen: in `visit_FnCallNode`, if `callee` is an expression, load its value as a pointer, `bitcast`/`getelementptr` as needed, and use `builder.call(loaded_fn, args)`.

Alternatively, keep the string and add a separate node `IndirectCallNode(callee_expr, args)`.

### 6.5 Arrays with index operator

1. Parser: add `expression : expression '[' expression ']'`. Since `'['` isn't currently a token in expressions (it appears in types and asm), verify the lexer emits it.
2. Precedence: `'['` should be at the same tier as `DOT`/`ARROW` (postfix, tightest).
3. New AST node `IndexNode(array, index)`.
4. Codegen:
   - `visit_IndexNode` (rvalue): `_address_of(array)` → GEP with `[0, index]` → load.
   - Assignment path in `visit_BinaryOpNode`: handle `IndexNode` on the left, similar to member access.

### 6.6 Add `extern` functions

1. Parser: allow an optional `EXTERN` before `DEF`, or add a new production `extern_decl : EXTERN DEF IDENT '(' parameter_list ')' ARROW type SEMI`.
2. Add `is_extern` to `FunctionDeclNode`.
3. Codegen: in `visit_ProgramNode`, still declare the function; skip definition for `is_extern`.

### 6.7 Global variables

1. Parser: add a top-level production (or extend `p_statement` with a flag on the program) for `GLOBAL LET ...`. Because the language currently treats all `let` uniformly, the simplest path is: keep `var_decl`, but move it to a `top_level_statement` non-terminal that also produces a `GlobalDeclNode`.
2. Codegen: `visit_GlobalDeclNode` creates `ir.GlobalVariable`, and `visit_IdentifierNode`/`visit_BinaryOpNode` check `module.globals` before failing.

### 6.8 Tuples / multiple return values

1. Parser: add `expression : '(' expr_list ')'` for `(a, b)` tuples (requires distinguishing from `p_expression_group`, e.g. by requiring ≥2 elements). New `TupleNode`.
2. Codegen: literal structs, `insert_value`/`extract_value`.

This interacts with `p_expression_group` (single-element `(x)`). PLY will see a shift/reduce conflict; resolve by making the tuple production require a trailing comma (`(x,)`) or by using lookahead.

### 6.9 Add a new cast target shape

1. Parser: extend both `p_type` and `p_cast_type` (including the recursive `p_cast_type_array` / `p_cast_type_vector` variants) with the new shape.
2. Codegen: teach `_get_llvm_type` to parse the new encoding, and make sure `_coerce` handles the resulting `ir.Type` category.

Keep `p_type` and `p_cast_type` in sync by hand. They are duplicated on purpose (see §4.12) — do not merge them into a shared non-terminal without re-checking the `FOLLOW` set implications for `MUL`.

---

## 7. Debugging Tips

- **Dump the AST:**
  ```python
  from lexer import lexer
  from parser import parser
  ast = parser.parse(src, lexer=lexer)
  # inspect ast.statements, etc.
  ```
- **Trace parsing:** `parser.parse(src, lexer=lexer, debug=True)` prints every shift/reduce.
- **Find conflicts:** build with `yacc.yacc(debug=True)` and inspect `parser.out`.
- **Stale tables:** if the grammar changes but behavior doesn't, delete `parsetab.py`.
- **Token-name mismatches:** if a rule "never fires", verify the token name matches `tokens` exactly (case-sensitive). PLY silently ignores unknown token names in rule docstrings only if they aren't defined; misspellings become non-terminals and cause conflicts or errors at build time.
- **Cast ambiguity:** if `x as T * y` parses unexpectedly, that's the documented shift-toward-pointer-type behavior (§4.12). Wrap the cast in parens to force multiplication.

---

## 8. Invariants To Preserve

1. **`p_statement` is the union of all statement forms.** Add new statements there (or to a new `top_level_statement` if scope-restricted).
2. **`p_expression_binop` uses `precedence` for disambiguation.** Add operators to both.
3. **`%prec UNARY` for prefix operators that share a token with a binary form** (`&`, `*`). Forget it and you get shift/reduce conflicts.
4. **`%prec AS` on `p_expression_cast`.** The rightmost symbol is a nonterminal (`cast_type`); without the override, PLY has no precedence to apply.
5. **`p_cast_type` is a deliberate duplicate of `p_type`, not a shared non-terminal.** Merging them reintroduces the `FOLLOW(type)` ambiguity with `MUL` in declaration contexts. Keep the two in sync by hand.
6. **Type names are `IDENT`s**, not keywords — with the exception of `float`, `double`, `void`. Keep it that way; it's what lets codegen own the type map.
7. **`LiteralNode.value` is a raw string.** Don't convert in the parser.
8. **`AsmOperandNode.constraint` is a quoted token.** Decode in codegen.
9. **`else if` chains produce nested `IfNode`s** — do not flatten them in the parser; codegen relies on recursion.
10. **`for_init` accepts `var_decl` without `SEMI`** because the `for` production supplies it.
11. **The `empty` non-terminal exists globally.** Reuse it for optional slots rather than duplicating "list may be absent" logic.
12. **No semantic analysis happens here.** No type checking, no name resolution, no constant folding. Keep it that way.

---

## 9. Known Limitations / Areas for Improvement

- No `let mut` despite the AST field existing — `is_mutable` is always `False`.
- No compound assignment (`+=`, `-=`, ...) parsing, even though the lexer defines the tokens and they're in `precedence`. `p_expression_binop` doesn't include them.
- No array indexing (`arr[i]`).
- Only one level of pointer (`T*`, not `T**`).
- No function pointers, no closures, no generics.
- No module/import system.
- No `match`/`switch` statement.
- No `enum` payloads (tagged unions).
- Trailing commas are inconsistent across list-like productions.
- `p_error` doesn't recover or raise.
- Literal type discrimination is codegen's heuristic (`"." in value` → double), not the parser's.
- Cast syntax has a documented ambiguity with multiplication when the target type can be followed by `*` (`x as T * y`); the parser resolves it in favor of the pointer type. Parenthesize to disambiguate.

If you extend the language, prefer:

1. Adding a **new AST node** over overloading an existing one.
2. Adding a **new production group** over growing an existing rule.
3. Keeping the parser **purely structural**; push semantic decisions into `codegen.py`.
4. **Mirroring new type shapes in both `p_type` and `p_cast_type`** rather than sharing a non-terminal.