import csv
import datetime
import re
import time
import urllib.parse

import backoff
import dateutil.parser
import numpy as np
import requests
from bs4 import BeautifulSoup
from bs4 import Tag

from .config import BASE_URL
from .config import BOOK_URL
from .config import IGNORE_GENRE_SUBSTRINGS
from .config import IGNORE_GENRES
from .config import REVIEW_URL
from .config import STANDARD_FIELDNAMES
from .config import STATS_URL
from .entities import AbsoluteUrl
from .entities import CaptchaSolver
from .entities import EnhanceExportException
from .entities import Path
from .login import login


def parse_csv(filename: Path) -> list[dict[str, str]]:
    try:
        with open(filename, newline="", encoding="utf-8") as file:
            reader = csv.DictReader(file)
            if reader.fieldnames is None:
                raise ValueError("Could not read csv column names")
            if set(reader.fieldnames) < set(STANDARD_FIELDNAMES):
                raise ValueError("CSV file does not contain the standard fieldnames!")
            return list(reader)
    except (ValueError, csv.Error, OSError) as e:
        raise EnhanceExportException(f"Error reading export file: {e}")


def write_csv(data: list[dict], fieldnames: list[str], filename: Path) -> None:
    try:
        with open(filename, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=fieldnames,
                delimiter=",",
                quotechar='"',
                quoting=csv.QUOTE_MINIMAL,
            )
            writer.writeheader()
            writer.writerows(data)
    except (OSError, csv.Error) as e:
        raise EnhanceExportException(f"Error writing export file: {e}")


@backoff.on_exception(
    backoff.expo, requests.exceptions.RequestException, max_tries=3, max_time=2
)
def get_with_retry(session, *args, **kwargs) -> requests.Response:
    resp = session.get(*args, timeout=10, **kwargs)
    resp.raise_for_status()
    return resp


def make_book_url(book_id) -> AbsoluteUrl:
    return AbsoluteUrl(BOOK_URL.format(book_id=book_id))


def make_review_url(book_id: str) -> AbsoluteUrl:
    return AbsoluteUrl(REVIEW_URL.format(book_id=book_id))


def make_stats_url(book_id: str) -> AbsoluteUrl:
    return AbsoluteUrl(STATS_URL.format(book_id=book_id))


def get_read_dates(
    soup: BeautifulSoup,
) -> list[tuple[datetime.datetime | None, datetime.datetime]]:
    readings = []
    for row in soup.select(".readingSessionRow"):
        start_date, end_date = tuple(
            dateutil.parser.parse(date_str, default=datetime.datetime(1900, 1, 1))
            if (
                date_str := "".join(
                    inputs[0].text
                    if (
                        inputs := row.select(
                            f".{start_end}{date_part} .setDate[selected='selected']"
                        )
                    )
                    else ""
                    for date_part in [
                        "Day",
                        "Month",
                        "Year",
                    ]
                )
            )
            else None
            for start_end in ["start", "end"]
        )
        if end_date is not None:
            readings.append((start_date, end_date))
    return readings


def valid_genre(genre: str) -> bool:
    if genre in IGNORE_GENRES or any((s in genre) for s in IGNORE_GENRE_SUBSTRINGS):
        return False
    if genre.isnumeric():
        return False
    return True


def get_genres(soup: BeautifulSoup) -> list[tuple[list[str], int]]:
    genrelinks = soup.find_all(class_="shelfStat")
    genres = []
    genre: list[str] = []
    for genre_link in genrelinks:
        lines = [l.strip() for l in genre_link.get_text().split("\n") if l.strip()]
        if lines[0] in IGNORE_GENRES:
            continue
        if len(lines) == 2:
            genres.append(
                (lines[0].strip(), int("".join(c for c in lines[1] if c.isdigit())))
            )
    genres.sort(key=lambda x: x[1], reverse=True)
    # format genre name
    genres = [(g[0].replace("-", " ").title(), g[1]) for g in genres]

    # filter out useless shelves (e.g. to-read) and ones with less than 10 people
    genres = [g for g in genres if g[1] >= 10 and valid_genre(g[0])]

    # genres used to support nested subgenres, this doesn't exist on the new book page.
    # To match the old format, treat all genres as 1 level (wrap name in list)
    genres = [([g[0]], g[1]) for g in genres]

    return genres[:20]


@backoff.on_exception(backoff.expo, Exception, max_tries=3, max_time=2)
def update_book_data(book: dict[str, str], session: requests.Session) -> None:
    book_id = book["Book Id"]

    review_page = get_with_retry(session, make_review_url(book_id))
    review_soup = BeautifulSoup(review_page.content, "html.parser")
    read_dates = get_read_dates(review_soup)
    book["read_dates"] = ";".join(
        ",".join(d.strftime("%Y-%m-%d") if d else "" for d in reading)
        for reading in read_dates
    )

    book_page = get_with_retry(session, make_book_url(book_id)).content.decode("utf-8")
    n_ratings_match = re.search(
        r'(?:"|&quot;)ratingsCount(?:"|&quot;)\s*:\s*(\d+)', book_page
    )
    if n_ratings_match is None:
        print(book_page)
        print("Did not find number of ratings in book page, not adding number of ratings!")
        book["n_ratings"] = np.nan
    else:
        book['n_ratings'] = n_ratings_match.group(1)

    shelves_url_match = re.search(
        '(?:"|&quot;)[^"&]*(work/shelves[^"&]+)(?:"|&quot;)', book_page
    )
    if shelves_url_match is None:
        print("Did not find link to shelves page on book page, not adding genres!")
        return
    shelves_url = AbsoluteUrl(f"{BASE_URL}/{shelves_url_match.group(1)}")

    genres_page = get_with_retry(session, shelves_url)
    genres_soup = BeautifulSoup(genres_page.content, "html.parser")
    genres = get_genres(genres_soup)
    book["genres"] = ";".join(f"{','.join(genre[0])}|{genre[1]}" for genre in genres)


def enhance_export(options: dict, captcha_solver: CaptchaSolver | None = None) -> None:
    books = parse_csv(options["csv"])
    input_columns = list(books[0].keys())
    output_columns = input_columns + [
        c for c in ["read_dates", "genres", "n_ratings"] if not c in input_columns
    ]

    session = login(
        options["email"], options["password"], captcha_solver=captcha_solver
    )
    if options["update"]:
        old_books_by_id = {b["Book Id"]: b for b in parse_csv(options["update"])}
        for b in books:
            # Update read_dates and genres from the old file for books that didn't change shelf and weren't re-read.
            ob = old_books_by_id.get(b["Book Id"], None)
            if (
                ob
                and ob["Exclusive Shelf"] == b["Exclusive Shelf"]
                and ob["Date Read"] == b["Date Read"]
            ):
                b["read_dates"] = ob.get("read_dates", b.get("read_dates", ""))
                b["genres"] = ob.get("genres", b.get("genres", ""))
                b["n_ratings"] = ob.get("n_ratings", b.get("n_ratings", ""))

    books_to_process = [
        b
        for b in books
        if (
            options["force"]
            or (
                not b.get("genres", None)
                and not b.get("read_dates", None)
                and not b.get("n_reviews", None)
            )
        )
    ]
    for i, book in enumerate(books_to_process):
        print(
            f"Book {i+1} of {len(books_to_process)}: {book['Title']} ({book['Author']})"
        )
        update_book_data(book, session)

        if i % 20 == 19 or i == len(books_to_process) - 1:
            print("saving csv")
            write_csv(books, output_columns, options["csv"])
    print("Finished processing!")
