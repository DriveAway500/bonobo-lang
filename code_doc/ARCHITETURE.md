# Compiler Architecture Guide

**A complete walkthrough of the `lexer.py` → `parser.py` → `codegen.py` pipeline**

This document describes how the three source files fit together as a compiler front end and back end. It is written to be read top-to-bottom as a single narrative: what each stage receives, what it produces, why it was designed that way, and where the seams are. It is **not** a modification guide — for that, see the per-file documentation (`LEXER.md`, `PARSER.md`, `CODEGEN.md`).

---

## 1. Executive Summary

The compiler is a classic three-stage pipeline:

```
source text
   │
   ▼
┌──────────┐     tokens      ┌──────────┐      AST       ┌───────────┐    LLVM IR
│ lexer.py │ ──────────────▶ │ parser.py│ ─────────────▶ │ codegen.py│ ───────────▶ .bc / .o
└──────────┘                 └──────────┘                └───────────┘
   PLY lex                     PLY yacc                    llvmlite.ir
```

Three deliberate design choices shape everything else:

1. **The lexer is dumb.** It recognizes token *shapes* and keyword *spellings*, nothing more. It does not track line numbers, does not decode strings, does not distinguish `i32`-the-type from `i32`-the-identifier.
2. **The parser is purely structural.** It builds a tree whose nodes carry *raw token text*. It performs no type checking, no name resolution, no constant folding, no desugaring beyond the bare minimum required to fit the grammar (`else if` → nested `IfNode`). The one structural duplication it carries — `cast_type` mirroring `type` — exists purely to keep the LALR grammar unambiguous, not to add semantics.
3. **The code generator owns all semantics.** Types, widths, coercions, calling conventions, LLVM lowering, and target-specific decisions live here. This is the only stage that knows LLVM exists.

The result is a pipeline where each stage has a single, well-defined contract with the next, and where the "interesting" logic is concentrated in the last stage where it's easiest to reason about in isolation.

---

## 2. File-by-File Responsibilities

### 2.1 `lexer.py`

**Role:** convert a character stream into a stream of typed tokens.

**Inputs:** raw source text (via `lexer.input(src)`).

**Outputs:** a sequence of `LexToken` objects, each with:
- `type` — one of the names in `tokens` (e.g. `NUMBER`, `IDENT`, `LET`, `PLUS`, `AS`).
- `value` — the **raw matched text** (a string, always).
- `lineno`, `lexpos` — position info (line is currently never advanced; see §6.1).

**Mechanism:** PLY compiles all `t_*` definitions into a master regular expression. Function rules (`def t_X(t)`) are tried first, in file order. Variable rules (`t_X = r'...'`) are tried afterward, sorted by decreasing pattern length. `literals` (single characters like `(`, `)`, `{`, `}`) are handled without regexes.

**Contract with the parser:**
- Keyword tokens are produced by re-typing identifiers via the `reserved` dict inside `t_IDENT`.
- String tokens include their surrounding quotes.
- Numeric tokens are strings, not numbers.
- Comments and whitespace are silently discarded (`t_ignore`, `t_ignore_COMMENT`).
- Illegal characters are reported and skipped (see §6.1).

**Notable design decisions:**
- **Sized integer types are not keywords.** `i32`, `i64`, `ptr`, `string`, `half`, `bfloat`, `fp128`, etc. lex as `IDENT`. Only `float`, `double`, `void` are reserved, because the parser's `p_type` rule names them explicitly. This means new integer widths can be supported by the back end alone, without touching the lexer or grammar.
- **`as` is a reserved word**, not an operator. It must be lexed by `t_IDENT` (via `reserved`) so the parser can distinguish the cast keyword from an identifier. It is therefore not in the operator block and not in `literals`.
- **Multi-character operators are ordered implicitly by regex length.** `<=` is longer than `<`, so PLY tries `LE` before `LT`; the same applies to `<<`/`<=`/`<`, `&&`/`&`, and so on. This is convenient but fragile if two operators ever have equal-length patterns.
- **The token list is assembled in two parts:** the base tuple of non-keyword tokens, plus `tuple(set(reserved.values()))`. Adding a keyword only requires editing `reserved`; adding an operator requires editing the base tuple.

### 2.2 `parser.py`

**Role:** convert a token stream into an Abstract Syntax Tree.

**Inputs:** tokens from `lexer.py` (PLY couples them via `parser.parse(src, lexer=lexer)`).

