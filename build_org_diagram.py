#!/usr/bin/env python3
"""
Org AWS diagram from Terraform, output as editable Visio .vdx (XML DatadiagramML).

Pipeline:
  1. Run `terravision graphdata` per repo under ~/terraform/<repo>  -> per-repo graphdict JSON
  2. Attribute each repo's resources to an AWS account by matching the repo's
     S3 backend key to a state file under ~/tfstates/<accountnumber>/
  3. Merge all repos into one graph, grouped into per-account containers
  4. Inject cross-account edges (peering / TGW / RAM) parsed from ~/tfstates/<account>
  5. Emit org.vdx  (open + edit in desktop Visio)

You supply nothing: repos, accounts, and names are all discovered.
  - which account a repo belongs to: its S3 backend key matched to a state file
    under ~/tfstates/<accountnumber>/ (account = the directory the state is in,
    the same directory-driven approach as tfgraph.py)
  - every resource a repo produces is placed in that account (a genuine
    multi-backend repo is drawn in each of its accounts)
  - a repo whose backend key matches no state is kept under an explicit
    'unresolved' container, never dropped

Account names: the account containers are labelled using your AWS CLI config
($AWS_CONFIG_FILE if set, else ~/.aws/config).  Each profile name must END with
the 12-digit account number, and the comment line directly beneath it holds the
human account name, e.g.:

    [profile myorg-prod-123456789012]
    # Production

If the config is absent (e.g. running off-box), labels fall back to the bare
account number.

Git auth (terravision clones git-sourced modules, e.g. from a private GitLab):
put GIT_USERNAME (or GIT_USER), GIT_TOKEN, and GIT_HOST in a .env file (loaded via
python-dotenv).  Creds are injected with no credential helper and no files via
Git's own GIT_CONFIG_* env vars (needs Git >= 2.31).

Run:
    python3 build_org_diagram.py \
        --terraform-root ~/terraform \
        --tfstates-root  ~/tfstates \
        --out            org.vdx

Skip the terravision step (already have per-repo jsons in a dir):
    python3 build_org_diagram.py --graphdir ./graphjsons --tfstates-root ~/tfstates --out org.vdx

Skip cross-account state layer:
    add --no-state
"""
import argparse, concurrent.futures, json, os, re, subprocess, sys, glob
from collections import defaultdict
from dotenv import load_dotenv, find_dotenv


# ----------------------------------------------------------------------------
# 0. environment: load .env, wire git auth from GIT_USERNAME / GIT_TOKEN
# ----------------------------------------------------------------------------
def configure_git_auth():
    """
    Authenticate terravision's git module clones with GIT_USERNAME (or GIT_USER)
    + GIT_TOKEN,
    no credential helper and no files: inject `url.https://<creds>@<host>/.insteadOf
    https://<host>/` via Git's own GIT_CONFIG_* env vars (Git >= 2.31).  Host
    comes from GIT_HOST (required when GIT_TOKEN is set; no default).  No-op if
    GIT_TOKEN is unset.
    """
    from urllib.parse import quote
    token = os.environ.get("GIT_TOKEN")
    if not token:
        return
    user = os.environ.get("GIT_USERNAME") or os.environ.get("GIT_USER") or ""
    host = os.environ.get("GIT_HOST")
    if not host:
        sys.exit("GIT_TOKEN is set but GIT_HOST is not. Set GIT_HOST (your git "
                 "host, e.g. the GitLab server) in .env so clones authenticate "
                 "to the right host.")
    cred = (quote(user, safe="") + ":" + quote(token, safe="")) if user \
        else quote(token, safe="")
    n = int(os.environ.get("GIT_CONFIG_COUNT", "0") or "0")
    os.environ[f"GIT_CONFIG_KEY_{n}"]   = f"url.https://{cred}@{host}/.insteadOf"
    os.environ[f"GIT_CONFIG_VALUE_{n}"] = f"https://{host}/"
    os.environ["GIT_CONFIG_COUNT"]      = str(n + 1)
    print(f"[git] auth configured for https://{host}/ as {user or '(token)'}",
          file=sys.stderr)


