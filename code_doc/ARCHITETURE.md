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
2. **The parser is purely structural.** It builds a tree whose nodes carry *raw token text*. It performs no type checking, no name resolution, no constant folding, no desugaring beyond the bare minimum required to fit the grammar (`else if` → nested `IfNode`).
3. **The code generator owns all semantics.** Types, widths, coercions, calling conventions, LLVM lowering, and target-specific decisions live here. This is the only stage that knows LLVM exists.

The result is a pipeline where each stage has a single, well-defined contract with the next, and where the "interesting" logic is concentrated in the last stage where it's easiest to reason about in isolation.

---

## 2. File-by-File Responsibilities

### 2.1 `lexer.py`

**Role:** convert a character stream into a stream of typed tokens.

**Inputs:** raw source text (via `lexer.input(src)`).

**Outputs:** a sequence of `LexToken` objects, each with:
- `type` — one of the names in `tokens` (e.g. `NUMBER`, `IDENT`, `LET`, `PLUS`).
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

**Contract with the code generator:**
- All literal values are strings. The back end converts them.
- All type names are strings. The back end resolves them.
- `AsmOperandNode.constraint` includes its quotes.
- `else if` chains are represented as nested `IfNode`s (the else-branch of one `IfNode` may be another `IfNode`).
- Statement ordering matches source ordering exactly.

**Notable design decisions:**
- **No semantic nodes.** There is no `CastNode`, no `TypedExpr`, no `ResolvedCall`. Type-driven decisions are made in the back end on the fly.
- **Precedence is centralized.** Adding an operator requires touching both `precedence` and `p_expression_binop` (or `p_expression_unary`).
- **`else if` collapses structurally.** No dedicated `ElifNode` exists; the grammar produces nested `IfNode`s directly.
- **Types are recursive.** Arrays (`[N x T]`) nest; vectors (`<N x T>`) would too, if the grammar had a rule for them. The type grammar uses `IDENT` for both real type names and the `x` separator in array types, because `x` is not a keyword.
- **Blocks and expression statements are transparent.** `BlockNode` wraps a list of statements; a bare expression statement is just the expression node itself, not wrapped.

### 2.3 `codegen.py`

**Role:** lower the AST into LLVM IR.

**Inputs:** a `ProgramNode` (and, transitively, every node reachable from it).

**Outputs:** an `ir.Module`. Two public methods:
- `generate(node) → ir.Module` — raw IR.
- `generate_optimized_ir(node, opt_level) → str` — IR after LLVM's optimization pipeline (handles both the legacy `PassManagerBuilder` and the newer `create_pass_builder` API).

**Mechanism:** a visitor pattern. `generate()` dispatches on node class name to a `visit_<ClassName>` method. Anything unhandled falls through to `generic_visit`, which raises a clear error.

**Core state (per generator):**

| Field | Purpose |
|---|---|
| `module`, `context` | The LLVM module being built. |
| `builder`, `current_fn` | Active IRBuilder and function (set by `_define_function`, cleared on return). |
| `symbol_table`, `symbol_types` | Maps local names to `alloca` instructions and their pointee types. Reset per function. |
| `loop_break_stack`, `loop_continue_stack` | Targets for `break`/`continue`. LIFO, pushed/popped with `try/finally`. |
| `type_map` | Named primitives (`i32`, `float`, `ptr`, `string`, ...). |
| `struct_types`, `struct_fields` | Identified struct types and field indices. |
| `enum_values`, `enum_types`, `enum_underlying_types`, `enum_value_types` | Enum bookkeeping. |
| `pointer_pointees` | For `->` support: which local holds a pointer to which struct. |
| `string_constants` | Dedup cache for string literals. |

**Notable design decisions:**

- **Opaque pointers throughout.** Never assumes `PointerType.pointee`. Uses `pointer_pointees` and passes `source_etype=` to `builder.gep` when needed.
- **Signless integers.** LLVM integers carry no signedness; `_coerce` chooses `sext`/`zext`/`sitofp`/`uitofp`/`icmp_signed` at each call site. The current code defaults to **signed** for everything.
- **Pointers decay to `i64` in arithmetic and comparisons.** The language treats pointers as raw addresses; any pointer operand in a binary op is `ptrtoint`-converted to `i64` first. This is what makes `buf + offset` work.
- **Arrays are declared but not zero-stored.** A `let buf: [512 x i8] = 0;` reserves stack space but the initializer is not stored, because large aggregate stores lower to `memset`, which is unavailable in freestanding builds. Programs are expected to fill such buffers explicitly.
- **Three-pass program traversal.** `visit_ProgramNode` runs enums first, then structs, then function declarations, then function definitions. This ordering:
  - Lets struct fields reference enum types declared later.
  - Lets function signatures reference structs and enums declared later.
  - Enables mutual recursion between functions.
  - By-value struct fields still require the field's struct to be declared earlier (no forward-declaration of struct bodies).
