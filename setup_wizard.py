"""
setup_wizard.py — 辅助脚本：启动 weflow-core 配置向导
在独立终端窗口中运行，提取微信解密密钥
"""
import subprocess
import sys
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WEFLOW_DIR = os.path.join(BASE_DIR, "weflow-core")
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

dist_js = os.path.join(WEFLOW_DIR, "dist", "index.js")
if not os.path.isfile(dist_js):
    print("[ERROR] weflow-core 未构建，请先运行: cd weflow-core && npm install && node esbuild.config.mjs")
    sys.exit(1)

print("=" * 50)
print("  WeFlow 配置向导")
print("  将提取微信解密密钥并写入 config.json")
print("=" * 50)
print()
print("请确保微信已登录！")
print()

# 在 Windows 上使用 start 打开新终端窗口
if sys.platform == "win32":
    cmd = (
        f'start "WeFlow 配置向导" /wait cmd /c '
        f'"cd /d {WEFLOW_DIR} && '
        f'node dist/index.js {CONFIG_PATH} && '
        f'echo. && echo 配置完成！按任意键关闭... && pause >nul"'
    )
else:
    cmd = f"cd {WEFLOW_DIR} && node dist/index.js {CONFIG_PATH}"

print(f"正在启动配置向导...")
print(f"命令: {cmd}")
subprocess.run(cmd, shell=True)
print("\n配置向导已关闭。")
