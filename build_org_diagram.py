#!/usr/bin/env python3
"""
Org AWS diagram from Terraform, output as editable Visio .vdx (XML DatadiagramML).

Pipeline:
  1. Run `terravision graphdata` per repo under ~/terraform/<repo>  -> per-repo graphdict JSON
  2. Attribute each repo's resources to an AWS account via your repo->account map
  3. Merge all repos into one graph, grouped into per-account containers
  4. Inject cross-account edges (peering / TGW / RAM) parsed from ~/tfstates/<account>
  5. Emit org.vdx  (open + edit in desktop Visio)

You supply: a repo->account map (JSON).  Everything else is discovered.

Account names: the account containers are labelled using your AWS CLI config
($AWS_CONFIG_FILE if set, else ~/.aws/config).  Each profile name must END with
the 12-digit account number, and the comment line directly beneath it holds the
human account name, e.g.:

    [profile myorg-prod-123456789012]
    # Production

If the config is absent (e.g. running off-box), labels fall back to the bare
account number.

Map format (repo_account_map.json):
    { "<reponame>": "<aws_account_number>", ... }
  If a repo deploys to several accounts, value may be a list:
    { "networking": ["111111111111","222222222222"], ... }
  When a repo maps to multiple accounts, its resources are attributed by the
  account embedded in each resource's ARN if present (meta_data), else duplicated.

Run:
    python3 build_org_diagram.py \
        --terraform-root ~/terraform \
        --tfstates-root  ~/tfstates \
        --map            repo_account_map.json \
        --out            org.vdx

Skip the terravision step (already have per-repo jsons in a dir):
    python3 build_org_diagram.py --graphdir ./graphjsons --tfstates-root ~/tfstates --map map.json --out org.vdx

Skip cross-account state layer:
    add --no-state
"""
import argparse, json, os, re, subprocess, sys, glob
from collections import defaultdict

# ----------------------------------------------------------------------------
# 1. terravision: run graphdata per repo
# ----------------------------------------------------------------------------
def run_terravision(terraform_root, workdir):
    """Run `terravision graphdata` for each repo dir; return {repo: graph_json_path}."""
    os.makedirs(workdir, exist_ok=True)
    repos = [d for d in glob.glob(os.path.join(os.path.expanduser(terraform_root), "*"))
             if os.path.isdir(d)]
    out = {}
    for repo in sorted(repos):
        name = os.path.basename(repo.rstrip("/"))
        dst = os.path.join(workdir, f"{name}.json")
        cmd = ["terravision", "graphdata", "--source", repo, "--outfile", dst]
        print(f"[terravision] {name} ...", file=sys.stderr)
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 or not os.path.exists(dst):
            print(f"  SKIP {name}: {r.stderr.strip()[:300]}", file=sys.stderr)
            continue
        out[name] = dst
    return out


def load_graphdicts(graphdir):
    """Load pre-generated per-repo json files: {repo: graphdict}."""
    out = {}
    for p in glob.glob(os.path.join(graphdir, "*.json")):
        name = os.path.splitext(os.path.basename(p))[0]
        try:
            with open(p) as f:
                doc = json.load(f)
            # terravision graphdata writes graphdict at top level, or under "graphdict"
            gd = doc.get("graphdict", doc) if isinstance(doc, dict) else doc
            md = doc.get("meta_data", {}) if isinstance(doc, dict) else {}
            out[name] = (gd, md)
        except Exception as e:
            print(f"  bad json {p}: {e}", file=sys.stderr)
    return out


# ----------------------------------------------------------------------------
# 1b. AWS config: account number -> account name
# ----------------------------------------------------------------------------
AWS_CONFIG_PATH = os.environ.get("AWS_CONFIG_FILE") or "~/.aws/config"

PROFILE_HDR   = re.compile(r"^\[(?:profile\s+)?(.+?)\]\s*$")
TRAILING_ACCT = re.compile(r"(\d{12})\D*$")
COMMENT_LABEL = re.compile(r"(?i)^(?:account[\s_]*name|account|name|alias)\s*[:=]\s*(.+)$")

