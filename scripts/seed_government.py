"""Queue the government portals people actually ask about.

The index held 146,000 pages and not one page of pmkisan.gov.in. District
`.nic.in` sites fill the results for questions about central schemes, because
they mention the scheme in passing while the scheme's own site was never
queued.

    python scripts/seed_government.py            # queue them
    python scripts/seed_government.py --dry-run  # show what would be queued

The crawler picks the rows up on its next pass. Each domain is queued at its
front door; link discovery takes it from there, provided the domain is in
WHITELISTED_DOMAINS or ALLOW_GLOBAL_DOMAINS is on.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agents import load_agent  # noqa: E402
from app.engine.config import get_settings  # noqa: E402
from app.libsql_http import build_client  # noqa: E402

normalise_url = load_agent("01_frontier_agent").normalise_url

#: Portals whose absence is felt as a wrong answer rather than a missing one.
#: Every one of these answers a question an Indian reader actually asks.
GOVERNMENT_SITES: tuple[str, ...] = (
    "https://pmkisan.gov.in/",
    "https://www.india.gov.in/",
    "https://uidai.gov.in/",
    "https://www.incometax.gov.in/",
    "https://www.gst.gov.in/",
    "https://www.epfindia.gov.in/",
    "https://www.passportindia.gov.in/",
    "https://parivahan.gov.in/",
    "https://www.nvsp.in/",
    "https://www.mca.gov.in/",
    "https://www.rbi.org.in/",
    "https://www.sebi.gov.in/",
    "https://nrega.nic.in/",
    "https://www.nhm.gov.in/",
    "https://ayushmanbharat.gov.in/",
    "https://www.myscheme.gov.in/",
    "https://scholarships.gov.in/",
    "https://www.nsp.gov.in/",
    "https://cowin.gov.in/",
    "https://www.digilocker.gov.in/",
    "https://karnataka.gov.in/",
    "https://www.tn.gov.in/",
    "https://kerala.gov.in/",
    "https://www.telangana.gov.in/",
    "https://www.ap.gov.in/",
    "https://www.maharashtra.gov.in/",
    "https://www.gujarat.gov.in/",
    "https://www.rajasthan.gov.in/",
    "https://www.mp.gov.in/",
    "https://www.up.gov.in/",
    "https://www.wb.gov.in/",
    "https://www.punjab.gov.in/",
    "https://www.haryana.gov.in/",
    "https://www.bihar.gov.in/",
    "https://www.odisha.gov.in/",
    "https://www.assam.gov.in/",
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Queue the government portals")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--priority", type=int, default=0, help="0 is highest")
    args = parser.parse_args()

    client = build_client(get_settings())
    queued = 0
    try:
        for url in GOVERNMENT_SITES:
            canonical = normalise_url(url)
            domain = canonical.split("//", 1)[1].split("/", 1)[0]
            if args.dry_run:
                print(f"would queue {canonical}")
                queued += 1
                continue
            result = await client.execute(
                "INSERT INTO crawl_queue (url, domain, status, priority, retry_count) "
                "VALUES (?, ?, 'pending', ?, 0) "
                "ON CONFLICT (url) DO UPDATE SET status = 'pending', priority = ?, "
                "  retry_count = 0, last_error = NULL",
                (canonical, domain, args.priority, args.priority),
            )
            queued += int(getattr(result, "rows_affected", 0) or 0)
            print(f"queued {canonical}")
    finally:
        await client.close()

    print(f"\n{queued} rows {'would be ' if args.dry_run else ''}queued")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
