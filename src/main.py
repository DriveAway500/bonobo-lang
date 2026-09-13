import sys
from cli import parse_args
from utils import (
    write_file,
    read_file,
    get_base_name,
    run_build_pipeline,
)

from lexer import tokenize
from parser import Parser
from codegen import CodeGen


class CompilerPipeline:
    """Encapsulates the full compilation flow from source file to output assembly/executable."""

    def __init__(self, source_path: str, output_path: str):
        self.source_path = source_path
        self.output_path = output_path
        self.codegen = CodeGen()

    def compile(self, assemble_and_link: bool = False) -> None:
        """Executes the pipeline: Read -> Lex -> Parse -> Codegen -> Write (+ optional NASM/LD)."""
        source_code = read_file(self.source_path)
        tokens = tokenize(source_code)

        parser = Parser(tokens)
        ast = parser.parse_program()

        assembly_code = self.codegen.generate(ast)
        write_file(self.output_path, assembly_code)
        print(f"Assembly written to '{self.output_path}'")

        if assemble_and_link:
            exe_path = get_base_name(self.output_path)
            run_build_pipeline(self.output_path, exe_path)
            print(f"Executable built successfully: '{exe_path}'")


def main() -> None:
    try:
        args = parse_args()
        pipeline = CompilerPipeline(
            source_path=args.source_path,
            output_path=args.output_path,
        )
        pipeline.compile(assemble_and_link=args.should_compile)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()