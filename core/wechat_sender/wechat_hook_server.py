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
from socketserver import ThreadingMixIn
from time import sleep
from urllib.parse import urlparse, parse_qs
from typing import Dict, Any, Optional
import threading

import pyautogui
import win32gui

from core.wechat_sender.wechat_sender_v3 import WeChatSenderV3
from core.wechat_sender.file_down import download_file, download_voice_file, is_in_voice_temp_dir
from core.wechat_sender.file_copy import copy_file_to_clipboard
from core.wechat_sender.send_queue import PrioritySendQueue, SendQueueClosed
from core.wechat_sender.mention_markup import MentionMarkupError, parse_and_resolve_mentions, resolve_native_segments

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Process-wide listener state. The bot starts this once and senders reuse it.
_httpd_lock = threading.RLock()
_httpd = None
_send_queue = None
_send_queue_lock = threading.RLock()


class PersistentHTTPServer(ThreadingMixIn, HTTPServer):
    """Concurrent HTTP front-end backed by one serialized UI worker."""

    allow_reuse_address = True
    daemon_threads = True
    block_on_close = True


class WeChatHookHandler(BaseHTTPRequestHandler):
    """微信 Hook HTTP 请求处理器"""
    
    # 类级别的发送器实例（延迟初始化）
    _sender = None
    _sender_lock = threading.Lock()
    _sender_config = {}
    
    def log_message(self, format, *args):
        """重写日志记录方法，不记录请求日志"""
        pass

    @classmethod
    def _get_sender(cls) -> WeChatSenderV3:
        """Return the process-wide sender shared by HTTP handlers and idle hooks."""
        if cls._sender is None:
            with cls._sender_lock:
                if cls._sender is None:
                    cls._sender = WeChatSenderV3(cls._sender_config)
        return cls._sender

    def _submit_send(self, operation, data, kind):
        """Run one UI operation through the process-wide priority queue."""
        with _send_queue_lock:
            queue = _send_queue
        if queue is None:
            raise RuntimeError("send queue is not running")

        source = str(data.get("source") or data.get("origin") or "bot").strip().lower()
        if str(data.get("kind") or kind).strip().lower() == "bulk-image":
            source = PrioritySendQueue.BULK_SOURCE
        # The Print Server marks task/payment-result notifications explicitly.
        # Keep a compatibility fallback for older callers that only provided kind.
        is_print = source in {"print", "printing", "print-server", "print_server"}
        if not is_print:
            is_print = str(data.get("kind") or kind).strip().lower() in {"task", "payment-result", "payment_result", "print"}
        if source == PrioritySendQueue.BULK_SOURCE:
            priority = PrioritySendQueue.BULK_PRIORITY
        elif is_print:
            priority = PrioritySendQueue.PRINT_PRIORITY
        else:
            try:
                priority = max(PrioritySendQueue.NORMAL_PRIORITY, int(data.get("priority", PrioritySendQueue.NORMAL_PRIORITY)))
            except (TypeError, ValueError):
                priority = PrioritySendQueue.NORMAL_PRIORITY
        logger.info("queue %s source=%s priority=%s pending=%s", kind, source, priority, queue.pending)
        try:
            return queue.submit(operation, priority=priority, source=source)
        except SendQueueClosed:
            raise RuntimeError("send queue is closed")
    
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
            
            # 获取 content / file / voice / duration 字段
            message_content = data.get('content')
            file_path = data.get('file')
            voice_path = data.get('voice')
            duration = data.get('duration')
            voice_start = data.get('start')
            
            # 验证 content / file / voice 只能有一个有值
            has_content = message_content is not None and message_content != ''
            has_file = file_path is not None and file_path != ''
            has_voice = voice_path is not None and str(voice_path).strip() != ''

            if not has_content and not has_file and not has_voice:
                self._send_error_response(
                    "缺少消息内容",
                    "请求体必须包含'content'（消息内容）、'file'（文件路径）或'voice'（语音文件路径）之一"
                )
                return

            if (has_content and has_file) or (has_content and has_voice) or (has_file and has_voice):
                self._send_error_response(
                    "参数冲突",
                    "'content'、'file'和'voice'字段不能同时有值，请只使用其中一个"
                )
                return
            
            sender = self._get_sender()
            
            # 处理语音发送
            if has_voice:
                # 校验语音时长（大于 0~60 秒，支持小数）
                try:
                    duration = float(duration) if duration is not None else None
                except (TypeError, ValueError):
                    self._send_error_response("无效的语音时长", "duration 必须是大于 0 且不超过 60 的数字（支持小数）")
                    return
                if duration is not None and (duration <= 0 or duration > 60):
                    self._send_error_response("语音时长超出限制", "duration 必须大于 0 且不超过 60 秒")
                    return
                # 校验起始秒（非负，支持小数；非法按 0 处理）
                try:
                    voice_start = float(voice_start) if voice_start is not None else None
                except (TypeError, ValueError):
                    self._send_error_response("无效的起始秒", "start 必须是非负数字（支持小数）")
                    return
                if voice_start is not None and voice_start < 0:
                    voice_start = 0.0

                local_voice_path = None
                try:
                    # 判断是否为远程文件
                    if str(voice_path).startswith('http://') or str(voice_path).startswith('https://'):
                        logger.info(f"检测到远程语音文件，开始下载：{voice_path}")
                        try:
                            local_voice_path = download_voice_file(voice_path)
                            logger.info(f"语音文件下载成功：{local_voice_path}")
                        except Exception as e:
                            self._send_error_response("语音文件下载失败", str(e), status_code=500)
                            return
                    else:
                        # 本地文件，验证文件是否存在
                        if not os.path.exists(voice_path):
                            self._send_error_response("语音文件不存在", f"本地语音文件不存在：{voice_path}")
                            return
                        local_voice_path = voice_path

                    logger.info(f"收到语音发送请求：目标={chat_target}, 语音={local_voice_path}, 时长={duration}, 起始={voice_start}")

                    # 发送语音
                    success = self._submit_send(lambda: sender.send_voice(local_voice_path, chat_target, duration, voice_start), data, "voice")

                    if success:
                        logger.info(f"语音发送成功：{chat_target}")
                        self._send_success_response({
                            "status": "success",
                            "message": "语音发送成功"
                        })
                    else:
                        logger.error(f"语音发送失败：{chat_target}")
                        self._send_error_response(
                            "语音发送失败",
                            "微信语音发送失败，请检查微信是否正常运行、是否已安装 VB-CABLE 并选中 CABLE Output 作为麦克风",
                            status_code=500
                        )
                finally:
                    # 发送完成后，由发送组件删除统一语音临时目录中的音频文件
                    if local_voice_path and is_in_voice_temp_dir(local_voice_path):
                        try:
                            os.remove(local_voice_path)
                            logger.info(f"已删除语音临时文件：{local_voice_path}")
                        except OSError as e:
                            logger.warning(f"删除语音临时文件失败：{local_voice_path}，原因：{e}")
                sleep(0.1)
                print("=" * 60)
                return

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
                success = self._submit_send(lambda: sender.send_file(local_file_path, chat_target), data, "file")
                
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
            
            logger.info("received text send request target=%s chars=%s", chat_target, len(message_content))
            try:
                # Exact-text plugins must not expand literal [[WX_AT:...]].
                mention_plan = resolve_native_segments(data["native_segments"], data.get("group_id") or chat_target, message_content) if "native_segments" in data else (
                    None if data.get("preserve_text") is True and data.get("resolve_mentions") is not True
                    else parse_and_resolve_mentions(message_content, data.get("group_id") or chat_target)
                )
            except MentionMarkupError as exc:
                self._send_error_response("Invalid @ marker", str(exc), status_code=400)
                return

            if mention_plan is None:
                success = self._submit_send(
                    lambda: sender.send_text(message_content, chat_target), data, "text"
                )
                display_content = message_content
            else:
                mention_segments, display_content = mention_plan
                mention_count = sum(1 for item in mention_segments if item.get("type") == "mention")
                logger.info(
                    "resolved group-verified mention markers target=%s count=%s display=%s",
                    chat_target, mention_count, display_content[:240],
                )
                success = self._submit_send(
                    lambda: sender.send_text_with_mentions(mention_segments, chat_target),
                    data,
                    "text",
                )

            if success:
                logger.info("text send succeeded: %s", chat_target)
                self._send_success_response({
                    "status": "success",
                    "message": "Message sent",
                    "display_content": display_content,
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
            
            # Safe policy check used before a bulk sender starts: no desktop/UI.
            if parsed_path.path.strip('/') == 'queue/status':
                with _send_queue_lock:
                    queue = _send_queue
                if queue is None:
                    self._send_error_response("send queue is not running", status_code=503)
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self._send_cors_headers()
                self.end_headers()
                self._write_json_body({"service": "wxsendhook", **queue.snapshot()})
                return

            # /voice/status 路径：虚拟麦克风注入器状态
            if parsed_path.path.strip('/') == 'voice/status':
                from core.wechat_sender.virtual_mic import VirtualMicInjector
                sender = self._get_sender()
                injector = VirtualMicInjector(
                    playback_device=sender.voice_playback_device,
                    capture_device=sender.voice_capture_device,
                )
                result = {
                    "service": "WeChat Voice Status API",
                    "dry_run": os.environ.get('WECHAT_SENDER_DRY_RUN', '0') == '1',
                    "virtual_mic": injector.describe(),
                }
                self.send_response(200 if injector.is_available() else 503)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self._send_cors_headers()
                self.end_headers()
                self._write_json_body(result)
                return

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
                
                self._write_json_body(result)
                return
            
            # 其他路径返回 404
            self._send_error_response("未找到请求的资源", f"未知路径：{parsed_path.path}")
        
        except Exception as e:
            logger.error(f"处理 GET 请求失败：{e}", exc_info=True)
            self._send_error_response("服务器内部错误", str(e), status_code=500)
    
    def _write_json_body(self, payload: Dict[str, Any]):
        """Write a response while treating a client disconnect as non-fatal.

        WinError 10053/10054 and BrokenPipeError mean the HTTP peer closed the
        connection while the bot was still producing its response. This is a
        transport-side secondary error, not an indication that the WeChat or
        LLM operation failed, so avoid a noisy traceback and return to the
        request loop.
        """
        try:
            self.wfile.write(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError) as exc:
            logger.info("HTTP client disconnected before response was written: %s", exc)
        except OSError as exc:
            if getattr(exc, "winerror", None) in {10053, 10054}:
                logger.info("HTTP client disconnected before response was written: %s", exc)
                return
            raise

    def _send_success_response(self, data: Dict[str, Any]):
        """发送成功的 JSON 响应"""
        self.send_response(200)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self._send_cors_headers()
        self.end_headers()
        
        response = {
            **data
        }
        self._write_json_body(response)
    
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
        
        self._write_json_body(response)


def run_server(
    host: str = 'localhost',
    port: int = 9999,
    config: Dict[str, Any] = None,
    ready_event: Optional[threading.Event] = None,
    startup_state: Optional[Dict[str, Any]] = None,
):
    """Run the process-wide wxSend listener until explicit shutdown.

    The startup wrapper waits for ``ready_event`` so a started thread is not
    mistaken for a successfully bound HTTP socket.
    """
    global _httpd, _send_queue
    WeChatHookHandler._sender_config = config if isinstance(config, dict) else {}
    queue = PrioritySendQueue()
    def _configured_idle_account(raw_config):
        raw_config = raw_config if isinstance(raw_config, dict) else {}
        # Accept the two historical names and the optional sender section, but
        # always prefer the explicitly documented current_wechat value.
        candidates = [
            raw_config.get("current_wechat"),
            raw_config.get("current_wx_account"),
            (raw_config.get("wechat_sender") or {}).get("current_wechat")
            if isinstance(raw_config.get("wechat_sender"), dict) else None,
            (raw_config.get("wechat_sender") or {}).get("current_wx_account")
            if isinstance(raw_config.get("wechat_sender"), dict) else None,
        ]
        for value in candidates:
            value = str(value or "").strip()
            if value:
                return value
        return ""

    idle_account = _configured_idle_account(config)
    sender_section = config.get("wechat_sender") if isinstance(config, dict) and isinstance(config.get("wechat_sender"), dict) else {}
    # Automatic UI restoration is opt-in. It can move the WeChat window while
    # a second request is arriving; normal sends must never trigger that race.
    auto_restore_idle = bool(
        sender_section.get("auto_restore_idle", False)
        or sender_section.get("restore_idle_account", False)
        or (config.get("auto_restore_idle", False) if isinstance(config, dict) else False)
    )

    def _restore_idle_account():
        logger.info("send queue idle callback fired; target_account=%r", idle_account)
        print(f"[WX SEND IDLE] queue empty; switching chat to {idle_account!r}", flush=True)
        if not idle_account:
            logger.warning("send queue idle callback skipped: no current_wechat/current_wx_account configured")
            return
        try:
            sender = WeChatHookHandler._get_sender()
            ok = bool(sender.switch_chat(idle_account))
            if ok:
                logger.info("send queue idle: switched chat to %r", idle_account)
                print(f"[WX SEND IDLE] switched chat to {idle_account!r}", flush=True)
            else:
                logger.warning("send queue idle: failed to switch chat to %r", idle_account)
                print(f"[WX SEND IDLE] switch failed for {idle_account!r}", flush=True)
        except Exception as exc:
            logger.exception("send queue idle switch failed target=%r", idle_account)
            print(f"[WX SEND IDLE] switch exception: {type(exc).__name__}: {exc}", flush=True)

    logger.info(
        "send queue configured idle target=%r codepoints=%s",
        idle_account,
        [hex(ord(ch)) for ch in idle_account],
    )
    if idle_account and auto_restore_idle:
        queue.set_idle_callback(_restore_idle_account)
    elif idle_account:
        logger.info("automatic idle chat restoration disabled; set wechat_sender.auto_restore_idle=true to opt in")
    queue.start()
    with _send_queue_lock:
        _send_queue = queue
    logger.info("send queue started; idle_target=%r auto_restore_idle=%s", idle_account, auto_restore_idle)
    server_address = (host, port)
    httpd = None
    try:
        logger.info("starting persistent WeChat Hook HTTP service...")
        httpd = PersistentHTTPServer(server_address, WeChatHookHandler)
        with _httpd_lock:
            _httpd = httpd
        if startup_state is not None:
            startup_state["bound"] = True
        if ready_event is not None:
            ready_event.set()

        logger.info("persistent WeChat Hook HTTP service is ready on %s:%s", host, port)
        httpd.serve_forever(poll_interval=0.5)
    except Exception as exc:
        if startup_state is not None:
            startup_state["error"] = exc
        if ready_event is not None:
            ready_event.set()
        logger.error("WeChat Hook HTTP service stopped: %s", exc, exc_info=not isinstance(exc, OSError))
    finally:
        with _send_queue_lock:
            active_queue = _send_queue
            _send_queue = None
        if active_queue is not None:
            try:
                active_queue.stop(drain=True)
            except Exception:
                logger.exception("failed to drain send queue during shutdown")
        with _httpd_lock:
            if _httpd is httpd:
                _httpd = None
        if httpd is not None:
            try:
                httpd.server_close()
            except Exception:
                logger.debug("failed to close WeChat Hook HTTP service", exc_info=True)
        logger.info("persistent WeChat Hook HTTP service closed")


def shutdown_server() -> None:
    """Request shutdown; the run thread performs the final close."""
    with _httpd_lock:
        httpd = _httpd
    if httpd is not None:
        httpd.shutdown()


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
