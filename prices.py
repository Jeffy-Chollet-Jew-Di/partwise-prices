"""Partwise price robot.

Reads the stores' affiliate product feeds (Awin), finds the products that are on
partwisepc.com, and writes prices.json (lowest in-stock offer per store and product,
with the store's tracked affiliate link). Runs once a day on GitHub Actions.

The Awin feed key comes from the AWIN_KEY secret and is never written to any file.

Local test without the key:  python prices.py --feed coolblue-de=feed.csv
"""
import csv, gzip, io, json, os, re, sys, datetime, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
COLS = "aw_deep_link,product_name,merchant_category,search_price,brand_name,ean,mpn,in_stock,stock_status,delivery_time,currency"
HISTORY_DAYS = 180

# Words that mark a different model. A store product containing one of these only matches
# a site product whose own search words contain it too ("RTX 5070" must not take a "5070 Ti").
VARIANT = {"xl", "ti", "super", "xt", "xtx", "gre", "x3d", "g2", "chromax", "pro", "plus", "lx", "slim", "mini", "max",
           "rgb", "argb", "white", "weiss", "laptop", "notebook", "sodimm", "so", "refurbished", "generalüberholt",
           "gebraucht", "b", "ware", "tray", "lcd", "v2", "v3", "x2", "x3", "shift", "elite", "gaming", "oc", "dex", "se", "wireless", "ii", "iii"}
# words that never decide anything
NOISE = {"the", "mit", "und", "with", "and", "für", "for", "schwarz", "black", "nvme", "ssd", "m2", "pcie",
         "dimm", "ddr", "mhz", "mt", "s", "cl", "kit", "single", "fan", "pack", "geforce", "radeon", "amd", "nvidia",
         "intel", "core", "ryzen", "processor", "prozessor", "boxed", "box", "wof", "atx", "gold", "cybenetics",
         "kühlkörper", "heatsink", "zoll", "inch", "sata", "graphics", "card", "grafikkarte"}
GPU_VARIANT = {"ti", "super", "xt", "xtx", "gre", "laptop", "notebook", "refurbished", "generalüberholt", "gebraucht"}
# GPU makers: a card "MSI GeForce RTX 5070" is the chip "RTX 5070" (the site compares chips, not board brands)
BOARD_BRANDS = {"asus", "msi", "gigabyte", "aorus", "zotac", "inno3d", "palit", "gainward", "pny", "sapphire",
                "powercolor", "xfx", "asrock", "sparkle", "acer", "kfa2", "galax", "biostar", "colorful", "maxsun",
                "twin", "trio", "ventus", "windforce", "eagle", "gaming", "dual", "prime", "tuf", "rog", "strix",
                "astral", "solo", "amp", "pulse", "nitro", "hellhound", "reaper", "swift", "quicksilver", "inspire",
                "shadow", "steel", "legend", "challenger", "taichi", "phantom", "spirit", "infinity", "aero", "master",
                "ice", "white", "black", "edition", "fe", "founders"}


