# bffile-server

`serve_bffile_zstd.py` serves every image file under a directory as a read-only
Zarr v3 store over HTTP.

It uses [bffile](https://github.com/imaging-formats/bffile), which wraps
[Bio-Formats](https://www.openmicroscopy.org/bio-formats/), to read image files in
any of the ~150 formats Bio-Formats supports (SVS, NDPI, OME-TIFF, QPTIFF, CZI, LIF,
ND2, ...). bffile presents each file as an OME-Zarr (NGFF v0.5) hierarchy:
bioformats2raw layout, one multiscales group per series. The script serves that
hierarchy with [zarr-http-server](https://github.com/zarr-developers/zarr-python/tree/main/packages/zarr-http-server).

Each chunk is decoded by Bio-Formats on request and compressed with zstd (level 1),
zarr-python's default codec for Zarr v3. Clients need no extra codecs: zarr-python,
zarrita.js and vizarr read it as is. Compared with sending decoded chunks
uncompressed, zstd costs 5-10% of throughput on a fast local network but sends about
4x fewer bytes, so it delivers about 5x the pixels per second at 25-50 Mbit/s.

This script started as a hand-rolled proof of concept that was further developed
and tested with use of Claude Opus 5.5.

## Running

The script declares its own dependencies, so it runs with
[uv](https://docs.astral.sh/uv/). It pins bffile to a git commit, until a release
has `tile_size="auto"` and keeps RGB samples in one chunk (bffile#75); uv needs `git`
to install it. On first
use, bffile (through scyjava) downloads a Java runtime and the Bio-Formats jars;
they are cached for later runs.

```sh
uv run serve_bffile_zstd.py /path/to/images
```

The server listens on port 9876 by default. Run with `--help` for all options.

## URLs

For an image at `/path/to/images/a/image.svs`:

```
GET /a/image.svs/zarr.json                   root group metadata
GET /a/image.svs/0/zarr.json                 series 0: multiscales metadata
GET /a/image.svs/0/0/c/0/0/0/1/2             one chunk of series 0, resolution 0
GET /a/image.svs/OME/METADATA.ome.xml        the file's OME-XML metadata
GET /a/image.svs.zarr/...                    the same store, for clients that need a .zarr URL
GET /_status                                 the images currently open
```

A file that Bio-Formats cannot read returns 404, the same as a missing file. A
multi-file format (such as .vsi with its folder) is served at its main file.

## How it reads

- **Chunks** use bffile's `tile_size="auto"`, since Bio-Formats decodes whole
  native tiles whatever region is asked for: a multiple of each array's native tile
  size, up to 512 pixels a side (480x480 for an SVS's 240x240 tiles). Readers
  without native tiles (such as ND2 or PNG) can decode the whole plane for any
  region, so their chunks are whole planes. T, C and Z are chunked by 1; RGB samples
  are served as channels.
- **Readers**: a Bio-Formats reader reads one chunk at a time, so each open image
  has one reader for metadata and `--readers` more, each with its own open file, that
  read its chunks in parallel (4 readers: about 3-4x the chunks per second of one).
  Requests waiting for an image's readers don't hold a worker thread, so they don't
  hold up other images. Up to `--max-open` images are kept open, least recently used
  are closed first, so up to `--max-open` x (`--readers` + 1) files are open at once.
- **Garbage collection**: JPype, which bffile uses to call Java, runs a full Python
  garbage collection after every Java one, which stalls every read. The server
  removes that coupling (3-4x the chunks per second) and runs a full Python
  collection every 5 minutes instead (`gc_interval` in `build_app`). See
  [jpype#1197](https://github.com/jpype-project/jpype/issues/1197),
  [jpype#1506](https://github.com/jpype-project/jpype/issues/1506) and
  [bffile#78](https://github.com/imaging-formats/bffile/pull/78).

## Options

| Option | Default | Description |
| --- | --- | --- |
| `ROOT` | | Directory of image files to serve |
| `-p`, `--port` | 9876 | Port to listen on |
| `--host` | 0.0.0.0 | Address to listen on |
| `-n`, `--max-open` | 32 | Images kept open at once; least recently used are closed first |
| `-r`, `--readers` | 4 | Bio-Formats readers per image that read its chunks in parallel |
| `--max-reads` | 32 | Worker threads reading and compressing, across all images |
| `--status/--no-status` | `--status` | Serve `/_status` |

Each option can also be set with an environment variable named `BFFILE_SERVE_<OPTION>`,
for example `BFFILE_SERVE_ROOT` or `BFFILE_SERVE_READERS`. A command-line flag takes
precedence.

Everything bffile writes is served, including `OME/METADATA.ome.xml`, the file's
OME-XML metadata. Keep the server private, like the image files themselves.

## Docker

The `Dockerfile` builds an image for Google Cloud Run. bffile is installed from git,
and Java and the Bio-Formats jars are downloaded, in a build stage; only the
resulting environment and caches are copied into the final image, so a cold start
downloads nothing. Bio-Formats is pinned to 8.5.0 (`BIOFORMATS_VERSION`). The image
expects the images mounted at `/images`, keeps at most 12 images open
(`BFFILE_SERVE_MAX_OPEN`), and listens on `$PORT` (8080 by default).

For 5-10 concurrent users, CPU and memory measured locally suggest 4 vCPU and
8 GiB, a single instance (min-instances = max-instances = 1, since each instance has
its own open images and readers) with CPU always allocated, and a request concurrency
of at least 200 (each Zarr client fetches about 10 chunks at once).

## License

The code in this repository is licensed under the MIT License (see `LICENSE`).

It does not include its dependencies, which are installed when the script runs or the
image is built, each under its own license. Notably, bffile is BSD-3-Clause, but by
default downloads Bio-Formats' `ome:formats-gpl` jars, licensed under the GPL v2 or
later; this server needs them, since the readers for SVS, NDPI, CZI, LIF and ND2 are
among them. The Docker image built from the `Dockerfile` therefore contains
GPL-licensed software (Bio-Formats 8.5.0, and the Zulu OpenJDK runtime, GPLv2 with
the Classpath Exception). Running the image is fine; but if you distribute it, for example in a public registry, you must comply with those licenses.
