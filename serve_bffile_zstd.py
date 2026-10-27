#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   # until a bffile release has tile_size="auto" and keeps RGB samples in one chunk
#   # (bffile#75): the commit of feat/auto-tile-size this script was tested with
#   "bffile[zarr] @ git+https://github.com/psobolewskiPhD/bffile@573eb813a13cbf8a5bec5355f93484d49aa91924",
#   "click",
#   "numcodecs",
#   "scyjava",
#   "zarr-http-server",
# ]
# ///
"""Serve every image file under a directory as a read-only Zarr v3 store over HTTP,
read with Bio-Formats and re-encoded with zstd.

bffile (https://github.com/imaging-formats/bffile) wraps Bio-Formats, which reads
some 150 formats, and presents a file as an OME-Zarr (NGFF v0.5) hierarchy:
bioformats2raw layout, one multiscales group per series. This script serves that
hierarchy with zarr-http-server. Each chunk is decoded by Bio-Formats on request and
compressed with zstd, zarr-python's default codec for Zarr v3, so most Zarr v3
clients read it without extra codecs.

    uv run serve_bffile_zstd.py /path/to/images

Each option can also be set by an environment variable, BFFILE_SERVE_<OPTION>, e.g.
BFFILE_SERVE_ROOT or BFFILE_SERVE_READERS (--help lists them); a flag on the
command line wins.

    GET /<path/to/image.czi>/zarr.json          -> root group metadata
    GET /<path/to/image.czi>/0/zarr.json        -> series 0: multiscales metadata
    GET /<path/to/image.czi>/0/0/c/0/0/0/1/2    -> one chunk of series 0, resolution 0
    GET /<path/to/image.czi>/OME/METADATA.ome.xml
    GET /_status                                -> the images currently open (--no-status hides it)

Each image is also served at /<path>.zarr, since some clients (napari-ome-zarr)
only recognize URLs ending in ".zarr".

Any file under the directory that Bio-Formats can read is served; any other gets
the same 404 as a missing file. A multi-file format (e.g. .vsi with its folder) is
served at its main file.

Chunks follow each array's native tiles (bffile's tile_size="auto"), grouped up to
512 pixels a side, or are whole planes for readers without native tiles. T, C and Z
are chunked by 1; RGB samples are served as channels.

Each open image has one reader for metadata and --readers more that read its chunks
in parallel, so up to --max-open x (--readers + 1) files are open at once.

Everything bffile writes is served, including OME/METADATA.ome.xml, the file's
OME-XML metadata. Keep the server private, like the image files themselves.
"""

import asyncio
import contextlib
import gc
import json
import logging
import pathlib
import queue
import signal
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import NamedTuple

import click
import numcodecs
import scyjava
from bffile import BioFile
from bffile._zarr import BFOmeZarrStore
from bffile._zarr._group_store import ParsedPath, PathLevel
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route
from zarr_http_server import serve, store_app

log = logging.getLogger("bffile-zstd")

ZARR_URL_SUFFIX = ".zarr"
# zarr-python's default for Zarr v3 is zstd at level 0, i.e. zstd's default, 3.
# Level 1 compressed real chunks within 2-7% of level 3, and 1.3-4x faster
ZSTD_LEVEL = 1
ZSTD_CODEC = {"name": "zstd", "configuration": {"level": ZSTD_LEVEL, "checksum": False}}


def decouple_jpype_gc() -> int:
    """Remove JPype's gc.callbacks hook; returns how many copies were removed. Call it
    after the JVM has started.

    The hook couples Python's garbage collector to Java's: after every Java
    collection (about one per two chunks read) JPype runs a full Python gc.collect()
    holding the GIL, which stalls the event loop and every read. Removing it gives
    3-4x the chunks per second. JPype 1.7 installs it twice. Removing it also stops
    those collections, but only because JPype then never resets the flag that
    suppresses them; a fix for jpype#1506 could change that. See
    https://github.com/jpype-project/jpype/issues/1197 and
    https://github.com/imaging-formats/bffile/pull/78."""
    try:
        import _jpype
    except ImportError:
        return 0
    hook = getattr(_jpype, "_collect", None)
    n = gc.callbacks.count(hook) if hook else 0
    gc.callbacks[:] = [cb for cb in gc.callbacks if cb != hook]
    return n