def _parse_comment_name(s):
    """Strip the comment marker and any 'label:'/'label =' prefix; return name."""
    s = s.lstrip("#;").strip()
    if not s:
        return None
    m = COMMENT_LABEL.match(s)
    return (m.group(1).strip() if m else s) or None

def load_account_names(path=AWS_CONFIG_PATH):
    """
    Parse the AWS CLI config -> {account_number: account_name}.

    The 12-digit account number is the trailing run of the profile name, and
    the account name is the comment line directly beneath the profile header:

        [profile myorg-prod-123456789012]
        # Production

    Tolerant to '[name]' or '[profile name]' headers, blank lines between the
    header and its comment, and comment forms like '# Account Name: Production'
    or '# name = Production'.  Returns {} (with a warning) if the file is absent.
    """
    names = {}
    full = os.path.expanduser(path or "")
    if not full or not os.path.exists(full):
        if full:
            print(f"  WARN aws config not found: {full}", file=sys.stderr)
        return names
    pending = None
    with open(full) as fh:
        for raw in fh:
            s = raw.strip()
            mh = PROFILE_HDR.match(s)
            if mh:
                ma = TRAILING_ACCT.search(mh.group(1))
                pending = ma.group(1) if ma else None
                continue
            if pending:
                if not s:
                    continue  # allow blank line(s) between header and comment
                if s.startswith("#") or s.startswith(";"):
                    nm = _parse_comment_name(s)
                    if nm:
                        names[pending] = nm
                pending = None  # only the first content line under the header counts
    return names


# ----------------------------------------------------------------------------
# 2. repo -> account attribution
# ----------------------------------------------------------------------------
ARN_ACCOUNT = re.compile(r"arn:aws[a-z\-]*:[^:]*:[^:]*:(\d{12}):")

def account_for_resource(addr, meta, repo_accounts):
    """Pick the account for one resource. Prefer ARN in metadata; else the
    repo's single mapped account; else None (caller duplicates across all)."""
    if isinstance(meta, dict):
        for v in meta.values():
            if isinstance(v, str):
                m = ARN_ACCOUNT.search(v)
                if m:
                    return m.group(1)
    if len(repo_accounts) == 1:
        return repo_accounts[0]
    return None  # ambiguous -> duplicate across mapped accounts


# ----------------------------------------------------------------------------
# 3. merge into one account-grouped graph
# ----------------------------------------------------------------------------
def merge(graphdicts, repo_map):
    """
    Returns:
      nodes: {global_id: {"label","type","account"}}
      edges: list of (src_global_id, dst_global_id)
    global_id = "<account>/<repo>/<address>" to keep accounts/repos disjoint.
    """
    nodes, edges = {}, []
    for repo, (gd, md) in graphdicts.items():
        mapped = repo_map.get(repo)
        if mapped is None:
            print(f"  WARN repo '{repo}' not in map; skipping", file=sys.stderr)
            continue
        repo_accounts = mapped if isinstance(mapped, list) else [mapped]

        # decide account per address
        addr_acct = {}
        for addr in gd:
            acct = account_for_resource(addr, md.get(addr, {}), repo_accounts)
            addr_acct[addr] = [acct] if acct else repo_accounts  # duplicate if ambiguous

        def gid(addr, acct):
            return f"{acct}/{repo}/{addr}"

        for addr, conns in gd.items():
            for acct in addr_acct[addr]:
                g = gid(addr, acct)
                if g not in nodes:
                    rtype = addr.split(".")[0]
                    nodes[g] = {"label": addr, "type": rtype, "account": acct}
                for dst in conns:
                    # connect within the same account attribution
                    for dacct in addr_acct.get(dst, [acct]):
                        if dacct == acct:
                            edges.append((g, gid(dst, dacct)))
    # keep only edges whose endpoints exist
    valid = set(nodes)
    edges = [(a, b) for a, b in edges if a in valid and b in valid]
    return nodes, edges