**Outputs:** a `ProgramNode` — the root of the AST — whose `statements` list contains every top-level declaration and statement in source order.

**Mechanism:** PLY's LR parser, driven by `precedence` (operator binding powers) and `p_*` functions (one per production group). Left/right associativity and precedence tiers are declared once and shared by all binary/unary operator rules.

**AST shape:**

```
ProgramNode
├── FunctionDeclNode(name, params, return_type, body: BlockNode)
├── TypedefNode(name, target_type)
├── StructDeclNode(name, fields: [(name, type_str)])
├── EnumDeclNode(name, variants: [(name, int|None)], underlying_type)
├── VarDeclNode(is_mutable, name, var_type, value)
├── IfNode(condition, then_branch, else_branch)
├── WhileNode(condition, body)
├── ForNode(init, condition, update, body)
├── ReturnNode(expression)
├── BreakNode / ContinueNode
├── AsmBlockNode(is_volatile, template, outputs, inputs, clobbers)
└── <expression> (bare expression statement)
```

Expression nodes:
- `BinaryOpNode(left, op, right)`
- `UnaryOpNode(op, operand)`
- `LiteralNode(value, type)` — `value` is always a **string** (raw token text).
- `IdentifierNode(name)`
- `FnCallNode(name, args)` — `name` is a string, not an expression.
- `MemberAccessNode(value, member, through_pointer)` — `.` and `->` collapse into one node with a flag.
- `CastNode(expression, target_type)` — `target_type` is a raw string.

**Contract with the code generator:**
- All literal values are strings. The back end converts them.
- All type names are strings. The back end resolves them.
- `AsmOperandNode.constraint` includes its quotes.
- `else if` chains are represented as nested `IfNode`s (the else-branch of one `IfNode` may be another `IfNode`).
- Statement ordering matches source ordering exactly.

**Notable design decisions:**
- **No semantic nodes.** There is no `TypedExpr`, no `ResolvedCall`, no desugaring of casts into `_coerce` calls. Type-driven decisions are made in the back end on the fly. `CastNode` is purely syntactic: it records "the user wrote `expr as T`" and nothing more.
- **Precedence is centralized.** Adding an operator requires touching both `precedence` and `p_expression_binop` (or `p_expression_unary`).
- **`else if` collapses structurally.** No dedicated `ElifNode` exists; the grammar produces nested `IfNode`s directly.
- **Types are recursive, and so is `cast_type`.** Arrays (`[N x T]`) and vectors (`<N x T>`) nest on both sides. The two type non-terminals are duplicated on purpose: sharing `type` between declarations and casts would enlarge `FOLLOW(type)` and reintroduce a `MUL` ambiguity in declaration contexts. See §5.8.
- **Blocks and expression statements are transparent.** `BlockNode` wraps a list of statements; a bare expression statement is just the expression node itself, not wrapped.
- **`%prec` is used to resolve token-sharing ambiguities.** Prefix `&` and `*` need `%prec UNARY` because those tokens are also binary; `as` needs `%prec AS` because the rule's rightmost symbol is a nonterminal with no inherent precedence.

### 2.3 `codegen.py`

**Role:** lower the AST into LLVM IR.

**Inputs:** a `ProgramNode` (and, transitively, every node reachable from it).

**Outputs:** an `ir.Module`. Two public methods:
- `generate(node) → ir.Module` — raw IR.
- `generate_optimized_ir(node, opt_level) → str` — IR after LLVM's optimization pipeline (validates `opt_level ∈ [0, 3]`, maps it to the new pass-manager API's speed level, and uses only the modern `create_pass_builder` pipeline — the legacy `PassManagerBuilder` API is no longer supported).

**Mechanism:** a visitor pattern. `generate()` dispatches on node class name to a `visit_<ClassName>` method. Anything unhandled falls through to `generic_visit`, which raises a clear error.

**Core state (per generator):**

