import argparse
import os
from queue import Queue
from threading import Thread
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup


def normalize_git_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "http://" + url

    if not url.rstrip("/").endswith(".git"):
        url = url.rstrip("/") + "/.git"

    return url.rstrip("/") + "/"


def safe_join(base_dir: str, relative_path: str) -> str:
    target = os.path.abspath(os.path.join(base_dir, relative_path))
    base = os.path.abspath(base_dir)

    if not target.startswith(base):
        raise ValueError(f"unsafe path: {relative_path}")

    return target


def should_skip_existing(file_path: str) -> bool:
    if not os.path.exists(file_path):
        return False

    try:
        with open(file_path, "r", encoding="utf-8") as f:
            data = f.read()
        return "系统发生错误" not in data
    except UnicodeDecodeError:
        return True


def download_file(session: requests.Session, file_url: str, file_path: str, timeout: int) -> None:
    if should_skip_existing(file_path):
        print(f"skip existing: {file_path}")
        return

    try:
        r = session.get(file_url, timeout=timeout)
    except requests.RequestException as e:
        print(f"request error: {file_url} - {e}")
        return

    if r.status_code != 200:
        print(f"download error [{r.status_code}]: {file_url}")
        return

    os.makedirs(os.path.dirname(file_path), exist_ok=True)

    with open(file_path, "wb") as f:
        f.write(r.content)

    print(f"download: {file_path}")


def worker(queue: Queue, base_url: str, output_dir: str, timeout: int) -> None:
    session = requests.Session()

    while True:
        path = queue.get()

        if path is None:
            queue.task_done()
            break

        url = urljoin(base_url, path)
        print(f"file tree: {url}")

        try:
            res = session.get(url, timeout=timeout)
        except requests.RequestException as e:
            print(f"request error: {url} - {e}")
            queue.task_done()
            continue

        if res.status_code != 200:
            print(f"tree error [{res.status_code}]: {url}")
            queue.task_done()
            continue

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
                continue

            file_url = urljoin(url, href)
            file_path = safe_join(output_dir, child_path)
            download_file(session, file_url, file_path, timeout)

        queue.task_done()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Download files from an exposed .git directory. Use only on authorized targets."
    )
    parser.add_argument("url", help="target .git URL, e.g. https://example.com/.git/")
    parser.add_argument("-o", "--output", default="create/.git", help="output directory")
    parser.add_argument("-t", "--threads", type=int, default=16, help="worker threads")
    parser.add_argument("--timeout", type=int, default=10, help="request timeout seconds")

    args = parser.parse_args()

    base_url = normalize_git_url(args.url)
    output_dir = os.path.abspath(args.output)

    parsed = urlparse(base_url)
    if not parsed.netloc:
        raise SystemExit(f"invalid URL: {args.url}")

    queue = Queue()
    queue.put("")

    threads = []
    for _ in range(args.threads):
        t = Thread(target=worker, args=(queue, base_url, output_dir, args.timeout), daemon=True)
        t.start()
        threads.append(t)

    queue.join()

    for _ in threads:
        queue.put(None)

    for t in threads:
        t.join()

    print(f"done: {output_dir}")


if __name__ == "__main__":
    main()
