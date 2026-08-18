import networkx as nx
import matplotlib.pyplot as plt

def build_network_graph():
    """Initializes the network topology graph with routers and physical links."""

    G=nx.Graph()

    #DEFINING ROUTER NODES 1 THROUGH 6

    routers = [f"Router_{i}" for i in range(1,7)]

    G.add_nodes_from(routers)

    #DEFINING PHYSICAL LINKS: SOURCE, TARGET CAPACITY IN MBPS, BASE LATENCY IN MS

    links = [
        ("Router_1", "Router_2",1000,5),
        ("Router_2", "Router_3",1000,5),
        ("Router_3", "Router_4",1000,5),
        ("Router_4", "Router_5",1000,5),
        ("Router_5", "Router_6",1000,5),
        ("Router_6", "Router_1",1000,5),
        ("Router_2", "Router_5",2000,2), #HIGH SPEED BACKBONE LINK

    ]

    #STORE NETWROK METRICS INSIDE EDGE ATTRIBUTES

    for u, v,capacity,delay in links:
        G.add_edge(u,v,capacity=capacity,base_delay=delay,current_load=0,latency=delay)

    return G

def visualize_topology(G):
    """Draws a visual plot of the router network structure."""

    pos=nx.spring_layout(G, seed=42)
    plt.figure(figsize=(8,5))

    edge_labels = {
        (u,v): f"{d['current_load']}/{d['capacity']} Mbps"
        for u,v,d in G.edges(data=True)
    }


    nx.draw_networkx_nodes(G,pos,node_color="#007ACC",node_size=1500)
    nx.draw_networkx_labels(G,pos,font_color="white",font_weight="bold")
    nx.draw_networkx_edges(G,pos,edge_color="#666666",width=2)
    nx.draw_networkx_edge_labels(G,pos,edge_labels=edge_labels,font_size=8)

    plt.title("FlowRider - Network Topology Map")
    plt.axis("off")
    plt.show()

def calculate_shortest_path(G, source, target, weight_metric="latency"):
    """Computes optimal router path using Dijkstra's algorithm based on live latency/weights."""
    try:
        path = nx.shortest_path(G, source=source, target=target, weight=weight_metric)
        
        total_path_latency = 0.0
        bottleneck_capacity = float('inf')
        
        # Walk step-by-step through the path links to aggregate cost and bottleneck bandwidth
        for i in range(len(path) - 1):
            u, v = path[i], path[i + 1]
            edge_data = G[u][v]
            total_path_latency += edge_data.get(weight_metric, edge_data['base_delay'])
            available_bw = edge_data['capacity'] - edge_data['current_load']
            bottleneck_capacity = min(bottleneck_capacity, available_bw)
            
        return {
            "path": path,
            "total_latency_ms": round(total_path_latency, 2),
            "bottleneck_capacity_mbps": max(0, bottleneck_capacity),
            "hop_count": len(path) - 1
        }
    except nx.NetworkXNoPath:
        return None  # Triggered when no physical path exists between routers


def set_link_status(G, u, v, active=True):
    """Simulates physical cable severing or recovery for fault-tolerance testing."""
    if G.has_edge(u, v):
        G[u][v]['active'] = active
        if not active:
            # Assign infinite penalty so shortest-path algorithms route around it
            G[u][v]['latency'] = float('inf')


def get_topology_health(G):
    """Generates snapshot metrics of overall network load, bottlenecks, and connectivity."""
    total_load = sum(d['current_load'] for u, v, d in G.edges(data=True))
    total_capacity = sum(d['capacity'] for u, v, d in G.edges(data=True))
    active_latencies = [d['latency'] for u, v, d in G.edges(data=True) if d['latency'] != float('inf')]
    
    congested_count = sum(1 for u, v, d in G.edges(data=True) if (d['current_load'] / d['capacity']) >= 0.80)
    
    return {
        "overall_utilization_pct": round((total_load / total_capacity) * 100, 2) if total_capacity else 0.0,
        "avg_link_latency_ms": round(sum(active_latencies) / len(active_latencies), 2) if active_latencies else 0.0,
        "congested_links": congested_count,
        "fully_connected": nx.is_connected(G)
    }


if __name__ == "__main__":
    graph = build_network_graph()
    
    # Test pathfinding from Router_1 to Router_4
    route_info = calculate_shortest_path(graph, "Router_1", "Router_4")
    print(f"Optimal Route: {' -> '.join(route_info['path'])}")
    print(f"Total Path Latency: {route_info['total_latency_ms']} ms")
    
    # Test network health check
    health = get_topology_health(graph)
    print(f"Network Health: {health}")
    
    visualize_topology(graph)