import sys
from utils.cli import parse_args
from utils.exect import run_build_pipeline
from utils.file import read_file, write_file

from lexer import lexer
from parser import parser
from codegen import LLVMCodeGenerator


class CompilerPipeline:
    """Encapsulates the full compilation flow from source file to output assembly/executable."""

    def __init__(self, source_path: str, output_path: str):
        self.source_path = source_path
        self.output_path = output_path
        self.codegen = LLVMCodeGenerator()

    def compile(self, assemble_and_link: bool = False) -> None:
        """Read, parse and generate LLVM IR, optionally compiling it with Clang."""
        print("=== [Stage 1: Reading File] ===")
        print(f"Loading source code from: '{self.source_path}'")
        source_code = read_file(self.source_path)
        print("--- Raw Source Code ---")
        print(source_code)
        print("-----------------------\n")

        print(f"=== [Stage 2: Lexical Analysis] ===")
        token_lexer = lexer.clone()
        token_lexer.input(source_code)
        tokens = list(token_lexer)
        print(f"Generated {len(tokens)} token(s):")
        for i, token in enumerate(tokens):
            print(f"  [{i:03d}] {token}")
        print()

        print(f"=== [Stage 3: Parsing AST] ===")
        lexer.input(source_code)
        ast = parser.parse(source_code, lexer=lexer)
        print("--- AST Representation ---")
        print(ast)
        print("---------------------------\n")

        print("=== [Stage 4: LLVM IR Generation] ===")
        llvm_ir = self.codegen.generate(ast)
        print("--- Generated LLVM IR ---")
        print(llvm_ir)
        print("--------------------------------------\n")

        print(f"=== [Stage 5: Writing Output File] ===")
        write_file(self.output_path, str(llvm_ir))
        print(f"LLVM IR written to '{self.output_path}'\n")

        if assemble_and_link:
            print("=== [Stage 6: Compiling & Linking (Clang)] ===")
            exe_path = self.output_path.rsplit('.', 1)[0]
            print(f"Running build pipeline: '{self.output_path}' -> '{exe_path}'")
            run_build_pipeline(self.output_path, exe_path)
            print(f"Executable built successfully: '{exe_path}'\n")


def main() -> None:
    try:
        args = parse_args()
        print("=== [Compiler Initialized] ===")
        print(f"Source: {args.source_path}")
        print(f"Output: {args.output_path}")
        print(f"Assemble & Link flag: {args.should_compile}\n")

        pipeline = CompilerPipeline(
            source_path=args.source_path,
            output_path=args.output_path,
        )
        pipeline.compile(assemble_and_link=args.should_compile)
        print("=== [Compilation Process Completed Successfully] ===")
    except Exception as e:
        print(f"\n[FATAL ERROR]: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()