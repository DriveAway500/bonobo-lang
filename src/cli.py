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
        description="Simple NASM x86_64 compiler pipeline."
    )

    parser.add_argument(
        "source",
        type=str,
        help="Path to the source file to compile"
    )

    parser.add_argument(
        "-c", "--compile",
        action="store_true",
        help="Assemble and link the generated NASM code into an executable"
    )

    args = parser.parse_args()

    # Always derive output filename from source file name
    base_name, _ = os.path.splitext(args.source)
    output_path = f"{base_name}.asm"

    return CLIArgs(
        source_path=args.source,
        output_path=output_path,
        should_compile=args.compile
    )