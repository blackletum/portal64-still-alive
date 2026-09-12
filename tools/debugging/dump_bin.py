import gdb
import os
import traceback

class DumpBin(gdb.Command):
    """
    Write binary data to a file.
    Specify symbol name, size (optional), and custom path (optional) as arguments.
    """

    DEFAULT_OUTPUT_FILE = "data.bin"

    def __init__(self):
        super().__init__("dump_bin", gdb.COMMAND_USER)

    def invoke(self, argument, from_tty):
        args = argument.split()

        if len(args) < 1:
            print("No source specified")
            return

        source_name = args[0]
        size = len(args) > 1 and args[1] or gdb.parse_and_eval(f"sizeof({source_name})")
        output_path = os.path.abspath(len(args) > 2 and args[2] or self.DEFAULT_OUTPUT_FILE)

        try:
            gdb.execute(f"dump binary memory {output_path} {source_name} ((char*){source_name})+{size}")
            print(f"Wrote to {output_path}")
        except:
            print("Error dumping binary data")
            print(traceback.format_exc())

DumpBin()
