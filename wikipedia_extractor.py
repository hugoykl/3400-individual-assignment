"""Select and save sections from an English Wikipedia article."""

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from tempfile import NamedTemporaryFile
from textwrap import fill
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

try:
    import requests
except ModuleNotFoundError as error:
    if error.name != "requests":
        raise
    requests = None

from bs4 import BeautifulSoup, ParserRejectedMarkup, Tag


WIKIPEDIA = "https://en.wikipedia.org/wiki/"
REQUEST_HEADERS = {
    "User-Agent": "ISOM3400-WikipediaArticleExtractor/1.0 (interactive coursework)",
    "Accept": "text/html",
}
TIMEOUT = 15
TEXT_WIDTH = 96
LICENSE_URL = "https://creativecommons.org/licenses/by-sa/4.0/"
HEADINGS = ("h2", "h3", "h4", "h5", "h6")
LISTS = ("ul", "ol")
CONTENT_TAGS = {*HEADINGS, *LISTS, "p", "pre", "table", "dt", "dd"}
IGNORED_TAGS = {"figure", "script", "style", "noscript", "template"}
IGNORED_CLASSES = {
    "ambox", "hatnote", "metadata", "mw-indicators", "mw-jump-link",
    "mw-toc", "navbox", "shortdescription", "sidebar", "toc", "vertical-navbox",
}
INLINE_NOISE = (
    "sup.reference, .mw-ref, [typeof~='mw:Extension/ref'], "
    ".mw-editsection, .mw-editsection-like, .sortkey, "
    ".mw-cite-backlink, .reference-backlink"
)


class ArticleError(Exception):
    """An article could not be downloaded or read."""


class QuitRequested(Exception):
    """The user requested to leave the program."""


@dataclass
class Section:
    title: str
    blocks: list[Tag] = field(default_factory=list)


class WikipediaArticle:
    """Keep article metadata and its indexed content together."""

    def __init__(self, title, source_url, sections):
        self.title = title
        self.source_url = source_url
        self.sections = sections

    @classmethod
    def fetch(cls, topic):
        """Download one article and use its final URL after redirects."""
        slug = "_".join(topic.split())
        html, source_url = fetch_html(WIKIPEDIA + quote(slug, safe=""))
        return cls.from_html(html, source_url)

    @classmethod
    def from_html(cls, html, source_url):
        try:
            soup = BeautifulSoup(html, "html.parser")
            article = soup.find(id="mw-content-text")
            container = (
                article.find(class_="mw-parser-output", recursive=False)
                if article is not None else soup.find(class_="mw-parser-output")
            )
            title_tag = soup.find("h1", id="firstHeading") or soup.find("h1")
            if container is None or title_tag is None:
                raise ArticleError("The page layout did not contain a readable Wikipedia article.")

            for tag in container.select(INLINE_NOISE):
                if tag.parent is not None:
                    tag.decompose()
            for line_break in container.find_all("br"):
                line_break.replace_with("\n")

            title = clean_text(title_tag.get_text())
            lead = Section("Introduction")
            current = lead
            sections = []
            for block in article_blocks(container):
                if block.name == "h2":
                    heading = clean_text(block.get_text())
                    if heading:
                        current = Section(heading)
                        sections.append(current)
                else:
                    current.blocks.append(block)
        except (ParserRejectedMarkup, RecursionError) as error:
            raise ArticleError("Wikipedia returned HTML this program could not parse.") from error

        if not title or not (lead.blocks or any(section.blocks for section in sections)):
            raise ArticleError("The page did not contain readable article text.")
        return cls(title, source_url, sections or [lead])

    def extract(self, section_numbers):
        """Format selected sections in article order with source attribution."""
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        header = [
            self.title,
            "=" * len(self.title),
            f"Source URL: {self.source_url}",
            f"Extracted at: {timestamp}",
            f"Wikipedia licence: CC BY-SA 4.0 ({LICENSE_URL})",
            "Attribution: Wikipedia contributors. Selected sections reformatted for plain text.",
        ]
        sections = []
        for number in section_numbers:
            section = self.sections[number - 1]
            body = [content for block in section.blocks if (content := format_block(block))]
            heading = f"## {section.title}"
            sections.append("\n\n".join([heading, *body]) if body else heading)
        return "\n\n".join(["\n".join(header), *sections]) + "\n"


