from __future__ import annotations

import base64
import json
import logging
import re
import shutil
import uuid
import time

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

    media_type: str


# ============================================================
# Client
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
            self.temp_dir /
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
                "Session base64 invalid"
            ) from exc



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


        return loader

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
                f"Instagram returned HTTP {status} "
                f"while {context}."
            )


        if status == 429:

            retry_after = None

            value = response.headers.get(
                "Retry-After"
            )

            if value:

                try:

                    retry_after = int(value)

                except ValueError:

                    pass


            raise InstagramRateLimitError(
                "Instagram rate limit",
                retry_after=retry_after,
            )


        response.raise_for_status()



    # ========================================================
    # Common Headers For CDN
    # ========================================================

    def _get_media_headers(
        self,
    ) -> dict:


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


            "Origin":
                "https://www.instagram.com",


            "Accept":
                "*/*",


            "Accept-Language":
                "en-US,en;q=0.9",

        }


        return headers



    # ========================================================
    # CDN Downloader
    #
    # مخصوص scontent.cdninstagram.com
    #
    # ========================================================

    def _download_cdn_media(
        self,
        url: str,
        path: Path,
    ) -> None:


        session = (
            self.loader.context._session
        )


        headers = (
            self._get_media_headers()
        )


        last_error = None



        for attempt in range(1, 4):

            try:

                logger.info(
                    "Downloading CDN media attempt %s",
                    attempt,
                )


                with session.get(

                    url,

                    headers=headers,

                    cookies=session.cookies,

                    stream=True,

                    timeout=(

                        20,

                        self.request_timeout_seconds,

                    ),

                    allow_redirects=True,

                ) as response:



                    # ----------------------------------------
                    # CDN sometimes returns temporary 404
                    # ----------------------------------------

                    if response.status_code == 404:

                        last_error = (
                            "CDN returned 404"
                        )

                        time.sleep(
                            attempt * 2
                        )

                        continue



                    self._raise_for_instagram_response(
                        response,
                        "downloading CDN media",
                    )



                    content_type = (
                        response.headers.get(
                            "Content-Type",
                            "",
                        )
                    )


                    logger.info(
                        "CDN Content-Type: %s",
                        content_type,
                    )



                    total = 0



                    with path.open(
                        "wb"
                    ) as file_handle:



                        for chunk in response.iter_content(

                            chunk_size=
                            1024 * 1024

                        ):


                            if not chunk:

                                continue



                            total += len(
                                chunk
                            )



                            if (
                                total
                                >
                                self.max_media_bytes
                            ):

                                raise InstagramError(
                                    "Media file too large"
                                )



                            file_handle.write(
                                chunk
                            )



                    # موفقیت

                    return



            except InstagramError:

                raise



            except requests.RequestException as exc:


                last_error = str(exc)



                time.sleep(
                    attempt * 2
                )



        raise InstagramError(
            "دانلود Media از CDN اینستاگرام "
            "ناموفق بود."
        )




    # ========================================================
    # Direct Media Download
    # ========================================================

    def _download_url(
        self,
        url: str,
        path: Path,
    ) -> None:


        # ----------------------------------------
        # Instagram CDN
        # ----------------------------------------

        if (
            "cdninstagram.com" in url
            or
            "scontent" in url
        ):

            return self._download_cdn_media(
                url,
                path,
            )



        headers = (
            self._get_media_headers()
        )


        session = (
            self.loader.context._session
        )



        try:


            with session.get(

                url,

                headers=headers,

                cookies=session.cookies,

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



                total = 0



                with path.open(
                    "wb"
                ) as file_handle:


                    for chunk in response.iter_content(

                        chunk_size=
                        1024 * 1024

                    ):


                        if not chunk:

                            continue



                        total += len(
                            chunk
                        )


                        if (
                            total
                            >
                            self.max_media_bytes
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



        except requests.RequestException:


            # مهم:
            # URL کامل CDN را داخل تلگرام نشان نمی‌دهیم

            raise InstagramError(
                "خطا در دریافت فایل رسانه‌ای "
                "از Instagram."
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
                        "CDN URL ویدئو پیدا نشد."
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


                if media_url:

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
                    )
                    .get(
                        "candidates"
                    )
                    or []
                )


                if not candidates:

                    raise InstagramError(
                        "CDN URL تصویر پیدا نشد."
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


                if media_url:

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
                    f"{username}_"
                    f"story_{media_id}.jpg"
                )


                media_type_name = (
                    "photo"
                )


            else:

                raise InstagramError(
                    f"نوع استوری پشتیبانی نمی‌شود: "
                    f"{media_type}"
                )


            if not media_url:

                raise InstagramError(
                    "CDN URL پیدا نشد."
                )


            logger.info(
                "Instagram Story CDN URL extracted"
            )


            # =================================================
            # Download from CDN
            # =================================================

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
