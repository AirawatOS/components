#!/usr/bin/env python3
"""flood-expert — an AI EXPERT (bundle, INT-0002). The Expert itself does NOT reason; its member Skills do
(each: Facts × Rules → cited governance.recommendation). This entrypoint is a bundle marker: it reports the
Skills this Expert brings. Installing the Expert FANS OUT to install these Skills; each runs on its own token
+ trigger, keeping the inbox stocked with grounded, contestable flood advisories for a human authority."""

SKILLS = ["ds:airawatdeveloper:skill/flood-advisory"]


def main():
    print("Flood Expert — a bundle of Skills (INT-0002). Member skills:")
    for s in SKILLS:
        print("  -", s)
    print("Each Skill runs on its own run-token/trigger and emits a cited governance.recommendation.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
