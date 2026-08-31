import time
from network_topology import (
    build_network_graph,
    calculate_shortest_path,
    get_topology_health,
    visualize_topology,
)
from traffic_engine import train_traffic_predictor, update_dynamic_weights

def run_simulation():
    print("=" * 60)
    print("      FlowRider: AI-Driven SDN Traffic Control Engine")
    print("=" * 60)

    # 1. Initialize Network Topology and AI Model
    print("\n[1/5] Initializing network graph & training Random Forest model...")
    graph = build_network_graph()
    ml_model = train_traffic_predictor()
    print("      ✔ Graph initialized (6 Routers, 7 Links)")
    print("      ✔ ML Traffic Regressor trained on historical telemetry")

    # 2. Baseline Route (Normal Traffic Conditions)
    src, dst = "Router_1", "Router_4"
    print(f"\n[2/5] Calculating baseline path ({src} -> {dst})...")
    base_route = calculate_shortest_path(graph, src, dst)
    print(f"      ► Path: {' -> '.join(base_route['path'])}")
    print(f"      ► Total Latency: {base_route['total_latency_ms']} ms")
    print(f"      ► Bottleneck Bandwidth: {base_route['bottleneck_capacity_mbps']} Mbps")

    # 3. Simulate Congestion Spike on High-Speed Backbone
    print("\n[3/5] Injecting traffic surge on backbone link (Router_2 <-> Router_5)...")
    graph["Router_2"]["Router_5"]["current_load"] = 1900  # 1900 / 2000 Mbps = 95% utilization
    print("      ⚠ Backbone link operating at 95% saturation!")

    # 4. Trigger Machine Learning Control Plane Update
    print("\n[4/5] Running AI weight recalculation & proactive reroute...")
    update_dynamic_weights(graph, ml_model, current_step=12)
    
    new_route = calculate_shortest_path(graph, src, dst)
    print(f"      ► Proactive Reroute Path: {' -> '.join(new_route['path'])}")
    print(f"      ► Adjusted Path Latency: {new_route['total_latency_ms']} ms")

    # 5. Print Network Health Telemetry & Plot Topology Map
    print("\n[5/5] Generating network health snapshot...")
    health = get_topology_health(graph)
    print(f"      ► Overall Network Utilization: {health['overall_utilization_pct']}%")
    print(f"      ► Average Link Latency: {health['avg_link_latency_ms']} ms")
    print(f"      ► Severely Congested Links (≥80%): {health['congested_links']}")
    print(f"      ► Full Mesh Connectivity: {health['fully_connected']}")

    print("\nRendering live topology visual map...")
    visualize_topology(graph)

if __name__ == "__main__":
    run_simulation()