from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import time
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
    re.I,
)


HIGHLIGHT_RE = re.compile(
    r"/stories/highlights/([0-9]+)",
    re.I,
)


HIGHLIGHT_RE2 = re.compile(
    r"/s/([^?]+)",
    re.I,
)


# ============================================================
# Exceptions
# ============================================================


class InstagramError(RuntimeError):
    pass


class InstagramAuthenticationError(
    InstagramError
):
    pass


class InstagramRateLimitError(
    InstagramError
):

    def __init__(
        self,
        message: str,
        retry_after: int | None = None,
    ):

        super().__init__(message)

        self.retry_after = retry_after



# ============================================================
# Media
# ============================================================


@dataclass(frozen=True)
class MediaFile:

    path: Path

    # photo | video
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
    ):

        self.username = (
            username.strip()
        )

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
            /
            f"session-{self.username}"
        )


        self.loader = (
            self._build_loader()
        )



    # ========================================================
    # Build Loader
    # ========================================================


    def _build_loader(
        self,
    ) -> instaloader.Instaloader:


        loader = instaloader.Instaloader(

            save_metadata=False,

            download_comments=False,

            user_agent=(
                "Mozilla/5.0 "
                "(Windows NT 10.0; Win64; x64)"
            ),

            max_connection_attempts=1,

            request_timeout=(
                self.request_timeout_seconds
            ),
        )


        try:

            raw = base64.b64decode(
                self.session_b64,
                validate=True,
            )


        except Exception as exc:

            raise InstagramAuthenticationError(
                "Session base64 invalid."
            ) from exc



        if not raw:

            raise InstagramAuthenticationError(
                "Instagram session empty."
            )



        self.session_path.write_bytes(
            raw
        )



        try:

            stripped = raw.lstrip()


            if (
                stripped.startswith(b"{")
                or
                stripped.startswith(b"[")
            ):

                self._load_json_cookies(
                    loader,
                    raw,
                )


            else:

                loader.load_session_from_file(
                    self.username,
                    str(
                        self.session_path
                    ),
                )


        except Exception as exc:

            raise InstagramAuthenticationError(
                f"Session load failed: {exc}"
            ) from exc



        loader.context._session.headers.update(

            {

                "User-Agent":
                    "Mozilla/5.0 "
                    "(Windows NT 10.0; Win64; x64)",


                "X-IG-App-ID":
                    "936619743392459",


                "Referer":
                    "https://www.instagram.com/",


                "Accept":
                    "*/*",

            }

        )


        logger.info(
            "Instagram session loaded: %s",
            self.username,
        )


        return loader



    # ========================================================
    # Load JSON Cookies
    # ========================================================


    def _load_json_cookies(
        self,
        loader: instaloader.Instaloader,
        raw: bytes,
    ) -> None:


        try:

            data = json.loads(
                raw.decode(
                    "utf-8"
                )
            )


        except Exception as exc:

            raise InstagramAuthenticationError(
                "Invalid JSON cookies."
            ) from exc



        cookies = (
            data.get("cookies")
            if isinstance(data, dict)
            else data
        )


        if not isinstance(
            cookies,
            list,
        ):

            raise InstagramAuthenticationError(
                "Cookies list missing."
            )


        count = 0


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


            if (
                "instagram.com"
                not in domain
            ):

                domain = ".instagram.com"


            loader.context._session.cookies.set(

                name,

                value,

                domain=domain,

                path=cookie.get(
                    "path",
                    "/",
                ),

            )


            count += 1



        if count == 0:

            raise InstagramAuthenticationError(
                "No cookies loaded."
            )



        logger.info(
            "Loaded %s cookies",
            count,
        )

    # ============================================================
    # URL validation
    # ============================================================

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


    # ============================================================
    # Cookie helper
    # ============================================================

    def _get_cookie(
        self,
        name: str,
    ) -> str:

        cookies = (
            self.loader
            .context
            ._session
            .cookies
        )


        for cookie in cookies:

            if cookie.name == name:

                return str(
                    cookie.value
                )


        return ""


    # ============================================================
    # Temporary directory
    # ============================================================

    def _new_job_dir(
        self,
    ) -> Path:

        path = (
            self.temp_dir
            /
            uuid.uuid4().hex
        )

        path.mkdir(
            parents=True,
            exist_ok=False,
        )

        return path


    # ============================================================
    # Dispatcher
    # ============================================================

    def download(
        self,
        url: str,
    ) -> list[MediaFile]:

        url = self.validate_url(
            url
        )


        if (
            HIGHLIGHT_RE.search(url)
            or
            HIGHLIGHT_RE2.search(url)
        ):

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
            "این نوع لینک پشتیبانی نمی‌شود."
        )

    # ============================================================
    # Find User ID From HTML
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
            ) from exc



        self._raise_for_instagram_response(
            response,
            "getting profile HTML"
        )


        html = response.text


        patterns = [

            r'"profile_id":"(\d+)"',

            r'"user_id":"(\d+)"',

            (
                r'"id":"(\d+)"'
                r',"username":"'
                + re.escape(username)
            ),

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
                    "User ID found: %s",
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


        csrf = self._get_cookie(
            "csrftoken"
        )


        if csrf:

            headers[
                "X-CSRFToken"
            ] = csrf



        try:

            response = requests.get(

                api_url,

                headers=headers,

                cookies=session.cookies,

                timeout=20,

            )


        except requests.RequestException as exc:

            raise InstagramError(
                f"خطا در دریافت Story API: {exc}"
            ) from exc



        self._raise_for_instagram_response(
            response,
            "getting story metadata"
        )


        try:

            data = response.json()


        except ValueError as exc:

            raise InstagramError(
                "پاسخ Story معتبر نیست."
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


                if str(
                    item.get(
                        "pk",
                        ""
                    )
                ) == str(media_id):

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


        username = match.group(1)


        media_id = int(
            match.group(2)
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


        if owner_id is None:


            owner_id = (
                self
                ._get_user_id_from_html(
                    username
                )
            )


        if not owner_id:


            raise InstagramError(
                "Owner ID پیدا نشد."
            )



        item = self._find_story_item(

            username,

            media_id,

            owner_id,

        )



        if item is None:


            raise InstagramError(
                "Story پیدا نشد یا منقضی شده."
            )



        job_dir = (
            self._new_job_dir()
        )


        try:


            media_type = item.get(
                "media_type"
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


                    raise InstagramError(
                        "ویدیو CDN پیدا نشد."
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
                    versions[0]
                    .get("url")
                )



                filename = (
                    f"{username}_"
                    f"story_{media_id}.mp4"
                )


                media_type_name = (
                    "video"
                )



            # =================================================
            # PHOTO
            # =================================================

            elif media_type == 1:


                candidates = (

                    item
                    .get(
                        "image_versions2",
                        {}
                    )
                    .get(
                        "candidates"
                    )
                    or []

                )



                if not candidates:


                    raise InstagramError(
                        "تصویر CDN پیدا نشد."
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
                    candidates[0]
                    .get("url")
                )


                filename = (
                    f"{username}_"
                    f"story_{media_id}.jpg"
                )


                media_type_name = (
                    "photo"
                )



            else:


                raise InstagramError(
                    f"نوع Media پشتیبانی نمی‌شود: {media_type}"
                )



            if not media_url:


                raise InstagramError(
                    "CDN URL وجود ندارد."
                )



            # اصلاح escape های JSON

            media_url = (
                media_url
                .replace(
                    "\\u0026",
                    "&"
                )
                .replace(
                    "\\/",
                    "/"
                )
            )



            path = (
                job_dir
                /
                filename
            )



            logger.info(
                "Story media extracted"
            )


            # دانلود واقعی فایل

            self._download_cdn_media(
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
    # POST / REEL / TV
    # ============================================================

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


        job_dir = (
            self._new_job_dir()
        )


        result = []


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


            except Exception as exc:


                raise InstagramError(
                    f"خطا در دریافت پست: {exc}"
                ) from exc



            username = (
                post.owner_username
                or "instagram"
            )



            # =================================================
            # IMAGE
            # =================================================

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



            # =================================================
            # VIDEO
            # =================================================

            elif post.typename == "GraphVideo":


                if not post.video_url:


                    raise InstagramError(
                        "URL ویدیو پیدا نشد."
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



            # =================================================
            # CAROUSEL
            # =================================================

            elif post.typename == "GraphSidecar":



                for index, node in enumerate(

                    post.get_sidecar_nodes(),

                    start=1,

                ):



                    if node.is_video:



                        if not node.video_url:

                            continue



                        path = (

                            job_dir

                            /

                            f"{username}_"
                            f"{shortcode}_"
                            f"{index}.mp4"

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

                            continue



                        path = (

                            job_dir

                            /

                            f"{username}_"
                            f"{shortcode}_"
                            f"{index}.jpg"

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
                    "نوع پست پشتیبانی نمی‌شود."
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



        parents = set()



        for media in files:



            try:


                parents.add(
                    media.path.parent
                )



                media.path.unlink(
                    missing_ok=True
                )



            except Exception:


                logger.exception(
                    "Cannot remove file %s",
                    media.path,
                )



        for parent in parents:


            shutil.rmtree(

                parent,

                ignore_errors=True,

            )

