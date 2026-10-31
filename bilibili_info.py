"""直接读取 B 站视频信息和 dm/view 字幕；不使用播放器字幕回退。"""
from dataclasses import asdict, dataclass
from http.client import HTTPException
import http.cookiejar
import json
import math
from pathlib import Path
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from exception import (SubtitleState, BilibiliError, AccessDenied, RequestFailed, ConfigurationError,
                       InvalidVideoURL, TransientRequestFailed, bilibili_http_error, bilibili_network_error)

from cookie_debug import cookie_check, cookie_operation


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


VIDEO_HOSTS = {"bilibili.com", "www.bilibili.com", "m.bilibili.com"}
SHORT_HOSTS = {"b23.tv", "www.b23.tv", "bili2233.cn", "www.bili2233.cn"}
MAX_REQUEST_ATTEMPTS = 2
MAX_REDIRECTS = 10

# 视频地址及分 P 属于业务校验；CLI 和服务器调用均使用同一入口。
def parse_video_url(value: str):
    """入口与短链接跳转共用规则；返回解析结果、可选 BV 号和整数分 P。"""
    if not isinstance(value, str):
        raise InvalidVideoURL("链接错误：请输入有效的 B 站视频链接。")
    value = value.strip()
    try:
        parsed = urlparse(value)
        if (parsed.scheme not in {"http", "https"} or
                parsed.hostname not in VIDEO_HOSTS | SHORT_HOSTS or
                parsed.username is not None or parsed.password is not None or
                parsed.port not in (None, 443 if parsed.scheme == "https" else 80) or
                parsed.params or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)):
            raise ValueError()
    except ValueError:
        raise InvalidVideoURL("链接错误：请输入有效的 B 站视频链接。") from None
    match = re.fullmatch(r"/video/(BV[1-9A-HJ-NP-Za-km-z]{10})/?", parsed.path)
    if parsed.hostname in VIDEO_HOSTS and not match:
        raise InvalidVideoURL("链接错误：请使用包含 BV 号的视频链接。")
    pages = parse_qs(parsed.query, keep_blank_values=True).get("p", ["1"])
    try:
        if len(pages) != 1 or not re.fullmatch(r"[0-9]+", pages[0]):
            raise ValueError()
        page = int(pages[0])
        if page < 1:
            raise ValueError()
    except ValueError:
        raise InvalidVideoURL("链接错误：分 P 参数 p 必须为单个正整数。") from None
    return parsed, match[1] if parsed.hostname in VIDEO_HOSTS else None, page


def normalize_language(language: str) -> str:
    return language.strip().lower().replace("_", "-")