# ----------------------------------------------------------------------------
# 4. cross-account edges from state
# ----------------------------------------------------------------------------
CROSS_TYPES = ("aws_vpc_peering_connection", "aws_ec2_transit_gateway_vpc_attachment",
               "aws_ec2_transit_gateway_peering_attachment", "aws_ram_principal_association")

def iter_state_resources(doc):
    """Yield (type, name, attributes) from raw state or show-json."""
    if "resources" in doc and "values" not in doc:
        for r in doc.get("resources", []):
            if r.get("mode") != "managed":
                continue
            for inst in r.get("instances", []):
                yield r.get("type",""), r.get("name",""), inst.get("attributes", {})
    elif "values" in doc:
        def walk(m):
            for r in m.get("resources", []):
                yield r.get("type",""), r.get("name",""), r.get("values", {})
            for c in m.get("child_modules", []):
                yield from walk(c)
        yield from walk(doc.get("values", {}).get("root_module", {}))

def cross_account_edges(tfstates_root, accounts):
    """
    Scan each ~/tfstates/<account>/**.tfstate for peering/TGW/RAM; build edges
    between account container nodes. Returns list of (acctA, acctB, label).
    """
    edges = []
    root = os.path.expanduser(tfstates_root)
    for acct in accounts:
        for sf in glob.glob(os.path.join(root, acct, "**", "*.tfstate"), recursive=True):
            if sf.endswith(".backup"):
                continue
            try:
                with open(sf) as f:
                    doc = json.load(f)
            except Exception:
                continue
            for rtype, name, attrs in iter_state_resources(doc):
                if rtype not in CROSS_TYPES:
                    continue
                peer = (attrs.get("peer_owner_id") or attrs.get("peer_account_id")
                        or attrs.get("principal") or "")
                m = re.search(r"\d{12}", str(peer))
                other = m.group(0) if m else None
                if other and other != acct:
                    label = rtype.replace("aws_", "").replace("_", " ")
                    edges.append((acct, other, label))
    # dedupe undirected
    seen, uniq = set(), []
    for a, b, l in edges:
        k = tuple(sorted((a, b)) + [l])
        if k not in seen:
            seen.add(k); uniq.append((a, b, l))
    return uniq


# ----------------------------------------------------------------------------
# 5. .vdx emitter  (DatadiagramML — plain XML Visio opens natively)
# ----------------------------------------------------------------------------
def esc(s):
    return (str(s).replace("&","&amp;").replace("<","&lt;").replace(">","&gt;")
            .replace('"',"&quot;"))

def build_vdx(nodes, edges, xedges, out_path, acct_names=None):
    """
    Lay out: one container box per account; resource boxes gridded inside;
    intra-account connectors; cross-account connectors between containers.
    Geometry in inches. Single Page. No external stencil dependency.

    acct_names: optional {account_number: account_name} for container labels.
    """
    acct_names = acct_names or {}
    by_acct = defaultdict(list)
    for gid, n in nodes.items():
        by_acct[n["account"]].append((gid, n))

    accounts = sorted(by_acct)
    COLS = max(1, int(len(accounts) ** 0.5 + 0.999))  # square-ish account grid
    ACCT_W, ACCT_H = 9.0, 7.0
    PAD = 1.0
    shapes, connects = [], []
    sid = [1]
    def next_id():
        i = sid[0]; sid[0] += 1; return i
    id_of = {}            # gid -> shape id
    acct_anchor = {}      # account -> (cx, cy) for cross-account links

    for ai, acct in enumerate(accounts):
        col, row = ai % COLS, ai // COLS
        ax = col * (ACCT_W + PAD) + PAD
        ay = row * (ACCT_H + PAD) + PAD
        cont_id = next_id()
        acct_anchor[acct] = (ax + ACCT_W/2, ay + ACCT_H/2)
        # container shape (account) — prefer human name from AWS config
        nm = acct_names.get(acct)
        acct_label = f"{nm} ({acct})" if nm else f"AWS Account {acct}"
        shapes.append(container_shape(cont_id, acct_label,
                                      ax, ay, ACCT_W, ACCT_H))
        # grid resources inside
        items = by_acct[acct]
        n = len(items)
        gcols = max(1, int(n ** 0.5 + 0.999))
        bw, bh = 1.6, 0.6
        gx_gap, gy_gap = 0.25, 0.35
        for k, (gid, node) in enumerate(items):
            gc, gr = k % gcols, k // gcols
            x = ax + 0.4 + gc * (bw + gx_gap)
            y = ay + ACCT_H - 0.8 - gr * (bh + gy_gap)
            shp = next_id(); id_of[gid] = shp
            shapes.append(node_shape(shp, node["label"], node["type"], x, y, bw, bh))

    # intra-account connectors
    for a, b in edges:
        if a in id_of and b in id_of:
            cid = next_id()
            shapes.append(connector_shape(cid))
            connects.append((cid, id_of[a], id_of[b]))

    # cross-account connectors (container to container), drawn bold
    for a, b, label in xedges:
        if a in acct_anchor and b in acct_anchor:
            # connect the two container shapes by id: find their container ids
            pass  # handled below via dynamic connector between container centers

    xml = vdx_document(shapes, connects, xedges, acct_anchor, accounts)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(xml)


