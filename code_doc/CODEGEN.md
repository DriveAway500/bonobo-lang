# codegen.py — LLVM IR Code Generator Documentation

This document explains the structure and internals of `codegen.py`, the AST-to-LLVM-IR backend for the language. It is written primarily as a hacking guide: how to extend, modify, and safely touch each part of the file.

## 1. Overview

`codegen.py` walks the AST produced by `parser.py` and emits LLVM IR using `llvmlite.ir`. It supports:

- Functions (declaration + definition)
- Local variables (`let`)
- Primitive types (integers of arbitrary width, floats, pointers)
- Aggregates: structs, literal structs, arrays, vectors
- Enums (with optional custom integer backing type)
- Typedefs (type aliases resolved lazily)
- Control flow: `if / else`, `while`, `for`, `break`, `continue`, `return`
- Unary and binary operators with sign-aware / float-aware coercion
- Explicit casts (`expr as T`) reusing the same coercion rules
- Member access (`.` and `->`)
- Inline assembly (asm blocks with outputs, inputs, clobbers)
- String literals (via private global constants)
- Address-of (`&`) and dereference (`*`) as raw byte pointers
- An optimized IR pipeline (`generate_optimized_ir`) that runs LLVM's pass manager

### Design constraints worth knowing before you edit anything

- Opaque pointers only. Never assume `PointerType.pointee`. Track pointee types in `pointer_pointees` and pass `source_etype=` to `builder.gep` when needed.
- Signless LLVM integers. Signedness is chosen at the conversion site, not encoded in the type.
- No libc assumptions for freestanding output. Some optimizations (notably large aggregate stores becoming `memset`) are intentionally avoided in a couple of spots.
- Enums/structs/typedefs are pre-registered in `visit_ProgramNode` so forward references work.
- LLVM initialization is one-shot and version-tolerant. The legacy `llvm.initialize()` call is no longer made.

---

## 2. Entry points

| Method | Purpose |
| --- | --- |
| `initialize_llvm()` | One-shot module-level LLVM init. Calls `initialize_native_target()` and `initialize_native_asmprinter()`. Safe to call repeatedly. |
| `LLVMCodeGenerator.generate(node)` | Dispatch on node class → `visit_<ClassName>`. Returns the `ir.Module`. |
| `LLVMCodeGenerator.generate_optimized_ir(node, opt_level)` | Runs `generate`, parses/verifies, then runs LLVM's new pass-manager pipeline via `create_pass_builder`. Validates `opt_level` in `[0, 3]` and maps it to a speed level (capped at 2, the maximum supported by `create_pipeline_tuning_options`). |

> Rule: `generate_optimized_ir` consumes the module. Create a fresh `LLVMCodeGenerator` for each AST.

> Note: The legacy `PassManagerBuilder` API was removed from LLVM/llvmlite. Only the new pass-manager pipeline is used.

---

## 3. State layout (`__init__`)

| Attribute | Meaning | When to update |
| --- | --- | --- |
| `context`, `module` | LLVM context/module | Once per generator |
| `builder`, `current_fn` | Active IR builder + function | Set by `_define_function`; `None` outside |
| `symbol_table` | `name → ir.AllocaInstr` (locals/params) | `visit_VarDeclNode`, `_define_function` |
| `symbol_types` | `name → ir.Type` (the pointee type of the alloca) | Same places |
| `loop_break_stack`, `loop_continue_stack` | Basic blocks for `break`/`continue` | Pushed/popped by loop visitors |
| `type_map` | Named primitive types (`"i32" → ir.IntType(32)`, `"string" → ptr`, etc.) | When adding a first-class primitive |
| `ptr_type` | Shared opaque pointer type | Never replaced |
| `struct_types` | `struct name → ir.IdentifiedStructType` | `visit_StructDeclNode` |
| `struct_fields` | `struct name → {field → (index, type)}` | `visit_StructDeclNode` |
| `enum_values` | `Enum.variant → int` | `visit_EnumDeclNode` |
| `enum_types` | `enum name → {variant → int}` | `visit_EnumDeclNode` |
| `enum_underlying_types` | `enum name → ir.IntType` | `visit_EnumDeclNode` |
| `enum_value_types` | `Enum.variant → ir.IntType` | `visit_EnumDeclNode` |
| `pointer_pointees` | `var name → pointee struct type for ->` | `_define_function`, `visit_VarDeclNode` |
| `string_constants` | dedup cache for string globals | `visit_LiteralNode` |
| `type_aliases` | `NAME → raw target type string` for typedefs | `visit_TypedefNode` |

