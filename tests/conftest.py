import sqlite3

import pytest


def create_fixture_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.execute("create table tiles(displayName text, appId text, cRank integer)")
    conn.execute("create table tiles_content(id integer primary key, c1 text)")
    conn.execute(
        "create virtual table synonyms using fts5(displayName UNINDEXED, rankPenalty UNINDEXED, synonym, source UNINDEXED)"
    )
    conn.executemany(
        "insert into tiles(displayName, appId, cRank) values (?, ?, ?)",
        [
            ("Google Chrome", "chrome", 10),
            ("Manual.pdf", "file:///Manual.pdf", 1),
        ],
    )
    conn.executemany(
        "insert into tiles_content(id, c1) values (?, ?)",
        [
            (1, r"C:\ProgramData\Windows\Start Menu\Programs\Google Chrome.lnk"),
            (2, "https://example.test/manual.pdf"),
        ],
    )
    conn.commit()
    conn.close()


@pytest.fixture
def fixture_db(tmp_path):
    path = tmp_path / "AppsIndex.db"
    create_fixture_db(path)
    return path
