from __future__ import annotations

import argparse
import ctypes
import re
import shutil
import tempfile
import zipfile
from pathlib import Path

PLUGIN_ID = re.compile(r"^[A-Za-z0-9_-]{1,63}$")
VERSION = re.compile(r"^[A-Za-z0-9.+-]{1,31}$")
SUPPORTED_ABI_VERSIONS = (1, 2)


def manifest_value(text: str, key: str) -> str:
    prefix = f"{key}:"
    for line in text.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip().strip("'\"")
    raise ValueError(f"plugin.yml is missing {key}")


class PluginMetadata(ctypes.Structure):
    _fields_ = [
        ("id", ctypes.c_ubyte * 64),
        ("name", ctypes.c_ubyte * 128),
        ("version", ctypes.c_ubyte * 32),
        ("author", ctypes.c_ubyte * 128),
        ("description", ctypes.c_ubyte * 256),
    ]


class PluginDescriptorV1(ctypes.Structure):
    _fields_ = [
        ("struct_size", ctypes.c_uint32),
        ("abi_version", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint64),
        ("metadata", PluginMetadata),
        ("create", ctypes.c_void_p),
        ("shutdown", ctypes.c_void_p),
        ("destroy", ctypes.c_void_p),
    ]


class PluginDescriptorV2(ctypes.Structure):
    _fields_ = PluginDescriptorV1._fields_ + [("on_tick", ctypes.c_void_p)]


DESCRIPTOR_TYPES = {
    1: ("winisland_plugin_entry_v1", PluginDescriptorV1),
    2: ("winisland_plugin_entry_v2", PluginDescriptorV2),
}


def fixed_text(value: ctypes.Array[ctypes.c_ubyte]) -> str:
    return bytes(value).split(b"\0", 1)[0].decode("utf-8")


def read_descriptor(dll: Path, expected_abi: int) -> tuple[int, int, bool, dict[str, str]]:
    try:
        symbol, descriptor_type = DESCRIPTOR_TYPES[expected_abi]
    except KeyError as error:
        raise ValueError(f"unsupported plugin ABI version {expected_abi}") from error
    library = ctypes.WinDLL(str(dll))
    try:
        try:
            entrypoint = getattr(library, symbol)
        except AttributeError as error:
            raise ValueError(f"plugin DLL has no ABI v{expected_abi} entry point") from error
        entrypoint.restype = ctypes.POINTER(descriptor_type)
        pointer = entrypoint()
        if not pointer:
            raise ValueError("plugin DLL returned a null descriptor")
        descriptor = pointer.contents
        lifecycle_complete = bool(
            descriptor.create and descriptor.shutdown and descriptor.destroy
        )
        metadata = {
            field: fixed_text(getattr(descriptor.metadata, field))
            for field in ("id", "name", "version", "author", "description")
        }
        return descriptor.struct_size, descriptor.abi_version, lifecycle_complete, metadata
    finally:
        ctypes.windll.kernel32.FreeLibrary(ctypes.c_void_p(library._handle))


def validate_descriptor(archive: zipfile.ZipFile, manifest: str) -> None:
    entry = manifest_value(manifest, "entry")
    if Path(entry).name != entry or not entry.lower().endswith(".dll"):
        raise ValueError("plugin.yml entry must be a root-level DLL")
    try:
        abi_version = int(manifest_value(manifest, "abi-version"))
    except ValueError as error:
        raise ValueError("plugin.yml has an invalid abi-version") from error
    if abi_version not in SUPPORTED_ABI_VERSIONS:
        supported = ", ".join(str(version) for version in SUPPORTED_ABI_VERSIONS)
        raise ValueError(f"plugin.yml abi-version must be one of: {supported}")
    with tempfile.TemporaryDirectory() as directory:
        dll = Path(directory) / entry
        dll.write_bytes(archive.read(entry))
        struct_size, descriptor_abi, lifecycle_complete, metadata = read_descriptor(
            dll, abi_version
        )
    descriptor_type = DESCRIPTOR_TYPES[abi_version][1]
    if (
        struct_size < ctypes.sizeof(descriptor_type)
        or descriptor_abi != abi_version
        or not lifecycle_complete
    ):
        raise ValueError(f"plugin DLL has an invalid ABI v{abi_version} descriptor")
    for field, declared in metadata.items():
        packaged = manifest_value(manifest, field)
        if packaged != declared:
            raise ValueError(
                f"plugin metadata mismatch for {field}: "
                f"plugin.yml has {packaged!r}, DLL has {declared!r}"
            )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packages = sorted(args.root.glob("target/*.zip"))
    if len(packages) != 1:
        raise ValueError(f"expected one target/*.zip package, found {len(packages)}")
    source = packages[0]
    with zipfile.ZipFile(source) as archive:
        manifest = archive.read("plugin.yml").decode("utf-8")
        validate_descriptor(archive, manifest)
    plugin_id = manifest_value(manifest, "id")
    version = manifest_value(manifest, "version")
    if not PLUGIN_ID.fullmatch(plugin_id) or not VERSION.fullmatch(version):
        raise ValueError("plugin.yml contains an invalid ID or version")
    args.output.mkdir(parents=True, exist_ok=True)
    destination = args.output / f"{plugin_id}-{version}.winisland-plugin.zip"
    shutil.copyfile(source, destination)
    print(destination.resolve())


if __name__ == "__main__":
    main()
