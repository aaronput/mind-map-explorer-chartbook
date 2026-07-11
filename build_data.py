"""
Build the mind-map dataset from Adam Tooze's Chartbook Substack sitemap.

Output: data.json containing threads, concepts, clusters, edges, learning_paths,
quizzes — same shape consumed by index.html (modeled on the Navnoor Bawa build).

Strategy:
  - Each /p/<slug> URL becomes a "thread" node.
  - Real titles are scraped from og:title (anonymous fetch; paywalled posts
    still expose OG metadata). Cached to title_cache.json — first run is slow
    (~50 min for ~1600 posts), incremental thereafter.
  - Concepts are extracted from real titles + slugs, mapped through a
    macro/geopolitics/energy taxonomy tuned for Tooze's beat.
  - Clusters are broad color groups for the graph.
  - Edges are weighted by Jaccard similarity over (concepts ∪ cluster).
  - Learning paths are curated reading sequences across Chartbook's themes.
  - Quizzes auto-generated, one per cluster sample.
"""

import json, os, re, time, html as html_lib
import xml.etree.ElementTree as ET
import urllib.request, urllib.error
from collections import defaultdict, Counter
from itertools import combinations
from pathlib import Path

SKIP_SCRAPE = os.environ.get("SKIP_SCRAPE") == "1"

SITEMAP_URL = "https://adamtooze.substack.com/sitemap.xml"
SITEMAP = Path("/tmp/at_sitemap.xml")
OUT     = Path(__file__).with_name("data.json")
TITLE_CACHE_FILE = Path(__file__).with_name("title_cache.json")
SCRAPE_DELAY = 1.8
USER_AGENT   = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/605.1.15 MindMapBuilder/1.0"

# ---------------------------------------------------------------------------
# 0. Ensure sitemap is on disk (refresh.sh also pulls this; safe to repeat)
# ---------------------------------------------------------------------------
if not SITEMAP.exists():
    print(f"Fetching sitemap from {SITEMAP_URL}...")
    req = urllib.request.Request(SITEMAP_URL, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as r:
        SITEMAP.write_bytes(r.read())

# ---------------------------------------------------------------------------
# 1. Parse sitemap
# ---------------------------------------------------------------------------
NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}
tree = ET.parse(SITEMAP)
posts = []
for u in tree.getroot().findall("s:url", NS):
    loc = u.find("s:loc", NS).text
    if "/p/" not in loc:
        continue
    lm = u.find("s:lastmod", NS)
    slug = loc.rsplit("/p/", 1)[1]
    posts.append({"slug": slug, "url": loc, "date": lm.text if lm is not None else ""})

posts.sort(key=lambda p: p["date"], reverse=True)
print(f"Loaded {len(posts)} posts")

# ---------------------------------------------------------------------------
# 1b. Real title/subtitle scraper (cached)
# ---------------------------------------------------------------------------
def load_title_cache():
    if TITLE_CACHE_FILE.exists():
        try:
            return json.loads(TITLE_CACHE_FILE.read_text())
        except Exception:
            return {}
    return {}

def save_title_cache(cache):
    TITLE_CACHE_FILE.write_text(json.dumps(cache, indent=2, sort_keys=True))

