#!/usr/bin/env python3
"""
tfgraph.py - Build one pruned, account-clustered dependency graph from
local Terraform state + source repos. Emits Graphviz DOT, then you render
with: dot -Tsvg infra.dot -o infra.svg

NO network. NO terraform calls. Pure stdlib. Reads raw .tfstate JSON.

Assumes:
  states at  STATES_DIR/<accountid>/*.tfstate
  repos at   REPOS_DIR/<repo>/**/*.tf   (backend "s3" blocks map repo->state)

Edit the CONFIG block, copy to the device, run:  python3 tfgraph.py
"""

import json, os, re, sys, glob, html
from collections import defaultdict

# ============================ CONFIG ============================
STATES_DIR = os.path.expanduser("~/terraform/tfstates")
REPOS_DIR  = os.path.expanduser("~/terraform/repos")
OUT_DOT    = "infra.dot"

# Anchor resource types to KEEP. Everything else is pruned.
# Edit freely. Matching is by exact terraform type string.
KEEP_TYPES = {
    "aws_vpc",
    "aws_subnet",
    "aws_route_table",
    "aws_internet_gateway",
    "aws_nat_gateway",
    "aws_ec2_transit_gateway",
    "aws_ec2_transit_gateway_vpc_attachment",
    "aws_ec2_transit_gateway_peering_attachment",
    "aws_vpc_peering_connection",
    "aws_security_group",          # groups, not rules
    "aws_iam_role",                # roles, not policy attachments
    "aws_s3_bucket",
    "aws_db_instance",
    "aws_rds_cluster",
    "aws_lb",
    "aws_alb",
    "aws_elb",
    "aws_ecs_cluster",
    "aws_eks_cluster",
    "aws_lambda_function",
    "aws_instance",
    "aws_cloudfront_distribution",
    "aws_api_gateway_rest_api",
    "aws_dynamodb_table",
    "aws_sns_topic",
    "aws_sqs_queue",
    "aws_kms_key",
    "aws_efs_file_system",
}

# Resource types whose ARNs/ids are worth matching across accounts for
# cross-account edges. Usually a subset of KEEP_TYPES that get referenced
# from other accounts. Empty set => match all kept resources' ids.
CROSS_ACCOUNT_MATCH_TYPES = set()  # empty = match all kept ids
# ===============================================================


# ---------- state loading ----------
def load_states(states_dir):
    """Return list of dicts: {account, path, data}."""
    out = []
    pattern = os.path.join(states_dir, "*", "*.tfstate")
    for path in glob.glob(pattern):
        account = os.path.basename(os.path.dirname(path))
        try:
            with open(path) as fh:
                data = json.load(fh)
        except Exception as e:
            sys.stderr.write(f"WARN: skip {path}: {e}\n")
            continue
        out.append({"account": account, "path": path,
                    "key": os.path.basename(path), "data": data})
    return out


# ---------- backend mapping (repo -> state key) ----------
BACKEND_RE = re.compile(r'backend\s+"s3"\s*\{(.*?)\}', re.DOTALL)
BUCKET_RE  = re.compile(r'bucket\s*=\s*"([^"]+)"')
KEY_RE     = re.compile(r'key\s*=\s*"([^"]+)"')

def map_repos(repos_dir):
    """
    Return dict: (basename_of_key) -> repo_name, plus raw list for debugging.
    Keyed on the state-file basename so it matches load_states()['key'].
    """
    mapping = {}
    for repo in sorted(os.listdir(repos_dir)):
        repo_path = os.path.join(repos_dir, repo)
        if not os.path.isdir(repo_path):
            continue
        for tf in glob.glob(os.path.join(repo_path, "**", "*.tf"), recursive=True):
            try:
                text = open(tf).read()
            except Exception:
                continue
            for block in BACKEND_RE.findall(text):
                km = KEY_RE.search(block)
                if not km:
                    continue
                key_basename = os.path.basename(km.group(1))
                mapping.setdefault(key_basename, repo)
    return mapping


# ---------- resource extraction ----------
def iter_resources(state):
    """Yield (type, name, module, instances[]) for resources in a state dict."""
    for res in state.get("resources", []):
        if res.get("mode") != "managed":
            continue
        yield (res.get("type", ""),
               res.get("name", ""),
               res.get("module", "root"),
               res.get("instances", []))


def node_id(account, rtype, rname, idx):
    raw = f"{account}__{rtype}__{rname}__{idx}"
    return re.sub(r'[^A-Za-z0-9_]', '_', raw)


def collect(states, repo_map):
    """
    Build:
      nodes: id -> {label, account, module, repo, rtype}
      intra_edges: list of (src_id, dst_id)
      id_index: arn/id string -> node_id   (for cross-account matching)
      ref_strings: node_id -> set(strings found in its attributes)
    """
    nodes = {}
    intra_edges = []
    id_index = {}
    ref_strings = defaultdict(set)

    for st in states:
        account = st["account"]
        repo = repo_map.get(st["key"], "")
        for rtype, rname, module, instances in iter_resources(st["data"]):
            if rtype not in KEEP_TYPES:
                continue
            for idx, inst in enumerate(instances):
                nid = node_id(account, rtype, rname, idx)
                attrs = inst.get("attributes", {}) or {}
                label = f"{rtype}\\n{rname}"
                nodes[nid] = {"label": label, "account": account,
                              "module": module or "root", "repo": repo,
                              "rtype": rtype}

                # index this resource's own identifiers for cross-account match
                for idkey in ("arn", "id"):
                    val = attrs.get(idkey)
                    if isinstance(val, str) and len(val) > 8:
                        if (not CROSS_ACCOUNT_MATCH_TYPES) or (rtype in CROSS_ACCOUNT_MATCH_TYPES):
                            id_index.setdefault(val, nid)

                # collect string-ish attribute values for reference detection
                for v in _walk_strings(attrs):
                    if len(v) > 8:
                        ref_strings[nid].add(v)

                # intra-state dependency edges from terraform's own metadata
                for dep in inst.get("dependencies", []) or []:
                    intra_edges.append((nid, ("DEP", account, dep)))

    return nodes, intra_edges, id_index, ref_strings


