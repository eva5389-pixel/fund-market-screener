#!/usr/bin/env python3
"""Credential-safe Yahoo fund history probe for GitHub Actions."""

import json
import os
import sys

from update_dashboard import fetch_yahoo_fund_closes


def main() -> int:
    identifier = os.environ.get("YAHOO_FUND_ID", "").strip()
    token = os.environ.get("APIFY_API_TOKEN", "")
    if not identifier or not token:
        print("YAHOO_FUND_ID and APIFY_API_TOKEN are required", file=sys.stderr)
        return 2
    values, provider = fetch_yahoo_fund_closes(identifier, token)
    print(json.dumps({"id": identifier, "provider": provider, "observations": len(values),
                      "first_nav": values[0], "last_nav": values[-1]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
