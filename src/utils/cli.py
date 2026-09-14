import argparse
import os
from typing import NamedTuple


class CLIArgs(NamedTuple):
    source_path: str
    output_path: str
    should_compile: bool


def parse_args() -> CLIArgs:
    """Parses command line arguments for the compiler interface."""
    parser = argparse.ArgumentParser(
        description="Simple LLVM IR compiler pipeline."
    )

    parser.add_argument(
        "source",
        type=str,
        help="Path to the source file to compile"
    )

    parser.add_argument(
        "-c", "--compile",
        action="store_true",
        help="Compile the generated LLVM IR into an executable"
    )

    args = parser.parse_args()

    # Always derive output filename from source file name
    base_name, _ = os.path.splitext(args.source)
    output_path = f"{base_name}.ll"

    return CLIArgs(
        source_path=args.source,
        output_path=output_path,
        should_compile=args.compile
    )