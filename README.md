# Bonobo

Bonobo is a small language that generates LLVM IR using Python and `llvmlite`.

This README focuses on the compiler's two low-level features:

- inline assembly blocks;
- LLVM types and conversions between types.

Every example below was compiled against the current `lexer.py` / `parser.py`
/ `codegen.py` while writing this document, so the LLVM IR shown is the
actual output, not illustrative pseudo-code.

## Requirements and usage

Use the project's virtual environment, since it contains the compatible
version of `llvmlite`:

```bash
source .venv/bin/activate
PYTHONPATH=src python src/main.py program.bon
```

The code generator also exposes `generate_optimized_ir(ast, opt_level)` for
LLVM optimization levels `0` through `3`. It returns verified optimized LLVM
IR; use a new `LLVMCodeGenerator` instance for each AST compilation.

To generate and compile an executable with Clang:

```bash
PYTHONPATH=src python src/main.py program.bon --compile
```

The compiler accepts functions in the form
`fn name(param: type) -> type { ... }`. The keyword `def` is also accepted
as a synonym for `fn` — both are valid ways to declare a function:

```bonobo
fn add(a: i32, b: i32) -> i32 {
    return a + b;
}
```

```bonobo
def add(a: i32, b: i32) -> i32 {
    return a + b;
}
```

These two snippets are equivalent alternatives, not code meant to be pasted
together: the compiler does not currently reject a duplicate top-level
function name (whether declared twice with `fn`, twice with `def`, or one of
each). Declaring `add` twice in the same program silently produces a single
LLVM function with two function bodies stacked inside it — the second one
unreachable and effectively dead code — instead of a compile error. Give
each function a distinct name.

A function without an explicit return type uses `void` and omits the `->`
clause entirely:

```bonobo
fn log_byte(value: i8) {
    // no "-> type" here: this function returns void
}
```

Line comments start with `//` and run to the end of the line.

## Inline assembly

Inline assembly follows the same constraint idea as LLVM/GCC. The block has
a `template`, output operands, input operands and clobbers. The keyword may
be written as `asm` or `asm volatile`, and the parenthesis may or may not be
preceded by a space — both are valid:

```bonobo
asm volatile(
    template: "add $1, $0",
    outputs: ["=r"(result)],
    inputs: ["r"(value)],
    clobbers: ["~{cc}"]
);
```

Each operand has the form:

```text
"constraint"(expression)
```

Lists may be empty. `volatile` is optional:

```bonobo
asm(
    template: "nop",
    outputs: [],
    inputs: [],
    clobbers: []
);
```

Constraint strings are forwarded as-is to LLVM, so besides generic
constraints like `r` you can also pin an operand to a specific hardware
register by naming it in braces, e.g. `"{rax}"`, `"{rdi}"`, `"={rax}"`:

```bonobo
asm volatile(
    template: "syscall",
    outputs: [],
    inputs: [
        "{rax}"(0),
        "{rdi}"(0),
        "{rsi}"(&buf),
        "{rdx}"(1)
    ],
    clobbers: ["~{rcx}", "~{r11}", "~{memory}"]
);
```

### Output operands

An output must use an existing variable and a constraint that starts with
`=` (optionally naming a specific register, e.g. `={rax}`). The backend
passes the variable's slot as the result of the inline operation and writes
the returned value back into that slot:

```bonobo
fn increment(value: i32) -> i32 {
    let result: i32 = value;
    asm volatile(
        template: "addl $1, $0",
        outputs: ["=r"(result)],
        inputs: ["0"(value)],
        clobbers: ["~{cc}"]
    );
    return result;
}
```

When there are multiple outputs, LLVM returns a `struct` literal from the
assembly and the backend extracts each field in the declared order:

```bonobo
def main() -> i32 {
    let low: i32 = 0;
    let high: i32 = 0;
    asm(
        template: "",
        outputs: ["=r"(low), "=r"(high)],
        inputs: [],
        clobbers: []
    );
    return low + high;
}
```

