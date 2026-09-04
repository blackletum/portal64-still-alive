#!/usr/bin/env python3

"""
This script parses RSP profiler output to generate a summary report and heatmap
of frame performance hot spots.

Usage:

1. Build the game with hardware debugging support and the RSP profiler enabled.

2. While debugging the game, press d-pad down on controller 3 to log timing
   information and save screenshots for the last rendered frame's display list.

3. Run this script with the debug log file passed via --debug-log-file. It will
   output a report of the cost of each captured display list command.

   The log file is also used to label dynamic model display lists. Optionally
   provide portal.map from the build directory using --symbol-map-file to label
   other display lists as well.

   Specify the saved screenshots' directory with --screenshot-dir to generate a
   heatmap highlighting pixel costs.

Caveats:

* Only commands from the top-level display list are present in the final output.
  The profiler does not recursively measure child performance.

* Dynamically loaded elements such as level geometry can share addresses with
  each other. Such display lists are labelled using a comma-separated list of
  all matching symbol names.

* The game builds some display lists at runtime, meaning they don't have symbol
  names. They are labelled using a +-separated list of all named child display
  lists (recursively). The top-level address is used when no names are found.

* Due to performance fluctuations and the way time is measured, some fast
  commands can be shown with negative run times.
"""

import argparse
import datetime
import itertools
import math
import os
import re
import struct

class SymbolMap:
    STATIC_SYMBOL_REGEX = re.compile(r"^\s+0x([a-f0-9]+)\s+(\w+)$")
    DYNAMIC_ASSET_RESET_REGEX = re.compile(r"^Reset dynamic assets$")
    DYNAMIC_ASSET_LOAD_REGEX = re.compile(r"^Loaded dynamic asset at 0x([a-f0-9]{8}): (\w+)$")

    def __init__(self, file_path):
        self._static_symbols = file_path and self._parse_symbol_file(file_path) or dict()
        self._dynamic_symbols = dict()

    @classmethod
    def _parse_static_symbol(cls, line):
        match = cls.STATIC_SYMBOL_REGEX.match(line)
        if not match:
            return None, None

        address, name = match.groups()
        return int(address, 16), name

    @classmethod
    def _parse_symbol_file(cls, file_path):
        address_to_symbol = dict()

        with open(file_path, "r") as f:
            for line in f:
                address, name = cls._parse_static_symbol(line)
                if address:
                    # Dynamic elements can share the same address
                    names = filter(None, (address_to_symbol.get(address), name))
                    address_to_symbol[address] = ",".join(names)

        return address_to_symbol

    @classmethod
    def _is_dynamic_asset_reset_line(cls, line):
        return cls.DYNAMIC_ASSET_RESET_REGEX.match(line)

    @classmethod
    def _parse_dynamic_asset_load(cls, line):
        match = cls.DYNAMIC_ASSET_LOAD_REGEX.match(line)
        if not match:
            return None, None

        address, name = match.groups()
        return int(address, 16), name

    def try_update(self, line):
        if (self._is_dynamic_asset_reset_line(line)):
            self._dynamic_symbols.clear()
            return True

        address, name = self._parse_dynamic_asset_load(line)
        if address:
            self._dynamic_symbols[address] = name
            return True

        return False

    def get_symbol_name(self, address):
        return self._static_symbols.get(address) or \
            self._dynamic_symbols.get(address)

