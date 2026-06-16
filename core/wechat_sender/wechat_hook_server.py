# -*- coding: utf-8 -*-
"""
微信消息 HTTP Hook 接口
版本：v1.0.0
创建日期：2026-03-08
功能：提供 HTTP 接口接收 POST 请求来发送微信消息
"""

import json
import logging
import os
from http.server import HTTPServer, BaseHTTPRequestHandler
from time import sleep
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any
import threading

import pyautogui
import win32gui

from core.wechat_sender.wechat_sender_v3 import WeChatSenderV3
from core.wechat_sender.file_down import download_file
from core.wechat_sender.file_copy import copy_file_to_clipboard

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class WeChatHookHandler(BaseHTTPRequestHandler):
    """微信 Hook HTTP 请求处理器"""
    
    # 类级别的发送器实例（延迟初始化）
    _sender = None
    _sender_lock = threading.Lock()
    
    def log_message(self, format, *args):
        """重写日志记录方法，不记录请求日志"""
        pass

    def _get_sender(self) -> WeChatSenderV3:
        """获取或创建微信发送器实例"""
        if WeChatHookHandler._sender is None:
            with WeChatHookHandler._sender_lock:
                if WeChatHookHandler._sender is None:
                    WeChatHookHandler._sender = WeChatSenderV3()
        return WeChatHookHandler._sender
    
    def _send_cors_headers(self):
        """发送 CORS 跨域头"""
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
    
    def do_OPTIONS(self):
        """处理 OPTIONS 预检请求"""
        self.send_response(200)
        self._send_cors_headers()
        self.end_headers()
    
    def do_POST(self):
        """处理 POST 请求"""
        try:
            # 解析 URL 路径
            parsed_path = urlparse(self.path)
            
            # 验证路径格式：/wxSend
            if parsed_path.path.strip('/') != 'wxSend':
                self._send_error_response("无效的路径格式", "路径应为：/wxSend")
                return
            
            # 读取请求体
            content_length = int(self.headers.get('Content-Length', 0))
            if content_length == 0:
                self._send_error_response("缺少请求体", "请求体不能为空")
                return
            
            request_body = self.rfile.read(content_length).decode('utf-8')
            
            # 解析 JSON
            try:
                data = json.loads(request_body)
            except json.JSONDecodeError as e:
                self._send_error_response(f"JSON 解析失败", str(e))
                return
            
            # 验证必需的字段
            if 'target' not in data:
                self._send_error_response("缺少必需字段", "请求体必须包含'target'字段（聊天对象名）")
                return
            
            chat_target = data['target']
            
            # 验证聊天对象名
            if not chat_target or not isinstance(chat_target, str):
                self._send_error_response("无效的聊天对象名", "target 必须是非空字符串")
                return
            
            # 获取 content 和 file 字段
            message_content = data.get('content')
            file_path = data.get('file')
            
            # 验证 content 和 file 只能有一个有值
            has_content = message_content is not None and message_content != ''
            has_file = file_path is not None and file_path != ''
            
            if not has_content and not has_file:
                self._send_error_response("缺少消息内容", "请求体必须包含'content'字段（消息内容）或'file'字段（文件路径）")
                return
            
            if has_content and has_file:
                self._send_error_response("参数冲突", "'content'和'file'字段不能同时有值，请只使用其中一个")
                return
            
            sender = self._get_sender()
            
            # 处理文件发送
            if has_file:
                # 判断是否为远程文件
                if file_path.startswith('http://') or file_path.startswith('https://'):
                    logger.info(f"检测到远程文件，开始下载：{file_path}")
                    try:
                        local_file_path = download_file(file_path)
                        logger.info(f"文件下载成功：{local_file_path}")
                    except Exception as e:
                        self._send_error_response("文件下载失败", str(e), status_code=500)
                        return
                else:
                    # 本地文件，验证文件是否存在
                    if not os.path.exists(file_path):
                        self._send_error_response("文件不存在", f"本地文件不存在：{file_path}")
                        return
                    local_file_path = file_path
                
                logger.info(f"收到文件发送请求：目标={chat_target}, 文件={local_file_path}")
                
                # 发送文件
                success = sender.send_file(local_file_path, chat_target)
                
                if success:
                    logger.info(f"文件发送成功：{chat_target}")
                    self._send_success_response({
                        "status": "success",
                        "message": "文件发送成功"
                    })
                else:
                    logger.error(f"文件发送失败：{chat_target}")
                    self._send_error_response(
                        "文件发送失败",
                        "微信文件发送失败，请检查微信是否正常运行",
                        status_code=500
                    )
                sleep(0.1)
                print("=" * 60)
                return
            
            # 处理文本消息发送
            if not isinstance(message_content, str):
                self._send_error_response("无效的消息内容", "content 必须是字符串")
                return
            
            logger.info(f"收到发送请求：目标={chat_target}, 消息长度={len(message_content)}")

            # 发送微信消息
            success = sender.send_text(message_content, chat_target)
            
            if success:
                logger.info(f"消息发送成功：{chat_target}")
                self._send_success_response({
                    "status": "success",
                    "message": "消息发送成功"
                })
            else:
                logger.error(f"消息发送失败：{chat_target}")
                self._send_error_response(
                    "消息发送失败",
                    "微信消息发送失败，请检查微信是否正常运行",
                    status_code=500
                )
            sleep(0.1)
            print("=" * 60)
        
        except Exception as e:
            logger.error(f"处理请求失败：{e}", exc_info=True)
            self._send_error_response("服务器内部错误", str(e), status_code=500)
    
    def do_GET(self):
        """处理 GET 请求（用于测试）"""
        try:
            parsed_path = urlparse(self.path)
            
            # /test 路径：测试微信窗口状态
            if parsed_path.path.strip('/') == 'test':
                logger.info("收到测试请求")

                # 退到桌面
                pyautogui.hotkey('win', 'd')
                
                # 创建发送器实例并执行测试
                sender = self._get_sender()
                
                result = {
                    "service": "WeChat Test API",
                    "tests": []
                }
                
                # 测试 1：查找微信进程
                try:
                    if sender.find_target_process():
                        result["tests"].append({
                            "name": "微信进程查找",
                            "status": "success",
                            "message": "✅ 个人微信进程查找成功",
                            "pid": sender.wechat_pid
                        })
                    else:
                        result["tests"].append({
                            "name": "微信进程查找",
                            "status": "failed",
                            "message": "❌ 个人微信进程查找失败"
                        })
                except Exception as e:
                    result["tests"].append({
                        "name": "微信进程查找",
                        "status": "error",
                        "message": f"❌ 个人微信进程查找异常：{e}"
                    })
                
                # 测试 2：查找微信窗口
                try:
                    if sender._find_wechat_windows():
                        result["tests"].append({
                            "name": "微信窗口查找",
                            "status": "success",
                            "message": "✅ 个人微信窗口查找成功",
                            "window_hwnd": sender.main_window_hwnd,
                            "window_title": win32gui.GetWindowText(sender.main_window_hwnd) if sender.main_window_hwnd and win32gui.IsWindow(sender.main_window_hwnd) else None
                        })
                    else:
                        result["tests"].append({
                            "name": "微信窗口查找",
                            "status": "failed",
                            "message": "❌ 个人微信窗口查找失败"
                        })
                except Exception as e:
                    result["tests"].append({
                        "name": "微信窗口查找",
                        "status": "error",
                        "message": f"❌ 个人微信窗口查找异常：{e}"
                    })
                
                # 测试 3：激活窗口
                try:
                    if sender.activate_application():
                        result["tests"].append({
                            "name": "微信窗口激活",
                            "status": "success",
                            "message": "✅ 窗口激活成功"
                        })
                    else:
                        result["tests"].append({
                            "name": "微信窗口激活",
                            "status": "failed",
                            "message": "❌ 窗口激活失败"
                        })
                except Exception as e:
                    result["tests"].append({
                        "name": "微信窗口激活",
                        "status": "error",
                        "message": f"❌ 窗口激活异常：{e}"
                    })
                
                # 清理资源
                sender.cleanup()
                sleep(0.1)
                print("=" * 60)
                
                # 判断总体状态
                all_success = all(test["status"] == "success" for test in result["tests"])
                result["overall_status"] = "success" if all_success else "failed"
                
                self.send_response(200 if all_success else 500)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self._send_cors_headers()
                self.end_headers()
                
                self.wfile.write(json.dumps(result, ensure_ascii=False).encode('utf-8'))
                return
            
            # 其他路径返回 404
            self._send_error_response("未找到请求的资源", f"未知路径：{parsed_path.path}")
        
        except Exception as e:
            logger.error(f"处理 GET 请求失败：{e}", exc_info=True)
            self._send_error_response("服务器内部错误", str(e), status_code=500)
    
    def _send_success_response(self, data: Dict[str, Any]):
        """发送成功的 JSON 响应"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_cors_headers()
        self.end_headers()
        
        response = {
            **data
        }
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))
    
    def _send_error_response(self, error: str, details: str = None, status_code: int = 400):
        """发送错误的 JSON 响应"""
        self.send_response(status_code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_cors_headers()
        self.end_headers()
        
        response = {
            "code": 1,
            "error": error
        }
        if details:
            response["details"] = details
        
        self.wfile.write(json.dumps(response, ensure_ascii=False).encode('utf-8'))


def run_server(host: str = 'localhost', port: int = 9999):
    """运行 HTTP 服务器"""
    server_address = (host, port)
    logger.info(f"正在启动微信 Hook HTTP 服务器...")
    httpd = HTTPServer(server_address, WeChatHookHandler)

    logger.info("服务启动成功")
    sleep(0.1)
    print()
    print("=" * 60)
    print()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        logger.info("收到停止信号，正在关闭服务器...")
        httpd.shutdown()
        logger.info("服务器已关闭")


def main():
    """主程序入口"""
    import sys
    
    # 默认配置
    default_host = '0.0.0.0'  # 绑定所有网络接口，支持本地和局域网访问
    default_port = 9999
    
    # 解析命令行参数
    host = default_host
    port = default_port
    
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print(f"无效的端口号：{sys.argv[1]}")
            sys.exit(1)
    
    if len(sys.argv) > 2:
        host = sys.argv[2]
    
    # 启动服务器
    run_server(host, port)


if __name__ == "__main__":
    main()