META_RE = {
    "og:title":       re.compile(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    "og:description": re.compile(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    "tw:title":       re.compile(r'<meta[^>]+name=["\']twitter:title["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    "tw:description": re.compile(r'<meta[^>]+name=["\']twitter:description["\'][^>]+content=["\']([^"\']+)["\']', re.I),
    "title_tag":      re.compile(r'<title[^>]*>([^<]+)</title>', re.I),
}

def _decode(s):
    return html_lib.unescape(s).strip() if s else ""

def fetch_meta(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read(200_000).decode("utf-8", errors="replace")
    title    = (META_RE["og:title"].search(body) or META_RE["tw:title"].search(body) or META_RE["title_tag"].search(body))
    subtitle = (META_RE["og:description"].search(body) or META_RE["tw:description"].search(body))
    t = _decode(title.group(1)) if title else ""
    s = _decode(subtitle.group(1)) if subtitle else ""
    # Strip trailing publication suffixes Substack sometimes appends.
    t = re.sub(r'\s*[-|·]\s*(Chartbook|Adam Tooze)\s*$', '', t).strip()
    return {"title": t, "subtitle": s}

cache = load_title_cache()
to_fetch = [] if SKIP_SCRAPE else [p for p in posts if not (cache.get(p["slug"]) or {}).get("title")]
if SKIP_SCRAPE:
    print("SKIP_SCRAPE=1 — using slug-derived titles only (no Substack fetches).")
if to_fetch:
    print(f"Scraping {len(to_fetch)} new posts (cache has {len(cache)} entries)...")
    for i, p in enumerate(to_fetch, 1):
        try:
            meta = fetch_meta(p["url"])
            cache[p["slug"]] = {**meta, "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
            if i % 25 == 0 or i == len(to_fetch):
                print(f"  [{i}/{len(to_fetch)}] {p['slug'][:50]} → {meta['title'][:60]}")
                save_title_cache(cache)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as e:
            print(f"  [{i}/{len(to_fetch)}] ✗ {p['slug']}: {e}")
            cache[p["slug"]] = {"title":"", "subtitle":"", "error":str(e), "fetched": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        time.sleep(SCRAPE_DELAY)
    save_title_cache(cache)
    print(f"Title cache now has {len(cache)} entries.")
else:
    print("Title cache is up to date.")

# ---------------------------------------------------------------------------
# 2. Slug → title fallback (when scraper hasn't filled in yet)
# ---------------------------------------------------------------------------
ACRONYMS = {
  # Central banks & institutions
  "fed","fomc","ecb","boe","boj","pboc","rbi","sebi","bis","imf","oecd","wto","wb",
  "g7","g20","brics","nato","eu","un","unctad","opec","mas","snb","cbrt","banxico",
  # Markets & finance
  "fx","gdp","cpi","ppi","pmi","tips","oas","ois","irs","etf","etfs","cds","mbs","abs",
  "usd","eur","jpy","gbp","cny","rmb","inr","brl","try","rub","zar","krw","mxn","aud","cad","chf",
  "bop","wti","brent","lng","ev","ai","ml","gpu","co2","ghg",
  # Countries / regions
  "us","usa","uk","uae","drc","prc","roc",
  # Other
  "qe","qt","mmf","sdr","spv","spr","iea","eia","ferc","cftc","sec","fda","tsa","cdc",
  "evs","esg","cop","cop26","cop27","cop28","cop29","ira","chips","mlk","fdi",
}

TITLE_FIXES = {}

def slug_to_title(slug):
    words = slug.split("-")
    out = []
    for w in words:
        wl = w.lower()
        if wl in ACRONYMS:
            out.append(wl.upper())
        elif wl in TITLE_FIXES:
            out.append(TITLE_FIXES[wl])
        elif w.isdigit() or re.match(r"^\d+[a-z]+$", wl):
            out.append(w)
        else:
            out.append(w.capitalize())
    return " ".join(out)

# ---------------------------------------------------------------------------
# 3. Concept taxonomy & extraction
# ---------------------------------------------------------------------------
TAXONOMY = {
  # Central banks
  "Fed":          ["fed","federal-reserve","powell","fomc"],
  "ECB":          ["ecb","lagarde","draghi"],
  "BoE":          ["boe","bank-of-england","bailey"],
  "BoJ":          ["boj","bank-of-japan","kuroda","ueda"],
  "PBoC":         ["pboc","peoples-bank-of-china","peoples-bank"],
  "Central Banks":["central-bank","central-banks","monetary-policy","interest-rates","rate-cut","rate-hike"],

  # Macro aggregates
  "Inflation":    ["inflation","inflationary","cpi","disinflation","deflation","reflation","price"],
  "Stagflation":  ["stagflation"],
  "Recession":    ["recession","recessions","downturn","slump"],
  "Debt":         ["debt","leverage","leveraged","indebtedness","debts"],
  "Deficit":      ["deficit","deficits","fiscal","budget"],
  "GDP / Growth": ["gdp","growth","gva","output"],
  "Productivity": ["productivity"],
  "Employment":   ["employment","jobs","unemployment","labour","labor","wages","wage"],
  "Trade":        ["trade","tariff","tariffs","import","imports","export","exports","wto"],
  "Supply Chain": ["supply-chain","supply-chains","logistics","shipping","ports","semiconductor","semiconductors","chips"],

  # Geopolitics & war
  "Ukraine War":  ["ukraine","ukrainian","zelensky","kyiv","kharkiv","donbas","crimea"],
  "Russia":       ["russia","russian","russias","putin","kremlin","moscow"],
  "Sanctions":    ["sanctions","sanction","embargo","embargoes","price-cap"],
  "Gaza / Israel":["gaza","israel","israeli","hamas","netanyahu","west-bank","palestine","palestinian"],
  "Iran":         ["iran","iranian","tehran","irgc"],
  "China":        ["china","chinese","chinas","xi","beijing","ccp"],
  "Taiwan":       ["taiwan","taiwanese","tsmc","strait"],
  "Korea":        ["korea","korean","seoul","kospi","pyongyang"],
  "NATO":         ["nato"],
  "Geopolitics":  ["geopolitics","geopolitical","war","wars","military","defence","defense","weapons"],

  # Energy & climate
  "Oil":          ["oil","crude","opec","wti","brent","barrel"],
  "Gas / LNG":    ["gas","lng","pipeline","gazprom","nordstream"],
  "Energy":       ["energy","power","electricity","grid","grids"],
  "Coal":         ["coal"],
  "Renewables":   ["renewable","renewables","solar","wind","hydrogen","battery","batteries","ev","evs"],
  "Climate":      ["climate","decarbonization","decarbonisation","net-zero","emissions","carbon","co2","ghg","esg","ets","cop","cop26","cop27","cop28","cop29"],
  "Climate Finance":["climate-finance","green-finance","transition-finance"],

  # Regions
  "Germany":      ["germany","german","berlin","scholz","merz","merkel","weimar"],
  "France":       ["france","french","paris","macron","le-pen"],
  "Italy":        ["italy","italian","rome","meloni","draghi"],
  "UK":           ["uk","britain","british","london","sunak","starmer","truss","johnson","brexit"],
  "Spain":        ["spain","spanish","madrid"],
  "Greece":       ["greece","greek","athens","grexit"],
  "Türkiye":      ["turkey","türkiye","turkish","erdogan","cbrt","istanbul"],
  "India":        ["india","indian","modi","rupee","bjp"],
  "Brazil":       ["brazil","brazilian","lula","bolsonaro","brl"],
  "South Africa": ["south-africa","sa","ramaphosa","zar"],
  "Africa":       ["africa","african","sahel","nigeria","kenya","ethiopia","egypt"],
  "Latin America":["latin-america","argentina","mexico","chile","peru","colombia","venezuela","milei"],
  "Japan":        ["japan","japanese","tokyo","yen","abenomics","abe","kishida"],
  "USA":          ["usa","america","american","washington","trump","biden","harris","obama"],
  "Eurozone":     ["eurozone","euro-area","euro","eu"],
  "Asia":         ["asia","asian","asean"],
  "Middle East":  ["middle-east","gulf","saudi","saudis","mbs","uae","qatar","kuwait","yemen"],

  # History & crises
  "1970s":        ["1970s","seventies","stagflation-70s"],
  "Weimar":       ["weimar"],
  "Bretton Woods":["bretton-woods","gold-standard"],
  "Cold War":     ["cold-war"],
  "2008 Crisis":  ["2008","gfc","lehman","subprime","financial-crisis"],
  "Eurozone Crisis":["eurozone-crisis","greek-crisis","grexit","peripheral"],
  "Covid":        ["covid","pandemic","coronavirus","lockdown"],
  "History":      ["history","historical","historian","centenary","anniversary","wwii","ww2","wwi","ww1"],

  # International institutions
  "IMF":          ["imf"],
  "World Bank":   ["world-bank","wb"],
  "BIS":          ["bis"],
  "G7 / G20":     ["g7","g20","brics","oecd"],

  # Markets & instruments
  "Bond Yields":  ["bond","bonds","yield","yields","treasury","treasuries","gilts","bunds","jgb","jgbs","duration"],
  "Sovereign Debt":["sovereign","sovereigns","sovereign-debt","default","restructuring"],
  "FX":           ["fx","currency","currencies","exchange-rate","dollar","euro-dollar","reserve-currency"],
  "Commodities":  ["commodity","commodities","copper","aluminium","aluminum","iron-ore","wheat","grain","food"],
  "Equities":     ["equities","equity","stocks","stock","sp500","mag-7"],
  "Credit":       ["credit","spreads","cds","junk","high-yield"],
  "Banking":      ["bank","banks","banking","svb","credit-suisse","cs","deutsche","jpmorgan","wells"],
  "Real Estate":  ["real-estate","property","housing","mortgage","mbs","reit"],
  "Crypto":       ["crypto","bitcoin","stablecoin","stablecoins","ether","ethereum"],

  # Economic & political concepts
  "Polycrisis":   ["polycrisis"],
  "Fiscal Dominance":["fiscal-dominance"],
  "Financialization":["financialization","financialisation"],
  "Industrial Policy":["industrial-policy","chips","chips-act","ira","subsidies","subsidy"],
  "Neoliberalism":["neoliberal","neoliberalism"],
  "Populism":     ["populism","populist","far-right","right-wing","left-wing"],
  "Democracy":    ["democracy","democratic","authoritarian","autocracy"],
  "Capitalism":   ["capitalism","capitalist"],
  "Empire / Hegemony":["empire","hegemony","hegemonic","imperial"],
  "Inequality":   ["inequality","inequalities","wealth","poverty","poor","rich"],
  "Demographics": ["demographic","demographics","aging","ageing","population","fertility","migration","immigration","refugees"],
  "Technology / AI":["technology","tech","ai","data-center","data-centers","artificial-intelligence","silicon"],
  "Health":       ["health","healthcare","mortality","life-expectancy","fentanyl","opioid"],

  # Tooze format / series
  "Top Links":    ["top-links","top-link"],
  "Chartbook":    ["chartbook"],
  "Audio / Ones & Tooze":["audio","ones-and-tooze","podcast"],
}

CLUSTERS = [
  ("Central Banks & Monetary",   {"Fed","ECB","BoE","BoJ","PBoC","Central Banks"}),
  ("Inflation & Macro",          {"Inflation","Stagflation","Recession","GDP / Growth","Productivity","Employment","Debt","Deficit"}),
  ("Geopolitics & War",          {"Ukraine War","Russia","Sanctions","Gaza / Israel","Iran","NATO","Geopolitics","Middle East"}),
  ("Energy & Climate",           {"Oil","Gas / LNG","Energy","Coal","Renewables","Climate","Climate Finance"}),
  ("China",                      {"China","Taiwan","PBoC"}),
  ("EU / Eurozone",              {"Eurozone","Germany","France","Italy","Spain","Greece","UK","ECB","Eurozone Crisis"}),
  ("US Politics & Economy",      {"USA","Fed"}),
  ("Emerging Markets & Global South",{"India","Brazil","South Africa","Africa","Latin America","Türkiye","Asia"}),
  ("History & Crises",           {"1970s","Weimar","Bretton Woods","Cold War","2008 Crisis","Eurozone Crisis","Covid","History"}),
  ("Markets & Finance",          {"Bond Yields","Sovereign Debt","FX","Commodities","Equities","Credit","Banking","Real Estate","Crypto","Trade","Supply Chain"}),
  ("Ideas & Institutions",       {"Polycrisis","Fiscal Dominance","Financialization","Industrial Policy","Neoliberalism","Populism","Democracy","Capitalism","Empire / Hegemony","Inequality","Demographics","Technology / AI","Health","IMF","World Bank","BIS","G7 / G20"}),
]

CLUSTER_COLORS = {
  "Central Banks & Monetary":      "#4e79a7",
  "Inflation & Macro":             "#e15759",
  "Geopolitics & War":             "#b07aa1",
  "Energy & Climate":              "#59a14f",
  "China":                         "#edc948",
  "EU / Eurozone":                 "#76b7b2",
  "US Politics & Economy":         "#f28e2b",
  "Emerging Markets & Global South":"#ff9da7",
  "History & Crises":              "#9c755f",
  "Markets & Finance":             "#bab0ac",
  "Ideas & Institutions":          "#af7aa1",
  "Other":                         "#6e7681",
}

def extract_concepts(text):
    s = re.sub(r"[^a-z0-9\-]+", "-", text.lower())
    s = re.sub(r"-+", "-", s).strip("-")
    hits = []
    for concept, patterns in TAXONOMY.items():
        for p in patterns:
            if "-" in p:
                if p in s:
                    hits.append(concept); break
            else:
                if re.search(rf"(^|-){re.escape(p)}($|-)", s):
                    hits.append(concept); break
    return list(dict.fromkeys(hits))

def assign_cluster(concepts):
    scores = []
    for name, members in CLUSTERS:
        scores.append((name, sum(1 for c in concepts if c in members)))
    scores.sort(key=lambda x: -x[1])
    return scores[0][0] if scores[0][1] > 0 else "Other"

def infer_post_type(slug):
    """Classify slug into chartbook essay, top-links roundup, or other.
    - 'chartbook-N-...', 'chartbook-audio-...', 'chartbook-newsletter-N' → chartbook
    - 'top-links-N-...', 'top-link-N-...', 'adam-tooze-top-links-N' → top_links
    - anything else (early posts, one-offs, cross-posts) → other
    """
    s = slug.lower()
    if re.match(r"^chartbook", s): return "chartbook"
    if re.match(r"^(adam-tooze-)?top-links?", s): return "top_links"
    return "other"

def infer_difficulty(concepts, slug):
    quanty = {"Fiscal Dominance","Financialization","Bond Yields","Sovereign Debt","Credit"}
    if any(c in quanty for c in concepts): return "advanced"
    if any(c in concepts for c in ["Ukraine War","Russia","Gaza / Israel","Iran","Geopolitics","China"]): return "intermediate"
    if "Top Links" in concepts: return "beginner"
    if not concepts: return "beginner"
    return "intermediate"

# ---------------------------------------------------------------------------
# 4. Build thread nodes
# ---------------------------------------------------------------------------
threads = []
concept_set = Counter()
for idx, p in enumerate(posts):
    slug = p["slug"]
    cached = cache.get(slug) or {}
    real_title    = cached.get("title")    or ""
    real_subtitle = cached.get("subtitle") or ""
    title   = real_title or slug_to_title(slug)
    summary = real_subtitle or f"Chartbook · {p['date']}"

    concepts = extract_concepts(slug + " " + real_title.lower())
    cluster  = assign_cluster(concepts)
    diff     = infer_difficulty(concepts, slug)

    threads.append({
        "id": f"t{idx}",
        "slug": slug,
        "title": title,
        "url": p["url"],
        "date": p["date"],
        "summary": summary,
        "concepts": concepts,
        "cluster": cluster,
        "difficulty": diff,
        "post_type": infer_post_type(slug),
    })
    for c in concepts: concept_set[c] += 1

# ---------------------------------------------------------------------------
# 5. Concept nodes
# ---------------------------------------------------------------------------
concepts_out = []
for name, count in concept_set.most_common():
    concepts_out.append({
        "id": f"c_{re.sub(r'[^a-z0-9]+','_', name.lower())}",
        "name": name,
        "count": count,
    })

# ---------------------------------------------------------------------------
# 6. Edges between threads (Jaccard, with degree cap)
# ---------------------------------------------------------------------------
edges = []
by_id = {t["id"]: set(t["concepts"]) | {"@cluster:" + t["cluster"]} for t in threads}
ids = list(by_id.keys())
EDGE_THRESHOLD = 0.22   # tighter than NB build to keep the much larger graph readable
MAX_DEG = 6
adj = defaultdict(list)
for a, b in combinations(ids, 2):
    sa, sb = by_id[a], by_id[b]
    if not sa or not sb: continue
    inter = len(sa & sb)
    if inter < 2: continue
    union = len(sa | sb)
    j = inter / union
    if j >= EDGE_THRESHOLD:
        adj[a].append((b, j, inter))
        adj[b].append((a, j, inter))

seen = set()
for a, neighbors in adj.items():
    neighbors.sort(key=lambda x: -x[1])
    for b, j, inter in neighbors[:MAX_DEG]:
        key = tuple(sorted([a,b]))
        if key in seen: continue
        seen.add(key)
        edges.append({"source": a, "target": b, "weight": round(j,3), "shared": inter, "type":"thread"})

print(f"Edges: {len(edges)}; threads: {len(threads)}; concepts: {len(concepts_out)}")

# ---------------------------------------------------------------------------
# 7. Learning paths
# ---------------------------------------------------------------------------
def path_for(name, *needles, limit=12):
    matches = []
    for t in threads:
        blob = (t["slug"] + " " + t["title"]).lower()
        if any(n in blob for n in needles):
            matches.append(t["id"])
    return {"name": name, "threads": matches[:limit]}

learning_paths = [
    path_for("Polycrisis Primer",
             "polycrisis","permacrisis","crisis-of"),
    path_for("Ukraine War: Economic Timeline",
             "ukraine","russia","sanctions","price-cap","gazprom"),
    path_for("China Picture",
             "china","chinese","xi","pboc","beijing","yuan","taiwan"),
    path_for("Inflation Decade 2021–2026",
             "inflation","disinflation","cpi","stagflation","price"),
    path_for("Climate Finance Tour",
             "climate","decarbonization","decarbonisation","net-zero","carbon","green","cop","emissions"),
    path_for("Energy & Oil Geopolitics",
             "oil","crude","opec","lng","gas","pipeline","brent","wti"),
    path_for("Gaza, Israel & the Middle East",
             "gaza","israel","palestine","iran","saudi","middle-east","hamas"),
    path_for("Eurozone & Germany",
             "eurozone","germany","ecb","draghi","lagarde","scholz","merz","euro"),
    path_for("US Election & Trump Economy",
             "trump","biden","harris","ira","chips","tariff","tariffs","mag-7"),
    path_for("Emerging Markets & the Global South",
             "africa","india","brazil","türkiye","turkey","argentina","milei","global-south"),
    path_for("Central Banking After the Pivot",
             "fed","fomc","powell","ecb","boj","central-bank","monetary-policy","rate-cut","rate-hike"),
    path_for("History & The 1970s Echo",
             "1970s","weimar","bretton-woods","cold-war","centenary","anniversary","history"),
]
learning_paths = [p for p in learning_paths if len(p["threads"]) >= 3]

# ---------------------------------------------------------------------------
# 8. Quizzes
# ---------------------------------------------------------------------------
quizzes = []
by_cluster = defaultdict(list)
for t in threads: by_cluster[t["cluster"]].append(t)
for cl, ts in by_cluster.items():
    for t in ts[:3]:
        if not t["concepts"]: continue
        quizzes.append({
            "id": f"q_{t['id']}",
            "thread": t["id"],
            "question": f"Which concepts does \"{t['title']}\" primarily touch?",
            "answer": ", ".join(t["concepts"][:4]) or "—",
            "cluster": cl,
        })

# ---------------------------------------------------------------------------
# 9. Emit
# ---------------------------------------------------------------------------
data = {
  "publication": {
    "name": "Chartbook",
    "tagline": "Adam Tooze · economic history & the present",
    "url": "https://adamtooze.substack.com/",
    "post_count": len(threads),
  },
  "clusters": [{"name": name, "color": CLUSTER_COLORS.get(name,"#6e7681")} for name,_ in CLUSTERS] + [{"name":"Other","color":CLUSTER_COLORS["Other"]}],
  "threads": threads,
  "concepts": concepts_out,
  "edges": edges,
  "learning_paths": learning_paths,
  "quizzes": quizzes,
}

OUT.write_text(json.dumps(data, indent=2))
print(f"Wrote {OUT} ({OUT.stat().st_size:,} bytes)")