| Field | Purpose |
|---|---|
| `module`, `context` | The LLVM module being built. |
| `builder`, `current_fn` | Active IRBuilder and function (set by `_define_function`, cleared on return). |
| `symbol_table`, `symbol_types` | Maps local names to `alloca` instructions and their pointee types. Reset per function. |
| `loop_break_stack`, `loop_continue_stack` | Targets for `break`/`continue`. LIFO, pushed/popped with `try/finally`. |
| `type_map` | Named primitives (`i32`, `float`, `ptr`, `string`, ...). Some entries are populated only if the installed `llvmlite` exposes the corresponding `ir` type class (`HalfType`, `BFloatType`, `MMXType`, `AMXType`, `LabelType`, `MetaDataType`, `TokenType`, ...); missing ones are filtered out. |
| `struct_types`, `struct_fields` | Identified struct types and field indices. |
| `enum_values`, `enum_types`, `enum_underlying_types`, `enum_value_types` | Enum bookkeeping. |
| `pointer_pointees` | For `->` support: which local holds a pointer to which struct. |
| `string_constants` | Dedup cache for string literals. |
| `type_aliases` | `NAME → raw target type string` for typedefs; resolved lazily by `_get_llvm_type`. |

**Notable design decisions:**

- **Opaque pointers throughout.** Never assumes `PointerType.pointee`. Uses `pointer_pointees` and passes `source_etype=` to `builder.gep` when needed.
- **Signless integers.** LLVM integers carry no signedness; `_coerce` chooses `sext`/`zext`/`sitofp`/`uitofp`/`icmp_signed` at each call site. The current code defaults to **signed** for everything.
- **Pointers decay to `i64` in arithmetic and comparisons.** The language treats pointers as raw addresses; any pointer operand in a binary op is `ptrtoint`-converted to `i64` first. This is what makes `buf + offset` work.
- **Arrays are declared but not zero-stored.** A `let buf: [512 x i8] = 0;` reserves stack space but the initializer is not stored, because large aggregate stores lower to `memset`, which is unavailable in freestanding builds. Programs are expected to fill such buffers explicitly. Vectors follow the same rule for the same reason.
- **Four-pass program traversal.** `visit_ProgramNode` runs typedefs first, then enums, then structs, then function declarations, then function definitions. This ordering:
  - Lets any later declaration reference a typedef regardless of source order (typedefs are pure name substitutions resolved lazily).
  - Lets struct fields reference enum types declared later.
  - Lets function signatures reference structs and enums declared later.
  - Enables mutual recursion between functions.
  - By-value struct fields still require the field's struct to be declared earlier (no forward-declaration of struct bodies).
- **Struct constructors are function-call-shaped.** `Point(1, 2)` parses as a `FnCallNode` whose name happens to be a struct; `visit_FnCallNode` detects this and emits a literal-struct `insertvalue` chain instead of a call.
- **Enum variants resolve through `MemberAccessNode`.** `PacketType.Tcp` parses as `MemberAccessNode(IdentifierNode("PacketType"), "Tcp")`; `visit_MemberAccessNode` special-cases this into an integer constant before falling back to real field access. Bare variant names (`Tcp` alone) are intentionally rejected — variants are always namespaced by their enum.
- **Inline asm is a real function call.** `visit_AsmBlockNode` builds an `InlineAsm` value with a synthesized function type (void / single type / literal struct for multiple outputs) and calls it through the builder. Output constraints pass the *alloca*; input constraints pass the *loaded value*. Constraint decoding goes through `_decode_string_literal` because the parser keeps the quotes.
- **Explicit casts reuse `_coerce`.** `visit_CastNode` is a thin wrapper: generate the operand, resolve the target via `_get_llvm_type`, delegate to the same coercion engine that already backs var initializers, assignments, call arguments, and returns. This means a cast supports exactly the conversions that are already legal implicitly — no more, no less — and stays consistent with the rest of the language by construction.

---

## 3. Data Flow — One Concrete Example

To make the pipeline tangible, consider compiling:

```rust
def add(a: i32, b: i32) -> i32 {
    let sum: i32 = a + b;
    return sum as i64 as i32;
}
```

### Stage 1 — Lexer

Produces (roughly):

```
DEF('def') IDENT('add') '(' IDENT('a') COLON IDENT('i32') COMMA
IDENT('b') COLON IDENT('i32') ')' ARROW IDENT('i32') '{'
LET('let') IDENT('sum') COLON IDENT('i32') ASSIGN
IDENT('a') PLUS IDENT('b') SEMI
RETURN('return') IDENT('sum') AS('as') IDENT('i64') AS('as') IDENT('i32') SEMI '}'
```

Every `value` is a string. No keyword is distinguished from an identifier except by `t.type`. `i32` and `i64` are `IDENT`, not reserved words. `as` is `AS` because it is in `reserved`.

### Stage 2 — Parser

Builds:

