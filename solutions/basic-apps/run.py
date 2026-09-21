#!/usr/bin/env python3
"""Basic Apps — a composed SOLUTION bundle (INT-0003 / ADR-0035): the base-OS utility apps every tenant gets.
Members self-declare `default_install: true`, so the app-host shelves them on every tenant at login; this
bundle is the governed grouping + Marketplace entry."""
MEMBERS = [
    "ds:airawat:app/file-explorer",        # Files — personal file manager (upload / folders / download)
]


def main():
    print("Solution:", "Basic Apps")
    for m in MEMBERS:
        print("  -", m)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
