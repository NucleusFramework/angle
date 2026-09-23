#!/usr/bin/env python3
"""Build, package and verify the trimmed ANGLE runtime DLLs shipped by Nucleus.

Nucleus' Tao Windows backend only ever asks ANGLE for a Direct3D 11 display
(hardware, falling back to WARP). Everything else ANGLE can do -- the Vulkan,
desktop-GL/WGL and D3D9 backends, SwiftShader, WebGPU, OpenCL, the GLES1
emulation -- is dead weight in the DLL we ship, so this harness turns it off at
the GN level instead of extracting a general-purpose build from a browser
release.

Subcommands mirror the CI steps and can each be run by hand on a Windows
machine with depot_tools on PATH:

    angle_prebuilt.py resolve-ref
    angle_prebuilt.py prepare-toolchain --source-root . --arch x64
    angle_prebuilt.py gn-args --arch x64 --output out/Release/args.gn
    angle_prebuilt.py package --build-dir out/Release --arch x64 ...
    angle_prebuilt.py verify --archive dist/angle-...zip --arch x64
"""

from __future__ import annotations

import argparse
import ast
import json
import hashlib
import os
import re
import shutil
import struct
import subprocess
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

CHROME_RELEASES_URL = (
    "https://chromiumdash.appspot.com/fetch_releases"
    "?channel=Stable&platform=Windows&num=1"
)
# The GitHub mirror rather than Gerrit: a fork shares its object store with its
# parent, so a commit resolved here can be turned into a branch of this fork
# through the refs API alone, with nothing to push.
ANGLE_GIT_URL = "https://github.com/google/angle.git"

DEBUGGER_PATCH_MARKER = "nucleus-prebuilt: optional Windows debugger tools"

# Shared with every target. Kept deliberately close to what upstream ANGLE's own
# release builds use, minus the test/tooling targets we never ship.
COMMON_GN_ARGS = {
    "is_debug": False,
    "is_component_build": False,
    "angle_use_static_angle": False,
    "angle_build_all": False,
    "angle_build_tests": False,
    "build_angle_deqp_tests": False,
    "build_angle_end2end_tests_library": False,
    "angle_enable_cl": False,
    "angle_enable_null": False,
    "angle_enable_renderdoc": False,
    "angle_enable_swiftshader": False,
    "angle_enable_trace": False,
    "angle_enable_vulkan": False,
    "angle_enable_vulkan_api_dump_layer": False,
    "angle_enable_vulkan_validation_layers": False,
    "angle_enable_wgpu": False,
    "angle_with_capture_by_default": False,
    "clang_use_chrome_plugins": False,
    "symbol_level": 0,
    "treat_warnings_as_errors": False,
}

# Direct3D 11 only. HLSL stays on because it is the shader language the D3D11
# backend emits; ESSL/GLSL are only consumed by the GL backend.
WIN32_GN_ARGS = {
    "target_os": "win",
    # No angle_enable_d3d9: upstream deleted the D3D9 backend, and GN rejects an
    # argument nothing reads. Electron's ANGLE still carried it, which is part of
    # why its libGLESv2.dll is as large as it is.
    "angle_enable_d3d11": True,
    # Nucleus drives DirectComposition itself (nucleus_tao_windows_overlay_dcomp.cpp
    # loads dcomp.dll and composes ANGLE pbuffers), so ANGLE never receives an
    # IDCompositionSurface as an EGL native window.
    "angle_enable_d3d11_compositor_native_window": False,
    "angle_enable_essl": False,
    "angle_enable_gl": False,
    "angle_enable_glsl": False,
    "angle_enable_hlsl": True,
    "angle_enable_metal": False,
    "angle_enable_msl": False,
}

# Imports we expect to see in the shipped DLLs. Anything else means a backend we
# thought was disabled came back.
ALLOWED_IMPORTS = {
    "kernel32.dll",
    "user32.dll",
    "gdi32.dll",
    "dxgi.dll",
    "advapi32.dll",
    "ole32.dll",
    "oleaut32.dll",
    "shell32.dll",
    "version.dll",
    "windowscodecs.dll",
    "libglesv2.dll",
}