```
ProgramNode([
  FunctionDeclNode(
    name='add',
    params=[('a','i32'), ('b','i32')],
    return_type='i32',
    body=BlockNode([
      VarDeclNode(is_mutable=False, name='sum', var_type='i32',
                  value=BinaryOpNode(
                    left=IdentifierNode('a'),
                    op='+',
                    right=IdentifierNode('b'))),
      ReturnNode(
        CastNode(
          expression=CastNode(
            expression=IdentifierNode('sum'),
            target_type='i64'),
          target_type='i32'))
    ])
  )
])
```

No types resolved, no `+` semantics chosen, no idea that `sum` will become an `alloca`. The two `CastNode`s are pure syntax: "the user wrote `sum as i64` and then `... as i32`".

### Stage 3 — Codegen

`visit_ProgramNode` walks in four passes:

1. No typedefs.
2. No enums, no structs.
3. `_declare_function(add)` creates `i32 @add(i32, i32)` in `module.globals`.
4. `_define_function(add)`:
   - Creates entry block.
   - For each parameter, allocates a slot and stores the incoming argument.
   - Sets `symbol_table['a']` and `symbol_table['b']` to those allocas.
   - Regenerates the body:
     - `visit_VarDeclNode` evaluates `a + b` (two loads, one `add i32`), coerces to `i32` (no-op), allocates `sum`, stores.
     - `visit_ReturnNode` evaluates the outer `CastNode`:
       - `visit_CastNode` (inner) generates `sum` (load `i32`), resolves target `i64`, calls `_coerce(loaded_i32, i64)` → `sext i32 to i64`.
       - `visit_CastNode` (outer) resolves target `i32`, calls `_coerce(sext_result, i32)` → `trunc i64 to i32`.
     - `visit_ReturnNode` then coerces the final `i32` to the declared return type `i32` (no-op) and emits `ret i32`.

Result (unoptimized):

```llvm
define i32 @add(i32 %0, i32 %1) {
entry:
  %a = alloca i32
  %b = alloca i32
  store i32 %0, ptr %a
  store i32 %1, ptr %b
  %a1 = load i32, ptr %a
  %b1 = load i32, ptr %b
  %addtmp = add i32 %a1, %b1
  %sum = alloca i32
  store i32 %addtmp, ptr %sum
  %sum1 = load i32, ptr %sum
  %sext = sext i32 %sum1 to i64
  %trunc = trunc i64 %sext to i32
  ret i32 %trunc
}
```

LLVM's `mem2reg` and `instcombine` passes (run by `generate_optimized_ir`) collapse the allocas into SSA values and fold `sext`→`trunc` back into the original `i32`, giving the expected tight form.

---

## 4. Dependency Graph

The three files form a strict DAG. There are no cycles.

```
lexer.py ─────────────▶ parser.py ─────────────▶ codegen.py
   │                        │                        │
   │                        │                        └─▶ llvmlite.ir
   │                        └─▶ ply.yacc              └─▶ llvmlite.binding
   └─▶ ply.lex
```

### Import-level dependencies

- `parser.py` imports `lexer`, `tokens`, and `reserved` from `lexer.py`.
- `codegen.py` imports AST node classes **directly from `parser.py`** — `ProgramNode`, `FunctionDeclNode`, and so on. This is why `codegen.py`'s visitor methods are named after the exact class names defined in `parser.py`.
- Neither `lexer.py` nor `parser.py` imports anything from `codegen.py`. The back end is a pure sink.
- `codegen.py` imports `llvmlite.binding` (for initialization and optimization) and `llvmlite.ir` (for building).

### Contract-level dependencies

The contracts are one-directional and can be summarized as:

| Producer | Consumer | Contract |
|---|---|---|
| `lexer.py` | `parser.py` | Token stream with raw text values; keyword re-typing via `reserved`. |
| `parser.py` | `codegen.py` | AST with string-typed literals, string-typed type names, and the specific node shapes listed in §2.2. |
| `codegen.py` | LLVM (via llvmlite) | Opaque-pointer IR, signless integer semantics chosen at each conversion site. |

**Any change to a contract on the producer side must be reflected on the consumer side.** The most common failure modes:

- Adding a field to an AST node without updating `codegen.py` → `AttributeError` at generation time.
- Renaming a keyword token in `lexer.reserved` without updating `parser.py` → the parser's rule for that keyword never fires; PLY may report a build error if the token name is unreferenced, or silently mis-parse.
- Changing string-literal conventions (e.g. stripping quotes in the lexer) without updating `codegen._decode_string_literal` → double-decoding or `IndexError` on `value[1:-1]`.
- Renaming `cast_type` to reuse `type` in `p_expression_cast` without re-checking the `FOLLOW` set implications → new shift/reduce conflicts in declaration contexts. See §5.8.

