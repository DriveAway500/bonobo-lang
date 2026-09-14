# Bonobo

Bonobo is a small language that generates LLVM IR using Python and `llvmlite`.

This README focuses on the compiler's two low-level features:

- inline assembly blocks;
- LLVM types and conversions between types.

## Requirements and usage

Use the project's virtual environment, since it contains the compatible
version of `llvmlite`:

```bash
source .venv/bin/activate
PYTHONPATH=src python src/main.py program.bon
```

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

def add(a: i32, b: i32) -> i32 {
    return a + b;
}
```

A function without an explicit return type uses `void` and omits the `->`
clause entirely:

```bonobo
fn print(msg: string, len: int) {
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
    outputs: ["={rax}"(n)],
    inputs: [
        "{rax}"(0),
        "{rdi}"(0),
        "{rsi}"(buf),
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
asm(
    template: "",
    outputs: ["=r"(low), "=r"(high)],
    inputs: [],
    clobbers: []
);
```

The variables `low` and `high` must be declared before the block, and their
types determine the LLVM types of the results.

### Input operands

For an input, the expression is evaluated and its LLVM-typed value is used
directly. Three kinds of expressions are accepted:

- an existing variable, loaded from its typed slot;
- an integer literal constant, used as-is;
- an address-of expression `&variable`, which yields a pointer to the
  variable's slot instead of its value.

```bonobo
let number: i64 = 42;
let buf: int = 0;

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
- The assembly must not return `void` while declaring outputs.
- The current string syntax has no escapes; a quote character ends the
  string.

## LLVM types

The backend maps these names directly to `llvmlite.ir`:

| Bonobo/LLVM | LLVM type |
| --- | --- |
| `bool`, `_Bool`, `i1` | 1-bit integer |
| `char`, `i8` | 8-bit integer |
| `short`, `i16` | 16-bit integer |
| `int`, `signed`, `unsigned`, `i32` | 32-bit integer |
| `long`, `i64` | 64-bit integer |
| `i128`, `iN` | arbitrary-width integer |
| `half` | 16-bit floating point |
| `float` | 32-bit floating point |
| `double` | 64-bit floating point |
| `fp128`, `x86_fp80`, `ppc_fp128` | extended LLVM float formats, when exposed by `llvmlite` |
| `ptr`, `string`, `str` | opaque pointer |
| `label`, `metadata`, `token` | special LLVM types, when exposed by `llvmlite` |

The backend also recognizes composite types in LLVM notation:

```text
[4 x i8]             ; array
<4 x i32>            ; vector
{i32, double}        ; struct literal
i8*                  ; legacy pointer, converted to an opaque pointer
```

In the current grammar, composite types using `[]`, `<>` or `{}` cannot yet
be written directly in declarations, because those characters belong to
other parser rules. They can already be resolved by the backend and are
intended for use by future grammar extensions.

## Type conversion

Conversions are inserted automatically whenever an expression is used in a
typed declaration, assignment, function argument, or return statement:

```bonobo
let small: i8 = 7;
let wide: i64 = small;
let decimal: double = wide;
let again: i32 = decimal;
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
compiler's context. The internal `_coerce_unsigned` API is available for
code paths that explicitly know a value is unsigned.

Pointers convert to and from integers the same way as any other value used
in a typed declaration:

```bonobo
let p: ptr = c;      // i64 -> ptr, inttoptr
let addr: i64 = p;   // ptr -> i64, ptrtoint
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
structs, arrays, `void`, `metadata`, `token` or `label`.

## Generated IR example

For a conversion from `i32` to `i64` and then to `double`, the expected
result includes instructions similar to:

```llvm
%wide = sext i32 %small to i64
%decimal = sitofp i64 %wide to double
```

The final IR can be inspected by running the compiler without `--compile`.