`_optional_llvm_type(name)` is a helper that returns `None` if the requested `llvmlite` type class doesn't exist in the installed version. Used in `type_map` to gracefully skip types (e.g. `HalfType`, `BFloatType`, `LabelType`, `MetaDataType`, `TokenType`, `MMXType`, `AMXType`) that may be absent depending on the llvmlite build. Add new optional types there rather than importing `getattr` everywhere.

---

## 4. Type System

### 4.1 Type lookup — `_get_llvm_type(type_str)`

Resolution order:

1. Alias expansion — repeatedly substitute while `type_str in self.type_aliases` (with a circular-typedef guard raising `CodeGenError`).
2. `type_map` (named primitives)
3. `struct_types`
4. `enum_underlying_types` (enums decay to their integer backing)
5. Trailing `*` → opaque pointer
6. `iN` regex (`i[1-9][0-9]*`) → `ir.IntType(N)` (any width, not just the ones in `type_map`)
7. `[N x T]` → array (recursive)
8. `<N x T>` → vector (recursive)
9. `{T1, T2, ...}` → literal struct (`_split_type_list` handles nesting)

To add a new primitive type: add an entry to `type_map` in `__init__`. If it's a struct-like thing, add a branch to `_get_llvm_type`.

To add a new syntactic type form (for example `fn(T) -> U` function pointers): add a regex branch to `_get_llvm_type`.

To add a new typedef target form: nothing extra is required — typedefs are resolved by alias substitution before the other branches.

### 4.2 Type categories

`_is_integer`, `_is_float`, `_is_pointer`, `_is_vector`, and `_type_category` classify types. `_float_rank` returns bit width for float ordering.

If you add a new float class (for example a hypothetical `TensorFloat32Type`), update:

- the `type_map` entry in `__init__`
- the tuple in `_is_float`
- the map in `_float_rank`

### 4.3 Constants

- `_zero(t)` — `0`/`null` for ints/floats/pointers.
- `_type_bit_width(t)` — width for integers, float rank for floats; `None` otherwise. Used by `_bitcast`.

### 4.4 Typedefs

`typedef NAME = <type>;` is stored in `type_aliases` as a raw string. Resolution is lazy, so a typedef may point to a struct/enum declared later in the source. `_get_llvm_type` expands aliases before any other lookup. Circular typedefs raise `CodeGenError`.

- To add typedef validation (for example restrict which targets are legal): do it in `visit_TypedefNode`.
- To add typedefs of aggregate shapes not covered by `_get_llvm_type`: add a branch to `_get_llvm_type`.

---

## 5. Conversions

`_coerce(value, target_type, signed=True)` is the central conversion helper. It:

- returns early if types match
- checks vector/scalar compatibility and vector length equality
- dispatches on `(source_category, target_category)`

The `conversion_map` dict at class scope is documentation only — the actual logic is in `_coerce`. Keep them in sync if you edit.

Helpers built on top:

- `_coerce_unsigned(v, t)` → `_coerce(..., signed=False)`
- `_bitcast(v, t)` — equal-width reinterpretation for scalars/vectors (used mainly for `int↔float` bitcast when you actually mean bits)
- `_as_bool(v)` — converts any first-class value to `i1` for conditions

`visit_CastNode` reuses `_coerce` for explicit `as T` casts, so a cast accepts exactly the conversions that are already legal implicitly (integer widen/narrow, int↔float, int↔pointer, pointer↔pointer), always signed.

When to touch this: if you introduce a new type category (for example aggregates as first-class values), extend `_coerce`'s dispatch and `conversion_map`.

---

## 6. Visitor dispatch

