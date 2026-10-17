/*
 * hook_qq_music.js
 * ---------------------------------------------------------------------------
 * 原理（逆向自 luyikk/qqmusic_decrypt）
 *
 * QQ音乐把下载的加密音乐（.mflac / .mgg / .mgg1 / .mflac0 ...）的解密逻辑
 * 放在 QQMusicCommon.dll 里的 C++ 类 EncAndDesMediaFile 中。这个类就是
 * QQ音乐播放器自己用来读取加密文件的“解密版 ifstream”：
 *
 *     构造 EncAndDesMediaFile 对象
 *     -> Open(路径, true, false)   // 内部识别加密类型并取密钥
 *     -> GetSize()                 // 得到【解密后】的明文长度
 *     -> Read(buf, size, 0)        // 读出【解密后】的明文数据
 *     -> 析构
 *
 * 所以不需要自己实现 QMC 算法、也不需要抓密钥：直接借用 QQ音乐自己的
 * 解密代码，任何新版本格式都能自动支持。
 *
 * 导出符号（MSVC 名字修饰）：
 *   ??0EncAndDesMediaFile@@QAE@XZ              构造函数
 *   ??1EncAndDesMediaFile@@QAE@XZ              析构函数
 *   ?Open@EncAndDesMediaFile@@QAE_NPB_W_N1@Z   bool Open(const wchar_t*, bool, bool)
 *   ?GetSize@EncAndDesMediaFile@@QAEKXZ        uint32 GetSize()
 *   ?Read@EncAndDesMediaFile@@QAEKPAEK_J@Z     uint Read(void*, uint32, int64)
 *
 * 本文件在原版基础上做了兼容性/健壮性改造：
 *   1. 同时兼容 Frida 17（Module.findGlobalExportByName）与 Frida <=16
 *      （Module.findExportByName），并给出明确的中文报错。
 *   2. 调用约定自动降级（thiscall -> win64 -> stdcall -> default）。
 *   3. 对象内存从原版的 0x28 放宽到 0x200，避免 QQ音乐升级后类变大导致堆越界。
 *   4. 检查 Open / GetSize / Read 的返回值，失败时抛出可读的中文错误。
 *   5. 分块读取大文件，避免一次性分配上百 MB 造成目标进程抖动。
 *   6. 直接通过 RPC 把解密数据（ArrayBuffer）返回给宿主，不再让 QQ音乐进程
 *      去写临时文件，省掉“临时文件 + md5 改名”的绕路。
 * ---------------------------------------------------------------------------
 */

var TARGET_DLL = "QQMusicCommon.dll";
var OBJECT_SIZE = 0x200;          // 原实现 0x28，这里留足余量
var CHUNK_SIZE = 16 * 1024 * 1024; // 单次读取上限 16MB

/* ---------------------------------------------------------------- 找导出函数 */
function findExport(exportName) {
  var addr = null;

  // Frida 17+
  if (typeof Module.findGlobalExportByName === "function") {
    try { addr = Module.findGlobalExportByName(exportName); } catch (e) { addr = null; }
  }
  if (addr === null || addr === undefined) {
    if (typeof Module.getGlobalExportByName === "function") {
      try { addr = Module.getGlobalExportByName(exportName); } catch (e) { addr = null; }
    }
  }
  // Frida <= 16
  if (addr === null || addr === undefined) {
    if (typeof Module.findExportByName === "function") {
      try { addr = Module.findExportByName(TARGET_DLL, exportName); } catch (e) { addr = null; }
    }
  }
  return addr;
}

var EXPORT_NAMES = {
  ctor:    "??0EncAndDesMediaFile@@QAE@XZ",
  dtor:    "??1EncAndDesMediaFile@@QAE@XZ",
  open:    "?Open@EncAndDesMediaFile@@QAE_NPB_W_N1@Z",
  getSize: "?GetSize@EncAndDesMediaFile@@QAEKXZ",
  read:    "?Read@EncAndDesMediaFile@@QAEKPAEK_J@Z"
};

var ADDR = {};
Object.keys(EXPORT_NAMES).forEach(function (key) {
  var a = findExport(EXPORT_NAMES[key]);
  if (a === null || a === undefined || a.isNull()) {
    throw new Error(
      "在 " + TARGET_DLL + " 中找不到导出函数 " + EXPORT_NAMES[key] +
      "。请确认 QQ音乐 正在运行且版本受支持。"
    );
  }
  ADDR[key] = a;
});

/* --------------------------------------------------------------- 包装本机函数 */
function makeNative(addr, retType, argTypes) {
  var abis = ["thiscall", "win64", "stdcall", "default"];
  var lastErr = null;
  for (var i = 0; i < abis.length; i++) {
    try {
      return new NativeFunction(addr, retType, argTypes, abis[i]);
    } catch (e) {
      lastErr = e;
    }
  }
  throw new Error("无法为该函数创建 NativeFunction: " + lastErr);
}

var fnCtor    = makeNative(ADDR.ctor,    "pointer", ["pointer"]);
var fnDtor    = makeNative(ADDR.dtor,    "void",    ["pointer"]);
var fnOpen    = makeNative(ADDR.open,    "bool",    ["pointer", "pointer", "bool", "bool"]);
var fnGetSize = makeNative(ADDR.getSize, "uint32",  ["pointer"]);
var fnRead    = makeNative(ADDR.read,    "uint",    ["pointer", "pointer", "uint32", "uint64"]);

/* ------------------------------------------------------------------ RPC 接口 */
rpc.exports = {

  /* 自检信息，宿主用它来判断环境是否正常 */
  info: function () {
    var mod = null;
    try {
      if (typeof Process.findModuleByName === "function") {
        mod = Process.findModuleByName(TARGET_DLL);
      } else if (typeof Module.load === "function") {
        mod = Process.getModuleByName(TARGET_DLL);
      }
    } catch (e) { mod = null; }

    return {
      frida: Frida.version,
      arch: Process.arch,
      pointerSize: Process.pointerSize,
      platform: Process.platform,
      dllPath: mod ? mod.path : null,
      exports: Object.keys(ADDR)
    };
  },

  /*
   * 解密 srcFileName -> 返回解密后的明文（ArrayBuffer）
   * 抛出的异常会被 frida 打包成 { type: "error", description: ... } 传回宿主
   */
  decrypt: function (srcFileName) {
    var obj = Memory.alloc(OBJECT_SIZE);
    var opened = false;

    try {
      fnCtor(obj);

      var widePath = Memory.allocUtf16String(srcFileName);
      // 与原实现保持一致：Open(path, true, false)
      opened = fnOpen(obj, widePath, 1, 0);
      if (!opened) {
        throw new Error("QQ音乐无法打开该文件（可能不是加密音乐，或格式不支持）：" + srcFileName);
      }

      var size = fnGetSize(obj);
      if (size === 0) {
        throw new Error("解密后文件长度为 0：" + srcFileName);
      }

      var out = Memory.alloc(size);
      var done = 0;
      while (done < size) {
        var want = Math.min(CHUNK_SIZE, size - done);
        var got = fnRead(obj, out.add(done), want, done);
        if (got === 0) {
          throw new Error("解密读取中断：已读 " + done + " / " + size + " 字节");
        }
        done += got;
      }

      // readByteArray 返回 ArrayBuffer，frida 会直接搬到宿主进程
      return out.readByteArray(done);

    } finally {
      try { fnDtor(obj); } catch (e) { /* 忽略析构异常 */ }
    }
  }
};
