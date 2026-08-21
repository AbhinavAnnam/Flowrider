import numpy as np
import networkx as nx
from sklearn.ensemble import RandomForestRegressor

def generate_historical_telemetry(samples=100):
    """Simulates historical link utilization percentage (0.0 to 1.0)."""
    np.random.seed(42)
    time_steps = np.arange(samples)
    
    # Simulate utilization wave (0% to 100%)
    base_util = 0.4 + 0.3 * np.sin(time_steps / 5.0)
    bursts = np.random.choice([0.0, 0.35], size=samples, p=[0.8, 0.2])
    noise = np.random.normal(0, 0.05, size=samples)
    
    utilization_series = np.clip(base_util + bursts + noise, 0.05, 0.98)
    
    X = np.column_stack((time_steps[:-1], utilization_series[:-1]))
    y = utilization_series[1:]
    
    return X, y

def train_traffic_predictor():
    """Trains regressor to predict next-step link utilization ratio."""
    X, y = generate_historical_telemetry()
    model = RandomForestRegressor(n_estimators=50, random_state=42)
    model.fit(X, y)
    return model

def update_dynamic_weights(G, model, current_step):
    """Predicts utilization ratio and applies exponential weight penalty."""
    for u, v, data in G.edges(data=True):
        current_load = data['current_load']
        capacity = data['capacity']
        base_delay = data['base_delay']
        
        # Calculate live utilization ratio
        current_util = min(current_load / capacity, 1.0)
        
        # ML Prediction for near-future utilization ratio
        features = np.array([[current_step, current_util]])
        predicted_util = float(model.predict(features)[0])
        predicted_util = np.clip(predicted_util, 0.0, 1.0)
        
        # Dynamic Weight: Exponential penalty scaled heavily above 80% saturation
        dynamic_weight = base_delay * (1 + (predicted_util ** 4) * 25)
        
        G[u][v]['predicted_load'] = round(predicted_util * capacity, 2)
        G[u][v]['latency'] = round(dynamic_weight, 2)

if __name__ == "__main__":
    from network_topology import build_network_graph, calculate_shortest_path
    
    graph = build_network_graph()
    ml_model = train_traffic_predictor()
    
    # 1. Baseline route BEFORE congestion
    route_before = calculate_shortest_path(graph, "Router_1", "Router_4")
    print("--- BEFORE ML Dynamic Weighting ---")
    print(f"Path: {' -> '.join(route_before['path'])} | Latency: {route_before['total_latency_ms']} ms")
    
    # 2. Simulate 95% congestion spike on the backbone link
    graph["Router_2"]["Router_5"]["current_load"] = 1900 # 1900 / 2000 Mbps = 95%
    
    # 3. Trigger Control Plane ML weight recalculation
    update_dynamic_weights(graph, ml_model, current_step=10)
    
    # 4. Route AFTER ML penalty
    route_after = calculate_shortest_path(graph, "Router_1", "Router_4")
    print("\n--- AFTER ML Dynamic Weighting (Proactive Reroute) ---")
    print(f"Path: {' -> '.join(route_after['path'])} | Latency: {route_after['total_latency_ms']} ms")