`generate(node)` computes `visit_<ClassName>` and falls back to `generic_visit`, which raises.

To support a new AST node:

1. Add a `visit_YourNode(self, node)` method.
2. Return an `ir.Value` if it's an expression; return `None` (and possibly terminate the current block) if it's a statement.
3. If it introduces a new scope-like thing (loop, conditional), remember `self.builder.block.is_terminated` before appending branches.

---

## 7. Per-node reference

### 7.1 `visit_ProgramNode`

Order of processing (this order matters — do not reorder casually):

1. Typedefs — registered first. They are pure name substitutions resolved lazily by `_get_llvm_type`, so registering all of them up front lets any later declaration reference an alias regardless of source order.
2. Enums — resolved to plain integers; registered before structs so struct fields can reference them.
3. Structs — bodies set here so by-value struct fields resolve.
4. Function declarations — creates `ir.Function` shells in `module.globals` (no body). Enables mutual recursion.
5. Function definitions — generates bodies.

To add a new top-level declaration (for example global constants, extern functions):

- decide if it must be registered before or after the five passes
- add a new loop or hook into an existing one

### 7.2 `visit_TypedefNode`

- Rejects duplicate typedef names (`CodeGenError`).
- Stores the raw target type string in `type_aliases[node.name]`.

> Note: The target string is not validated at registration time. Errors surface when `_get_llvm_type` first resolves the alias. This is intentional — it lets a typedef reference a struct/enum declared later.

### 7.3 `visit_StructDeclNode`

- Creates/reuses an `IdentifiedStructType` via `module.context.get_identified_type(name)`.
- Calls `set_body(*field_types)` once. LLVM doesn't allow redefining.
- Fills `struct_fields[name]`.

> Note: No forward-declaration support for by-value fields — declare inner structs first. Recursive structs would require pointer indirection.

### 7.4 `visit_EnumDeclNode`

- Picks `underlying_type` from `node.underlying_type` or defaults to `i32`.
- Validates it's an integer.
- Assigns values sequentially, honoring explicit values.
- Registers the qualified name `Enum.Variant` in `enum_values` and `enum_value_types`.
- Registers the bare variant name in `enum_types[enum_name]` (used only for duplicate detection scoped to that enum).

> Variants are intentionally not reachable by bare name. Two different enums may reuse the same variant name.

To change enum representation (for example tagged unions): this is the place. You would need to change `_get_llvm_type` for enum names, `visit_IdentifierNode` for variant loads, and `_resolve_enum_member`.

### 7.5 `_declare_function` / `_define_function`

`_declare_function` creates the `ir.Function` without a body. It is idempotent: re-declaration returns the existing global. Named arguments are assigned from `node.params`.

`_define_function`:

- appends an entry block and sets `builder`, `current_fn`
- resets per-function state: `symbol_table`, `symbol_types`, `pointer_pointees`
- allocates and stores each parameter (needed because parameters are SSA values and the language treats variables as mutable)
- records struct-pointer params in `pointer_pointees` for `->` support (when the param type ends in `*` and the base name is a known struct)
- emits a default return if the block isn't terminated
- clears `current_fn` / `builder`

> Gotcha: `_define_function` assumes the function was already declared in `visit_ProgramNode`. If you add a code path that defines functions directly, call `_declare_function` first.

### 7.6 `visit_BlockNode`

Iterates statements; skips them if the current block is terminated (dead code after `return`, `break`, or `continue`).

### 7.7 `visit_VarDeclNode`

- evaluates the initializer if present
- determines type: explicit annotation, else the initializer's type, else `i32`
- allocates
- if the value is an array-typed local, the initializer is a placeholder (typically `0`) used only to reserve stack space; it is not stored. Rationale: storing a large zero-initializer gets lowered to `memset`, which breaks freestanding builds. Keep this behavior unless you're willing to link libc.
- otherwise, coerces and stores
- registers the symbol and (if applicable) `pointer_pointees` for `T*` annotations or `&var` initializers

When adding a new storage qualifier (for example `const`, `static`): this is the place to change linkage/behavior. For true globals, you would instead declare a `GlobalVariable` and skip the alloca.

