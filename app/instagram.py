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


INSTAGRAM_URL_RE = re.compile(
    r"^https?://(?:www\.)?instagram\.com/",
    re.I,
)

POST_RE = re.compile(
    r"/(?:p|reel|tv)/([^/?#]+)",
    re.I,
)

STORY_RE = re.compile(
    r"/stories/([^/]+)/([0-9]+)",
    re.I,
)

HIGHLIGHT_RE = re.compile(
    r"/stories/highlights/([0-9]+)",
    re.I,
)


class InstagramError(RuntimeError):
    """Base exception for Instagram-related failures."""


class InstagramAuthenticationError(InstagramError):
    """Instagram session/authentication error."""


class InstagramRateLimitError(InstagramError):
    """Instagram rate-limit error."""

    def __init__(
        self,
        message: str,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class MediaFile:
    path: Path
    media_type: str  # "photo" | "video"


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
        self.session_b64 = session_b64.strip()
        self.temp_dir = Path(temp_dir)
        self.max_media_bytes = int(max_media_bytes)
        self.request_timeout_seconds = int(request_timeout_seconds)

        self.temp_dir.mkdir(
            parents=True,
            exist_ok=True,
        )

        self.session_path = (
            self.temp_dir / f"session-{self.username}"
        )

        self.loader = self._build_loader()

    # ============================================================
    # Instagram Session
    # ============================================================

    def _build_loader(self) -> instaloader.Instaloader:
        loader = instaloader.Instaloader(
            save_metadata=False,
            download_comments=False,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "Chrome/131 Safari/537.36"
            ),
            max_connection_attempts=1,
            request_timeout=self.request_timeout_seconds,
        )

        try:
            raw = base64.b64decode(
                self.session_b64,
                validate=True,
            )
        except Exception as exc:
            raise InstagramAuthenticationError(
                "INSTAGRAM_SESSION_B64 is not valid base64."
            ) from exc

        if not raw:
            raise InstagramAuthenticationError(
                "Instagram session is empty."
            )

        self.session_path.write_bytes(raw)

        try:
            stripped = raw.lstrip()

            # JSON cookies from the user's existing instagram.json
            if stripped.startswith(b"{") or stripped.startswith(b"["):
                self._load_json_cookies(
                    loader,
                    raw,
                )

            # Native Instaloader session file
            else:
                loader.load_session_from_file(
                    self.username,
                    str(self.session_path),
                )

        except InstagramAuthenticationError:
            raise

        except Exception as exc:
            raise InstagramAuthenticationError(
                f"Instagram session could not be loaded: {exc}"
            ) from exc

        # IMPORTANT:
        # Do not call loader.test_login() here.
        #
        # Instagram may temporarily return 401/429 during startup.
        # That must not crash the whole Telegram bot.
        logger.info(
            "Instagram session loaded for @%s; "
            "startup login validation skipped",
            self.username,
        )

        return loader

    def _load_json_cookies(
        self,
        loader: instaloader.Instaloader,
        raw: bytes,
    ) -> None:

        try:
            data = json.loads(
                raw.decode("utf-8")
            )

        except (
            UnicodeDecodeError,
            json.JSONDecodeError,
        ) as exc:
            raise InstagramAuthenticationError(
                "INSTAGRAM_SESSION_B64 contains JSON, "
                "but the JSON is invalid."
            ) from exc

        cookies = (
            data.get("cookies")
            if isinstance(data, dict)
            else data
        )

        if isinstance(cookies, dict):

            cookie_items = [
                {
                    "name": name,
                    "value": value,
                }
                for name, value in cookies.items()
            ]

        elif isinstance(cookies, list):

            cookie_items = cookies

        else:

            raise InstagramAuthenticationError(
                "Instagram JSON session must contain "
                "a 'cookies' list or cookie mapping."
            )

        loaded = 0

        for cookie in cookie_items:

            if not isinstance(cookie, dict):
                continue

            name = cookie.get("name")
            value = cookie.get("value")

            if not name or value is None:
                continue

            # Keep cookie loading identical to the working standalone
            # test: normalize every Instagram cookie to the same domain
            # and path. This avoids duplicate-domain/path CookieJar
            # conflicts (especially csrftoken/sessionid).
            loader.context._session.cookies.set(
                str(name),
                str(value),
                domain=".instagram.com",
                path="/",
            )

            loaded += 1

        if loaded == 0:

            raise InstagramAuthenticationError(
                "No Instagram cookies were found "
                "in INSTAGRAM_SESSION_B64."
            )

        loader.context._session.headers.update(
            {
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 "
                    "Chrome/131 Safari/537.36"
                ),
                "X-IG-App-ID": "936619743392459",
                "Referer": "https://www.instagram.com/",
                "Accept": "*/*",
            }
        )

        logger.info(
            "Loaded %d Instagram cookies from JSON session",
            loaded,
        )

    # ============================================================
    # Resolve username -> user ID from profile HTML
    # ============================================================

    def get_user_id_from_html(session, username):

        url = f"https://www.instagram.com/{username}/"
    
        headers = {
    
            "User-Agent":
            "Mozilla/5.0",
    
            "X-IG-App-ID":
            "936619743392459",
    
            "X-CSRFToken":
            get_cookie("csrftoken"),
    
            "Referer":
            "https://www.instagram.com/",
    
            "Accept":
            "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    
        }
    
        response = session.get(
            url,
            headers=headers,
            timeout=15
        )
    
        print("Profile status:", response.status_code)
    
        html = response.text
    
        patterns = [
            r'"profile_id":"(\d+)"',
            r'"user_id":"(\d+)"',
            r'"owner":\{"id":"(\d+)"',
            r'"id":"(\d+)","username":"' + re.escape(username)
        ]
    
        for pattern in patterns:
    
            match = re.search(pattern, html)
    
            if match:
                return match.group(1)
    
        return None

    @staticmethod
    def _parse_retry_after(
        response: requests.Response,
    ) -> int | None:

        raw = response.headers.get("Retry-After")

        if not raw:
            return None

        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    # ============================================================
    # Cookie helper
    # ============================================================

    def _get_cookie(
        self,
        name: str,
    ) -> str:
        """
        Safely read a cookie.

        requests.cookies.get() raises CookieConflictError when
        multiple cookies have the same name but different
        domains/paths. Instagram cookie exports can contain exactly
        that situation.

        We prefer an Instagram-wide cookie and otherwise use the
        first matching cookie.
        """

        candidates = []

        for cookie in self.loader.context._session.cookies:

            if cookie.name == name:
                candidates.append(cookie)

        if not candidates:
            return ""

        preferred_domains = {
            ".instagram.com",
            "instagram.com",
        }

        for cookie in candidates:

            if (
                str(cookie.domain).lower()
                in preferred_domains
            ):
                return str(cookie.value)

        return str(
            candidates[0].value
        )

    # ============================================================
    # URL validation
    # ============================================================

    @staticmethod
    def validate_url(
        url: str,
    ) -> str:

        normalized = url.strip()

        parsed = urlparse(normalized)

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

    # ============================================================
    # Dispatcher
    # ============================================================

    def download(
        self,
        url: str,
    ) -> list[MediaFile]:

        url = self.validate_url(url)

        # IMPORTANT:
        # Highlight URLs also match STORY_RE.
        # Therefore Highlight MUST come first.

        if HIGHLIGHT_RE.search(url):

            return self._download_highlight(
                url
            )

        if STORY_RE.search(url):

            return self._download_story(
                url
            )

        if POST_RE.search(url):

            return self._download_post(
                url
            )

        raise InstagramError(
            "این نوع لینک اینستاگرام "
            "در نسخه فعلی پشتیبانی نمی‌شود."
        )

    # ============================================================
    # Temporary job directory
    # ============================================================

    def _new_job_dir(self) -> Path:

        job_dir = (
            self.temp_dir
            / uuid.uuid4().hex
        )

        job_dir.mkdir(
            parents=True,
            exist_ok=False,
        )

        return job_dir

    # ============================================================
    # Instagram HTTP error handling
    # ============================================================

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
                f"Instagram returned HTTP {status} "
                f"while {context}."
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
                f"Instagram rate-limited the request "
                f"while {context}.",
                retry_after=retry_after,
            )

        response.raise_for_status()

    # ============================================================
    # Direct media download
    # ============================================================

    def _download_url(
        self,
        url: str,
        path: Path,
    ) -> None:

        headers = {
            "User-Agent": self.loader.context._session.headers.get(
                "User-Agent",
                "Mozilla/5.0",
            ),
            "Referer": "https://www.instagram.com/",
        }

        try:

            with self.loader.context._session.get(
                url,
                headers=headers,
                stream=True,
                timeout=(
                    20,
                    self.request_timeout_seconds,
                ),
                allow_redirects=True,
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
                                "فایل بیش از حد مجاز بزرگ است."
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

                        total += len(chunk)

                        if (
                            total
                            > self.max_media_bytes
                        ):
                            raise InstagramError(
                                "فایل بیش از حد مجاز بزرگ است."
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
                f"خطا در دانلود فایل از Instagram: {exc}"
            ) from exc

    # ============================================================
    # Post / Reel / TV / Carousel
    # ============================================================

    def _download_post(
        self,
        url: str,
    ) -> list[MediaFile]:

        match = POST_RE.search(url)

        if not match:

            raise InstagramError(
                "URL پست معتبر نیست."
            )

        shortcode = match.group(1)

        job_dir = self._new_job_dir()

        result: list[MediaFile] = []

        try:

            try:

                post = (
                    instaloader.Post.from_shortcode(
                        self.loader.context,
                        shortcode,
                    )
                )

            except instaloader.exceptions.LoginRequiredException as exc:

                raise InstagramAuthenticationError(
                    "Session اینستاگرام "
                    "برای این پست معتبر نیست."
                ) from exc

            except instaloader.exceptions.QueryReturnedNotFoundException as exc:

                raise InstagramError(
                    "پست پیدا نشد یا در دسترس نیست."
                ) from exc

            except Exception as exc:

                message = str(exc).lower()

                if (
                    "429" in message
                    or "too many requests"
                    in message
                ):

                    raise InstagramRateLimitError(
                        "Instagram هنگام دریافت "
                        "اطلاعات پست rate limit اعمال کرده."
                    ) from exc

                if (
                    "401" in message
                    or "unauthorized" in message
                ):

                    raise InstagramAuthenticationError(
                        "Instagram درخواست پست "
                        "را unauthorized رد کرد."
                    ) from exc

                raise InstagramError(
                    f"خطا در دریافت اطلاعات پست: {exc}"
                ) from exc

            username = (
                post.owner_username
                or "instagram"
            )

            # -------------------------
            # Photo
            # -------------------------

            if post.typename == "GraphImage":

                path = (
                    job_dir
                    / f"{username}_{shortcode}.jpg"
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

            # -------------------------
            # Video
            # -------------------------

            elif post.typename == "GraphVideo":

                if not post.video_url:

                    raise InstagramError(
                        "URL ویدیو دریافت نشد."
                    )

                path = (
                    job_dir
                    / f"{username}_{shortcode}.mp4"
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

            # -------------------------
            # Carousel
            # -------------------------

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
                            / (
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
                            / (
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
                    f"نوع پست پشتیبانی نمی‌شود: "
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
    # Stories
    # ============================================================
    
    def _find_story_item(
        self,
        username: str,
        media_id: int,
        owner_id: int,
    ) -> dict | None:
        """
        Fetch one active story item directly from Instagram's
        reels_media endpoint.
    
        If the story URL already contains reel_owner_id,
        owner_id can be used directly.
    
        For normal shared story URLs that do not contain
        reel_owner_id, _download_story() resolves the owner ID
        from the username first.
        """
    
        api_url = (
            "https://www.instagram.com/api/v1/feed/reels_media/"
            f"?reel_ids={owner_id}"
        )
    
        session = self.loader.context._session
    
        # Use the existing cookie helper because Instagram
        # sessions can contain multiple cookies with the same name.
        csrf_token = self._get_cookie("csrftoken")
    
        headers = {
            "User-Agent": session.headers.get(
                "User-Agent",
                (
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 "
                    "Chrome/131 Safari/537.36"
                ),
            ),
            "X-IG-App-ID": "936619743392459",
            "Referer": "https://www.instagram.com/",
            "Accept": "*/*",
        }
    
        if csrf_token:
            headers["X-CSRFToken"] = csrf_token
    
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
                "دریافت اطلاعات استوری به دلیل timeout ناموفق بود."
            ) from exc
    
        except requests.exceptions.RequestException as exc:
            raise InstagramError(
                f"خطا در ارتباط با Instagram برای استوری: {exc}"
            ) from exc
    
        logger.info(
            "Instagram reels_media request: status=%s owner_id=%s media_id=%s",
            response.status_code,
            owner_id,
            media_id,
        )

        # --------------------------------------------------------
        # HTTP error handling
        # --------------------------------------------------------
    
        if response.status_code == 429:
    
            retry_after = None
    
            retry_after_raw = response.headers.get(
                "Retry-After"
            )
    
            if retry_after_raw:
                try:
                    retry_after = int(
                        retry_after_raw
                    )
                except ValueError:
                    retry_after = None
    
            raise InstagramRateLimitError(
                "Instagram برای دریافت استوری rate limit اعمال کرده.",
                retry_after=retry_after,
            )
    
        if response.status_code in {401, 403}:
    
            raise InstagramAuthenticationError(
                f"Instagram درخواست استوری را با HTTP "
                f"{response.status_code} رد کرد."
            )
    
        if response.status_code != 200:
    
            raise InstagramError(
                f"Story API Error: HTTP "
                f"{response.status_code} - "
                f"{response.text[:300]}"
            )
    
        # --------------------------------------------------------
        # Parse JSON
        # --------------------------------------------------------
    
        try:
            data = response.json()
    
        except ValueError as exc:
            raise InstagramError(
                "پاسخ Instagram برای استوری JSON معتبر نیست."
            ) from exc
    
        reels = data.get("reels") or {}
    
        if not reels:
            return None
    
        wanted_id = str(media_id)
    
        # --------------------------------------------------------
        # Find requested story item
        # --------------------------------------------------------
    
        for reel in reels.values():
    
            if not isinstance(reel, dict):
                continue
    
            for item in reel.get("items", []):
    
                if not isinstance(item, dict):
                    continue
    
                item_id = str(
                    item.get("pk")
                    or item.get("id")
                    or ""
                )
    
                if item_id == wanted_id:
                    return item
    
        return None
    
    
    def _download_story(
        self,
        url: str,
    ) -> list[MediaFile]:
    
        # استخراج اطلاعات از لینک استوری
        username_match = re.search(
            r"/stories/([^/]+)/",
            url
        )

        story_id_match = re.search(
            r"/stories/[^/]+/(\d+)",
            url
        )

        owner_id_match = re.search(
            r"reel_owner_id=(\d+)",
            url
        )


        if not username_match or not story_id_match:

            print("Invalid story URL")
            return

        
        username = username_match.group(1)
        print("Story user:", username)
        story_id = story_id_match.group(1)
        print("Story Id:", story_id)


        headers = {

            "User-Agent":
            "Mozilla/5.0",

            "X-IG-App-ID":
            "936619743392459",

            "X-CSRFToken":
            get_cookie("csrftoken"),

            "Referer":
            "https://www.instagram.com/"

        }
        if owner_id_match:
            owner_id = owner_id_match.group(1)
        else:
            session = L_session.context._session

            owner_id = get_user_id_from_html(
                session,
                username
            )
            
        print("Owner ID:", owner_id)

        # --------------------------------------------------------
        # Fetch requested story
        # --------------------------------------------------------
    
        item = self._find_story_item(
            username,
            media_id,
            owner_id,
        )
    
        if item is None:
    
            raise InstagramError(
                "استوری پیدا نشد "
                "یا دیگر فعال نیست."
            )
    
        # --------------------------------------------------------
        # Create temporary directory
        # --------------------------------------------------------
    
        job_dir = self._new_job_dir()
    
        try:
    
            media_type_code = item.get(
                "media_type"
            )
    
            # ====================================================
            # Story Video
            # ====================================================
    
            if media_type_code == 2:
    
                video_versions = (
                    item.get("video_versions")
                    or []
                )
    
                if not video_versions:
    
                    raise InstagramError(
                        "نسخه ویدیویی استوری دریافت نشد."
                    )
    
                # Prefer the first available version.
                media_url = (
                    video_versions[0].get("url")
                )
    
                if not media_url:
    
                    raise InstagramError(
                        "URL ویدیوی استوری دریافت نشد."
                    )
    
                suffix = ".mp4"
                media_type = "video"
    
            # ====================================================
            # Story Photo
            # ====================================================
    
            elif media_type_code == 1:
    
                image_versions = (
                    item.get("image_versions2")
                    or {}
                )
    
                candidates = (
                    image_versions.get(
                        "candidates"
                    )
                    or []
                )
    
                if not candidates:
    
                    raise InstagramError(
                        "نسخه تصویری استوری دریافت نشد."
                    )
    
                # Prefer the first available candidate.
                media_url = (
                    candidates[0].get("url")
                )
    
                if not media_url:
    
                    raise InstagramError(
                        "URL تصویر استوری دریافت نشد."
                    )
    
                suffix = ".jpg"
                media_type = "photo"
    
            # ====================================================
            # Unsupported media type
            # ====================================================
    
            else:
    
                raise InstagramError(
                    "نوع مدیای استوری پشتیبانی نمی‌شود: "
                    f"{media_type_code}"
                )
    
            # ----------------------------------------------------
            # Output path
            # ----------------------------------------------------
    
            path = (
                job_dir
                / (
                    f"{username}_story_"
                    f"{media_id}{suffix}"
                )
            )
    
            # ----------------------------------------------------
            # Download media
            # ----------------------------------------------------
    
            self._download_url(
                media_url,
                path,
            )
    
            return [
                MediaFile(
                    path,
                    media_type,
                )
            ]
    
        except Exception:
    
            shutil.rmtree(
                job_dir,
                ignore_errors=True,
            )
    
            raise
    # ============================================================
    # Highlights
    # ============================================================

    def _download_highlight(
        self,
        url: str,
    ) -> list[MediaFile]:

        match = HIGHLIGHT_RE.search(url)

        if not match:

            raise InstagramError(
                "URL هایلایت معتبر نیست."
            )

        highlight_id = match.group(1)

        session = (
            self.loader.context._session
        )

        api_url = (
            "https://www.instagram.com/"
            "api/v1/feed/reels_media/"
            f"?reel_ids=highlight:{highlight_id}"
        )

        # IMPORTANT:
        # Do not use session.cookies.get("csrftoken")
        # because requests raises CookieConflictError when
        # multiple csrftoken cookies exist.
        csrf_token = self._get_cookie(
            "csrftoken"
        )

        headers = {
            "User-Agent": session.headers.get(
                "User-Agent",
                "Mozilla/5.0",
            ),
            "X-IG-App-ID": "936619743392459",
            "Referer": "https://www.instagram.com/",
            "Accept": "*/*",
        }

        if csrf_token:

            headers["X-CSRFToken"] = (
                csrf_token
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

            self._raise_for_instagram_response(
                response,
                "getting highlight metadata",
            )

            try:

                data = response.json()

            except ValueError as exc:

                raise InstagramError(
                    "پاسخ Instagram برای "
                    "هایلایت معتبر نبود."
                ) from exc

        except InstagramError:
            raise

        except requests.exceptions.Timeout as exc:

            raise InstagramError(
                "دریافت اطلاعات هایلایت "
                "timeout شد."
            ) from exc

        except requests.exceptions.RequestException as exc:

            raise InstagramError(
                "خطا در ارتباط با Instagram "
                f"برای هایلایت: {exc}"
            ) from exc

        highlight = (
            (data.get("reels") or {})
            .get(
                f"highlight:{highlight_id}"
            )
        )

        # Fallback for alternate response key formats
        if not highlight:

            reels = data.get("reels") or {}

            if isinstance(
                reels,
                dict,
            ):

                for key, value in reels.items():

                    if str(key).endswith(
                        str(highlight_id)
                    ):

                        highlight = value
                        break

        if not highlight:

            raise InstagramError(
                "اطلاعات هایلایت پیدا نشد."
            )

        items = (
            highlight.get("items")
            or []
        )

        if not items:

            raise InstagramError(
                "هایلایت خالی است یا "
                "آیتمی قابل دسترسی نیست."
            )

        job_dir = self._new_job_dir()

        result: list[MediaFile] = []

        username = str(
            (
                highlight.get("user")
                or {}
            ).get("username")
            or "highlight"
        )

        try:

            for index, item in enumerate(
                items,
                start=1,
            ):

                media_type = (
                    item.get("media_type")
                )

                # -------------------------
                # Highlight video
                # -------------------------

                if media_type == 2:

                    versions = (
                        item.get(
                            "video_versions"
                        )
                        or []
                    )

                    if not versions:
                        continue

                    media_url = (
                        versions[0].get(
                            "url"
                        )
                    )

                    if not media_url:
                        continue

                    path = (
                        job_dir
                        / (
                            f"{username}_"
                            f"highlight_"
                            f"{highlight_id}_"
                            f"{index}.mp4"
                        )
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

                # -------------------------
                # Highlight photo
                # -------------------------

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
                        continue

                    media_url = (
                        candidates[0].get(
                            "url"
                        )
                    )

                    if not media_url:
                        continue

                    path = (
                        job_dir
                        / (
                            f"{username}_"
                            f"highlight_"
                            f"{highlight_id}_"
                            f"{index}.jpg"
                        )
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

            if not result:

                raise InstagramError(
                    "هایلایت خالی است یا "
                    "فایل قابل دانلود پیدا نشد."
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
