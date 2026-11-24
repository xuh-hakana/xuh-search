#!/bin/zsh
# 编译 search.app · 双击运行
# 只在改过 launcher.m / Info.plist / 图标后需要跑一次；平时直接双击 search.app。
set -e
cd "$(dirname "$0")" || exit 1

ROOT="$(cd .. && pwd)"
APP="$ROOT/search.app"

if ! command -v clang >/dev/null 2>&1; then
  echo "没有找到 clang。先装 Xcode 命令行工具：xcode-select --install"
  read -r "?按回车关闭…"
  exit 1
fi

echo "→ 清理旧包"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

echo "→ 编译原生窗口"
clang -fobjc-arc -O2 -framework Cocoa -framework WebKit -framework Carbon \
  -framework ApplicationServices \
  launcher.m -o "$APP/Contents/MacOS/search"
chmod +x "$APP/Contents/MacOS/search"

echo "→ 写入 Info.plist"
cp Info.plist "$APP/Contents/Info.plist"

if [ ! -f "AppIcon.icns" ]; then
  echo "→ 生成图标"
  /usr/bin/python3 make_icon.py || echo "  （图标跳过，不影响运行）"
fi
if [ -f "AppIcon.icns" ]; then
  cp "AppIcon.icns" "$APP/Contents/Resources/AppIcon.icns"
fi

echo "→ 本地签名"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || echo "  （签名跳过，不影响本机运行）"

touch "$APP"
echo ""
echo "完成：$APP"
echo "双击它就能开，不用终端。菜单栏会出现一个放大镜图标；⌃⌥S 唤出 / 收起。"
