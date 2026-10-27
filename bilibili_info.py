"""直接读取 B 站视频信息和 dm/view 字幕；不使用播放器字幕回退。"""
from dataclasses import asdict, dataclass
from enum import Enum
from http.client import HTTPException
import http.cookiejar
import json
import math
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse

from cookie_debug import cookie_check, cookie_operation


class SubtitleState(str, Enum):
    CHINESE = "chinese"
    OTHER_LANGUAGES = "other_languages"
    NO_SUBTITLES = "no_subtitles"
    ACCESS_DENIED = "access_denied"
    REQUEST_FAILED = "request_failed"


class BilibiliError(Exception):
    pass


class AccessDenied(BilibiliError):
    state = SubtitleState.ACCESS_DENIED


class RequestFailed(BilibiliError):
    state = SubtitleState.REQUEST_FAILED


@dataclass(frozen=True)
class VideoInfo:
    bvid: str
    aid: int
    cid: int
    page: int
    title: str
    desc: str
    duration: float
    url: str

    def metadata(self):
        return asdict(self)


@dataclass(frozen=True)
class VideoResult:
    info: VideoInfo
    state: SubtitleState
    text: str = ""
    languages: tuple[str, ...] = ()


def is_chinese(language: str) -> bool:
    code = language.lower().replace("_", "-")
    if code.startswith("ai-"):
        code = code[3:]
    return code.split("-")[0] in {"zh", "cmn", "yue"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BilibiliRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        parsed = urlparse(newurl)
        if parsed.scheme not in {"http", "https"} or parsed.hostname not in {
            "www.bilibili.com", "bilibili.com", "m.bilibili.com", "b23.tv", "www.b23.tv",
            "bili2233.cn", "www.bili2233.cn",
        }:
            raise RequestFailed("短链接重定向目标异常。")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class BilibiliClient:
    def __init__(self, cookies: Path | None = None, timeout: float = 120):
        self.cookies = cookies
        self.timeout = timeout
        self.jar = http.cookiejar.MozillaCookieJar()
        if cookies and cookie_check("BilibiliClient.__init__", "file_exists", cookies.exists):
            try:
                cookie_check("BilibiliClient.__init__", "netscape_parse_expiry_filter",
                             lambda: self.jar.load(str(cookies), ignore_discard=True, ignore_expires=False))
            except (OSError, ValueError):
                raise RequestFailed("Cookie 配置错误：需要有效的 Netscape Cookie 文件。") from None
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())
        self.body_opener = urllib.request.build_opener(NoRedirect())
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }

    def _json(self, url, label, body=False):
        if not body:
            cookie_operation("BilibiliClient._json", "http_cookie_processor_selection")
        try:
            request = urllib.request.Request(url, headers=self.headers)
            with (self.body_opener if body else self.opener).open(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise RequestFailed(f"{label}请求失败：HTTP {status}。") from None
        except (OSError, urllib.error.URLError, HTTPException):
            raise RequestFailed(f"{label}网络请求失败或超时。") from None
        except ValueError:
            raise RequestFailed(f"{label}响应不是有效 JSON。") from None
        if not isinstance(payload, dict):
            raise RequestFailed(f"{label}响应结构异常。")
        return payload

    def _api(self, path, params=None):
        payload = self._json("https://api.bilibili.com" + path + ("?" + urlencode(params) if params else ""), path)
        code = payload.get("code")
        if code == -101:
            raise AccessDenied("B 站要求有效登录，请更新 Netscape Cookie。")
        if type(code) is not int or code != 0:
            raise RequestFailed(f"{path}业务请求失败：code={code if type(code) is int else '异常'}。")
        data = payload.get("data")
        if not isinstance(data, dict):
            raise RequestFailed(f"{path}缺少有效 data 对象。")
        return data

    def _retry(self, action):
        for attempt in range(2):
            try:
                return action()
            except RequestFailed:
                if attempt:
                    raise
                print("B 站信息或字幕读取异常，重试一次……", file=sys.stderr)

    def _resolve(self, url):
        parsed = urlparse(url)
        if parsed.hostname in {"b23.tv", "www.b23.tv", "bili2233.cn", "www.bili2233.cn"}:
            try:
                opener = urllib.request.build_opener(BilibiliRedirect())
                with opener.open(urllib.request.Request(url, headers=self.headers), timeout=self.timeout) as response:
                    parsed = urlparse(response.url)
            except (OSError, urllib.error.URLError, HTTPException, ValueError):
                raise RequestFailed("B 站短链接解析失败。") from None
        match = re.fullmatch(r"/video/(BV[1-9A-HJ-NP-Za-km-z]{10})/?", parsed.path)
        if parsed.hostname not in {"www.bilibili.com", "bilibili.com", "m.bilibili.com"} or not match:
            raise RequestFailed("无法从链接取得有效 BV 号。")
        try:
            page = int(parse_qs(parsed.query).get("p", ["1"])[0])
            if page < 1:
                raise ValueError()
        except ValueError:
            raise RequestFailed("视频分 P 必须为正整数。") from None
        return match[1], page

    @staticmethod
    def _permission(data):
        exclusive = data.get("is_upower_exclusive")
        play = data.get("is_upower_play")
        if not isinstance(exclusive, bool):
            raise RequestFailed("视频访问权限字段缺失或异常，无法确认。")
        if exclusive:
            if play is False:
                raise AccessDenied("付费或订阅权限不足：当前账号不能完整观看该专属视频，停止下载。")
            if play is not True:
                raise RequestFailed("专属视频的完整观看权限无法确认。")
        rights = data.get("rights")
        if not isinstance(rights, dict):
            raise RequestFailed("视频 rights 结构异常，无法确认访问权限。")
        # 这些收费类型尚无已验证的已购权益字段，不能由标价推断已获授权。
        if any(rights.get(k) for k in ("pay", "ugc_pay", "arc_pay")):
            raise RequestFailed("视频包含付费标记，当前接口无法确认已购权限，停止下载。")

    def _info(self, bvid, page):
        data = self._api("/x/web-interface/view", {"bvid": bvid})
        self._permission(data)
        if data.get("bvid") != bvid or type(data.get("aid")) is not int:
            raise RequestFailed("视频信息的 BV/AID 不匹配或缺失。")
        pages = data.get("pages")
        if not isinstance(pages, list):
            raise RequestFailed("视频分 P 列表异常。")
        parts = [p for p in pages if isinstance(p, dict) and p.get("page") == page]
        if len(parts) != 1 or type(parts[0].get("cid")) is not int:
            raise RequestFailed("请求的分 P 不存在或 CID 异常。")
        part = parts[0]
        duration = part.get("duration", data.get("duration") if page == 1 else None)
        if (not isinstance(duration, (int, float)) or isinstance(duration, bool) or
                not math.isfinite(duration) or duration <= 0):
            raise RequestFailed("视频分 P 时长异常。")
        if not isinstance(data.get("title"), str) or not data["title"].strip() or not isinstance(data.get("desc"), str):
            raise RequestFailed("视频标题或简介结构异常。")
        return VideoInfo(bvid, data["aid"], part["cid"], page, data["title"], data["desc"], duration,
                         f"https://www.bilibili.com/video/{bvid}/?p={page}")

    def _login(self):
        data = cookie_check("BilibiliClient._login", "server_auth",
                            lambda: self._api("/x/web-interface/nav"))
        if data.get("isLogin") is not True:
            raise AccessDenied("Cookie 未登录或已失效，停止 ASR 下载。")

    def require_download_cookie(self):
        if not self.cookies or not cookie_check("BilibiliClient.require_download_cookie", "file_is_file", self.cookies.is_file) or not cookie_check(
            "BilibiliClient.require_download_cookie", "bilibili_domain_presence",
            lambda: any(c.domain.lstrip(".") == "bilibili.com" or c.domain.endswith(".bilibili.com") for c in self.jar)
        ):
            raise AccessDenied("ASR 下载需要有效的 B 站 Netscape Cookie 文件。")
        self._retry(self._login)

    def _body(self, track, info):
        url = track.get("subtitle_url")
        if not isinstance(url, str) or not url:
            raise RequestFailed("中文字幕轨道缺少正文地址，不能按无字幕处理。")
        if url.startswith("//"):
            url = "https:" + url
        if url.startswith("http://"):
            url = "https://" + url[7:]
        try:
            parsed = urlparse(url)
            valid_url = (parsed.scheme == "https" and parsed.hostname == "aisubtitle.hdslb.com" and
                         parsed.port in (None, 443) and not parsed.username and not parsed.password and not parsed.fragment)
        except ValueError:
            valid_url = False
        if not valid_url:
            raise RequestFailed("中文字幕正文地址不属于已验证的 HTTPS 字幕源。")
        data = self._json(url, "中文字幕正文", body=True)
        for key, expected in (("bvid", info.bvid), ("aid", info.aid), ("cid", info.cid)):
            if key in data and data[key] != expected:
                raise RequestFailed("中文字幕正文的视频标识不匹配。")
        if not isinstance(data.get("lang"), str) or not is_chinese(data["lang"]):
            raise RequestFailed("中文字幕正文语言与轨道不符或缺失。")
        lines = data.get("body")
        if not isinstance(lines, list) or not lines:
            raise RequestFailed("中文字幕正文为空或结构异常。")
        text = []
        for line in lines:
            if not isinstance(line, dict) or not isinstance(line.get("content"), str):
                raise RequestFailed("中文字幕段落结构异常。")
            start, end = line.get("from"), line.get("to")
            if any(type(x) not in (int, float) or not math.isfinite(x) for x in (start, end)) or not 0 <= start <= end <= info.duration + 5:
                raise RequestFailed("中文字幕时间范围与目标分 P 不符。")
            if line["content"].strip():
                text.append(line["content"].strip())
        if not text:
            raise RequestFailed("中文字幕正文没有有效文本。")
        return "\n".join(text)

    def _fetch(self, url, preferred_lang):
        bvid, page = self._resolve(url)
        self.headers["Referer"] = f"https://www.bilibili.com/video/{bvid}/"
        info = self._info(bvid, page)
        data = self._api("/x/v2/dm/view", {"aid": info.aid, "oid": info.cid, "type": 1})
        if data.get("need_login_subtitle") is True:
            raise AccessDenied("字幕接口要求登录或权限，不能判定为无字幕。")
        subtitle = data.get("subtitle")
        if not isinstance(subtitle, dict) or "subtitles" not in subtitle:
            raise RequestFailed("字幕列表字段缺失，不能判定为无字幕。")
        tracks = subtitle["subtitles"]
        if tracks is None or tracks == []:
            # 匿名空列表不能证明没有隐藏轨道；仅空列表判定需要验证登录。
            try:
                self._login()
            except AccessDenied:
                raise RequestFailed("匿名或失效登录下返回空字幕列表，无法确认是否真的没有字幕。") from None
            return VideoResult(info, SubtitleState.NO_SUBTITLES)
        if not isinstance(tracks, list):
            raise RequestFailed("字幕列表类型异常。")
        for track in tracks:
            if (not isinstance(track, dict) or not isinstance(track.get("lan"), str) or
                    not re.fullmatch(r"(?:ai-)?[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8})*", track["lan"])):
                raise RequestFailed("字幕语言字段异常，无法确认状态。")
        languages = tuple(t["lan"] for t in tracks)
        chinese = [t for t in tracks if is_chinese(t["lan"])]
        if not chinese:
            return VideoResult(info, SubtitleState.OTHER_LANGUAGES, languages=languages)
        unlocked = [t for t in chinese if not t.get("is_lock")]
        if not unlocked:
            raise AccessDenied("中文字幕轨道被锁定，停止下载。")
        unlocked.sort(key=lambda t: t["lan"] != preferred_lang)
        track = unlocked[0]
        if "ai_status" in track and track["ai_status"] != 2:
            raise RequestFailed("中文字幕状态尚未验证为可用，不能启动 ASR。")
        return VideoResult(info, SubtitleState.CHINESE, self._body(track, info), languages)

    def fetch(self, url, preferred_lang="zh-CN"):
        return self._retry(lambda: self._fetch(url, preferred_lang))
