# Nucleus ANGLE prebuilts

This fork of [google/angle](https://github.com/google/angle) publishes the ANGLE
runtime DLLs that the Nucleus Tao Windows backend ships, built for what Nucleus
actually uses and nothing else.

## Branch layout

| Branch | Contents |
| --- | --- |
| `nucleus` (default) | This harness, and only this harness. Orphan branch — it shares no history with ANGLE. |
| `main` | An untouched mirror of upstream `main`, kept current by fast-forward. |
| `chromium/NNNN` | Untouched mirrors of ANGLE's release branches. These are what gets built. |

Keeping the harness off `main` is what makes the upstream sync a fast-forward
forever: nothing we write ever lands on a branch upstream also writes to, so
there is no merge and nothing to conflict. Any patch we ever need to carry on
ANGLE itself goes on top of a `chromium/NNNN` branch, where it is one visible
commit.

The default branch has to be the one carrying the workflows — GitHub only runs
`schedule` triggers from the default branch — which is why it is `nucleus` and
not `main`.

## Why this exists

Nucleus used to extract `libEGL.dll` / `libGLESv2.dll` from a pinned Electron
release: 292 MB of download for 8.5 MB of DLL, and a `libGLESv2.dll` carrying
the Vulkan, desktop-GL/WGL and D3D9 backends, SwiftShader, the OpenCL frontend
and the GLES1 emulation — none of which Nucleus can reach.

The Tao backend only ever asks for one thing (`nucleus_tao_gl.c`):

```c
EGL_PLATFORM_ANGLE_TYPE_ANGLE, EGL_PLATFORM_ANGLE_TYPE_D3D11_ANGLE
EGL_PLATFORM_ANGLE_DEVICE_TYPE_ANGLE, HARDWARE → fallback D3D_WARP
```

plus `EGL_EXT_device_query` (to borrow ANGLE's D3D11 device) and
`EGL_ANGLE_d3d_texture_client_buffer` (`TextureView`'s zero-copy import).
DirectComposition is driven by Nucleus itself in
`nucleus_tao_windows_overlay_dcomp.cpp`, so ANGLE never sees an
`IDCompositionSurface` as an EGL native window.

So the build turns everything else off at the GN level rather than trimming a
browser's binary after the fact.

## What is published

Every release carries one archive per architecture:

```
angle-<position>-win32-x64.zip
angle-<position>-win32-arm64.zip
  libEGL.dll
  libGLESv2.dll
  LICENSE            # ANGLE, BSD 3-Clause
  angle-build.json   # branch, commit, Chrome version, full GN args, per-file SHA-256
SHA256SUMS.txt
```

No import libraries, no PDBs, no `d3dcompiler_47.dll`: Nucleus resolves ANGLE
through `LoadLibraryW` + `GetProcAddress` and never links against it, and the
D3D11 backend picks up the `d3dcompiler_47.dll` that ships in `System32` on
every supported Windows version.

`<position>` is the Chromium branch number, so `angle-8037` is the ANGLE revision
Chrome 154 stable shipped.

## Which revision gets built

`resolve-ref` maps the current stable Chrome `M.0.BBBB.P` to ANGLE's
`chromium/BBBB` release branch — the revision that has already been through a
full Chrome stable cycle, rather than today's `main`.

## Layout

| Path | Role |
| --- | --- |
| `.github/workflows/sync-upstream.yml` | Monthly: fast-forward `main`, point `chromium/NNNN` at upstream, dispatch a build. A fork shares its parent's object store, so this is all refs API calls. |
| `.github/workflows/build.yml` | Build, package, verify, smoke-test, release. Two checkouts: ANGLE at the release branch, and this harness from the default branch. |
| `.github/nucleus/angle_prebuilt.py` | Every CI step, each runnable by hand. |
| `.github/nucleus/smoke.cpp` | Walks Nucleus' own EGL path against the packaged DLLs. |

## Building by hand

On a Windows machine with [depot_tools](https://chromium.googlesource.com/chromium/tools/depot_tools.git)
on `PATH`, from a `gclient`-synced ANGLE checkout:

```bash
python3 angle_prebuilt.py prepare-toolchain --source-root . --arch x64
python3 angle_prebuilt.py gn-args --arch x64 \
    --output out/Release/args.gn --json-output /tmp/gn-args.json
gn gen out/Release && autoninja -C out/Release libEGL libGLESv2
```

`prepare-toolchain` adapts Chromium's Windows toolchain to a hosted runner. It
retargets the SDK version Chromium pins — in **both** `build/vs_toolchain.py`
and `build/toolchain/win/setup_toolchain.py`, which carry independent copies —
at an SDK that is actually installed, retargets `NTDDI_VERSION` at a macro that
SDK declares (an undefined one silently evaluates to 0 and hides every Win10-era
declaration), and makes the optional SDK Debuggers component non-fatal.

## Verification

`verify` runs before anything is published and fails the build if a disabled
backend came back — it checks the PE machine type, rejects any imported DLL
outside a known list (Windows API sets aside), and greps the binary for the
`__FILE__` strings ANGLE's asserts leave behind (`libANGLE/renderer/vulkan`,
`libANGLE/renderer/gl/`, `libANGLE/renderer/d3d/d3d9`, `libANGLE/renderer/wgpu`).

`smoke.cpp` then runs against the archive's own DLLs, in a directory holding
nothing else, so anything the build tree happened to provide is missing there.

## License

ANGLE is BSD 3-Clause. The upstream `LICENSE` lives on `main` and on every
`chromium/NNNN` branch, and is redistributed inside every archive.
