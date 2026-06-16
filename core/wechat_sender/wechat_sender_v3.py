# -*- coding: utf-8 -*-
"""
个人微信自动发送模块 - 接口版
版本：v3.0.0
创建日期：2025-09-11
功能：基于通用接口的个人微信自动发送功能（WeChat.exe/Weixin.exe）
"""

import logging
import time
from time import sleep
from typing import Dict, Any

import psutil
import pyautogui
import pyperclip
import win32con
import win32gui
import win32process
import win32ts

from core.wechat_sender.message_sender_interface import MessageSenderInterface, MessageSenderFactory

# 配置日志
logger = logging.getLogger(__name__)

# 设置 pyautogui 全局配置（必须在任何 pyautogui 操作之前设置）
pyautogui.FAILSAFE = False  # 禁用 fail-safe，防止鼠标移动到角落时触发异常
# pyautogui.PAUSE = 0.1  # 每次操作后的默认暂停时间（秒）


class WeChatSenderV3(MessageSenderInterface):
    """个人微信发送器 v3.0"""

    def __init__(self, config: Dict[str, Any] = None):
        """初始化个人微信发送器"""
        super().__init__(config)

        # 微信进程和窗口信息
        self.wechat_process = None
        self.wechat_pid = None
        self.wechat_pids = []
        self.main_window_hwnd = None

        # 默认配置
        self.process_names = ["WeChat.exe", "Weixin.exe", "wechat.exe"]
        self.default_group = config.get('default_group', '文件传输助手') if config else '存储统计报告群'

    def initialize(self) -> bool:
        """初始化个人微信发送器"""
        try:
            logger.info("初始化个人微信发送器...")

            # 查找微信进程
            if not self.find_target_process():
                logger.error("未找到个人微信进程")
                return False

            # 查找微信窗口
            if not self._find_wechat_windows():
                logger.error("未找到个人微信窗口")
                return False

            self.is_initialized = True
            logger.info("个人微信发送器初始化成功")
            return True

        except Exception as e:
            logger.error(f"初始化个人微信发送器失败: {e}")
            return False

    def find_target_process(self) -> bool:
        """查找个人微信进程"""
        try:
            wechat_processes = []
            for proc in psutil.process_iter(['pid', 'name', 'exe']):
                try:
                    proc_name = proc.info['name']
                    if any(name.lower() in proc_name.lower() for name in self.process_names):
                        wechat_processes.append(proc)
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue

            if not wechat_processes:
                logger.error("未找到个人微信进程，请先启动微信")
                return False

            # 选择第一个微信进程
            self.wechat_process = wechat_processes[0]
            self.wechat_pid = self.wechat_process.pid
            self.wechat_pids = [proc.pid for proc in wechat_processes]
            logger.info(f"找到个人微信进程 PID: {self.wechat_pid}")
            return True

        except Exception as e:
            logger.error(f"查找个人微信进程失败: {e}")
            return False

    def _find_wechat_windows(self) -> bool:
        """查找个人微信窗口"""
        try:
            if not self.wechat_pid:
                logger.error("请先查找个人微信进程")
                return False

            # 枚举所有窗口
            windows_list = []
            win32gui.EnumWindows(self._enum_windows_callback, windows_list)

            # 查找属于微信进程的窗口
            wechat_windows = [w for w in windows_list if w['pid'] in self.wechat_pids]

            if not wechat_windows:
                logger.error("未找到个人微信窗口")
                return False

            # 查找主窗口（通常类名包含WeChatMainWndForPC）
            main_windows = [w for w in wechat_windows if 'WeChatMainWndForPC' in w['class']]
            if main_windows:
                self.main_window_hwnd = main_windows[0]['hwnd']
                logger.info(f"找到个人微信主窗口: {main_windows[0]['title']}")
            else:
                # 备选方案：选择第一个有标题的窗口
                titled_windows = [w for w in wechat_windows if w['title'].strip()]
                if titled_windows:
                    self.main_window_hwnd = titled_windows[0]['hwnd']
                    logger.info(f"使用备选个人微信窗口: {titled_windows[0]['title']}")
                else:
                    logger.error("无法确定个人微信主窗口")
                    return False

            return self.main_window_hwnd is not None

        except Exception as e:
            logger.error(f"查找个人微信窗口失败: {e}")
            return False

    def _enum_windows_callback(self, hwnd, windows_list):
        """枚举窗口回调函数"""
        if win32gui.IsWindowVisible(hwnd):
            window_text = win32gui.GetWindowText(hwnd)
            class_name = win32gui.GetClassName(hwnd)

            # 获取窗口所属进程ID
            _, window_pid = win32process.GetWindowThreadProcessId(hwnd)

            windows_list.append({
                'hwnd': hwnd,
                'title': window_text,
                'class': class_name,
                'pid': window_pid
            })

    def _check_session_active(self) -> bool:
        """
        检查当前 Windows 会话是否处于活动状态（有交互式桌面）
        远程桌面断开后，会话会进入断开连接状态，此时无法进行 GUI 操作
        """
        try:
            # 获取当前进程的会话 ID
            current_session_id = win32ts.ProcessIdToSessionId(win32process.GetCurrentProcessId())
            
            # 枚举所有会话，检查当前会话状态
            sessions = win32ts.WTSEnumerateSessions(win32ts.WTS_CURRENT_SERVER_HANDLE)
            for session in sessions:
                if session['SessionId'] == current_session_id:
                    # WTSActive = 0 表示活动状态
                    # WTSDisconnected = 4 表示断开连接状态
                    state = session['State']
                    if state == win32ts.WTSActive:
                        return True
                    elif state == win32ts.WTSDisconnected:
                        logger.warning("检测到远程桌面已断开连接，GUI 操作将无法正常执行")
                        return False
                    else:
                        logger.warning(f"会话状态异常: {state}")
                        return False
            return True
        except Exception as e:
            logger.warning(f"检查会话状态失败: {e}，继续尝试执行")
            return True  # 检查失败时继续尝试

    def activate_application(self) -> bool:
        """激活个人微信窗口（使用三层降级策略）"""
        try:
            if not self.main_window_hwnd:
                logger.error("个人微信窗口句柄不存在")
                return False

            # 检查会话是否活动（远程桌面是否已断开）
            if not self._check_session_active():
                logger.error("远程桌面已断开，无法激活窗口。请重新连接远程桌面后再试。")
                return False

            # 检查窗口是否最小化，如果是则恢复
            if win32gui.IsIconic(self.main_window_hwnd):
                win32gui.ShowWindow(self.main_window_hwnd, win32con.SW_RESTORE)
                time.sleep(0.1)

            # 关键：模拟用户输入使当前进程获得前台权限
            # Windows 只允许前台进程调用 SetForegroundWindow
            pyautogui.press('alt')
            time.sleep(0.1)

            # 第一层：尝试 SetForegroundWindow
            try:
                win32gui.SetForegroundWindow(self.main_window_hwnd)
                time.sleep(0.1)
                logger.info("个人微信窗口已激活 (SetForegroundWindow)")
                return True
            except Exception as e1:
                logger.warning(f"SetForegroundWindow 失败: {e1}，尝试第二层...")

            # 第二层：尝试 BringWindowToTop
            try:
                win32gui.BringWindowToTop(self.main_window_hwnd)
                time.sleep(0.1)
                # 再次尝试 SetForegroundWindow
                win32gui.SetForegroundWindow(self.main_window_hwnd)
                time.sleep(0.1)
                logger.info("个人微信窗口已激活 (BringWindowToTop + SetForegroundWindow)")
                return True
            except Exception as e2:
                logger.warning(f"BringWindowToTop 失败: {e2}，尝试第三层...")

            # 第三层：使用 ShowWindow 强制激活
            try:
                win32gui.ShowWindow(self.main_window_hwnd, win32con.SW_SHOW)
                time.sleep(0.1)
                win32gui.SetForegroundWindow(self.main_window_hwnd)
                time.sleep(0.1)
                logger.info("个人微信窗口已激活 (ShowWindow + SetForegroundWindow)")
                return True
            except Exception as e3:
                logger.error(f"所有窗口激活方法均失败: {e3}")
                return False

        except Exception as e:
            logger.error(f"激活个人微信窗口失败: {e}")
            return False

    def search_group(self, group_name: str) -> bool:
        """搜索并进入个人微信群聊"""
        try:
            logger.info(f"搜索个人微信群聊(ctrl+f): {group_name}")

            # 激活微信窗口
            if not self.activate_application():
                return False

            # 使用快捷键打开搜索（Ctrl+F）
            pyautogui.hotkey('ctrl', 'f')
            time.sleep(0.3)
            pyautogui.hotkey('ctrl', 'f')

            # 输入群名搜索
            # 将群名称复制到剪贴板，用于后续粘贴到微信搜索框
            pyperclip.copy(group_name)
            # 全选搜索框中的原有内容（Ctrl+A），确保后续的粘贴可以全部覆盖搜索框
            pyautogui.hotkey('ctrl', 'a')
            time.sleep(0.1)
            # 粘贴剪贴板中的群名称到搜索框（Ctrl+V）
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.5)

            # 按回车选择第一个结果，进入对应的聊天对象窗口
            pyautogui.press('enter')
            time.sleep(0.2)

            # 退到桌面
            pyautogui.hotkey('win', 'd')
            time.sleep(0.1)
            # 再执行下，又退到微信，以便于激活输入框光标
            pyautogui.hotkey('win', 'd')

            logger.info(f"已搜索并进入个人微信群聊: {group_name}")
            return True

        except Exception as e:
            logger.error(f"搜索个人微信群聊失败: {e}")
            return False

    def send_message(self, message: str, target_group: str = None) -> bool:
        """发送消息到个人微信
            
        Args:
            message: 要发送的消息内容
            target_group: 目标群聊名称（可选）
                
        Returns:
            bool: 发送是否成功
        """
        try:
            logger.info("准备发送消息到个人微信")

            # 如果指定了目标群聊，先搜索群聊
            if target_group:
                if not self.search_group(target_group):
                    logger.error(f"搜索群聊失败：{target_group}")
                    return False

            # 确保微信窗口处于前台
            if not self.activate_application():
                return False

            # 将消息复制到剪贴板
            pyperclip.copy(message)
            time.sleep(0.2)

            # 粘贴消息
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(0.5)

            # 发送消息（Alt+S）
            pyautogui.hotkey('alt', 's')
            time.sleep(1)

            logger.info("个人微信消息发送完成")
            return True

        except Exception as e:
            logger.error(f"发送个人微信消息失败：{e}")
            return False

    def cleanup(self) -> bool:
        """清理资源"""
        try:
            logger.info("清理个人微信发送器资源")
            self.wechat_process = None
            self.wechat_pid = None
            self.main_window_hwnd = None
            self.is_initialized = False
            return True
        except Exception as e:
            logger.error(f"清理资源失败: {e}")
            return False

    def send_text(self, text: str, target_group: str) -> bool:
        """发送文本消息到指定的个人微信聊天对象
        
        Args:
            text: 要发送的文本内容
            target_group: 目标聊天对象名称
            
        Returns:
            bool: 发送是否成功
        """
        try:
            # 退到桌面
            pyautogui.hotkey('win', 'd')
            logger.info(f"开始发送文本消息到个人微信：{target_group}")

            # 初始化发送器
            if not self.initialize():
                logger.error("初始化个人微信发送器失败")
                return False

            # 发送消息
            if not self.send_message(text, target_group):
                logger.error("发送消息失败")
                return False

            logger.info(f"个人微信文本消息发送成功！目标：{target_group}")
            return True

        except Exception as e:
            logger.error(f"个人微信自动发送文本消息失败：{e}")
            return False
        finally:
            self.cleanup()

    def send_file(self, file_path: str, target_group: str) -> bool:
        """发送文件到指定的个人微信聊天对象
        
        Args:
            file_path: 本地文件的绝对路径
            target_group: 目标聊天对象名称
            
        Returns:
            bool: 发送是否成功
        """
        try:
            # 退到桌面
            pyautogui.hotkey('win', 'd')
            logger.info(f"开始发送文件到个人微信：{target_group}")

            # 初始化发送器
            if not self.initialize():
                logger.error("初始化个人微信发送器失败")
                return False

            # 搜索目标聊天对象
            if not self.search_group(target_group):
                logger.error(f"搜索聊天对象失败：{target_group}")
                return False

            # 确保微信窗口处于前台
            if not self.activate_application():
                return False

            # 将文件复制到剪贴板
            from core.wechat_sender.file_copy import copy_file_to_clipboard
            copy_file_to_clipboard(file_path)
            time.sleep(0.3)

            # 粘贴文件
            pyautogui.hotkey('ctrl', 'v')
            time.sleep(1)

            # 发送文件（Alt+S）
            pyautogui.hotkey('alt', 's')
            time.sleep(1)

            logger.info(f"个人微信文件发送成功！目标：{target_group}")
            return True

        except Exception as e:
            logger.error(f"个人微信自动发送文件失败：{e}")
            return False
        finally:
            self.cleanup()

    def get_debug_info(self) -> Dict[str, Any]:
        """获取调试信息"""
        try:
            info = super().get_sender_info()

            # 添加个人微信特有信息
            if self.wechat_process:
                info["个人微信进程信息"] = {
                    "PID": self.wechat_pid,
                    "进程名": self.wechat_process.name(),
                    "可执行文件": getattr(self.wechat_process, 'exe', lambda: "无法获取")()
                }

            if self.main_window_hwnd:
                info["窗口信息"] = {
                    "窗口句柄": self.main_window_hwnd,
                    "窗口标题": win32gui.GetWindowText(self.main_window_hwnd),
                    "窗口类名": win32gui.GetClassName(self.main_window_hwnd)
                }

            return info

        except Exception as e:
            logger.error(f"获取调试信息失败: {e}")
            return {"错误": str(e)}

    # ==================== 向后兼容的方法 ====================
    def smart_search_group(self, group_name: str) -> bool:
        """向后兼容：智能搜索群聊"""
        return self.search_group(group_name)

    def send_message_to_current_chat(self, message: str) -> bool:
        """向后兼容：发送消息到当前聊天"""
        return self.send_message(message)

    def interactive_select_process(self) -> bool:
        """向后兼容：交互式选择进程（简化版）"""
        return self.find_target_process()

    def interactive_select_window(self) -> bool:
        """向后兼容：交互式选择窗口（简化版）"""
        return self._find_wechat_windows()


