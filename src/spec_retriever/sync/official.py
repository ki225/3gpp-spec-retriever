"""Read-only 3GPP archive discovery and bounded downloads (Node.js >=18)."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote, urljoin, urlparse

ROOT = "https://www.3gpp.org/ftp/specs/archive/"
MAX_BYTES = 64 * 1024 * 1024

# Node fetch interoperates with the official server on hosts where urllib is
# rejected. Redirects are checked on every hop; this is not a general URL fetcher.
FETCH_JS = r"""
let u = process.argv[1];
let finished = false;
const valid = s => { const x = new URL(s); return x.protocol === 'https:' &&
  x.hostname === 'www.3gpp.org' && !x.port && !x.username && !x.password &&
  !x.search && !x.hash && x.pathname.startsWith('/ftp/specs/archive/'); };
for (let hops=0; hops<5; hops++) {
  if (!valid(u)) throw new Error('Non-official archive URL');
  const r = await fetch(u, {redirect:'manual', signal:AbortSignal.timeout(60000)});
  if ([301,302,303,307,308].includes(r.status)) {
    const loc = r.headers.get('location'); if (!loc) throw new Error('Missing redirect');
    u = new URL(loc,u).href; continue;
  }
  if (!r.ok) throw new Error('HTTP '+r.status);
  let n=0; const chunks=[];
  for await (const b of r.body) {
    n+=b.length; if(n>67108864) throw new Error('Download exceeds 64 MiB'); chunks.push(b);
  }
  await new Promise((resolve, reject) => process.stdout.write(Buffer.concat(chunks), error => error ? reject(error) : resolve()));
  finished = true; break;
}
if (!finished) throw new Error('Too many redirects');
"""

DOWNLOAD_JS = r"""
import {open} from 'node:fs/promises';
let u = process.argv[1];
const target = process.argv[2];
const valid = s => { const x = new URL(s); return x.protocol === 'https:' &&
  x.hostname === 'www.3gpp.org' && !x.port && !x.username && !x.password &&
  !x.search && !x.hash && x.pathname.startsWith('/ftp/specs/archive/'); };
