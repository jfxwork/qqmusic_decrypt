# 0.2.0
#### Features
- 新增 `python-gui`：Windows 图形界面版本，支持拖拽文件/文件夹自动解密
- 解密后可直接转 MP3（128 / 192 / 256 / 320 kbps），可选保留原生 ogg/flac
- 兼容 Frida 17（改用 `Module.findGlobalExportByName`），调用约定自动降级
- 解密数据改为经 RPC 直接取回，不再依赖目标进程写临时文件
- 新增环境自检脚本 `python-gui/selftest.py`

#### Fixes
- 修正 `EncAndDesMediaFile::Open` 标志位用法，避免误用 `(0, 1)` 导致源文件被截断
- 检查 `Open` / `GetSize` / `Read` 返回值，失败时给出可读的中文错误

# 0.1.1
#### Features
可以自定义QQ音乐下载路径

# 0.1.0
#### Features
第一次提交