# API sets (api-ms-win-core-synch-l1-2-0.dll and friends) are OS forwarders, not
# a backend coming back, so they are allowed wholesale.
ALLOWED_IMPORT_PREFIXES = ("api-ms-win-", "ext-ms-win-")

# Source paths of backends that must not be compiled in. ANGLE keeps __FILE__
# strings for its asserts, so a disabled backend leaves no trace in the binary.
FORBIDDEN_SOURCE_MARKERS = (
    "libANGLE/renderer/vulkan",
    "libANGLE/renderer/gl/",
    "libANGLE/renderer/d3d/d3d9",
    "libANGLE/renderer/wgpu",
)


def run(command, **kwargs):
    printable = " ".join(str(part) for part in command)
    print(f"+ {printable}", flush=True)
    return subprocess.run(command, check=True, **kwargs)


def emit_output(**values):
    """Writes step outputs for GitHub Actions, and to stdout when run by hand."""
    path = os.environ.get("GITHUB_OUTPUT")
    for key, value in values.items():
        print(f"{key}={value}")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


# --------------------------------------------------------------------------- #
#  resolve-ref                                                                 #
# --------------------------------------------------------------------------- #


def fetch_stable_chrome_version():
    request = urllib.request.Request(
        CHROME_RELEASES_URL, headers={"User-Agent": "nucleus-angle-prebuilt/1.0"}
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        releases = json.loads(response.read().decode("utf-8"))
    if not releases:
        raise RuntimeError("Chromium Dash returned no stable release")
    version = releases[0]["version"]
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        raise RuntimeError(f"Unexpected Chrome version: {version!r}")
    return version


def cmd_resolve_ref(args):
    """Maps the current stable Chrome to the ANGLE release branch it ships.

    Chrome `M.0.BBBB.P` is cut from Chromium branch `BBBB`, and ANGLE cuts a
    `chromium/BBBB` branch at the same point -- so this picks the ANGLE revision
    that has been through a full Chrome stable cycle rather than today's main.
    """
    stable = fetch_stable_chrome_version()
    stable_branch = f"chromium/{stable.split('.')[2]}"

    branch = args.branch or stable_branch
    # Only claim a Chrome version for the branch it was actually cut from: an
    # explicitly requested older branch is not what stable ships today.
    version = stable if branch == stable_branch else ""

    output = subprocess.check_output(
        ["git", "ls-remote", "--heads", ANGLE_GIT_URL, f"refs/heads/{branch}"],
        text=True,
    ).strip()
    if not output:
        raise RuntimeError(f"Upstream ANGLE has no branch {branch}")
    commit = output.split()[0]

    emit_output(
        branch=branch,
        commit=commit,
        chrome_version=version,
        position=branch.rsplit("/", 1)[-1],
    )


# --------------------------------------------------------------------------- #
#  prepare-toolchain                                                           #
# --------------------------------------------------------------------------- #


def windows_sdk_root():
    if os.environ.get("WINDOWSSDKDIR"):
        return Path(os.environ["WINDOWSSDKDIR"])
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    return Path(program_files_x86) / "Windows Kits" / "10"


def installed_windows_sdks(sdk_root, *arches):
    """Returns the installed SDK versions usable for every `arches`, oldest first."""
    include_root = Path(sdk_root) / "Include"
    if not include_root.is_dir():
        raise FileNotFoundError(f"Windows SDK include directory not found: {include_root}")

    usable = []
    for candidate in include_root.iterdir():
        if not candidate.is_dir():
            continue
        if not re.fullmatch(r"\d+(?:\.\d+){3}", candidate.name):
            continue
        version = candidate.name
        required = [
            Path(sdk_root) / "Include" / version / "shared" / "sdkddkver.h",
            Path(sdk_root) / "Include" / version / "ucrt" / "stdio.h",
            Path(sdk_root) / "Include" / version / "um" / "Windows.h",
        ]
        for arch in arches:
            required += [
                Path(sdk_root) / "Lib" / version / "ucrt" / arch / "ucrt.lib",
                Path(sdk_root) / "Lib" / version / "um" / arch / "kernel32.lib",
            ]
        if all(path.is_file() for path in required):
            usable.append((tuple(int(part) for part in version.split(".")), version))
    return [version for _, version in sorted(usable)]


# Chromium pins the Windows SDK in two independent places: vs_toolchain.py
# gates on it, and setup_toolchain.py passes it to vcvarsall. Retargeting only
# the first leaves GN building an include path for an SDK that is not there.
SDK_VERSION_FILES = (
    ("build", "vs_toolchain.py"),
    ("build", "toolchain", "win", "setup_toolchain.py"),
)

SDK_VERSION_PATTERN = r"^SDK_VERSION\s*=\s*['\"]([^'\"]+)['\"]\s*$"


def pin_installed_sdk_version(source_root, arch):
    """Points Chromium's build at an SDK the runner actually has.

    Chromium pins an SDK version that is often newer than the one on GitHub's
    Windows images, and aborts when it is absent. Rather than installing a
    second multi-gigabyte SDK, retarget the build at the newest installed one.
    """
    pins = {}
    for parts in SDK_VERSION_FILES:
        path = Path(source_root).joinpath(*parts)
        matches = re.findall(SDK_VERSION_PATTERN, path.read_text(encoding="utf-8"), re.MULTILINE)
        if len(matches) != 1:
            raise RuntimeError(f"Expected exactly one SDK_VERSION in {path}, found {len(matches)}")
        pins[path] = matches[0]

    distinct = set(pins.values())
    if len(distinct) != 1:
        raise RuntimeError(f"Chromium pins disagreeing SDK versions: {sorted(distinct)}")
    pinned = distinct.pop()

    sdk_root = windows_sdk_root()
    # x86 as well as the target: GN instantiates every Windows toolchain it
    # knows about, so an SDK missing the x86 libraries fails `gn gen` even for
    # an x64-only build.
    arches = (arch, "x86")
    available = installed_windows_sdks(sdk_root, *arches)
    if not available:
        raise RuntimeError(f"No Windows SDK under {sdk_root} is usable for {', '.join(arches)}")
    print(f"Windows SDK root: {sdk_root}")
    print(f"Installed SDKs usable for {', '.join(arches)}: {', '.join(available)}")

    if pinned in available:
        print(f"Using the upstream-pinned Windows SDK {pinned}")
        return pinned

    selected = available[-1]
    print(f"::notice::Upstream pins Windows SDK {pinned}, which is not installed; using {selected}")
    for path in pins:
        text = path.read_text(encoding="utf-8")
        path.write_text(
            re.sub(
                SDK_VERSION_PATTERN,
                f"SDK_VERSION = '{selected}'",
                text,
                count=1,
                flags=re.MULTILINE,
            ),
            encoding="utf-8",
        )
        print(f"Retargeted {path}")
    return selected


def align_ntddi_version(source_root, sdk_version):
    """Keeps Chromium's target NTDDI macro one the chosen SDK actually defines.

    Chromium targets the NTDDI of the SDK it pins. Compiled against an older
    SDK the macro is simply undefined, so `NTDDI_VERSION` evaluates to 0 in the
    preprocessor and every Win10-era declaration disappears -- the build then
    fails on things as basic as FILE_INFO_BY_HANDLE_CLASS. Retarget it at the
    newest NTDDI the installed SDK knows about.
    """
    header = windows_sdk_root() / "Include" / sdk_version / "shared" / "sdkddkver.h"
    defined = {
        name: int(value, 16)
        for name, value in re.findall(
            r"^#define\s+(NTDDI_[A-Z0-9_]+)\s+(0x[0-9A-Fa-f]{8})\s*$",
            header.read_text(encoding="utf-8", errors="replace"),
            re.MULTILINE,
        )
    }
    if not defined:
        raise RuntimeError(f"No NTDDI macros found in {header}")

    path = Path(source_root) / "build" / "config" / "win" / "BUILD.gn"
    text = path.read_text(encoding="utf-8")
    matches = re.findall(r'"NTDDI_VERSION=(NTDDI_[A-Z0-9_]+)"', text)
    if len(matches) != 1:
        raise RuntimeError(f"Expected exactly one NTDDI_VERSION in {path}, found {len(matches)}")
    requested = matches[0]

    if requested in defined:
        print(f"Windows SDK {sdk_version} defines {requested}")
        return

    selected = max(defined, key=defined.get)
    print(f"::notice::{requested} is undefined in Windows SDK {sdk_version}; targeting {selected}")
    path.write_text(
        text.replace(f'"NTDDI_VERSION={requested}"', f'"NTDDI_VERSION={selected}"'),
        encoding="utf-8",
    )


def make_debugger_tools_optional(source_root):
    """Lets the build proceed without the SDK's optional Debuggers component.

    Chromium copies `dbghelp.dll` & friends next to every binary it links. That
    component is not part of the SDK installed on GitHub's Windows images, and
    the DLLs are a debugging convenience we never ship, so the copy becomes a
    no-op when they are absent.
    """
    path = Path(source_root) / "build" / "vs_toolchain.py"
    text = path.read_text(encoding="utf-8")
    if DEBUGGER_PATCH_MARKER in text:
        return

    tree = ast.parse(text, filename=str(path))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_CopyDebugger"
    ]
    if not functions:
        if "dbghelp.dll" not in text:
            print("Chromium no longer copies debugger tools; no patch needed")
            return
        raise RuntimeError("Chromium debugger copying changed; refusing to patch blindly")
    if len(functions) != 1:
        raise RuntimeError("Found multiple _CopyDebugger definitions")

    function = functions[0]
    argument_names = {argument.arg for argument in function.args.args}
    if "target_cpu" not in argument_names:
        raise RuntimeError("_CopyDebugger no longer takes target_cpu")

    body = function.body
    first = body[0]
    is_docstring = (
        isinstance(first, ast.Expr)
        and isinstance(first.value, ast.Constant)
        and isinstance(first.value.value, str)
    )
    anchor = body[1] if (is_docstring and len(body) > 1) else first
    insert_at = first.end_lineno if is_docstring else first.lineno - 1

    lines = text.splitlines(keepends=True)
    anchor_line = lines[anchor.lineno - 1]
    indent = anchor_line[: len(anchor_line) - len(anchor_line.lstrip(" "))]
    if not indent:
        raise RuntimeError("Could not determine _CopyDebugger body indentation")

    patch = (
        f"{indent}# {DEBUGGER_PATCH_MARKER}.\n"
        f"{indent}_nucleus_sdk_dir = SetEnvironmentAndGetSDKDir()\n"
        f"{indent}if not _nucleus_sdk_dir or not os.path.isfile(\n"
        f"{indent}        os.path.join(_nucleus_sdk_dir, 'Debuggers', target_cpu, 'dbghelp.dll')):\n"
        f"{indent}    print('Skipping absent Windows debugger tools for %s' % target_cpu)\n"
        f"{indent}    return\n"
    )
    lines.insert(insert_at, patch)
    patched = "".join(lines)
    ast.parse(patched, filename=str(path))  # fail loudly rather than at build time
    path.write_text(patched, encoding="utf-8")
    print("Made the optional Windows debugger tools non-fatal")


