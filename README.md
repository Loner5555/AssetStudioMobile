# AssetStudio Mobile

基于 [AssetStudio](https://github.com/Perfare/AssetStudio) 移植的 **Android 端 Unity 资源浏览与贴图替换工具**，使用 **Kotlin + Jetpack Compose（Material Design 3）** 开发，核心解析层为 C# 逐行移植，贴图解码器通过 **NDK + JNI** 直接编译 AssetStudio 的 C++ 实现。

## 稳定性设计（v1.0.1）

Android 应用堆远小于桌面端，解析引擎内置多层防护，单点数据异常不会导致闪退：

- **数组长度校验**：所有 Unity 数组（float/int/vector/对象数组等 118 处）读取长度前缀时强制校验 `count × elemSize ≤ 文件剩余字节`，解析错位读到的垃圾长度（如 0xFE000000 ≈ 4GB）会抛 `EOFException` 而非直接分配内存
- **对象级容错**：单个资产解析失败（含 `OutOfMemoryError`）仅记录错误并跳过，其余资产照常加载，失败详情显示在加载结果中
- **文件大小预检**：超过 768MB 的文件在读取前直接提示拒绝，避免必然的内存溢出
- **文件夹批量加载防护（v1.7.3）**：见下

## 文件夹整包加载（v1.7.3，v1.7.4 放宽数量上限）

主页新增「加载文件夹」按钮：选一个目录 → 自动扫描全部资源文件 → 确认清单 → 一键批量加载，不再需要逐个勾选文件。

| 防护措施 | 说明 |
| --- | --- |
| 智能筛选 | 扩展名白名单（bundle/unity3d/assetbundle/assets/zip/unitypackage/resS 等）+ 无扩展名文件头部魔数嗅探（UnityFS/UnityWeb/UnityRaw/UnityArchive/KH 加密头/gzip），媒体与文档自动排除 |
| 数量上限（v1.7.4 放宽） | 单次最多 5000 个文件（原 500）：真正的约束是内存预算而非文件数，游戏美化包常见数千个十几 KB 的小 bundle，总大小远小于预算即全部加载；上限仅作病态目录（误选存储根目录等）的最后保险，截断保留较小的 |
| 双阶段真实堆防线（v1.7.5） | 文件字节只是内存小头，对象构造后会膨胀数倍——v1.7.4 及之前只按文件字节估算，数千文件的对象图可撑满堆导致厂商系统线程（ColorOS oplus_force_gc_t 等）分配失败闪退。现改为**读取阶段**每 8 个文件检查真实堆用量（GC 复测防误判），超 60% 即停止读取；**对象构造阶段**每文件构造前检查，超 78% 即停止构造剩余对象（已构造部分可正常浏览，汇总中说明） |
| 类型树剥离（v1.7.6） | 对象构造完成后，剥离不含 MonoBehaviour 文件的类型树（解析结构的内存大头，数千节点×字符串；对象构造用手写解析器，完成后即无用；含 MonoBehaviour 的保留以支持详情预览与导出转储），首轮与「继续加载」每批构造后都执行，内存大幅回落 |
| 内存软上限 | 按设备应用堆动态计算（`largeHeap` 下通常 512MB 堆 → 约 280MB 驻留）；逐文件加载后实时核对已驻留字节，预算耗尽自动跳过剩余大文件而不是 OOM |
| 单文件隔离 | 逐个文件 try/catch(Throwable)：损坏、加密、超限的文件只记录失败原因，绝不中断其余文件 |
| 可取消 | 加载全程显示进度条（当前序号/总数/文件名），随时取消并保留已加载部分 |
| 性能保护 | 批量模式延迟对象构造与关联（避免逐文件全局后处理的 O(N²)）；统一解析阶段有独立状态提示；小文件优先加载，尽快看到进度 |
| 透明汇总 | 完成后显示：成功/取消/内存停止/跳过（内存预算）/失败明细与占用内存估算 |

## 无感加载（v1.9.0 统一列表 → v1.10.0 批次彻底隐形）

v1.8.x 曾把「批次」作为用户概念（批次卡片、勾选组合、合并查看、全选收紧）——用户被迫理解内存管理才能看全资产。v1.9.0 起彻底反转：**列表与内存解耦**；v1.10.0 把批次从界面上完全抹掉，退回为纯内部实现。

用户视角只有一件事：**选文件夹 → 得到一个完整的资产库**。

```
内部（用户不可见）:
  分装批次 1（独立解析器）→ 读满内存安全线
    → 驱逐旧批次对象（轻量索引常驻）→ 分装批次 2 → … 直到全部装完

用户视角:
  进度条: 正在加载 12/34：xxx.unity3d
  完成后: 「34 个文件 · 8,512 项」 → 查看资产（一个入口、一份列表、永远完整）
  内存装不下的部分: 标记「磁盘」→ 点开时自动从磁盘只装那一个文件（约 1 秒）
```

| 特性 | 说明 |
| --- | --- |
| 统一资产列表（v1.9.0） | 列表 = **全部文件全部资产**的并集：驻留批次的条目带对象引用；被释放批次的条目以轻量索引常驻（名称/类型/大小/pathID），与内存状态无关，永不缺项 |
| 索引-对象分离（v1.9.0） | `AssetItem.obj` 可空：null = 懒条目（磁盘暂存）。看列表、搜索、筛选、统计零内存压力 |
| 按需装载（v1.10.0 补全） | 详情页/导出遇懒条目自动从磁盘只装它所在的**一个文件**（走 60%/78% 内存防线），成功后回填列表自动出预览；失败显示原因并可重试 |
| 按需缓存上限（v1.10.0） | 回填对象最多常驻 48 个，超限淘汰最旧一半降回懒状态——长时间浏览不会把已释放内存重新钉死 |
| 导出聚簇（v1.10.0） | 批量导出先导已驻留项（零读盘），懒条目按来源文件聚簇——每个文件只读一次盘，而非每个资产读一次 |
| 界面隐形（v1.10.0） | 进度、汇总、提示全部只讲「文件 / 资产 / 磁盘暂存」；主页无批次卡片、无勾选、无合并按钮，仅保留「查看资产」与「清空」 |
| 防闪退防线 | 双阶段真实堆检查 60%/78%、类型树剥离、GC 等待复测在每个内部分装批次独立生效；批次间驱逐 + 等待 GC + 复测堆保证链式推进可终止 |

SAF 选择器加载的批次无持久 Uri 权限，不可从磁盘按需装载，故永不驱逐（始终驻留）。

## 批量导出（v1.7.5）

资产列表长按进入多选 → 勾选任意类型资产（不限于贴图）→ 右上角「批量导出」→ 选输出文件夹。按类型自动选格式（贴图/Sprite→PNG、模型→OBJ、音频→WAV、视频→原始数据、材质/着色器/MonoBehaviour→文本转储、字体→TTF）。逐个导出可取消，单个失败跳过并继续；每 8 个资产主动 GC 防贴图解码峰值叠加；文件名自动去非法字符、重名加序号。

## 贴图方向修复（v1.7.7）

修复此前所有贴图（预览、导出 PNG、Sprite 裁剪）上下颠倒的问题。根因：Unity 纹理数据按 OpenGL 习惯存储（row 0 = 图像**底部**），而 Android Bitmap / PNG 的 row 0 是图像**顶部**，两者相差一次垂直翻转——原版 C# AssetStudio 在 `GetImage()` 里用 `RotateFlip(RotateNoneFlipY)` 完成转换，移植版的通用封装路径（`decodeBitmap`）做了翻转，但预览、导出、替换三条直连解码路径遗漏了这一步。现统一收敛到 `PixelFlip` 单一实现：解码（预览/导出/Sprite）翻转一次底部序→顶部序；替换回写反向翻转一次顶部序→底部序，与解码方向互逆，「导出→改图→替换」闭环方向正确，替换后的贴图在游戏内不再颠倒。

## 功能总览

| 功能 | 说明 |
| --- | --- |
| 加载 Unity 资源 | `.unity3d` / `.assetbundle` / `.bundle`（UnityFS、UnityWeb/Raw v6、旧版 Web/Raw）、`.assets`、`.zip`、`.unitypackage` 结构 |
| **加密 bundle 支持** | AB管理器加密包（UnityKHFS/UnityKHNFS/UnityKH1FS 三版自动解密，含 LZ4 压缩清单与数据区对齐）；自动探测：头部垃圾字节（自定义头偏移剥离）+ 单字节 XOR 加密（全文件/仅头部自动判别）；手动解密：XOR 循环密钥 / AES-ECB / AES-CBC（128/192/256，hex 密钥 + 前缀偏移） |
| 资产浏览 | 按类型筛选（贴图/文本/音频/Mesh/Sprite/MonoBehaviour 等）、关键词搜索、PathID 定位 |
| 资产信息复制 | 详情页"资产信息"卡片支持长按自由选择复制（PathID、文件名、容器路径等可任意范围选取） |
| 贴图预览 | 30+ 种纹理格式解码为位图预览，支持 mipmap 级别切换；自适应缩放：小尺寸贴图等比放大到可辨识大小（原始比例不变，最近邻保持像素清晰），超宽条图保证最小可辨识高度；缩放滑杆拖拉控制等比缩放（25%~800%，切换贴图自动重置），超出视口部分双向滑动查看 |
| **Mesh 预览** | 实体 3D 渲染（纯 Kotlin 软件光栅化，无 OpenGL 依赖）：z-buffer 逐像素消隐、面法线自动朝向相机、Lambert 定向光 + 环境光着色；自动旋转、拖拽旋转；超大网格（>4 万三角形）自动降采样保证流畅 |
| **渲染器组件预览与替换（v1.7.1）** | SkinnedMeshRenderer / MeshFilter 详情页直接 3D 预览其引用的网格（解引用 `m_Mesh` PPtr，含顶点/三角形统计）；信息卡展示骨骼数量、材质槽位、混合形状权重；"关联资产"卡片可一键跳转 Mesh 详情；"替换网格"按钮替换其引用的 Mesh（骨骼绑定与材质槽保持不变，跨文件引用同样支持） |
| **MCP 渲染器组件支持（v1.7.2）** | MCP 服务器与 App UI 同等支持渲染器组件：`list_assets` 可按 SkinnedMeshRenderer / MeshFilter 类型过滤；详情返回骨骼数、材质槽数、混合形状权重数与引用网格统计（原始 PPtr + 解引用统计）；`dump_render_asset` 转储网格引用/骨骼列表/材质槽/混合形状权重为可读文本；`export_asset` 经渲染器引用的网格直接导出 OBJ、转储文本导出 TXT |
| 导出 | Texture2D → PNG / TextAsset 与 MonoBehaviour → 文本 / AudioClip → WAV / 任意资产 → 原始字节转储；导出时按类型使用正确 MIME，保存后缀与内容一致（贴图恒为 `.png`） |
| **贴图替换** | 选择新图片（尺寸可变）→ 重编码 → 重写 SerializedFile → 重打包 Bundle；另存时默认使用原文件名，保存后可直接替换游戏内同名文件；另存文件可在本软件重新打开预览验证（目录表字段顺序与 Unity 标准一致、头部 fileSize 按大端修补，后缀不影响识别）；旧版本另存的坏文件（头部 fileSize 字节序错误）重新加载时自动修复 |
| **批量贴图替换（v1.7.0）** | 资产列表长按进入多选 → 勾选多张贴图 → 选图片文件夹 → 文件名自动匹配（忽略大小写/分隔符/扩展名，Unity "(instance)" 后缀可配对，歧义不误配）→ 一键全部替换；单张失败不影响其余；同一容器只输出一份文件，多容器自动打包 ZIP；内存逐张控制，大文件夹不溢出 |
| **ASTC 原格式编码（v1.7.0）** | 纯 Kotlin 实现 Khronos ASTC LDR 编码器（4x4/5x5/6x6/8x8 块），原格式为 ASTC 的贴图替换后**保持 ASTC 格式**（此前只能回退 RGBA32，包体翻倍）；含 ISE trit/quint 编码、端点动态量化范围、PCA 主轴端点选择、权重 infill；布局经独立解码器（texture2ddecoder）交叉验证 28/28 |

## 贴图替换（核心特性）

完整链路，输出文件可直接放回游戏生效：

```
用户图片(PNG/JPG) ─→ TextureEncoder（生成 mip 链 + 按目标格式编码）
                  ─→ Texture2DObjectRewriter（重建 Texture2D 对象字节，同步宽高/格式/mip/数据大小字段）
                  ─→ SerializedFileRewriter（重排对象数据区，修补对象表 byteStart/byteSize 与文件头 fileSize）
                  ─→ BundleRepacker（UnityFS 重打包，LZ4 分块压缩 + blocksInfo/目录表重建）
                  ─→ 另存为可用的 .bundle / .unity3d / .assets / .zip
```

### 替换能力矩阵

| 项 | 支持情况 |
| --- | --- |
| 原格式保持 | RGBA32 / ARGB32 / RGB24 / BGRA32 / Alpha8 / R8 / RGB565 / ARGB4444 / DXT1 / DXT5 / **ASTC 4x4·5x5·6x6·8x8（RGB/RGBA，v1.7.0）** |
| 自动回退 | 上述之外的格式自动改写为 RGBA32（格式字段同步更新，游戏可正常加载） |
| Mipmap | 自动用 box filter 生成，沿用原图级数 |
| 尺寸 | 允许与原图不同（如 512→1024），对象字段全部同步；DXT 系列要求 4 的倍数，否则回退 RGBA32 |
| 容器 | UnityFS Bundle、UnityWeb/Raw v6、纯 .assets、.zip 内嵌、WebFile（.unity3d）嵌套 |
| 限制 | 旧版（v6 以下）Bundle 暂不支持重打包；嵌套 crunch 贴图替换后变为普通格式数据 |

## 纹理解码格式（NDK 原生）

BC1-BC7（DXT1/3/5/BC4/5/6H/7）、ETC1、ETC2（RGB/A1/A8）、EAC R/RG（含 signed）、ASTC（全块尺寸）、PVRTC（2/4bpp）、ATC、crunch / Unity crunch。

未压缩格式（RGBA32/RGB24/RGB565/ARGB4444/Alpha8/R8/R16 等）与调色板格式（索引 4/8 + RGB/RGBA 调色板）在纯 Kotlin 层解码。

## 项目结构

```
app/src/main/
├── java/com/assetstudio/mobile/
│   ├── core/                      # AssetStudio C# 核心逐行移植（Kotlin）
│   │   ├── io/                    # EndianBinaryReader / FileReader / Gzip / Brotli
│   │   ├── bundle/                # BundleFile / WebFile / LZ4 / LZMA / 7zLZMA
│   │   ├── serialized/            # SerializedFile / TypeTree / CommonString
│   │   ├── classes/               # Texture2D / Sprite / Mesh / MonoBehaviour 等 30+ 资产类
│   │   ├── texture/               # TextureConverter（解码调度）+ NativeDecoder(JNI 声明)
│   │   ├── manager/               # AssetsManager（加载/解析/容器层级追踪 SourceRef）
│   │   └── math/                  # Vector/Matrix/Quaternion/Rect
│   ├── replace/                   # ★ 贴图替换链路（编码/对象重写/文件重写/Bundle重打包）
│   ├── export/                    # PNG / 文本 / 转储导出
│   ├── ui/                        # Compose MD3 界面（主页/列表/详情）
│   └── MainActivity.kt / MainViewModel.kt / App.kt
└── cpp/                           # NDK 原生解码器（AssetStudio C++ 源 + JNI 桥）
    ├── texture2ddecoder_jni.cpp
    └── decoder/                   # astc / bcn / etc / pvrtc / atc / crunch / unitycrunch
```

## 使用说明

1. 安装 APK（`app/build/outputs/apk/debug/app-debug.apk`），Android 8.0+，arm64 推荐。
2. 主页点「选择文件」选 `.bundle` / `.unity3d` / `.assets` / `.zip`（可多选；剥离过版本的文件在加载前填写 Unity 版本，如 `2021.3.20f1`）；**或点「加载文件夹」直接选整个目录批量加载**（自动扫描、可含子文件夹、带进度与取消、防 OOM）。文件过多内存装不下时**自动分批**：主页「已加载批次」区为每个批次出一张卡片，点「查看资产列表」即可独立浏览该批次，相当于同时拥有多份可切换的资产列表。
3. 进入资产列表，按类型 Chip 筛选或搜索，点击资产进入详情。
4. **导出**：详情页点「导出」，保存对话框默认带正确后缀（贴图 `.png`、音频 `.wav`、文本 `.txt`）。
5. **Mesh 预览**：Mesh 详情页自动加载线框预览，可拖动旋转查看。
6. **替换贴图**：Texture2D 详情页点「替换贴图」→ 选新图 → 确认（默认尝试保持原格式，可勾选强制 RGBA32）→ 替换成功后点「另存文件」，保存对话框默认使用原文件名，确认保存后即可用该文件直接替换游戏内原文件。

## 构建

```bash
# 需要 JDK 17、Android SDK 34、NDK 26、CMake 3.22+
export JAVA_HOME=<jdk17> ANDROID_HOME=<sdk>
./gradlew assembleDebug
```

ABI：arm64-v8a / armeabi-v7a / x86 / x86_64。minSdk 26（Android 8.0），targetSdk 34。

## 与原版 AssetStudio 的差异

- 文件访问改为 SAF（存储访问框架）+ 内存模型，不依赖文件系统目录扫描；外部 `.resS` 通过回调按需加载。
- 无 TypeTree 反序列化依赖的运行时代码生成，MonoBehaviour 通过内置 TypeTree 解析为树形文本展示。
- 贴图替换为 AssetStudio GUI 未内置的能力（原版需借助 UABE），本项目内置全链路实现。
- 导出 Mesh/Audio 等重资产时导出原始数据或简化格式（OBJ 导出未移植）。

## 许可

AssetStudio 为 MIT 许可；本项目遵循相同许可。仅用于学习与研究，请勿用于分发侵权资源。

---

作者：醉莫

## Automatic GitHub Actions build and translation

The workflow in `.github/workflows/build.yml` runs on pushes and pull requests. It translates Chinese text found in supported source/documentation files to English, checks that no CJK text remains in those files, then builds both debug and release APKs.

When a workflow run completes, open **Actions → the completed run → Artifacts** to download:

- `AssetStudioMobile-APKs` — debug and release APKs.
- `AssetStudioMobile-translated-source` — a ZIP of the translated source tree.

To run it manually, open **Actions → Translate Chinese and Build APK → Run workflow**. Enable `commit_translations` only when you also want the workflow to commit the translated files back to the selected branch. The translator uses the Google Translate service through `deep-translator`, so the runner needs internet access; text is sent to that external translation service.