def is_chinese(language: str) -> bool:
    code = normalize_language(language).removeprefix("ai-")
    return code.partition("-")[0] in {"zh", "cmn", "yue"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class BilibiliClient:
    def __init__(self, cookies: Path | None = None, timeout: float = 120):
        self.timeout = timeout
        self._login_checked_at = None
        self.jar = http.cookiejar.MozillaCookieJar()
        if cookies and cookie_check("BilibiliClient.__init__", "file_exists", cookies.exists):
            try:
                cookie_check("BilibiliClient.__init__", "netscape_parse_expiry_filter",
                             lambda: self.jar.load(str(cookies), ignore_discard=True, ignore_expires=False))
            except (OSError, ValueError):
                raise ConfigurationError("Cookie 配置错误：需要有效的 Netscape Cookie 文件。") from None
        try:
            self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect())
            self.body_opener = urllib.request.build_opener(NoRedirect())
        except (OSError, ValueError) as exc:
            raise ConfigurationError("B 站客户端初始化失败，请检查网络配置。") from exc
        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/137.0.0.0 Safari/537.36",
            "Referer": "https://www.bilibili.com/",
        }

    def _json(self, url, label, body=False):
        return self._retry(lambda: self._json_once(url, label, body), label)

    def _json_once(self, url, label, body=False):
        if not body:
            cookie_operation("BilibiliClient._json", "http_cookie_processor_selection")
        try:
            request = urllib.request.Request(url, headers=self.headers)
            with (self.body_opener if body else self.opener).open(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            bilibili_http_error(exc, label)
        except (OSError, urllib.error.URLError, HTTPException) as exc:
            bilibili_network_error(exc, label)
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

    def _retry(self, action, label):
        for attempt in range(MAX_REQUEST_ATTEMPTS):
            try:
                return action()
            except TransientRequestFailed as exc:
                if attempt + 1 == MAX_REQUEST_ATTEMPTS:
                    raise TransientRequestFailed(f"{exc}已尝试 {MAX_REQUEST_ATTEMPTS} 次，停止请求。") from None
                print(f"{label}暂时性网络错误，重试一次……", file=sys.stderr)

    def _redirect_target(self, url):
        # 每一跳分别重试，已经成功的跳转不重新访问；不携带账号 Cookie。
        try:
            with self.body_opener.open(urllib.request.Request(url, headers=self.headers), timeout=self.timeout) as response:
                return response.url
        except urllib.error.HTTPError as exc:
            if exc.code in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location")
                exc.close()
                if not location:
                    raise RequestFailed("短链接重定向缺少目标地址。") from None
                try:
                    return urljoin(url, location)
                except ValueError:
                    raise InvalidVideoURL("短链接重定向目标地址无效。") from None
            bilibili_http_error(exc, "短链接")
        except (OSError, urllib.error.URLError, HTTPException) as exc:
            bilibili_network_error(exc, "短链接")

    def _resolve(self, url):
        parsed, bvid, page = parse_video_url(url)
        visited = set()
        while bvid is None:
            current = parsed.geturl()
            if current in visited or len(visited) >= MAX_REDIRECTS:
                raise RequestFailed("短链接重定向循环或超过次数上限。")
            visited.add(current)
            target = self._retry(lambda: self._redirect_target(current), "短链接")
            parsed, bvid, page = parse_video_url(target)
        return bvid, page

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
        if not isinstance(data.get("title"), str) or not isinstance(data.get("desc"), str):
            raise RequestFailed("视频标题或简介结构异常。")
        return VideoInfo(bvid, data["aid"], part["cid"], page, data["title"], data["desc"], duration,
                         f"https://www.bilibili.com/video/{bvid}/?p={page}")

    def _login(self):
        self._login_checked_at = None
        data = cookie_check("BilibiliClient._login", "server_auth",
                            lambda: self._api("/x/web-interface/nav"))
        if data.get("isLogin") is not True:
            raise AccessDenied("Cookie 未登录或已失效，停止 ASR 下载。")
        self._login_checked_at = time.monotonic()

    def require_download_cookie(self):
        if not cookie_check(
            "BilibiliClient.require_download_cookie", "bilibili_domain_presence",
            lambda: any(not c.is_expired() and (c.domain.lstrip(".") == "bilibili.com" or
                       c.domain.endswith(".bilibili.com")) for c in self.jar)
        ):
            raise AccessDenied("ASR 下载需要有效的 B 站 Netscape Cookie 文件。")
        # 仅复用本次提取中最近一分钟的成功检查；新提取会清空缓存。
        if self._login_checked_at is None or time.monotonic() - self._login_checked_at >= 60:
            self._login()

    @staticmethod
    def _check_identity(data, info):
        # dm/view 的 aid + oid 绑定目标分 P；响应携带标识时必须一致。
        for key, expected in (("bvid", info.bvid), ("aid", info.aid), ("cid", info.cid)):
            if key in data and (type(data[key]) is not type(expected) or data[key] != expected):
                raise RequestFailed("字幕响应的视频标识与目标视频或分 P 不匹配。")

    def _body(self, track, info):
        self._check_identity(track, info)
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
        self._check_identity(data, info)
        # CC 正文只有样式和 body，语言来自绑定目标 CID 的 dm/view 轨道。
        # AI 正文仍要求 lang；任何正文显式给出非中文/异常 lang 都拒绝。
        if ("lang" in data or track.get("type") != 0) and (
                not isinstance(data.get("lang"), str) or not is_chinese(data["lang"])):
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
            content = line["content"].strip()
            if content:
                text.append(content)
        if not text:
            raise RequestFailed("中文字幕正文没有有效文本。")
        return "\n".join(text)

    def fetch(self, url, preferred_lang="zh-CN"):
        self._login_checked_at = None
        bvid, page = self._resolve(url)
        self.headers["Referer"] = f"https://www.bilibili.com/video/{bvid}/"
        info = self._info(bvid, page)
        data = self._api("/x/v2/dm/view", {"aid": info.aid, "oid": info.cid, "type": 1})
        self._check_identity(data, info)
        if "oid" in data and (type(data["oid"]) is not int or data["oid"] != info.cid):
            raise RequestFailed("字幕列表的 oid 与目标分 P 不匹配。")
        if data.get("need_login_subtitle") is True:
            raise AccessDenied("字幕接口要求登录或权限，不能判定为无字幕。")
        subtitle = data.get("subtitle")
        if not isinstance(subtitle, dict) or "subtitles" not in subtitle:
            raise RequestFailed("字幕列表字段缺失，不能判定为无字幕。")
        self._check_identity(subtitle, info)
        tracks = subtitle["subtitles"]
        if tracks is None or tracks == []:
            # 匿名空列表不能证明没有隐藏轨道；仅空列表判定需要验证登录。
            # 保留认证失败的类型和原因，不能转换为字幕错误或启动下载。
            self._login()
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
        ready = [t for t in unlocked if type(t.get("type")) is int and
                 (t["type"] == 0 or t["type"] == 1 and t.get("ai_status") == 2)]
        if not ready:
            raise RequestFailed("中文字幕类型或状态尚未验证为可用，不能启动 ASR。")
        preferred = normalize_language(preferred_lang)
        track = next((t for t in ready if normalize_language(t["lan"]) == preferred), ready[0])
        return VideoResult(info, SubtitleState.CHINESE, self._body(track, info), languages)