### 7.8 Inline asm — `visit_AsmOperandNode` + `visit_AsmBlockNode`

`visit_AsmOperandNode` returns `(constraint_token, value, target_var_name)`:

- output constraints (decoded, `"="`-prefixed) pass the alloca as the value
- input constraints load from the variable
- non-identifier expressions are just evaluated

`visit_AsmBlockNode`:

- collects output constraints/types/target names
- collects input constraints/values/types
- concatenates all constraints (outputs, inputs, clobbers) — order matters and must match `InlineAsm` expectations
- builds a return type: `void` for 0 outputs, single type for 1, literal struct for many
- constructs `ir.InlineAsm` and calls it
- stores results back to output allocas, using `extract_value` for multi-output

> Note on constraint decoding: `node.constraint` is a raw lexer token that includes quotes. Always run it through `_decode_string_literal` before inspecting prefixes. This was a past bug.

To add a new operand category (for example `"+r"` read-write, or memory operands): extend `AsmOperandNode` in the parser, then handle it here by adding to the appropriate list.

### 7.9 `visit_BinaryOpNode`

Structure of the method:

- Assignment (`=`) — three sub-cases:
  - `*(addr) = value`: `inttoptr + i8 store`
  - `member = value`: resolve field address via `_member_address`
  - `identifier = value`: plain store
  - assignment returns the stored value (useful in `for` updates, etc.)
- Pointer decay: either operand being a pointer is coerced to `i64` via `ptrtoint`. This matches the language's "pointers are raw addresses" convention. Change this if you introduce typed pointer arithmetic.
- Logical `&&` / `||`: converts to `i1` and uses `and` / `or`. No short-circuiting. If you need short-circuit, you'll have to emit branch/phi — the current visitor is value-based and cannot do it in-place.
- Floating arithmetic/comparison: widens to float or double (whichever participates), then dispatches. Note `%` uses `frem`.
- Integer arithmetic/comparison: picks the wider type, coerces, uses `sdiv`, `srem`, `ashr`, `icmp_signed`. Unsigned operations are not currently representable — if you add them, you'll need an AST signal (for example new operators or unsigned type tags).

To add a new operator:

- add it to the arithmetic and/or comparisons dicts
- or add a new branch before the float/int blocks

### 7.10 `visit_UnaryOpNode`

- `&x` → returns the alloca directly. Only identifiers are allowed.
- `*x` → `inttoptr` to byte pointer and `load i8`. See pointer-decay note above.
- `-x` → `fsub(0, x)` for floats, `neg` for ints.
- `!x` → `_as_bool(x) == 0`.
- `~x` → `not_` for ints.

To add `++` / `--`: decide whether it's prefix or postfix, then emit a load, an add/sub, a store, and return the appropriate old/new value. Remember `symbol_types[name]` for the load type.

### 7.11 `visit_CastNode`

Explicit `expr as T` cast. Generates the operand, resolves the target type via `_get_llvm_type`, and delegates to `_coerce`. Supports exactly the conversions that are already legal implicitly (integer widen/narrow, int↔float, int↔pointer, pointer↔pointer), always signed.

To support a distinct cast family (for example bitcasts, or unsigned casts): add a new AST node or a modifier on `CastNode`, and dispatch to `_bitcast` / `_coerce_unsigned` here.

### 7.12 `visit_LiteralNode`

- Strings → private global `ArrayType(i8, len+1)` with trailing NUL, deduped via `string_constants`. Returns a GEP to the first element (`i8*`).
- Floats → heuristic: contains `.` or `e` → `DoubleType`. Fragile; if the lexer can produce float tokens distinctly, prefer that.
- Integers → `i32`. `int(value, 0)` allows `0x`, `0o`, `0b`.

To add a suffix like `u32` or `f32` literal types: the AST would need to carry them; then this visitor should produce constants with the right type instead of hardcoding `i32` / `DoubleType`.

### 7.13 `visit_IdentifierNode`

- If not a local, raise `CodeGenError`. Enum variants are intentionally not resolved here: they are namespaced under their enum and must be reached through it (e.g. `Color.Red`, never bare `Red`).
- Otherwise load from the alloca using `symbol_types[name]` as the type.

