"""Search-engine module: finds payer policy-document links independently of the crawler.

Sources: a local SearXNG instance (keyword search, DuckDuckGo engine only) and the public web-archive
URL indexes (Common Crawl, Internet Archive). Outputs are written to search_engine/output/ and are never
merged into the crawler's own output.
"""