The variables `low` and `high` must be declared before the block, and their
types determine the LLVM types of the results.

### Input operands

For an input, the expression is evaluated and its LLVM-typed value is used
directly. The most common inputs are:

- an existing variable, loaded from its typed slot;
- an integer literal constant, used as-is;
- an address-of expression `&variable`, which yields a pointer to the
  variable's slot instead of its value.

Any other expression (`a + b`, a function call, a struct field access, ...)
is also accepted as an input: it is simply evaluated like any other
expression and the resulting value is fed to the asm call. Only a **bare
variable name** gets the special "load from its declared slot" treatment
described above — and note that a bare enum variant name (e.g. `Active`)
does **not** work as an asm input, because it is not a declared variable and
the operand lookup only knows how to resolve variables.

```bonobo
def main() -> i32 {
    let number: i64 = 42;
    let buf: i32 = 0;

    asm volatile(
        template: "syscall",
        outputs: [],
        inputs: [
            "{rax}"(0),      // syscall number, as a literal constant
            "{rdi}"(0),      // fd
            "{rsi}"(&buf),   // address of the variable, via &
            "{rdx}"(1)
        ],
        clobbers: ["~{rcx}", "~{r11}", "~{memory}"]
    );
    return 0;
}
```

The template and constraints are forwarded to `llvmlite.ir.InlineAsm`;
therefore register names and constraints must be valid for the target
architecture's LLVM/Clang backend.

### Clobbers and caveats

Clobbers are LLVM strings, e.g. `"~{rax}"`, `"~{rcx}"`, `"~{r11}"`,
`"~{memory}"` or `"~{cc}"`. The compiler does not validate whether the
template semantically matches the declared registers.

- Declare every register, flag or memory state altered by the assembly.
- Use `asm volatile` when the instruction cannot be removed or reordered.
- A `.template` string can contain the usual backslash escapes (`\n`, `\t`,
  `\\`, `\xNN`, ...); they are decoded before being forwarded to LLVM, so
  `"movb $$10, %al\n\tint $$0x80"` produces a real newline and tab in the
  emitted `asm` string.
- The one thing you cannot escape is a literal quote character: the lexer's
  string rule stops at the first unescaped `"`, so a `"` always ends the
  string — there is no way to put one inside a template or constraint.

## Structs and enums

Struct fields are declared by name and can be initialized positionally with
the struct name. Fields can be read or assigned with `.`. A typed pointer to a
struct can use `->`:

```bonobo
struct Point { x: i32, y: i32 }

enum Color { Red, Green = 4, Blue }

fn distance_x() -> i32 {
    let point: Point = Point(10, 20);
    point.x = Green;
    let point_ptr: Point* = &point;
    return point_ptr->x;
}
```

Enum variants are integer constants. Implicit values start at zero and then
increment; an explicit value resets the next implicit value (so `Blue`
above is `5`, one past the explicit `Green = 4`).

A struct field, function parameter, return type or `let` can freely use
another struct's name, an enum's name, or a pointer to either — the backend
resolves each one to its underlying LLVM type the same way it resolves
`i32` or `float`:

```bonobo
struct Task {
    id: i32,
    status: Color        // an enum used as a struct field's type
}

fn make_task(id: i32, c: Color) -> Task {
    return Task(id, c);   // struct passed/returned by value
}
```

Note that struct assignment and struct field assignment require an **exact**
type match — two structs with identical fields but different names (e.g.
`struct A { v: i32 }` and `struct B { v: i32 }`) are not interchangeable,
and assigning a `B` where an `A` is expected is a `CodeGenError`, the same
way it would be in C or Rust.

### Enum variant names are global, not per-enum

Bare variant names (`Green`, not `Color.Green`) are looked up in a single,
program-wide table so they can be used unqualified. Because of that, **two
different enums cannot share a variant name**, even if the enums themselves
are unrelated:

```bonobo
enum Color { Red, Green, Blue }
enum Status { Red }   // CodeGenError: Duplicate enum variant: Red
```

Use the qualified form (`Color.Red`) if you want to disambiguate, and avoid
reusing a variant name across enums in the same program.

### Choosing the enum's underlying type

By default, an enum's constants are `i32`, exactly like an untyped `let`. You
can pick a different native integer type explicitly by writing `: type`
between the enum's name and its body:

```bonobo
enum PacketType : i8 {
    Ping = 1,
    Pong = 2,
    Data = 3
}
```

`PacketType`'s variants are now genuine `i8` constants, and any parameter,
struct field, return type or `let` typed as `PacketType` gets an `i8` slot
instead of `i32`:

```bonobo
struct Packet {
    kind: PacketType,   // i8
    length: i64
}

fn kind_of(pt: PacketType) -> i8 {
    return pt;
}
```

Only integer types are valid here (`i1`, `i8`, `i16`, `i32`, `i64`, `i128`,
or an arbitrary width like `i24` — see the types table below); using a
non-integer type such as `enum Bad : double { X = 1 }` is a `CodeGenError`
at compile time, not a silent truncation.

When you omit `: type`, the enum behaves exactly as before this feature was
added — no changes are needed to existing code that declares enums without
one.

## LLVM types

The backend maps these names directly to `llvmlite.ir`:

| Bonobo/LLVM | LLVM type |
| --- | --- |
| `i1` | 1-bit integer |
| `i8` | 8-bit integer |
| `i16` | 16-bit integer |
| `i32` | 32-bit integer |
| `i64` | 64-bit integer |
| `i128` | 128-bit integer |
| `iN` (any other positive width, e.g. `i24`, `i512`) | arbitrary-width integer |
| `half` | 16-bit floating point |
| `float` | 32-bit floating point |
| `double` | 64-bit floating point |
| `bfloat`, `fp128`, `x86_fp80`, `ppc_fp128`, `x86_mmx`, `x86_amx` | extended LLVM formats, only if exposed by the installed `llvmlite` build (see below) |
| `ptr`, `string`, `str` | opaque pointer |
| `label`, `metadata`, `token` | special LLVM types, only if exposed by the installed `llvmlite` build |
| `void` | no value (only valid as a function return type) |
| the name of any declared `struct` | that struct's type |
| the name of any declared `enum` | that enum's underlying integer type (`i32` by default, or whatever follows `:` in its declaration) |

There is **no** `bool`, `_Bool`, `char`, `short`, `int`, `signed`, `unsigned`
or `long` alias in the current compiler, even though names like that appear
in some C-inspired examples online — writing `let n: int = 0;` fails with
`CodeGenError: Unknown type: int`. Use the explicit LLVM-style name instead
(`i1` for a boolean, `i32` for `int`, `i64` for `long`, and so on).

The extended float/label/metadata/token row depends on what your installed
`llvmlite` exposes on `llvmlite.ir` (e.g. `ir.BFloatType`, `ir.FP128Type`,
`ir.TokenType`); a type from that row that your build doesn't have is simply
absent from the type table and fails the same way an unknown name would.
You can check what your environment actually supports with:

```python
import llvmlite.ir as ir
for name in ("BFloatType", "FP128Type", "X86_FP80Type", "PPC_FP128Type",
             "MMXType", "AMXType", "TokenType"):
    print(name, hasattr(ir, name))
```

The backend also recognizes composite types in LLVM notation:

```text
[4 x i8]              ; array
<4 x i32>             ; vector
{i32, double}         ; struct literal (unnamed)
i8*                   ; legacy pointer, converted to an opaque pointer
```

Of these, **only the array form can currently be written directly** in a
`let`, parameter, field or return type, because the grammar has a dedicated
rule for it:

```bonobo
def main() -> i32 {
    let buf: [4 x i8];
    return 0;
}
```

