# Copyright (C) 2026 James Hickman
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

"""Plugin interface + result types."""
from __future__ import annotations

from abc import ABC, abstractmethod
import os
from dataclasses import dataclass, field
from typing import Callable, Iterator, List, Optional


#: Bytes read from a file-backed rendition at a time.
RENDITION_CHUNK = 4 * 1024 * 1024


@dataclass
class Rendition:
    """One alternate-format copy of a source file, to be stored as a hidden child.

    A rendition carries its payload **either** in memory (``data``) or on disk
    (``path``). Small outputs — a thumbnail, a poster frame — are fine in
    memory. Large ones — a converted PDF, an XKT for a big model, a video
    preview — should stay on disk: the converter already wrote a file, and
    reading it back into bytes only to hand it to a streaming writer means
    holding the whole thing for no reason.

    **Ownership.** A file-backed rendition owns its file. The converter's own
    temp dir is gone by the time anyone reads it (``tools.workdir`` cleans up on
    exit), so :func:`tools.detach` moves the output somewhere the rendition
    controls and gives it a ``cleanup``. Whoever consumes a
    :class:`ConversionResult` must ``close()`` it — the pipeline does this in a
    ``finally`` — or the files leak. That is the cost of not copying them.
    """
    fmt: str    # logical kind: "pdf" | "preview" | "thumbnail" | "poster"
    ext: str    # file extension: "pdf" | "png" | "webp" | "mp4"
    data: Optional[bytes] = None
    mime: str = ""
    path: Optional[str] = None
    cleanup: Optional[Callable[[], None]] = None

    @classmethod
    def from_path(cls, fmt: str, ext: str, path: str, mime: str,
                  cleanup: Optional[Callable[[], None]] = None) -> "Rendition":
        return cls(fmt=fmt, ext=ext, data=None, mime=mime, path=path, cleanup=cleanup)

    @property
    def size(self) -> int:
        if self.path is not None:
            try:
                return os.path.getsize(self.path)
            except OSError:
                return 0
        return len(self.data or b"")

    def chunks(self, size: int = RENDITION_CHUNK) -> Iterator[bytes]:
        """Yield the payload in bounded pieces, from wherever it lives."""
        if self.path is not None:
            with open(self.path, "rb") as f:
                while True:
                    piece = f.read(size)
                    if not piece:
                        return
                    yield piece
        elif self.data:
            for start in range(0, len(self.data), size):
                yield self.data[start:start + size]

    def read(self) -> bytes:
        """The whole payload. For callers that genuinely need one buffer —
        which, now that the writer streams, should be tests and little else."""
        return b"".join(self.chunks())

    def release(self) -> None:
        if self.cleanup is not None:
            try:
                self.cleanup()
            finally:
                self.cleanup = None
                self.path = None


@dataclass
class ConversionResult:
    renditions: List[Rendition] = field(default_factory=list)
    markdown: Optional[str] = None   # extracted text content, or None
    supported: bool = True           # False when no plugin handled the MIME type

    def close(self) -> None:
        """Release every file-backed rendition. Safe to call twice."""
        for r in self.renditions:
            r.release()

    def __enter__(self) -> "ConversionResult":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class ConversionPlugin(ABC):
    """A converter for a family of MIME types.

    ``render`` and ``extract`` must be side-effect-free and degrade gracefully
    (return ``[]`` / ``None``) when their external tool is unavailable — the
    pipeline records partial/unsupported status rather than failing the file."""

    name: str = "plugin"

    #: Does this converter keep its own memory use bounded, whatever the input?
    #:
    #: Two ways to be able to say yes. It STREAMS — writes the bytes to a temp
    #: file and hands the path to ffmpeg or ImageMagick, so the expansion happens
    #: in a child process with its own limits and a failure takes only that
    #: process down. Or it enforces its OWN input limit, deliberately chosen for
    #: what it converts (the 3D plugin's ``threed_max_input_mb``).
    #:
    #: The converters that cannot say yes build a full object model of the
    #: document in THIS interpreter — docling, pypdf — several times the size of
    #: the file, and when that fails it kills the worker. Only those are subject
    #: to the unattended sweeps' blanket size limit.
    #:
    #: That is why this is not simply "is it big": a two-hour video and a 40 MB
    #: BIM model are the files that most need a preview, and neither is a threat
    #: to the process. A 40 MB PDF is.
    #:
    #: Declared rather than derived, unlike :meth:`extracts_text`: nothing in a
    #: plugin's shape says where its memory goes. It defaults to False so a new
    #: converter is limited until somebody says otherwise — forgetting it costs a
    #: skipped preview on a large file, while the opposite default costs the
    #: worker.
    bounds_own_memory: bool = False

    @abstractmethod
    def supports(self, mime: str) -> bool:
        ...

    def render(self, data: bytes, mime: str, name: str) -> List[Rendition]:
        """Presentation renditions for the source (default: none)."""
        return []

    def extract(self, data: bytes, mime: str, name: str) -> Optional[str]:
        """Extracted Markdown/text content for the source (default: none)."""
        return None

    def extracts_text(self) -> bool:
        """Whether this plugin yields indexable text for the types it supports.

        Derived from whether the subclass overrides :meth:`extract`, not declared
        per plugin: a flag would be one more thing a new converter could forget to
        set, and the failure would be silent in the direction that hurts — a
        document quietly absent from the index.

        The reconcile sweep needs this to tell "no text by nature" (an image, a
        video) from "text we failed to get". Both look like a document with no
        chunks; only the second is a fault, and without this the sweep would
        either re-convert every JPEG forever or keep skipping the PDF that
        actually failed.
        """
        return type(self).extract is not ConversionPlugin.extract