def norm(s):
    s = s.lower().replace("g.skill", "gskill").replace("be quiet!", "bequiet").replace("be quiet", "bequiet")
    s = s.replace("wd_black", "wd black").replace("ß", "ss").replace("ä", "a").replace("ö", "o").replace("ü", "u")
    s = s.replace("m.2", "m2")
    s = re.sub(r"\b(\d{4,6})\s*mb\b", lambda m: "%dgb" % (int(m.group(1)) // 1024) if int(m.group(1)) % 1024 == 0 else m.group(0), s)   # 16384 MB -> 16gb
    s = re.sub(r"\b(rtx|gtx|rx)(\d)", r"\1 \2", s)                 # RX9060 -> rx 9060
    s = re.sub(r"\b(\d{1,2})g\b", r"\1gb", s)                        # 8G -> 8gb (card names)
    s = re.sub(r"\b[345]\.[01]\s*x[1248]\b", " ", s)                 # interface lanes "4.0 x4 / 5.0 x2" are not a version
    s = re.sub(r"\b(\d{1,2})\.(\d{3})\s*watt\b", r"\1\2w", s)               # 1.000 Watt -> 1000w
    s = re.sub(r"\b80\s*plus\b", "80plus", s)                             # "80 PLUS Gold" is an efficiency label, not a "Plus" version
    s = re.sub(r"(\d)\s*watt\b", r"\1w", s)                            # 850 Watt -> 850w
    s = re.sub(r"\b(\d{4})(ti|xtx|xt|gre)\b", r"\1 \2", s)        # 5060Ti -> 5060 ti
    s = re.sub(r"(\d)\.(\d{3})\b", r"\1\2", s)            # 6.000 -> 6000, 1.000 W -> 1000 W
    s = re.sub(r"(\d)(mt/s|mhz)", r"\1 \2", s)                    # 6000MT/s -> 6000 mt/s
    s = re.sub(r"(\d)\s*(gb|tb|w|mm)\b", r"\1\2", s)       # 2 TB -> 2tb, 850 W -> 850w
    s = re.sub(r"\(?\s*\b2\s*x\s*(\d+)gb\s*\)?", lambda m: " %dgb " % (2 * int(m.group(1))), s)   # 2 x 16GB -> 32gb
    s = re.sub(r"\b(1[24]0)x\1x25\b", r"\1mm", s)                  # fan 120x120x25 -> 120mm
    s = re.sub(r"\bc(\d\d)\b", r"cl\1", s)                          # C16 -> cl16 (CAS latency)
    s = re.sub(r"\bweiss\b", "white", s)
    s = re.sub(r"\b(wit|blanc|bianco|blanco)\b", "white", s)          # Dutch, French, Italian, Spanish shops
    toks = re.split(r"[^a-z0-9+]+", s)
    return [t for t in toks if t]


def wanted(q):
    return [t for t in norm(q) if t not in NOISE]


def matches(prod, name, cat):
    if prod["cat"] != cat:
        return False
    toks = norm(name)
    if "+" in toks or " + " in name:                       # bundles ("card + power supply")
        return False
    have = set(toks)
    need = wanted(prod["q"])
    if not all(t in have for t in need):
        return False
    if have & set(prod.get("qx") or []):                  # words that mark another version of this product
        return False
    extra = (have & VARIANT) - set(need)
    if prod["cat"] == "gpu":                               # board names (TWIN X2, OC, PLUS...) do not matter,
        extra &= GPU_VARIANT                                # only a different chip does
    return not extra


def fetch_awin(feed_id, lang="de"):
    key = os.environ.get("AWIN_KEY", "").strip()
    if not key:
        raise SystemExit("AWIN_KEY secret is missing")
    url = ("https://productdata.awin.com/datafeed/download/apikey/%s/fid/%d/format/csv/language/%s/"
           "delimiter/%%2C/compression/gzip/columns/%s/" % (key, feed_id, lang, COLS))
    req = urllib.request.Request(url, headers={"User-Agent": "partwise-price-robot"})
    with urllib.request.urlopen(req, timeout=300) as r:
        data = r.read()
    return gzip.decompress(data).decode("utf-8", "replace")


def category_of(st, mc):
    """Site category of a store category. Exact name, or with "prefix": true the longest matching branch
    of a path such as "PC-Komponenten>Grafikkarten>NVIDIA>RTX 5000" (Caseking)."""
    cat = st["cats"].get(mc)
    if cat or not st.get("prefix"):
        return cat
    best = ""
    for k in st["cats"]:
        if mc.startswith(k + ">") and len(k) > len(best):
            best = k
    return st["cats"].get(best)


def stock_of(row):
    s = (row.get("stock_status") or "").lower()
    if row.get("in_stock") in ("1", "true", "yes") or "in stock" in s:
        return "in"
    if "out" in s or row.get("in_stock") in ("0", "false", "no"):
        return "out"
    return "order"


def run(local_feeds):
    cfg = json.load(open(os.path.join(HERE, "config.json"), encoding="utf-8"))
    prods = json.load(open(os.path.join(HERE, "products.json"), encoding="utf-8"))
    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0)
    seen = now.isoformat().replace("+00:00", "Z")
    out_path = os.path.join(HERE, "prices.json")
    try:
        old = json.load(open(out_path, encoding="utf-8"))
    except Exception:
        old = {}
    offers, report = {}, {"updated": seen, "stores": {}, "rejected": []}

    for st in cfg["stores"]:
        try:
            text = open(local_feeds[st["r"]], encoding="utf-8").read() if st["r"] in local_feeds else fetch_awin(st["feed"], st.get("lang", "de"))
        except Exception as e:                              # one broken feed must not stop the others
            report["stores"][st["r"]] = {"error": str(e)[:200].replace(os.environ.get("AWIN_KEY", "") or "§", "***")}
            continue
        rows = list(csv.DictReader(io.StringIO(text)))
        best, n_pc, unmatched = {}, 0, []
        for row in rows:
            cat = category_of(st, row.get("merchant_category", ""))
            if not cat:
                continue
            n_pc += 1
            try:
                price = float(row["search_price"])
            except Exception:
                continue
            hit = False
            for p in prods:
                if not matches(p, row["product_name"], cat):
                    continue
                hit = True
                if p["ref"] and not (0.45 * p["ref"] <= price <= 2.5 * p["ref"]):   # wrong match or price error
                    report["rejected"].append([st["r"], p["id"], row["product_name"][:80], price])
                    continue
                o = {"r": st["r"], "price": round(price, 2), "ship": 0, "stock": stock_of(row), "url": row["aw_deep_link"],
                     "seen": seen, "cur": row.get("currency") or st["currency"], "title": row["product_name"][:90],
                     "delivery": (row.get("delivery_time") or "")[:40]}
                cur = best.get(p["id"])
                rank = lambda x: (x["stock"] == "out", x["price"])
                if cur is None or rank(o) < rank(cur):
                    best[p["id"]] = o
            if not hit:                                     # PC part the site does not list yet
                unmatched.append([cat, row["product_name"][:90], price])
        report.setdefault("not_on_site", {})[st["r"]] = sorted(unmatched)
        for pid, o in best.items():
            offers.setdefault(st["country"], {}).setdefault(pid, []).append(o)
        report["stores"][st["r"]] = {"feed_rows": len(rows), "pc_rows": n_pc, "matched": len(best)}

    # price history: lowest in-stock price per country and product, one point per day
    hist = old.get("history", {})
    day = now.date().isoformat()
    limit = (now.date() - datetime.timedelta(days=HISTORY_DAYS)).isoformat()
    for c, per in offers.items():
        for pid, lst in per.items():
            ins = [o["price"] for o in lst if o["stock"] != "out"]
            if not ins:
                continue
            h = [x for x in hist.setdefault(c, {}).get(pid, []) if x[0] != day and x[0] >= limit]
            h.append([day, min(ins)])
            hist[c][pid] = h
    for c, per in offers.items():
        for lst in per.values():
            lst.sort(key=lambda o: o["price"])
    json.dump({"updated": seen, "offers": offers, "history": hist}, open(out_path, "w", encoding="utf-8"),
              ensure_ascii=False, separators=(",", ":"))
    json.dump(report, open(os.path.join(HERE, "report.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(json.dumps(report["stores"]), "rejected:", len(report["rejected"]))
    ok = any("matched" in v for v in report["stores"].values())
    return 0 if ok else 1


if __name__ == "__main__":
    feeds = {}
    a = sys.argv[1:]
    while a:
        if a[0] == "--feed" and len(a) > 1:
            k, v = a[1].split("=", 1); feeds[k] = v; a = a[2:]
        else:
            a = a[1:]
    sys.exit(run(feeds))
