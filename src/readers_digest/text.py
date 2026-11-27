"""HTML mail and chat messages -> plain text, for the digest, the window and the review."""

import re
from html.parser import HTMLParser
from typing import ClassVar


class _TextExtractor(HTMLParser):
    _BLOCK: ClassVar = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "blockquote"}
    _SKIP: ClassVar = {"script", "style", "head"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        # Depth inside elements whose text is not shown, such as <style>.
        self.skipping = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skipping += 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self.skipping:
            self.skipping -= 1
        elif tag in self._BLOCK:
            self.out.append("\n")

    def handle_data(self, data):
        if not self.skipping:
            self.out.append(data)


def html_to_text(html: str) -> str:
    """The text a reader sees in `html`: a line per block, runs of blanks collapsed, links and images dropped."""
    p = _TextExtractor()
    p.feed(html)
    p.close()
    lines = (" ".join(line.split()) for line in "".join(p.out).splitlines())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()
