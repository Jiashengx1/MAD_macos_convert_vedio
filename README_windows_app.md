# Windows GUI 打包说明

Windows 版必须在 Windows 上打包。PyInstaller 不能从 macOS 直接交叉生成
Windows EXE。

## 运行源码版

```powershell
py -3.12 gui.py
```

源码版需要当前 Windows 已安装 `ffmpeg.exe` 和 `ffprobe.exe`，并能在 PATH 中找到。

## 准备 FFmpeg

推荐把 Windows x64 版 FFmpeg 放到：

```text
vendor\ffmpeg-windows-x64\ffmpeg.exe
vendor\ffmpeg-windows-x64\ffprobe.exe
```

如果没有这个目录，打包脚本会改用当前 Windows PATH 中的 FFmpeg。发给其他电脑前，
请确认使用的是静态或可移植版本，避免目标电脑缺少运行库。

## 生成免安装版

先安装打包工具：

```powershell
py -3.12 -m pip install pyinstaller
```

然后在项目目录运行：

```powershell
py -3.12 build_windows_app.py
```

生成结果：

- `dist\windows\医务科监控修复助手\医务科监控修复助手.exe`
- `dist\医务科监控修复助手-windows-x64.zip`

把 zip 发给别人，对方解压后双击 `医务科监控修复助手.exe` 即可。不要只复制单个
EXE，必须保留同目录下的 `_internal` 文件夹。

## Windows 安全提示

这个应用没有代码签名证书。别人第一次打开时，Windows Defender SmartScreen 可能提示
未知发布者；若要彻底消除该提示，需要购买 Windows 代码签名证书并签名发布包。