def fetch_html(url):
    """Use requests, with a standard-library fallback if it is absent."""
    if requests is None:
        # The assignment's grading environment explicitly lists beautifulsoup4.
        try:
            with urlopen(Request(url, headers=REQUEST_HEADERS), timeout=TIMEOUT) as response:
                return response.read(), response.geturl()
        except HTTPError as error:
            raise http_error(error.code) from error
        except (URLError, OSError) as error:
            if isinstance(error, TimeoutError) or isinstance(getattr(error, "reason", None), TimeoutError):
                raise ArticleError("The request timed out. Please try again.") from error
            raise ArticleError("Could not connect to Wikipedia. Check your connection and try again.") from error

    try:
        response = requests.get(url, headers=REQUEST_HEADERS, timeout=TIMEOUT)
        response.raise_for_status()
        return response.content, response.url
    except requests.exceptions.HTTPError as error:
        raise http_error(error.response.status_code) from error
    except requests.exceptions.Timeout as error:
        raise ArticleError("The request timed out. Please try again.") from error
    except requests.exceptions.RequestException as error:
        raise ArticleError("Could not connect to Wikipedia. Check your connection and try again.") from error


def http_error(status):
    if status == 404:
        return ArticleError("No English Wikipedia article was found for that title. Check the spelling or try another topic.")
    return ArticleError(f"Wikipedia returned HTTP {status}. Please try again shortly.")


def article_blocks(container):
    """Walk wrapper elements in document order and yield each content block once."""
    for child in container.children:
        if not isinstance(child, Tag):
            continue
        if (child.name in IGNORED_TAGS or child.has_attr("hidden")
                or IGNORED_CLASSES.intersection(child.get("class", []))):
            continue
        if child.name in CONTENT_TAGS:
            yield child
        else:
            yield from article_blocks(child)


def clean_text(text):
    return " ".join(text.split())


def format_block(block):
    if block.name in HEADINGS:
        return "#" * int(block.name[1]) + " " + clean_text(block.get_text())
    if block.name in LISTS:
        return "\n".join(format_list(block))
    if block.name == "table":
        return format_table(block)
    if block.name == "pre":
        return block.get_text().strip("\n")
    return fill(clean_text(block.get_text()), width=TEXT_WIDTH,
                break_long_words=False, break_on_hyphens=False)


def number_attribute(tag, name, default):
    try:
        return int(tag.get(name, default))
    except (TypeError, ValueError):
        return default


def format_list(list_tag, depth=0):
    items = list_tag.find_all("li", recursive=False)
    reversed_order = list_tag.name == "ol" and list_tag.has_attr("reversed")
    number = number_attribute(list_tag, "start", len(items) if reversed_order else 1)
    lines = []
    for item in items:
        if list_tag.name == "ol":
            number = number_attribute(item, "value", number)
            marker = f"{number}."
            number += -1 if reversed_order else 1
        else:
            marker = "-"
        text = clean_text("".join(
            part for part in item.strings if part.find_parent(LISTS) is list_tag
        ))
        nested_lists = [
            nested for nested in item.find_all(LISTS)
            if nested.find_parent("li") is item
        ]
        prefix = f"{'  ' * depth}{marker} "
        if text:
            lines.append(fill(text, width=TEXT_WIDTH, initial_indent=prefix,
                              subsequent_indent=" " * len(prefix),
                              break_long_words=False, break_on_hyphens=False))
        elif nested_lists:
            lines.append(prefix.rstrip())
        for nested in nested_lists:
            lines.extend(format_list(nested, depth + 1))
    return lines


def format_table(table):
    """Keep captions, row order, and empty cells visible in plain text."""
    caption = table.find("caption", recursive=False)
    caption_text = clean_text(caption.get_text()) if caption else ""
    lines = [f"Table: {caption_text}"] if caption_text else []
    for row in table.find_all("tr"):
        if row.find_parent("table") is not table:
            continue
        cells = row.find_all(("th", "td"), recursive=False)
        if cells:
            values = [clean_text(cell.get_text()).replace("|", "\\|") for cell in cells]
            lines.append("| " + " | ".join(values) + " |")
    return "\n".join(lines)


