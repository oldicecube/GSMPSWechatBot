import json
import time
import os
import requests
import glob

# ======================
# ComfyUI 配置
# ======================
COMFY_URL = "http://127.0.0.1:8188"

# 你的 ComfyUI 输出目录（你刚刚指定的）
OUTPUT_DIR = r"D:\Anima\ComfyUI\output"

# workflow 文件
WORKFLOW_PATH = "workflow.json"

# ======================
# 1. 读取 workflow
# ======================
with open(WORKFLOW_PATH, "r", encoding="utf-8") as f:
    workflow = json.load(f)


# ======================
# 2. 提交任务
# ======================
def submit_workflow(workflow):
    r = requests.post(
        f"{COMFY_URL}/prompt",
        json={"prompt": workflow}
    )
    r.raise_for_status()

    data = r.json()
    print("\n[SUBMIT]")
    print(data)

    return data["prompt_id"]


# ======================
# 3. 等待生成完成（只等时间）
# ======================
def wait_generate(seconds=5):
    print(f"\n[WAIT] {seconds}s for generation...")
    time.sleep(seconds)


# ======================
# 4. 获取最新图片
# ======================
def get_latest_image():
    files = glob.glob(os.path.join(OUTPUT_DIR, "*.png"))

    if not files:
        return None

    latest = max(files, key=os.path.getmtime)
    return latest


# ======================
# 5. 主流程
# ======================
def main():
    print("[START] sending workflow...")

    prompt_id = submit_workflow(workflow)

    print("\n[PROMPT ID]", prompt_id)

    # 给 GPU 一点时间（可改大）
    wait_generate(10)

    # 获取最新图片
    img_path = get_latest_image()

    if img_path:
        print("\n[SUCCESS]")
        print(img_path)
    else:
        print("\n[ERROR] No image found in output folder")


if __name__ == "__main__":
    main()