# Load .env and configure git auth at load time so everything that shells out to
# git inherits it -- terravision's module clones AND terraform init when run direct.
_dotenv = find_dotenv(usecwd=True)
if _dotenv:
    load_dotenv(_dotenv)   # override=False: real environment variables still win
    print(f"[env] loaded {_dotenv}", file=sys.stderr)
else:
    print("[env] no .env found", file=sys.stderr)
configure_git_auth()


# ----------------------------------------------------------------------------
# 1. terravision: run graphdata per repo
# ----------------------------------------------------------------------------
def run_terravision(terraform_root, workdir, jobs=4):
    """
    Run `terravision graphdata` for each repo under ~/terraform/<repo>, up to
    `jobs` at a time, and return {repo: json_path}.  Each repo runs its own
    `terraform init/plan` (slow, mostly I/O-bound), so they overlap.  Output is
    inherited, not captured, so concurrent runs interleave on the terminal.
    """
    # Share downloaded providers across terravision's per-repo temp TF_DATA_DIRs
    # so terraform stops re-downloading the (large) AWS provider every run.
    cache = os.environ.setdefault("TF_PLUGIN_CACHE_DIR",
                                  os.path.expanduser("~/.terraform.d/plugin-cache"))
    os.makedirs(cache, exist_ok=True)
    os.makedirs(workdir, exist_ok=True)
    repos = [d for d in glob.glob(os.path.join(os.path.expanduser(terraform_root), "*"))
             if os.path.isdir(d)]

    def _one(repo):
        name = os.path.basename(repo.rstrip("/"))
        dst = os.path.join(workdir, f"{name}.json")
        print(f"[terravision] {name}: starting", file=sys.stderr, flush=True)
        try:
            rc = subprocess.run(["terravision", "graphdata",
                                 "--source", repo, "--outfile", dst]).returncode
        except OSError as e:
            print(f"[terravision] {name}: could not launch terravision ({e})",
                  file=sys.stderr, flush=True)
            return name, dst, False
        ok = rc == 0 and os.path.exists(dst)
        print(f"[terravision] {name}: {'done' if ok else f'FAILED (exit {rc})'}",
              file=sys.stderr, flush=True)
        return name, dst, ok

    out = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, jobs)) as ex:
        for fut in concurrent.futures.as_completed(
                ex.submit(_one, r) for r in sorted(repos)):
            name, dst, ok = fut.result()
            if ok:
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
# 2. repo -> account attribution  (derived; no manual map)
# ----------------------------------------------------------------------------
BACKEND_S3  = re.compile(r'backend\s+"s3"\s*\{(.*?)\}', re.DOTALL)
BACKEND_KEY = re.compile(r'key\s*=\s*"([^"]+)"')
UNRESOLVED  = "unresolved"   # cluster for repos whose backend key matched no state

def _state_basename_index(tfstates_root):
    """{state_file_basename: set(account_numbers)} from ~/tfstates/<acct>/*.tfstate."""
    index = defaultdict(set)
    root = os.path.expanduser(tfstates_root)
    if not os.path.isdir(root):
        print(f"  WARN tfstates root not found: {root}", file=sys.stderr)
        return index
    for acct in sorted(os.listdir(root)):
        adir = os.path.join(root, acct)
        if not os.path.isdir(adir):
            continue
        for sf in glob.glob(os.path.join(adir, "**", "*.tfstate"), recursive=True):
            if sf.endswith(".backup"):
                continue
            index[os.path.basename(sf)].add(acct)
    return index

