"""WebFetch工具 —— 根据URL抓取网页完整内容"""


def execute(url: str) -> str:
    """
    抓取网页完整内容。

    Args:
        url: 目标网页URL

    Returns:
        网页正文内容（纯文本）
    """
    try:
        import requests
    except ImportError:
        return "错误: 需要requests库，请pip install requests"

    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
    except requests.exceptions.HTTPError as e:
        return f"错误: HTTP {resp.status_code}: {url}"
    except requests.exceptions.Timeout:
        return f"错误: 请求超时: {url}"
    except requests.exceptions.ConnectionError:
        return f"错误: 连接失败: {url}"
    except Exception as e:
        return f"错误: {e}"

    # 提取正文
    return _extract_text(resp.text)


def _extract_text(html: str) -> str:
    """从HTML提取正文文本"""
    # 优先用html2text
    try:
        import html2text
        converter = html2text.HTML2Text()
        converter.ignore_links = False
        converter.ignore_images = True
        converter.body_width = 0
        return converter.handle(html).strip()
    except ImportError:
        pass

    # 降级用BeautifulSoup
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        # 移除script/style
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        return soup.get_text(separator="\n", strip=True)
    except ImportError:
        pass

    # 最终降级：简单正则去标签
    import re
    text = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.DOTALL)
    text = re.sub(r"<style[^>]*>.*?</style>", "", text, flags=re.DOTALL)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:50000]  # 限制长度
