# AssetStudio Mobile

An **Android Unity asset browser and texture replacement tool** ported from [AssetStudio](https://github.com/Perfare/AssetStudio). It is built with **Kotlin + Jetpack Compose (Material Design 3)**. The core parsing layer is a line-by-line C# port, and texture decoders compile AssetStudio's C++ implementations directly through **NDK + JNI**.

## Stability safeguards (v1.0.1)

Android application heaps are much smaller than desktop heaps, so the parsing engine includes several layers of protection. An isolated malformed data item should not crash the entire app:

- **Array-length validation:** Before reading length prefixes for Unity arrays (float/int/vector/object arrays, etc.; 118 locations), the parser verifies `count × elemSize ≤ remaining file bytes`. An invalid length caused by a misaligned parse (for example, `0xFE000000`, approximately 4 GB) raises `EOFException` instead of triggering a huge allocation.
- **Per-object fault isolation:** If parsing an individual asset fails (including with `OutOfMemoryError`), the error is recorded and that asset is skipped. Other assets continue loading, and failure details appear in the load summary.
- **File-size preflight:** Files larger than 768 MB are rejected before reading to prevent predictable out-of-memory failures.
- **Folder batch-loading safeguards (v1.7.3):** See below.

## Load an entire folder (v1.7.3; file limit increased in v1.7.4)

The home screen provides a **Load Folder** button: choose a directory → automatically scan for asset files → review the file list → load everything in one operation, without selecting files one by one.

| Safeguard | Details |
| --- | --- |
| Smart filtering | Uses an extension allowlist (`bundle`, `unity3d`, `assetbundle`, `assets`, `zip`, `unitypackage`, `resS`, etc.) and probes magic bytes for extensionless files (UnityFS/UnityWeb/UnityRaw/UnityArchive/KH encrypted headers/gzip). Media and document files are excluded automatically. |
| File-count limit (raised in v1.7.4) | Up to 5,000 files per operation (previously 500). Memory budget is the real constraint, not the number of files: game-mod packs often contain thousands of small bundles of only a few dozen KB each, whose combined size remains within budget. The limit is a final safeguard against pathological selections (such as the storage root); if truncation is required, smaller files are retained first. |
| Two-stage real-heap safeguards (v1.7.5) | File bytes account for only a small part of memory use; objects can expand several times after construction. Earlier versions estimated memory from file sizes alone, so object graphs from thousands of files could exhaust the heap and cause allocation failures in vendor system threads (such as ColorOS `oplus_force_gc_t`). The read stage now checks actual heap usage every 8 files (with a GC-and-recheck step to avoid false positives) and stops above 60%. Before constructing each file's objects, the construction stage checks again and stops creating remaining objects above 78%. Objects already built remain browsable, and the summary explains the stop. |
| Type-tree pruning (v1.7.6) | After objects have been constructed, type trees are discarded for files without `MonoBehaviour` objects. Type trees are a major source of parsing-structure memory use (thousands of nodes and strings) and are no longer needed after the hand-written parser finishes. Trees for files containing `MonoBehaviour` are retained for detail previews and dump exports. Pruning runs after both the first construction pass and each **Continue Loading** batch, substantially reducing memory use. |
| Soft memory limit | Calculated dynamically from the app heap (with `largeHeap`, a 512 MB heap commonly allows about 280 MB of resident data). Resident bytes are checked after each file; once the budget is exhausted, remaining large files are skipped instead of risking an OOM. |
| Per-file isolation | Each file is handled in its own `try/catch(Throwable)`. Corrupt, encrypted, or oversized files record a failure reason without interrupting the remaining files. |
| Cancellation | A progress indicator shows the current index, total count, and filename throughout loading. Loading can be cancelled at any time, preserving assets already loaded. |
| Performance safeguards | Batch mode delays object construction and cross-object linking to avoid per-file global post-processing with O(N²) cost. The shared parsing stage has its own status indicator, and smaller files are processed first so progress appears quickly. |
| Transparent summary | On completion, the app reports successful loads, cancellation, memory-limit stops, files skipped due to the memory budget, failure details, and estimated memory usage. |

## Seamless loading (unified list in v1.9.0 → batches fully hidden in v1.10.0)

In v1.8.x, batches were a user-facing concept (batch cards, selecting combinations, merged views, and restricted select-all behavior), forcing users to understand memory management just to see all assets. Starting with v1.9.0, the list is decoupled from memory. In v1.10.0, batches are completely hidden from the UI and remain an internal implementation detail.

From the user's perspective, there is only one operation: **choose a folder → get one complete asset library**.

```text
Internal (hidden from the user):
  Fill batch 1 using an independent parser until the memory safety threshold is reached
    → evict old batch objects while keeping lightweight indexes resident
    → fill batch 2 → … until all files have been processed

User-facing experience:
  Progress: Loading 12/34: xxx.unity3d
  Completion: “34 files · 8,512 items” → View Assets (one entry point, one complete list)
  Assets that do not fit in memory: marked “On disk” → tapping one loads only its source file from disk (about 1 second)
```

| Feature | Details |
| --- | --- |
| Unified asset list (v1.9.0) | The list is the union of every asset in every file. Resident entries keep object references; entries from evicted batches retain lightweight indexes (name/type/size/pathID), independent of memory state, so no entries disappear. |
| Separate indexes and objects (v1.9.0) | `AssetItem.obj` can be null. A null object means the entry is lazy and stored on disk. Browsing, searching, filtering, and statistics require little additional memory. |
| On-demand loading (completed in v1.10.0) | Opening details or exporting a lazy entry loads only its **single source file** from disk, subject to the 60%/78% memory safeguards. On success, the list entry is populated and its preview becomes available. On failure, the reason is displayed and the operation can be retried. |
| On-demand cache limit (v1.10.0) | Up to 48 reloaded objects stay resident. Above that limit, the oldest half are evicted and returned to lazy state, preventing long browsing sessions from pinning released memory again. |
| Grouped export (v1.10.0) | Batch export handles resident entries first without disk reads. Lazy entries are grouped by source file, so each file is read only once rather than once per asset. |
| Invisible batching (v1.10.0) | Progress, summaries, and messages refer only to files, assets, and disk-backed entries. The home screen has no batch cards, selection controls, or merge button; only **View Assets** and **Clear** remain. |
| Crash-prevention safeguards | The two-stage 60%/78% heap checks, type-tree pruning, and GC/recheck logic apply independently to every internal batch. Between batches, objects are evicted, GC is allowed to run, and heap usage is checked again so the chain can terminate safely. |

Batches loaded through the SAF picker do not have persistent URI permissions, so their data cannot be loaded back on demand from disk. These batches are therefore never evicted and remain resident.

## Batch export (v1.7.5)

Long-press an item in the asset list to enter multi-select mode → select assets of any type, not just textures → tap **Batch Export** in the upper-right corner → choose an output folder. Output formats are selected by asset type: textures/Sprites → PNG, models → OBJ, audio → WAV, video → raw data, materials/shaders/`MonoBehaviour` → text dumps, and fonts → TTF. Export can be cancelled while in progress. A failed asset is skipped and export continues. Garbage collection is triggered every 8 assets to prevent texture-decoding memory peaks from accumulating. Invalid filename characters are removed, and duplicate names receive numeric suffixes.

## Texture orientation fix (v1.7.7)

Fixed the upside-down orientation previously seen in texture previews, exported PNGs, and Sprite crops. Unity texture data follows OpenGL-style storage (`row 0` is the **bottom** of the image), whereas Android Bitmap and PNG use `row 0` for the **top**. The original C# AssetStudio converts between these conventions with `RotateFlip(RotateNoneFlipY)` in `GetImage()`. The port's shared `decodeBitmap` path already flipped the image, but the direct decoding paths for previews, exports, and replacement omitted the conversion.

All paths now use a single `PixelFlip` implementation. Decoding (preview/export/Sprite) flips once from bottom-first to top-first; writing a replacement flips in the opposite direction, from top-first back to bottom-first. The operations are inverses, so the full **export → edit → replace** cycle preserves the correct orientation and the replaced texture is no longer upside down in-game.

## Feature overview

| Feature | Details |
| --- | --- |
| Load Unity assets | `.unity3d`, `.assetbundle`, `.bundle` (UnityFS, UnityWeb/Raw v6, and legacy Web/Raw), `.assets`, `.zip`, and `.unitypackage` structures. |
| **Encrypted bundle support** | Automatically decrypts all three AB Manager variants (UnityKHFS/UnityKHNFS/UnityKH1FS), including LZ4-compressed manifests and data-block alignment. Automatic detection handles leading junk bytes (stripping custom header offsets) and single-byte XOR encryption (detecting whether the entire file or just its header is encrypted). Manual decryption supports repeating XOR keys and AES-ECB/AES-CBC (128/192/256-bit keys, hexadecimal keys, and prefix offsets). |
| Asset browsing | Filter by type (Texture, Text, Audio, Mesh, Sprite, `MonoBehaviour`, etc.), search by keyword, and locate assets by PathID. |
| Copy asset information | Long-press the **Asset Information** card on the detail page to select and copy any range of text, including PathID, filename, and container path. |
| Texture preview | Decodes 30+ texture formats into bitmap previews and supports mipmap-level selection. Adaptive scaling enlarges small textures while preserving aspect ratio and pixel clarity (nearest-neighbor scaling), ensures very wide strips remain tall enough to inspect, and provides a 25%–800% scale slider that resets when switching textures. Pan in both directions to inspect content outside the viewport. |
| **Mesh preview** | Actual 3D rendering via a pure-Kotlin software rasterizer with no OpenGL dependency: per-pixel z-buffering, face normals oriented toward the camera, Lambert directional lighting plus ambient lighting, automatic rotation, and drag-to-rotate controls. Very large meshes (over 40,000 triangles) are downsampled automatically to keep rendering responsive. |
| **Renderer component preview and replacement (v1.7.1)** | The detail page for `SkinnedMeshRenderer` / `MeshFilter` renders the referenced mesh in 3D by resolving its `m_Mesh` PPtr, including vertex/triangle statistics. Information cards show bone counts, material slots, and blend-shape weights. The **Related Assets** card opens the Mesh detail page, and **Replace Mesh** swaps the referenced Mesh while preserving bone bindings and material slots; cross-file references are supported too. |
| **MCP renderer-component support (v1.7.2)** | The MCP server supports renderer components as fully as the app UI. `list_assets` can filter by `SkinnedMeshRenderer` / `MeshFilter`; details include bone count, material-slot count, blend-shape-weight count, and referenced-mesh statistics (raw PPtr and resolved statistics). `dump_render_asset` outputs mesh references, bone lists, material slots, and blend-shape weights as readable text. `export_asset` can export a renderer-referenced mesh directly as OBJ or export a text dump as TXT. |
| Export | `Texture2D` → PNG; `TextAsset` and `MonoBehaviour` → text; `AudioClip` → WAV; any asset → raw-byte dump. MIME types are selected by asset type so the saved extension matches the content (textures always use `.png`). |
| **Texture replacement** | Select a new image of any dimensions → re-encode → rewrite the SerializedFile → repack the Bundle. The save dialog defaults to the original filename, so the output can replace the corresponding game file directly. Saved files can be reopened in the app for preview verification. Directory-table fields follow Unity's standard order and the header's `fileSize` is repaired in big-endian order, regardless of the file extension. Older output files with an incorrectly byte-ordered `fileSize` are repaired automatically when loaded again. |
| **Batch texture replacement (v1.7.0)** | Long-press an asset to enter multi-select mode → select multiple textures → choose an image folder → automatically match filenames while ignoring case, separators, and extensions. Unity `(instance)` suffixes can be matched, while ambiguous matches are not applied. Replace all in one operation. A failed replacement does not stop the others; one output is produced per container, with multiple containers packaged into a ZIP. Memory usage is controlled per texture to handle large folders. |
| **Native-format ASTC encoding (v1.7.0)** | A pure-Kotlin Khronos ASTC LDR encoder supports 4×4, 5×5, 6×6, and 8×8 blocks. Textures originally encoded as ASTC **remain in ASTC format** after replacement (previously they fell back to RGBA32, doubling bundle size). Includes ISE trit/quint encoding, dynamic endpoint quantization ranges, PCA-based principal-axis endpoint selection, and weight infill. The layout was cross-validated against the independent `texture2ddecoder` decoder (28/28 cases passed). |

## Texture replacement pipeline (core feature)

The complete pipeline produces an output file that can be placed back into the game:

```text
User image (PNG/JPG)
  ─→ TextureEncoder (generate mip chain + encode in the target format)
  ─→ Texture2DObjectRewriter (rebuild Texture2D object bytes and synchronize width/height/format/mip/data-size fields)
  ─→ SerializedFileRewriter (reorder object data and repair object-table byteStart/byteSize and header fileSize)
  ─→ BundleRepacker (repack UnityFS with LZ4 block compression and rebuild blocksInfo/directory table)
  ─→ Save as a usable .bundle / .unity3d / .assets / .zip file
```

### Replacement capability matrix

| Item | Support |
| --- | --- |
| Preserve original format | RGBA32 / ARGB32 / RGB24 / BGRA32 / Alpha8 / R8 / RGB565 / ARGB4444 / DXT1 / DXT5 / **ASTC 4×4, 5×5, 6×6, 8×8 (RGB/RGBA, v1.7.0)** |
| Automatic fallback | Other formats are rewritten as RGBA32, with the format field updated so the game can load the texture normally. |
| Mipmaps | Generated automatically with a box filter, preserving the original number of mip levels. |
| Dimensions | May differ from the original (for example, 512 → 1024); all object fields are updated. DXT formats require dimensions divisible by 4, otherwise the encoder falls back to RGBA32. |
| Containers | UnityFS Bundle, UnityWeb/Raw v6, standalone `.assets`, embedded in `.zip`, and nested WebFile (`.unity3d`). |
| Limitations | Repacking legacy Bundles below v6 is not yet supported. Replacing a nested Crunch texture converts it to regular texture data. |

## Native texture decoding formats (NDK)

BC1–BC7 (DXT1/3/5, BC4/5/6H/7), ETC1, ETC2 (RGB/A1/A8), EAC R/RG (including signed variants), ASTC (all block sizes), PVRTC (2/4 bpp), ATC, Crunch, and Unity Crunch.

Uncompressed formats (RGBA32/RGB24/RGB565/ARGB4444/Alpha8/R8/R16, etc.) and palette formats (4/8-bit indices with RGB/RGBA palettes) are decoded in the pure-Kotlin layer.

## Project structure

```text
app/src/main/
├── java/com/assetstudio/mobile/
│   ├── core/                      # Line-by-line Kotlin port of AssetStudio's C# core
│   │   ├── io/                    # EndianBinaryReader / FileReader / Gzip / Brotli
│   │   ├── bundle/                # BundleFile / WebFile / LZ4 / LZMA / 7zLZMA
│   │   ├── serialized/            # SerializedFile / TypeTree / CommonString
│   │   ├── classes/               # Texture2D / Sprite / Mesh / MonoBehaviour and 30+ asset classes
│   │   ├── texture/               # TextureConverter (decode dispatch) + NativeDecoder (JNI declarations)
│   │   ├── manager/               # AssetsManager (loading/parsing/container hierarchy and SourceRef tracking)
│   │   └── math/                  # Vector / Matrix / Quaternion / Rect
│   ├── replace/                   # ★ Texture replacement pipeline (encoding/object rewrite/file rewrite/Bundle repacking)
│   ├── export/                    # PNG / text / dump exports
│   ├── ui/                        # Compose MD3 UI (home/list/details)
│   └── MainActivity.kt / MainViewModel.kt / App.kt
└── cpp/                           # Native NDK decoders (AssetStudio C++ sources + JNI bridge)
    ├── texture2ddecoder_jni.cpp
    └── decoder/                   # astc / bcn / etc / pvrtc / atc / crunch / unitycrunch
```

## Usage

1. Install the APK (`app/build/outputs/apk/debug/app-debug.apk`). Android 8.0 or later is required; `arm64` is recommended.
2. On the home screen, tap **Select Files** to choose `.bundle`, `.unity3d`, `.assets`, or `.zip` files (multiple selection is supported). For files whose version metadata has been stripped, enter the Unity version before loading, such as `2021.3.20f1`. Alternatively, tap **Load Folder** to scan and load an entire directory, including subfolders, with progress reporting, cancellation, and OOM safeguards. When not all data can remain in memory, the app uses the internal batch-and-index mechanism described above; the user still sees one unified asset list.
3. In the asset list, filter with type chips or search by keyword, then tap an asset to open its detail page.
4. **Export:** Tap **Export** on the detail page. The save dialog supplies the appropriate extension (`.png` for textures, `.wav` for audio, `.txt` for text).
5. **Mesh preview:** The Mesh detail page loads a wireframe preview automatically. Drag to rotate it.
6. **Replace a texture:** On a `Texture2D` detail page, tap **Replace Texture** → choose an image → confirm. The app attempts to preserve the original format by default; you can force RGBA32 if needed. After replacement succeeds, tap **Save As**. The dialog defaults to the original filename, and the saved file can be used to replace the corresponding file in the game.

## Build

```bash
# Requires JDK 17, Android SDK 34, NDK 26, and CMake 3.22+
export JAVA_HOME=<jdk17> ANDROID_HOME=<sdk>
./gradlew assembleDebug
```

Supported ABIs: `arm64-v8a` / `armeabi-v7a` / `x86` / `x86_64`. `minSdk` is 26 (Android 8.0), and `targetSdk` is 34.

## Differences from the original AssetStudio

- File access uses SAF (Storage Access Framework) and an in-memory model rather than scanning filesystem directories. External `.resS` data is loaded on demand through callbacks.
- The app does not use runtime code generation for TypeTree-dependent deserialization. `MonoBehaviour` objects are parsed with the built-in TypeTree and displayed as tree-structured text.
- Texture replacement is not built into the original AssetStudio GUI (the original workflow requires a tool such as UABE); this project implements the complete replacement pipeline internally.
- Large assets such as Mesh and Audio are exported as raw data or in simplified formats. OBJ export has not been ported for general Mesh export.

## License

AssetStudio is licensed under the MIT License, and this project follows the same license. For learning and research only; do not use it to distribute infringing assets.

---

Author: Zuimo

## Automatic GitHub Actions build and translation

The workflow in `.github/workflows/build.yml` runs on pushes and pull requests. It translates Chinese text found in supported source and documentation files to English, checks that no CJK text remains in the files being scanned, and then builds both debug and release APKs.

When a workflow run completes, open **Actions → the completed run → Artifacts** to download:

- `AssetStudioMobile-APKs` — debug and release APKs.
- `AssetStudioMobile-translated-source` — a ZIP archive of the translated source tree.

To run it manually, open **Actions → Translate Chinese and Build APK → Run workflow**. Enable `commit_translations` only if you want the workflow to commit translated files back to the selected branch. The translator uses the open-source Argos Translate Chinese-to-English model locally. GitHub Actions downloads the model on the first run and caches it for later runs; translating source strings does not make per-string requests to Google Translate. Translation quality may be less fluent than a cloud translation service, so review user-facing text before release.