def container_shape(i, text, x, y, w, h):
    return f"""
      <Shape ID='{i}' Type='Shape'>
        <Cell N='PinX' V='{x + w/2:.3f}'/><Cell N='PinY' V='{y + h/2:.3f}'/>
        <Cell N='Width' V='{w:.3f}'/><Cell N='Height' V='{h:.3f}'/>
        <Cell N='LineColor' V='#2563eb'/><Cell N='LineWeight' V='0.03'/>
        <Cell N='FillForegnd' V='#eff6ff'/>
        <Cell N='VerticalAlign' V='0'/>
        <Text>{esc(text)}</Text>
        <Geom IX='0'>
          <MoveTo IX='1' X='0' Y='0'/><LineTo IX='2' X='{w:.3f}' Y='0'/>
          <LineTo IX='3' X='{w:.3f}' Y='{h:.3f}'/><LineTo IX='4' X='0' Y='{h:.3f}'/>
          <LineTo IX='5' X='0' Y='0'/>
        </Geom>
      </Shape>"""

TYPE_COLOR = {
    "aws_vpc":"#dbeafe","aws_subnet":"#d1fae5","aws_instance":"#fef3c7",
    "aws_security_group":"#fee2e2","aws_lb":"#ede9fe","aws_db_instance":"#fce7f3",
    "aws_s3_bucket":"#dcfce7","aws_lambda_function":"#ffedd5",
}
def node_shape(i, label, rtype, x, y, w, h):
    fill = TYPE_COLOR.get(rtype, "#f3f4f6")
    return f"""
      <Shape ID='{i}' Type='Shape'>
        <Cell N='PinX' V='{x + w/2:.3f}'/><Cell N='PinY' V='{y + h/2:.3f}'/>
        <Cell N='Width' V='{w:.3f}'/><Cell N='Height' V='{h:.3f}'/>
        <Cell N='FillForegnd' V='{fill}'/><Cell N='LineColor' V='#6b7280'/>
        <Cell N='LineWeight' V='0.01'/>
        <Text>{esc(label)}</Text>
        <Geom IX='0'>
          <MoveTo IX='1' X='0' Y='0'/><LineTo IX='2' X='{w:.3f}' Y='0'/>
          <LineTo IX='3' X='{w:.3f}' Y='{h:.3f}'/><LineTo IX='4' X='0' Y='{h:.3f}'/>
          <LineTo IX='5' X='0' Y='0'/>
        </Geom>
      </Shape>"""