# 注册个人微信发送器到工厂
MessageSenderFactory.register_sender("wechat", WeChatSenderV3)


# ==================== 命令行接口 ====================
def main():
    """主程序入口"""
    import sys
    import json

    # 配置日志
    logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

    sender = WeChatSenderV3()

    if len(sys.argv) > 1:
        command = sys.argv[1]

        if command == "send":
            # 发送文本消息到指定的聊天对象
            # 用法：uv run wechat_sender_v3.py send [聊天对象名] [聊天文本内容]
            if len(sys.argv) < 4:
                print("❌ 参数不足！用法：uv run wechat_sender_v3.py send [聊天对象名] [聊天文本内容]")
                sys.exit(1)

            target_name = sys.argv[2]
            message_text = sys.argv[3]

            success = sender.send_text(message_text, target_name)
            if success:
                print(f"✅ 消息发送成功！目标：{target_name}")
            else:
                print(f"❌ 消息发送失败！目标：{target_name}")
            sleep(0.1)
            print("=" * 60)

        elif command == "debug":
            # 退到桌面
            pyautogui.hotkey('win', 'd')
            # 获取调试信息
            sender.initialize()
            debug_info = sender.get_debug_info()
            print("=== 个人微信调试信息 ===")
            print(json.dumps(debug_info, ensure_ascii=False, indent=2))

        elif command == "test":
            test(sender)

        else:
            print("未知命令。可用命令：")
            print("  send [聊天对象名] [聊天文本内容] - 发送文本消息到个人微信")
            print("  debug - 获取调试信息")
            print("  test - 测试功能")
    else:
        print("个人微信自动发送工具 v3.0 (接口版)")
        print("用法:")
        print("  uv run wechat_sender_v3.py send [聊天对象名] [聊天文本内容] - 发送文本消息")
        print("  uv run wechat_sender_v3.py debug - 获取调试信息")
        print("  uv run wechat_sender_v3.py test - 测试功能")


def test(sender: WeChatSenderV3):
    # 退到桌面
    pyautogui.hotkey('win', 'd')
    # 测试功能
    print("测试个人微信进程查找...")
    if sender.find_target_process():
        print("✅ 个人微信进程查找成功")

        print("测试个人微信窗口查找...")
        if sender._find_wechat_windows():
            print("✅ 个人微信窗口查找成功")

            print("测试窗口激活...")
            if sender.activate_application():
                print("✅ 窗口激活成功")
            else:
                print("❌ 窗口激活失败")
        else:
            print("❌ 个人微信窗口查找失败")
    else:
        print("❌ 个人微信进程查找失败")
    print("=" * 60)


if __name__ == "__main__":
    main()
