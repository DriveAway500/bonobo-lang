import ply.yacc as yacc

# Import token list and lexer definition from your lexer
from lexer import lexer, tokens, reserved


# --- AST Node Definitions ---

class ASTNode:
    """Base class for all Abstract Syntax Tree nodes."""
    pass

class ProgramNode(ASTNode):
    def __init__(self, statements):
        self.statements = statements

class FunctionDeclNode(ASTNode):
    def __init__(self, name, params, return_type, body):
        self.name = name
        self.params = params
        self.return_type = return_type
        self.body = body

class VarDeclNode(ASTNode):
    def __init__(self, is_mutable, name, var_type, value):
        self.is_mutable = is_mutable
        self.name = name
        self.type = var_type
        self.value = value

class StructDeclNode(ASTNode):
    def __init__(self, name, fields):
        self.name = name
        self.fields = fields

class BlockNode(ASTNode):
    def __init__(self, statements):
        self.statements = statements

class IfNode(ASTNode):
    def __init__(self, condition, then_branch, else_branch):
        self.condition = condition
        self.then_branch = then_branch
        self.else_branch = else_branch

class WhileNode(ASTNode):
    def __init__(self, condition, body):
        self.condition = condition
        self.body = body

class ReturnNode(ASTNode):
    def __init__(self, expression):
        self.expression = expression

# --- New Assembly AST Nodes ---

class AsmOperandNode(ASTNode):
    def __init__(self, constraint, expression):
        self.constraint = constraint    # Ex: "={rax}"
        self.expression = expression    # AST Node para a variável/valor

class AsmBlockNode(ASTNode):
    def __init__(self, is_volatile, template, outputs, inputs, clobbers):
        self.is_volatile = is_volatile  # True/False
        self.template = template        # String com a instrução ex: "syscall"
        self.outputs = outputs          # Lista de AsmOperandNode
        self.inputs = inputs            # Lista de AsmOperandNode
        self.clobbers = clobbers        # Lista de strings ex: ["~{rcx}", "~{r11}"]

# ------------------------------

class BinaryOpNode(ASTNode):
    def __init__(self, left, op, right):
        self.left = left
        self.op = op
        self.right = right

class UnaryOpNode(ASTNode):
    def __init__(self, op, operand):
        self.op = op
        self.operand = operand

class LiteralNode(ASTNode):
    def __init__(self, value, literal_type):
        self.value = value
        self.type = literal_type

class IdentifierNode(ASTNode):
    def __init__(self, name):
        self.name = name

class FnCallNode(ASTNode):
    def __init__(self, name, args):
        self.name = name
        self.args = args


# --- Operator Precedence and Associativity ---

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
    ('right', 'UNARY', 'LOGICAL_NOT', 'BIT_NOT'),
    ('left', 'DOT', 'ARROW'),
)


# --- Grammar Rules ---

def p_program(p):
    '''program : statement_list'''
    p[0] = ProgramNode(p[1])

def p_statement_list(p):
    '''statement_list : statement_list statement
                      | empty'''
    if len(p) == 3:
        p[0] = p[1] + [p[2]]
    else:
        p[0] = []

def p_statement(p):
    '''statement : function_decl
                 | var_decl SEMI
                 | struct_decl
                 | if_statement
                 | while_statement
                 | return_statement SEMI
                 | asm_statement SEMI
                 | expr_statement SEMI
                 | block'''
    p[0] = p[1]

# Rust-like syntax: fn name(param: type) -> ReturnType { ... }
def p_function_decl(p):
    '''function_decl : DEF IDENT '(' parameter_list ')' ARROW type block
                     | DEF IDENT '(' parameter_list ')' block'''
    if len(p) == 9:
        p[0] = FunctionDeclNode(p[2], p[4], p[7], p[8])
    else:
        p[0] = FunctionDeclNode(p[2], p[4], "void", p[6])

def p_parameter_list(p):
    '''parameter_list : parameter_list_nonempty
                      | empty'''
    p[0] = p[1] if p[1] is not None else []

def p_parameter_list_nonempty(p):
    '''parameter_list_nonempty : parameter_list_nonempty COMMA parameter
                               | parameter'''
    if len(p) == 4:
        p[0] = p[1] + [p[3]]
    else:
        p[0] = [p[1]]

def p_parameter(p):
    '''parameter : IDENT COLON type'''
    p[0] = (p[1], p[3])

# Rust-like variable declarations: let mut x: int = 10; or let x = 10;
def p_var_decl(p):
    '''var_decl : LET IDENT COLON type ASSIGN expression
                | LET IDENT ASSIGN expression
                | LET IDENT COLON type'''
    if len(p) == 7:
        p[0] = VarDeclNode(is_mutable=False, name=p[2], var_type=p[4], value=p[6])
    elif len(p) == 5 and p[3] == '=':
        p[0] = VarDeclNode(is_mutable=False, name=p[2], var_type=None, value=p[4])
    else:
        p[0] = VarDeclNode(is_mutable=False, name=p[2], var_type=p[4], value=None)

# Struct declarations: struct Point { x: int, y: int }
def p_struct_decl(p):
    '''struct_decl : KEYWORD_STRUCT IDENT '{' struct_field_list '}' '''
    p[0] = StructDeclNode(p[2], p[4])

def p_struct_field_list(p):
    '''struct_field_list : struct_field_list COMMA struct_field
                         | struct_field
                         | empty'''
    if len(p) == 4:
        p[0] = p[1] + [p[3]]
    elif len(p) == 2 and p[1] is not None:
        p[0] = [p[1]]
    else:
        p[0] = []

def p_struct_field(p):
    '''struct_field : IDENT COLON type'''
    p[0] = (p[1], p[3])

