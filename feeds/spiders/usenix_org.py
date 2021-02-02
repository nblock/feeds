import re
from base64 import b64encode
from datetime import datetime
from io import BytesIO
from zipfile import ZipFile

import scrapy
from scrapy.http import HtmlResponse

from feeds.loaders import FeedEntryItemLoader
from feeds.spiders import FeedsSpider
from feeds.utils import generate_feed_header


class UsenixOrgSpider(FeedsSpider):
    name = "usenix.org"
    start_urls = ["https://www.usenix.org/publications/login"]

    def feed_headers(self):
        return []

    def parse(self, response):
        yield generate_feed_header(
            title=";login:",
            subtitle="The Usenix Magazine",
            link=response.url,
            path="login",
        )
        yield generate_feed_header(
            title=";login:",
            subtitle="The Usenix Magazine (Articles)",
            link=response.url,
            path="login-articles",
        )

        # Only scrape the last 8 issues.
        issues = response.css(".issues .month a::attr(href)").extract()[:8]

        def _sort_key(link):
            seasons = ["spring", "summer", "fall", "winter"]
            season, year = re.search(
                r"/publications/login/(?P<season>.*)(?P<year>\d{4})",
                link,
            ).groups()
            return (year, seasons.index(season))

        issues = sorted(issues, key=_sort_key, reverse=True)

        # FIXME
        premium = False

        for issue in issues:
            yield scrapy.Request(
                response.urljoin(issue),
                self.parse_login_issue,
                meta={"premium": premium}
            )

        # Only extract the latest available issue as an epub because the feed will
        # include the images base64 encoded which results in a few MB.
        issue = issues[0] if premium else issues[4]
        yield scrapy.Request(response.urljoin(issue), self.parse_login_issue_for_epub)

    def parse_login_issue(self, response):
        remove_elems = [
            ".field-name-field-file-access",
            ".field-name-field-login-issue-file",
            ".field-name-field-product",
            ".field-commerce-price",
            ".views-field-field-file-access",
            ".view-header",
        ]
        il = FeedEntryItemLoader(
            response=response,
            base_url=f"https://www.{self.name}",
            remove_elems=remove_elems,
        )
        il.add_value("link", response.url)
        title = response.css("h1::text").extract_first().strip()
        il.add_value("title", title)
        il.add_value("updated", self._date_from_title(title))
        il.add_css("content_html", ".content-wrapper")
        il.add_value("path", "login")
        if response.css(".usenix-files-protected") and not response.meta["premium"]:
            il.add_value("category", "paywalled")
        return il.load_item()

    def parse_login_issue_for_epub(self, response):
        article_links = response.css(
            ".view-content a::attr('href'), .view-content a span::text"
        ).extract()
        article_links = dict(zip(article_links[1::2], article_links[::2]))
        epub_url = response.css(
            "#block-system-main a:contains('ePub')::attr('href')"
        ).extract_first()
        return scrapy.Request(
            epub_url, self._parse_epub, meta={"article_links": article_links}
        )

    def _parse_epub(self, response):
        def _inline_image(elem):
            image = b64encode(images[elem.attrib["src"]]).decode("ascii")
            elem.attrib["src"] = "data:image/jpg;base64," + image
            return elem

        images = {}
        articles = []

        # Extract all articles and images from the epub file.
        with ZipFile(BytesIO(response.body)) as zf:
            files = zf.infolist()

            # Read all images
            for image_file in (
                f for f in files
                if f.filename.startswith("OEBPS/images/")
                and not f.filename.endswith("/")
            ):
                images[image_file.filename.replace("OEBPS/", "")] = zf.read(image_file)

            for article_file in (
                f for f in files
                if f.filename.startswith("OEBPS/") and f.filename.endswith(".xhtml")
            ):
                articles.append(
                    (
                        datetime(*article_file.date_time),
                        zf.read(article_file),
                        article_file.filename,
                    )
                )

        replace_elems = {"img": _inline_image}
        remove_elems = ["h1", ".author small", ".chapsubtitle"]

        for date_time, article, filename in articles:
            article_response = HtmlResponse(body=article, url="https://www.usenix.org")

            il = FeedEntryItemLoader(
                response=article_response,
                remove_elems=remove_elems,
                replace_elems=replace_elems,
            )
            il.add_value("updated", date_time)

            # Handle title(s).
            titles = article_response.css("h1 ::text").extract()
            if titles:
                il.add_value("title", titles)
            else:
                fallback_title = re.match(r"OEBPS/\d{2}_(.*)\.xhtml", filename).group(1)
                il.add_value("title", fallback_title.title())
            il.add_css("title", ".chapsubtitle")

            # Handle link.
            title = article_response.css("h1.chaptitle::text").extract_first()
            subtitle = article_response.css(".chapsubtitle::text").extract_first()
            if subtitle:
                title += ": " + subtitle
            if title in response.meta["article_links"].keys():
                il.add_value(
                    "link",
                    "https://www.usenix.org{}".format(
                        response.meta["article_links"][title]
                    )
                )
            else:
                # Fallback to the epub URL with the filename appended to get unique
                # links.
                il.add_value("link", "{}#{}".format(response.url, filename))

            # Handle author name(s).
            authors = article_response.css(".author small::text").extract_first()
            if authors:
                authors = [
                    author.title()
                    for author in re.sub(r",? AND ", ", ", authors).split(", ")
                ]
            il.add_value("author_name", authors)

            il.add_value("path", "login-articles")
            il.add_css("content_html", "body")

            yield il.load_item()

    def _date_from_title(self, issue):
        """Try to guess the publication date of an issue from the title."""
        match = re.search(
            r"(?P<season>Spring|Summer|Fall|Winter) (?P<year>\d{4})", issue
        )
        if match:
            seasons = {"Winter": "1", "Spring": "4", "Summer": "7", "Fall": "10"}
            month = int(seasons[match.group("season")])
            year = int(match.group("year"))
            date = datetime(day=1, month=month, year=year)
            # Issues become free after a year which should be reflected by
            # bumping the updated date by a year as well.
            date_free = datetime(day=1, month=month, year=year + 1)
            return date_free if date_free < date.utcnow() else date
        else:
            self.logger.warning(f'Could not extract date from title "{issue}"!')
