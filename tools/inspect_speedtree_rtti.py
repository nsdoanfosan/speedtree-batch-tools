"""Inspect SpeedTree's PE RTTI and string cross-references without loading it.

This is intentionally dependency-free so it can be used against an installed
Modeler binary from the regular Python interpreter.
"""

from __future__ import annotations

import argparse
import struct
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Section:
    name: str
    rva: int
    virtual_size: int
    raw_offset: int
    raw_size: int

    def contains_rva(self, rva: int) -> bool:
        return self.rva <= rva < self.rva + max(self.virtual_size, self.raw_size)

    def contains_offset(self, offset: int) -> bool:
        return self.raw_offset <= offset < self.raw_offset + self.raw_size


class PeImage:
    def __init__(self, path: Path):
        self.path = path
        self.data = path.read_bytes()
        pe_offset = struct.unpack_from("<I", self.data, 0x3C)[0]
        if self.data[pe_offset : pe_offset + 4] != b"PE\0\0":
            raise ValueError(f"not a PE image: {path}")
        coff = pe_offset + 4
        section_count = struct.unpack_from("<H", self.data, coff + 2)[0]
        optional_size = struct.unpack_from("<H", self.data, coff + 16)[0]
        optional = coff + 20
        if struct.unpack_from("<H", self.data, optional)[0] != 0x20B:
            raise ValueError(f"not a PE32+ image: {path}")
        self.image_base = struct.unpack_from("<Q", self.data, optional + 24)[0]
        section_table = optional + optional_size
        self.sections = []
        for index in range(section_count):
            row = section_table + index * 40
            name = self.data[row : row + 8].split(b"\0", 1)[0].decode("ascii")
            virtual_size, rva, raw_size, raw_offset = struct.unpack_from(
                "<IIII", self.data, row + 8
            )
            self.sections.append(
                Section(name, rva, virtual_size, raw_offset, raw_size)
            )

    def offset_to_rva(self, offset: int) -> int | None:
        for section in self.sections:
            if section.contains_offset(offset):
                return section.rva + offset - section.raw_offset
        return offset if offset < min(s.raw_offset for s in self.sections) else None

    def rva_to_offset(self, rva: int) -> int | None:
        for section in self.sections:
            if section.contains_rva(rva):
                delta = rva - section.rva
                return section.raw_offset + delta if delta < section.raw_size else None
        return rva if rva < min(s.rva for s in self.sections) else None

    def section_for_rva(self, rva: int) -> Section | None:
        return next((s for s in self.sections if s.contains_rva(rva)), None)

    def function_bounds(self, rva: int) -> tuple[int, int] | None:
        """Return the x64 .pdata runtime-function range containing an RVA."""
        pdata = next((s for s in self.sections if s.name == ".pdata"), None)
        if pdata is None:
            return None
        end = pdata.raw_offset + pdata.raw_size
        for offset in range(pdata.raw_offset, end - 11, 12):
            begin_rva, end_rva, _unwind_rva = struct.unpack_from(
                "<III", self.data, offset
            )
            if begin_rva <= rva < end_rva:
                return begin_rva, end_rva
        return None

    def rtti_class_names(self, hierarchy_rva: int) -> list[str]:
        hierarchy_offset = self.rva_to_offset(hierarchy_rva)
        if hierarchy_offset is None:
            return []
        _signature, _attributes, base_count, base_array_rva = struct.unpack_from(
            "<IIII", self.data, hierarchy_offset
        )
        base_array_offset = self.rva_to_offset(base_array_rva)
        if base_array_offset is None or base_count > 1024:
            return []
        names = []
        for index in range(base_count):
            base_descriptor_rva = struct.unpack_from(
                "<I", self.data, base_array_offset + index * 4
            )[0]
            base_descriptor_offset = self.rva_to_offset(base_descriptor_rva)
            if base_descriptor_offset is None:
                continue
            type_descriptor_rva = struct.unpack_from(
                "<I", self.data, base_descriptor_offset
            )[0]
            type_descriptor_offset = self.rva_to_offset(type_descriptor_rva)
            if type_descriptor_offset is None:
                continue
            name_start = type_descriptor_offset + 16
            name_end = self.data.find(b"\0", name_start, name_start + 512)
            if name_end < 0:
                continue
            names.append(self.data[name_start:name_end].decode("ascii", "replace"))
        return names


def iter_occurrences(data: bytes, needle: bytes):
    start = 0
    while True:
        found = data.find(needle, start)
        if found < 0:
            return
        yield found
        start = found + 1