Vectors (`<N x type>`) and unnamed struct literals (`{i32, double}`) are
recognized by the backend's type resolver, but the current grammar has no
rule that lets you type them out in source — `<` and `{` there already
belong to comparisons and blocks respectively. They're reachable only if
something builds that type string programmatically; there's no Bonobo
syntax for them yet.

**Array initializers are ignored, not zero-filled.** Writing
`let buf: [4 x i8] = 0;` parses and compiles, but the `= 0` is silently
dropped — no store is emitted, and `buf` starts as uninitialized stack
memory, exactly as if you had written `let buf: [4 x i8];` with no
initializer at all. This is intentional (a naive store of a large
zero-initialized array would get lowered to a `memset` call, which doesn't
link in a freestanding binary with no libc), but it's easy to misread as a
zeroed buffer. Fill it explicitly — e.g. through inline assembly or manual
byte stores — before reading from it.

## Type conversion

Conversions are inserted automatically whenever an expression is used in a
typed declaration, assignment, function argument, or return statement:

```bonobo
def main() -> i32 {
    let small: i8 = 7;
    let wide: i64 = small;
    let decimal: double = wide;
    let again: i32 = decimal;
    return again;
}
```

The backend picks the LLVM instruction according to the source and
destination types:

| Source | Destination | LLVM instruction |
| --- | --- | --- |
| smaller integer | larger signed integer | `sext` |
| smaller integer | larger unsigned integer | `zext` |
| larger integer | smaller integer | `trunc` |
| integer | signed floating point | `sitofp` |
| integer | unsigned floating point | `uitofp` |
| floating point | signed integer | `fptosi` |
| floating point | unsigned integer | `fptoui` |
| smaller float | larger float | `fpext` |
| larger float | smaller float | `fptrunc` |
| integer | pointer | `inttoptr` |
| pointer | integer | `ptrtoint` |
| pointer, same address space | pointer | `bitcast` |
| pointer, different address spaces | pointer | `addrspacecast` |

LLVM integers have no intrinsic signedness: `i32` does not say whether a
value is signed or unsigned. Because of that, the choice between
`sext`/`zext`, `sitofp`/`uitofp` and `fptosi`/`fptoui` has to come from the
compiler's context — ordinary code always goes through the signed path
(`sext`, `sitofp`, `fptosi`). The internal `_coerce_unsigned` API exists for
code paths that explicitly know a value is unsigned, but nothing in the
grammar currently lets you request it from source; it's a backend hook for
future unsigned-aware syntax, not something you can reach from Bonobo code
today.

Pointers convert to and from integers the same way as any other value used
in a typed declaration:

```bonobo
def main() -> i32 {
    let c: i64 = 4096;
    let p: ptr = c;      // i64 -> ptr, inttoptr
    let addr: i64 = p;   // ptr -> i64, ptrtoint
    return 0;
}
```

### Reinterpretation with `bitcast`

`bitcast` does not convert the value numerically; it just reinterprets the
same bits as another type of the same width. The backend offers `_bitcast`
to validate this rule before emitting the instruction:

```llvm
%as_float = bitcast i32 %value to float
```

This is different from:

```llvm
%as_float = sitofp i32 %value to float
```

In the first case, the bits are preserved. In the second, the integer
number is converted to a floating-point value.

Conversions between types that have no corresponding LLVM instruction are
rejected with `CodeGenError`; the compiler does not silently convert
structs, arrays, `void`, `metadata`, `token` or `label`. This also applies
between two *different* structs — even with identical field layouts, one
struct's type is never implicitly coerced into another's (see "Structs and
enums" above).

## Generated IR example

For a conversion from `i32` to `i64` and then to `double`, the expected
result includes instructions similar to:

```llvm
%wide = sext i32 %small to i64
%decimal = sitofp i64 %wide to double
```

The final IR can be inspected by running the compiler without `--compile`.