To add global variables: check for globals after locals and before enums.

### 7.14 `visit_FnCallNode`

Two behaviors:

1. Struct constructor call — if `node.name` matches a struct type, builds a zero-initializer and inserts each coerced field value. This is how `Point(1, 2)` works.
2. Real call — checks arity, coerces each argument, calls the function.

When the callee is a function pointer, this currently fails because `module.globals.get(name)` won't find non-globals. You would extend this to also accept an expression for the callee.

### 7.15 Member access

Three helpers:

- `_address_of(node)` — returns `(ptr, type)` for an lvalue. Handles `IdentifierNode` and nested `MemberAccessNode`.
- `_member_address(node)` — handles `.` and `->`, computes GEP.
  - for `->`: requires `pointer_pointees[name]`, loads the pointer, uses the pointee struct type
  - uses `source_etype` when the base pointer is opaque (llvmlite's modern API)
- `visit_MemberAccessNode` — first tries enum variant resolution, else loads via `_member_address`

Key invariant: every GEP has exactly two indices: `[0, field_index]`. The `0` steps through the pointer; the field index selects the field.

To add arrays as members: `_member_address` returns the array type; the caller (`visit_MemberAccessNode`) currently does `load`, which is fine for arrays in LLVM only as aggregates. If you index arrays, add a `visit_IndexNode` that uses `_address_of + GEP`.

To add nested pointers (for example `p->q->r`): the `through_pointer` branch handles `IdentifierNode` and `&` of an identifier only. For chains, you'd need to recursively resolve the left side as an lvalue.

### 7.16 `visit_ReturnNode`

- enforces void/non-void consistency
- coerces the return value to the declared return type

### 7.17 `visit_BreakNode` / `visit_ContinueNode`

Branch to the top of the corresponding stack. Raise if the stack is empty (that is, outside a loop).

### 7.18 `visit_IfNode`

Standard CFG:

```text
cond -> then_bb or else_bb-or-merge_bb
then -> merge
else -> merge
merge continues
```

Critical detail: after generating the body, the code checks `self.builder.block.is_terminated`, not `then_bb.is_terminated`. This is because nested control flow (nested `if` / `while`) leaves the builder in an inner merge block, not the block we created. Preserve this pattern in any new control-flow visitor.

### 7.19 `visit_WhileNode`

```text
entry -> cond
cond  -> body | end
body  -> cond
end   ->
```

`loop_break_stack` / `loop_continue_stack` are pushed/popped with `try/finally`, so even exceptions restore state.

### 7.20 `visit_ForNode`

```text
[init]
-> cond -> body | end
  body -> update
  update -> cond
  end
```

Note: `continue` targets `update_bb`, not `cond_bb` — that's the correct semantic for `for` loops. A null condition defaults to true. A null `init` is skipped before the entry branch; a null `update` is skipped in the update block (which still branches back to `cond`).

---

## 8. Common modification recipes

### Add a new binary operator

- `visit_BinaryOpNode`: add it to the arithmetic or comparisons dicts for the relevant type branch. For a new operation category, add a dedicated branch before the float/int dispatch.
- If it's unsigned, you need a way to distinguish it from signed variants (new AST field, separate node, or new operator token).

### Add a new primitive type

- add a `type_map` entry in `__init__`
- if float-like, update `_is_float` and `_float_rank`
- if you want implicit coercion with existing types, extend `_coerce`

### Add a new aggregate shape

- extend `_get_llvm_type` with a new regex branch
- extend `_split_type_list` if the new shape has its own bracket pair (currently `[]`, `{}`, `<>`)
- if it needs a constructor call, add a branch in `visit_FnCallNode`

### Add unsigned arithmetic

LLVM integers are signless, so the type doesn't distinguish. You need a signal in the AST. Options:

- new operator tokens (`u/`, `u<`, etc.)
- type annotations resolved at the expression level (`u32`, `u64`)

Then dispatch to `udiv`, `urem`, `lshr`, `icmp_unsigned`, and `zext`.

### Add short-circuit `&&` / `||`

Currently `visit_BinaryOpNode` returns a value directly, which cannot emit branches. To short-circuit, either:

- convert to a statement-like node that produces a `phi`, or
- introduce a helper that emits CFG and returns the `phi` value. You would need access to `self.current_fn` for new blocks.

### Add globals

- in `visit_ProgramNode`, before functions: declare `ir.GlobalVariable` for each top-level `let` with global `constant` / `linkage` as appropriate
- `visit_IdentifierNode`: check `module.globals` after `symbol_table` and before `enum_values`
- assignment in `visit_BinaryOpNode`: if the target is a global (not in `symbol_table`), emit a store to the global

### Change the default integer width

The default is hardcoded as `i32` in several places: `_get_llvm_type` fallback, `visit_LiteralNode`, `visit_VarDeclNode` fallback, `_member_address` GEP index type, and `visit_LiteralNode` GEP index. Introduce a helper like `_default_int()` and replace these call sites.

### Add struct forward declarations

Currently, `visit_StructDeclNode` calls `set_body` immediately, so a struct that references another must appear after it. To support forward declarations, you'd need:

- a pre-pass that creates `IdentifiedStructType` shells for all struct names
- a deferred `set_body` pass after all fields are resolvable

### Add a new typedef form

- `visit_TypedefNode`: change how the target is stored (currently a raw string). If you need validation at registration time, do it here.
- `_get_llvm_type`: the alias-expansion loop already handles any new target form as long as it can be resolved by one of the existing branches.

---

## 9. Debugging tips

- Dump raw IR: `print(str(gen.generate(ast)))`
- Dump optimized IR: `print(gen.generate_optimized_ir(ast, opt_level=2))`
- Verify explicitly: `llvm.parse_assembly(str(module)).verify()`
- If a `visit_*` method is missing, `generic_visit` raises a clear error naming the node type.
- When a branch seems not to occur, check `self.builder.block.is_terminated` — the pattern of "did the last statement already terminate this block?" recurs throughout control flow.
- For type resolution failures, check `type_aliases` first — an unresolved alias produces a clear "Unknown type" error, but a circular one produces "Circular typedef involving ...".

---

## 10. Invariants to preserve

- Opaque pointers. Never use `.pointee`. Use `pointer_pointees` and `source_etype=`.
- `set_body` once per struct. No redefinition.
- Terminated blocks aren't appended to. Always check before branching.
- `builder.block`, not the saved `bb`, when checking termination after nested control flow.
- Type coercion before store/call/return. Never store a raw value into a differently typed alloca.
- Per-function state reset in `_define_function` — `symbol_table`, `symbol_types`, `pointer_pointees`.
- Typedefs before enums before structs before functions in `visit_ProgramNode`.
- Loop stacks are LIFO and popped via `try/finally`.
- String globals are deduped — reuse `string_constants` keyed on the decoded text (including NUL).
- Array locals are not zero-stored unless you also accept a libc dependency.
- Typedef resolution is lazy and cycle-checked. Do not eagerly resolve aliases in `visit_TypedefNode`.
- `generate_optimized_ir` validates `opt_level` before consuming the module.

---

## 11. Known limitations / areas for improvement

- Signedness is hardcoded (signed) for all integer ops and comparisons.
- Pointer arithmetic decays to `i64` — no typed GEP-based arithmetic.
- No short-circuit `&&` / `||`.
- No index operator (`arr[i]`) visitor — arrays currently only participate as members or via asm.
- No function pointer calls.
- No global variable declarations.
- Float literal type selection is heuristic-based, not lexer-driven.
- Structs cannot be forward-declared or self-referential by value.
- Typedefs cannot be redefined and are not validated until first use.
- `visit_ForNode` requires the AST to expose `init`, `condition`, `update`, and `body`. If any is `None`, the code handles it, but a fully empty `for(;;)` still emits `cond = true`.

> If you extend the language, prefer adding a visitor method rather than mutating existing ones, and prefer extending `_coerce` over ad-hoc `bitcast` / `inttoptr` calls scattered through visitors.