# Control Flow
def p_if_statement(p):
    '''if_statement : IF expression block ELSE block
                    | IF expression block'''
    if len(p) == 6:
        p[0] = IfNode(p[2], p[3], p[5])
    else:
        p[0] = IfNode(p[2], p[3], None)

def p_while_statement(p):
    '''while_statement : WHILE expression block'''
    p[0] = WhileNode(p[2], p[3])

def p_return_statement(p):
    '''return_statement : RETURN expression
                         | RETURN'''
    p[0] = ReturnNode(p[2] if len(p) == 3 else None)


# --- Inline Assembly Parsing Rules ---

def p_asm_statement(p):
    '''asm_statement : KEYWORD_ASM KEYWORD_VOLATILE '(' asm_field_list ')'
                     | KEYWORD_ASM '(' asm_field_list ')' '''
    if len(p) == 6:
        fields = p[4]
        is_volatile = True
    else:
        fields = p[3]
        is_volatile = False

    p[0] = AsmBlockNode(
        is_volatile=is_volatile,
        template=fields.get('template', ""),
        outputs=fields.get('outputs', []),
        inputs=fields.get('inputs', []),
        clobbers=fields.get('clobbers', [])
    )

def p_asm_field_list(p):
    '''asm_field_list : asm_field_list COMMA asm_field
                      | asm_field'''
    if len(p) == 4:
        p[1].update(p[3])
        p[0] = p[1]
    else:
        p[0] = p[1]

def p_asm_field(p):
    '''asm_field : KEYWORD_TEMPLATE COLON STRING
                 | KEYWORD_OUTPUTS COLON '[' asm_operand_list ']'
                 | KEYWORD_INPUTS COLON '[' asm_operand_list ']'
                 | KEYWORD_CLOBBERS COLON '[' string_list ']' '''
    key = p[1]
    if key == 'template':
        p[0] = {'template': p[3]}
    elif key == 'outputs':
        p[0] = {'outputs': p[4]}
    elif key == 'inputs':
        p[0] = {'inputs': p[4]}
    elif key == 'clobbers':
        p[0] = {'clobbers': p[4]}

def p_asm_operand_list(p):
    '''asm_operand_list : asm_operand_list COMMA asm_operand
                         | asm_operand
                         | empty'''
    if len(p) == 4:
        p[0] = p[1] + [p[3]]
    elif len(p) == 2 and p[1] is not None:
        p[0] = [p[1]]
    else:
        p[0] = []

def p_asm_operand(p):
    '''asm_operand : STRING '(' expression ')' '''
    p[0] = AsmOperandNode(constraint=p[1], expression=p[3])

def p_string_list(p):
    '''string_list : string_list COMMA STRING
                   | STRING
                   | empty'''
    if len(p) == 4:
        p[0] = p[1] + [p[3]]
    elif len(p) == 2 and p[1] is not None:
        p[0] = [p[1]]
    else:
        p[0] = []

# -------------------------------------


def p_block(p):
    '''block : '{' statement_list '}' '''
    p[0] = BlockNode(p[2])

def p_expr_statement(p):
    '''expr_statement : expression'''
    p[0] = p[1]

# Types
def p_type(p):
    '''type : TYPE_INT
            | TYPE_CHAR
            | TYPE_FLOAT
            | TYPE_DOUBLE
            | TYPE_VOID
            | TYPE_BOOL
            | TYPE_SHORT
            | TYPE_LONG
            | TYPE_SIGNED
            | TYPE_UNSIGNED
            | IDENT'''
    p[0] = p[1]

# Expressions
def p_expression_binop(p):
    '''expression : expression PLUS expression
                  | expression MINUS expression
                  | expression MUL expression
                  | expression DIV expression
                  | expression MOD expression
                  | expression EQ expression
                  | expression NEQ expression
                  | expression LT expression
                  | expression LE expression
                  | expression GT expression
                  | expression GE expression
                  | expression LOGICAL_AND expression
                  | expression LOGICAL_OR expression
                  | expression BIT_AND expression
                  | expression BIT_OR expression
                  | expression BIT_XOR expression
                  | expression SHL expression
                  | expression SHR expression
                  | expression ASSIGN expression'''
    p[0] = BinaryOpNode(p[1], p[2], p[3])

def p_expression_unary(p):
    '''expression : MINUS expression %prec UNARY
                  | LOGICAL_NOT expression
                  | BIT_NOT expression
                  | BIT_AND expression %prec UNARY'''
    p[0] = UnaryOpNode(p[1], p[2])

def p_expression_group(p):
    '''expression : '(' expression ')' '''
    p[0] = p[2]

def p_expression_literal(p):
    '''expression : NUMBER
                  | FLOAT_NUMBER
                  | HEX_NUMBER
                  | STRING'''
    p[0] = LiteralNode(p[1], "literal")

def p_expression_identifier(p):
    '''expression : IDENT'''
    p[0] = IdentifierNode(p[1])

def p_expression_call(p):
    '''expression : IDENT '(' arg_list ')' '''
    p[0] = FnCallNode(p[1], p[3])

def p_arg_list(p):
    '''arg_list : arg_list_nonempty
                | empty'''
    p[0] = p[1] if p[1] is not None else []

def p_arg_list_nonempty(p):
    '''arg_list_nonempty : arg_list_nonempty COMMA expression
                         | expression'''
    if len(p) == 4:
        p[0] = p[1] + [p[3]]
    else:
        p[0] = [p[1]]

def p_empty(p):
    '''empty :'''
    pass

def p_error(p):
    if p:
        print(f"Syntax error at token '{p.value}' (line {p.lineno})")
    else:
        print("Syntax error at EOF")

# Build the parser
parser = yacc.yacc()