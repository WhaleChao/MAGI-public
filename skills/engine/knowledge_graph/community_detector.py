from __future__ import annotations

from typing import Dict, List

import networkx as nx


def detect_communities(graph: nx.DiGraph) -> List[Dict[str, object]]:
    if graph.number_of_nodes() == 0:
        return []

    undirected = graph.to_undirected()
    try:
        import igraph as ig
        import leidenalg

        nodes = list(undirected.nodes())
        index = {node: idx for idx, node in enumerate(nodes)}
        ig_graph = ig.Graph()
        ig_graph.add_vertices(len(nodes))
        ig_graph.vs["name"] = nodes
        ig_graph.add_edges([(index[a], index[b]) for a, b in undirected.edges()])
        partition = leidenalg.find_partition(ig_graph, leidenalg.ModularityVertexPartition)
        communities = []
        for community_id, membership in enumerate(partition):
            members = [nodes[idx] for idx in membership]
            communities.append({"community_id": community_id, "members": members, "size": len(members)})
        return communities
    except Exception:
        communities = []
        for community_id, members in enumerate(nx.connected_components(undirected)):
            member_list = sorted(members)
            communities.append({"community_id": community_id, "members": member_list, "size": len(member_list)})
        return communities