### Runtime coupling

There is a subtle runtime coupling: `codegen.py`'s dispatch is by **Python class name**. If you rename `FunctionDeclNode` to `FnDeclNode` in `parser.py` and forget to rename `visit_FunctionDeclNode` in `codegen.py`, the visitor silently falls through to `generic_visit` and raises a clear error — which is exactly the intended fail-loud behavior. Keeping the class names stable is therefore part of the public contract between the two files.

---

## 5. Cross-Cutting Design Choices

These decisions cut across all three files and are worth understanding as a whole.

### 5.1 Strings carry meaning, types are late-bound

`i32`, `Point`, `PacketType`, `float`, and `str` are all just strings flowing from the lexer to the code generator, where `_get_llvm_type` resolves them (via `type_aliases` expansion, `type_map`, `struct_types`, `enum_underlying_types`, the `iN` regex, and the aggregate-shape regexes). This is why the language can add integer widths, new named types, new aggregate shapes, and new type aliases without touching the lexer or grammar.

**Cost:** no early error detection. `let x: bogus;` parses fine and only fails at codegen, where the message is `Unknown type: bogus` with no line number.

### 5.2 Scope is not modeled explicitly

There are no lexically nested scopes. `symbol_table` is per-function and flat. A variable declared inside an `if` branch remains visible in the merge block and after it. This is a deliberate simplification: it matches the freestanding, single-file style the language targets, and it avoids the complexity of scope-chain lookups in the code generator.

**Consequence:** shadowing is an error (duplicate name in `symbol_table` overwrites silently, but the previous alloca stays allocated — wasted stack but not incorrect).

**Consequence:** `for`-loop induction variables leak into the enclosing scope after the loop.

### 5.3 Pointers are integers

The language has two views of pointers:

- **Typed access via `->`:** for struct pointers only, when the pointee type is tracked in `pointer_pointees`.
- **Raw address via `&` / `*`:** `&x` yields the alloca pointer; `*p` treats `p` as a byte address and loads/stores a single `i8`.

There is no pointer arithmetic on typed pointers. `buf + n` decays both sides to `i64` and adds. This is the same model C has if you cast everything to `uintptr_t` before touching it, and it's what makes syscalls and memory-mapped I/O straightforward.

**Cost:** the type system does not prevent you from `*`-dereferencing an unrelated integer, nor from storing a struct pointer into an `i64` and losing track of its pointee type. This is by design — the language targets systems programming where this is expected.

### 5.4 The signless-integer problem

LLVM `i32` is neither signed nor unsigned. The front end doesn't tag signedness, so the back end picks **signed** semantics everywhere: `sdiv`, `srem`, `ashr`, `icmp_signed`, `sitofp`, `sext`. This means:

- `>>` is an arithmetic shift, not logical.
- `%` is a signed remainder, not unsigned modulo.
- Comparisons treat the high bit as a sign bit.
- Widening uses sign extension.

Adding unsigned semantics requires a signal in the AST (a `u32`-like type tag, or a `/u`-style operator) and corresponding branches in `_coerce` and `visit_BinaryOpNode`. This is a front-end change, not a back-end one.

### 5.5 Four-pass program ordering

Typedefs, then enums, then structs, then function signatures, then function bodies. Each pass unlocks the next:

- **Typedefs first** because they are pure name substitutions resolved lazily by `_get_llvm_type`; registering all of them up front lets any later declaration (enum, struct, function, variable) reference an alias regardless of source order.
- **Enums next** because they resolve to plain integers and have no dependencies on other declarations.
- **Structs third** because their fields may reference enums or typedefs, and later code (function signatures, variables) may reference structs by value.
- **Function declarations fourth** because their signatures may reference structs and enums, and because mutual recursion requires all signatures to exist before any body is generated.
- **Function bodies last** because they may call any previously declared function.

The only remaining gap is **by-value self-reference or forward reference between structs**. There is no pre-declaration pass for struct bodies, so `struct A { b: B }` requires `struct B` to be declared earlier in the file.

### 5.6 Inline asm as a first-class construct