def string_xrefs(image: PeImage, value: bytes):
    text = next(section for section in image.sections if section.name == ".text")
    text_data = image.data[text.raw_offset : text.raw_offset + text.raw_size]
    targets = []
    for offset in iter_occurrences(image.data, value + b"\0"):
        rva = image.offset_to_rva(offset)
        if rva is not None:
            targets.append((offset, rva))
    rows = []
    for index in range(len(text_data) - 7):
        # x64 LEA reg,[RIP+disp32]. REX can be 0x48 or 0x4c and the ModRM
        # register field varies, while mod=00 and r/m=101 remain fixed.
        rex, opcode, modrm = text_data[index : index + 3]
        if rex not in (0x48, 0x4C) or opcode != 0x8D or modrm & 0xC7 != 0x05:
            continue
        displacement = struct.unpack_from("<i", text_data, index + 3)[0]
        instruction_rva = text.rva + index
        target_rva = instruction_rva + 7 + displacement
        for offset, string_rva in targets:
            if target_rva == string_rva:
                rows.append(
                    {
                        "string_offset": offset,
                        "string_rva": string_rva,
                        "xref_rva": instruction_rva,
                        "function": image.function_bounds(instruction_rva),
                    }
                )
    return rows


def inspect_rtti(image: PeImage, query: str, max_vtable_slots: int):
    text = next(section for section in image.sections if section.name == ".text")
    query_bytes = query.casefold().encode("ascii")
    descriptors = []
    for offset in iter_occurrences(image.data, b".?AV"):
        end = image.data.find(b"\0", offset)
        if end < 0 or end - offset > 512:
            continue
        name = image.data[offset:end]
        if query_bytes not in name.lower():
            continue
        descriptor_offset = offset - 16
        descriptor_rva = image.offset_to_rva(descriptor_offset)
        if descriptor_rva is None:
            continue
        descriptors.append((name.decode("ascii"), descriptor_offset, descriptor_rva))

    output = []
    for name, descriptor_offset, descriptor_rva in descriptors:
        cols = []
        encoded = struct.pack("<I", descriptor_rva)
        for occurrence in iter_occurrences(image.data, encoded):
            col_offset = occurrence - 12
            if col_offset < 0:
                continue
            col_rva = image.offset_to_rva(col_offset)
            if col_rva is None:
                continue
            signature, object_offset, constructor_offset, type_rva, class_rva, self_rva = (
                struct.unpack_from("<IIIIII", image.data, col_offset)
            )
            if signature != 1 or type_rva != descriptor_rva or self_rva != col_rva:
                continue
            col_va = image.image_base + col_rva
            vtables = []
            for pointer_offset in iter_occurrences(image.data, struct.pack("<Q", col_va)):
                pointer_rva = image.offset_to_rva(pointer_offset)
                if pointer_rva is None:
                    continue
                vtable_rva = pointer_rva + 8
                vtable_offset = image.rva_to_offset(vtable_rva)
                if vtable_offset is None:
                    continue
                methods = []
                for slot in range(max_vtable_slots):
                    method_va = struct.unpack_from(
                        "<Q", image.data, vtable_offset + slot * 8
                    )[0]
                    method_rva = method_va - image.image_base
                    if not text.contains_rva(method_rva):
                        break
                    methods.append(method_rva)
                if methods:
                    vtables.append({"vtable_rva": vtable_rva, "methods": methods})
            cols.append(
                {
                    "col_rva": col_rva,
                    "object_offset": object_offset,
                    "constructor_offset": constructor_offset,
                    "class_rva": class_rva,
                    "class_names": image.rtti_class_names(class_rva),
                    "vtables": vtables,
                }
            )
        output.append(
            {
                "name": name,
                "descriptor_offset": descriptor_offset,
                "descriptor_rva": descriptor_rva,
                "cols": cols,
            }
        )
    return output


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("exe", type=Path)
    parser.add_argument("--rtti", action="append", default=[])
    parser.add_argument("--string", action="append", default=[])
    parser.add_argument("--max-vtable-slots", type=int, default=64)
    args = parser.parse_args()
    image = PeImage(args.exe.resolve())
    print(f"image_base=0x{image.image_base:X}")
    for query in args.rtti:
        print(f"RTTI {query!r}")
        for row in inspect_rtti(image, query, args.max_vtable_slots):
            print(
                f"  {row['name']} descriptor_rva=0x{row['descriptor_rva']:X} "
                f"raw=0x{row['descriptor_offset']:X}"
            )
            for col in row["cols"]:
                print(
                    f"    COL rva=0x{col['col_rva']:X} offset={col['object_offset']} "
                    f"cd={col['constructor_offset']} class=0x{col['class_rva']:X}"
                )
                if col["class_names"]:
                    print("      hierarchy=" + " -> ".join(col["class_names"]))
                for vtable in col["vtables"]:
                    methods = " ".join(f"0x{value:X}" for value in vtable["methods"])
                    print(
                        f"      vtable rva=0x{vtable['vtable_rva']:X} methods={methods}"
                    )
    for value in args.string:
        print(f"STRING {value!r}")
        rows = string_xrefs(image, value.encode("ascii"))
        if not rows:
            print("  no direct LEA xrefs")
        for row in rows:
            function = row["function"]
            bounds = (
                f" function=0x{function[0]:X}-0x{function[1]:X}"
                if function
                else ""
            )
            print(
                f"  raw=0x{row['string_offset']:X} rva=0x{row['string_rva']:X} "
                f"xref=0x{row['xref_rva']:X}{bounds}"
            )


if __name__ == "__main__":
    main()
