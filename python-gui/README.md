# QQ音乐加密音乐 · 解密 / 转换工具

把 QQ音乐下载的加密音乐（`.mgg` / `.mflac` / `.mgg1` / `.mflac0` / `.mggl` 等）**拖进窗口**，
自动解密成 `ogg` / `flac`，并可以直接转成 `mp3`。

界面：`qqmusic_gui.py`　启动：双击 `启动工具.bat`

---

## 一、原理

本项目改编自 [luyikk/qqmusic_decrypt](https://github.com/luyikk/qqmusic_decrypt)。

**核心思路：不自己实现 QMC 加密算法，而是借用 QQ音乐自己的解密代码。**

QQ音乐要播放这些加密文件，所以它自己的 DLL 里一定有「解密读取」的实现。
逆向发现，`QQMusicCommon.dll` 中有一个 C++ 类 `EncAndDesMediaFile`，
它就是一个「会自动解密的 ifstream」：

```
构造 EncAndDesMediaFile 对象
  -> Open(路径, true, false)      // 内部识别加密类型、取出密钥
  -> GetSize()                    // 返回【解密后】的明文长度
  -> Read(buf, size, offset)      // 读出【解密后】的明文
  -> 析构
```

用到的导出符号（MSVC 名字修饰）：

| 符号 | 含义 |
|---|---|
| `??0EncAndDesMediaFile@@QAE@XZ` | 构造函数 |
| `??1EncAndDesMediaFile@@QAE@XZ` | 析构函数 |
| `?Open@EncAndDesMediaFile@@QAE_NPB_W_N1@Z` | `bool Open(const wchar_t*, bool, bool)` |
| `?GetSize@EncAndDesMediaFile@@QAEKXZ` | `uint32 GetSize()` |
| `?Read@EncAndDesMediaFile@@QAEKPAEK_J@Z` | `uint Read(void*, uint32, int64)` |

本工具通过 **Frida** 把一段 JS 注入 QQ音乐进程，在进程内构造该对象、
调用这几个函数，再把解密后的数据通过 RPC 搬回 Python，写成文件。

这样做的好处：

* **不依赖密钥表**，任何新版本加密格式自动支持；
* 不会因为 QQ音乐升级而失效（只要这几个导出还在）；
* 不需要抓包、不需要登录态参与。

### 原版的改造点

原版是命令行程序，且有几处可以更稳：

| 原版 | 本工具 |
|---|---|
| `Module.findExportByName`（Frida ≤16 专属） | 兼容 Frida 17 的 `Module.findGlobalExportByName`，并给出中文报错 |
| 调用约定固定 `thiscall` | `thiscall → win64 → stdcall → default` 自动降级 |
| 对象只分配 `0x28` 字节 | 放宽到 `0x200`，避免升级后类变大导致堆越界 |
| 不检查 `Open/GetSize/Read` 返回值 | 全部检查，失败抛出可读的中文错误 |
| 一次性读整个文件 | 16MB 分块读取 |
| 让 QQ音乐进程写临时文件再改名为 md5 | 直接经 RPC 把字节返回 Python，省掉临时文件 |
| 只能扫固定目录 | 图形界面，拖拽任意文件/文件夹 |
| 只输出 flac / ogg | 可再转 mp3 |

> ⚠️ **重要安全发现**：`Open` 的第二个参数若传 `(0, 1)`，QQ音乐会**把源文件截断为 0 字节**。
> 本工具只使用经过验证的 `Open(路径, 1, 0)`；`(1,0)` / `(0,0)` / `(1,1)` 均不会改动源文件。

---

## 二、环境要求

| 组件 | 说明 |
|---|---|
| Windows | 64 位系统即可（QQ音乐是 32 位进程，Frida 会自动处理） |
| Python 3.8+ | 需要 `frida`、`tkinterdnd2` |
| QQ音乐 | **必须安装并且正在运行**（解密代码在它的进程里） |
| ffmpeg | 只有要转 MP3 时才需要 |

安装依赖：

```bat
python -m pip install frida tkinterdnd2
```

ffmpeg 的获取方式（任选其一）：

1. 把 `ffmpeg.exe` 放到本目录下的 `ffmpeg\bin\` 文件夹里；
2. 加入系统 `PATH`；
3. 界面里点「指定…」手动选择 `ffmpeg.exe`。

> 注意要用**带 libmp3lame** 的完整版 ffmpeg，
> 精简版没有 MP3 编码器，程序会提示「不支持 libmp3lame」。

---

## 三、使用方法

1. **先启动 QQ音乐并登录**（保持运行，不要关闭）。
2. 双击 `启动工具.bat`。
3. 顶部显示「已连接 QQ音乐 (PID xxxx)」即为正常；
   若显示未检测到，点「重新连接」。
4. 把加密音乐文件**直接拖进窗口**，也可以拖整个文件夹（会递归查找）。
   或用「添加文件 / 添加文件夹」按钮。
5. 选择输出格式：
   * **原生格式** —— `mgg → ogg`，`mflac → flac`（无损、原格式）
   * **MP3** —— 用 ffmpeg 转码，可选 128/192/256/320 kbps
   * **原生 + MP3** —— 两者都保留
6. 点「开始转换」。

输出位置：

* 默认写到**源文件所在目录下的 `output` 文件夹**；
* 也可以选「指定目录」。

其它选项：

* **跳过已存在的文件** —— 重复运行时只处理新增的；
* **生成 MP3 后不保留原生文件** —— 仅 MP3 模式下生效，转完删掉中间的 flac/ogg。

---

## 四、文件说明

| 文件 | 作用 |
|---|---|
| `qqmusic_gui.py` | 图形界面主程序 |
| `qqmusic_decryptor.py` | 解密核心：连接 QQ音乐、调用导出函数、识别真实格式 |
| `hook_qq_music.js` | 注入到 QQ音乐进程的 Frida 脚本 |
| `transcoder.py` | 定位并调用 ffmpeg 转 MP3 |
| `启动工具.bat` | 双击启动 |
| `config.json` | 自动保存的设置（首次运行后生成） |

命令行调用示例：

```bat
python qqmusic_gui.py
```

---

## 五、常见问题

**Q：提示「没有找到运行中的 QQ音乐」**
A：先启动 QQ音乐并登录，然后点界面上的「重新连接」。
QQ音乐是 32 位进程，如果同时开了多个 QQ音乐 相关进程，程序会优先选主进程 `QQMusic.exe`。

**Q：提示「QQ音乐无法打开该文件（可能不是加密音乐，或格式不支持）」**
A：解密函数会按**扩展名**分派：
* 真正的加密文件（VIP 下载的 `.mgg`/`.mflac`）可以正常打开；
* 只是把普通 mp3 改名成 `.mgg` 会被正确拒绝；
* QQ音乐 P2P 缓存目录（`QQMusicCache\downloadproxyNew\tp2p\.tpfs`）里的 `.mgg`
  是**需要外部密钥的分片缓存**，不属于可独立解密的标准加密文件，同样会被拒绝。
  请使用 QQ音乐「下载」功能得到的文件。

**Q：解密后是 ogg / flac，能直接播放吗？**
A：可以，`ogg`（Vorbis）和 `flac` 都是通用格式，主流播放器都支持。

**Q：转 MP3 会损失音质吗？**
A：MP3 是有损格式，从 flac 转 320kbps MP3 会有轻微损失。
想要无损就选「原生格式」，保留 flac。

**Q：杀毒软件报警？**
A：Frida 注入 + 调试器附加是它的正常行为，容易被启发式引擎误报。
本工具只读取文件，不修改 QQ音乐程序本体。

---

## 六、免责声明

本工具仅用于**个人已购买/已下载音乐的格式转换与备份**，方便在自己的设备上播放。
请勿用于传播版权内容或任何商业用途。请遵守所在地区的法律法规以及 QQ音乐的服务条款。
