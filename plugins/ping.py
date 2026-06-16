import time


def handle(content, context):
    raw = context["raw"]

    # SSE 推送不含 messageKey，使用 _ts（WeFlowClient 接收消息时的时间戳）
    start = raw.get("_ts", time.time())

    processing_seconds = time.time() - start
    send_delay_seconds = context.get("planned_send_delay_seconds", 0.0) or 0.0

    return (
        f"砰! (处理消息用时 {processing_seconds:.3f} 秒，"
        f"延迟 {send_delay_seconds:.3f} 秒发送)"
    )
