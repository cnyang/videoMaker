"""英文維基百科：小知識影片唯一的資料來源。先抓條目純文字，LLM 只能依抓到的內容寫。

內容採 CC BY-SA 4.0：影片說明欄一定要附條目永久連結與授權（見 plan.make_plan）。
"""

import re
from dataclasses import dataclass
from functools import lru_cache

import requests

API = "https://en.wikipedia.org/w/api.php"
ZH_API = "https://zh.wikipedia.org/w/api.php"
HEADERS = {
    "User-Agent": "videoMaker/1.0 (personal educational video project)"
}  # Wikimedia API 規範要求可辨識的 UA
MIN_CHARS = 1500  # 太短的條目寫不出多則知識
MAX_CHARS = 8000  # 只給 LLM 前面這些，避免撐大 context；也限定了 quote 能引用的範圍


@dataclass(frozen=True)
class Article:
    title: str
    url: str
    revid: int
    text: str
    zh_title: str = ""  # 中文維基條目的台灣正體名稱（zh-tw 變體）；沒有對應條目或查不到時為空

    @property
    def permalink(self) -> str:
        """固定到抓取當時的版本：條目之後被改過，影片的來源仍然指得到原文。"""
        return f"{self.url}?oldid={self.revid}"


def _zh_tw(title: str) -> str:
    """中文維基的條目名稱轉台灣正體（MediaWiki 的 varianttitles）。跨語言連結常是簡體（长颈鹿），手寫的簡體字表抓不完。
    查不到就回空字串：寧可不給中文名稱，也不要把簡體強塞給 LLM。"""
    if not title:
        return ""
    try:
        r = requests.get(
            ZH_API,
            params={
                "action": "query",
                "format": "json",
                "titles": title,
                "prop": "info",
                "inprop": "varianttitles",
            },
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        page = next(iter(r.json()["query"]["pages"].values()))
        tw = page.get("varianttitles", {}).get("zh-tw", "")
    except (requests.RequestException, ValueError, KeyError, StopIteration):
        return ""
    return re.sub(r"\s*[（(].*?[）)]", "", tw).strip()  # 去掉消歧義括號


@lru_cache(maxsize=64)  # 同一晚選主題與產作業用同一份（同一個版本），也少打一次 API；例外不會被快取
def fetch(title: str) -> Article:
    """條目不存在、是消歧義頁或內容太短都丟 ValueError；網路錯誤維持 requests 的例外。"""
    r = requests.get(
        API,
        params={
            "action": "query",
            "format": "json",
            "redirects": 1,
            "titles": title,
            "prop": "extracts|info|revisions|pageprops|langlinks",
            "explaintext": 1,
            "inprop": "url",
            "rvprop": "ids",
            "ppprop": "disambiguation",
            "lllang": "zh",
            "lllimit": 1,
        },
        headers=HEADERS,
        timeout=30,
    )
    r.raise_for_status()
    page = next(iter(r.json()["query"]["pages"].values()))
    if "missing" in page:
        raise ValueError(f"維基百科沒有條目：{title}")
    if "disambiguation" in page.get("pageprops", {}):
        raise ValueError(f"{title} 是消歧義頁，請改用具體條目名稱")
    text = page.get("extract", "")
    if len(text) < MIN_CHARS:
        raise ValueError(f"{page['title']} 條目太短（{len(text)} 字元 < {MIN_CHARS}）")
    zh = _zh_tw((page.get("langlinks") or [{}])[0].get("*", ""))
    return Article(page["title"], page["fullurl"], page["revisions"][0]["revid"], text[:MAX_CHARS], zh)