let finished = false;
for (let hops=0; hops<5; hops++) {
  if (!valid(u)) throw new Error('Non-official archive URL');
  const r = await fetch(u, {redirect:'manual', signal:AbortSignal.timeout(300000)});
  if ([301,302,303,307,308].includes(r.status)) {
    const loc = r.headers.get('location'); if (!loc) throw new Error('Missing redirect');
    u = new URL(loc,u).href; continue;
  }
  if (!r.ok) throw new Error('HTTP '+r.status);
  const file = await open(target, 'wx');
  try {
    let n=0;
    for await (const b of r.body) {
      n+=b.length; if(n>536870912) throw new Error('Download exceeds 512 MiB');
      for (let offset=0; offset<b.length;) {
        const written = await file.write(b, offset, b.length-offset);
        offset += written.bytesWritten;
      }
    }
    await file.sync();
  } finally { await file.close(); }
  finished = true; break;
}
if (!finished) throw new Error('Too many redirects');
"""


def official_url(url: str) -> str:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.netloc != "www.3gpp.org" or parsed.query
            or parsed.fragment or not parsed.path.startswith("/ftp/specs/archive/")
            or ".." in unquote(parsed.path).split("/")):
        raise ValueError("expected an official HTTPS 3GPP archive URL")
    return url


def fetch_bytes(url: str) -> bytes:
    official_url(url)
    try:
        result = subprocess.run(["node", "--input-type=module", "-e", FETCH_JS, url],
                                capture_output=True, timeout=75)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError(f"official fetch requires Node.js >=18: {error}") from error
    if result.returncode:
        # Never echo server HTML, environment variables or arbitrary subprocess output.
        message = result.stderr.decode("utf-8", errors="replace")
        status = re.search(r"HTTP \d{3}", message)
        raise ValueError(f"official fetch failed: {url} ({status[0] if status else 'transport error'})")
    if len(result.stdout) > MAX_BYTES:
        raise ValueError("download exceeds 64 MiB")
    return result.stdout


def download_archive(url: str, target: Path) -> None:
    """Stream an official ZIP to a new file without buffering it in memory."""
    official_url(url)
    try:
        result = subprocess.run(["node", "--input-type=module", "-e", DOWNLOAD_JS, url, str(target)],
                                capture_output=True, timeout=330)
    except (OSError, subprocess.TimeoutExpired) as error:
        target.unlink(missing_ok=True)
        raise ValueError(f"official download requires Node.js >=18: {error}") from error
    if result.returncode:
        target.unlink(missing_ok=True)
        message = result.stderr.decode("utf-8", errors="replace")
        status = re.search(r"HTTP \d{3}", message)
        raise ValueError(f"official download failed: {url} ({status[0] if status else 'transport error'})")


class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            self.links.extend(value for key, value in attrs if key == "href" and value)


def links_in(data: bytes) -> list[str]:
    parser = Links()
    parser.feed(data.decode("utf-8-sig", errors="replace"))
    return parser.links


def spec_directory(spec: str) -> str:
    if not re.fullmatch(r"\d{2,3}\.\d{3}(?:-\d+)?", spec):
        raise ValueError(f"unsupported specification number: {spec}")
    return f"{ROOT}{spec.split('.')[0]}_series/{spec}/"


@dataclass(frozen=True)
class Archive:
    spec: str
    version: str
    url: str


class OfficialCatalog:
    def __init__(self, fetch=fetch_bytes):
        self.fetch = fetch
        self.checked_at = datetime.now(timezone.utc).isoformat()
        self._archives = {}

    def archives(self, spec: str) -> list[Archive]:
        if spec not in self._archives:
            directory = spec_directory(spec)
            pattern = re.compile(re.escape(spec.replace(".", "")) + r"-([0-9a-z])([0-9a-z])([0-9a-z])\.zip", re.I)
            found = {}
            document_files = []
            for href in links_in(self.fetch(directory)):
                url = urljoin(directory, href)
                if urlparse(url).path.rsplit("/", 1)[0] + "/" != urlparse(directory).path:
                    continue
                filename = urlparse(url).path.rsplit("/", 1)[-1]
                if filename.lower().endswith((".zip", ".doc", ".docx")):
                    document_files.append(filename)
                match = pattern.fullmatch(filename)
                if match:
                    version = ".".join(str(int(part, 36)) for part in match.groups())
                    found[version] = Archive(spec, version, official_url(url))
            if not found and document_files:
                raise ValueError(f"no canonical archive entries found for {spec}")
            self._archives[spec] = list(found.values())
        return self._archives[spec]

    def latest(self, spec: str, release: int) -> Archive | None:
        candidates = [item for item in self.archives(spec) if int(item.version.split('.')[0]) == release]
        return max(candidates, key=lambda item: tuple(map(int, item.version.split('.'))), default=None)

    def series(self) -> list[str]:
        """Discover all numbered archive series from the official root."""
        root_path = urlparse(ROOT).path.rstrip("/")
        found = set()
        for href in links_in(self.fetch(ROOT)):
            url = urljoin(ROOT, href)
            path = urlparse(url).path.rstrip("/")
            match = re.fullmatch(re.escape(root_path) + r"/(\d{2,3})_series", path)
            if match:
                official_url(url)
                found.add(match[1])
        if not found:
            raise ValueError("no numbered series found in the official archive")
        return sorted(found)

    def series_specs(self, series: str, *, allow_empty: bool = False) -> list[str]:
        if not re.fullmatch(r"\d{2,3}", series):
            raise ValueError("series must be two or three digits")
        directory = f"{ROOT}{series}_series/"
        found = set()
        for href in links_in(self.fetch(directory)):
            url = urljoin(directory, href)
            match = re.fullmatch(re.escape(urlparse(directory).path) + r"(\d{2,3}\.\d{3}(?:-\d+)?)/?", urlparse(url).path)
            if match and match[1].startswith(series):
                official_url(url)
                found.add(match[1])
        if not found and not allow_empty:
            raise ValueError(f"no specification directories found for series {series}")
        return sorted(found)
