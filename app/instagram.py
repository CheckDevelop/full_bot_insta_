from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import uuid

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlparse

import instaloader
import requests


logger = logging.getLogger(__name__)


# ============================================================
# URL REGEX
# ============================================================

INSTAGRAM_URL_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/",
    re.I,
)


POST_RE = re.compile(
    r"/(?:p|reel|tv)/([^/?#]+)",
    re.I,
)


STORY_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/"
    r"stories/"
    r"([^/?]+)/"
    r"(\d+)"
    r"(?:/|\?|$)",
    re.IGNORECASE,
)


# Example:
#
# https://www.instagram.com/stories/highlights/123456789/
#
HIGHLIGHT_RE = re.compile(
    r"/stories/highlights/([0-9]+)",
    re.I,
)


# Example:
#
# https://www.instagram.com/s/aGlnaGxpZ2h0OjE3OTU2MDUyMzg0NjQ3NTY0
#
HIGHLIGHT_RE2 = re.compile(
    r"/s/([^?]+)",
    re.I,
)


# ============================================================
# Exceptions
# ============================================================


class InstagramError(RuntimeError):
    """Base exception for Instagram-related failures."""


class InstagramAuthenticationError(
    InstagramError
):
    """Instagram session/authentication error."""


class InstagramRateLimitError(
    InstagramError
):
    """Instagram rate-limit error."""

    def __init__(
        self,
        message: str,
        retry_after: int | None = None,
    ) -> None:

        super().__init__(message)

        self.retry_after = retry_after


# ============================================================
# Media
# ============================================================


@dataclass(frozen=True)
class MediaFile:

    path: Path

    # "photo" | "video"
    media_type: str


# ============================================================
# Instagram Client
# ============================================================