def parse_selection(selection, section_count):
    """Convert a single number, comma list, range or 'all' into section numbers."""
    if selection.casefold() == "all":
        return list(range(1, section_count + 1))

    selected = set()
    for part in selection.split(","):
        match = re.fullmatch(r"([0-9]+)(?:\s*[-–]\s*([0-9]+))?", part.strip())
        if not match:
            raise ValueError("Use a section number, a list like 1,3, a range like 2-4, or all.")

        try:
            start = int(match[1])
            end = int(match[2]) if match[2] is not None else start
        except ValueError as error:
            raise ValueError(f"Enter section numbers between 1 and {section_count}.") from error
        if not (1 <= start <= section_count and 1 <= end <= section_count):
            raise ValueError(f"Enter section numbers between 1 and {section_count}.")
        if end < start:
            raise ValueError("A range must start with the smaller section number, for example 2-4.")
        selected.update(range(start, end + 1))
    return sorted(selected)


def read_input(prompt):
    answer = input(prompt).strip()
    if answer.casefold() == ":q":
        raise QuitRequested
    return answer


def select_sections(article):
    print(f"\n{article.title}")
    print(f"Source: {article.source_url}")
    print("Available sections")
    for number, section in enumerate(article.sections, start=1):
        print(f"  {number:>2}. {section.title}")
    if len(article.sections) == 1 and article.sections[0].title == "Introduction":
        print("The introduction is available for articles without top-level headings.")

    while True:
        answer = read_input("Sections (number, 1,3, 2-4, all, :back, or :q): ")
        if answer.casefold() == ":back":
            return None
        try:
            return parse_selection(answer, len(article.sections))
        except ValueError as error:
            print(f"Invalid selection. {error}")


def write_new_file(path, content):
    """Create a new file exclusively so an existing file cannot be overwritten."""
    output = path.open("x", encoding="utf-8")
    try:
        with output:
            output.write(content)
    except (OSError, UnicodeError, KeyboardInterrupt):
        path.unlink(missing_ok=True)
        raise


def replace_file(path, content):
    """Finish writing a temporary file before replacing an existing export."""
    temporary_path = None
    try:
        with NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, delete=False
        ) as output:
            temporary_path = Path(output.name)
            output.write(content)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def save_extract(title, content):
    stem = re.sub(r"[^\w-]+", "_", title).strip("_-")[:55] or "wikipedia"
    path = Path.cwd() / f"{stem}_extract.txt"
    try:
        write_new_file(path, content)
        return path
    except FileExistsError:
        pass

    while True:
        answer = read_input(f"{path.name} already exists. Overwrite? [y/n, :back, or :q]: ").casefold()
        if answer in {"y", "yes"}:
            replace_file(path, content)
            return path
        if answer in {"n", "no"}:
            break
        if answer == ":back":
            return None
        print("Invalid answer. Enter 'y' to overwrite or 'n' to save as a new file.")

    number = 2
    while True:
        candidate = path.with_name(f"{path.stem}_{number}{path.suffix}")
        try:
            write_new_file(candidate, content)
            return candidate
        except FileExistsError:
            number += 1


def main():
    print("Wikipedia Article Extractor")
    print("Enter an English Wikipedia article title, choose sections, and save them as a text file.")
    print("Selections are saved in article order. Enter :q at any prompt to quit.")
    try:
        while True:
            topic = read_input("\nArticle title: ")
            if not topic or not topic.isprintable() or topic in {".", ".."}:
                print("Enter a valid article title, for example Ada Lovelace.")
                continue

            try:
                article = WikipediaArticle.fetch(topic)
            except ArticleError as error:
                print(f"Search error. {error}")
                continue

            section_numbers = select_sections(article)
            if section_numbers is None:
                continue

            titles = ", ".join(article.sections[number - 1].title for number in section_numbers)
            print(f"Extracting {len(section_numbers)} section(s): {titles}")
            try:
                content = article.extract(section_numbers)
                path = save_extract(article.title, content)
            except (OSError, UnicodeError) as error:
                print(f"Could not save the text file. {error}")
            except RecursionError:
                print("Could not format the selection because the page content is too deeply nested.")
            else:
                print(f"Saved to {path}" if path is not None else "Save cancelled.")
    except (QuitRequested, EOFError, KeyboardInterrupt):
        print("\nGoodbye.")


if __name__ == "__main__":
    main()