class DisplayList:
    G_FILLRECT = 0xf6
    G_POPMTX   = 0xd8
    G_MTX      = 0xda
    G_MOVEWORD = 0xdb
    G_DL       = 0xde
    G_ENDDL    = 0xdf

    class Command:
        COMMAND_REGEX = re.compile(r"^dl 0x([a-f0-9]{2})([a-f0-9]{6})([a-f0-9]{8})$")

        def __init__(self, command, w0, w1):
            self.command = command
            self.w0 = w0
            self.w1 = w1
            self.children = []
            self.name = None

        @classmethod
        def parse(cls, line):
            match = cls.COMMAND_REGEX.match(line)
            if not match:
                return None

            command, w0, w1 = match.groups()
            return cls(
                int(command, 16),
                int(w0, 16),
                int(w1, 16)
            )

    def __init__(self, symbol_map):
        self._symbol_map = symbol_map
        self._stack = [None]  # Dummy element for root display list
        self._visited = set()
        self._child_names = dict()

    def _update(self, command):
        match command.command:
            case self.G_DL:
                current = self._stack[-1]
                if current:
                    current.children.append(command)
                self._stack.append(command)

            case self.G_ENDDL:
                if not self._stack:
                    raise ValueError("Malformed display list")

                dl = self._stack.pop()
                if dl and not (dl in self._visited):
                    name = self._symbol_map.get_symbol_name(dl.w1) or \
                        "+".join(filter(None, (c.name for c in dl.children)))

                    if name:
                        dl.name = name
                        self._child_names[dl.w1] = dl.name

                    self._visited.add(dl.w1)

    def try_update(self, line):
        command = self.Command.parse(line)
        if command:
            self._update(command)
            return True

        return False

    def get_command_name(self, command):
        match command.command:
            case self.G_FILLRECT:
                return "gsDPFillRectangle"
            case self.G_POPMTX:
                return "gsSPPopMatrix"
            case self.G_MTX:
                return "gsSPMatrix"
            case self.G_MOVEWORD:
                segment_num = (command.w0 // 4) & 0xf
                return f"gsSPSegment(0x{segment_num:x}, 0x{command.w1:08x})"
            case self.G_DL:
                address = self._child_names.get(command.w1) or \
                    f"0x{command.w1:08x}"
                return f"gsSPDisplayList({address})"
            case _:
                return f"unknown 0x{command.command:x} 0x{command.w0:08x}{command.w1:08x}"

class CommandInfo:
    SAMPLE_REGEX = re.compile(r"^(\d+)\/\d+ 0x([a-f0-9]{2})([a-f0-9]{6})([a-f0-9]{8}) (\d+\.?\d+) ms$")
    SCREENSHOT_NAME_REGEX = re.compile(r"^.+'(.+\.bin)'.+$")

    def __init__(self, command_index, command, w0, w1, start_time_ms):
        self.command_index = command_index
        self.command = command
        self.w0 = w0
        self.w1 = w1
        self.start_time_ms = start_time_ms
        self.sample_count = 1

        self.run_time_ms = 0
        self.screenshot = None
        self.diff_pixels = []

    def __iadd__(self, other):
        self.start_time_ms += other.start_time_ms
        self.sample_count += 1
        return self

    def try_update(self, line, screenshot_dir):
        match = self.SCREENSHOT_NAME_REGEX.match(line)
        if not match:
            return False

        screenshot_path = os.path.join(screenshot_dir, match[1])

        if os.path.exists(screenshot_path):
            with open(screenshot_path, "rb") as s:
                # Load up front for performance
                # This will stay in memory longer than needed, but it is simpler
                self.screenshot = list(itertools.batched(s.read(), 2, strict=True))

        return True

    def finish(self):
        self.start_time_ms /= self.sample_count

    def compute_diff(self, next):
        self.run_time_ms = next.start_time_ms - self.start_time_ms

        if self.screenshot and next.screenshot:
            self.diff_pixels = [
                i for i, (px1, px2) in enumerate(zip(self.screenshot, next.screenshot))
                if px1 != px2
            ]

    @classmethod
    def parse(cls, line):
        match = cls.SAMPLE_REGEX.match(line)
        if not match:
            return None

        command_index, command, w0, w1, start_time_ms = match.groups()
        return cls(
            int(command_index),
            int(command, 16),
            int(w0, 16),
            int(w1, 16),
            float(start_time_ms)
        )

class Heatmap:
    WIDTH = 320
    HEIGHT = 240

    BMP_HEADER_SIZE = 14
    BMP_DIB_HEADER_SIZE = 12
    BMP_BPP = 24

    def __init__(self):
        self._pixel_costs = dict()

    def update(self, pixel_indices, element_cost):
        if not pixel_indices or element_cost < 0:
            return False

        # Highlight elements with high per-pixel cost.
        #
        # Large expensive objects are more tolerable than small ones, as they
        # cover more of the screen. Small objects are more likely to appear
        # multiple times, or alongside other expensive objects.
        average_cost = element_cost / len(pixel_indices)
        for i in pixel_indices:
            self._pixel_costs[i] = self._pixel_costs.get(i, 0) + average_cost
        return True

    def write_bitmap(self, file_path):
        max_cost = max(self._pixel_costs.values())

        with open(file_path, "wb") as f:
            # Header and DIB header
            data_offset = self.BMP_HEADER_SIZE + self.BMP_DIB_HEADER_SIZE
            data_size = self.WIDTH * self.HEIGHT * (self.BMP_BPP // 8)
            f.write(struct.pack("<2cI4xI", b"\x42", b"\x4d", data_offset + data_size, data_offset))
            f.write(struct.pack("<I4H", self.BMP_DIB_HEADER_SIZE, self.WIDTH, self.HEIGHT, 1, self.BMP_BPP))

            # Data
            for row in range(self.HEIGHT):
                for col in range(self.WIDTH):
                    # Bitmaps store lines in reverse order
                    px_row = self.HEIGHT - row - 1
                    px_idx = (self.WIDTH * px_row) + col

                    value = math.floor((255 * self._pixel_costs.get(px_idx, 0) / max_cost) + 0.5)
                    f.write(struct.pack("3B", value, value, value))

class Profile:
    BEGIN_PROFILE_REGEX = re.compile(r"^Begin RSP profile$")
    END_PROFILE_REGEX = re.compile(r"^End RSP profile$")

    def __init__(self, symbol_map, screenshot_dir):
        self._name = datetime.datetime.now().strftime(f"profile-%y%m%d-%H%M%S%f")
        self._display_list = DisplayList(symbol_map)
        self._screenshot_dir = screenshot_dir
        self._command_info = []
        self._is_finished = False

    def _finish(self):
        for i in range(len(self._command_info)):
            current = self._command_info[i]
            current.finish()

            if i > 0:
                prev = self._command_info[i - 1]
                prev.compute_diff(current)

        # The last command is always a pipe sync we don't care about
        self._command_info.pop()
        self._is_finished = True

    @classmethod
    def is_profile_start_line(cls, line):
        return cls.BEGIN_PROFILE_REGEX.match(line)

    @classmethod
    def is_profile_end_line(cls, line):
        return cls.END_PROFILE_REGEX.match(line)

    def try_update(self, line):
        if (self.is_profile_end_line(line)):
            self._finish()
            return True

        info = CommandInfo.parse(line)
        if info:
            if info.command_index == len(self._command_info):
                self._command_info.append(info)
            elif info.command_index == len(self._command_info) - 1:
                self._command_info[-1] += info
            else:
                raise IndexError(f"Sample indices must be sequential")
            return True

        if self._command_info and self._command_info[-1].try_update(line, self._screenshot_dir):
            return True

        return self._display_list.try_update(line)

    def name(self):
        return self._name

    def is_finished(self):
        return self._is_finished

    def format_stats(self, sort=True):
        info = self._command_info[:]
        if sort:
            info.sort(
                key=lambda i: i.run_time_ms,
                reverse=True
            )

        column_padding = [20, 6, 0]
        pad = lambda e: f"{e[1]}".ljust(column_padding[e[0]])

        lines = [
            ["Run time (ms)", "Idx", "Command"],
            ["-------------", "---", "-------"],
            *[[
                f"{c.run_time_ms:.16f}",
                c.command_index,
                self._display_list.get_command_name(c)
            ] for c in info]
        ]

        return "\n".join(
            " ".join(pad(e) for e in enumerate(line))
            for line in lines
        )

    def build_heatmap(self):
        heatmap = Heatmap()

        num_updates = sum(
            1
            for info in self._command_info
            if heatmap.update(info.diff_pixels, info.run_time_ms)
        )

        return (num_updates > 0 and heatmap) or None

    def write(self, output_dir, sort=True):
        file_name_base = os.path.join(output_dir, f"{self._name}")

        with open(f"{file_name_base}.txt", "w") as f:
            f.write(self.format_stats(sort))

        heatmap = self.build_heatmap()
        if heatmap:
            heatmap.write_bitmap(f"{file_name_base}.bmp")

# Main

def get_args():
    parser = argparse.ArgumentParser(
        prog="parse_rsp_profile",
        description="Generates frame performance data from RSP profiler output"
    )
    parser.add_argument(
        "-d", "--debug-log-file",
        required=True,
        help="File containing debug console output"
    )
    parser.add_argument(
        "-m", "--symbol-map-file",
        help="Linker-generated file containing symbol names and addresses"
    )
    parser.add_argument(
        "-s", "--screenshot-dir",
        default=os.getcwd(),
        help="Directory containing screenshot files (defaults to current directory)"
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=os.getcwd(),
        help="Directory to output results to (defaults to current directory)"
    )
    parser.add_argument(
        "-u", "--unsorted",
        action="store_true",
        help="Output the summary without sorting by command run time"
    )

    return parser.parse_args()

args = get_args()

symbol_map = SymbolMap(args.symbol_map_file)
current_profile = None

with open(args.debug_log_file, "r") as f:
    for line in f:
        if Profile.is_profile_start_line(line):
            current_profile = Profile(symbol_map, args.screenshot_dir)

        if current_profile:
            if current_profile.try_update(line):
                continue
            elif current_profile.is_finished():
                current_profile.write(args.output_dir, sort=not args.unsorted)
                print(f"Wrote {current_profile.name()} to {args.output_dir}")
                current_profile = None
                continue

        symbol_map.try_update(line)