- **Struct constructors are function-call-shaped.** `Point(1, 2)` parses as a `FnCallNode` whose name happens to be a struct; `visit_FnCallNode` detects this and emits a literal-struct `insertvalue` chain instead of a call.
- **Enum variants resolve through `MemberAccessNode`.** `PacketType.Tcp` parses as `MemberAccessNode(IdentifierNode("PacketType"), "Tcp")`; `visit_MemberAccessNode` special-cases this into an integer constant before falling back to real field access.
- **Inline asm is a real function call.** `visit_AsmBlockNode` builds an `InlineAsm` value with a synthesized function type (void / single type / literal struct for multiple outputs) and calls it through the builder.

---

## 3. Data Flow — One Concrete Example

To make the pipeline tangible, consider compiling:

```rust
def add(a: i32, b: i32) -> i32 {
    let sum: i32 = a + b;
    return sum;
}
```

### Stage 1 — Lexer

Produces (roughly):

```
DEF('def') IDENT('add') '(' IDENT('a') COLON IDENT('i32') COMMA
IDENT('b') COLON IDENT('i32') ')' ARROW IDENT('i32') '{'
LET('let') IDENT('sum') COLON IDENT('i32') ASSIGN
IDENT('a') PLUS IDENT('b') SEMI
RETURN('return') IDENT('sum') SEMI '}'
```

Every `value` is a string. No keyword is distinguished from an identifier except by `t.type`. `i32` is `IDENT`, not a reserved word.

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
      ReturnNode(IdentifierNode('sum'))
    ])
  )
])
```

No types resolved, no `+` semantics chosen, no idea that `sum` will become an `alloca`.

### Stage 3 — Codegen

`visit_ProgramNode` walks in three passes:

1. No enums, no structs.
2. `_declare_function(add)` creates `i32 @add(i32, i32)` in `module.globals`.
3. `_define_function(add)`:
   - Creates entry block.
   - For each parameter, allocates a slot and stores the incoming argument.
   - Sets `symbol_table['a']` and `symbol_table['b']` to those allocas.
   - Regenerates the body:
     - `visit_VarDeclNode` evaluates `a + b` (two loads, one `add i32`), coerces to `i32` (no-op), allocates `sum`, stores.
     - `visit_ReturnNode` loads `sum`, coerces to `i32` (no-op), emits `ret i32`.

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
  ret i32 %sum1
}
```

LLVM's `mem2reg` pass (run by `generate_optimized_ir`) collapses the allocas into SSA values, giving the expected tight form.

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

### Runtime coupling

There is a subtle runtime coupling: `codegen.py`'s dispatch is by **Python class name**. If you rename `FunctionDeclNode` to `FnDeclNode` in `parser.py` and forget to rename `visit_FunctionDeclNode` in `codegen.py`, the visitor silently falls through to `generic_visit` and raises a clear error — which is exactly the intended fail-loud behavior. Keeping the class names stable is therefore part of the public contract between the two files.

---

## 5. Cross-Cutting Design Choices

These decisions cut across all three files and are worth understanding as a whole.

### 5.1 Strings carry meaning, types are late-bound

`i32`, `Point`, `PacketType`, `float`, and `str` are all just strings flowing from the lexer to the code generator, where `_get_llvm_type` resolves them (via `type_map`, `struct_types`, `enum_underlying_types`, the `iN` regex, and the aggregate-shape regexes). This is why the language can add integer widths, new named types, and new aggregate shapes without touching the lexer or grammar.

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

### 5.5 Three-pass program ordering

Enums, then structs, then function signatures, then function bodies. Each pass unlocks the next:

- **Enums first** because they resolve to plain integers and have no dependencies.
- **Structs second** because their fields may reference enums, and later code (function signatures, variables) may reference structs by value.
- **Function declarations third** because their signatures may reference structs and enums, and because mutual recursion requires all signatures to exist before any body is generated.
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