def connector_shape(i):
    # 1-D dynamic connector; endpoints set by Connect rows
    return f"""
      <Shape ID='{i}' Type='Shape'>
        <Cell N='LineColor' V='#9ca3af'/><Cell N='LineWeight' V='0.008'/>
        <Cell N='BeginX' V='0'/><Cell N='BeginY' V='0'/>
        <Cell N='EndX' V='1'/><Cell N='EndY' V='0'/>
        <Geom IX='0'><MoveTo IX='1' X='0' Y='0'/><LineTo IX='2' X='1' Y='0'/></Geom>
      </Shape>"""

def vdx_document(shapes, connects, xedges, acct_anchor, accounts):
    shape_xml = "".join(shapes)
    connect_xml = ""
    for cid, a, b in connects:
        connect_xml += (f"<Connect FromSheet='{cid}' FromCell='BeginX' ToSheet='{a}'/>"
                        f"<Connect FromSheet='{cid}' FromCell='EndX' ToSheet='{b}'/>")
    # cross-account connectors as straight bold lines between container centers
    extra = ""
    base = 90000
    for n,(a,b,label) in enumerate(xedges):
        if a in acct_anchor and b in acct_anchor:
            ax,ay = acct_anchor[a]; bx,by = acct_anchor[b]
            i = base + n
            extra += f"""
      <Shape ID='{i}' Type='Shape'>
        <Cell N='BeginX' V='{ax:.3f}'/><Cell N='BeginY' V='{ay:.3f}'/>
        <Cell N='EndX' V='{bx:.3f}'/><Cell N='EndY' V='{by:.3f}'/>
        <Cell N='LineColor' V='#dc2626'/><Cell N='LineWeight' V='0.04'/>
        <Text>{esc(label)}</Text>
        <Geom IX='0'><MoveTo IX='1' X='{ax:.3f}' Y='{ay:.3f}'/>
          <LineTo IX='2' X='{bx:.3f}' Y='{by:.3f}'/></Geom>
      </Shape>"""
    return f"""<?xml version='1.0' encoding='UTF-8'?>
<VisioDocument xmlns='http://schemas.microsoft.com/visio/2003/core'>
  <Pages>
    <Page ID='0' Name='Org'>
      <PageSheet><Cell N='PageWidth' V='200'/><Cell N='PageHeight' V='200'/></PageSheet>
      <Shapes>{shape_xml}{extra}</Shapes>
      <Connects>{connect_xml}</Connects>
    </Page>
  </Pages>
</VisioDocument>"""


# ----------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--terraform-root", default="~/terraform")
    ap.add_argument("--tfstates-root", default="~/tfstates")
    ap.add_argument("--map", required=True, help="repo->account JSON")
    ap.add_argument("--graphdir", help="dir of pre-generated per-repo json; skips terravision")
    ap.add_argument("--workdir", default="./_graphdata")
    ap.add_argument("--no-state", action="store_true", help="skip cross-account edge layer")
    ap.add_argument("--out", default="org.vdx")
    args = ap.parse_args()

    with open(args.map) as f:
        repo_map = json.load(f)

    if args.graphdir:
        graphdicts = load_graphdicts(args.graphdir)
    else:
        paths = run_terravision(args.terraform_root, args.workdir)
        graphdicts = load_graphdicts(args.workdir)
    if not graphdicts:
        sys.exit("No graphdicts produced. Check terravision ran and emitted json.")

    nodes, edges = merge(graphdicts, repo_map)
    accounts = sorted({n["account"] for n in nodes.values()})
    print(f"accounts={len(accounts)} nodes={len(nodes)} intra_edges={len(edges)}",
          file=sys.stderr)

    acct_names = load_account_names()
    matched = sum(1 for a in accounts if a in acct_names)
    print(f"account names: {len(acct_names)} parsed, {matched}/{len(accounts)} matched",
          file=sys.stderr)
    missing = [a for a in accounts if a not in acct_names]
    if missing:
        print("  no name for: " + ", ".join(missing), file=sys.stderr)

    xedges = [] if args.no_state else cross_account_edges(args.tfstates_root, accounts)
    print(f"cross_account_edges={len(xedges)}", file=sys.stderr)

    build_vdx(nodes, edges, xedges, args.out, acct_names)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
