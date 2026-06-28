import itertools
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, "/home/tripps/Projects/terravision-vsdx")
os.chdir("/home/tripps/Projects/terravision-vsdx")

import modules.drawing as drawing
import modules.tfwrapper as tfwrapper
import modules.vsdx_emitter as vsdx
from modules.provider_detector import get_primary_provider_or_default
from modules.xdot_parser import XdotEdge, parse_xdot, run_xdot

OUTDIR = Path("/tmp/opencode/edgefix3")
OUTDIR.mkdir(parents=True, exist_ok=True)

FIXTURES = {
    "aws": "tests/json/bastion-tfdata.json",
    "azure": "tests/json/azure-aks-tfdata.json",
    "gcp": "tests/json/gcp-three-tier-webapp-tfdata.json",
}

results = {}

def emit_from_fixture(name: str, fixture: str, *, complete: bool = False) -> dict:
    tfdata = tfwrapper.load_json_source(fixture)
    _diagram, _provider, postdot = drawing._build_diagram(
        tfdata,
        f"edgefix3-{name}",
        fixture,
        outformat="dot",
        show=False,
        announce_render=False,
    )
    graph = parse_xdot(run_xdot(str(postdot)))
    if complete:
        icon_nodes = [
            nid
            for nid, node in graph.nodes.items()
            if not vsdx._is_pseudo_node(node)
            and (getattr(node, "attrs", {}) or {}).get("_clusterlabel") != "1"
            and vsdx._resolve_node_png(node, "aws")
        ]
        icon_nodes.sort()
        graph.edges = [XdotEdge(source=a, target=b) for a, b in itertools.combinations(icon_nodes, 2)]
    else:
        icon_nodes = []
    try:
        provider = get_primary_provider_or_default(tfdata)
    except Exception:
        provider = "aws"
    outfile = OUTDIR / f"{name}.vsdx"
    path = vsdx.emit_vsdx(
        graph,
        tfdata.get("node_id_map", {}),
        tfdata.get("cluster_id_map", {}),
        provider=provider,
        outfile=str(outfile),
    )
    return {
        "fixture": fixture,
        "vsdx": path,
        "provider": provider,
        "nodes": len(graph.nodes),
        "clusters": len(graph.clusters),
        "edges": len(graph.edges),
        "complete_icon_nodes": len(icon_nodes),
    }

for name, fixture in FIXTURES.items():
    results[name] = emit_from_fixture(name, fixture)
results["stress"] = emit_from_fixture("stress-complete-aws", FIXTURES["aws"], complete=True)

summary_path = OUTDIR / "generation.json"
summary_path.write_text(json.dumps(results, indent=2, sort_keys=True))
print(summary_path)
print(json.dumps(results, indent=2, sort_keys=True))
