#!/usr/bin/env python3
"""Aggregate job postings -> feed.xml (RSS), jobs.json, jobs.html (sortable tables).

Usage: python jobfeed.py [output_dir]      (default output_dir: ./site)
Standard library only. Configure keywords and sources in config.json.
"""
import hashlib
import html as htmllib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime, parsedate_to_datetime
from pathlib import Path

HERE = Path(__file__).parent
CFG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
NOW = datetime.now(timezone.utc)

INTERN = re.compile(r"\b(intern|interns|internship|co-?op|student trainee|pathways)\b", re.I)
EXCL = re.compile(r"\b(" + "|".join(re.escape(x) for x in CFG["exclude_title"]) + r")\b", re.I)
MONTHS = {m: i + 1 for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split())}
SEASONS = {"winter": 1, "spring": 3, "summer": 6, "fall": 9, "autumn": 9}
SEASON_RE = re.compile(r"\b(winter|spring|summer|fall|autumn)\s+(20\d\d)\b", re.I)
START_RE = re.compile(
    r"(?:start(?:s|ing)?(?:\s+date)?|begin(?:s|ning)?|commenc\w+)[^.\n]{0,30}?"
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+(?:\d{1,2},?\s+)?(20\d\d)\b", re.I)


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "jobfeed/1.0 (personal use)"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return r.read()


def strip_tags(s):
    s = re.sub(r"<[^>]+>", " ", s or "")
    return re.sub(r"\s+", " ", s.replace("&nbsp;", " ")).strip()


def to_dt(x):
    if not x:
        return None
    try:
        if isinstance(x, (int, float)):
            return datetime.fromtimestamp(x / 1000, timezone.utc)
        try:
            d = parsedate_to_datetime(x)
        except (TypeError, ValueError):
            d = datetime.fromisoformat(x.replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def find_start(title, text):
    """Return (display, sortkey) if a start date or season is stated, else ('', '')."""
    m = START_RE.search(text)
    if m:
        mon = m.group(1).lower()[:3]
        return f"{mon.title()} {m.group(2)}", f"{m.group(2)}-{MONTHS[mon]:02d}"
    m = SEASON_RE.search(title) or SEASON_RE.search(text)
    if m:
        s = m.group(1).lower()
        return f"{s.title()} {m.group(2)}", f"{m.group(2)}-{SEASONS[s]:02d}"
    return "", ""


# ---- sources: each yields dicts with title, company, location, link, posted, text ----
def src_rss(s):
    root = ET.fromstring(get(s["url"]))
    for it in root.iter("item"):
        f = {}
        for c in it:
            f.setdefault(c.tag.split("}")[-1], (c.text or "").strip())
        title, company = f.get("title", ""), s["name"]
        if s.get("split_company") and ": " in title:
            company, title = title.split(": ", 1)
        yield dict(title=title, company=company,
                   location=f.get("region") or f.get("location") or "Remote",
                   link=f.get("link", ""), posted=to_dt(f.get("pubDate")), text=strip_tags(f.get("description")))


def src_remotive(terms):
    for t in terms:
        data = json.loads(get("https://remotive.com/api/remote-jobs?search=" + urllib.parse.quote(t)))
        for j in data.get("jobs", []):
            yield dict(title=j.get("title", ""), company=j.get("company_name", ""),
                       location=j.get("candidate_required_location") or "Remote", link=j.get("url", ""),
                       posted=to_dt(j.get("publication_date")), text=strip_tags(j.get("description")))


def src_greenhouse(tokens):
    for t in tokens:
        data = json.loads(get(f"https://boards-api.greenhouse.io/v1/boards/{t}/jobs?content=true"))
        for j in data.get("jobs", []):
            yield dict(title=j.get("title", ""), company=t.replace("-", " ").title(),
                       location=(j.get("location") or {}).get("name", ""), link=j.get("absolute_url", ""),
                       posted=to_dt(j.get("updated_at")), text=strip_tags(htmllib.unescape(j.get("content", ""))))


def src_lever(tokens):
    for t in tokens:
        for j in json.loads(get(f"https://api.lever.co/v0/postings/{t}?mode=json")):
            yield dict(title=j.get("text", ""), company=t.replace("-", " ").title(),
                       location=(j.get("categories") or {}).get("location", ""), link=j.get("hostedUrl", ""),
                       posted=to_dt(j.get("createdAt")), text=j.get("descriptionPlain", ""))


def src_adzuna(cfg):
    app_id, app_key = os.environ.get("ADZUNA_APP_ID", ""), os.environ.get("ADZUNA_APP_KEY", "")
    if not (app_id and app_key):
        print("[skip] Adzuna: ADZUNA_APP_ID / ADZUNA_APP_KEY not set", file=sys.stderr)
        return
    for where in cfg.get("where", [""]):
        for what in cfg.get("what", []):
            q = {"app_id": app_id, "app_key": app_key, "what": what, "sort_by": "date",
                 "results_per_page": cfg.get("results_per_page", 50), "max_days_old": cfg.get("max_days_old", 14)}
            if where:
                q.update({"where": where, "distance": cfg.get("distance_km", 40)})
            data = json.loads(get("https://api.adzuna.com/v1/api/jobs/us/search/1?" + urllib.parse.urlencode(q)))
            for j in data.get("results", []):
                yield dict(uid="adzuna:" + str(j.get("id")), title=j.get("title", ""),
                           company=(j.get("company") or {}).get("display_name", ""),
                           location=(j.get("location") or {}).get("display_name", ""),
                           link=j.get("redirect_url", ""), posted=to_dt(j.get("created")),
                           text=strip_tags(j.get("description")))


def src_usajobs(cfg):
    key, email = os.environ.get("USAJOBS_API_KEY", ""), os.environ.get("USAJOBS_EMAIL", "")
    if not (key and email):
        print("[skip] USAJOBS: USAJOBS_API_KEY / USAJOBS_EMAIL not set", file=sys.stderr)
        return
    hdr = {"Host": "data.usajobs.gov", "User-Agent": email, "Authorization-Key": key}
    for kw in cfg.get("keywords", []):
        q = {"Keyword": kw, "ResultsPerPage": cfg.get("results_per_page", 50), "DatePosted": cfg.get("days", 14)}
        if cfg.get("location"):
            q["LocationName"] = cfg["location"]
        data = json.loads(get("https://data.usajobs.gov/api/search?" + urllib.parse.urlencode(q), hdr))
        for it in (data.get("SearchResult") or {}).get("SearchResultItems", []):
            d = it.get("MatchedObjectDescriptor") or {}
            det = (d.get("UserArea") or {}).get("Details") or {}
            yield dict(uid="usajobs:" + str(it.get("MatchedObjectId") or d.get("PositionURI", "")),
                       title=d.get("PositionTitle", ""), company=d.get("OrganizationName") or d.get("DepartmentName", ""),
                       location=d.get("PositionLocationDisplay", ""), link=d.get("PositionURI", ""),
                       posted=to_dt(d.get("PublicationStartDate")),
                       text=strip_tags(det.get("JobSummary", "") + " " + d.get("QualificationSummary", "")))


def collect():
    s = CFG["sources"]
    runners = [(src_rss, s.get("rss", [])), (src_remotive, [s.get("remotive", [])]),
               (src_greenhouse, [s.get("greenhouse", [])]), (src_lever, [s.get("lever", [])]),
               (src_adzuna, [s.get("adzuna")]), (src_usajobs, [s.get("usajobs")])]
    for fn, args in runners:
        for a in args:
            if not a:
                continue
            try:
                yield from fn(a)
            except Exception as e:  # one broken source should not stop the run
                print(f"[warn] {fn.__name__} failed: {e}", file=sys.stderr)


# ---- matching ----
def _rx(kw):
    return re.compile(r"\b" + re.escape(kw) + r"\b", re.I)


IND = {name: ([_rx(k) for k in g["keywords"]], [_rx(k) for k in g["intern_topics"]])
       for name, g in CFG["industries"].items()}


def classify(title):
    """Return (industry, keyword, is_intern) or None."""
    if EXCL.search(title):
        return None
    intern = bool(INTERN.search(title))
    for name, (kws, topics) in IND.items():
        for rx in kws + (topics if intern else []):
            m = rx.search(title)
            if m:
                return name, m.group(0).lower(), intern
    return None


def loc_ok(loc):
    allow = [x.lower() for x in CFG.get("locations", [])]
    return not allow or any(a in (loc or "").lower() for a in allow)


def main(out):
    out.mkdir(parents=True, exist_ok=True)
    db_path = out / "jobs.json"
    db = {j["id"]: j for j in json.loads(db_path.read_text("utf-8"))} if db_path.exists() else {}
    new = 0
    for j in collect():
        c = classify(j["title"])
        if not c or not j["link"] or not loc_ok(j["location"]):
            continue
        jid = hashlib.sha1((j.get("uid") or j["link"] or j["title"] + j["company"]).encode()).hexdigest()[:16]
        if jid in db:
            continue
        start, key = find_start(j["title"], j["text"])
        db[jid] = dict(id=jid, title=j["title"], company=j["company"], location=j["location"] or "",
                       link=j["link"], industry=c[0], keyword=c[1], intern=c[2], start=start, start_key=key,
                       posted=(j["posted"] or NOW).date().isoformat(), first_seen=NOW.isoformat())
        new += 1
    cutoff = (NOW - timedelta(days=CFG["keep_days"])).isoformat()
    rows = sorted((j for j in db.values() if j["first_seen"] >= cutoff), key=lambda j: j["first_seen"], reverse=True)
    db_path.write_text(json.dumps(rows, indent=1, ensure_ascii=False), "utf-8")
    write_feed(rows, out / "feed.xml")
    write_html(rows, out / "jobs.html")
    print(f"{new} new, {len(rows)} total -> {out}")


def write_feed(rows, path):
    rss = ET.Element("rss", version="2.0")
    ch = ET.SubElement(rss, "channel")
    ET.SubElement(ch, "title").text = "Job postings"
    ET.SubElement(ch, "link").text = CFG.get("site_url") or "https://example.com/"
    ET.SubElement(ch, "description").text = "Aggregated entry-level and internship postings"
    ET.SubElement(ch, "lastBuildDate").text = format_datetime(NOW)
    for j in rows[:200]:
        it = ET.SubElement(ch, "item")
        tag = "[Internship] " if j["intern"] else ""
        ET.SubElement(it, "title").text = f'{tag}{j["title"]} - {j["company"]} ({j["location"] or "n/a"})'
        ET.SubElement(it, "link").text = j["link"]
        g = ET.SubElement(it, "guid", isPermaLink="false")
        g.text = j["id"]
        ET.SubElement(it, "pubDate").text = format_datetime(datetime.fromisoformat(j["first_seen"]))
        ET.SubElement(it, "category").text = j["industry"]
        ET.SubElement(it, "description").text = (
            f'{j["industry"]} | keyword: {j["keyword"]} | start: {j["start"] or "not stated"} | posted {j["posted"]}')
    ET.indent(rss)
    ET.ElementTree(rss).write(path, encoding="utf-8", xml_declaration=True)


def write_html(rows, path):
    data = json.dumps(rows, ensure_ascii=False).replace("</", "<\\/")
    path.write_text(PAGE.replace("__DATA__", data), "utf-8")


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Job tracker</title>
<style>
:root{--bg:#fff;--fg:#1c1f23;--mut:#666d75;--line:#dcdfe3;--hd:#f2f4f6;--ac:#0a58ca}
@media (prefers-color-scheme:dark){:root{--bg:#15171a;--fg:#e8eaed;--mut:#98a0a8;--line:#2c3036;--hd:#1f2227;--ac:#7aa7ff}}
body{margin:0;padding:24px;background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
h1{font-size:20px;margin:0 0 12px}h2{font-size:16px;margin:28px 0 8px}
.bar{display:flex;gap:8px;flex-wrap:wrap}
input,select{padding:7px 9px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--fg);font:inherit}
.wrap{overflow-x:auto;border:1px solid var(--line);border-radius:6px}
table{border-collapse:collapse;width:100%;min-width:860px}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);vertical-align:top}
tr:last-child td{border-bottom:0}
th{background:var(--hd);cursor:pointer;user-select:none;white-space:nowrap}
th:focus-visible{outline:2px solid var(--ac);outline-offset:-2px}
th.on::after{content:" \\25B2"}th.on.desc::after{content:" \\25BC"}
a{color:var(--ac)}.mut{color:var(--mut)}
</style></head><body>
<h1>Job tracker</h1>
<div class="bar">
<input id="q" type="search" placeholder="Search title, company, keyword" aria-label="Search">
<select id="ind" aria-label="Industry"><option value="">All industries</option></select>
<input id="loc" type="search" placeholder="Filter by location" aria-label="Location">
</div>
<h2 id="h-ft"></h2><div class="wrap"><table id="t-ft"></table></div>
<h2 id="h-in"></h2><div class="wrap"><table id="t-in"></table></div>
<script>
const DATA=__DATA__;
const COLS=[["title","Title"],["company","Company"],["location","Location"],["industry","Industry"],
["keyword","Keyword"],["start","Start date","start_key"],["posted","Posted"]];
const T={ft:{intern:false,label:"Full-time and entry-level",k:"posted",d:true},
         in:{intern:true,label:"Internships",k:"posted",d:true}};
const q=document.getElementById("q"),ind=document.getElementById("ind"),loc=document.getElementById("loc");
[...new Set(DATA.map(j=>j.industry))].sort().forEach(v=>{const o=document.createElement("option");o.value=o.textContent=v;ind.append(o)});
function el(t,txt,cls){const e=document.createElement(t);if(txt!=null)e.textContent=txt;if(cls)e.className=cls;return e}
function render(id){
  const s=T[id],qq=q.value.toLowerCase(),ll=loc.value.toLowerCase();
  const rows=DATA.filter(j=>j.intern===s.intern&&(!ind.value||j.industry===ind.value)
    &&(!ll||(j.location||"").toLowerCase().includes(ll))
    &&(!qq||[j.title,j.company,j.keyword].join(" ").toLowerCase().includes(qq)));
  const col=COLS.find(c=>c[0]===s.k),f=col[2]||col[0];
  rows.sort((a,b)=>{const x=(a[f]||"").toLowerCase(),y=(b[f]||"").toLowerCase();
    if(!x&&!y)return 0;if(!x)return 1;if(!y)return -1;return(x<y?-1:x>y?1:0)*(s.d?-1:1)});
  document.getElementById("h-"+id).textContent=s.label+" ("+rows.length+")";
  const t=document.getElementById("t-"+id);t.replaceChildren();
  const hr=el("tr");
  COLS.forEach(c=>{const th=el("th",c[1]);th.tabIndex=0;th.setAttribute("role","button");
    if(c[0]===s.k)th.className="on"+(s.d?" desc":"");
    const go=()=>{s.d=(s.k===c[0])?!s.d:false;s.k=c[0];render(id)};
    th.onclick=go;th.onkeydown=e=>{if(e.key==="Enter"||e.key===" "){e.preventDefault();go()}};hr.append(th)});
  const head=el("thead");head.append(hr);t.append(head);
  const tb=el("tbody");
  rows.forEach(j=>{const tr=el("tr");
    COLS.forEach(c=>{const td=el("td");
      if(c[0]==="title"&&/^https?:\\/\\//.test(j.link)){const a=el("a",j.title);a.href=j.link;a.target="_blank";a.rel="noopener noreferrer";td.append(a)}
      else if(c[0]==="start"&&!j.start){td.append(el("span","not stated","mut"))}
      else td.textContent=j[c[0]]||"";
      tr.append(td)});tb.append(tr)});
  if(!rows.length){const tr=el("tr"),td=el("td","No matching postings.","mut");td.colSpan=COLS.length;tr.append(td);tb.append(tr)}
  t.append(tb);
}
const all=()=>Object.keys(T).forEach(render);
[q,ind,loc].forEach(e=>e.addEventListener("input",all));all();
</script></body></html>
"""

if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else HERE / "site")
