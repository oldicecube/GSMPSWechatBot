import time


def handle(content, context):
    raw = context["raw"]

    key = raw.get("messageKey", "")

    try:
        ts_ms = int(key.split(":")[3])  # 提取时间戳
        start = ts_ms / 1000
    except:
        start = raw.get("_ts", time.time())

    processing_seconds = time.time() - start
    send_delay_seconds = context.get("planned_send_delay_seconds", 0.0) or 0.0

    return (
        f"砰! (处理消息用时 {processing_seconds:.3f} 秒，"
        f"延迟 {send_delay_seconds:.3f} 秒发送)"
    )
