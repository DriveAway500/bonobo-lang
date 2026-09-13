"""
templates.py — Trechos de assembly (NASM, x86-64, Linux) usados pelo codegen.

Manter os templates separados da lógica em codegen.py facilita ajustar o
assembly gerado (ex.: trocar convenção de chamada, mudar syscalls) sem mexer
no percurso da AST.
"""

# Convenção de chamada (System V AMD64): primeiros 6 args inteiros/ponteiro
ARG_REGISTERS = ['rdi', 'rsi', 'rdx', 'rcx', 'r8', 'r9']
RETURN_REGISTER = 'rax'

HEADER = """\
; ---------------------------------------------------------
; código gerado automaticamente pelo compilador Bonobo
; ---------------------------------------------------------
default rel
"""

SECTION_DATA_HEADER = "section .data"
SECTION_RODATA_HEADER = "section .rodata"
SECTION_BSS_HEADER = "section .bss"
SECTION_TEXT_HEADER = "section .text"

GLOBAL_START = "global _start"

# ---- dados -----------------------------------------------------------
STRING_DATA = "{label}: db {bytes}, 0\n{label}_len equ $ - {label} - 1"

GLOBAL_VAR_BSS = "{label}: resq 1"

# ---- funções -----------------------------------------------------------
FUNC_PROLOGUE = """\
{label}:
    push rbp
    mov rbp, rsp"""

FUNC_EPILOGUE = """\
    mov rsp, rbp
    pop rbp
    ret"""

INLINE_ASM_BEGIN = '; --- bloco asm ---'
INLINE_ASM_END = '; --- fim bloco asm ---'

PARAM_SPILL = "    mov [rbp-{offset}], {reg}    ; parâmetro '{name}'"

START_ENTRY = """\
_start:
    call func_main
    mov rdi, rax
    mov rax, 60
    syscall"""

# ---- operadores --------------------------------------------------------
BIN_OP_INSTR = {
    'PLUS':    'add rax, rbx',
    'MINUS':   'sub rax, rbx',
    'MUL':     'imul rax, rbx',
    'BIT_AND': 'and rax, rbx',
    'BIT_OR':  'or rax, rbx',
    'BIT_XOR': 'xor rax, rbx',
    'SHL':     'mov rcx, rbx\n    shl rax, cl',
    'SHR':     'mov rcx, rbx\n    shr rax, cl',
}

DIV_OP_INSTR = """\
    xor rdx, rdx
    idiv rbx"""

COMPARISON_SETCC = {
    'EQ':  'sete al',
    'NEQ': 'setne al',
    'LT':  'setl al',
    'GT':  'setg al',
    'LE':  'setle al',
    'GE':  'setge al',
}

UNARY_OP_INSTR = {
    'MINUS':   'neg rax',
    'BIT_NOT': 'not rax',
}

# ---- controle de fluxo --------------------------------------------------
JUMP_IF_FALSE = "    cmp rax, 0\n    je {label}"
JUMP = "    jmp {label}"
LABEL = "{label}:"