class ImageRequest(NamedTuple):
    """A request URL, split into the image file it names and the Zarr key within it."""

    image: str   # the image file's path under the root, e.g. "a/image.czi"
    prefix: str  # the URL path that named it, e.g. "a/image.czi.zarr"
    key: str     # the Zarr key, e.g. "0/0/c/0/0/0/1/2"


class TiledStore(BFOmeZarrStore):
    """bffile's OME-Zarr store with tile_size="auto", chunks compressed with zstd
    (arrays' zarr.json lists the codec), and get() run in worker threads.

    A Bio-Formats reader isn't thread-safe, so the store's own reader serves metadata
    under a lock, and chunks are read on a pool of `readers` more BioFiles of the
    same file, each with its own open file, so they decode in parallel."""

    def __init__(self, biofile: BioFile, readers: int):
        super().__init__(biofile, tile_size="auto")
        self._read_lock = threading.Lock()  # for the store's own reader
        self._zstd = numcodecs.Zstd(level=ZSTD_LEVEL)
        # chunk readers free or not yet opened: requests wait here, not in a thread
        self._free = asyncio.Semaphore(readers)
        # idle chunk readers, closed by their BioFiles' finalizers once this store is freed
        self._idle: queue.SimpleQueue[BFOmeZarrStore] = queue.SimpleQueue()

    async def get(self, key, prototype, byte_range=None):
        parsed = ParsedPath.from_key(key)
        if parsed is None:
            return None
        if parsed.level == PathLevel.CHUNK:
            data = await self._get_chunk(parsed)
        else:
            buf = await asyncio.to_thread(self._get_metadata, key, prototype)
            data = None if buf is None else buf.to_bytes()
            if data is not None and parsed.level == PathLevel.ARRAY_METADATA:
                data = with_zstd_codec(data)
        if data is None:
            return None
        if byte_range is not None:  # taken from what is sent, i.e. compressed
            data = self._apply_byte_range(data, byte_range)
        return prototype.buffer.from_bytes(data)

    async def _get_chunk(self, parsed: ParsedPath) -> bytes | None:
        """The chunk compressed with zstd, or None if the key names no chunk. It is
        compressed after its reader is back in the pool, free to read the next."""
        key = (parsed.series, parsed.resolution)
        array = self._array_stores.get(key)
        if array is None:
            array = await asyncio.to_thread(self._create_array, *key)
        if array is None or (coords := array._parse_chunk_key(parsed.chunk_key)) is None:
            return None
        async with self._free:
            data = await asyncio.to_thread(self._read_chunk, key, coords)
        return await asyncio.to_thread(self._zstd.encode, data)

    def _create_array(self, series: int, resolution: int):
        """The array's store, created on first use; None if the file has no such array."""
        with self._read_lock:
            bf = self._biofile
            if 0 <= series < len(bf) and 0 <= resolution < bf.core_metadata(series).resolution_count:
                return self._get_array_store(series, resolution)
            return None

    def _read_chunk(self, key: tuple[int, int], coords) -> bytes:
        """Read a chunk on an idle chunk reader, opened if none is idle. Call with a
        free reader acquired, so at most `readers` are ever opened."""
        try:
            store = self._idle.get_nowait()
        except queue.Empty:
            # chunks need no OME metadata, which can take long to build
            biofile = BioFile(self._biofile.filename, meta=False).open()
            store = BFOmeZarrStore(biofile, tile_size="auto")
        try:
            # the array's chunk reader, directly: through BFOmeZarrStore.get is ~25% slower
            return store._get_array_store(*key)._read_chunk(coords)
        finally:
            self._idle.put(store)

    def _get_metadata(self, key, prototype):
        # BFOmeZarrStore.get never awaits (its reads block), so it is run to completion
        # on a loop of this thread's own
        with self._read_lock:
            return asyncio.run(super().get(key, prototype))


def with_zstd_codec(document: bytes) -> bytes:
    """An array's zarr.json, with the zstd codec after its "bytes" codec."""
    metadata = json.loads(document)
    metadata["codecs"].append(ZSTD_CODEC)
    return json.dumps(metadata).encode()


