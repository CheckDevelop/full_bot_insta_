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
    r"https?://(?:www\.)?instagram\.com/"
    r"stories/"
    r"([^/?]+)/"
    r"(\d+)"
    r"(?:/|\?|$)",
    re.IGNORECASE
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

            domain = str(
                cookie.get("domain")
                or ".instagram.com"
            )

            path = str(
                cookie.get("path")
                or "/"
            )

            # Never trust cookie exports to point
            # at unrelated domains.
            if "instagram.com" not in domain.lower():
                domain = ".instagram.com"

            loader.context._session.cookies.set(
                str(name),
                str(value),
                domain=domain,
                path=path,
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
    # Instagram Story
    # ============================================================
    
    def _get_story_owner_id(
        self,
        username: str
    ) -> int | None:
        """
        Resolve Instagram user ID for Story.
        Only used for Story downloader.
        """
    
        session = self.loader.context._session
    
    
        # =====================================================
        # Method 1 - Instaloader
        # =====================================================
    
        try:
    
            print(
                f"Trying Instaloader profile for @{username}"
            )
    
            profile = instaloader.Profile.from_username(
                self.loader.context,
                username
            )
    
            user_id = int(
                profile.userid
            )
    
            print(
                "Owner ID from Instaloader:",
                user_id
            )
    
            return user_id
    
    
        except Exception as e:
    
            print(
                "Instaloader failed:",
                e
            )
    
    
        # =====================================================
        # Method 2 - Instagram web_profile_info
        # =====================================================
    
        try:
    
            print(
                "Trying web_profile_info..."
            )
    
    
            url = (
                "https://www.instagram.com/api/v1/users/"
                "web_profile_info/"
                f"?username={username}"
            )
    
    
            headers = {
    
                "User-Agent":
                    session.headers.get(
                        "User-Agent",
                        "Mozilla/5.0"
                    ),
    
                "X-IG-App-ID":
                    "936619743392459",
    
                "X-Requested-With":
                    "XMLHttpRequest",
    
                "Referer":
                    f"https://www.instagram.com/{username}/",
    
                "Accept":
                    "*/*"
    
            }
    
    
            csrf = self._get_cookie(
                "csrftoken"
            )
    
    
            if csrf:
    
                headers[
                    "X-CSRFToken"
                ] = csrf
    
    
    
            response = session.get(
                url,
                headers=headers,
                timeout=15
            )
    
    
            print(
                "web_profile_info status:",
                response.status_code
            )
    
    
            if response.status_code == 200:
    
    
                data = response.json()
    
    
                user = (
                    data
                    .get("data", {})
                    .get("user", {})
                )
    
    
                user_id = user.get(
                    "id"
                )
    
    
                if user_id:
    
                    user_id = int(
                        user_id
                    )
    
    
                    print(
                        "Owner ID from API:",
                        user_id
                    )
    
    
                    return user_id
    
    
        except Exception as e:
    
            print(
                "web_profile_info failed:",
                e
            )
    
    
        print(
            "Could not resolve Story owner ID"
        )
    
    
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
            "https://www.instagram.com/api/v1/feed/reels_media/"
            f"?reel_ids={owner_id}"
        )
    
        session = self.loader.context._session
    
        csrf_token = self._get_cookie(
            "csrftoken"
        )
    
        headers = {
            "User-Agent": session.headers.get(
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
    
            headers["X-CSRFToken"] = csrf_token
    
        print(
            "Getting story metadata..."
        )
    
        print(
            "Owner ID:",
            owner_id
        )
    
        print(
            "Media ID:",
            media_id
        )
    
        try:
    
            response = session.get(
                api_url,
                headers=headers,
                timeout=(
                    20,
                    self.request_timeout_seconds
                )
            )
    
        except requests.RequestException as exc:
    
            raise InstagramError(
                f"خطا در دریافت اطلاعات استوری: {exc}"
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
                "پاسخ Instagram برای Story معتبر نیست."
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
                    item.get("pk", "")
                )
    
                if item_id == str(
                    media_id
                ):
    
                    print(
                        "Story item found:",
                        item_id
                    )
    
                    return item
    
        print(
            "Story media ID was not found."
        )
    
        return None
    
    
    # ============================================================
    # Download Story
    # ============================================================
    
    def _download_story(
        self,
        url: str,
    ) -> list[MediaFile]:
    
        # --------------------------------------------------------
        # Parse Story URL
        # --------------------------------------------------------
    
        match = STORY_RE.search(
            url
        )
    
        if not match:
    
            raise InstagramError(
                "URL استوری معتبر نیست."
            )
    
        username = match.group(1)
    
        media_id = int(
            match.group(2)
        )
    
        print(
            "Story username:",
            username
        )
    
        print(
            "Story media ID:",
            media_id
        )
    
        # --------------------------------------------------------
        # Get owner ID from URL
        # --------------------------------------------------------
    
        parsed = urlparse(
            url
        )
    
        owner_id = None
    
        query_params = dict(
            parse_qsl(
                parsed.query
            )
        )
    
        # Primary parameter
        reel_owner_id = query_params.get(
            "reel_owner_id"
        )
    
        if (
            reel_owner_id
            and reel_owner_id.isdigit()
        ):
    
            owner_id = int(
                reel_owner_id
            )
    
            print(
                "Owner ID found in URL:",
                owner_id
            )
    
        # --------------------------------------------------------
        # If owner ID is not in URL
        # --------------------------------------------------------
    
        if owner_id is None:
        
        
            print(
                "Owner ID not found in URL."
            )
        
        
            owner_id = self._get_story_owner_id(
                username
            )
        
        
            if owner_id is None:
        
                raise InstagramError(
                    "Instagram نتوانست Owner ID این Story را پیدا کند."
                )
        
        
            print(
                "Resolved Owner ID:",
                owner_id
            )
    
        # ========================================================
        # Find Story
        # ========================================================
    
        item = self._find_story_item(
            username,
            media_id,
            owner_id
        )
    
        if item is None:
    
            raise InstagramError(
                "استوری پیدا نشد، منقضی شده یا "
                "Media ID مربوط به این User نیست."
            )
    
        # ========================================================
        # Create job directory
        # ========================================================
    
        job_dir = self._new_job_dir()
    
        try:
    
            media_type = item.get(
                "media_type"
            )
    
            # ----------------------------------------------------
            # VIDEO
            # ----------------------------------------------------
    
            if media_type == 2:
    
                video_versions = item.get(
                    "video_versions",
                    []
                )
    
                media_url = None
    
                for version in video_versions:
    
                    if not isinstance(
                        version,
                        dict
                    ):
    
                        continue
    
                    candidate_url = version.get(
                        "url"
                    )
    
                    if candidate_url:
    
                        media_url = candidate_url
    
                        break
    
                if not media_url:
    
                    raise InstagramError(
                        "CDN URL ویدئوی استوری پیدا نشد."
                    )
    
                path = (
                    job_dir /
                    f"{username}_story_{media_id}.mp4"
                )
    
                file_type = "video"
    
            # ----------------------------------------------------
            # PHOTO
            # ----------------------------------------------------
    
            elif media_type == 1:
    
                candidates = (
                    item
                    .get(
                        "image_versions2",
                        {}
                    )
                    .get(
                        "candidates",
                        []
                    )
                )
    
                media_url = None
    
                for candidate in candidates:
    
                    if not isinstance(
                        candidate,
                        dict
                    ):
    
                        continue
    
                    candidate_url = candidate.get(
                        "url"
                    )
    
                    if candidate_url:
    
                        media_url = candidate_url
    
                        break
    
                if not media_url:
    
                    raise InstagramError(
                        "CDN URL تصویر استوری پیدا نشد."
                    )
    
                path = (
                    job_dir /
                    f"{username}_story_{media_id}.jpg"
                )
    
                file_type = "photo"
    
            # ----------------------------------------------------
            # Unsupported
            # ----------------------------------------------------
    
            else:
    
                raise InstagramError(
                    f"نوع استوری پشتیبانی نمی‌شود: "
                    f"{media_type}"
                )
    
            # ----------------------------------------------------
            # CDN
            # ----------------------------------------------------
    
            print(
                "========================================"
            )
    
            print(
                "Story CDN URL:"
            )
    
            print(
                media_url
            )
    
            print(
                "========================================"
            )
    
            # ----------------------------------------------------
            # Download
            # ----------------------------------------------------
    
            self._download_url(
                media_url,
                path
            )
    
            print(
                "Story downloaded:"
            )
    
            print(
                path
            )
    
            return [
                MediaFile(
                    path,
                    file_type
                )
            ]
    
        except Exception:
    
            shutil.rmtree(
                job_dir,
                ignore_errors=True
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
