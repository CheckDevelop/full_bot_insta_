from __future__ import annotations

import base64
import json
import logging
import os
import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import instaloader
import requests

logger = logging.getLogger(__name__)

INSTAGRAM_URL_RE = re.compile(r"^https?://(?:www\.)?instagram\.com/", re.I)
POST_RE = re.compile(r"/((?:p)|(?:reel)|(?:tv))/([^/?#]+)", re.I)
STORY_RE = re.compile(r"/stories/([^/]+)/([0-9]+)", re.I)
HIGHLIGHT_RE = re.compile(r"/stories/highlights/([0-9]+)", re.I)


class InstagramError(RuntimeError):
    pass


class InstagramAuthenticationError(InstagramError):
    pass


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
        self.username = username
        self.session_b64 = session_b64
        self.temp_dir = temp_dir
        self.max_media_bytes = max_media_bytes
        self.request_timeout_seconds = request_timeout_seconds
        self.session_path = temp_dir / f"session-{username}"
        self.loader = self._build_loader()

    def _build_loader(self) -> instaloader.Instaloader:
        """Build Instaloader and load the existing Instagram session.

        Important: startup does NOT call loader.test_login(). Instagram may
        temporarily answer 401/429 during startup, and that should not make
        the whole Telegram service crash. The real Instagram request is
        allowed to validate the session when a job is processed.
        """
        loader = instaloader.Instaloader(
            save_metadata=False,
            download_comments=False,
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "Chrome/131 Safari/537.36"
            ),
            max_connection_attempts=3,
            request_timeout=self.request_timeout_seconds,
        )

        try:
            raw = base64.b64decode(self.session_b64, validate=True)
        except Exception as exc:
            raise InstagramAuthenticationError(
                "INSTAGRAM_SESSION_B64 is not valid base64"
            ) from exc

        if not raw:
            raise InstagramAuthenticationError("Instagram session is empty")

        self.temp_dir.mkdir(parents=True, exist_ok=True)
        self.session_path.write_bytes(raw)

        try:
            stripped = raw.lstrip()

            # Existing project format: instagram.json containing cookies.
            if stripped.startswith(b"{") or stripped.startswith(b"["):
                self._load_json_cookies(loader, raw)

            # Native Instaloader session file (pickle) format.
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

        # Do not call loader.test_login() here.
        # A temporary Instagram 401/429 must not crash the whole bot.
        logger.info(
            "Instagram session loaded for @%s; startup login validation skipped",
            self.username,
        )

        return loader

    def _load_json_cookies(
        self,
        loader: instaloader.Instaloader,
        raw: bytes,
    ) -> None:
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise InstagramAuthenticationError(
                "INSTAGRAM_SESSION_B64 contains JSON, but the JSON is invalid"
            ) from exc

        cookies = data.get("cookies") if isinstance(data, dict) else data

        if isinstance(cookies, dict):
            # Simple mapping: {"sessionid": "...", "csrftoken": "..."}
            cookie_items = [
                {"name": name, "value": value}
                for name, value in cookies.items()
            ]
        elif isinstance(cookies, list):
            cookie_items = cookies
        else:
            raise InstagramAuthenticationError(
                "Instagram JSON session must contain a 'cookies' list or cookie mapping"
            )

        loaded = 0
        for cookie in cookie_items:
            if not isinstance(cookie, dict):
                continue

            name = cookie.get("name")
            value = cookie.get("value")
            if not name or value is None:
                continue

            domain = str(cookie.get("domain") or ".instagram.com")
            path = str(cookie.get("path") or "/")

            # Never trust a cookie export to point to an unrelated host.
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
                "No Instagram cookies were found in INSTAGRAM_SESSION_B64"
            )

        # Keep headers compatible with the old working implementation.
        loader.context._session.headers.update(
            {
                "User-Agent": "Mozilla/5.0",
                "X-IG-App-ID": "936619743392459",
                "Referer": "https://www.instagram.com/",
                "Accept": "*/*",
            }
        )

        logger.info("Loaded %d Instagram cookies from JSON session", loaded)

    @staticmethod
    def validate_url(url: str) -> str:
        normalized = url.strip()
        parsed = urlparse(normalized)

        if parsed.scheme not in {"http", "https"}:
            raise InstagramError("لینک اینستاگرام معتبر نیست.")

        if parsed.netloc.lower() not in {
            "instagram.com",
            "www.instagram.com",
        }:
            raise InstagramError("لینک اینستاگرام معتبر نیست.")

        return normalized

    def download(self, url: str) -> list[MediaFile]:
        """Route URL to the correct downloader.

        Highlight MUST be checked before Story because
        /stories/highlights/<id>/ also matches STORY_RE.
        """
        url = self.validate_url(url)

        if HIGHLIGHT_RE.search(url):
            return self._download_highlight(url)

        if STORY_RE.search(url):
            return self._download_story(url)

        if POST_RE.search(url):
            return self._download_post(url)

        raise InstagramError(
            "این نوع لینک اینستاگرام در نسخه فعلی پشتیبانی نمی‌شود."
        )

    def _new_job_dir(self) -> Path:
        self.temp_dir.mkdir(parents=True, exist_ok=True)
        job_dir = self.temp_dir / uuid.uuid4().hex
        job_dir.mkdir(parents=True, exist_ok=False)
        return job_dir

    def _download_url(self, url: str, path: Path) -> None:
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
                timeout=(20, self.request_timeout_seconds),
                allow_redirects=True,
            ) as response:
                response.raise_for_status()

                length = response.headers.get("Content-Length")
                if length:
                    try:
                        if int(length) > self.max_media_bytes:
                            raise InstagramError("فایل بیش از حد مجاز بزرگ است.")
                    except ValueError:
                        pass

                total = 0
                with path.open("wb") as file_handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        if not chunk:
                            continue

                        total += len(chunk)
                        if total > self.max_media_bytes:
                            raise InstagramError("فایل بیش از حد مجاز بزرگ است.")

                        file_handle.write(chunk)

        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status in {401, 403}:
                raise InstagramAuthenticationError(
                    "Instagram اجازه دریافت فایل را نداد. Session ممکن است منقضی شده باشد."
                ) from exc
            if status == 429:
                raise InstagramError(
                    "Instagram موقتاً درخواست‌ها را محدود کرده است. لطفاً چند دقیقه بعد دوباره تلاش کنید."
                ) from exc
            raise InstagramError(
                f"خطای HTTP هنگام دانلود فایل Instagram: {status or 'unknown'}"
            ) from exc
        except requests.RequestException as exc:
            raise InstagramError("خطای شبکه هنگام دانلود فایل Instagram.") from exc
        except OSError as exc:
            raise InstagramError("ذخیره فایل دانلودشده ناموفق بود.") from exc

    def _download_post(self, url: str) -> list[MediaFile]:
        match = POST_RE.search(url)
        if not match:
            raise InstagramError("URL پست معتبر نیست.")

        shortcode = match.group(2)
        job_dir = self._new_job_dir()
        result: list[MediaFile] = []

        try:
            post = instaloader.Post.from_shortcode(
                self.loader.context,
                shortcode,
            )
            username = post.owner_username or "instagram"

            if post.typename == "GraphImage":
                path = job_dir / f"{username}_{shortcode}.jpg"
                self._download_url(post.url, path)
                result.append(MediaFile(path, "photo"))

            elif post.typename == "GraphVideo":
                if not post.video_url:
                    raise InstagramError("URL ویدیو دریافت نشد.")

                path = job_dir / f"{username}_{shortcode}.mp4"
                self._download_url(post.video_url, path)
                result.append(MediaFile(path, "video"))

            elif post.typename == "GraphSidecar":
                for index, node in enumerate(
                    post.get_sidecar_nodes(),
                    start=1,
                ):
                    if node.is_video:
                        if not node.video_url:
                            raise InstagramError(
                                "URL یکی از ویدیوهای carousel دریافت نشد."
                            )

                        path = job_dir / f"{username}_{shortcode}_{index}.mp4"
                        self._download_url(node.video_url, path)
                        result.append(MediaFile(path, "video"))
                    else:
                        path = job_dir / f"{username}_{shortcode}_{index}.jpg"
                        self._download_url(node.display_url, path)
                        result.append(MediaFile(path, "photo"))

            else:
                raise InstagramError(
                    f"نوع پست پشتیبانی نمی‌شود: {post.typename}"
                )

            return result

        except instaloader.exceptions.LoginRequiredException as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise InstagramAuthenticationError(
                "Session اینستاگرام معتبر نیست یا نیاز به ورود مجدد دارد."
            ) from exc
        except instaloader.exceptions.PrivateProfileNotFollowedException as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise InstagramError(
                "این پست مربوط به یک حساب خصوصی است که Session فعلی اجازه دسترسی به آن را ندارد."
            ) from exc
        except instaloader.exceptions.TooManyRequestsException as exc:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise InstagramError(
                "Instagram موقتاً درخواست‌ها را محدود کرده است. لطفاً کمی بعد دوباره تلاش کنید."
            ) from exc
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    def _find_story_item(self, username: str, media_id: int):
        try:
            profile = instaloader.Profile.from_username(
                self.loader.context,
                username,
            )

            for story in self.loader.get_stories(userids=[profile.userid]):
                for item in story.get_items():
                    if int(item.mediaid) == media_id:
                        return item

        except instaloader.exceptions.LoginRequiredException as exc:
            raise InstagramAuthenticationError(
                "Session اینستاگرام برای استوری معتبر نیست."
            ) from exc
        except instaloader.exceptions.TooManyRequestsException as exc:
            raise InstagramError(
                "Instagram برای استوری موقتاً درخواست‌ها را محدود کرده است."
            ) from exc

        return None

    def _download_story(self, url: str) -> list[MediaFile]:
        match = STORY_RE.search(url)
        if not match:
            raise InstagramError("URL استوری معتبر نیست.")

        username = match.group(1)
        media_id = int(match.group(2))

        item = self._find_story_item(username, media_id)
        if item is None:
            raise InstagramError("استوری پیدا نشد یا دیگر فعال نیست.")

        job_dir = self._new_job_dir()
        suffix = ".mp4" if item.is_video else ".jpg"
        path = job_dir / f"{username}_story_{media_id}{suffix}"

        try:
            media_url = item.video_url if item.is_video else item.url
            self._download_url(media_url, path)
            return [
                MediaFile(
                    path,
                    "video" if item.is_video else "photo",
                )
            ]
        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    def _download_highlight(self, url: str) -> list[MediaFile]:
        match = HIGHLIGHT_RE.search(url)
        if not match:
            raise InstagramError("URL هایلایت معتبر نیست.")

        highlight_id = match.group(1)
        session = self.loader.context._session

        api_url = (
            "https://www.instagram.com/api/v1/feed/reels_media/"
            f"?reel_ids=highlight:{highlight_id}"
        )

        headers = {
            "User-Agent": session.headers.get("User-Agent", "Mozilla/5.0"),
            "X-IG-App-ID": "936619743392459",
            "X-CSRFToken": session.cookies.get("csrftoken", ""),
            "Referer": "https://www.instagram.com/",
            "Accept": "application/json, text/plain, */*",
        }

        try:
            response = session.get(
                api_url,
                headers=headers,
                timeout=(20, self.request_timeout_seconds),
            )

            if response.status_code in {401, 403}:
                raise InstagramAuthenticationError(
                    "Instagram اجازه دسترسی به Highlight را نداد. Session ممکن است منقضی یا محدود شده باشد."
                )

            if response.status_code == 429:
                raise InstagramError(
                    "Instagram موقتاً درخواست‌های Highlight را محدود کرده است. لطفاً چند دقیقه بعد دوباره تلاش کنید."
                )

            response.raise_for_status()
            data = response.json()

            highlight = data.get("reels", {}).get(
                f"highlight:{highlight_id}"
            )

            if not highlight:
                raise InstagramError("اطلاعات هایلایت پیدا نشد.")

            items = highlight.get("items", [])

        except InstagramError:
            raise
        except InstagramAuthenticationError:
            raise
        except requests.RequestException as exc:
            raise InstagramError(
                "دسترسی به اطلاعات هایلایت ناموفق بود."
            ) from exc
        except ValueError as exc:
            raise InstagramError(
                "پاسخ اینستاگرام برای هایلایت معتبر نبود."
            ) from exc

        job_dir = self._new_job_dir()
        result: list[MediaFile] = []
        username = str(
            highlight.get("user", {}).get("username")
            or "highlight"
        )

        try:
            for index, item in enumerate(items, start=1):
                media_type = item.get("media_type")

                if media_type == 2:
                    versions = item.get("video_versions") or []
                    if not versions:
                        continue

                    media_url = versions[0].get("url")
                    if not media_url:
                        continue

                    path = (
                        job_dir
                        / f"{username}_highlight_{highlight_id}_{index}.mp4"
                    )
                    self._download_url(media_url, path)
                    result.append(MediaFile(path, "video"))

                elif media_type == 1:
                    candidates = (
                        item.get("image_versions2", {}).get("candidates")
                        or []
                    )
                    if not candidates:
                        continue

                    media_url = candidates[0].get("url")
                    if not media_url:
                        continue

                    path = (
                        job_dir
                        / f"{username}_highlight_{highlight_id}_{index}.jpg"
                    )
                    self._download_url(media_url, path)
                    result.append(MediaFile(path, "photo"))

            if not result:
                raise InstagramError(
                    "هایلایت خالی است یا فایل قابل دانلود پیدا نشد."
                )

            return result

        except Exception:
            shutil.rmtree(job_dir, ignore_errors=True)
            raise

    @staticmethod
    def cleanup_files(files: list[MediaFile]) -> None:
        parents: set[Path] = set()

        for media in files:
            try:
                parents.add(media.path.parent)
                media.path.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Failed to remove file %s",
                    media.path,
                )

        for parent in parents:
            shutil.rmtree(parent, ignore_errors=True)