Inline asm is not a builtin function or a special-cased call; it's a distinct AST node (`AsmBlockNode`) that lowers to an `ir.InlineAsm` value, called through the builder. The pieces:

- **Template** — the raw assembly string.
- **Outputs** — a list of `AsmOperandNode`, each with a constraint and an expression. When the expression is an identifier, the code generator passes the **alloca** for output constraints (`"=..."`) and the **loaded value** for input constraints.
- **Inputs** — same node shape, always loaded.
- **Clobbers** — a list of strings (e.g. `"~{rcx}"`).

The constraint string handed to LLVM is the concatenation of all three lists, in the order outputs → inputs → clobbers. This order is required by LLVM's `InlineAsm` API and is why `AsmBlockNode` stores the three lists separately rather than as one merged list.

Multi-output asm blocks return a literal struct, and the code generator extracts each field with `extractvalue` and stores it back to the corresponding variable's alloca. Single-output blocks return the value directly. Zero-output blocks are `void`.

One subtlety: the constraint token from the parser still carries its surrounding quotes (e.g. `"=r"` including the `"` characters). The code generator decodes it with `_decode_string_literal` before checking the `=` prefix. Skipping that decode step is a known past bug and a common footgun when extending the asm path.

### 5.7 Explicit casts are syntactic sugar for coercion

The `expr as T` syntax exists at the parser level as a distinct node (`CastNode`), but the code generator treats it as a thin wrapper over `_coerce`. That means:

- A cast never introduces a new conversion path. It reuses exactly the conversions that implicit coercions already support: integer widen/narrow, int↔float, int↔pointer, pointer↔pointer, always signed.
- A cast never bypasses type checking. If `_coerce` would reject `T1 → T2`, so does `x as T2` when `x : T1`.
- Adding a new legal conversion (say, unsigned widen) automatically makes it usable from casts and from implicit sites at the same time.

**Cost:** you cannot express a bitcast via `as` unless the source and target are already considered "coercible" by `_coerce`. If you want `as` to also cover bitcasts, you need a separate syntax or a modifier on `CastNode`, because `_coerce` and `_bitcast` are deliberately distinct operations.

### 5.8 `type` vs `cast_type`: two copies of the same shape, on purpose

The parser has two non-terminals for type syntax: `type` (used in declarations, parameters, struct fields, typedef targets, and `enum : T`) and `cast_type` (used only after `as`). They accept the same shapes — primitives, identifiers, one level of `*`, arrays, vectors — but they are defined separately.

The reason is LALR lookahead. If `p_expression_cast` reused `type`, then `type` would be reachable from inside `expression`, and `FOLLOW(type)` would grow to include every binary-operator token. Because LALR states merge by grammar position, that would make `type -> IDENT .` also consider `MUL` a valid lookahead in declaration contexts — where `MUL` is not actually meaningful — producing spurious conflicts.

Keeping `cast_type` separate confines the one resulting ambiguity to casts alone: with `x as T .` and a `MUL` lookahead, the parser cannot tell whether `MUL` begins a pointer type (`x as T*`) or is multiplication following a finished cast (`x as T * y`). It defaults to shift (pointer type wins). The practical rule is to wrap the cast in parens when multiplication is intended: `(x as T) * y`.

**Cost:** `p_type` and `p_cast_type` must be kept in sync by hand. Every new type shape needs both rules updated, including the recursive array/vector variants. This is a deliberate trade: a small amount of duplication in exchange for not having to re-derive the entire LALR conflict set when a new type form is added.

### 5.9 Error handling philosophy

Three separate error surfaces:

- `t_error` in the lexer: prints a message and **skips one character**, continuing.
- `p_error` in the parser: prints a message and **continues parsing**, which can produce cascading errors.
- `CodeGenError` in the back end: raised, propagates up, and **aborts generation**.

This asymmetry reflects different maturity levels of the three stages: the lexer and parser were built for interactive use (where continuing is helpful), while the back end needs to fail loudly to avoid emitting invalid IR. Unifying these into a single exception-based error model is a natural next step.

---

## 6. Known Architectural Gaps

These are not bugs; they are consequences of the design that would need structural changes to address.

### 6.1 No source position tracking

Tokens carry `lineno`, but `\n` is in `t_ignore`, so `lineno` never increments. AST nodes carry no position at all. Error messages therefore identify problems by kind, not by location.

**Impact:** once the language grows past toy examples, this becomes the single biggest usability problem. Fixing it requires a `t_newline` function in the lexer, propagating `lineno` into AST nodes (parser change), and formatting it in error messages (back-end change).