def cmd_prepare_toolchain(args):
    selected = pin_installed_sdk_version(args.source_root, args.arch)
    align_ntddi_version(args.source_root, selected)
    make_debugger_tools_optional(args.source_root)
    emit_output(windows_sdk=selected)


# --------------------------------------------------------------------------- #
#  gn-args                                                                     #
# --------------------------------------------------------------------------- #


def format_gn_value(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(value)


def cmd_gn_args(args):
    gn_args = dict(COMMON_GN_ARGS)
    gn_args.update(WIN32_GN_ARGS)
    gn_args["target_cpu"] = args.arch

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"{key} = {format_gn_value(gn_args[key])}\n" for key in sorted(gn_args))
    output.write_text(body, encoding="utf-8")
    print(body)

    if args.json_output:
        Path(args.json_output).write_text(
            json.dumps(gn_args, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


# --------------------------------------------------------------------------- #
#  package                                                                     #
# --------------------------------------------------------------------------- #

SHIPPED_DLLS = ("libEGL.dll", "libGLESv2.dll")


def sha256_of(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def cmd_package(args):
    build_dir = Path(args.build_dir)
    staging = Path(args.staging)
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    for name in SHIPPED_DLLS:
        source = build_dir / name
        if not source.is_file():
            raise FileNotFoundError(f"Missing build output: {source}")
        shutil.copy2(source, staging / name)

    shutil.copy2(Path(args.source_root) / "LICENSE", staging / "LICENSE")

    manifest = {
        "angleBranch": args.branch,
        "angleCommit": args.commit,
        "chromeStableVersion": args.chrome_version,
        "platform": "win32",
        "arch": args.arch,
        "generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "gnArgs": json.loads(Path(args.gn_args_json).read_text(encoding="utf-8")),
        "files": {
            name: {
                "bytes": (staging / name).stat().st_size,
                "sha256": sha256_of(staging / name),
            }
            for name in SHIPPED_DLLS
        },
    }
    (staging / "angle-build.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    archive = Path(args.output)
    archive.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for entry in sorted(staging.iterdir()):
            bundle.write(entry, entry.name)

    for name in SHIPPED_DLLS:
        info = manifest["files"][name]
        print(f"{name}: {info['bytes']} bytes, sha256 {info['sha256']}")
    print(f"archive: {archive} ({archive.stat().st_size} bytes)")
    emit_output(archive=archive.name)


# --------------------------------------------------------------------------- #
#  verify                                                                      #
# --------------------------------------------------------------------------- #


def pe_imported_dlls(data):
    """Returns the DLL names in a PE image's import directory.

    A tiny reader rather than a dependency: CI only needs the import table, and
    `dumpbin` is not on PATH outside a Visual Studio developer prompt.
    """
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    if data[pe_offset : pe_offset + 4] != b"PE\0\0":
        raise ValueError("Not a PE image")
    coff = pe_offset + 4
    number_of_sections = struct.unpack_from("<H", data, coff + 2)[0]
    optional_size = struct.unpack_from("<H", data, coff + 16)[0]
    optional = coff + 20
    magic = struct.unpack_from("<H", data, optional)[0]
    if magic == 0x20B:  # PE32+
        directories = optional + 112
    elif magic == 0x10B:  # PE32
        directories = optional + 96
    else:
        raise ValueError(f"Unknown optional header magic 0x{magic:x}")
    import_rva, import_size = struct.unpack_from("<II", data, directories + 8)
    if not import_rva or not import_size:
        return []

    sections = []
    section_table = optional + optional_size
    for index in range(number_of_sections):
        entry = section_table + index * 40
        virtual_size, virtual_address, raw_size, raw_pointer = struct.unpack_from(
            "<IIII", data, entry + 8
        )
        sections.append((virtual_address, max(virtual_size, raw_size), raw_pointer))

    def to_offset(rva):
        for virtual_address, size, raw_pointer in sections:
            if virtual_address <= rva < virtual_address + size:
                return raw_pointer + (rva - virtual_address)
        raise ValueError(f"RVA 0x{rva:x} is outside every section")

    names = []
    cursor = to_offset(import_rva)
    while True:
        descriptor = data[cursor : cursor + 20]
        if len(descriptor) < 20 or descriptor == b"\0" * 20:
            break
        name_rva = struct.unpack_from("<I", descriptor, 12)[0]
        if not name_rva:
            break
        start = to_offset(name_rva)
        end = data.index(b"\0", start)
        names.append(data[start:end].decode("ascii").lower())
        cursor += 20
    return names


PE_MACHINE = {"x64": 0x8664, "arm64": 0xAA64}


def pe_machine(data):
    pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
    return struct.unpack_from("<H", data, pe_offset + 4)[0]


def cmd_verify(args):
    failures = []
    with zipfile.ZipFile(args.archive) as bundle:
        entries = set(bundle.namelist())
        for expected in (*SHIPPED_DLLS, "LICENSE", "angle-build.json"):
            if expected not in entries:
                failures.append(f"{args.archive}: missing {expected}")

        for name in SHIPPED_DLLS:
            if name not in entries:
                continue
            data = bundle.read(name)

            machine = pe_machine(data)
            if machine != PE_MACHINE[args.arch]:
                failures.append(
                    f"{name}: machine 0x{machine:x}, expected 0x{PE_MACHINE[args.arch]:x} for {args.arch}"
                )

            imports = set(pe_imported_dlls(data))
            unexpected = sorted(
                name
                for name in imports - ALLOWED_IMPORTS
                if not name.startswith(ALLOWED_IMPORT_PREFIXES)
            )
            if unexpected:
                failures.append(f"{name}: unexpected imports {', '.join(unexpected)}")

            # ANGLE keeps assert __FILE__ strings, so a backend that survived the
            # GN flags leaves its source paths behind.
            text = data.decode("latin-1")
            for marker in FORBIDDEN_SOURCE_MARKERS:
                for spelling in (marker, marker.replace("/", "\\")):
                    if spelling in text:
                        failures.append(f"{name}: disabled backend still present ({marker})")
                        break

            print(f"{name}: {len(data)} bytes, imports {', '.join(sorted(imports))}")

    if failures:
        for failure in failures:
            print(f"::error::{failure}")
        sys.exit(1)
    print("Archive layout, architecture, imports and disabled backends all verified")


# --------------------------------------------------------------------------- #


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    resolve = subparsers.add_parser("resolve-ref", help="Pick the ANGLE branch to build")
    resolve.add_argument("--branch", default="", help="Override, e.g. chromium/8037")
    resolve.set_defaults(handler=cmd_resolve_ref)

    toolchain = subparsers.add_parser("prepare-toolchain", help="Adapt Chromium's Windows toolchain")
    toolchain.add_argument("--source-root", required=True)
    toolchain.add_argument("--arch", required=True, choices=("x64", "arm64"))
    toolchain.set_defaults(handler=cmd_prepare_toolchain)

    gn = subparsers.add_parser("gn-args", help="Write the trimmed args.gn")
    gn.add_argument("--arch", required=True, choices=("x64", "arm64"))
    gn.add_argument("--output", required=True)
    gn.add_argument("--json-output", default="")
    gn.set_defaults(handler=cmd_gn_args)

    package = subparsers.add_parser("package", help="Zip the shipped DLLs")
    package.add_argument("--build-dir", required=True)
    package.add_argument("--source-root", required=True)
    package.add_argument("--staging", required=True)
    package.add_argument("--output", required=True)
    package.add_argument("--arch", required=True, choices=("x64", "arm64"))
    package.add_argument("--branch", required=True)
    package.add_argument("--commit", required=True)
    package.add_argument("--chrome-version", default="")
    package.add_argument("--gn-args-json", required=True)
    package.set_defaults(handler=cmd_package)

    verify = subparsers.add_parser("verify", help="Check the archive we are about to publish")
    verify.add_argument("--archive", required=True)
    verify.add_argument("--arch", required=True, choices=("x64", "arm64"))
    verify.set_defaults(handler=cmd_verify)

    args = parser.parse_args()
    args.handler(args)


if __name__ == "__main__":
    main()