class ImageRouter:
    """ASGI app that sends each request for /<image file>/<key> to that image's Zarr store app,
    opening the file with Bio-Formats on its first request."""

    def __init__(self, root: pathlib.Path, *, max_open: int, readers: int):
        self.root = root.resolve()
        self.max_open = max_open
        self.readers = readers
        self.open_images: OrderedDict[str, Starlette] = OrderedDict()  # least -> most recently used
        self._opening: dict[str, asyncio.Task] = {}  # images being opened, shared by their requests
        self._rejected: set[str] = set()  # files Bio-Formats can't read, not tried again

    def parse_request_path(self, url_path: str) -> ImageRequest | None:
        """Split a URL path at the file it names under the root:
        '/a/image.czi.zarr/0/0/c/0/0/0/1/2' -> ImageRequest('a/image.czi', 'a/image.czi.zarr', '0/0/c/0/0/0/1/2').
        None if no prefix of the path is a file under the root."""
        parts = url_path.strip("/").split("/")
        if any(p in ("", ".", "..") for p in parts):
            return None
        for i in range(1, len(parts) + 1):
            prefix = "/".join(parts[:i])
            image = prefix.removesuffix(ZARR_URL_SUFFIX)
            path = self.root / image
            if image in self.open_images or path.is_file():
                if image not in self.open_images and not path.resolve().is_relative_to(self.root):
                    return None  # a symlink out of the root (open images were checked)
                return ImageRequest(image, prefix, "/".join(parts[i:]))
            if not (self.root / prefix).is_dir():
                return None
        return None

    def make_image_app(self, image: str) -> Starlette | None:
        """The zarr-http-server app serving the image file, or None if it is a format
        Bio-Formats doesn't read. Blocking: opening the file parses its metadata and can
        take seconds."""
        path = (self.root / image).resolve()
        log.info("opening %s", path)
        try:
            biofile = BioFile(path).open()
        except scyjava.jimport("loci.formats.UnknownFormatException"):
            return None
        # closed by its finalizer once nothing uses it, i.e. after eviction and the
        # last request using it
        return store_app(TiledStore(biofile, self.readers))

    async def get_image_app(self, image: str) -> Starlette | None:
        """The image's app: the open one, or opened now; None if Bio-Formats can't read
        it. Concurrent requests for an image that is being opened wait for the same open."""
        if image in self.open_images:
            self.open_images.move_to_end(image)
            return self.open_images[image]
        if image in self._rejected:
            return None
        if image not in self._opening:
            # a task of its own, so a client disconnecting mid-open doesn't cancel it for the rest
            self._opening[image] = asyncio.create_task(self._open_image(image))
        return await asyncio.shield(self._opening[image])

    async def _open_image(self, image: str) -> Starlette | None:
        """Make the image's app in a worker thread, then keep it open, evicting the least
        recently used images beyond max_open."""
        try:
            app = await asyncio.to_thread(self.make_image_app, image)
        finally:
            del self._opening[image]  # a failed open is tried again on the next request
        if app is None:
            log.info("not a format Bio-Formats reads: %s", image)
            self._rejected.add(image)  # the files are assumed never to change
            return None
        self.open_images[image] = app
        while len(self.open_images) > self.max_open:
            log.info("evicting %s", self.open_images.popitem(last=False)[0])
        return app

    async def __call__(self, scope, receive, send):
        # the path below this app's mount point (the route's {path}), not scope["path"],
        # which also holds root_path when the app is mounted under a prefix
        request = self.parse_request_path(scope["path_params"]["path"])
        if request is None:
            return await PlainTextResponse("Not found", 404)(scope, receive, send)
        try:
            app = await self.get_image_app(request.image)
        except Exception as exc:
            log.warning("could not open %s: %s", request.image, exc)
            return await PlainTextResponse(f"Could not open {request.image}", 500)(scope, receive, send)
        if app is None:  # not a format Bio-Formats reads: answered like a missing file
            return await PlainTextResponse("Not found", 404)(scope, receive, send)
        # pass the request on as if the image's app were mounted at /<prefix>: Starlette
        # routes on scope["path"] with root_path removed, which leaves /<key>
        mount = scope.get("root_path", "") + "/" + request.prefix
        await app(dict(scope, root_path=mount), receive, send)


