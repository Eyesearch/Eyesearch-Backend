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


#: District portals, by the name the NIC registry uses for them. A state portal
#: that answers 403 to a datacenter IP is not reachable, but its districts are
#: served from the same `.nic.in` estate that already crawls for other states —
#: Nilgiris and Tamenglong are both in the index.
#:
#: The names are the registry's, not the current official spelling: `mysore`,
#: `belgaum`, `gulbarga` and `shimoga` are how those districts are registered,
#: and `mysuru.nic.in` does not exist. A wrong guess costs one 404, not a crawl.
DISTRICT_SITES: tuple[str, ...] = (
    "https://bengaluruurban.nic.in/",
    "https://bengalurural.nic.in/",
    "https://bagalkot.nic.in/",
    "https://belgaum.nic.in/",
    "https://bellary.nic.in/",
    "https://bidar.nic.in/",
    "https://bijapur.nic.in/",
    "https://chamarajanagar.nic.in/",
    "https://chickmagalur.nic.in/",
    "https://chikkaballapur.nic.in/",
    "https://chitradurga.nic.in/",
    "https://davanagere.nic.in/",
    "https://dharwad.nic.in/",
    "https://gadag.nic.in/",
    "https://gulbarga.nic.in/",
    "https://hassan.nic.in/",
    "https://haveri.nic.in/",
    "https://kodagu.nic.in/",
    "https://kolar.nic.in/",
    "https://koppal.nic.in/",
    "https://mandya.nic.in/",
    "https://mysore.nic.in/",
    "https://raichur.nic.in/",
    "https://shimoga.nic.in/",
    "https://tumkur.nic.in/",
    "https://udupi.nic.in/",
    "https://uttarakannada.nic.in/",
    "https://yadgir.nic.in/",
)


async def main() -> int:
    parser = argparse.ArgumentParser(description="Queue the government portals")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--priority", type=int, default=0, help="0 is highest")
    parser.add_argument(
        "--districts",
        action="store_true",
        help="queue the district portals instead, for a state portal that blocks us",
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help="only report what the queue says about these domains",
    )
    args = parser.parse_args()

    sites = DISTRICT_SITES if args.districts else GOVERNMENT_SITES

    client = build_client(get_settings())
    queued = 0
    try:
        if args.report:
            result = await client.execute(
                "SELECT domain, status, left(coalesce(last_error, ''), 40) e, count(*) "
                "FROM crawl_queue WHERE domain = ANY(?) GROUP BY 1, 2, 3 ORDER BY 4 DESC",
                ([url.split("//", 1)[1].split("/", 1)[0] for url in sites],),
            )
            for row in result.rows:
                print(f"  {row[0]:32} {row[1]:10} {row[2]:42} {row[3]}")
            return 0

        for url in sites:
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

    if not args.dry_run:
        print("\nWhat the queue says about these domains now:")
        result = await client.execute(
            "SELECT domain, status, count(*) FROM crawl_queue "
            "WHERE domain = ANY(?) GROUP BY 1, 2 ORDER BY 3 DESC",
            ([url.split("//", 1)[1].split("/", 1)[0] for url in sites],),
        )
        for row in result.rows:
            print(f"  {row[0]:32} {row[1]:10} {row[2]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