class InstagramClient:

    def __init__(
        self,
        username: str,
        session_b64: str,
        temp_dir: Path,
        max_media_bytes: int,
        request_timeout_seconds: int = 60,
    ) -> None:

        self.username = username.strip()

        self.session_b64 = (
            session_b64.strip()
        )

        self.temp_dir = Path(
            temp_dir
        )

        self.max_media_bytes = int(
            max_media_bytes
        )

        self.request_timeout_seconds = int(
            request_timeout_seconds
        )

        self.temp_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.session_path = (
            self.temp_dir
            / f"session-{self.username}"
        )

        self.loader = (
            self._build_loader()
        )

    # ========================================================
    # Instagram Session
    # ========================================================

    def _build_loader(
        self,
    ) -> instaloader.Instaloader:

        loader = instaloader.Instaloader(

            save_metadata=False,

            download_comments=False,

            user_agent="Mozilla/5.0",

            max_connection_attempts=1,

            request_timeout=(
                self.request_timeout_seconds
            ),
        )

        # ----------------------------------------------------
        # Decode Base64 session
        # ----------------------------------------------------

        try:

            raw = base64.b64decode(
                self.session_b64,
                validate=True,
            )

        except Exception as exc:

            raise InstagramAuthenticationError(
                "INSTAGRAM_SESSION_B64 "
                "is not valid base64."
            ) from exc

        if not raw:

            raise InstagramAuthenticationError(
                "Instagram session is empty."
            )

        self.session_path.write_bytes(
            raw
        )

        # ----------------------------------------------------
        # Load session
        # ----------------------------------------------------

        try:

            stripped = raw.lstrip()

            # ------------------------------------------------
            # JSON cookies
            # ------------------------------------------------

            if (
                stripped.startswith(b"{")
                or stripped.startswith(b"[")
            ):

                self._load_json_cookies(
                    loader,
                    raw,
                )

            # ------------------------------------------------
            # Native Instaloader session
            # ------------------------------------------------

            else:

                loader.load_session_from_file(
                    self.username,
                    str(
                        self.session_path
                    ),
                )

        except InstagramAuthenticationError:

            raise

        except Exception as exc:

            raise InstagramAuthenticationError(
                "Instagram session could not "
                f"be loaded: {exc}"
            ) from exc

        # ----------------------------------------------------
        # Headers
        # ----------------------------------------------------

        loader.context._session.headers.update(
            {

                "User-Agent":
                    "Mozilla/5.0",

                "X-IG-App-ID":
                    "936619743392459",

                "Referer":
                    "https://www.instagram.com/",

                "Accept":
                    "*/*",
            }
        )

        logger.info(
            "Instagram session loaded for @%s; "
            "startup login validation skipped",
            self.username,
        )

        return loader

    # ========================================================
    # JSON Cookies
    # ========================================================

    def _load_json_cookies(
        self,
        loader: instaloader.Instaloader,
        raw: bytes,
    ) -> None:

        try:

            data = json.loads(
                raw.decode("utf-8")
            )

        except Exception as exc:

            raise InstagramAuthenticationError(
                "Instagram JSON session is invalid."
            ) from exc

        cookies = (
            data.get("cookies")
            if isinstance(
                data,
                dict,
            )
            else data
        )

        if not isinstance(
            cookies,
            list,
        ):

            raise InstagramAuthenticationError(
                "Cookies list not found."
            )

        loaded = 0

        for cookie in cookies:

            if not isinstance(
                cookie,
                dict,
            ):

                continue

            name = cookie.get(
                "name"
            )

            value = cookie.get(
                "value"
            )

            if not name or value is None:

                continue

            domain = cookie.get(
                "domain",
                ".instagram.com",
            )

            path = cookie.get(
                "path",
                "/",
            )

            if "instagram.com" not in domain:

                domain = ".instagram.com"

            loader.context._session.cookies.set(

                name,

                value,

                domain=domain,

                path=path,
            )

            loaded += 1

        if loaded == 0:

            raise InstagramAuthenticationError(
                "No Instagram cookies loaded."
            )

        logger.info(
            "Loaded %s Instagram cookies",
            loaded,
        )

    # ========================================================
    # Cookie helper
    # ========================================================

    def _get_cookie(
        self,
        name: str,
    ) -> str:

        candidates = []

        for cookie in (
            self.loader.context
            ._session
            .cookies
        ):

            if cookie.name == name:

                candidates.append(
                    cookie
                )

        if not candidates:

            return ""

        preferred_domains = {
            ".instagram.com",
            "instagram.com",
        }

        for cookie in candidates:

            if (
                str(
                    cookie.domain
                ).lower()
                in preferred_domains
            ):

                return str(
                    cookie.value
                )

        return str(
            candidates[0].value
        )

    # ========================================================
    # URL validation
    # ========================================================

    @staticmethod
    def validate_url(
        url: str,
    ) -> str:

        normalized = url.strip()

        parsed = urlparse(
            normalized
        )

        if parsed.scheme not in {
            "http",
            "https",
        }:

            raise InstagramError(
                "لینک اینستاگرام معتبر نیست."
            )

        if parsed.netloc.lower() not in {
            "instagram.com",
            "www.instagram.com",
        }:

            raise InstagramError(
                "لینک اینستاگرام معتبر نیست."
            )

        return normalized

    # ========================================================
    # Dispatcher
    # ========================================================

    def download(
        self,
        url: str,
    ) -> list[MediaFile]:

        url = self.validate_url(
            url
        )

        # ----------------------------------------------------
        # IMPORTANT
        #
        # /s/... is Highlight
        # and must be checked before Story.
        # ----------------------------------------------------

        if (
            HIGHLIGHT_RE.search(url)
            or HIGHLIGHT_RE2.search(url)
        ):

            return self._download_highlight(
                url
            )

        # ----------------------------------------------------
        # Normal Story
        # ----------------------------------------------------

        if STORY_RE.search(url):

            return self._download_story(
                url
            )

        # ----------------------------------------------------
        # Post / Reel / TV
        # ----------------------------------------------------

        if POST_RE.search(url):

            return self._download_post(
                url
            )

        raise InstagramError(
            "این نوع لینک اینستاگرام "
            "در نسخه فعلی پشتیبانی نمی‌شود."
        )

    # ========================================================
    # Temporary Job Directory
    # ========================================================

    def _new_job_dir(
        self,
    ) -> Path:

        job_dir = (
            self.temp_dir
            / uuid.uuid4().hex
        )

        job_dir.mkdir(
            parents=True,
            exist_ok=False,
        )

        return job_dir

    # ========================================================
    # Instagram HTTP Errors
    # ========================================================

    def _raise_for_instagram_response(
        self,
        response: requests.Response,
        context: str,
    ) -> None:

        status = response.status_code

        if status in {
            401,
            403,
        }:

            raise InstagramAuthenticationError(
                f"Instagram returned HTTP "
                f"{status} while {context}."
            )

        if status == 429:

            retry_after_raw = (
                response.headers.get(
                    "Retry-After"
                )
            )

            retry_after = None

            if retry_after_raw:

                try:

                    retry_after = int(
                        retry_after_raw
                    )

                except ValueError:

                    retry_after = None

            raise InstagramRateLimitError(
                "Instagram rate-limited "
                f"the request while {context}.",
                retry_after=retry_after,
            )

        response.raise_for_status()

    # ========================================================
    # Direct Media Download
    # ========================================================

    def _download_url(
        self,
        url: str,
        path: Path,
    ) -> None:

        headers = {
        
            "User-Agent":
                self.loader.context
                ._session
                .headers.get(
                    "User-Agent",
                    "Mozilla/5.0",
                ),
        
            "Referer":
                "https://www.instagram.com/",
        
            "Accept":
                "*/*",
        
            "Accept-Language":
                "en-US,en;q=0.9",
        
            "Origin":
                "https://www.instagram.com",
        
        }

        try:

            with (
                self.loader.context
                ._session
                .get(
                    url,

                    headers=headers,

                    stream=True,

                    timeout=(
                        20,
                        self.request_timeout_seconds,
                    ),

                    allow_redirects=True,
                )
            ) as response:

                self._raise_for_instagram_response(
                    response,
                    "downloading media",
                )

                content_length = (
                    response.headers.get(
                        "Content-Length"
                    )
                )

                if content_length:

                    try:

                        if (
                            int(content_length)
                            > self.max_media_bytes
                        ):

                            raise InstagramError(
                                "فایل بیش از حد "
                                "مجاز بزرگ است."
                            )

                    except ValueError:

                        pass

                total = 0

                with path.open(
                    "wb"
                ) as file_handle:

                    for chunk in response.iter_content(
                        chunk_size=1024 * 1024
                    ):

                        if not chunk:

                            continue

                        total += len(
                            chunk
                        )

                        if (
                            total
                            > self.max_media_bytes
                        ):

                            raise InstagramError(
                                "فایل بیش از حد "
                                "مجاز بزرگ است."
                            )

                        file_handle.write(
                            chunk
                        )

        except InstagramError:

            raise

        except requests.exceptions.Timeout as exc:

            raise InstagramError(
                "دانلود از Instagram "
                "به دلیل timeout ناموفق بود."
            ) from exc

        except requests.exceptions.RequestException as exc:

            raise InstagramError(
                f"خطا در دانلود فایل "
                f"از Instagram: {exc}"
            ) from exc

    # ========================================================
    # POST / REEL / TV
    # ========================================================

    def _download_post(
        self,
        url: str,
    ) -> list[MediaFile]:

        match = POST_RE.search(
            url
        )

        if not match:

            raise InstagramError(
                "URL پست معتبر نیست."
            )

        shortcode = match.group(
            1
        )

        job_dir = self._new_job_dir()

        result: list[MediaFile] = []

        try:

            try:

                post = (
                    instaloader
                    .Post
                    .from_shortcode(
                        self.loader.context,
                        shortcode,
                    )
                )

            except (
                instaloader
                .exceptions
                .LoginRequiredException
            ) as exc:

                raise InstagramAuthenticationError(
                    "Session اینستاگرام "
                    "برای این پست معتبر نیست."
                ) from exc

            except (
                instaloader
                .exceptions
                .QueryReturnedNotFoundException
            ) as exc:

                raise InstagramError(
                    "پست پیدا نشد یا "
                    "در دسترس نیست."
                ) from exc

            except Exception as exc:

                message = str(
                    exc
                ).lower()

                if (
                    "429" in message
                    or
                    "too many requests"
                    in message
                ):

                    raise InstagramRateLimitError(
                        "Instagram هنگام دریافت "
                        "اطلاعات پست rate limit "
                        "اعمال کرده."
                    ) from exc

                if (
                    "401" in message
                    or
                    "unauthorized"
                    in message
                ):

                    raise InstagramAuthenticationError(
                        "Instagram درخواست پست "
                        "را unauthorized رد کرد."
                    ) from exc

                raise InstagramError(
                    "خطا در دریافت اطلاعات "
                    f"پست: {exc}"
                ) from exc

            username = (
                post.owner_username
                or "instagram"
            )

            # ------------------------------------------------
            # Photo
            # ------------------------------------------------

            if post.typename == "GraphImage":

                path = (
                    job_dir
                    /
                    f"{username}_{shortcode}.jpg"
                )

                self._download_url(
                    post.url,
                    path,
                )

                result.append(
                    MediaFile(
                        path,
                        "photo",
                    )
                )

            # ------------------------------------------------
            # Video
            # ------------------------------------------------

            elif post.typename == "GraphVideo":

                if not post.video_url:

                    raise InstagramError(
                        "URL ویدیو دریافت نشد."
                    )

                path = (
                    job_dir
                    /
                    f"{username}_{shortcode}.mp4"
                )

                self._download_url(
                    post.video_url,
                    path,
                )

                result.append(
                    MediaFile(
                        path,
                        "video",
                    )
                )

            # ------------------------------------------------
            # Carousel
            # ------------------------------------------------

            elif post.typename == "GraphSidecar":

                for index, node in enumerate(
                    post.get_sidecar_nodes(),
                    start=1,
                ):

                    if node.is_video:

                        if not node.video_url:

                            raise InstagramError(
                                "URL یکی از ویدیوهای "
                                "carousel دریافت نشد."
                            )

                        path = (
                            job_dir
                            /
                            (
                                f"{username}_"
                                f"{shortcode}_"
                                f"{index}.mp4"
                            )
                        )

                        self._download_url(
                            node.video_url,
                            path,
                        )

                        result.append(
                            MediaFile(
                                path,
                                "video",
                            )
                        )

                    else:

                        if not node.display_url:

                            raise InstagramError(
                                "URL یکی از تصاویر "
                                "carousel دریافت نشد."
                            )

                        path = (
                            job_dir
                            /
                            (
                                f"{username}_"
                                f"{shortcode}_"
                                f"{index}.jpg"
                            )
                        )

                        self._download_url(
                            node.display_url,
                            path,
                        )

                        result.append(
                            MediaFile(
                                path,
                                "photo",
                            )
                        )

            else:

                raise InstagramError(
                    "نوع پست پشتیبانی نمی‌شود: "
                    f"{post.typename}"
                )

            return result

        except Exception:

            shutil.rmtree(
                job_dir,
                ignore_errors=True,
            )

            raise

    # ============================================================
    # Find User Id
    # ============================================================
    def _get_user_id_from_html(
        self,
        username: str,
    ) -> int | None:
    
        url = (
            f"https://www.instagram.com/{username}/"
        )
    
        session = (
            self.loader.context._session
        )
    
        headers = {
    
            "User-Agent":
                session.headers.get(
                    "User-Agent",
                    "Mozilla/5.0",
                ),
    
            "Referer":
                "https://www.instagram.com/",
    
            "Accept":
                "text/html,*/*",
    
        }
    
    
        try:
    
            response = session.get(
                url,
                headers=headers,
                timeout=30,
            )
    
        except requests.RequestException as exc:
    
            raise InstagramError(
                f"خطا در دریافت HTML پروفایل: {exc}"
            )
    
    
        self._raise_for_instagram_response(
            response,
            "getting profile HTML"
        )
    
    
        html = response.text
    
    
        patterns = [
    
            # جدید
            r'"profile_id":"(\d+)"',
    
            # user object
            r'"user_id":"(\d+)"',
    
            # graphql data
            r'"id":"(\d+)","username":"'
            + re.escape(username),
    
            # legacy
            r'"owner_id":"(\d+)"',
    
        ]
    
    
        for pattern in patterns:
    
            match = re.search(
                pattern,
                html,
                re.IGNORECASE,
            )
    
            if match:
    
                user_id = int(
                    match.group(1)
                )
    
                logger.info(
                    "User ID found from HTML: %s",
                    user_id,
                )
    
                return user_id
    
    
        return None
    # ============================================================
    # Find Story Item
    # ============================================================

    def _find_story_item(
        self,
        username: str,
        media_id: int,
        owner_id: int,
    ) -> dict | None:

        api_url = (
            "https://www.instagram.com/"
            "api/v1/feed/reels_media/"
            f"?reel_ids={owner_id}"
        )

        session = (
            self.loader.context._session
        )

        csrf_token = self._get_cookie(
            "csrftoken"
        )

        headers = {

            "User-Agent":
                session.headers.get(
                    "User-Agent",
                    "Mozilla/5.0"
                ),

            "X-IG-App-ID":
                "936619743392459",

            "Referer":
                "https://www.instagram.com/",

            "Accept":
                "*/*",
        }

        if csrf_token:

            headers["X-CSRFToken"] = (
                csrf_token
            )

        try:

            response = requests.get(

                api_url,

                headers=headers,

                cookies=session.cookies,

                timeout=15,
            )

        except requests.RequestException as exc:

            raise InstagramError(
                f"خطا در دریافت اطلاعات "
                f"استوری: {exc}"
            ) from exc

        print(
            "Story API status:",
            response.status_code
        )

        self._raise_for_instagram_response(
            response,
            "getting story metadata"
        )

        try:

            data = response.json()

        except ValueError as exc:

            raise InstagramError(
                "پاسخ Instagram برای "
                "Story معتبر نیست."
            ) from exc

        reels = data.get(
            "reels",
            {}
        )

        if not isinstance(
            reels,
            dict
        ):

            return None

        for reel in reels.values():

            if not isinstance(
                reel,
                dict
            ):

                continue

            for item in reel.get(
                "items",
                []
            ):

                if not isinstance(
                    item,
                    dict
                ):

                    continue

                item_id = str(
                    item.get(
                        "pk",
                        ""
                    )
                )

                if item_id == str(
                    media_id
                ):

                    print(
                        "Story item found:",
                        item_id
                    )

                    return item

        return None

    # ============================================================
    # Download Normal Story
    # ============================================================

    def _download_story(
        self,
        url: str,
    ) -> list[MediaFile]:

        match = STORY_RE.search(
            url
        )

        if not match:
            raise InstagramError(
                "URL استوری معتبر نیست."
            )
            
        username = match.group(
            1
        )

        media_id = int(
            match.group(
                2
            )
        )

        print(
            "Story username:",
            username
        )

        print(
            "Story media ID:",
            media_id
        )

        parsed = urlparse(
            url
        )

        query_params = dict(
            parse_qsl(
                parsed.query
            )
        )

        owner_id = None

        reel_owner_id = (
            query_params.get(
                "reel_owner_id"
            )
        )

        if (
            reel_owner_id
            and
            reel_owner_id.isdigit()
        ):

            owner_id = int(
                reel_owner_id
            )

            print(
                "Owner ID from URL:",
                owner_id
            )

        if owner_id is None:

            print(
                "Owner ID not found in URL."
            )
        
            print(
                "Trying HTML profile..."
            )
        
        
            owner_id = (
                self
                ._get_user_id_from_html(
                    username
                )
            )
        
        
            if owner_id:
        
                print(
                    "Owner ID from HTML:",
                    owner_id
                )
        
            else:
        
                raise InstagramError(
                    "User ID از HTML پیدا نشد."
                )

        print(
            "Owner ID:",
            owner_id
        )
 
        item = self._find_story_item(
            username,
            media_id,
            owner_id,
        )

        if item is None:

            raise InstagramError(
                "استوری پیدا نشد، "
                "منقضی شده یا Media ID "
                "مربوط به این User نیست."
            )

        job_dir = self._new_job_dir()

        try:

            media_type = item.get(
                "media_type"
            )

            # ------------------------------------------------
            # Video
            # ------------------------------------------------

            if media_type == 2:

                versions = (
                    item.get(
                        "video_versions"
                    )
                    or []
                )

                if not versions:

                    raise InstagramError(
                        "CDN URL ویدئو "
                        "پیدا نشد."
                    )

                versions = sorted(
                    versions,
                    key=lambda x:
                        (
                            x.get(
                                "width",
                                0
                            )
                            *
                            x.get(
                                "height",
                                0
                            )
                        ),
                    reverse=True,
                )

                media_url = (
                    versions[0].get(
                        "url"
                    )
                )

                path = (
                    job_dir
                    /
                    f"{username}_"
                    f"story_{media_id}.mp4"
                )

                media_type_name = (
                    "video"
                )

            # ------------------------------------------------
            # Photo
            # ------------------------------------------------

            elif media_type == 1:

                candidates = (
                    item.get(
                        "image_versions2",
                        {},
                    ).get(
                        "candidates"
                    )
                    or []
                )

                if not candidates:

                    raise InstagramError(
                        "CDN URL تصویر "
                        "پیدا نشد."
                    )

                candidates = sorted(
                    candidates,
                    key=lambda x:
                        (
                            x.get(
                                "width",
                                0
                            )
                            *
                            x.get(
                                "height",
                                0
                            )
                        ),
                    reverse=True,
                )

                media_url = (
                    candidates[0].get(
                        "url"
                    )
                )

                path = (
                    job_dir
                    /
                    f"{username}_"
                    f"story_{media_id}.jpg"
                )

                media_type_name = (
                    "photo"
                )

            else:

                raise InstagramError(
                    f"نوع استوری "
                    f"پشتیبانی نمی‌شود: "
                    f"{media_type}"
                )

            if not media_url:

                raise InstagramError(
                    "CDN URL پیدا نشد."
                )

            print()
            print(
                "Story CDN URL:"
            )
            print(
                media_url
            )
            print()

            self._download_url(
                media_url,
                path,
            )

            return [
                MediaFile(
                    path,
                    media_type_name,
                )
            ]

        except Exception:

            shutil.rmtree(
                job_dir,
                ignore_errors=True,
            )

            raise

    # ============================================================
    # Highlight ID
    # ============================================================

    @staticmethod
    def get_highlight_id_from_url(
        url: str,
    ) -> str | None:

        match = HIGHLIGHT_RE2.search(
            url
        )

        if not match:

            return None

        encoded = match.group(
            1
        )

        try:

            encoded += "=" * (
                -len(encoded) % 4
            )

            decoded = (
                base64
                .b64decode(
                    encoded
                )
                .decode(
                    "utf-8"
                )
            )

            print(
                "Decoded Highlight:",
                decoded
            )

            if decoded.startswith(
                "highlight:"
            ):

                return decoded.split(
                    ":",
                    1
                )[1]

        except Exception as exc:

            print(
                "Highlight decode error:",
                repr(exc)
            )

        return None

    # ============================================================
    # Story Media ID from Highlight URL
    # ============================================================

    @staticmethod
    def get_story_media_id_from_url(
        url: str,
    ) -> str | None:

        parsed = urlparse(
            url
        )

        params = dict(
            parse_qsl(
                parsed.query
            )
        )

        story_media_id = (
            params.get(
                "story_media_id"
            )
        )

        if not story_media_id:

            return None

        # Example:
        #
        # 3845518169410282443_14886042089
        #
        # We only need:
        #
        # 3845518169410282443

        match = re.match(
            r"^(\d+)",
            story_media_id
        )

        if not match:

            return None

        return match.group(
            1
        )

    # ============================================================
    # Highlight
    # ============================================================

    def _download_highlight(
        self,
        url: str,
    ) -> list[MediaFile]:

        # ========================================================
        # Highlight ID
        # ========================================================

        match1 = HIGHLIGHT_RE.search(
            url
        )

        if match1:

            highlight_id = (
                match1.group(1)
            )

        else:

            highlight_id = (
                self
                .get_highlight_id_from_url(
                    url
                )
            )

        if not highlight_id:

            raise InstagramError(
                "URL هایلایت معتبر نیست."
            )

        print()
        print(
            "Highlight ID:",
            highlight_id
        )

        # ========================================================
        # Story Media ID
        # ========================================================

        story_media_id = (
            self
            .get_story_media_id_from_url(
                url
            )
        )

        if story_media_id:

            print(
                "Requested Story Media ID:",
                story_media_id
            )

        # ========================================================
        # Session
        # ========================================================

        session = (
            self.loader.context._session
        )

        csrf_token = self._get_cookie(
            "csrftoken"
        )

        headers = {

            "User-Agent":
                session.headers.get(
                    "User-Agent",
                    "Mozilla/5.0",
                ),

            "X-IG-App-ID":
                "936619743392459",

            "Referer":
                "https://www.instagram.com/",

            "Accept":
                "*/*",
        }

        if csrf_token:

            headers["X-CSRFToken"] = (
                csrf_token
            )

        # ========================================================
        # API
        # ========================================================

        api_url = (
            "https://www.instagram.com/"
            "api/v1/feed/reels_media/"
            f"?reel_ids=highlight:{highlight_id}"
        )

        print()
        print(
            "Highlight API:"
        )
        print(
            api_url
        )

        try:

            response = session.get(

                api_url,

                headers=headers,

                timeout=(
                    20,
                    self.request_timeout_seconds,
                ),
            )

        except requests.exceptions.Timeout as exc:

            raise InstagramError(
                "دریافت اطلاعات هایلایت "
                "timeout شد."
            ) from exc

        except requests.exceptions.RequestException as exc:

            raise InstagramError(
                f"خطا در ارتباط با Instagram: "
                f"{exc}"
            ) from exc

        print(
            "Highlight API status:",
            response.status_code
        )

        self._raise_for_instagram_response(
            response,
            "getting highlight metadata",
        )

        try:

            data = response.json()

        except ValueError as exc:

            print(
                response.text[:1000]
            )

            raise InstagramError(
                "پاسخ Instagram برای "
                "هایلایت JSON معتبر نبود."
            ) from exc

        # ========================================================
        # Find Highlight
        # ========================================================

        reels = data.get(
            "reels",
            {}
        )

        if not isinstance(
            reels,
            dict
        ):

            raise InstagramError(
                "ساختار پاسخ Highlight "
                "معتبر نیست."
            )

        highlight = reels.get(
            f"highlight:{highlight_id}"
        )

        # --------------------------------------------------------
        # Fallback
        # --------------------------------------------------------

        if not highlight:

            for key, value in reels.items():

                if str(key).endswith(
                    str(highlight_id)
                ):

                    highlight = value

                    break

        if not highlight:

            print(
                "Available reel keys:",
                list(reels.keys())
            )

            raise InstagramError(
                "اطلاعات Highlight "
                "پیدا نشد."
            )

        # ========================================================
        # Items
        # ========================================================

        items = (
            highlight.get(
                "items"
            )
            or []
        )

        print(
            "Highlight items:",
            len(items)
        )

        if not items:

            raise InstagramError(
                "هایلایت هیچ Story "
                "قابل دسترسی ندارد."
            )

        # ========================================================
        # Find requested Story
        # ========================================================

        selected_item = None

        if story_media_id:

            print()
            print(
                "Searching requested story..."
            )

            for item in items:

                if not isinstance(
                    item,
                    dict
                ):

                    continue

                pk = str(
                    item.get(
                        "pk",
                        ""
                    )
                )

                item_id = str(
                    item.get(
                        "id",
                        ""
                    )
                )

                print(
                    "Checking:",
                    pk,
                    "|",
                    item_id
                )

                if pk == story_media_id:

                    selected_item = item

                    break

                if item_id == story_media_id:

                    selected_item = item

                    break

                if item_id.startswith(
                    story_media_id + "_"
                ):

                    selected_item = item

                    break

        # ========================================================
        # Result selection
        # ========================================================

        if story_media_id:

            if not selected_item:

                print()
                print(
                    "❌ Requested Story "
                    "was not found."
                )

                print(
                    "Requested:",
                    story_media_id
                )

                print()
                print(
                    "Available stories:"
                )

                for item in items:

                    print(
                        "PK:",
                        item.get("pk"),
                        "| ID:",
                        item.get("id")
                    )

                raise InstagramError(
                    "Story موردنظر داخل "
                    "Highlight پیدا نشد."
                )

            items_to_download = [
                selected_item
            ]

        else:

            items_to_download = items

        # ========================================================
        # Download
        # ========================================================

        job_dir = self._new_job_dir()

        result: list[MediaFile] = []

        username = str(
            (
                highlight.get(
                    "user"
                )
                or {}
            ).get(
                "username"
            )
            or "highlight"
        )

        try:

            for index, item in enumerate(
                items_to_download,
                start=1,
            ):

                media_type = item.get(
                    "media_type"
                )

                print()
                print(
                    "Media type:",
                    media_type
                )

                # =================================================
                # VIDEO
                # =================================================

                if media_type == 2:

                    versions = (
                        item.get(
                            "video_versions"
                        )
                        or []
                    )

                    if not versions:

                        print(
                            "No video_versions."
                        )

                        continue

                    versions = sorted(
                        versions,
                        key=lambda x:
                            (
                                x.get(
                                    "width",
                                    0
                                )
                                *
                                x.get(
                                    "height",
                                    0
                                )
                            ),
                        reverse=True,
                    )

                    media_url = (
                        versions[0].get(
                            "url"
                        )
                    )

                    if not media_url:

                        continue

                    print()
                    print(
                        "================================"
                    )
                    print(
                        "VIDEO CDN URL:"
                    )
                    print(
                        media_url
                    )
                    print(
                        "================================"
                    )

                    media_id = str(
                        item.get(
                            "pk"
                        )
                        or story_media_id
                        or index
                    )

                    path = (
                        job_dir
                        /
                        f"{username}_"
                        f"story_"
                        f"{media_id}.mp4"
                    )

                    self._download_url(
                        media_url,
                        path,
                    )

                    result.append(
                        MediaFile(
                            path,
                            "video",
                        )
                    )

                # =================================================
                # PHOTO
                # =================================================

                elif media_type == 1:

                    candidates = (
                        item.get(
                            "image_versions2",
                            {},
                        ).get(
                            "candidates"
                        )
                        or []
                    )

                    if not candidates:

                        print(
                            "No image candidates."
                        )

                        continue

                    candidates = sorted(
                        candidates,
                        key=lambda x:
                            (
                                x.get(
                                    "width",
                                    0
                                )
                                *
                                x.get(
                                    "height",
                                    0
                                )
                            ),
                        reverse=True,
                    )

                    media_url = (
                        candidates[0].get(
                            "url"
                        )
                    )

                    if not media_url:

                        continue

                    print()
                    print(
                        "================================"
                    )
                    print(
                        "IMAGE CDN URL:"
                    )
                    print(
                        media_url
                    )
                    print(
                        "================================"
                    )

                    media_id = str(
                        item.get(
                            "pk"
                        )
                        or story_media_id
                        or index
                    )

                    path = (
                        job_dir
                        /
                        f"{username}_"
                        f"story_"
                        f"{media_id}.jpg"
                    )

                    self._download_url(
                        media_url,
                        path,
                    )

                    result.append(
                        MediaFile(
                            path,
                            "photo",
                        )
                    )

                else:

                    print(
                        "Unsupported media type:",
                        media_type
                    )

            if not result:

                raise InstagramError(
                    "فایل Media قابل دانلود "
                    "پیدا نشد."
                )

            return result

        except Exception:

            shutil.rmtree(
                job_dir,
                ignore_errors=True,
            )

            raise

    # ============================================================
    # Cleanup
    # ============================================================

    @staticmethod
    def cleanup_files(
        files: list[MediaFile],
    ) -> None:

        parents: set[Path] = set()

        for media in files:

            try:

                parents.add(
                    media.path.parent
                )

                media.path.unlink(
                    missing_ok=True
                )

            except OSError:

                logger.exception(
                    "Failed to remove file %s",
                    media.path,
                )

        for parent in parents:

            shutil.rmtree(
                parent,
                ignore_errors=True,
            )