### 5.7 Error handling philosophy

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
| Unsigned semantics | — | (probably a type tag) | `_coerce` + `visit_BinaryOpNode` branches |
| Short-circuit `&&` | — | — | restructure `visit_BinaryOpNode` to emit CFG |
| Globals | — | new top-level rule | `visit_GlobalDeclNode` + lookup in `visit_IdentifierNode` |
| Scope tracking | — | — | replace flat `symbol_table` with a stack; update `visit_BlockNode`, loop visitors |
| Source positions | `t_newline` + track `lineno` | thread `lineno` through every AST node | include in `CodeGenError` messages |
| Struct forward decls | — | — | split `visit_StructDeclNode` into "create shell" and "set body" passes |

The table is not exhaustive, but its shape is the point: **the earlier a change lives in the pipeline, the more files it touches.** Most extensions land entirely in `codegen.py`; the exceptions are new syntax, which by definition requires all three stages.

---

## 8. Invariants Across the Pipeline

These hold throughout the codebase. Breaking any of them silently breaks something downstream.

1. **Raw text is preserved.** Literals, string tokens, and asm constraints retain their original spelling through the lexer and parser. Interpretation is a codegen concern.
2. **Types are strings until codegen.** `i32`, `Point`, `[512 x i8]` are all strings in the AST.
3. **The parser is a pure tree builder.** No type checking, no name resolution, no desugaring beyond `else if` collapsing.
4. **Node class names are the dispatch key.** Renaming a node in `parser.py` requires renaming the corresponding `visit_<Name>` in `codegen.py`.
5. **Statement order is preserved.** `BlockNode.statements` and `ProgramNode.statements` follow source order.
6. **Opaque pointers in codegen.** Never inspect `.pointee`; use `pointer_pointees` and `source_etype=`.
7. **LLVM integers are signless.** Signedness is chosen at each conversion site, and currently always signed.
8. **Program passes are ordered enums → structs → function decls → function defs.**
9. **Loop stacks are LIFO and cleaned up with `try/finally`.**
10. **Terminated blocks are never appended to.** Always check `builder.block.is_terminated` before emitting a branch or instruction.

---

## 9. Reading Order

For someone new to the codebase, the recommended reading order is the pipeline order:

1. **`lexer.py`** — smallest file, establishes the vocabulary.
2. **`parser.py`'s AST node definitions** — read the class definitions first, before the grammar. They document the shape of the language.
3. **`parser.py`'s `precedence` tuple** — then the `p_*` functions, starting from `p_program` and following top-down.
4. **`codegen.py`'s `__init__`** — see what state is tracked. This is a map of the language's semantic concepts.
5. **`codegen.py`'s `visit_ProgramNode`** — the entry point and pass structure.
6. **`codegen.py`'s `_get_llvm_type`** — the type resolver, which is where the language's type vocabulary lives.
7. **`codegen.py`'s `_coerce`** — the conversion engine, which is where type semantics live.
8. **The `visit_*` methods** in the same order as the grammar rules.

Reading in this order means every concept is introduced at the layer where it first appears, and every subsequent file builds on the one before.

---

## 10. Summary

The compiler is a clean three-stage pipeline with a deliberately minimal lexer, a purely structural parser, and a semantics-heavy back end. The contracts between stages are narrow — tokens, AST, LLVM IR — and mostly defined by convention (raw text preservation, string-typed types) rather than by validation. This keeps each stage comprehensible in isolation at the cost of late error detection.

The design's real strength is that **most meaningful extensions live in a single file**. New types, new operators, new lowering strategies, new struct and enum representations, and new codegen features all land in `codegen.py`. Only genuinely new syntax requires touching all three stages, and even then the changes are localized: one regex in the lexer, one production in the parser, one visitor in the back end.

The design's real cost is that **errors are discovered late and without location information**. Both are consequences of the same choice — keep the front end dumb — and both are fixable without disturbing the overall architecture: add `t_newline` and thread `lineno` through AST nodes, and unify the three error surfaces into a single exception-based model. Neither requires rethinking the pipeline; both improve the developer experience substantially.

If you take one thing away from this guide: **the pipeline is the API**. Each stage's output is a stable, well-understood artifact — tokens, AST, LLVM IR — and any change to the language should be understood as a change to one of those three contracts, with the corresponding ripple effects through the stages that produce and consume it.