def build_app(root: pathlib.Path, *, max_open: int, readers: int, max_reads: int,
              status: bool, gc_interval: float = 300) -> Starlette:
    """The server's ASGI app: the image router, /_status, and CORS for browser clients.

    Reads run in the event loop's default thread pool, sized to max_reads at startup.
    Every gc_interval seconds (0: never) a full Python garbage collection frees
    reference cycles. Startup runs only when this is the top-level app: an app that
    mounts this one must size its own loop's pool and collect garbage itself."""
    router = ImageRouter(root, max_open=max_open, readers=readers)

    async def collect_garbage():
        # With JPype's hook removed (see decouple_jpype_gc), reference cycles are left to
        # Python's own collector, which on 3.14 can take long to reach them; a cycle
        # holding a reader would keep its file open. A full collection takes ~20 ms.
        while True:
            await asyncio.sleep(gc_interval)
            gc.collect()

    @contextlib.asynccontextmanager
    async def lifespan(app):
        asyncio.get_running_loop().set_default_executor(
            ThreadPoolExecutor(max_reads, thread_name_prefix="read"))
        collector = asyncio.create_task(collect_garbage()) if gc_interval > 0 else None
        yield
        if collector is not None:
            collector.cancel()

    async def status_page(request):
        return JSONResponse({"root": str(router.root),
                             "max_open": router.max_open,
                             "readers": router.readers,
                             "max_reads": max_reads,
                             "bioformats": BioFile.bioformats_version(),
                             "open": list(router.open_images)})  # least -> most recently used

    routes = [Route("/{path:path}", router, methods=["GET", "HEAD"])]
    if status:
        routes.insert(0, Route("/_status", status_page, methods=["GET"]))
    # CORS on the outer app, so preflights and 404s get the headers without opening an image
    cors = Middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET", "HEAD"],
                      allow_headers=["Range"], expose_headers=["Content-Range"])
    return Starlette(routes=routes, middleware=[cors], lifespan=lifespan)


@click.command(context_settings={"auto_envvar_prefix": "BFFILE_SERVE", "show_default": True})
@click.argument("root", envvar="BFFILE_SERVE_ROOT",
                type=click.Path(exists=True, file_okay=False, path_type=pathlib.Path))
@click.option("-p", "--port", default=9876, show_envvar=True)
@click.option("--host", default="0.0.0.0", show_envvar=True)
@click.option("-n", "--max-open", default=32, show_envvar=True,
              help="Images kept open at once (least recently used are evicted).")
@click.option("-r", "--readers", default=4, type=click.IntRange(min=1), show_envvar=True,
              help="Bio-Formats readers per image that read its chunks in parallel, "
                   "each with its own open file.")
@click.option("--max-reads", default=32, type=click.IntRange(min=1), show_envvar=True,
              help="Worker threads reading and compressing, across all images.")
@click.option("--status/--no-status", default=True, show_envvar=True,
              help="Serve /_status, which lists open images' file names.")
def main(root, port, host, max_open, readers, max_reads, status):
    """Serve the image files under ROOT (or $BFFILE_SERVE_ROOT) as read-only Zarr v3
    stores, read and decoded by Bio-Formats, with chunks compressed with zstd."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    # bffile's logger (bffile's and Bio-Formats' messages) has a handler of its own:
    # warnings only, and not again through the root logger
    logging.getLogger("bffile").setLevel(logging.WARNING)
    logging.getLogger("bffile").propagate = False
    # start the JVM (fetching Java and Bio-Formats on first use) now, rather than in
    # the first request
    log.info("Bio-Formats %s", BioFile.bioformats_version())
    log.info("removed %d JPype gc hook(s)", decouple_jpype_gc())
    # uvicorn re-raises the signal that stopped it after shutting down: a SIGTERM
    # (Cloud Run's stop) then exits cleanly, like Ctrl-C
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        serve(build_app(root, max_open=max_open, readers=readers, max_reads=max_reads,
                        status=status), host=host, port=port)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