### 6.2 No scope modeling

Flat per-function `symbol_table`. See §5.2.

### 6.3 No short-circuit evaluation

`&&` and `||` lower to a single `and`/`or` instruction on `i1` values. Both operands are always evaluated. Fixing this requires emitting CFG in the middle of an expression, which the current value-based visitor pattern doesn't support without introducing `phi` nodes and restructuring the binary-op visitor.

### 6.4 No unsigned semantics

See §5.4.

### 6.5 No struct forward declarations

See §5.5.

### 6.6 No globals

Top-level `let` is not currently parsed as a distinct construct; every variable lives in a function's stack frame. Adding globals requires:

- A new grammar rule (or a flag on `VarDeclNode`) to distinguish top-level from local.
- A `visit_GlobalDeclNode` that emits `ir.GlobalVariable`.
- A check in `visit_IdentifierNode` and `visit_BinaryOpNode` that consults `module.globals` after `symbol_table`.

### 6.7 No `let mut` enforcement

`VarDeclNode.is_mutable` exists but is always `False`, and codegen ignores it. The language currently allows assigning to any variable. Adding mutation checking requires a `symbol_mutable` dict per function and a guard in the assignment branch of `visit_BinaryOpNode`.

### 6.8 Pointer arithmetic is untyped

All pointer arithmetic decays to `i64`, so the language can't express "advance by one `T`". This is consistent with the "pointers are integers" model, but it means the type system never helps with unit errors.

### 6.9 The `else` branch of `IfNode` is polymorphic

It may be `None`, a `BlockNode`, or another `IfNode`. The code generator handles all three by recursing via `self.generate(node.else_branch)`. Any refactor of `visit_IfNode` must preserve this — it's the mechanism by which `else if` chains work.

### 6.10 Enums are pure integers

There is no tagged-union representation and no payload variants. `PacketType.Tcp` is exactly `1`. Adding payload variants would change the enum lowering from "integer constant" to "struct with tag + union", touching `_get_llvm_type`, `visit_EnumDeclNode`, `visit_IdentifierNode`, `_resolve_enum_member`, and `visit_MemberAccessNode` simultaneously.

### 6.11 Typedefs are unvalidated at registration time

`visit_TypedefNode` stores the raw target string without resolving it. Errors (unknown target, circular chain) surface only when the alias is first used by `_get_llvm_type`. This is intentional — it lets a typedef point to a struct or enum declared later — but it means a typo in an unused typedef is never reported.

---

## 7. Extensibility Seams

Where each kind of change belongs, and what it forces.

| Change | Lexer | Parser | Codegen |
|---|---|---|---|
| New primitive type (e.g. `bool`) | — | — | `type_map` entry |
| New sized integer (`i256`) | — | — | — (iN regex handles it) |
| New operator (`**`) | new token | new precedence + production | new `visit_BinaryOpNode` branch |
| New keyword (`match`) | `reserved` entry | new production + `p_statement` | new visitor |
| New statement form | (maybe) | new node + production | new visitor + loop/scope bookkeeping |
| New aggregate shape (`<N x T>` vector) | — | new `p_type` production | new `_get_llvm_type` branch |
| New literal form (`0b`) | new regex or function rule | — | `visit_LiteralNode` needs `int(x, 0)` support |
| New typedef target form | — | mirror in `p_type` and `p_cast_type` | extend `_get_llvm_type` |
| New cast target shape | — | mirror in both type non-terminals | ensure `_coerce` accepts the resulting category |
| Unsigned semantics | — | (probably a type tag) | `_coerce` + `visit_BinaryOpNode` branches |
| Short-circuit `&&` | — | — | restructure `visit_BinaryOpNode` to emit CFG |
| Globals | — | new top-level rule | `visit_GlobalDeclNode` + lookup in `visit_IdentifierNode` |
| Scope tracking | — | — | replace flat `symbol_table` with a stack; update `visit_BlockNode`, loop visitors |
| Source positions | `t_newline` + track `lineno` | thread `lineno` through every AST node | include in `CodeGenError` messages |
| Struct forward decls | — | — | split `visit_StructDeclNode` into "create shell" and "set body" passes |

The table is not exhaustive, but its shape is the point: **the earlier a change lives in the pipeline, the more files it touches.** Most extensions land entirely in `codegen.py`; the exceptions are new syntax