def derive_repo_accounts(terraform_root, tfstates_root):
    """
    Map each repo under ~/terraform/<repo> to its AWS account(s) with no manual
    input: read the S3 backend `key` from the repo's .tf files, then match that
    key's basename to a state file under ~/tfstates/<accountnumber>/.  The
    account is the directory the matching state file lives in.

    Returns {repo: [account_number, ...]} -- normally one account; >1 only for a
    genuine multi-backend repo (or a state filename shared across accounts).
    """
    state_index = _state_basename_index(tfstates_root)
    repo_map = {}
    troot = os.path.expanduser(terraform_root)
    for repo_dir in sorted(glob.glob(os.path.join(troot, "*"))):
        if not os.path.isdir(repo_dir):
            continue
        repo = os.path.basename(repo_dir.rstrip("/"))
        accts = set()
        for tf in glob.glob(os.path.join(repo_dir, "**", "*.tf"), recursive=True):
            try:
                text = open(tf).read()
            except Exception:
                continue
            for block in BACKEND_S3.findall(text):
                km = BACKEND_KEY.search(block)
                if km:
                    accts |= state_index.get(os.path.basename(km.group(1)), set())
        if accts:
            repo_map[repo] = sorted(accts)
    return repo_map

# ----------------------------------------------------------------------------
# 3. merge into one account-grouped graph
# ----------------------------------------------------------------------------
def merge(graphdicts, repo_map):
    """
    Combine the per-repo terravision graphs into one account-grouped graph.

    Account assignment is directory-driven, the tfgraph.py way: a repo's S3
    backend points at exactly one state directory (~/tfstates/<account>/), so
    every resource that repo produces is placed in that account -- no per-resource
    ARN guessing.  A repo with no backend-key match is kept under the 'unresolved'
    cluster, never dropped.  global_id = "<account>/<repo>/<address>".
    """
    nodes, edges = {}, []
    for repo, (gd, _md) in graphdicts.items():
        accts = repo_map.get(repo) or [UNRESOLVED]
        if accts == [UNRESOLVED]:
            print(f"  WARN repo '{repo}' not matched to an account; placing under "
                  f"'{UNRESOLVED}'", file=sys.stderr)
        for acct in accts:   # normally one; >1 only for a genuine multi-backend repo
            for addr, conns in gd.items():
                g = f"{acct}/{repo}/{addr}"
                if g not in nodes:
                    nodes[g] = {"label": addr, "type": addr.split(".")[0],
                                "account": acct}
                for dst in conns:
                    edges.append((g, f"{acct}/{repo}/{dst}"))
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
        if acct == UNRESOLVED:
            acct_label = "Unresolved (no state/ARN match)"
        elif nm:
            acct_label = f"{nm} ({acct})"
        else:
            acct_label = f"AWS Account {acct}"
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
    ap.add_argument("--graphdir", help="dir of pre-generated per-repo json; skips terravision")
    ap.add_argument("--workdir", default="./_graphdata")
    ap.add_argument("-j", "--jobs", type=int, default=1, metavar="N",
                    help="parallel terravision runs (default: 1 = serial)")
    ap.add_argument("--no-state", action="store_true", help="skip cross-account edge layer")
    ap.add_argument("--out", default="org.vdx")
    args = ap.parse_args()

    repo_map = derive_repo_accounts(args.terraform_root, args.tfstates_root)
    print(f"repos resolved to accounts: {len(repo_map)}", file=sys.stderr)

    if args.graphdir:
        graphdicts = load_graphdicts(args.graphdir)
    else:
        run_terravision(args.terraform_root, args.workdir, args.jobs)
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
    missing = [a for a in accounts if a not in acct_names and a != UNRESOLVED]
    if missing:
        print("  no name for: " + ", ".join(missing), file=sys.stderr)

    xedges = [] if args.no_state else cross_account_edges(args.tfstates_root, accounts)
    print(f"cross_account_edges={len(xedges)}", file=sys.stderr)

    build_vdx(nodes, edges, xedges, args.out, acct_names)
    print(f"wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