def _walk_strings(obj):
    if isinstance(obj, str):
        yield obj
    elif isinstance(obj, dict):
        for v in obj.values():
            yield from _walk_strings(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk_strings(v)


def resolve_intra_edges(nodes, intra_edges):
    """
    intra_edges currently hold ('DEP', account, dependency_address).
    Terraform 'dependencies' are addresses like 'aws_vpc.main'. Resolve to
    any node in the same account whose type+name prefix matches.
    """
    # build address index per account: 'type.name' -> [node_ids]
    addr_index = defaultdict(list)
    for nid, meta in nodes.items():
        # node label first line is 'type', second is 'name'
        rtype = meta["rtype"]
        rname = meta["label"].split("\\n")[1] if "\\n" in meta["label"] else ""
        addr_index[(meta["account"], f"{rtype}.{rname}")].append(nid)

    resolved = set()
    for src, dst in intra_edges:
        if not (isinstance(dst, tuple) and dst[0] == "DEP"):
            continue
        _, account, dep = dst
        # dep may be 'module.x.aws_vpc.main' etc; take last type.name pair
        m = re.search(r'([a-z0-9_]+\.[A-Za-z0-9_\-]+)$', dep)
        if not m:
            continue
        for tgt in addr_index.get((account, m.group(1)), []):
            if tgt != src:
                resolved.add((src, tgt))
    return resolved


def cross_account_edges(nodes, id_index, ref_strings):
    """
    If node B's attributes contain a string that is node A's arn/id (A in a
    different account), draw A -> B as a cross-account edge.
    """
    edges = set()
    for nid, strings in ref_strings.items():
        b_account = nodes[nid]["account"]
        for s in strings:
            owner = id_index.get(s)
            if owner and owner != nid:
                if nodes[owner]["account"] != b_account:
                    edges.add((owner, nid))
    return edges


# ---------- DOT emission ----------
def emit_dot(nodes, intra, cross, out_path):
    # group nodes account -> module -> [nid]
    tree = defaultdict(lambda: defaultdict(list))
    for nid, meta in nodes.items():
        tree[meta["account"]][meta["module"]].append(nid)

    lines = []
    lines.append('digraph infra {')
    lines.append('  rankdir=LR;')
    lines.append('  compound=true;')
    lines.append('  node [shape=box, style="rounded,filled", '
                 'fillcolor="#eef3fb", fontname="Helvetica", fontsize=10];')
    lines.append('  graph [fontname="Helvetica", fontsize=12];')
    lines.append('  edge [color="#888888"];')

    for account, modules in sorted(tree.items()):
        repo_label = ""
        # find any repo name attached to nodes in this account
        for m, nids in modules.items():
            for nid in nids:
                if nodes[nid]["repo"]:
                    repo_label = nodes[nid]["repo"]
                    break
            if repo_label:
                break
        acc_title = f"account {account}" + (f"  ({repo_label})" if repo_label else "")
        lines.append(f'  subgraph "cluster_acc_{account}" {{')
        lines.append(f'    label="{html.escape(acc_title)}";')
        lines.append('    style="filled"; fillcolor="#f7f9fc"; color="#3b5b92";')

        for module, nids in sorted(modules.items()):
            safe_mod = re.sub(r'[^A-Za-z0-9_]', '_', module)
            if module and module != "root":
                lines.append(f'    subgraph "cluster_{account}_{safe_mod}" {{')
                lines.append(f'      label="{html.escape(module)}";')
                lines.append('      style="filled"; fillcolor="#eef3fb"; color="#7a93c2";')
                indent = "      "
            else:
                indent = "    "
            for nid in sorted(nids):
                lbl = html.escape(nodes[nid]["label"]).replace("&#x27;", "'")
                lines.append(f'{indent}"{nid}" [label="{lbl}"];')
            if module and module != "root":
                lines.append('    }')
        lines.append('  }')

    for src, dst in sorted(intra):
        if src in nodes and dst in nodes:
            lines.append(f'  "{src}" -> "{dst}";')

    for src, dst in sorted(cross):
        lines.append(f'  "{src}" -> "{dst}" '
                     '[color="#cc3333", penwidth=2, constraint=false];')

    lines.append('}')
    with open(out_path, "w") as fh:
        fh.write("\n".join(lines))


def main():
    states = load_states(STATES_DIR)
    if not states:
        sys.exit(f"No state files under {STATES_DIR}/<account>/*.tfstate")
    repo_map = map_repos(REPOS_DIR) if os.path.isdir(REPOS_DIR) else {}

    nodes, intra_raw, id_index, ref_strings = collect(states, repo_map)
    intra = resolve_intra_edges(nodes, intra_raw)
    cross = cross_account_edges(nodes, id_index, ref_strings)

    emit_dot(nodes, intra, cross, OUT_DOT)

    # summary to stderr (no infra details leave the box; this is just counts)
    n_acc = len({m["account"] for m in nodes.values()})
    sys.stderr.write(
        f"states: {len(states)}  repos_mapped: {len(repo_map)}\n"
        f"kept nodes: {len(nodes)}  accounts: {n_acc}\n"
        f"intra edges: {len(intra)}  cross-account edges: {len(cross)}\n"
        f"wrote {OUT_DOT}\n"
        f"render: dot -Tsvg {OUT_DOT} -o infra.svg\n"
    )


if __name__ == "__main__":
    main()
