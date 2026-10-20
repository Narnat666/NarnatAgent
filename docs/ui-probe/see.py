"""识图 —— 把界面截图交给视觉模型，让它描述/挑毛病。

供 AI 测试装置使用：截图（Edge headless --screenshot）→ 本脚本 → 文字结论。
依赖只用标准库；密钥与接口地址从 narnat.json 读，不硬编码。

用法：
    python see.py <图片路径> ["提示词"] [--max-tokens N] [--config 路径]

    python see.py shot.png
    python see.py shot.png "评估对齐、密度、对比度、缺陷，分点简答，200字内"
    python see.py shot.png "这张图有什么" --max-tokens 3000

提示：该视觉模型会先输出 thinking 且计入 max_tokens，给太小会导致无正文
（脚本会检测并提示调大，默认 4000）。

配置查找顺序（第一个存在的即用）：
    1. --config 指定
    2. 环境变量 NARNAT_CONFIG
    3. 仓库内 .narnat/config/narnat.json（相对本脚本定位，仓库可整体移动）
    4. 仓库向上若干级的 .narnat/config/narnat.json（源码模式在子目录里跑时）
    5. 用户主目录 .narnat/config/narnat.json（装成 exe 后的默认位置）
"""

import argparse
import base64
import json
import os
import ssl
import sys
import urllib.request

VISION_MODEL = "deepseek-v4-flash-vision-exp"
DEFAULT_PROMPT = (
    "评估这个界面：1) 元素对齐与错位 2) 信息密度是否过挤 3) 配色对比度 "
    "4) 明显缺陷。分点简答，不要长篇思考。"
)

def _config_candidates():
    """按「离脚本最近 → 最远」排出候选配置路径。

    原来这里写死了一个 D 盘的绝对路径当兜底，仓库一移动就失效，
    而且别人的机器上根本不存在那个盘。改成从脚本位置向上找 .narnat，
    再加用户主目录（装成 exe 后后端把配置放在那里）。
    """
    here = os.path.dirname(os.path.abspath(__file__))
    out = []
    # 3/4. 从脚本所在目录一路向上找仓库根的 .narnat
    cur = here
    for _ in range(6):
        out.append(os.path.join(cur, ".narnat", "config", "narnat.json"))
        parent = os.path.dirname(cur)
        if parent == cur:
            break
        cur = parent
    # 5. 用户主目录
    try:
        out.append(os.path.join(os.path.expanduser("~"), ".narnat", "config", "narnat.json"))
    except Exception:
        pass
    return out


CANDIDATES = [os.environ.get("NARNAT_CONFIG", "")] + _config_candidates()


def find_config(explicit=None):
    paths = ([explicit] if explicit else []) + CANDIDATES
    for p in paths:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return None


def load_ai(cfg_path):
    with open(cfg_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    ai = data.get("智能体") or data
    key = ai.get("接口密钥") or ""
    base = (ai.get("接口地址") or "").rstrip("/")
    if not key or not base:
        raise SystemExit(f"[错误] {cfg_path} 缺少 接口密钥 或 接口地址")
    return key, base + "/v1/messages"


def ask(key, url, img_path, prompt, model, max_tokens):
    with open(img_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("ascii")
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image",
                 "source": {"type": "base64", "media_type": "image/png", "data": b64}},
                {"type": "text", "text": prompt},
            ],
        }],
    }).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST", headers={
        "content-type": "application/json",
        "x-api-key": key,
        "anthropic-version": "2023-06-01",
    })
    ctx = ssl.create_default_context()
    with urllib.request.urlopen(req, timeout=300, context=ctx) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    ap = argparse.ArgumentParser(description="界面截图交给视觉模型评审")
    ap.add_argument("image")
    ap.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--model", default=VISION_MODEL)
    ap.add_argument("--config")
    args = ap.parse_args()

    if not os.path.isfile(args.image):
        raise SystemExit(f"[错误] 图片不存在: {args.image}")
    cfg = find_config(args.config)
    if not cfg:
        raise SystemExit("[错误] 找不到 narnat.json，请用 --config 指定")
    key, url = load_ai(cfg)

    try:
        data = ask(key, url, args.image, args.prompt, args.model, args.max_tokens)
    except urllib.error.HTTPError as e:
        raise SystemExit(f"[HTTP {e.code}] {e.read().decode('utf-8', 'replace')[:400]}")
    except Exception as e:
        raise SystemExit(f"[请求失败] {type(e).__name__}: {e}")

    texts = [c.get("text", "") for c in data.get("content", [])
             if c.get("type") == "text" and c.get("text", "").strip()]
    if texts:
        print("\n".join(texts))
    else:
        print("[无正文输出] stop_reason=" + str(data.get("stop_reason"))
              + "，思考占满 max_tokens，请调大 --max-tokens", file=sys.stderr)
        th = next((c for c in data.get("content", []) if c.get("type") == "thinking"), None)
        if th:
            print("[thinking 片段] " + th.get("thinking", "")[-300:], file=sys.stderr)
        return 1

    u = data.get("usage", {})
    print(f"\n[usage] in={u.get('input_tokens')} out={u.get('output_tokens')} "
          f"stop={data.get('stop_reason')} config={cfg}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
