"""Migrations: numbered, ordered, and split the way the runner expects."""

from __future__ import annotations

import re

from zep_context_agent.db import _migration_files, _split_statements


def test_migration_files_are_numbered_and_ordered():
    files = _migration_files()
    assert files, "no migrations found"
    numbers = []
    for name, path in files:
        match = re.match(r"^(\d{4})_", path.name)
        assert match, f"migration {path.name} is not numbered"
        numbers.append(int(match.group(1)))
        assert name == match.group(1)  # the runner keys on the version
    assert numbers == sorted(numbers)
    assert len(set(numbers)) == len(numbers)


def test_split_statements():
    sql = """
    CREATE TABLE a (id int);
    -- a comment line
    CREATE TABLE b (id int);;
    """
    statements = _split_statements(sql)
    assert len(statements) == 2
    assert statements[0].startswith("CREATE TABLE a")
    assert statements[1].startswith("CREATE TABLE b")


def test_migrations_cover_extensions_and_briefs():
    combined = ""
    for _, path in _migration_files():
        combined += path.read_text(encoding="utf-8")
    assert "CREATE EXTENSION IF NOT EXISTS vector" in combined
    assert "CREATE TABLE IF NOT EXISTS briefs" in combined
    assert "CREATE TABLE IF NOT EXISTS citation_index" in combined
